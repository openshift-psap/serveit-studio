#!/usr/bin/env python3
"""Parse guidellm JSON output and print only the metrics we need.

Used by the orchestrator to extract results from the remote workload pod
without copying the full JSON file.

Usage:
    python3 parse_guidellm.py /tmp/results.json
    python3 parse_guidellm.py -  # read from stdin
"""

import json
import sys


def parse(path):
    if path == '-':
        data = json.load(sys.stdin)
    else:
        with open(path) as f:
            data = json.load(f)

    benchmarks = data.get('benchmarks', [])
    if not benchmarks:
        print(json.dumps({'error': 'No benchmarks found'}))
        return

    b = benchmarks[0]
    metrics = b.get('metrics', {})

    def dist(key):
        d = metrics.get(key, {}).get('successful', {})
        pcts = d.get('percentiles', {})
        return {
            'mean': d.get('mean'),
            'min': d.get('min'),
            'max': d.get('max'),
            'std_dev': d.get('std_dev'),
            'p25': pcts.get('p25'),
            'p50': pcts.get('p50'),
            'p75': pcts.get('p75'),
            'p90': pcts.get('p90'),
            'p95': pcts.get('p95'),
            'p99': pcts.get('p99'),
            'p999': pcts.get('p999'),
        }

    totals = metrics.get('request_totals', {})

    result = {
        'ttft_ms': dist('time_to_first_token_ms'),
        'itl_ms': dist('inter_token_latency_ms'),
        'throughput_rps': dist('requests_per_second'),
        'tpot_ms': dist('time_per_output_token_ms'),
        'e2e_latency_s': dist('request_latency'),
        'output_tps': dist('output_tokens_per_second'),
        'prompt_tokens': dist('prompt_token_count'),
        'output_tokens': dist('output_token_count'),
        'concurrency': dist('request_concurrency'),
        'request_totals': {
            'total': totals.get('total'),
            'successful': totals.get('successful'),
            'incomplete': totals.get('incomplete'),
            'errored': totals.get('errored'),
        },
        'benchmark_duration_s': b.get('duration'),
        'warmup_duration_s': b.get('warmup_duration'),
        'start_time': b.get('start_time'),
        'end_time': b.get('end_time'),
    }

    print(json.dumps(result))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: parse_guidellm.py <path>'}))
        sys.exit(1)
    try:
        parse(sys.argv[1])
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)
