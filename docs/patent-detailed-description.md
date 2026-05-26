# Detailed Description of the Invention

The invention is an automated 11-step optimization pipeline that deploys LLM inference pods on the user's Kubernetes cluster, benchmarks them with the user's specific workload (model, input/output length, concurrency), and discovers optimal configurations from measured data. Unlike estimation-based tools, every recommendation is backed by real benchmarks on real hardware.

**Step 1 — Initialization & Resource Detection.** Automatically scans the cluster to discover GPU count, VRAM, RDMA NICs, cloud provider, network type (DRA/NAD/shared device), and storage. Detects model architecture (dense vs MoE, GQA vs MHA) from HuggingFace config.json. Computes engine parameters: `max_model_len = (ISL + OSL) × 1.05`, `block_size = next_power_of_2(sqrt(ISL+OSL))` with minimum 128 for PD (NIXL transfer efficiency), and `gpu_memory_utilization` from per-pod overhead estimation. No manual configuration required.

**Steps 2–3 — TP Calibration.** Tests every valid tensor parallelism (TP) value with isolated workloads: ISL=user's value with OSL=1 for prefill, and ISL=1 with OSL=user's value for decode. Measures Tokens Per Second Per GPU (TPSG = throughput × seq_len / TP) — a normalization that enables fair comparison across TP values. Profiles actual vLLM memory overhead from pod logs (CUDA graphs, activation buffers) to auto-tune `gpu_memory_utilization`. Concurrency is capped at estimated KV cache capacity to prevent overload.

**Steps 4–5 — Capacity Analysis & Feasible Split Generation.** Calculates GPU cost per request from calibration data and derives ideal P/D split mathematically: `D_ideal = total_gpus / (r × prefill_tp + decode_tp)` where `r = decode_throughput / prefill_throughput`. Tests only ~3 candidates around the optimum per TP pair (floor/ceil/±1), reducing 132+ valid configurations on a 32-GPU cluster to ~6 tests — a 22× reduction. Filters asymmetric TP pairs where prefill_tp > decode_tp (NIXL KV transfer constraint, configurable).

**Step 6 — Aggregated Configuration Search.** Tests all valid TP values in standard aggregated mode (all GPUs in one pool) at the user's full workload concurrency. Establishes the aggregated baseline for architecture comparison.

**Step 7 — PD/EP Split Testing & Pareto Front.** Deploys each feasible split as separate prefill and decode LWS pods with sequential deployment (higher GPU requirement first to prevent scheduling deadlocks). Builds Pareto front using TTFT P99 vs throughput P90 — using P99 instead of P90 penalizes configurations with unstable tail latency (e.g., 920ms P90 but 264,000ms P99 from queue collapse). EP architecture reuses PD templates with expert-parallel flags conditionally enabled for TP > 1.

**Step 8 — Architecture Comparison.** Compares best PD/EP (by P99 TTFT) against best aggregated at P90, P95, P99 TTFT and throughput. No new tests — pure analysis of existing results.

**Step 9 — EPP Weight Derivation from Prometheus Metrics.** Derives routing weights mathematically from five vLLM metrics measured during Step 7: prefix cache hit rate (`vllm_prefix_cache_hits_rate`), KV cache pressure (`vllm_kv_cache_pct`), queue depth (`vllm_requests_waiting`), active request load (`vllm_requests_running`), and SLO overshoot (`vllm_ttft_p99` vs target). Each weight is proportional to the time impact of routing on that dimension. Tests by swapping only the gateway ConfigMap (~10s), isolating routing from pod config. Falls back to balanced weights if derived weights degrade performance.

**Step 10 — Latency-Bounded Throughput Search.** Finds maximum throughput under a user-defined latency SLA using two-phase search: exponential ramp-up (×2.0 then ×1.2) to find the boundary, then binary search converging at 5% precision. Starting concurrency uses a 60% factor to avoid wasting GPU time on overloaded tests. Finds the SLA boundary in ~4–6 tests.

**Step 11 — Calibrated Load Validation.** Re-tests the best PD and aggregated configs at sustainable concurrency (derived from Step 4 capacity analysis) to produce realistic numbers when the user's requested load exceeds cluster capacity.

**Additional innovations:** (a) Prefix cache simulation with three tokenizer-validated dataset modes (identical, shared prefix, multi-group) seeded deterministically for reproducibility. (b) State reconstruction from database on resume — all optimizer state (TPSG calibration, Pareto front, baselines) is rebuilt from completed tests, enabling EPP tuning and later steps without re-running earlier ones. (c) Automatic `kubectl port-forward` to remote Prometheus when the UI and test clusters differ. (d) Production-ready Kubernetes manifests and EPP configmaps generated for every recommendation.

Complete formulas and derivations: `docs/optimization-math.md`. System diagrams: `docs/diagrams.html`.
