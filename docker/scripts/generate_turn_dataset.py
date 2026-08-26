#!/usr/bin/env python3
"""Generate multi-turn conversation dataset using guidellm's SyntheticTextDataset.

Produces a JSONL file compatible with guidellm's file-based dataset loading,
preserving the exact same data format that guidellm generates in-memory.
Uses multiprocessing for parallel generation (up to 8 workers or CPU count).

Each worker generates a subset of conversations with a different seed offset,
ensuring first_prompt_tokens applies correctly to turn 0 of every conversation.

Usage:
    generate_turn_dataset \
        --model RedHatAI/NVIDIA-Nemotron-3-Ultra-550B-A55B-FP8-block \
        --prompt-tokens 1500 --output-tokens 425 \
        --first-prompt-tokens 160000 \
        --prefix-tokens 3000 --prefix-count 1 \
        --turns 540 --rows 100 --seed 42 \
        --output /mnt/storage/datasets/nemotron-agentic.jsonl
"""

import argparse
import json
import multiprocessing
import os
import time


def _build_config(args):
    """Build guidellm SyntheticTextDataArgs from CLI args."""
    from guidellm.data.deserializers.synthetic import (
        SyntheticTextDataArgs,
        SyntheticTextPrefixBucketConfig,
    )

    kwargs = {
        'kind': 'synthetic_text',
        'prompt_tokens': args.prompt_tokens,
        'output_tokens': args.output_tokens,
        'turns': args.turns,
    }
    for cli_attr, config_key in [
        ('prompt_tokens_stdev', 'prompt_tokens_stdev'),
        ('prompt_tokens_min', 'prompt_tokens_min'),
        ('prompt_tokens_max', 'prompt_tokens_max'),
        ('output_tokens_stdev', 'output_tokens_stdev'),
        ('output_tokens_min', 'output_tokens_min'),
        ('output_tokens_max', 'output_tokens_max'),
        ('first_prompt_tokens', 'first_prompt_tokens'),
        ('first_prompt_tokens_stdev', 'first_prompt_tokens_stdev'),
        ('first_prompt_tokens_min', 'first_prompt_tokens_min'),
        ('first_prompt_tokens_max', 'first_prompt_tokens_max'),
        ('first_output_tokens', 'first_output_tokens'),
        ('first_output_tokens_stdev', 'first_output_tokens_stdev'),
    ]:
        val = getattr(args, cli_attr, None)
        if val:
            kwargs[config_key] = val

    if args.prefix_tokens > 0:
        kwargs['prefix_buckets'] = [
            SyntheticTextPrefixBucketConfig(
                bucket_weight=100,
                prefix_count=args.prefix_count,
                prefix_tokens=args.prefix_tokens,
            )
        ]

    return SyntheticTextDataArgs(**kwargs)


def _worker_generate(worker_args):
    """Worker function: generate a chunk of conversations."""
    worker_id, model_name, config_dict, num_rows, start_idx, seed = worker_args

    import sys
    print(f"Worker {worker_id}: generating {num_rows} conversations (idx {start_idx}-{start_idx + num_rows - 1})...",
          file=sys.stderr, flush=True)

    from guidellm.data.deserializers.synthetic import (
        SyntheticTextDataArgs,
        SyntheticTextDataset,
        SyntheticTextPrefixBucketConfig,
    )
    from transformers import AutoTokenizer

    if 'prefix_buckets' in config_dict and config_dict['prefix_buckets']:
        config_dict['prefix_buckets'] = [
            SyntheticTextPrefixBucketConfig(**pb) for pb in config_dict['prefix_buckets']
        ]

    config = SyntheticTextDataArgs(**config_dict)
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    ds = SyntheticTextDataset(config=config, processor=tokenizer, random_seed=seed)

    results = []
    count = 0
    progress_interval = max(num_rows // 5, 1)
    for sample in ds:
        results.append(json.dumps(sample))
        count += 1
        if count % progress_interval == 0:
            print(f"Worker {worker_id}: {count}/{num_rows}", file=sys.stderr, flush=True)
        if count >= num_rows:
            break

    print(f"Worker {worker_id}: done ({count} conversations)", file=sys.stderr, flush=True)
    return results


CORPUS_PATHS = ['/app/corpus/wikitext-103.txt', '/mnt/storage/corpus/wikitext-103.txt']


def _find_corpus():
    for path in CORPUS_PATHS:
        if os.path.exists(path):
            return path
    import sys as _sys
    print("ERROR: corpus not found", file=_sys.stderr, flush=True)
    _sys.exit(1)


def _corpus_window(tokenizer, tokens, start, length):
    """Cut an exact-length window from tokenized corpus."""
    end = start + length
    for _ in range(8):
        if end <= start or end > len(tokens):
            return None
        text = tokenizer.decode(tokens[start:end], skip_special_tokens=False, clean_up_tokenization_spaces=False)
        actual = len(tokenizer.encode(text, add_special_tokens=False))
        if actual == length:
            return text
        end += length - actual
    return None


def _corpus_turn_worker(worker_args):
    """Worker: build conversations from corpus windows."""
    worker_id, num_rows, start_idx, model_name, prompt_tokens, output_tokens, turns, \
        first_prompt_tokens, prefix_text, corpus_tokens_slice_start, corpus_tokens, stdev_args = worker_args

    import sys, random
    from transformers import AutoTokenizer
    print(f"Worker {worker_id}: building {num_rows} conversations...", file=sys.stderr, flush=True)

    hf_home = os.environ.get('HF_HOME', '/mnt/storage/.cache/huggingface')
    hf_token = os.environ.get('HF_TOKEN')
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True, cache_dir=hf_home, token=hf_token)

    isl_stdev = stdev_args.get('isl_stdev', 0) or 0
    isl_min = stdev_args.get('isl_min')
    isl_max = stdev_args.get('isl_max')
    osl_stdev = stdev_args.get('osl_stdev', 0) or 0
    osl_min = stdev_args.get('osl_min')
    osl_max = stdev_args.get('osl_max')
    fp_stdev = stdev_args.get('fp_stdev', 0) or 0
    fp_min = stdev_args.get('fp_min')
    fp_max = stdev_args.get('fp_max')

    def _vary(base, stdev, lo, hi, rng):
        if not stdev:
            return base
        val = int(base + rng.gauss(0, stdev))
        if lo is not None:
            val = max(val, lo)
        else:
            val = max(val, base // 4)
        if hi is not None:
            val = min(val, hi)
        return max(10, val)

    results = []
    offset = corpus_tokens_slice_start
    for row in range(num_rows):
        rng = random.Random(start_idx + row)
        conversation = []
        for t in range(turns):
            if t == 0 and first_prompt_tokens:
                isl = _vary(first_prompt_tokens, fp_stdev, fp_min, fp_max, rng)
            else:
                isl = _vary(prompt_tokens, isl_stdev, isl_min, isl_max, rng)
            osl = _vary(output_tokens, osl_stdev, osl_min, osl_max, rng)

            prompt = _corpus_window(tokenizer, corpus_tokens, offset, isl)
            if not prompt:
                offset = 0
                prompt = _corpus_window(tokenizer, corpus_tokens, offset, isl)
            if not prompt:
                print(f"Worker {worker_id}: could not cut {isl}-token window, skipping", file=sys.stderr, flush=True)
                break

            if prefix_text and t == 0:
                prompt = prefix_text + '\n' + prompt

            conversation.append({
                'prompt': prompt,
                'prompt_tokens_count': isl + (len(tokenizer.encode(prefix_text, add_special_tokens=False)) if prefix_text and t == 0 else 0),
                'output_tokens_count': osl,
            })
            offset += isl + prompt_tokens

        if conversation:
            results.append(json.dumps({'conversation_turns': conversation}))

        if (row + 1) % max(num_rows // 5, 1) == 0:
            print(f"Worker {worker_id}: {row + 1}/{num_rows}", file=sys.stderr, flush=True)

    print(f"Worker {worker_id}: done ({len(results)} conversations)", file=sys.stderr, flush=True)
    return results


def generate_corpus_turns(args):
    """Generate multi-turn conversations using corpus text."""
    import sys
    from transformers import AutoTokenizer

    hf_home = os.environ.get('HF_HOME', '/mnt/storage/.cache/huggingface')
    hf_token = os.environ.get('HF_TOKEN')

    first_isl = args.first_prompt_tokens or args.prompt_tokens
    tokens_per_conv = first_isl + (args.turns - 1) * args.prompt_tokens
    needed = tokens_per_conv * args.rows * 2

    print(f"Corpus mode: {args.turns} turns, first={first_isl}, rest={args.prompt_tokens}, ~{tokens_per_conv} tokens/conv", file=sys.stderr, flush=True)

    corpus_path = _find_corpus()
    print(f"Loading corpus from {corpus_path}...", file=sys.stderr, flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True, cache_dir=hf_home, token=hf_token)

    corpus_tokens = []
    with open(corpus_path, 'r', encoding='utf-8') as f:
        while len(corpus_tokens) < needed:
            chunk = f.read(10_000_000)
            if not chunk:
                break
            corpus_tokens.extend(tokenizer.encode(chunk, add_special_tokens=False))
            print(f"  tokenized {len(corpus_tokens):,} tokens...", file=sys.stderr, flush=True)
    print(f"Corpus: {len(corpus_tokens):,} tokens", file=sys.stderr, flush=True)

    prefix_text = None
    if args.prefix_tokens > 0:
        prefix_text = _corpus_window(tokenizer, corpus_tokens, 0, args.prefix_tokens)
        if prefix_text:
            print(f"Shared prefix from corpus: {args.prefix_tokens} tokens", file=sys.stderr, flush=True)

    num_workers = min(multiprocessing.cpu_count(), 8, args.rows)
    chunk_size = args.rows // num_workers
    remainder = args.rows % num_workers

    stdev_args = {
        'isl_stdev': args.prompt_tokens_stdev,
        'isl_min': args.prompt_tokens_min,
        'isl_max': args.prompt_tokens_max,
        'osl_stdev': args.output_tokens_stdev,
        'osl_min': args.output_tokens_min,
        'osl_max': args.output_tokens_max,
        'fp_stdev': args.first_prompt_tokens_stdev,
        'fp_min': args.first_prompt_tokens_min,
        'fp_max': args.first_prompt_tokens_max,
    }

    worker_args = []
    start_idx = 0
    corpus_offset = args.prefix_tokens * 2 if prefix_text else 0
    for w in range(num_workers):
        n = chunk_size + (1 if w < remainder else 0)
        worker_args.append((
            w, n, start_idx, args.model, args.prompt_tokens, args.output_tokens,
            args.turns, args.first_prompt_tokens, prefix_text,
            corpus_offset, corpus_tokens, stdev_args
        ))
        corpus_offset += n * tokens_per_conv * 2
        start_idx += n

    print(f"Using {num_workers} workers for {args.rows} conversations...", file=sys.stderr, flush=True)
    start = time.time()

    with multiprocessing.Pool(num_workers) as pool:
        chunks = pool.map(_corpus_turn_worker, worker_args)

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    total = 0
    with open(args.output, 'w') as f:
        f.write(json.dumps(_build_turn_meta(args)) + '\n')
        for chunk in chunks:
            for line in chunk:
                f.write(line + '\n')
                total += 1

    elapsed = time.time() - start
    file_size = os.path.getsize(args.output)
    print(f"Generation complete: {total} conversations in {elapsed:.1f}s ({file_size / 1024 / 1024:.1f} MB)", file=sys.stderr, flush=True)
    print(f"Reproduce with: generate_turn_dataset --reproduce {args.output} --output <new_path>", file=sys.stderr, flush=True)


def _build_turn_meta(args):
    """Build metadata dict from all multi-turn generation parameters."""
    return {
        '_meta': True,
        'type': 'multi_turn',
        'model': args.model,
        'prompt_tokens': args.prompt_tokens,
        'output_tokens': args.output_tokens,
        'prompt_tokens_stdev': args.prompt_tokens_stdev,
        'prompt_tokens_min': args.prompt_tokens_min,
        'prompt_tokens_max': args.prompt_tokens_max,
        'output_tokens_stdev': args.output_tokens_stdev,
        'output_tokens_min': args.output_tokens_min,
        'output_tokens_max': args.output_tokens_max,
        'first_prompt_tokens': args.first_prompt_tokens,
        'first_prompt_tokens_stdev': args.first_prompt_tokens_stdev,
        'first_prompt_tokens_min': args.first_prompt_tokens_min,
        'first_prompt_tokens_max': args.first_prompt_tokens_max,
        'first_output_tokens': args.first_output_tokens,
        'first_output_tokens_stdev': args.first_output_tokens_stdev,
        'prefix_tokens': args.prefix_tokens,
        'prefix_count': args.prefix_count,
        'turns': args.turns,
        'rows': args.rows,
        'seed': args.seed,
        'use_corpus': getattr(args, 'use_corpus', False),
    }


def main():
    parser = argparse.ArgumentParser(description='Generate multi-turn conversation dataset')
    parser.add_argument('--model', default=None, help='HuggingFace model for tokenizer')
    parser.add_argument('--prompt-tokens', type=int, default=None, help='Average prompt tokens per turn')
    parser.add_argument('--output-tokens', type=int, default=None, help='Average output tokens per turn')
    parser.add_argument('--prompt-tokens-stdev', type=int, default=0)
    parser.add_argument('--prompt-tokens-min', type=int, default=None)
    parser.add_argument('--prompt-tokens-max', type=int, default=None)
    parser.add_argument('--output-tokens-stdev', type=int, default=0)
    parser.add_argument('--output-tokens-min', type=int, default=None)
    parser.add_argument('--output-tokens-max', type=int, default=None)
    parser.add_argument('--first-prompt-tokens', type=int, default=None, help='First turn prompt override')
    parser.add_argument('--first-prompt-tokens-stdev', type=int, default=None)
    parser.add_argument('--first-prompt-tokens-min', type=int, default=None)
    parser.add_argument('--first-prompt-tokens-max', type=int, default=None)
    parser.add_argument('--first-output-tokens', type=int, default=None)
    parser.add_argument('--first-output-tokens-stdev', type=int, default=None)
    parser.add_argument('--prefix-tokens', type=int, default=0, help='Shared system prompt tokens')
    parser.add_argument('--prefix-count', type=int, default=1, help='Number of unique prefixes')
    parser.add_argument('--turns', type=int, default=1, help='Turns per conversation')
    parser.add_argument('--rows', type=int, default=100, help='Number of conversations to generate')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--output', required=True, help='Output JSONL file path')
    parser.add_argument('--use-corpus', action='store_true', default=False, help='Use real prose from bundled corpus')
    parser.add_argument('--reproduce', type=str, default=None, help='Path to existing dataset — reproduce from its embedded metadata')
    args = parser.parse_args()

    import sys

    if args.reproduce:
        import base64 as _b64
        meta = None
        try:
            decoded = _b64.b64decode(args.reproduce).decode('utf-8')
            meta = json.loads(decoded)
            print("Reproducing from seed", file=sys.stderr, flush=True)
        except Exception:
            pass
        if not meta and os.path.isfile(args.reproduce):
            with open(args.reproduce, 'r') as f:
                first = f.readline().strip()
            meta = json.loads(first) if first else {}
            if not meta.get('_meta'):
                meta = None
            else:
                print(f"Reproducing from file: {args.reproduce}", file=sys.stderr, flush=True)
        if not meta:
            print(f"ERROR: invalid seed or file: {args.reproduce}", file=sys.stderr, flush=True)
            sys.exit(1)
        # Map seed fields to args (handle both UI seed format and file metadata format)
        field_map = {
            'model': 'model', 'prompt_tokens': 'prompt_tokens', 'output_tokens': 'output_tokens',
            'isl': 'prompt_tokens', 'osl': 'output_tokens',
            'isl_stdev': 'prompt_tokens_stdev', 'osl_stdev': 'output_tokens_stdev',
            'prompt_tokens_stdev': 'prompt_tokens_stdev', 'prompt_tokens_min': 'prompt_tokens_min',
            'prompt_tokens_max': 'prompt_tokens_max', 'output_tokens_stdev': 'output_tokens_stdev',
            'output_tokens_min': 'output_tokens_min', 'output_tokens_max': 'output_tokens_max',
            'first_prompt_tokens': 'first_prompt_tokens', 'first_prompt_tokens_stdev': 'first_prompt_tokens_stdev',
            'first_prompt_tokens_min': 'first_prompt_tokens_min', 'first_prompt_tokens_max': 'first_prompt_tokens_max',
            'first_output_tokens': 'first_output_tokens', 'first_output_tokens_stdev': 'first_output_tokens_stdev',
            'prefix_tokens': 'prefix_tokens', 'prefix_count': 'prefix_count',
            'turns': 'turns', 'rows': 'rows', 'seed': 'seed', 'use_corpus': 'use_corpus',
        }
        for key, attr in field_map.items():
            if meta.get(key) is not None and (getattr(args, attr, None) is None or attr != 'model'):
                setattr(args, attr, meta[key])
        if not args.seed:
            args.seed = 42
        if not args.rows:
            args.rows = 100

    if not args.model or not args.prompt_tokens or not args.output_tokens:
        parser.error("--model, --prompt-tokens, and --output-tokens are required (or use --reproduce)")

    print(f"Generating multi-turn dataset: {args.rows} conversations, {args.turns} turns each", file=sys.stderr, flush=True)
    print(f"Model: {args.model}", file=sys.stderr, flush=True)
    print(f"Prompt: mean={args.prompt_tokens}, stdev={args.prompt_tokens_stdev}", file=sys.stderr, flush=True)
    if args.first_prompt_tokens:
        print(f"First turn: mean={args.first_prompt_tokens}, stdev={args.first_prompt_tokens_stdev}", file=sys.stderr, flush=True)
    if args.prefix_tokens:
        print(f"Prefix: {args.prefix_tokens} tokens, {args.prefix_count} unique", file=sys.stderr, flush=True)

    if getattr(args, 'use_corpus', False):
        generate_corpus_turns(args)
        return

    config = _build_config(args)

    config_dict = config.model_dump()

    num_workers = min(multiprocessing.cpu_count(), 8)
    if args.rows < num_workers:
        num_workers = args.rows

    chunk_size = args.rows // num_workers
    remainder = args.rows % num_workers

    worker_args = []
    start_idx = 0
    for w in range(num_workers):
        n = chunk_size + (1 if w < remainder else 0)
        worker_seed = args.seed + w * 10000
        worker_args.append((w, args.model, config_dict, n, start_idx, worker_seed))
        start_idx += n

    print(f"Using {num_workers} workers for {args.rows} conversations...", file=sys.stderr, flush=True)

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
    start = time.time()

    with multiprocessing.Pool(num_workers) as pool:
        chunks = pool.map(_worker_generate, worker_args)

    with open(args.output, 'w') as f:
        f.write(json.dumps(_build_turn_meta(args)) + '\n')
        total = 0
        for chunk in chunks:
            for line in chunk:
                f.write(line + '\n')
                total += 1

    elapsed = time.time() - start
    file_size = os.path.getsize(args.output)
    rate = total / elapsed if elapsed > 0 else 0
    print(f"Generation complete: {total} conversations in {elapsed:.1f}s ({rate:.1f}/s, {file_size / 1024 / 1024:.1f} MB)",
          file=sys.stderr, flush=True)
    print(f"Reproduce with: generate_turn_dataset --reproduce {args.output} --output <new_path>", file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
