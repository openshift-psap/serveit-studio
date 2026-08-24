#!/usr/bin/env python3
"""Download wikitext-103-raw-v1 and save as a single clean text file.

Run once during docker build to bundle the corpus into the image.
Filters out empty lines, section headers (lines starting with ' = '),
and very short paragraphs (< 100 chars) to keep only substantial prose.

Usage:
    prepare_corpus.py --output /app/corpus/wikitext-103.txt
"""
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    from datasets import load_dataset

    print("Downloading wikitext-103-raw-v1...", file=sys.stderr, flush=True)
    ds = load_dataset('Salesforce/wikitext', 'wikitext-103-raw-v1', split='train')

    os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)

    total_chars = 0
    paragraphs = 0
    with open(args.output, 'w', encoding='utf-8') as f:
        for row in ds:
            text = row['text'].strip()
            if not text or len(text) < 100:
                continue
            if text.startswith('= ') and text.endswith(' ='):
                continue
            f.write(text + '\n')
            total_chars += len(text)
            paragraphs += 1

    size_mb = os.path.getsize(args.output) / (1024 * 1024)
    approx_tokens = total_chars // 4
    print(
        f"Corpus ready: {paragraphs} paragraphs, {total_chars:,} chars "
        f"(~{approx_tokens:,} tokens), {size_mb:.1f} MB",
        file=sys.stderr, flush=True,
    )
    print(f"Saved to: {args.output}", file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
