# Inftune Studio — System Diagrams

All diagrams use Mermaid syntax and render natively on GitHub.

---

## 1. System Architecture Overview

```mermaid
graph TB
    subgraph Launcher["Launcher (Control Plane)"]
        LP[Launcher Pod<br>Flask + SocketIO]
        LDB[(launcher.db<br>Users, Clusters, Instances)]
        LP --> LDB
    end

    subgraph Instance["Instance (per-user)"]
        IP[Inftune Studio Pod<br>Flask + SocketIO]
        IDB[(inftune.db<br>Runs, Results, Config)]
        IP --> IDB
    end

    subgraph K8s["Target Cluster"]
        LWS[LeaderWorkerSet<br>vLLM Pods]
        GW[Inference Gateway<br>+ EPP]
        GL[Guidellm<br>Benchmark Runner]
        PM[Prometheus<br>Metrics]
        LWS --> PM
        GW --> LWS
        GL --> GW
    end

    User([User Browser]) --> LP
    LP -->|Creates Instance| IP
    IP -->|Deploys| LWS
    IP -->|Configures| GW
    IP -->|Runs Benchmarks| GL
    IP -->|"Collects Metrics<br>(port-forward for<br>remote clusters)"| PM

    style Launcher fill:#f0f9fa,stroke:#2A7B88
    style Instance fill:#fff7ed,stroke:#d97706
    style K8s fill:#f0fdf4,stroke:#22c55e
```

---

## 2. Optimization Pipeline (11 Steps)

```mermaid
flowchart TD
    S1[Step 1: Initialization<br>Scan cluster, detect GPUs,<br>RDMA, cloud provider] --> S2

    S2[Step 2: Decode TP Sweep<br>Test all valid TPs with ISL=1<br>Measure decode throughput] --> S3

    S3[Step 3: Prefill TP Sweep<br>Test all valid TPs with OSL=1<br>Measure prefill throughput] --> S4

    S4[Step 4: Capacity Analysis<br>GPU cost per request,<br>sustainable throughput] --> S5

    S5[Step 5: Feasible Splits<br>Smart PD Search formula,<br>NIXL constraints] --> S6

    S6[Step 6: Aggregated Search<br>Test all TPs at full workload<br>with all GPUs] --> S7

    S7[Step 7: PD / EP Testing<br>Test feasible splits,<br>build Pareto front] --> S8

    S8[Step 8: Architecture Comparison<br>PD vs Aggregated vs EP<br>No new tests] --> S9

    S9{EPP Benchmark<br>enabled?}
    S9 -->|Yes| S9a[Step 9: EPP Tuning<br>Derive weights from<br>Prometheus metrics]
    S9 -->|No| S10

    S9a --> S10

    S10{Latency SLA<br>enabled?}
    S10 -->|Yes| S10a[Step 10: Latency Search<br>Binary search for max<br>throughput under SLA]
    S10 -->|No| S11

    S10a --> S11

    S11{Overloaded?}
    S11 -->|Yes| S11a[Step 11: Calibrated Load<br>Re-test at sustainable<br>concurrency]
    S11 -->|No| DONE

    S11a --> DONE[Results & Report<br>Pareto front,<br>recommendations]

    style S1 fill:#e0f2fe,stroke:#0ea5e9
    style S2 fill:#fef3c7,stroke:#f59e0b
    style S3 fill:#fef3c7,stroke:#f59e0b
    style S5 fill:#f5f3ff,stroke:#8b5cf6
    style S7 fill:#f0fdf4,stroke:#22c55e
    style DONE fill:#ecfdf5,stroke:#10b981
```

---

## 3. Smart PD Search Algorithm

```mermaid
flowchart TD
    CAL[Steps 2-3: TP Calibration<br>Measure prefill & decode<br>throughput per TP] --> PAIRS

    PAIRS[Select top-N TP pairs<br>e.g. prefill_tp=8, decode_tp=1<br>prefill_tp=4, decode_tp=2] --> RATIO

    RATIO[Calculate throughput ratio<br>r = decode_thr / prefill_thr] --> IDEAL

    IDEAL["Compute ideal decode pods<br>D_ideal = GPUs / (r × prefill_tp + decode_tp)"] --> CANDIDATES

    CANDIDATES["Generate candidates<br>floor(D), ceil(D), ±1<br>~3 configs per TP pair"] --> NIXL

    NIXL{Asymmetric TP<br>check}
    NIXL -->|"prefill_tp ≤ decode_tp<br>OR allow_asymmetric=true"| TEST
    NIXL -->|"prefill_tp > decode_tp"| SKIP[Skip: NIXL KV transfer<br>crashes with asymmetric TP<br>vllm#43523]

    TEST[Run benchmark<br>for each candidate] --> PARETO

    PARETO[Build Pareto front<br>TTFT P99 vs Throughput P90]

    style CAL fill:#fef3c7,stroke:#f59e0b
    style IDEAL fill:#f5f3ff,stroke:#8b5cf6
    style PARETO fill:#ecfdf5,stroke:#10b981
```

### Comparison: Smart vs Exhaustive Search

```mermaid
graph LR
    subgraph Exhaustive["Exhaustive Manual Search"]
        E1[32 GPU cluster] --> E2[All valid splits:<br>132 configurations]
        E2 --> E3[132 benchmark runs<br>~1 hour each manually]
        E3 --> E4[Total: ~132 hours<br>5.5 days]
    end

    subgraph Smart["Smart PD Search (Automated)"]
        S1[32 GPU cluster] --> S2[Mathematical calculation:<br>~3 per TP pair]
        S2 --> S3[~6 benchmark runs<br>~10 min each automated]
        S3 --> S4[Total: ~1 hour]
    end

    style Exhaustive fill:#fef2f2,stroke:#dc2626
    style Smart fill:#f0fdf4,stroke:#22c55e
```

---

## 4. Single Test Execution Flow

```mermaid
sequenceDiagram
    participant U as User
    participant W as Wizard UI
    participant O as Optimizer
    participant K as Kubernetes
    participant G as Guidellm
    participant P as Prometheus

    U->>W: Select config from<br>report recommendation
    W->>W: Pre-fill wizard with<br>all test settings
    U->>W: Confirm & Start
    W->>O: SingleTestStrategy<br>(architecture, TP, pods, EPP)

    O->>K: Deploy prerequisites<br>(Gateway, EPP, RBAC)
    O->>K: Deploy vLLM pods<br>(LWS manifest)
    K-->>O: Pods Running

    O->>K: Wait for vLLM<br>model loading
    K-->>O: Model loaded,<br>profiled memory data

    O->>G: Run benchmark<br>(guidellm)
    G->>K: Send requests via<br>Inference Gateway
    G-->>O: Benchmark results

    O->>P: Collect GPU metrics,<br>vLLM metrics
    P-->>O: TTFT, throughput,<br>GPU utilization

    O->>K: Cleanup deployment
    O-->>W: Results saved to DB
    W-->>U: Report with results
```

---

## 5. Deployment Lifecycle (PD Architecture)

```mermaid
sequenceDiagram
    participant DM as Deployment Manager
    participant K as Kubernetes
    participant V as vLLM Pods

    Note over DM: Deploy highest GPU<br>requirement first

    DM->>K: Apply decode LWS<br>(e.g. TP=8, 1 pod)
    loop Wait for running
        DM->>K: Check LWS status
        K-->>DM: replicas: 0/1
        Note over DM: Poll every 5s
    end
    K-->>DM: replicas: 1/1 ✓

    DM->>K: Apply prefill LWS<br>(e.g. TP=4, 2 pods)
    loop Wait for running
        DM->>K: Check LWS status
        K-->>DM: replicas: 1/2
    end
    K-->>DM: replicas: 2/2 ✓

    DM->>K: Apply services

    loop Wait for model loading
        DM->>V: Stream pod logs
        V-->>DM: "Loading model..."
    end
    V-->>DM: "Application startup complete"
    Note over DM: Extract profiled<br>memory data from logs

    alt Pod stuck > 180s
        DM->>K: Delete stuck pod
        K->>K: LWS recreates pod
        Note over DM: Max 3 retries
    end
```

---

## 6. Cluster Scanning & Auto-Detection

```mermaid
flowchart TD
    SCAN[Scan Cluster] --> GPU
    SCAN --> NET
    SCAN --> CLOUD
    SCAN --> STORAGE

    GPU[GPU Detection] --> GPU1[Node allocatable:<br>nvidia.com/gpu]
    GPU1 --> GPU2[GPU model from labels<br>nvidia.com/gpu.product]
    GPU2 --> GPU3[VRAM from labels<br>or model lookup]

    NET[Network Detection] --> NET1{RDMA resources<br>in allocatable?}
    NET1 -->|Yes| NET2[Count physical NICs]
    NET1 -->|No| NET3[No RDMA]
    NET2 --> NIC1[Try port labels]
    NIC1 -->|Found| NICOK[NIC count]
    NIC1 -->|Not found| NIC2[Try speed labels]
    NIC2 -->|Found| NICOK
    NIC2 -->|Not found| NIC3[Try device plugin ConfigMap]
    NIC3 -->|Found| NICOK
    NIC3 -->|Not found| NIC4[Fallback: 1 NIC per GPU]
    NIC4 --> NICOK

    CLOUD[Cloud Provider] --> CL1{OpenShift?}
    CL1 -->|Yes| CL2[Read infrastructure<br>resource → platform]
    CL1 -->|No| CL3{CoreWeave labels?}
    CL3 -->|Yes| CL4[CoreWeave]
    CL3 -->|No| CL5[Bare Metal]

    STORAGE[Storage Classes] --> ST1[List all StorageClass<br>resources]

    GPU3 --> RESULT[ClusterResources]
    NICOK --> RESULT
    CL2 --> RESULT
    CL4 --> RESULT
    CL5 --> RESULT
    ST1 --> RESULT
    NET3 --> RESULT

    style SCAN fill:#e0f2fe,stroke:#0ea5e9
    style RESULT fill:#ecfdf5,stroke:#10b981
```

---

## 7. Network Mode Selection

```mermaid
flowchart TD
    DETECT[Detect Cloud Provider] --> IBM{IBM Cloud?}

    IBM -->|Yes| DRA_CHECK{DRA resources<br>available?}
    DRA_CHECK -->|Yes| DRA[DRA Mode<br>GPU+NIC pairing via<br>PCIe affinity webhook]
    DRA_CHECK -->|No| NAD

    IBM -->|No| CW{CoreWeave?}
    CW -->|Yes| SHARED[Shared Device Mode<br>Pods request rdma/ib<br>from device plugin]
    CW -->|No| BM{Bare Metal?}

    BM -->|Yes| NAD[NAD Mode<br>Explicit NIC assignment<br>via Multus CNI]
    BM -->|No| OTHER{RDMA available?}
    OTHER -->|Yes| SHARED
    OTHER -->|No| TCP[TCP Only<br>No RDMA acceleration]

    style DRA fill:#f5f3ff,stroke:#8b5cf6
    style NAD fill:#fef3c7,stroke:#f59e0b
    style SHARED fill:#f0fdf4,stroke:#22c55e
    style TCP fill:#f0f0f0,stroke:#999
```

---

## 8. Model Architecture Detection

```mermaid
flowchart TD
    START[Model Name<br>e.g. RedHatAI/gpt-oss-20b] --> PVC{Model on<br>local PVC?}

    PVC -->|Yes| READ[Read config.json<br>from PVC volume]
    PVC -->|No| HF[Fetch config.json<br>from HuggingFace API]

    HF --> HF_OK{Success?}
    HF_OK -->|Yes| PARSE
    HF_OK -->|401/403| TOKEN[Retry with<br>HF token]
    HF_OK -->|Fail| LOOKUP[Hardcoded lookup table<br>Llama, Qwen, Mixtral, etc.]

    TOKEN --> HF_OK2{Success?}
    HF_OK2 -->|Yes| PARSE
    HF_OK2 -->|No| LOOKUP

    READ --> PARSE
    LOOKUP --> DEFAULTS[Conservative defaults<br>40 layers, 8 KV heads, 128 head_dim]

    PARSE[Parse config.json] --> ARCH{MoE model?<br>num_local_experts?}
    ARCH -->|Yes| MOE[MoE: effective_size =<br>experts × expert_size / 1.2]
    ARCH -->|No| DENSE[Dense model]

    MOE --> EXTRACT
    DENSE --> EXTRACT
    DEFAULTS --> EXTRACT

    EXTRACT[Extract:<br>hidden_size, layers,<br>num_kv_heads, head_dim] --> GQA{num_kv_heads <<br>num_attention_heads?}

    GQA -->|Yes| GQA_YES[GQA: fewer KV heads<br>shared across attn heads<br>→ smaller KV cache]
    GQA -->|No| MHA[MHA: KV heads = attn heads<br>→ larger KV cache]

    GQA_YES --> VRAM[Calculate VRAM<br>requirements & min TP]
    MHA --> VRAM

    style START fill:#e0f2fe,stroke:#0ea5e9
    style PARSE fill:#fef3c7,stroke:#f59e0b
    style VRAM fill:#ecfdf5,stroke:#10b981
```

---

## 9. Prefix Cache Simulation Modes

```mermaid
flowchart LR
    subgraph identical["Identical Mode"]
        I1["80% of requests →<br>same prompt A"]
        I2["20% of requests →<br>unique prompts"]
        I1 --- I3["Cache: prompt A<br>always cached"]
    end

    subgraph shared["Shared Prefix Mode"]
        S1["All requests share<br>first 80% of tokens<br>(system prompt)"]
        S2["Each request has<br>unique 20% suffix<br>(user message)"]
        S1 --- S3["Cache: shared prefix<br>always hit"]
    end

    subgraph multi["Multi-Group Mode"]
        M1["Group 1: tenant A<br>identical prompts"]
        M2["Group 2: tenant B<br>identical prompts"]
        M3["Group N: tenant N<br>identical prompts"]
        M4["Remaining:<br>unique prompts"]
        M1 --- M5["Cache: per-tenant<br>prompt cached"]
    end

    style identical fill:#f0fdf4,stroke:#22c55e
    style shared fill:#e0f2fe,stroke:#0ea5e9
    style multi fill:#f5f3ff,stroke:#8b5cf6
```

---

## 10. EPP Routing Decision & Metrics-Driven Weight Derivation

```mermaid
flowchart TD
    REQ[Incoming Request] --> EPP[EPP Endpoint Picker]

    EPP --> SCORE[Score each vLLM server]

    SCORE --> PC[Prefix Cache Score<br>vllm_prefix_cache_hits_rate]
    SCORE --> KV[KV Cache Score<br>vllm_kv_cache_pct]
    SCORE --> QU[Queue Score<br>vllm_requests_waiting]
    SCORE --> AR[Active Request Score<br>vllm_requests_running]
    SCORE --> SLO[SLO Score<br>vllm_ttft_p99 vs target]

    PC --> WEIGHTED["Weighted sum:<br>score = w₁×prefix + w₂×kv + w₃×queue + w₄×active + w₅×slo"]
    KV --> WEIGHTED
    QU --> WEIGHTED
    AR --> WEIGHTED
    SLO --> WEIGHTED

    WEIGHTED --> BEST[Route to highest<br>scoring server]

    subgraph Derivation["Step 9: Smart Weight Derivation"]
        PROM[Prometheus Metrics<br>from Step 7] --> DERIVE
        DERIVE["Derive weights from<br>measured KV pressure,<br>queue depth, active load,<br>cache hit rate, tail latency"]
        DERIVE --> TEST_W[Test derived weights<br>swap ConfigMap only ~10s]
        TEST_W --> COMPARE{Better than<br>baseline?}
        COMPARE -->|Yes| USE[Use derived weights]
        COMPARE -->|No| FALL[Balanced fallback 2:2:2:2]
    end

    Derivation -.-> WEIGHTED

    style EPP fill:#f5f3ff,stroke:#8b5cf6
    style BEST fill:#ecfdf5,stroke:#10b981
    style PROM fill:#fef3c7,stroke:#f59e0b
```

---

## 11. Latency-Bounded Throughput Search (Step 10)

```mermaid
flowchart TD
    START["Estimate starting concurrency<br>C₀ = throughput × (target_ms / observed_ms) × 0.6"] --> PHASE1

    subgraph PHASE1["Phase 1: Exponential Ramp-Up"]
        T1[Test at C₀] --> CHECK1{Latency <<br>target?}
        CHECK1 -->|Yes, far from limit| DOUBLE["C = C × 2.0"]
        CHECK1 -->|Yes, approaching| BUMP["C = C × 1.2"]
        CHECK1 -->|No| FOUND[Found upper bound]
        DOUBLE --> T1
        BUMP --> T1
    end

    FOUND --> PHASE2

    subgraph PHASE2["Phase 2: Binary Search"]
        BS["low = last good C<br>high = first bad C"] --> MID["Test at mid = (low+high)/2"]
        MID --> CHECK2{Latency <<br>target?}
        CHECK2 -->|Yes| UP["low = mid"]
        CHECK2 -->|No| DOWN["high = mid"]
        UP --> CONV{"|high-low|/low<br>< 5%?"}
        DOWN --> CONV
        CONV -->|No| MID
        CONV -->|Yes| RESULT["Max throughput<br>under SLA = low"]
    end

    style START fill:#e0f2fe,stroke:#0ea5e9
    style RESULT fill:#ecfdf5,stroke:#10b981
```

---

## 12. Multi-Cluster Management

```mermaid
flowchart TD
    ADMIN[Admin User] --> LAUNCHER[Launcher Dashboard]

    LAUNCHER --> C1[Cluster: Local<br>Current kubectl context]
    LAUNCHER --> C2[Cluster: IBM Cloud<br>Kubeconfig stored as<br>K8s Secret]
    LAUNCHER --> C3[Cluster: CoreWeave<br>Kubeconfig stored as<br>K8s Secret]

    C1 --> I1[Instance: team-dev<br>5 GPUs, 1 node]
    C1 --> I2[Instance: prod-test<br>16 GPUs, 2 nodes]

    C2 --> I3[Instance: frankfurt-bench<br>32 GPUs, 4 nodes]

    C3 --> I4[Instance: h200-eval<br>8 GPUs, 1 node]

    I1 --> WIZ1[Wizard → Optimize → Report]
    I3 --> WIZ2[Wizard → Optimize → Report]

    subgraph Each Instance
        DB[(inftune.db)]
        OPT[Optimizer Pipeline]
        RPT[Report & Charts]
        OPT --> DB
        DB --> RPT
    end

    style LAUNCHER fill:#f0f9fa,stroke:#2A7B88
    style C1 fill:#f0fdf4,stroke:#22c55e
    style C2 fill:#e0f2fe,stroke:#0ea5e9
    style C3 fill:#fef3c7,stroke:#f59e0b
```
