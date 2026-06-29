#!/usr/bin/env python3
"""Generate benchmark datasets for ServeIt Studio.

Usage:
    generate_dataset.py --model MODEL --isl ISL --osl OSL --seed SEED --rows ROWS --output PATH [--mode random|cache] [--hit-pct PCT] [--isl-stdev S] [--osl-stdev S]
"""
import argparse
import json
import os
import random
import sys


def build_prompt_maker(model_name, use_tokenizer=True):
    """Build a prompt generation function using the model tokenizer if available."""
    vocab = None
    tokenizer = None
    if use_tokenizer:
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
                vocab = None
            print(f"Using model tokenizer ({len(vocab or [])} vocab words)", file=sys.stderr)
        except Exception as e:
            print(f"Tokenizer not available ({e}), using random words", file=sys.stderr)
    else:
        print(f"Skipping tokenizer (fast mode)", file=sys.stderr)

    def make_prompt(length_tokens, rng_instance):
        if vocab and tokenizer and use_tokenizer:
            words = [rng_instance.choice(vocab) for _ in range(length_tokens * 2)]
            text = ' '.join(words)
            tokens = tokenizer.encode(text, add_special_tokens=False)
            if len(tokens) > length_tokens:
                text = tokenizer.decode(tokens[:length_tokens], skip_special_tokens=True)
        elif vocab:
            words = [rng_instance.choice(vocab) for _ in range(length_tokens)]
            text = ' '.join(words)
        else:
            words = []
            for _ in range(int(length_tokens * 1.3)):
                wlen = rng_instance.randint(3, 10)
                words.append(''.join(rng_instance.choices('abcdefghijklmnopqrstuvwxyz', k=wlen)))
            text = ' '.join(words)
        return text

    return make_prompt


def _generate_chunk(chunk_args):
    """Generate a chunk of random rows (for multiprocessing)."""
    start_idx, count, seed, isl, osl, isl_stdev, osl_stdev, model_name, use_tokenizer = chunk_args
    make_prompt = build_prompt_maker(model_name, use_tokenizer=use_tokenizer)
    rows = []
    for i in range(count):
        idx = start_idx + i
        rng = random.Random(seed + idx + 1)
        row_isl = max(10, int(rng.gauss(isl, isl_stdev))) if isl_stdev > 0 else isl
        row_osl = max(1, int(rng.gauss(osl, osl_stdev))) if osl_stdev > 0 else osl
        prompt = make_prompt(row_isl, rng)
        rows.append(json.dumps({'prompt': prompt, 'output_tokens_count': row_osl}))
        if (i + 1) % 2000 == 0:
            print(f"   Worker {start_idx//count}: {i+1}/{count} rows...", file=sys.stderr)
    return rows


def generate_random(args, make_prompt):
    """Generate dataset with unique random prompts (0% cache hit) using multiprocessing."""
    import multiprocessing
    num_workers = min(multiprocessing.cpu_count(), 4)
    chunk_size = args.rows // num_workers
    remainder = args.rows % num_workers

    chunks = []
    offset = 0
    for w in range(num_workers):
        n = chunk_size + (1 if w < remainder else 0)
        use_tok = True  # each worker loads its own tokenizer
        chunks.append((offset, n, args.seed, args.isl, args.osl, args.isl_stdev, args.osl_stdev, args.model, use_tok))
        offset += n

    print(f"   Using {num_workers} workers...", file=sys.stderr)
    with multiprocessing.Pool(num_workers) as pool:
        results = pool.map(_generate_chunk, chunks)

    rows = []
    for chunk_rows in results:
        rows.extend(chunk_rows)
    return rows


def generate_cache(args, make_prompt):
    """Generate dataset with controlled prefix cache hit ratio."""
    rng = random.Random(args.seed)
    shared_prompt = make_prompt(args.isl, rng)

    rows = []
    hit_count = int(args.rows * args.hit_pct / 100)
    unique_count = args.rows - hit_count

    for i in range(hit_count):
        osl = max(1, int(random.Random(args.seed + i).gauss(args.osl, args.osl_stdev))) if args.osl_stdev > 0 else args.osl
        rows.append(json.dumps({'prompt': shared_prompt, 'output_tokens_count': osl}))

    for i in range(unique_count):
        row_rng = random.Random(args.seed + hit_count + i + 1)
        row_isl = max(10, int(row_rng.gauss(args.isl, args.isl_stdev))) if args.isl_stdev > 0 else args.isl
        row_osl = max(1, int(row_rng.gauss(args.osl, args.osl_stdev))) if args.osl_stdev > 0 else args.osl
        prompt = make_prompt(row_isl, row_rng)
        rows.append(json.dumps({'prompt': prompt, 'output_tokens_count': row_osl}))

    random.Random(args.seed + 999).shuffle(rows)

    if (len(rows)) % 5000 == 0 or len(rows) == args.rows:
        print(f"   {len(rows)}/{args.rows} rows generated...", file=sys.stderr)
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

    print(f"Generating {args.mode} dataset: {args.rows} rows, ISL={args.isl}, OSL={args.osl}, seed={args.seed}", file=sys.stderr)

    make_prompt = build_prompt_maker(args.model)

    if args.mode == 'random':
        rows = generate_random(args, make_prompt)
    else:
        rows = generate_cache(args, make_prompt)

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    with open(args.output, 'w') as f:
        f.write('\n'.join(rows) + '\n')

    size_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"Generated {args.output} ({size_mb:.1f} MB, {len(rows)} rows)", file=sys.stderr)


if __name__ == '__main__':
    main()
