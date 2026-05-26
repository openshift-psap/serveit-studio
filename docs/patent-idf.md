# Invention Disclosure Form — Inftune Studio

## Section 1: Inventors and Summary

**Title of the Invention:**
Recipe-Based Automated Optimization of Disaggregated LLM Inference Configurations Across Multi-Architecture Kubernetes Deployments

**Primary Inventor Contact:**
Boaz Ben Shabat (bbenshab)

**Business Entity:** Red Hat, Inc.

**Business Unit:** Other

**Primary Technology Category:** 059 - High Performance Computing

**Other relevant categories:**
- 094 - Quality of Service
- 005 - Application Performance Management

---

## Section 2: Invention Description

### Problem Description

Deploying Large Language Models (LLMs) for inference at scale requires choosing from hundreds of possible configurations — tensor parallelism (TP) size, number of pods, Prefill/Decode disaggregation ratios, Expert Parallelism settings, request routing weights, and KV cache parameters. Today, engineers manually guess configurations, deploy them, benchmark, and iterate — a process that takes days or weeks per model and often produces suboptimal results.

**The fundamental gap in existing solutions:** Current tools and vendor recommendations (NVIDIA NIM, llm-d Planner, cloud provider sizing guides) rely on pre-computed benchmarks for specific models at specific request rates on specific hardware. They provide **estimations** based on historical data — not measurements from the user's actual cluster, actual model, actual workload, and actual concurrency level. A recommendation that assumes 50 concurrent users on A100 GPUs tells you nothing about how your specific cluster with 100 concurrent users on H200, H100, L40S, or any other GPU will actually perform. Real-world performance varies dramatically based on factors that are unique to each deployment: RDMA vs Ethernet networking, InfiniBand switch topology and fabric congestion, NCCL inter-GPU communication overhead, GPU memory fragmentation under load, KV cache eviction patterns at the user's specific concurrency level, prefix cache hit rates with the user's actual prompt distribution, model quantization behavior (FP8 vs FP16 vs BF16), Kubernetes scheduling constraints and pod placement, noisy neighbor effects from other workloads sharing the same nodes or network fabric, PCIe bandwidth contention between GPUs and NICs, and driver/firmware version differences across GPU generations. These factors interact in complex, hardware-specific ways that cannot be predicted from static benchmarks or analytical models — they must be measured on the user's actual infrastructure with the user's actual workload.

The configuration space grows combinatorially with GPU count. A 32-GPU cluster has **132 valid Prefill/Decode split configurations** across all TP pair combinations (4 TP options × 4 TP options × variable pod counts). A 64-GPU cluster has **280 valid configurations**. When an engineer manually tests a configuration, the full cycle — choosing parameters, writing manifests, deploying pods, waiting for model loading, running a benchmark, analyzing results, and deciding next steps — takes roughly **1 hour per test**. Exhaustive manual search of a 32-GPU cluster would take **~132 hours (over 5 days)** of continuous engineer time. A 64-GPU cluster would take **~280 hours (nearly 12 days)**.

No existing tool takes the user's specific workload parameters (model, input/output length, concurrent users, latency requirements), deploys real inference pods on the user's own cluster, runs benchmarks with the user's actual concurrency and sequence lengths, and discovers the optimal configuration from measured data — not estimations.

### Detailed Description

The invention is an automated 11-step recipe-based optimization pipeline that deploys vLLM inference pods on Kubernetes, benchmarks them with real workloads, and discovers optimal configurations without manual intervention.

**Key novel contributions:**

**1. Smart PD Search Algorithm**

A mathematical method to calculate the near-optimal Prefill/Decode GPU split from calibration data, reducing the search space from 132+ configurations to **~6 tests** (22× reduction for a 32-GPU cluster, 46× for 64 GPUs).

The formula derives the ideal decode pod count:

```
r = decode_throughput_p90 / prefill_throughput_p90
D_ideal = total_gpus / (r × prefill_tp + decode_tp)
```

This exploits the insight that optimal P/D splits occur where the prefill and decode GPU pools are balanced — neither bottlenecked while the other is idle. The balance constraint (`prefill_throughput × P = decode_throughput × D`) combined with the GPU constraint (`P × prefill_tp + D × decode_tp = total_gpus`) yields a closed-form solution.

Only `floor(D_ideal)`, `ceil(D_ideal)`, and ±1 neighbors are tested (~3 per TP pair), finding configurations within 1–2% of the exhaustive optimum.

| Cluster Size | Exhaustive Configs | Smart PD Search (top-2) | Reduction | Manual Time (~1hr/test) | Automated Smart Time |
|-------------|-------------------|------------------------|-----------|------------------------|---------------------|
| 16 GPUs | 58 | ~6 | 10× | ~58 hours (2.4 days) | ~1 hour |
| 32 GPUs | 132 | ~6 | 22× | ~132 hours (5.5 days) | ~1 hour |
| 64 GPUs | 280 | ~6 | 46× | ~280 hours (11.7 days) | ~1 hour |

**2. TP Calibration with Isolated Measurement**

Steps 2–3 isolate prefill and decode performance by setting ISL=1 (pure decode measurement) and OSL=1 (pure prefill measurement), using Tokens Per Second Per GPU (TPSG) normalization to enable fair comparison across different TP values. This per-GPU normalization (`TPSG = throughput × sequence_length / TP`) allows comparing TP=2 against TP=8 on equal footing.

**3. Profiled Memory Auto-Tuning**

Rather than using heuristic estimates, the system profiles actual vLLM memory overhead from pod logs during calibration:

```
gpu_memory_utilization = min((gpu_vram - measured_overhead - 2GB_fragmentation) / gpu_vram, 0.95)
```

This adapts to each model's actual memory footprint including CUDA graphs and activation buffers — data that theoretical calculations cannot predict.

**4. Block Size Auto-Tuning**

KV cache block size is computed as:

```
block_size = next_power_of_2(sqrt(ISL + OSL))
Clamped to [8, 512], floor = 128 for PD mode
```

The square root balances management overhead (too many small blocks) against internal fragmentation (too few large blocks). The PD floor of 128 ensures NIXL KV cache transfer efficiency — each block transfer has fixed overhead, so larger blocks amortize it.

**5. Multi-Architecture Comparison with P99 Pareto Front**

The pipeline automatically tests three inference architectures — Aggregated (standard), Prefill/Decode (disaggregated), and Expert Parallelism (MoE with PD disaggregation and expert-parallel flags) — on the same hardware and workload, then compares them on a Pareto front of TTFT P99 (tail latency) vs throughput P90. Using P99 instead of P90 for the Pareto front penalizes configurations with unstable tail latency that appear fast at P90 but collapse at P99 (e.g., 920ms P90 but 264,000ms P99). This is the first system to perform automated cross-architecture comparison with tail-latency-aware selection.

**6. Metrics-Driven EPP Weight Derivation**

Automated tuning of the Endpoint Picker's request routing weights using real Prometheus metrics collected during Step 7 benchmarks. Instead of brute-force testing preset combinations, the system derives near-optimal weights mathematically from five measured dimensions:

- **Prefix cache**: `vllm_prefix_cache_hits_rate / queries_rate` — actual cache hit effectiveness
- **KV cache**: `vllm_kv_cache_pct` — measured memory pressure per pod
- **Queue**: `vllm_requests_waiting / (running + waiting)` — actual queue imbalance
- **Active requests**: `vllm_requests_running / max_num_seqs` — pod saturation level
- **SLO** (when latency SLA enabled): `vllm_ttft_p99` vs target — tail latency overshoot

Each weight is proportional to the time impact of optimal routing on that dimension. The system tests the derived weights by swapping only the gateway ConfigMap (~10 seconds), isolating routing impact from pod configuration. Falls back to balanced weights if derived weights degrade performance.

**7. Latency-Bounded Binary Search**

Finds maximum throughput under a user-defined latency SLA:

```
Starting concurrency = throughput × (target_ms / observed_ms) × 0.6
Phase 1: Exponential ramp-up (×2.0 then ×1.2)
Phase 2: Binary search, converge at 5% precision
```

The 60% starting factor avoids wasting GPU time on overloaded tests. The two-phase approach finds the SLA boundary in ~4–6 tests instead of linear scanning.

**8. Prefix Cache Simulation**

Generates synthetic datasets with three deterministic modes — identical prompts, shared prefix (system prompt pattern), and multi-group clustering (multi-tenant) — to test prefix cache effectiveness. Prompts are generated using the model's actual tokenizer to guarantee exact token counts (preventing prompt-too-long rejections from BPE subword overestimation). Pool size is derived from estimated available KV cache capacity, and all randomness is seeded from a config hash for reproducibility.

**9. Auto-Adaptation Pipeline**

Automatically detects and adapts to:
- Cloud provider (IBM Cloud, CoreWeave, AWS, GCP, Azure, bare metal) via OpenShift infrastructure resources or node labels
- GPU model and VRAM via node allocatable and labels
- RDMA NIC count via four fallback strategies (port labels → speed labels → device plugin ConfigMap → GPU count)
- Network type (DRA GPU+NIC pairing, NAD Multus CNI, shared RDMA device plugin)
- Model architecture (dense vs MoE, GQA vs MHA) via HuggingFace config.json with hardcoded fallback table

No manual configuration is required.

**10. State Reconstruction on Resume**

The pipeline supports mid-run resume: if an optimization is interrupted and later resumed, all in-memory optimizer state (calibration TPSG results, optimal TP selections, aggregated baselines, Pareto front) is reconstructed from the database. This allows later steps (EPP tuning, calibrated load validation) to use data from earlier steps that were skipped on resume, without re-running them.

**11. Report-to-Test Flow**

The system stores complete test configurations (`test_config_json`) in the database, enabling a "Reuse" mode where users can pre-fill the wizard with any recommended configuration from the report page, and a "Single Test" mode to re-run an exact configuration with one click. All settings — model, workload, EPP weights, deployment parameters, prefix cache, latency SLA — are restored automatically.

**12. Prometheus Metrics Port-Forward for Remote Clusters**

When the optimization UI runs on one cluster (e.g., OpenShift) but test pods run on a remote cluster (e.g., vanilla K8s), the system automatically establishes a `kubectl port-forward` to the remote Prometheus service using the test cluster's kubeconfig. This enables vLLM metrics collection (KV cache utilization, queue depth, request rates) without requiring external Prometheus exposure or cross-cluster networking.

**Attached diagrams:** See `docs/diagrams.md` (12 Mermaid diagrams), `docs/optimization-math.md` (complete formula reference), `docs/supporting-material.md` (detection and lifecycle details).

---

### Prior Art Information

**Are you aware of similar inventions?**

- **vLLM** (UC Berkeley, 2023) — The inference engine itself. Handles model serving, KV cache management, PagedAttention. Does NOT optimize deployment configurations or compare architectures.
- **SGLang** (Stanford, 2024) — Alternative inference engine with RadixAttention for prefix caching. Does NOT provide automated configuration search.
- **llm-d** (Red Hat, 2024) — Disaggregated inference framework with NIXL KV cache transfer and EPP routing. Provides the runtime but NOT automated optimization of configurations.
- **Anyscale/Ray Serve** — General-purpose model serving with autoscaling. Does NOT perform multi-architecture comparison or calculate optimal PD splits.
- **BentoML, TensorRT-LLM** — Inference serving frameworks. None perform automated TP/PD/EP sweep with mathematical split optimization.
- **guidellm** (Neural Magic, 2024) — Benchmark tool for LLM inference. Used as a component in this invention but does NOT optimize configurations.
- **Optuna / Hyperparameter tuning frameworks** — General optimization frameworks. Could theoretically be applied but lack domain-specific knowledge (NIXL constraints, TPSG normalization, PD balance equations). Would require many more trials to converge.

**The critical distinction:** Every existing tool provides recommendations based on estimations from pre-computed benchmark data for specific models on specific hardware. This invention is the first to deploy, benchmark, and optimize on the **user's own cluster** with the **user's own workload parameters** — producing recommendations from measured data, not predictions. No existing tool combines: (a) automated multi-architecture deployment on the user's Kubernetes cluster, (b) real benchmarks with the user's specific concurrency, sequence lengths, and model, (c) mathematical PD split optimization from live calibration data, (d) metrics-driven EPP weight derivation from real Prometheus data, (e) P99 tail-latency-aware Pareto selection, and (f) production-ready manifest generation.

**What advantages does your invention have over identified prior art?**

1. **Measured, not estimated** — The only system that benchmarks on the user's actual cluster with the user's actual workload (model, ISL, OSL, concurrency). Existing tools provide estimations from pre-computed data for specific models on specific hardware — this invention produces recommendations from real measurements that account for the user's specific RDMA topology, GPU memory fragmentation, KV cache behavior, and Kubernetes scheduling.
2. **22–46× faster search** — Smart PD Search tests ~6 configs vs 132–280 exhaustive, finding configurations within 1–2% of optimal. Reduces 5+ days of manual testing to ~1 hour.
3. **No manual configuration** — Auto-detects hardware, model architecture, network type, and cloud provider constraints.
4. **Architecture-agnostic comparison** — First system to automatically compare Aggregated vs PD vs EP on the same hardware and workload.
5. **Production-ready output** — Generates deployable Kubernetes manifests and EPP configmaps, not just tuning recommendations.
6. **Profiled accuracy** — Memory utilization derived from actual vLLM pod logs, not theoretical estimates, eliminating OOM crashes and wasted VRAM.
7. **Metrics-driven routing optimization** — EPP weights derived from measured Prometheus metrics (KV cache utilization, queue depth, active requests, cache hit rates) rather than heuristic presets.
8. **Tail-latency-aware selection** — Pareto front uses P99 TTFT to prevent recommending configurations with good P90 but catastrophic tail latency.

---

## Section 3: Conception and Other Events

**When was the invention first conceived?**
*(Fill in the month/year of your earliest design or prototype work)*

**Has the invention been publicly disclosed?**

No. The source code repository is private on GitHub (github.com/bbenshab/inftune-studio). No public demos, blog posts, conference talks, or papers have been published. No code has been pushed to any public repository. The invention has not been shared outside of Red Hat.

**Date of first public disclosure:**
N/A — no public disclosure has occurred.

**Sales or marketing activities:**
No sales or marketing activities have occurred or been planned. The product has been used internally for development and testing only.

**Will this invention be incorporated into a Red Hat product?**
Yes — this relates to the llm-d project and Red Hat OpenShift AI inference optimization.

---

## Section 4: Value of the Invention

### Business Value

**Likelihood that proprietary companies will want to use this:** High

Any company deploying LLMs at scale faces the same configuration optimization problem. Specific companies and relevance:

- **NVIDIA** — NIM (NVIDIA Inference Microservices) deploys vLLM but requires manual configuration tuning. This invention automates what their customers do manually.
- **Amazon (AWS)** — SageMaker inference endpoints require manual TP/instance selection. Automated optimization would reduce customer GPU costs.
- **Microsoft (Azure)** — Azure ML model deployments face the same manual tuning. Their Olive toolkit handles model compilation but not deployment configuration optimization.
- **Google (GCP)** — Vertex AI model serving requires manual resource configuration.
- **Anyscale** — Ray Serve could integrate similar optimization for their managed LLM deployment offering.
- **CoreWeave** — GPU cloud provider whose customers manually optimize vLLM deployments on their infrastructure.

### Detectability

**Likelihood of detecting use in a competitor's product:** Medium-High

The system produces several distinctive signatures:
- Approximately 3 test deployments per TP pair instead of exhaustive search
- Calibration runs with ISL=1 / OSL=1 (isolated prefill/decode measurement)
- The specific formula `D = GPUs / (r × prefill_tp + decode_tp)` visible in configuration outputs or documentation
- Block size auto-tuning heuristic (`sqrt(ISL+OSL)` rounded to power of 2) identifiable in generated configurations
- TPSG normalization (`throughput × seq_len / TP`) as a comparison metric
- P99 TTFT used for Pareto dominance instead of the standard P90
- EPP weights derived from five measured vLLM Prometheus metrics (prefix cache hits, KV utilization, queue depth, active requests, TTFT P99)
- `kubectl port-forward` to remote Prometheus for cross-cluster metrics collection

A competitor implementing these techniques would be detectable through their documentation, API parameters, published benchmarks, or observable deployment patterns.

### Design Arounds

1. **Exhaustive search** — Test all valid configurations without Smart PD Search. Disadvantage: 22–46× slower, requires 132–280 benchmark runs at ~1 hour each manually (5–12 days of engineer time). Cost-prohibitive for production use.
2. **Analytical modeling / estimation tools** — Use pre-computed benchmark data or theoretical performance models (e.g., llm-d Planner, vendor sizing guides). Disadvantage: Recommendations are based on specific models tested on specific hardware at specific rates. They cannot predict how a different model with different concurrency will perform on the user's specific cluster with its unique RDMA topology, memory fragmentation, and scheduling constraints. Estimations miss real-world effects that only live benchmarking reveals.
3. **Bayesian optimization (Optuna/similar)** — Use generic hyperparameter tuning. Disadvantage: Treats the problem as a black box, ignoring domain knowledge (NIXL constraints, TPSG normalization, PD balance equations). Requires many more trials to converge. Does not exploit the closed-form solution that exists for balanced PD splits.
4. **Manual tuning with heuristics** — Current industry practice. Disadvantage: Requires deep expertise in vLLM internals, NCCL, RDMA, and Kubernetes scheduling. Takes days to weeks per model. Often produces suboptimal results because engineers cannot test enough configurations and rely on rules of thumb that don't account for their specific hardware and workload.

All alternatives are significantly slower, less accurate, or require domain expertise that the automated pipeline eliminates.

### Comments

**Have you discussed this invention with a member of the Patent Team or an Inventor Mentor?**
*(Fill in)*

**Inventor Comments:**

Complete technical documentation is available in the private repository (github.com/bbenshab/inftune-studio):
- `docs/optimization-math.md` — All formulas, derivations, and constants with justification
- `docs/supporting-material.md` — Model detection, cloud/network auto-discovery, deployment lifecycle
- `docs/diagrams.md` — 12 Mermaid diagrams covering all system flows
- `core/` — Source code for the optimization pipeline

The three documentation files together provide a complete technical specification suitable for patent claims drafting.
