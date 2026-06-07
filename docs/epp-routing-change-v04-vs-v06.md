# EPP Routing Change: v0.4.0 vs v0.6.0

## Summary

The throughput drop (11-28%) observed between RHAIIS 3.3 (llm-d v0.4.0) and RHAIIS 3.4 (llm-d v0.6.0) is caused by a change in the EPP (Endpoint Picker) scheduling profiles, not by vLLM engine changes.

## What Changed

**Source**: `guides/pd-disaggregation/gaie-pd/values.yaml` in the [llm-d repo](https://github.com/llm-d/llm-d)

### v0.4.0 (RHAIIS 3.3) — Queue-Only Routing

```yaml
schedulingProfiles:
- name: prefill
  plugins:
  - pluginRef: prefill-filter
  - pluginRef: queue-scorer
    weight: 1.0
  - pluginRef: max-score-picker
- name: decode
  plugins:
  - pluginRef: decode-filter
  - pluginRef: queue-scorer
    weight: 1.0
  - pluginRef: max-score-picker
```

Routes requests to the pod with the shortest queue. Simple round-robin-like behavior. Maximizes throughput by spreading load evenly.

### v0.6.0 (RHAIIS 3.4) — Prefix Cache + Queue Routing

```yaml
schedulingProfiles:
- name: prefill
  plugins:
  - pluginRef: prefill-filter
  - pluginRef: max-score-picker
  - pluginRef: prefix-cache-scorer
    weight: 2
  - pluginRef: queue-scorer
    weight: 1
- name: decode
  plugins:
  - pluginRef: decode-filter
  - pluginRef: max-score-picker
  - pluginRef: prefix-cache-scorer
    weight: 2
  - pluginRef: queue-scorer
    weight: 1
```

Routes requests to pods that have the most cached prefix data (weight 2), with queue depth as a secondary signal (weight 1). Also added `prefix-based-pd-decider` with `nonCachedTokens: 16`.

## Impact

| Metric | Effect | Reason |
|--------|--------|--------|
| TTFT | -10% to -23% (improved) | Requests hit cached prefixes, skipping redundant prefill computation |
| Throughput | -11% to -28% (dropped) | Requests cluster on pods with cached prefixes instead of spreading evenly |
| ITL | -25% to -35% (improved) | Less prefill interference on decode pods |

## Why This Is a Deliberate Tradeoff

The v0.6.0 change prioritizes **user-perceived latency** (how fast each user sees the first token) over **raw system throughput** (total requests per second). For most production workloads, lower TTFT is more valuable than higher req/s — users notice latency, infrastructure teams measure throughput.

## Measured Results (100 concurrent users, ISL=2000, OSL=100, 50% prefix cache)

### Qwen3-32B PD (3P+1D TP4, same config)

| Version | TTFT P90 | Throughput |
|---------|----------|------------|
| v0.4.0 (3.3) | 880 ms | 31.2 req/s |
| v0.6.0 (3.4) | 675 ms (-23%) | 22.5 req/s (-28%) |

### Llama-70B FP8 PD (3P+1D TP4, same config)

| Version | TTFT P90 | Throughput |
|---------|----------|------------|
| v0.4.0 (3.3) | 1,027 ms | 28.7 req/s |
| v0.6.0 (3.4) | 925 ms (-10%) | 25.6 req/s (-11%) |

## Verification

```bash
git clone https://github.com/llm-d/llm-d.git
git diff v0.4.0..v0.6.0 -- guides/pd-disaggregation/gaie-pd/values.yaml
```
