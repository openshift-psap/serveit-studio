"""Torch profiling script that runs inside vLLM pods during the benchmark.

Usage: python3 torch_profile_pod.py <duration_seconds>

Captures CPU + CUDA activity for the specified duration, then outputs:
1. Full Chrome trace at /tmp/torch_profiles/trace.json (downloadable)
2. Operator summary JSON at /tmp/torch_profiles/summary.json (for the report)
3. Summary printed to stdout (captured by kubectl exec)
"""
import torch
import time
import os
import json
import sys

duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60
output_dir = '/tmp/torch_profiles'
os.makedirs(output_dir, exist_ok=True)

from torch.profiler import profile, ProfilerActivity

print(f'PROFILE_START {duration}s', flush=True)

prof = profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
    record_shapes=False,
    profile_memory=True,
    with_stack=False,
)
prof.start()
time.sleep(duration)
prof.stop()

# Export full Chrome trace
trace_path = os.path.join(output_dir, 'trace.json')
prof.export_chrome_trace(trace_path)
trace_size = os.path.getsize(trace_path)

# Build operator summary
summary = {
    'profiler': 'torch',
    'torch_version': torch.__version__,
    'duration_s': duration,
    'trace_size_bytes': trace_size,
    'operators': [],
}

total_cpu_us = 0
total_cuda_us = 0

try:
    for evt in prof.key_averages():
        cpu_total_ms = evt.cpu_time_total / 1000.0  # microseconds → ms
        cuda_total_ms = evt.cuda_time_total / 1000.0
        self_cpu_ms = evt.self_cpu_time_total / 1000.0
        self_cuda_ms = evt.self_cuda_time_total / 1000.0
        total_cpu_us += evt.cpu_time_total
        total_cuda_us += evt.cuda_time_total
        summary['operators'].append({
            'name': evt.key,
            'cpu_total_ms': round(cpu_total_ms, 2),
            'cuda_total_ms': round(cuda_total_ms, 2),
            'self_cpu_ms': round(self_cpu_ms, 2),
            'self_cuda_ms': round(self_cuda_ms, 2),
            'cpu_mem_bytes': evt.cpu_memory_usage,
            'calls': evt.count,
        })
except Exception as e:
    print(f'SUMMARY_ERROR {e}', flush=True)

# Sort by total CPU time, keep top 25
summary['operators'].sort(key=lambda o: o['cpu_total_ms'], reverse=True)
summary['operators'] = summary['operators'][:25]
summary['total_cpu_ms'] = round(total_cpu_us / 1000.0, 1)
summary['total_cuda_ms'] = round(total_cuda_us / 1000.0, 1)
summary['operator_count'] = len(summary['operators'])

# Save summary for the report
summary_path = os.path.join(output_dir, 'summary.json')
with open(summary_path, 'w') as f:
    json.dump(summary, f)

print(f'PROFILE_DONE trace={trace_size}B ops={summary["operator_count"]}', flush=True)
print('SUMMARY_JSON:' + json.dumps(summary), flush=True)
