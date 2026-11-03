"""Pure-Python parser for pprof (Google CPU/heap profile) files.

The pprof format is a gzipped protobuf-encoded `Profile` message
(perftools.profiles.Profile). This module decodes it without any
external protobuf dependency using a minimal varint wire-format
reader, then computes per-function flat/cumulative sample time.

Used by the profile collector to summarize pod CPU profiles for the
ServeIt report (top-N functions, total CPU, sample rate).
"""

import gzip
import struct

# Wire types
WT_VARINT = 0
WT_FIXED64 = 1
WT_LEN = 2
WT_FIXED32 = 5


def _read_varint(buf, pos):
    """Read a base-128 varint from buf at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError('truncated varint')
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError('varint too long')


def _read_packed_varints(buf, pos, end):
    """Read a length-delimited run of varints (repeated packed field)."""
    vals = []
    while pos < end:
        v, pos = _read_varint(buf, pos)
        vals.append(v)
    return vals


def _skip_field(buf, pos, wire_type):
    """Skip a field of the given wire type. Returns new position."""
    if wire_type == WT_VARINT:
        _, pos = _read_varint(buf, pos)
        return pos
    if wire_type == WT_FIXED64:
        return pos + 8
    if wire_type == WT_LEN:
        length, pos = _read_varint(buf, pos)
        return pos + length
    if wire_type == WT_FIXED32:
        return pos + 4
    raise ValueError(f'unsupported wire type {wire_type}')


def _iter_fields(buf):
    """Yield (field_number, wire_type, value) for each field in a message.

    For WT_LEN fields the value is (pos, end) into buf; the caller can
    slice buf[pos:end] or iterate it recursively as a sub-message.
    """
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == WT_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == WT_FIXED64:
            value = struct.unpack_from('<Q', buf, pos)[0]
            pos += 8
        elif wire_type == WT_LEN:
            length, pos = _read_varint(buf, pos)
            value = (pos, pos + length)
            pos += length
        elif wire_type == WT_FIXED32:
            value = struct.unpack_from('<I', buf, pos)[0]
            pos += 4
        else:
            raise ValueError(f'unsupported wire type {wire_type}')
        yield field_num, wire_type, value


# ── pprof message field numbers (perftools.profiles proto) ──────────────────
# Profile:    1 sample_type, 2 sample, 3 mapping, 4 location, 5 function,
#             6 string_table, 7 drop_frames, 8 keep_frames, 9 time_nanos,
#             10 duration_nanos, 11 period_type, 12 period, 13 comment,
#             14 default_sample_type
# ValueType:  1 type (string idx), 2 unit (string idx)
# Sample:     1 location_id (uint64, repeated), 2 value (int64, repeated)
# Location:   1 id, 2 mapping_id, 3 address, 4 line (repeated), 5 is_folded
# Line:       1 function_id, 2 line, 3 column
# Function:   1 id, 2 name, 3 system_name, 4 filename, 5 start_line


class _PprofProfile:
    __slots__ = ('string_table', 'functions', 'locations', 'samples',
                 'duration_nanos', 'period', 'period_type', 'sample_types',
                 'time_nanos')

    def __init__(self):
        self.string_table = []
        self.functions = {}       # id -> name
        self.locations = {}       # id -> [function names] (leaf first)
        self.samples = []         # [(location_ids, values)]
        self.duration_nanos = 0
        self.period = 0
        self.period_type = None   # (type_idx, unit_idx)
        self.sample_types = []    # [(type_idx, unit_idx)]
        self.time_nanos = 0


def _parse_value_type(buf):
    type_idx = unit_idx = 0
    for num, wt, val in _iter_fields(buf):
        if num == 1:
            type_idx = val
        elif num == 2:
            unit_idx = val
    return type_idx, unit_idx


def _parse_sample(buf):
    location_ids = []
    values = []
    for num, wt, val in _iter_fields(buf):
        if num == 1:  # location_id — packed or single
            if wt == WT_LEN:
                s, e = val
                location_ids.extend(_read_packed_varints(buf, s, e))
            else:
                location_ids.append(val)
        elif num == 2:  # value — packed or single
            if wt == WT_LEN:
                s, e = val
                values.extend(_read_packed_varints(buf, s, e))
            else:
                values.append(val)
    return location_ids, values


def _parse_line(buf):
    function_id = 0
    for num, wt, val in _iter_fields(buf):
        if num == 1:
            function_id = val
    return function_id


def _parse_location(buf, functions):
    loc_id = 0
    fn_ids = []
    for num, wt, val in _iter_fields(buf):
        if num == 1:
            loc_id = val
        elif num == 4:  # line
            s, e = val
            fid = _parse_line(buf[s:e])
            if fid:
                fn_ids.append(fid)
    return loc_id, fn_ids


def _parse_function(buf):
    fn_id = name_idx = 0
    for num, wt, val in _iter_fields(buf):
        if num == 1:
            fn_id = val
        elif num == 2:
            name_idx = val
    return fn_id, name_idx


def parse_pprof(data):
    """Parse raw (optionally gzipped) pprof bytes into a _PprofProfile."""
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    prof = _PprofProfile()
    for num, wt, val in _iter_fields(data):
        if num == 1:  # sample_type
            s, e = val
            prof.sample_types.append(_parse_value_type(data[s:e]))
        elif num == 2:  # sample
            s, e = val
            prof.samples.append(_parse_sample(data[s:e]))
        elif num == 4:  # location
            s, e = val
            loc_id, fn_ids = _parse_location(data[s:e], prof.functions)
            prof.locations[loc_id] = fn_ids
        elif num == 5:  # function
            s, e = val
            fn_id, name_idx = _parse_function(data[s:e])
            prof.functions[fn_id] = name_idx
        elif num == 6:  # string_table
            s, e = val
            prof.string_table.append(data[s:e].decode('utf-8', 'replace'))
        elif num == 9:  # time_nanos
            prof.time_nanos = val
        elif num == 10:  # duration_nanos
            prof.duration_nanos = val
        elif num == 11:  # period_type
            s, e = val
            prof.period_type = _parse_value_type(data[s:e])
        elif num == 12:  # period
            prof.period = val
    return prof


def _fn_name(prof, fn_id):
    idx = prof.functions.get(fn_id)
    if idx is None:
        return '<unknown>'
    return prof.string_table[idx] if idx < len(prof.string_table) else '<unknown>'


def _loc_fns(prof, loc_id):
    """Function names for a location, leaf frame first (pprof order)."""
    fn_ids = prof.locations.get(loc_id, [])
    return [_fn_name(prof, fid) for fid in fn_ids]


def summarize_cpu_profile(data, top_n=25):
    """Summarize a CPU pprof profile.

    Returns dict: {duration_s, cpu_time_ms, cpu_pct, sample_count,
                   period_type, functions: [{name, flat_ms, cum_ms,
                   flat_pct, cum_pct}]} sorted by cumulative time.
    """
    prof = parse_pprof(data)

    # Find the CPU-time (or samples) sample type index. Go CPU profiles use
    # sample_type "samples"/"count" with period_type "cpu"/"nanoseconds",
    # value[0] = sample count, value[1] = nanoseconds. Python pprof middleware
    # emits similar. We prefer the nanoseconds column when present.
    nanos_idx = None
    samples_idx = 0
    for i, (t_idx, u_idx) in enumerate(prof.sample_types):
        tname = prof.string_table[t_idx] if t_idx < len(prof.string_table) else ''
        uname = prof.string_table[u_idx] if u_idx < len(prof.string_table) else ''
        if 'nanoseconds' in uname or tname in ('cpu', 'time', 'CPU'):
            nanos_idx = i
        if tname in ('samples', 'sample_count') or uname == 'count':
            samples_idx = i

    duration_s = prof.duration_nanos / 1e9 if prof.duration_nanos else 0.0

    flat = {}   # function name -> nanoseconds (leaf frame)
    cum = {}    # function name -> nanoseconds (any frame in stack)
    total_nanos = 0
    total_samples = 0

    for location_ids, values in prof.samples:
        if not location_ids:
            continue
        nanos = 0
        if nanos_idx is not None and nanos_idx < len(values):
            nanos = values[nanos_idx]
        elif len(values) > 1:
            nanos = values[1]  # common layout: [count, nanos]
        elif values:
            # No time column: convert sample counts via period (ns/sample)
            period = prof.period if prof.period else 1
            nanos = values[0] * period
        total_nanos += nanos
        total_samples += values[samples_idx] if samples_idx < len(values) else 1

        seen_in_stack = set()
        for li, loc_id in enumerate(location_ids):
            for fn in _loc_fns(prof, loc_id):
                cum[fn] = cum.get(fn, 0) + nanos
                if li == 0 and fn not in seen_in_stack:
                    # attribute flat time to the leaf frame's functions
                    flat[fn] = flat.get(fn, 0) + nanos
                seen_in_stack.add(fn)

    total_ms = total_nanos / 1e6
    functions = []
    for fn in cum:
        f_ms = flat.get(fn, 0) / 1e6
        c_ms = cum[fn] / 1e6
        functions.append({
            'name': fn,
            'flat_ms': round(f_ms, 2),
            'cum_ms': round(c_ms, 2),
            'flat_pct': round(100 * f_ms / (total_ms / 1000) / 1000, 2) if total_ms else 0,
            'cum_pct': round(100 * c_ms / (total_ms / 1000) / 1000, 2) if total_ms else 0,
        })
    functions.sort(key=lambda f: f['cum_ms'], reverse=True)

    cpu_pct = round(100 * total_nanos / prof.duration_nanos, 2) if prof.duration_nanos else None

    period_type_str = ''
    if prof.period_type:
        t_idx, u_idx = prof.period_type
        tname = prof.string_table[t_idx] if t_idx < len(prof.string_table) else ''
        uname = prof.string_table[u_idx] if u_idx < len(prof.string_table) else ''
        period_type_str = f'{tname}/{uname}'

    return {
        'duration_s': round(duration_s, 1),
        'cpu_time_ms': round(total_ms, 1),
        'cpu_pct': cpu_pct,
        'sample_count': total_samples,
        'period_type': period_type_str,
        'functions': functions[:top_n],
        'function_count': len(functions),
    }


def summarize_profile_file(path, top_n=25):
    """Load a pprof file from disk and summarize it."""
    with open(path, 'rb') as f:
        return summarize_cpu_profile(f.read(), top_n=top_n)
