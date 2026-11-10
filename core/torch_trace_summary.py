#!/usr/bin/env python3
"""Streaming Chrome-trace parser for torch profiler output.

Parses .pt.trace.json.gz files (Chrome trace format) WITHOUT loading them
into memory: streams the gunzipped bytes, extracts events incrementally
with json.JSONDecoder.raw_decode, and aggregates per-name statistics.

Emits a compact JSON summary (a few KB):
  total_events, kernel_count, total_cpu_ms, total_cuda_ms,
  top_kernels (by CUDA time), top_cpu_ops (by CPU time), categories

Runs inside vLLM pods (or the server) where the trace files live — the raw
traces (30-80MB each) are never copied anywhere.

Usage:
  python3 torch_trace_summary.py <trace.json.gz> [--top N]
  python3 torch_trace_summary.py <dir>            # parses rank* first
"""

import gzip
import json
import sys
from collections import Counter


def iter_events(path, chunk_size=1 << 20):
    """Yield Chrome trace events from a .json[.gz] file, streaming.

    Uses raw_decode on a sliding buffer so a 1.3GB decompressed trace
    never exceeds a few MB of resident memory.
    """
    opener = gzip.open if path.endswith(".gz") else open
    decoder = json.JSONDecoder()
    buf = ""
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        # Skip to the traceEvents array
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                return
            buf += chunk
            idx = buf.find('"traceEvents"')
            if idx >= 0:
                # Advance past the opening bracket
                j = buf.find("[", idx)
                if j >= 0:
                    buf = buf[j + 1:]
                    break
                # Bracket not yet buffered — keep the tail after the key
                buf = buf[idx:]
                continue

        pos = 0
        eof = False
        while True:
            # Find the next event start
            while True:
                while pos < len(buf) and buf[pos] not in "{] \t\n\r":
                    pos += 1
                if pos >= len(buf):
                    if eof:
                        return
                    chunk = f.read(chunk_size)
                    if not chunk:
                        eof = True
                        continue
                    buf = buf[pos:] + chunk
                    pos = 0
                    # Trim the buffer periodically to stay small
                    continue
                break
            c = buf[pos]
            if c == "]":
                return  # end of traceEvents
            if c in " \t\n\r":
                pos += 1
                continue

            # Try to decode one event object at pos
            while True:
                try:
                    obj, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    if eof:
                        return
                    chunk = f.read(chunk_size)
                    if not chunk:
                        eof = True
                        continue
                    buf = buf[pos:] + chunk
                    pos = 0
                    # After compaction pos=0 must point at '{' — retry
                    if not buf or buf[0] != "{":
                        break

            if pos >= len(buf) or buf[pos] != "{":
                continue
            yield obj
            pos = end
            # Compact the buffer
            if pos > chunk_size:
                buf = buf[pos:]
                pos = 0


def summarize(path, top_n=15):
    """Aggregate streaming stats for one trace file."""
    cats = Counter()
    kernel_stats = {}   # name -> {us, n}
    cpu_stats = {}      # name -> {us, n}
    total_events = 0
    total_cuda_us = 0
    total_cpu_us = 0
    kernel_count = 0
    ts_min = None
    ts_max = None

    for evt in iter_events(path):
        if evt.get("ph") != "X":
            continue
        total_events += 1
        cat = evt.get("cat") or ""
        name = evt.get("name") or "?"
        dur = evt.get("dur") or 0
        cats[cat] += 1

        # Profile-window wall span (first event start to last event end) —
        # the denominator for honest in-window GPU utilization
        ts = evt.get("ts")
        if ts is not None:
            end = ts + (dur or 0)
            if ts_min is None or ts < ts_min:
                ts_min = ts
            if ts_max is None or end > ts_max:
                ts_max = end

        if cat == "kernel" or cat == "gpu_memcpy" or cat == "gpu_memset":
            total_cuda_us += dur
            kernel_count += 1
            st = kernel_stats.get(name)
            if st is None:
                kernel_stats[name] = {"us": dur, "n": 1}
            else:
                st["us"] += dur
                st["n"] += 1
        elif cat == "cpu_op" or cat == "user_annotation":
            total_cpu_us += dur
            st = cpu_stats.get(name)
            if st is None:
                cpu_stats[name] = {"us": dur, "n": 1}
            else:
                st["us"] += dur
                st["n"] += 1
        elif cat in ("cuda_runtime", "cuda_driver"):
            total_cpu_us += dur
            st = cpu_stats.get(name)
            if st is None:
                cpu_stats[name] = {"us": dur, "n": 1}
            else:
                st["us"] += dur
                st["n"] += 1

    def top(stats, total_us):
        out = []
        for name, st in sorted(stats.items(), key=lambda x: -x[1]["us"])[:top_n]:
            out.append({
                "name": name,
                "total_ms": round(st["us"] / 1000.0, 2),
                "count": st["n"],
                "pct": round(100.0 * st["us"] / total_us, 1) if total_us else 0,
            })
        return out

    window_s = round((ts_max - ts_min) / 1e6, 1) if (ts_min is not None and ts_max is not None and ts_max > ts_min) else None
    # ts span is in microseconds, same unit as total_cuda_us
    gpu_busy_pct = round(100.0 * total_cuda_us / (ts_max - ts_min), 1) if window_s else None

    return {
        "total_events": total_events,
        "kernel_count": kernel_count,
        "total_cuda_ms": round(total_cuda_us / 1000.0, 1),
        "total_cpu_ms": round(total_cpu_us / 1000.0, 1),
        # Wall-clock duration of the profiled window and the GPU duty
        # cycle within it (kernel-execution time / window span). This is
        # the honest utilization figure — NOT cuda_ms / benchmark
        # duration, which mixes a window-limited numerator with a
        # full-test denominator.
        "window_s": window_s,
        "gpu_busy_pct": gpu_busy_pct,
        "top_kernels": top(kernel_stats, total_cuda_us),
        "top_cpu_ops": top(cpu_stats, total_cpu_us),
        "categories": dict(cats.most_common(8)),
    }


def pick_trace(directory):
    """Prefer the rank* (GPU worker) trace in a directory."""
    import os
    best = None
    for f in sorted(os.listdir(directory)):
        if f.endswith(".pt.trace.json.gz"):
            if f.startswith("rank"):
                return os.path.join(directory, f)
            if best is None:
                best = os.path.join(directory, f)
    return best


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/torch_traces"
    top_n = 15
    if "--top" in sys.argv:
        top_n = int(sys.argv[sys.argv.index("--top") + 1])

    import os
    if os.path.isdir(target):
        target = pick_trace(target)
    if not target:
        print("{}", flush=True)
        sys.exit(1)

    summary = summarize(target, top_n)
    print("SUMMARY_JSON:" + json.dumps(summary), flush=True)
