# ServeIt Studio — Execution Examples

All examples assume `URL` is set to your instance URL.

```bash
URL="https://serveit-admin-kermit-kermit-ui-inftune.apps.ocp4.intlab.redhat.com"
```

---

## 1. Login

Option A — username/password:
```bash
curl -sk -c /tmp/sck -X POST "$URL/login" \
  -d "username=admin&password=serveit123" -L -o /dev/null
```

Option B — API key (copy from launcher → 🔑 Copy API Key button):
```bash
TOKEN="<paste token from launcher>"
curl -sk -c /tmp/sck "$URL/auto-login?token=$TOKEN" -L -o /dev/null
```

---

## 2. Cluster Scan

```bash
curl -sk -b /tmp/sck -X POST "$URL/api/scan" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print('GPUs:', data.get('total_gpus'))
print('Storage classes:', [s['name'] for s in data.get('storage_classes', [])])
print('Network:', data.get('network_type'))
"
```

---

## 3. Save Config

```bash
curl -sk -b /tmp/sck -X POST "$URL/api/config/lock"

curl -sk -b /tmp/sck -X POST "$URL/api/config" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "zai-org/GLM-5.3",
    "isl": 7000, "osl": 500,
    "isl_stdev": 3000, "osl_stdev": 500,
    "users": 30, "turns": 300,
    "turn_delay": 15, "turn_delay_stdev": 55,
    "turn_delay_min": 1, "turn_delay_max": 100,
    "first_prompt_tokens": 160000,
    "first_prompt_tokens_stdev": 233600,
    "first_prompt_tokens_min": 10000,
    "first_prompt_tokens_max": 990000,
    "prefix_cache_hit_pct": 60,
    "prefix_cache_mode": "multi_group",
    "prefix_cache_groups": 10,
    "use_corpus": true,
    "workload_mode": "dataset",
    "goal": "pd_only",
    "max_gpus": 16,
    "storage_class": "serveit-local",
    "image": "quay.io/llm-d/llm-d-cuda-ubuntu@sha256:38fbb9970ecaaef364a62c2119b96c6b9b135f186f9e64ee9aa9e19bc22de7f5",
    "advanced_vllm_custom_enabled": true,
    "advanced_vllm": {
      "max_model_len": {"mode": "custom", "value": 1000000},
      "block_size": {"mode": "custom", "value": 128},
      "kv_cache_dtype": {"mode": "custom", "value": "fp8"},
      "cpu_offload_gb": {"mode": "custom", "value": 200},
      "enable_expert_parallel": {"mode": "on"},
      "enable_prefix_caching": {"mode": "on"},
      "speculative_method": "mtp",
      "num_speculative_tokens": {"mode": "custom", "value": 3},
      "prefill_speculative_num_tokens": {"mode": "custom", "value": 1},
      "prefill_attention_config": {"mode": "custom", "value": "{\"sparse_mla_force_mqa\":true}"},
      "context_parallel_entries": [
        {"flag": "--prefill-context-parallel-size", "value": null}
      ],
      "_ctx_parallel_preset": "on",
      "_mode": "form"
    },
    "rate_type": "concurrent",
    "stop_mode": "duration",
    "max_test_duration": 300
  }'
```

---

## 4. Storage Setup (model download)

```bash
curl -sk -b /tmp/sck -X POST "$URL/api/setup_storage" \
  -H 'Content-Type: application/json' \
  -d '{"model": "zai-org/GLM-5.3", "hf_token": ""}'
```

---

## 5. Start Optimization

```bash
curl -sk -b /tmp/sck -X POST "$URL/api/start_optimization" \
  -H 'Content-Type: application/json' -d '{}' | python3 -c "
import json, sys; print(json.load(sys.stdin))
"
```

---

## 6. Check Status

```bash
curl -sk -b /tmp/sck "$URL/api/status" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('Running:', d.get('optimization_running'))
print('Run ID:', d.get('current_run_id'))
"
```

---

## 7. Get Results

```bash
curl -sk -b /tmp/sck "$URL/api/runs" | python3 -c "
import json, sys
runs = json.load(sys.stdin)
for r in runs[:3]:
    print(f'Run #{r[\"id\"]}: {r.get(\"status\")} — {len(r.get(\"test_configs\", []))} tests')
"
```

---

## GLM-5.3 Quick Reference (Balanced goal)

```json
{"goal": "balanced", "model": "zai-org/GLM-5.3",
 "isl": 7000, "osl": 500, "users": 30, "max_gpus": 16}
```

## GLM-5.3 Quick Reference (PD-only, 1M context, HiSparse)

See `SKILL.md` for the full config JSON with advanced_vllm settings.

## Long-Run Soak (50 cycles, parallel workloads, custom EPP)

```bash
cat > /tmp/soak-payload.json << 'EOF'
{
  "model_name": "RedHatAI/gemma-4-31B-it-FP8-dynamic",
  "namespace": "<instance-namespace>",
  "isl": 6000, "osl": 500, "qps": 6, "rate_type": "concurrent",
  "total_gpus": 8, "test_duration": 300, "stop_mode": "duration",
  "prefix_tokens": 4800, "prefix_cache_hit_pct": 50,
  "prefix_cache_mode": "multi_group", "prefix_cache_groups": 20,
  "workload_mode": "synthetic", "use_corpus": true,
  "optimization_metric": "long_run", "long_run_cycles": 50,
  "single_test_architecture": "aggregated", "single_test_tp": 1,
  "single_test_replicas": 8, "single_test_pipeline_parallelism": 1,
  "advanced_vllm_custom_enabled": false,
  "epp_custom_enabled": true,
  "epp_config": {
    "preset": "custom",
    "raw_yaml": "plugins:\n  - type: approx-prefix-cache-producer\n  - type: inflight-load-producer\n  - type: prefix-cache-affinity-filter\n  - type: token-load-scorer\nschedulingProfiles:\n  - name: default\n    plugins:\n      - pluginRef: prefix-cache-affinity-filter\n      - pluginRef: token-load-scorer"
  },
  "enable_pprof": true, "enable_epp_profile": true,
  "parallel_workloads": [
    {"name": "W1-big-prefix", "prefix_tokens": 4800, "isl": 6000, "isl_min": 4800, "isl_max": 7200, "osl": 500, "concurrency": 3},
    {"name": "W2-mid", "prefix_tokens": 300, "isl": 4500, "isl_min": 3000, "isl_max": 6000, "osl": 200, "concurrency": 4},
    {"name": "W3-burst", "prefix_tokens": 150, "isl": 4500, "isl_min": 3000, "isl_max": 6000, "osl": 100, "concurrency": 5}
  ]
}
EOF

curl -sk -b /tmp/sck -X POST "$URL/api/start_optimization" \
  -H 'Content-Type: application/json' -d @/tmp/soak-payload.json
```

Flags `advanced_vllm_custom_enabled` / `epp_custom_enabled` are pinned deliberately — omitted payload fields fall back to the last-saved UI wizard state, which silently flipped both in past runs (broken speculative config, default EPP routing). Expect ~13-17 min per cycle (two-pass profiling). Resume with `POST /api/resume_optimization {"run_id": N}` — completed cycles are skipped and pods are reused.

---

## Why curl instead of run.py?

`run.py` is useful for **end-to-end automation** (scan → download → config → run → wait → results).
For **one-shot runs** where storage is already set up, the curl sequence above is simpler:
steps 1 → 3 → 5 → 6 is all you need.
