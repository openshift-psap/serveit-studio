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

---

## Why curl instead of run.py?

`run.py` is useful for **end-to-end automation** (scan → download → config → run → wait → results).
For **one-shot runs** where storage is already set up, the curl sequence above is simpler:
steps 1 → 3 → 5 → 6 is all you need.
