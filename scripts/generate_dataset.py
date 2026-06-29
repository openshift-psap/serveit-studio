#!/usr/bin/env python3
"""Generate benchmark datasets for ServeIt Studio.

Usage:
    generate_dataset.py --model MODEL --isl ISL --osl OSL --seed SEED --rows ROWS --output PATH [--mode random|cache] [--hit-pct PCT] [--isl-stdev S] [--osl-stdev S]
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
        words = [rng_instance.choice(vocab) for _ in range(length_tokens * 2)]
        text = ' '.join(words)
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
        row_isl = max(10, int(rng.gauss(isl, isl_stdev))) if isl_stdev > 0 else isl
        row_osl = max(1, int(rng.gauss(osl, osl_stdev))) if osl_stdev > 0 else osl
        prompt = _make_prompt(row_isl, rng, tokenizer, vocab)
        rows.append(json.dumps({'prompt': prompt, 'output_tokens_count': row_osl}))
        if (i + 1) % 2000 == 0:
            print(f"Worker {pid}: {i+1}/{count}", file=sys.stderr, flush=True)

    print(f"Worker {pid}: done ({count} rows)", file=sys.stderr, flush=True)
    return rows


def generate_random_parallel(args):
    """Generate random dataset using multiprocessing."""
    num_workers = min(multiprocessing.cpu_count(), 4)
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


def _generate_cache_chunk(chunk_args):
    """Generate a chunk of cache dataset rows in a worker process."""
    start_idx, count, seed, isl, osl, isl_stdev, osl_stdev, model_name, shared_prompt, hit_pct = chunk_args
    pid = os.getpid()
    tokenizer, vocab = _load_tokenizer(model_name)
    print(f"Worker {pid}: generating {count} cache rows...", file=sys.stderr, flush=True)

    hit_count = int(count * hit_pct / 100)
    unique_count = count - hit_count

    rows = []
    for i in range(hit_count):
        row_osl = max(1, int(random.Random(seed + start_idx + i).gauss(osl, osl_stdev))) if osl_stdev > 0 else osl
        rows.append(json.dumps({'prompt': shared_prompt, 'output_tokens_count': row_osl}))

    for i in range(unique_count):
        rng = random.Random(seed + start_idx + hit_count + i + 1)
        row_isl = max(10, int(rng.gauss(isl, isl_stdev))) if isl_stdev > 0 else isl
        row_osl = max(1, int(rng.gauss(osl, osl_stdev))) if osl_stdev > 0 else osl
        prompt = _make_prompt(row_isl, rng, tokenizer, vocab)
        rows.append(json.dumps({'prompt': prompt, 'output_tokens_count': row_osl}))
        if (i + 1) % 2000 == 0:
            print(f"Worker {pid}: {i+1}/{unique_count} unique rows", file=sys.stderr, flush=True)

    print(f"Worker {pid}: done ({count} rows)", file=sys.stderr, flush=True)
    return rows


def generate_cache_parallel(args):
    """Generate cache dataset using multiprocessing."""
    num_workers = min(multiprocessing.cpu_count(), 4)

    # Generate shared prompt in main process
    tokenizer, vocab = _load_tokenizer(args.model)
    shared_rng = random.Random(args.seed)
    shared_prompt = _make_prompt(args.isl, shared_rng, tokenizer, vocab)
    print(f"Shared prompt generated ({len(shared_prompt)} chars)", file=sys.stderr, flush=True)

    chunk_size = args.rows // num_workers
    remainder = args.rows % num_workers

    chunks = []
    offset = 0
    for w in range(num_workers):
        n = chunk_size + (1 if w < remainder else 0)
        chunks.append((offset, n, args.seed, args.isl, args.osl, args.isl_stdev, args.osl_stdev, args.model, shared_prompt, args.hit_pct))
        offset += n

    print(f"Using {num_workers} workers for {args.rows} rows...", file=sys.stderr, flush=True)
    t0 = time.time()

    with multiprocessing.Pool(num_workers) as pool:
        results = pool.map(_generate_cache_chunk, chunks)

    rows = []
    for chunk_rows in results:
        rows.extend(chunk_rows)

    random.Random(args.seed + 999).shuffle(rows)

    elapsed = time.time() - t0
    print(f"Generation complete: {len(rows)} rows in {elapsed:.1f}s ({len(rows)/max(elapsed,0.1):.0f} rows/s)", file=sys.stderr, flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description='Generate benchmark dataset')
    parser.add_argument('--model', required=True, help='HuggingFace model name')
    parser.add_argument('--isl', type=int, required=True, help='Input sequence length')
    parser.add_argument('--osl', type=int, required=True, help='Output sequence length')
    parser.add_argument('--seed', type=int, required=True, help='Random seed')
    parser.add_argument('--rows', type=int, required=True, help='Number of rows')
    parser.add_argument('--output', required=True, help='Output JSONL path')
    parser.add_argument('--mode', default='random', choices=['random', 'cache'], help='Dataset mode')
    parser.add_argument('--hit-pct', type=int, default=100, help='Cache hit percentage (cache mode)')
    parser.add_argument('--isl-stdev', type=float, default=0, help='ISL standard deviation')
    parser.add_argument('--osl-stdev', type=float, default=0, help='OSL standard deviation')
    args = parser.parse_args()

    print(f"Generating {args.mode} dataset: {args.rows} rows, ISL={args.isl}, OSL={args.osl}, seed={args.seed}", file=sys.stderr, flush=True)

    if args.mode == 'random':
        rows = generate_random_parallel(args)
    else:
        rows = generate_cache_parallel(args)

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        f.write('\n'.join(rows) + '\n')

    size_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"Generated {args.output} ({size_mb:.1f} MB, {len(rows)} rows)", file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
