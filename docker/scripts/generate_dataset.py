#!/usr/bin/env python3
"""Generate benchmark datasets for ServeIt Studio.

Usage:
    generate_dataset.py --model MODEL --isl ISL --osl OSL --seed SEED --rows ROWS --output PATH [--mode random|cache|corpus] [--hit-pct PCT] [--isl-stdev S] [--osl-stdev S]

Modes:
    random        — synthetic word-salad prompts (fast, tokenizer-aware)
    cache         — shared prefix with hit_pct cache hits
    prefix_group  — N groups with shared prefix + unique suffix
    corpus        — contiguous prose windows from bundled wikitext-103 corpus;
                    meaningful text for speculative decoding acceptance measurement
"""
import argparse
import json
import multiprocessing
import os
import random
import sys
import time


def _load_tokenizer(model_name):
    """Load tokenizer and extract vocab. Returns (tokenizer, vocab) or (None, None)."""
    try:
        from transformers import AutoTokenizer
        hf_home = os.environ.get('HF_HOME', '/mnt/storage/.cache/huggingface')
        hf_token = os.environ.get('HF_TOKEN')
        tokenizer = AutoTokenizer.from_pretrained(
            model_name, trust_remote_code=True,
            cache_dir=hf_home, token=hf_token
        )
        vocab = [t for t in tokenizer.get_vocab().keys()
                 if len(t) > 2 and t.isascii() and t.isalpha()]
        if len(vocab) < 500:
            return None, None
        return tokenizer, vocab
    except Exception:
        return None, None


def _make_prompt(length_tokens, rng_instance, tokenizer, vocab):
    """Generate a prompt of approximately length_tokens tokens."""
    if vocab and tokenizer:
        target = int(length_tokens * 1.1)
        words = [rng_instance.choice(vocab) for _ in range(target)]
        text = ' '.join(words)
        tokens = tokenizer.encode(text, add_special_tokens=False)
        while len(tokens) < length_tokens:
            extra = length_tokens - len(tokens)
            words = [rng_instance.choice(vocab) for _ in range(extra + 10)]
            text += ' ' + ' '.join(words)
            tokens = tokenizer.encode(text, add_special_tokens=False)
        if len(tokens) > length_tokens:
            text = tokenizer.decode(tokens[:length_tokens], skip_special_tokens=True)
    else:
        words = []
        for _ in range(int(length_tokens * 1.3)):
            wlen = rng_instance.randint(3, 10)
            words.append(''.join(rng_instance.choices('abcdefghijklmnopqrstuvwxyz', k=wlen)))
        text = ' '.join(words)
    return text


def _generate_random_chunk(chunk_args):
    """Generate a chunk of random rows in a worker process."""
    start_idx, count, seed, isl, osl, isl_stdev, osl_stdev, model_name = chunk_args
    pid = os.getpid()
    tokenizer, vocab = _load_tokenizer(model_name)
    print(f"Worker {pid}: generating {count} rows (idx {start_idx}-{start_idx+count-1})...", file=sys.stderr, flush=True)

    rows = []
    for i in range(count):
        rng = random.Random(seed + start_idx + i + 1)
        row_isl = isl + int(rng.random() * isl_stdev) if isl_stdev > 0 else isl
        row_osl = osl + int(rng.random() * osl_stdev) if osl_stdev > 0 else osl
        prompt = _make_prompt(row_isl, rng, tokenizer, vocab)
        rows.append(json.dumps({'prompt': prompt, 'output_tokens_count': row_osl}))
        if (i + 1) % 2000 == 0:
            print(f"Worker {pid}: {i+1}/{count}", file=sys.stderr, flush=True)

    print(f"Worker {pid}: done ({count} rows)", file=sys.stderr, flush=True)
    return rows


def generate_random_parallel(args):
    """Generate random dataset using multiprocessing."""
    num_workers = min(multiprocessing.cpu_count(), 8)
    chunk_size = args.rows // num_workers
    remainder = args.rows % num_workers

    chunks = []
    offset = 0
    for w in range(num_workers):
        n = chunk_size + (1 if w < remainder else 0)
        chunks.append((offset, n, args.seed, args.isl, args.osl, args.isl_stdev, args.osl_stdev, args.model))
        offset += n

    print(f"Using {num_workers} workers for {args.rows} rows...", file=sys.stderr, flush=True)
    t0 = time.time()

    with multiprocessing.Pool(num_workers) as pool:
        results = pool.map(_generate_random_chunk, chunks)

    rows = []
    for chunk_rows in results:
        rows.extend(chunk_rows)

    elapsed = time.time() - t0
    print(f"Generation complete: {len(rows)} rows in {elapsed:.1f}s ({len(rows)/max(elapsed,0.1):.0f} rows/s)", file=sys.stderr, flush=True)
    return rows


_SHARED_CORPUS = None  # Global for fork-inherited corpus tokens

def _generate_cache_chunk(chunk_args):
    """Generate a chunk of cache dataset rows in a worker process."""
    start_idx, count, seed, isl, osl, isl_stdev, osl_stdev, model_name, shared_prompt, hit_count, unique_count, use_corpus = chunk_args
    pid = os.getpid()
    tokenizer, vocab = _load_tokenizer(model_name)
    corpus_tokens = _SHARED_CORPUS if use_corpus else None
    print(f"Worker {pid}: generating {count} cache rows ({hit_count} hits, {unique_count} unique, ISL {isl}+{isl_stdev})...", file=sys.stderr, flush=True)

    if tokenizer:
        shared_toks = tokenizer.encode(shared_prompt, add_special_tokens=False)
    else:
        shared_toks = None
    max_isl = isl + (int(isl_stdev) if isl_stdev > 0 else 0)

    rows = []
    for i in range(hit_count):
        rng = random.Random(seed + start_idx + i)
        row_isl = isl + int(rng.random() * isl_stdev) if isl_stdev > 0 else isl
        row_osl = osl + int(rng.random() * osl_stdev) if osl_stdev > 0 else osl
        if isl_stdev > 0 and row_isl < max_isl:
            if shared_toks is not None:
                prompt = tokenizer.decode(shared_toks[:row_isl], skip_special_tokens=True)
            else:
                words = shared_prompt.split()
                cut = max(1, int(len(words) * row_isl / max_isl))
                prompt = ' '.join(words[:cut])
        else:
            prompt = shared_prompt
        rows.append(json.dumps({'prompt': prompt, 'output_tokens_count': row_osl}))

    for i in range(unique_count):
        rng = random.Random(seed + start_idx + hit_count + i + 1)
        row_isl = isl + int(rng.random() * isl_stdev) if isl_stdev > 0 else isl
        row_osl = osl + int(rng.random() * osl_stdev) if osl_stdev > 0 else osl
        if corpus_tokens is not None and tokenizer:
            offset = ((start_idx + hit_count + i) * max_isl * 3) % max(1, len(corpus_tokens) - row_isl)
            prompt = _corpus_window(tokenizer, corpus_tokens, offset, row_isl)
            if not prompt:
                prompt = _make_prompt(row_isl, rng, tokenizer, vocab)
        else:
            prompt = _make_prompt(row_isl, rng, tokenizer, vocab)
        rows.append(json.dumps({'prompt': prompt, 'output_tokens_count': row_osl}))
        if (i + 1) % 2000 == 0:
            print(f"Worker {pid}: {i+1}/{unique_count} unique rows", file=sys.stderr, flush=True)

    print(f"Worker {pid}: done ({count} rows)", file=sys.stderr, flush=True)
    return rows


def generate_cache_parallel(args):
    """Generate cache dataset using multiprocessing."""
    num_workers = min(multiprocessing.cpu_count(), 8)
    max_isl = args.isl + (int(args.isl_stdev) if args.isl_stdev > 0 else 0)

    if getattr(args, 'use_corpus', False):
        needed = min(5_000_000, max_isl * min(args.rows, 500) * 2)
        corpus_tok, corpus_tokens = _load_corpus_tokens(args.model, needed)
        shared_prompt = _corpus_window(corpus_tok, corpus_tokens, 0, max_isl)
        if not shared_prompt:
            print("ERROR: could not cut shared prompt from corpus", file=sys.stderr, flush=True)
            sys.exit(1)
        print(f"Shared prompt from corpus ({len(shared_prompt)} chars, {max_isl} tokens)", file=sys.stderr, flush=True)
    else:
        tokenizer, vocab = _load_tokenizer(args.model)
        shared_rng = random.Random(args.seed)
        shared_prompt = _make_prompt(max_isl, shared_rng, tokenizer, vocab)
        print(f"Shared prompt generated ({len(shared_prompt)} chars, max ISL {max_isl})", file=sys.stderr, flush=True)

    total_hits = int(args.rows * args.hit_pct / 100)

    chunk_size = args.rows // num_workers
    remainder = args.rows % num_workers

    uc = getattr(args, 'use_corpus', False)
    # Distribute hits round-robin across workers for exact count
    worker_hits = [0] * num_workers
    worker_sizes = []
    for w in range(num_workers):
        worker_sizes.append(chunk_size + (1 if w < remainder else 0))
    for i in range(total_hits):
        worker_hits[i % num_workers] += 1
    # Clamp hits to worker size
    for w in range(num_workers):
        worker_hits[w] = min(worker_hits[w], worker_sizes[w])

    chunks = []
    offset = 0
    for w in range(num_workers):
        n = worker_sizes[w]
        wh = worker_hits[w]
        wu = n - wh
        chunks.append((offset, n, args.seed, args.isl, args.osl, args.isl_stdev, args.osl_stdev, args.model, shared_prompt, wh, wu, uc))
        offset += n

    global _SHARED_CORPUS
    if uc:
        _SHARED_CORPUS = corpus_tokens
    print(f"Using {num_workers} workers for {args.rows} rows...", file=sys.stderr, flush=True)
    t0 = time.time()

    with multiprocessing.Pool(num_workers) as pool:
        results = pool.map(_generate_cache_chunk, chunks)

    _SHARED_CORPUS = None
    rows = []
    for chunk_rows in results:
        rows.extend(chunk_rows)

    random.Random(args.seed + 999).shuffle(rows)

    elapsed = time.time() - t0
    print(f"Generation complete: {len(rows)} rows in {elapsed:.1f}s ({len(rows)/max(elapsed,0.1):.0f} rows/s)", file=sys.stderr, flush=True)
    return rows


def _generate_prefix_group_chunk(chunk_args):
    """Generate a chunk of prefix-group rows: shared prefix + unique suffix per row."""
    start_idx, count, seed, isl, isl_stdev, osl, osl_stdev, prefix_pct, model_name, group_prefixes, use_corpus = chunk_args
    pid = os.getpid()
    tokenizer, vocab = _load_tokenizer(model_name)
    corpus_tokens = _SHARED_CORPUS if use_corpus else None
    num_groups = len(group_prefixes)
    print(f"Worker {pid}: generating {count} prefix-group rows ({num_groups} groups, {prefix_pct}% prefix, ISL {isl}+{isl_stdev})...", file=sys.stderr, flush=True)

    rows = []
    for i in range(count):
        rng = random.Random(seed + start_idx + i + 1)
        row_isl = isl + int(rng.random() * isl_stdev) if isl_stdev > 0 else isl
        row_prefix_tokens = max(1, int(row_isl * prefix_pct / 100))
        row_suffix_tokens = max(1, row_isl - row_prefix_tokens)

        group_idx = (start_idx + i) % num_groups
        full_prefix = group_prefixes[group_idx]

        if tokenizer:
            prefix_toks = tokenizer.encode(full_prefix, add_special_tokens=False)[:row_prefix_tokens]
            prefix = tokenizer.decode(prefix_toks, skip_special_tokens=True)
        else:
            words = full_prefix.split()
            prefix = ' '.join(words[:max(1, int(row_prefix_tokens * 0.8))])

        if corpus_tokens is not None and tokenizer:
            offset = ((start_idx + i) * isl * 3) % max(1, len(corpus_tokens) - row_suffix_tokens)
            suffix = _corpus_window(tokenizer, corpus_tokens, offset, row_suffix_tokens)
            if not suffix:
                suffix = _make_prompt(row_suffix_tokens, rng, tokenizer, vocab)
        else:
            suffix = _make_prompt(row_suffix_tokens, rng, tokenizer, vocab)
        prompt = prefix + '\n' + suffix
        row_osl = osl + int(rng.random() * osl_stdev) if osl_stdev > 0 else osl
        rows.append(json.dumps({'prompt': prompt, 'output_tokens_count': row_osl}))
        if (i + 1) % 2000 == 0:
            print(f"Worker {pid}: {i+1}/{count}", file=sys.stderr, flush=True)

    print(f"Worker {pid}: done ({count} rows)", file=sys.stderr, flush=True)
    return rows


def generate_prefix_group_parallel(args):
    """Generate prefix-group dataset: N groups, each with a shared prefix + unique suffix per row."""
    # Fewer workers for corpus mode — each worker loads a tokenizer (~1-2GB)
    num_workers = min(multiprocessing.cpu_count(), 8)
    num_groups = args.prefix_groups
    prefix_pct = args.hit_pct if args.hit_pct > 0 else 60
    max_isl = args.isl + (int(args.isl_stdev) if args.isl_stdev > 0 else 0)
    max_prefix_tokens = max(1, int(max_isl * prefix_pct / 100))

    shared_corpus_tokens = None
    if getattr(args, 'use_corpus', False):
        # Cap at 5M tokens (~140MB) — workers wrap around for larger datasets
        needed = min(5_000_000, max_isl * min(args.rows, 500) * 2)
        needed = max(needed, max_prefix_tokens * (num_groups + 1) * 3)
        corpus_tok, shared_corpus_tokens = _load_corpus_tokens(args.model, needed)
        print(f"Generating {num_groups} group prefixes from corpus ({max_prefix_tokens} tokens each)...", file=sys.stderr, flush=True)
        group_prefixes = []
        for g in range(num_groups):
            offset = g * max_prefix_tokens * 3
            prefix = _corpus_window(corpus_tok, shared_corpus_tokens, offset, max_prefix_tokens)
            if not prefix:
                tokenizer, vocab = _load_tokenizer(args.model)
                grng = random.Random(args.seed + g * 10000)
                prefix = _make_prompt(max_prefix_tokens, grng, tokenizer, vocab)
            group_prefixes.append(prefix)
    else:
        tokenizer, vocab = _load_tokenizer(args.model)
        print(f"Generating {num_groups} group prefixes ({max_prefix_tokens} max tokens, {prefix_pct}% of ISL)...", file=sys.stderr, flush=True)
        group_prefixes = []
        for g in range(num_groups):
            grng = random.Random(args.seed + g * 10000)
            group_prefixes.append(_make_prompt(max_prefix_tokens, grng, tokenizer, vocab))
    print(f"Generated {num_groups} group prefixes (max {max_prefix_tokens} tokens, ISL range {args.isl}-{max_isl})", file=sys.stderr, flush=True)

    chunk_size = args.rows // num_workers
    remainder = args.rows % num_workers
    chunks = []
    offset = 0
    for w in range(num_workers):
        n = chunk_size + (1 if w < remainder else 0)
        chunks.append((offset, n, args.seed, args.isl, args.isl_stdev, args.osl, args.osl_stdev, prefix_pct, args.model, group_prefixes, getattr(args, 'use_corpus', False)))
        offset += n

    global _SHARED_CORPUS
    if getattr(args, 'use_corpus', False):
        _SHARED_CORPUS = shared_corpus_tokens
    print(f"Using {num_workers} workers for {args.rows} rows...", file=sys.stderr, flush=True)
    t0 = time.time()

    with multiprocessing.Pool(num_workers) as pool:
        results = pool.map(_generate_prefix_group_chunk, chunks)

    _SHARED_CORPUS = None
    rows = []
    for chunk_rows in results:
        rows.extend(chunk_rows)

    random.Random(args.seed + 999).shuffle(rows)

    elapsed = time.time() - t0
    print(f"Generation complete: {len(rows)} rows in {elapsed:.1f}s ({len(rows)/max(elapsed,0.1):.0f} rows/s)", file=sys.stderr, flush=True)
    return rows


CORPUS_PATHS = [
    '/app/corpus/wikitext-103.txt',
    '/mnt/storage/corpus/wikitext-103.txt',
]


def _find_corpus():
    """Find the bundled wikitext-103 corpus file."""
    for path in CORPUS_PATHS:
        if os.path.exists(path):
            return path
    print(f"ERROR: corpus not found at any of: {CORPUS_PATHS}", file=sys.stderr, flush=True)
    sys.exit(1)


def _load_corpus_tokens(model_name, min_tokens):
    """Load and tokenize enough corpus text to provide the requested tokens."""
    from transformers import AutoTokenizer
    hf_home = os.environ.get('HF_HOME', '/mnt/storage/.cache/huggingface')
    hf_token = os.environ.get('HF_TOKEN')
    tokenizer = AutoTokenizer.from_pretrained(
        model_name, trust_remote_code=True,
        cache_dir=hf_home, token=hf_token
    )
    corpus_path = _find_corpus()
    print(f"Loading corpus from {corpus_path}...", file=sys.stderr, flush=True)
    book_tokens = []
    with open(corpus_path, 'r', encoding='utf-8') as f:
        while len(book_tokens) < min_tokens:
            chunk = f.read(10_000_000)
            if not chunk:
                break
            book_tokens.extend(tokenizer.encode(chunk, add_special_tokens=False))
    print(f"Corpus: {len(book_tokens):,} tokens", file=sys.stderr, flush=True)
    return tokenizer, book_tokens


def _corpus_window(tokenizer, book_tokens, start, length):
    """Cut an exact-length window from the tokenized corpus."""
    end = start + length
    for _ in range(8):
        if end <= start or end > len(book_tokens):
            return None
        prompt = tokenizer.decode(
            book_tokens[start:end],
            skip_special_tokens=False,
            clean_up_tokenization_spaces=False,
        )
        actual = len(tokenizer.encode(prompt, add_special_tokens=False))
        if actual == length:
            return prompt
        end += length - actual
    return None


def _corpus_worker(worker_args):
    """Worker: cut windows from the tokenized corpus."""
    worker_id, starts, book_tokens, isl, osl, model_name, isl_stdev, osl_stdev, seed = worker_args
    from transformers import AutoTokenizer
    hf_home = os.environ.get('HF_HOME', '/mnt/storage/.cache/huggingface')
    hf_token = os.environ.get('HF_TOKEN')
    tokenizer = AutoTokenizer.from_pretrained(
        model_name, trust_remote_code=True,
        cache_dir=hf_home, token=hf_token
    )
    print(f"Worker {worker_id}: cutting {len(starts)} windows...", file=sys.stderr, flush=True)

    rows = []
    skipped = 0
    for idx, start in enumerate(starts):
        rng = random.Random(seed + idx)
        row_isl = isl
        if isl_stdev > 0:
            row_isl = max(isl // 4, int(isl + rng.gauss(0, isl_stdev)))
        row_osl = osl
        if osl_stdev > 0:
            row_osl = max(osl // 4, int(osl + rng.gauss(0, osl_stdev)))

        prompt = _corpus_window(tokenizer, book_tokens, start, row_isl)
        if prompt:
            rows.append(json.dumps({
                'prompt': prompt,
                'prompt_tokens_count': row_isl,
                'output_tokens_count': row_osl,
            }))
        else:
            skipped += 1

    print(f"Worker {worker_id}: done ({len(rows)} windows, {skipped} skipped)", file=sys.stderr, flush=True)
    return rows


def generate_corpus(args):
    """Generate dataset from bundled wikitext-103 corpus with evenly-spaced windows."""
    from transformers import AutoTokenizer
    hf_home = os.environ.get('HF_HOME', '/mnt/storage/.cache/huggingface')
    hf_token = os.environ.get('HF_TOKEN')

    print(f"Tokenizing corpus with {args.model}...", file=sys.stderr, flush=True)
    tokenizer = AutoTokenizer.from_pretrained(
        args.model, trust_remote_code=True,
        cache_dir=hf_home, token=hf_token
    )

    # Tokenize in chunks to avoid OOM on the full 500MB corpus.
    # Read only enough text to cover all requested windows.
    # Estimate: ~4 chars per token, need ISL * rows tokens minimum,
    # but windows are spaced across the corpus so read proportionally.
    needed_tokens = args.isl * args.rows * 2  # 2x headroom
    chars_estimate = needed_tokens * 5  # ~5 chars/token conservative

    corpus_path = _find_corpus()
    print(f"Loading corpus from {corpus_path}...", file=sys.stderr, flush=True)
    corpus_size = os.path.getsize(corpus_path)

    book_tokens = []
    with open(corpus_path, 'r', encoding='utf-8') as f:
        chunk_size = 10_000_000  # 10MB chunks
        while len(book_tokens) < needed_tokens:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            book_tokens.extend(tokenizer.encode(chunk, add_special_tokens=False))
            print(f"  tokenized {len(book_tokens):,} tokens...", file=sys.stderr, flush=True)
            if chars_estimate < corpus_size and len(book_tokens) >= needed_tokens:
                break

    print(f"Corpus: {len(book_tokens):,} tokens (read until sufficient)", file=sys.stderr, flush=True)

    if len(book_tokens) < args.isl:
        print(f"ERROR: corpus has only {len(book_tokens)} tokens, need at least {args.isl}", file=sys.stderr, flush=True)
        sys.exit(1)

    max_start = len(book_tokens) - args.isl
    if args.rows == 1:
        starts = [0]
    else:
        starts = [round(i * max_start / (args.rows - 1)) for i in range(args.rows)]

    stride = starts[1] - starts[0] if len(starts) > 1 else max_start
    print(f"Cutting {args.rows} windows (ISL={args.isl}, stride={stride} tokens, overlap={'yes' if stride < args.isl else 'no'})",
          file=sys.stderr, flush=True)

    num_workers = min(multiprocessing.cpu_count(), 8, args.rows)
    chunk_size = args.rows // num_workers
    remainder = args.rows % num_workers

    worker_args = []
    offset = 0
    for w in range(num_workers):
        n = chunk_size + (1 if w < remainder else 0)
        worker_starts = starts[offset:offset + n]
        worker_args.append((w, worker_starts, book_tokens, args.isl, args.osl, args.model, args.isl_stdev, args.osl_stdev, args.seed + offset))
        offset += n

    print(f"Using {num_workers} workers...", file=sys.stderr, flush=True)
    t0 = time.time()

    with multiprocessing.Pool(num_workers) as pool:
        results = pool.map(_corpus_worker, worker_args)

    rows = []
    for chunk_rows in results:
        rows.extend(chunk_rows)

    elapsed = time.time() - t0
    print(f"Generation complete: {len(rows)} windows in {elapsed:.1f}s", file=sys.stderr, flush=True)

    if not rows:
        print("ERROR: no windows generated", file=sys.stderr, flush=True)
        sys.exit(1)

    return rows


def _build_meta(args):
    """Build metadata dict from all generation parameters."""
    return {
        '_meta': True,
        'model': args.model,
        'isl': args.isl,
        'osl': args.osl,
        'seed': args.seed,
        'rows': args.rows,
        'mode': args.mode,
        'hit_pct': args.hit_pct,
        'prefix_tokens': args.prefix_tokens,
        'prefix_groups': args.prefix_groups,
        'isl_stdev': args.isl_stdev,
        'osl_stdev': args.osl_stdev,
        'use_corpus': getattr(args, 'use_corpus', False),
    }


def _load_meta(path):
    """Read metadata from the first line of a JSONL dataset."""
    with open(path, 'r') as f:
        first = f.readline().strip()
        if first:
            meta = json.loads(first)
            if meta.get('_meta'):
                return meta
    return None


def main():
    parser = argparse.ArgumentParser(description='Generate benchmark dataset')
    parser.add_argument('--model', default=None, help='HuggingFace model name')
    parser.add_argument('--isl', type=int, default=None, help='Input sequence length')
    parser.add_argument('--osl', type=int, default=None, help='Output sequence length')
    parser.add_argument('--seed', type=int, default=None, help='Random seed')
    parser.add_argument('--rows', type=int, default=None, help='Number of rows')
    parser.add_argument('--output', required=True, help='Output JSONL path')
    parser.add_argument('--mode', default='random', choices=['random', 'cache', 'prefix_group', 'corpus'], help='Dataset mode')
    parser.add_argument('--hit-pct', type=int, default=100, help='Cache hit percentage (cache mode)')
    parser.add_argument('--prefix-tokens', type=int, default=0, help='Shared prefix length in tokens (prefix_group mode)')
    parser.add_argument('--prefix-groups', type=int, default=10, help='Number of prefix groups (prefix_group mode)')
    parser.add_argument('--isl-stdev', type=float, default=0, help='ISL standard deviation')
    parser.add_argument('--osl-stdev', type=float, default=0, help='OSL standard deviation')
    parser.add_argument('--use-corpus', action='store_true', default=False, help='Use real prose from bundled corpus')
    parser.add_argument('--reproduce', type=str, default=None, help='Base64 workload seed or path to existing dataset file')
    args = parser.parse_args()

    # Reproduce mode: decode base64 seed or read metadata from file
    if args.reproduce:
        meta = None
        # Try base64 decode first
        try:
            import base64
            decoded = base64.b64decode(args.reproduce).decode('utf-8')
            meta = json.loads(decoded)
            print("Reproducing from seed", file=sys.stderr, flush=True)
        except Exception:
            pass
        # Fall back to file
        if not meta and os.path.isfile(args.reproduce):
            meta = _load_meta(args.reproduce)
            if meta:
                print(f"Reproducing from file: {args.reproduce}", file=sys.stderr, flush=True)
        if not meta:
            print(f"ERROR: invalid seed or file: {args.reproduce}", file=sys.stderr, flush=True)
            sys.exit(1)
        for key in ['model', 'isl', 'osl', 'seed', 'rows', 'mode', 'hit_pct',
                     'prefix_tokens', 'prefix_groups', 'isl_stdev', 'osl_stdev', 'use_corpus']:
            if meta.get(key) is not None:
                setattr(args, key.replace('-', '_'), meta.get(key))
        # Infer mode from seed if not explicitly set
        if args.mode == 'random' and meta.get('hit_pct') and meta['hit_pct'] > 0:
            if meta.get('prefix_cache_mode') == 'multi_group' or meta.get('structured_prefix'):
                args.mode = 'prefix_group'
            else:
                args.mode = 'cache'
        elif args.mode == 'random' and meta.get('use_corpus') and not meta.get('hit_pct'):
            args.mode = 'corpus'
        if not args.seed:
            import hashlib
            seed_input = ':'.join(str(meta.get(k, '')) for k in ['model','isl','osl','isl_stdev','osl_stdev','mode','hit_pct'])
            args.seed = int(hashlib.sha256(seed_input.encode()).hexdigest()[:8], 16)
        if not args.rows:
            args.rows = 100000
        print(f"Config: model={args.model}, ISL={args.isl}, OSL={args.osl}, seed={args.seed}, "
              f"mode={args.mode}, rows={args.rows}, corpus={args.use_corpus}", file=sys.stderr, flush=True)

    if not args.model or not args.isl or not args.osl or args.seed is None or not args.rows:
        parser.error("--model, --isl, --osl, --seed, and --rows are required (or use --reproduce)")

    print(f"Generating {args.mode} dataset: {args.rows} rows, ISL={args.isl}, OSL={args.osl}, seed={args.seed}", file=sys.stderr, flush=True)

    if args.mode == 'random':
        rows = generate_random_parallel(args)
    elif args.mode == 'corpus':
        rows = generate_corpus(args)
    elif args.mode == 'prefix_group':
        if args.hit_pct <= 0:
            args.hit_pct = 60
        rows = generate_prefix_group_parallel(args)
    else:
        rows = generate_cache_parallel(args)

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    meta_line = json.dumps(_build_meta(args))
    with open(args.output, 'w') as f:
        f.write(meta_line + '\n')
        f.write('\n'.join(rows) + '\n')

    size_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"Generated {args.output} ({size_mb:.1f} MB, {len(rows)} rows)", file=sys.stderr, flush=True)
    print(f"Reproduce with: generate_dataset --reproduce {args.output} --output <new_path>", file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
