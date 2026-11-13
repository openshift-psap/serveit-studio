#!/usr/bin/env python3
"""Convert WekaTrace agentic traces to ServeIt-guidellm dataset JSONL.

Usage:
    generate_wekatrace_dataset.py --traces PATH --model MODEL --output PATH
        [--max-traces N] [--max-is-blocks N] [--include-subagents]
        [--block-content corpus|random] [--concurrency N]

Reads the WekaTrace JSONL (Claude Code agentic sessions with hash-block
prefix cache patterns) and produces a guidellm-compatible dataset where:

  - Each request becomes {"prompt": str, "output_tokens_count": int}
  - The prompt is reconstructed from 64-token blocks matching the trace's
    hash_ids — requests sharing hash_ids share prefix content, so the EPP's
    prefix-cache affinity sees the same block-level sharing as production
  - Multi-turn ISL growth is preserved (hash_ids grow each turn)
  - Subagent fan-out is flattened as parallel requests
  - The text is real prose from the corpus (not word salad) so speculative
    decoding and cache behavior are realistic

The output preserves the theoretical cache hit rate from the trace: if a
session's requests share 97% of their hash blocks, the generated prompts
will share 97% of their prefix content.
"""
import argparse
import json
import os
import random
import sys


BLOCK_SIZE = 64  # WekaTrace hash block size (tokens)


def load_corpus():
    """Load the wikitext corpus for realistic block content."""
    corpus_paths = [
        '/app/corpus/wikitext-103.txt',
        os.path.join(os.path.dirname(__file__), '..', 'corpus', 'wikitext-103.txt'),
    ]
    for path in corpus_paths:
        if os.path.exists(path):
            with open(path) as f:
                return f.read()
    return None


def build_block_pool(corpus_text, num_blocks_needed, rng):
    """Build a pool of 64-token text blocks.

    Each block is a unique slice of text (or random tokens if no corpus).
    The block pool is shared across all requests in a trace — requests
    referencing the same hash_id get the same text block.
    """
    if corpus_text:
        # Tokenize by whitespace (approximately, for corpus mode)
        words = corpus_text.split()
        if len(words) < num_blocks_needed * BLOCK_SIZE:
            # Not enough corpus for unique blocks — repeat with variation
            words = words * (num_blocks_needed * BLOCK_SIZE // len(words) + 1)

        blocks = []
        start = 0
        for i in range(num_blocks_needed):
            chunk = words[start:start + BLOCK_SIZE]
            if len(chunk) < BLOCK_SIZE:
                chunk = chunk + rng.sample(words, BLOCK_SIZE - len(chunk))
            blocks.append(' '.join(chunk))
            start += BLOCK_SIZE
        return blocks
    else:
        # Fallback: random alphanumeric tokens
        import string
        chars = string.ascii_lowercase
        blocks = []
        for i in range(num_blocks_needed):
            tokens = [''.join(rng.choices(chars, k=8)) for _ in range(BLOCK_SIZE)]
            blocks.append(' '.join(tokens))
        return blocks


def parse_trace_requests(trace, include_subagents=False):
    """Flatten a trace into a list of (hash_ids, output_tokens, t) tuples."""
    requests = []

    for r in trace.get('requests', []):
        if r.get('type') == 'subagent':
            if include_subagents:
                # Flatten subagent inner requests
                for sr in r.get('requests', []):
                    requests.append((
                        sr.get('hash_ids', []),
                        sr.get('out', 100),
                        sr.get('t', 0),
                        r.get('subagent_type', 'unknown'),
                    ))
        else:
            requests.append((
                r.get('hash_ids', []),
                r.get('out', 100),
                r.get('t', 0),
                'main',
            ))

    return requests


def trace_stats(trace):
    """Compute summary stats for a trace."""
    all_reqs = []
    for r in trace.get('requests', []):
        if r.get('type') == 'subagent':
            for sr in r.get('requests', []):
                all_reqs.append(sr)
        else:
            all_reqs.append(r)

    if not all_reqs:
        return None

    isls = [r.get('in', 0) for r in all_reqs]
    osls = [r.get('out', 0) for r in all_reqs]
    return {
        'requests': len(all_reqs),
        'isl_min': min(isls),
        'isl_max': max(isls),
        'isl_mean': sum(isls) // max(len(isls), 1),
        'osl_mean': sum(osls) // max(len(osls), 1),
        'blocks_max': max(len(r.get('hash_ids', [])) for r in all_reqs),
        'duration_s': max(r.get('t', 0) for r in all_reqs),
        'subagents': sum(1 for r in trace.get('requests', []) if r.get('type') == 'subagent'),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.rstrip())
    parser.add_argument('--traces', required=True, help='Path to WekaTrace JSONL file')
    parser.add_argument('--model', required=True, help='Model name (for tokenizer)')
    parser.add_argument('--output', required=True, help='Output JSONL path')
    parser.add_argument('--max-traces', type=int, default=10, help='Max traces to convert')
    parser.add_argument('--max-is-blocks', type=int, default=16000,
                        help='Skip requests above this block count (16000*64=1M tokens)')
    parser.add_argument('--max-is-tokens', type=int, default=None,
                        help='Skip requests above this input token count (model context budget)')
    parser.add_argument('--include-subagents', action='store_true',
                        help='Include subagent inner requests')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--block-content', choices=['corpus', 'random'], default='corpus')
    parser.add_argument('--stats-only', action='store_true',
                        help='Print stats without generating dataset')
    args = parser.parse_args()

    rng = random.Random(args.seed)

    # Load traces
    traces = []
    with open(args.traces) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                traces.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    print(f'Loaded {len(traces)} traces', file=sys.stderr)

    # Filter traces
    valid = []
    for t in traces:
        stats = trace_stats(t)
        if not stats:
            continue
        if stats['blocks_max'] > args.max_is_blocks:
            continue
        valid.append(t)

    traces = valid[:args.max_traces]
    print(f'{len(traces)} traces after filtering (max_is_blocks={args.max_is_blocks})',
          file=sys.stderr)

    if args.stats_only:
        for i, t in enumerate(traces):
            s = trace_stats(t)
            print(f'trace[{i}]: {s["requests"]} reqs, ISL {s["isl_min"]:,}-{s["isl_max"]:,} '
                  f'(mean {s["isl_mean"]:,}), {s["subagents"]} subagents, '
                  f'{s["duration_s"]/60:.1f}min')
        return

    # Determine total unique blocks needed
    all_hash_ids = set()
    for t in traces:
        for r in t.get('requests', []):
            if r.get('type') == 'subagent':
                if args.include_subagents:
                    for sr in r.get('requests', []):
                        all_hash_ids.update(sr.get('hash_ids', []))
            else:
                all_hash_ids.update(r.get('hash_ids', []))

    max_block_id = max(all_hash_ids) + 1 if all_hash_ids else 0
    print(f'Total unique blocks: {len(all_hash_ids)} (max id: {max_block_id})', file=sys.stderr)

    # Build block pool
    corpus = load_corpus() if args.block_content == 'corpus' else None
    if corpus:
        print(f'Using corpus for block content ({len(corpus):,} chars)', file=sys.stderr)
    else:
        print('No corpus found — using random tokens', file=sys.stderr)
    block_pool = build_block_pool(corpus, max_block_id, rng)

    # Generate output
    total_requests = 0
    total_isl = 0
    skipped_tokens = 0
    cache_shares = []

    with open(args.output, 'w') as out:
        for trace_idx, trace in enumerate(traces):
            flat_reqs = parse_trace_requests(trace, args.include_subagents)

            # Track previous request's hash_ids for cache sharing analysis
            prev_ids = set()

            for req_idx, (hash_ids, out_tokens, t, req_type) in enumerate(flat_reqs):
                if not hash_ids:
                    continue

                # Skip requests above block limit
                if len(hash_ids) > args.max_is_blocks:
                    continue

                # Skip requests above the model's context budget
                req_isl = len(hash_ids) * BLOCK_SIZE
                if args.max_is_tokens and req_isl > args.max_is_tokens:
                    skipped_tokens += 1
                    continue

                # Build the prompt from blocks
                prompt_parts = []
                for hid in hash_ids:
                    if hid < len(block_pool):
                        prompt_parts.append(block_pool[hid])
                    else:
                        prompt_parts.append(block_pool[hid % len(block_pool)])

                prompt = '\n'.join(prompt_parts)

                # Compute actual cache sharing
                curr_ids = set(hash_ids)
                if prev_ids:
                    shared = len(curr_ids & prev_ids)
                    cache_shares.append(shared / max(len(curr_ids), 1))
                prev_ids = curr_ids

                # Emit guidellm-compatible row
                row = {
                    'prompt': prompt,
                    'output_tokens_count': max(1, out_tokens),
                }
                out.write(json.dumps(row) + '\n')
                total_requests += 1
                total_isl += len(hash_ids) * BLOCK_SIZE

            if (trace_idx + 1) % 10 == 0:
                print(f'  processed {trace_idx + 1}/{len(traces)} traces '
                      f'({total_requests} requests)', file=sys.stderr)

    # Summary
    print(f'\nDataset generated: {total_requests} requests', file=sys.stderr)
    if skipped_tokens:
        print(f'Skipped {skipped_tokens} requests over token limit '
              f'(max_is_tokens={args.max_is_tokens})', file=sys.stderr)
    print(f'Total input tokens: {total_isl:,}', file=sys.stderr)
    print(f'Mean ISL: {total_isl // max(total_requests, 1):,}', file=sys.stderr)
    if cache_shares:
        cache_shares.sort()
        print(f'Cache sharing: p50={cache_shares[len(cache_shares)//2]:.0%} '
              f'p90={cache_shares[int(len(cache_shares)*0.9)]:.0%} '
              f'mean={sum(cache_shares)/len(cache_shares):.0%}', file=sys.stderr)
    print(f'Output: {args.output}', file=sys.stderr)


if __name__ == '__main__':
    main()
