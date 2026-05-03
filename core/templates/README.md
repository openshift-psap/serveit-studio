# InfeRecipe Deployment Templates

This directory contains Jinja2 templates for deploying different LLM inference architectures for optimization testing.

## Decision Matrix

InfeRecipe helps users find the optimal deployment architecture based on their priorities:

| User Priority | Architectures Tested | Why |
|--------------|---------------------|-----|
| **Throughput** | Aggregated vs EP (Expert Parallelism) | Benchmarks traditional aggregated inference against expert-parallel routing to identify which delivers maximum throughput for your workload |
| **Response Time** | Aggregated vs PD (Prefill/Decode) | Benchmarks traditional aggregated inference against prefill/decode disaggregation to determine which minimizes TTFT for your workload |
| **Balanced** | Aggregated vs PD vs EP | Evaluates all three to find the optimal balance between TTFT and throughput |

## Directory Structure

```
templates/
├── aggregated/          # Single aggregated vLLM deployment
│   ├── lws.yaml.j2     # LeaderWorkerSet template
│   ├── service.yaml.j2 # Service template
│   └── configmap.yaml.j2
├── pd/                  # Prefill/Decode disaggregation
│   ├── prefill-lws.yaml.j2
│   ├── decode-lws.yaml.j2
│   ├── prefill-service.yaml.j2
│   ├── decode-service.yaml.j2
│   └── configmap.yaml.j2
└── ep/                  # Expert Parallelism
    ├── prefill-lws.yaml.j2
    ├── decode-lws.yaml.j2
    ├── service.yaml.j2
    └── configmap.yaml.j2
```

## Template Variables

### Common Variables

All templates support these variables:

- `model_name`: HuggingFace model name (e.g., `RedHatAI/Qwen3-235B-A22B-FP8-dynamic`)
- `namespace`: Kubernetes namespace (default: `llm-d`)
- `tensor_parallelism`: TP size (e.g., 1, 2, 4, 8)
- `replicas`: Number of pod replicas
- `max_model_len`: Maximum sequence length (e.g., 8192, 40960)
- `gpu_memory_utilization`: GPU memory fraction (e.g., 0.95)
- `image`: Container image (e.g., `ghcr.io/llm-d/llm-d-cuda:v0.5.1`)
- `test_id`: Unique test identifier for this configuration

### Architecture-Specific Variables

#### PD (Prefill/Decode)
- `prefill_replicas`: Number of prefill pods
- `decode_replicas`: Number of decode pods
- `kv_connector`: KV cache connector type (e.g., `NixlConnector`)

#### EP (Expert Parallelism)
- `enable_chunked_prefill`: Enable chunked prefill (boolean)
- `prefill_batch_size`: Prefill batch size

#### Aggregated
- `node_count`: Number of nodes for multi-node deployment

## Usage

Templates are rendered using Jinja2:

```python
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader('templates'))
template = env.get_template('aggregated/lws.yaml.j2')

rendered = template.render(
    model_name='RedHatAI/Qwen3-235B-A22B-FP8-dynamic',
    namespace='llm-d',
    tensor_parallelism=8,
    replicas=1,
    max_model_len=40960,
    gpu_memory_utilization=0.95,
    test_id='test-001'
)
```

## Source

Templates are based on the deployment configurations in:
`/Users/bbenshab/Infrabric-deployer/rig/llm-d/overlays/`

### Mapping

- `aggregated/` ← `multinode-aggregated/`
- `pd/` ← `pd-disaggregation/`
- `ep/` ← `ep-multinode/`

## Next Steps

1. Create template generator module (`template_manager.py`)
2. Integrate with configuration generator
3. Add validation for rendered templates
4. Support custom vLLM arguments per architecture
