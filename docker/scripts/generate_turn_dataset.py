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


def main():
    parser = argparse.ArgumentParser(description='Generate multi-turn conversation dataset')
    parser.add_argument('--model', required=True, help='HuggingFace model for tokenizer')
    parser.add_argument('--prompt-tokens', type=int, required=True, help='Average prompt tokens per turn')
    parser.add_argument('--output-tokens', type=int, required=True, help='Average output tokens per turn')
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
    args = parser.parse_args()

    import sys
    print(f"Generating multi-turn dataset: {args.rows} conversations, {args.turns} turns each", file=sys.stderr, flush=True)
    print(f"Model: {args.model}", file=sys.stderr, flush=True)
    print(f"Prompt: mean={args.prompt_tokens}, stdev={args.prompt_tokens_stdev}", file=sys.stderr, flush=True)
    if args.first_prompt_tokens:
        print(f"First turn: mean={args.first_prompt_tokens}, stdev={args.first_prompt_tokens_stdev}", file=sys.stderr, flush=True)
    if args.prefix_tokens:
        print(f"Prefix: {args.prefix_tokens} tokens, {args.prefix_count} unique", file=sys.stderr, flush=True)

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
    print(f"Saved to: {args.output}", file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
