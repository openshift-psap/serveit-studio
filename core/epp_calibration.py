"""EPP affinity-filter calibration.

The prefix-cache-affinity-filter estimates per-endpoint TTFT from in-flight
tokens: TTFT_ms = inFlightTokens / peakPrefillThroughput * 1000, and breaks
stickiness when the sticky endpoint's TTFT exceeds the best non-sticky one by
maxTTFTPenaltyMs. Both defaults are calibrated for Qwen-32B on 2x H100 TP2
(15928 tok/s, 18000 ms) — wrong for any other model/hardware, and the 18s
penalty can never fire at modest concurrency, so cache affinity concentrates
load on few pods.

This module measures the true per-pod peak prefill throughput (the upstream
guides/recipes/router/calibration method: fresh random token-ID prompts so the
prefix cache misses every time, TTFT to first streamed chunk, R_peak =
CHUNK_SIZE / median(TTFT)), derives a workload-aware maxTTFTPenaltyMs (one
re-prefill of the workload's ISL — the crossover where spreading beats
sticking), persists the calibration per (model, gpu, tp), and injects both
parameters into EPP configs that use the affinity filter.
"""

import json
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

WARMUP_REQUESTS = 5
MEASUREMENT_REQUESTS = 20
# Conservative token-ID range, valid for common tokenizers (Qwen ~150k,
# Llama ~128k, Gemma ~260k).
TOKEN_MIN = 100
TOKEN_MAX = 10000

# In-pod measurement script. Runs in the guidellm workload pod (python3
# available), sends requests directly to one inference pod's IP so the
# measurement is a true single-endpoint peak (bypasses the router).
_CALIBRATE_SCRIPT = r'''
import json, os, random, statistics, sys, time, urllib.request, urllib.error

endpoint = os.environ["CAL_ENDPOINT"].rstrip("/")
model = os.environ["CAL_MODEL"]
chunk = int(os.environ["CAL_CHUNK"])
warmup = int(os.environ.get("CAL_WARMUP", "5"))
measure = int(os.environ.get("CAL_MEASURE", "20"))
token_min = int(os.environ.get("CAL_TOKEN_MIN", "100"))
token_max = int(os.environ.get("CAL_TOKEN_MAX", "10000"))

rng = random.Random(time.time_ns())

def fresh_prompt():
    return [rng.randint(token_min, token_max) for _ in range(chunk)]

def send_one(prompt_ids):
    body = json.dumps({
        "model": model,
        "prompt": prompt_ids,
        "max_tokens": 1,
        "stream": True,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        endpoint + "/v1/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    ttft = None
    with urllib.request.urlopen(req, timeout=120) as resp:
        for line in resp:
            if not line.strip().startswith(b"data:"):
                continue
            payload = line.strip()[5:].strip()
            if payload in (b"[DONE]", b""):
                break
            event = json.loads(payload)
            if isinstance(event, dict) and "error" in event:
                raise RuntimeError(str(event["error"])[:300])
            if ttft is None:
                ttft = time.monotonic() - start
    if ttft is None:
        raise RuntimeError("no streamed chunk received")
    return ttft

for i in range(warmup):
    send_one(fresh_prompt())
samples = [send_one(fresh_prompt()) for _ in range(measure)]
median_ttft = statistics.median(samples)
r_peak = chunk / median_ttft
print("CAL_RESULT=" + json.dumps({
    "median_ttft_s": round(median_ttft, 4),
    "r_peak": int(r_peak),
    "samples": [round(s, 4) for s in samples],
}))
'''


def calibrate_peak_prefill(
    kubectl,
    namespace: str,
    workload_pod: str,
    target_pod_ip: str,
    model_name: str,
    chunk_size: int,
    port: int = 8000,
    warmup: int = WARMUP_REQUESTS,
    measurements: int = MEASUREMENT_REQUESTS,
    log=None,
) -> Optional[dict]:
    """Measure one inference pod's peak prefill throughput.

    Sends warmup + measurement requests of exactly chunk_size random token
    IDs to the pod (fresh prompt each time — prefix cache always misses) and
    returns {"median_ttft_s", "r_peak", "samples"}, or None on failure.
    """
    script_path = "/tmp/epp_calibrate.py"
    env = (
        f"CAL_ENDPOINT=http://{target_pod_ip}:{port} "
        f'CAL_MODEL="{model_name}" '
        f"CAL_CHUNK={chunk_size} "
        f"CAL_WARMUP={warmup} "
        f"CAL_MEASURE={measurements} "
        f"CAL_TOKEN_MIN={TOKEN_MIN} "
        f"CAL_TOKEN_MAX={TOKEN_MAX} "
        f"python3 {script_path}"
    )
    try:
        # Stage the script
        import base64
        r = kubectl.run(
            ["exec", workload_pod, "-n", namespace, "--", "bash", "-c",
             f"echo {base64.b64encode(_CALIBRATE_SCRIPT.encode()).decode()} | base64 -d > {script_path}"],
            check=False, timeout=30,
        )
        if r.returncode != 0:
            logger.warning(f"calibration script staging failed: {r.stderr[:200] if r.stderr else 'rc=' + str(r.returncode)}")
            return None
        # Run it
        r = kubectl.run(
            ["exec", workload_pod, "-n", namespace, "--", "bash", "-c", env],
            check=False, timeout=600,
        )
        if r.returncode != 0:
            logger.warning(f"calibration run failed: {r.stderr[:300] if r.stderr else 'rc=' + str(r.returncode)}")
            return None
        for line in r.stdout.strip().splitlines():
            if line.startswith("CAL_RESULT="):
                result = json.loads(line[len("CAL_RESULT="):])
                if log:
                    log(f"   📐 Peak prefill: {result['r_peak']} tok/s "
                        f"(median TTFT {result['median_ttft_s']}s over {len(result['samples'])} samples)")
                return result
        logger.warning("calibration produced no CAL_RESULT line")
        return None
    except Exception as e:
        logger.warning(f"calibration error: {e}")
        return None


def derive_max_ttft_penalty_ms(isl_tokens: int, r_peak: int) -> int:
    """Workload-aware stickiness-break threshold.

    The natural crossover: break stickiness when the sticky endpoint's extra
    queueing delay exceeds the cost of re-prefilling the whole prompt on a
    cold pod (ISL / R_peak). Below that, cache locality wins; above it,
    spreading wins. Clamped to [200, 10000] ms.
    """
    if not r_peak or r_peak <= 0 or not isl_tokens or isl_tokens <= 0:
        return 2000  # safe default
    penalty = int(isl_tokens / r_peak * 1000)
    return max(200, min(10000, penalty))


# ── persistence ────────────────────────────────────────────────────────────

def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS epp_calibrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            gpu_model TEXT NOT NULL,
            tensor_parallelism INTEGER NOT NULL,
            peak_prefill_throughput INTEGER NOT NULL,
            median_ttft_s REAL,
            chunk_size INTEGER,
            calibrated_at TEXT NOT NULL
        )
    """)


def get_stored_calibration(db_path: str, model_name: str, gpu_model: str, tp: int) -> Optional[dict]:
    """Most recent calibration for (model, gpu, tp), or None."""
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        _ensure_table(conn)
        row = conn.execute(
            """SELECT peak_prefill_throughput, median_ttft_s, chunk_size, calibrated_at
               FROM epp_calibrations
               WHERE model_name=? AND gpu_model=? AND tensor_parallelism=?
               ORDER BY id DESC LIMIT 1""",
            (model_name, gpu_model, tp),
        ).fetchone()
        conn.close()
        if row:
            return {
                "peak_prefill_throughput": row[0],
                "median_ttft_s": row[1],
                "chunk_size": row[2],
                "calibrated_at": row[3],
            }
    except Exception as e:
        logger.warning(f"calibration lookup failed: {e}")
    return None


def store_calibration(db_path: str, model_name: str, gpu_model: str, tp: int,
                      r_peak: int, median_ttft_s: float, chunk_size: int) -> None:
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        _ensure_table(conn)
        conn.execute(
            """INSERT INTO epp_calibrations
               (model_name, gpu_model, tensor_parallelism, peak_prefill_throughput,
                median_ttft_s, chunk_size, calibrated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (model_name, gpu_model, tp, r_peak, median_ttft_s, chunk_size,
             datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"calibration store failed: {e}")


# ── config injection ───────────────────────────────────────────────────────

def inject_affinity_filter_params(raw_yaml: str, peak_prefill_throughput: int,
                                  max_ttft_penalty_ms: int) -> str:
    """Add parameters to the prefix-cache-affinity-filter in an EPP raw config.

    Works on the textual YAML (the raw config is passed through verbatim, so
    we preserve formatting): finds the plugin entry and inserts/updates a
    parameters block. Returns the modified YAML, or the input unchanged if
    the filter isn't present.
    """
    if "prefix-cache-affinity-filter" not in raw_yaml:
        return raw_yaml

    lines = raw_yaml.splitlines()
    out = []
    in_filter = False
    filter_indent = None
    injected = False

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Detect the filter plugin entry
        if stripped.startswith("- type: prefix-cache-affinity-filter"):
            in_filter = True
            filter_indent = indent
            out.append(line)
            continue

        if in_filter:
            # Still inside this plugin entry? (same or deeper indentation,
            # and not a new list item at the filter's level)
            is_sibling = stripped.startswith("- ") and indent <= filter_indent
            if is_sibling or (stripped and indent < filter_indent):
                # Leaving the filter entry — inject parameters before the
                # next sibling if we haven't already
                if not injected:
                    pi = " " * (filter_indent + 4)
                    out.append(" " * (filter_indent + 2) + "parameters:")
                    out.append(f"{pi}peakPrefillThroughput: {peak_prefill_throughput}")
                    out.append(f"{pi}maxTTFTPenaltyMs: {max_ttft_penalty_ms}")
                    injected = True
                in_filter = False
            elif "peakPrefillThroughput:" in stripped or "maxTTFTPenaltyMs:" in stripped:
                # Existing parameter — update in place
                if "peakPrefillThroughput:" in stripped:
                    out.append(f"{line.split(':')[0]}: {peak_prefill_throughput}")
                else:
                    out.append(f"{line.split(':')[0]}: {max_ttft_penalty_ms}")
                injected = True
                continue
        out.append(line)

    # Filter was the last entry in the file
    if in_filter and not injected:
        pi = " " * (filter_indent + 4)
        out.append(" " * (filter_indent + 2) + "parameters:")
        out.append(f"{pi}peakPrefillThroughput: {peak_prefill_throughput}")
        out.append(f"{pi}maxTTFTPenaltyMs: {max_ttft_penalty_ms}")

    return "\n".join(out) + ("\n" if raw_yaml.endswith("\n") else "")


# ── computed default (no measurement) ──────────────────────────────────────

# Reference point: the upstream default 15928 tok/s is Qwen-32B dense on
# 2x H100 80GB (TP2). Per-GPU H100 prefill ≈ 8000 tok/s for a ~32B dense
# model in BF16. We scale by GPU memory bandwidth (prefill is
# bandwidth-dominated for large models), TP degree, and precision.
_GPU_BW_GB_S = {
    "H100": 3.35,
    "H200": 4.80,
    "A100": 2.04,
    "B200": 7.70,
}


def estimate_peak_prefill(gpu_model: str, tensor_parallelism: int,
                          model_config: dict) -> int:
    """Estimate peak prefill throughput (tokens/sec) without measurement.

    Scales the upstream reference point (Qwen-32B, 2x H100, BF16, TP2 =
    15928 tok/s) by GPU memory bandwidth, TP degree, and quantization.
    Conservative: rounds down. Accuracy target is "right order of
    magnitude with the right sign vs the 15928 default" — measured
    gemma-31B-FP8/H200/TP1 came in at ~12500 vs this estimate's ~11400.
    """
    gpu = (gpu_model or "").upper()
    for key, bw in _GPU_BW_GB_S.items():
        if key in gpu:
            gpu_bw = bw
            break
    else:
        gpu_bw = 3.35  # assume H100-class

    tp = max(1, tensor_parallelism or 1)

    # Model size scaling: prefill throughput falls roughly with parameter
    # count (more bytes per token to move).
    params_b = 32.0
    try:
        layers = model_config.get("num_hidden_layers") or 32
        hidden = model_config.get("hidden_size") or 4096
        # crude dense-parameter estimate: 12 * layers * hidden^2 / 1e9
        params_b = min(120.0, 12 * layers * hidden * hidden / 1e9)
    except Exception:
        pass

    # Quantization: FP8 halves weight bytes vs BF16 → ~1.5x prefill for
    # bandwidth-bound models (not 2x — activations stay BF16).
    quant_mult = 1.5 if "fp8" in (gpu_model or "").lower() else 1.0

    # Reference: 15928 tok/s = Qwen-32B (~32B params) on 2x H100 (6.7 GB/s
    # combined BW), BF16, TP2.
    ref_bw = 2 * _GPU_BW_GB_S["H100"]
    ref_params = 32.0

    estimate = (
        15928
        * (gpu_bw * tp / ref_bw)
        * (ref_params / max(params_b, 1.0)) ** 0.5
        * quant_mult
    )
    return int(estimate * 0.9)  # round down 10% — conservative
