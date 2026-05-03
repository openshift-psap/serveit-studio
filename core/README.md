# InfeRecipe Core Modules

Core optimization engine for finding optimal LLM inference configurations.

## Modules

### `metrics_collector.py` ✅
Collects metrics from Prometheus/Thanos for performance analysis.

**Features**:
- Class-based design with `MetricsCollector` and `MetricsConfig`
- Categorized metric collection (GPU, Pod, vLLM, InfiniBand, Inference Gateway)
- Standalone CLI execution support
- Session reuse for HTTP performance

**Usage**:
```python
from core.metrics_collector import MetricsCollector, MetricsConfig

config = MetricsConfig.from_env()
collector = MetricsCollector(config)
collector.collect_all_metrics(start_time, end_time, 'output.json')
```

### `system_scanner.py` ⏳ (Next)
Scans Kubernetes cluster for available resources.

**Features**:
- Detect number of GPUs per node
- Identify RDMA/InfiniBand NICs
- Calculate max tensor parallelism values
- Determine node count and distribution

**Usage**:
```python
from core.system_scanner import SystemScanner

scanner = SystemScanner(namespace='llm-d')
resources = scanner.scan_cluster()
print(f"Available GPUs: {resources.total_gpus}")
print(f"RDMA support: {resources.has_rdma}")
```

### `config_generator.py` ⏳
Generates test configurations based on user inputs.

**Features**:
- Parse user inputs (ISL, OSL, users, priority)
- Determine architectures to test (Aggregated+PD or Aggregated+EP)
- Generate TP combinations based on available GPUs
- For PD: Generate prefill/decode ratios
- Create test matrix with unique test IDs

**Usage**:
```python
from core.config_generator import ConfigGenerator

generator = ConfigGenerator()
configs = generator.generate_test_matrix(
    isl=3000,
    osl=100,
    users=100,
    priority='response_time',
    available_gpus=8
)
```

### `template_manager.py` ⏳
Renders Jinja2 templates with test configurations.

**Features**:
- Load templates from `templates/` directory
- Render with configuration parameters
- Validate rendered YAML
- Support for all architectures (Aggregated, PD, EP)

**Usage**:
```python
from core.template_manager import TemplateManager

manager = TemplateManager(templates_dir='templates')
manifests = manager.render_configuration('pd', config)
```

### `deployment_manager.py` ⏳
Manages Kubernetes deployments for test configurations.

**Features**:
- Apply rendered manifests to cluster
- Wait for pods to be ready
- Verify service endpoints
- Clean up after test completion
- Handle failures and rollbacks

**Usage**:
```python
from core.deployment_manager import DeploymentManager

manager = DeploymentManager(namespace='llm-d')
manager.deploy_configuration(manifests)
manager.wait_for_ready(test_id, timeout=300)
endpoint = manager.get_service_endpoint(test_id)
```

### `test_orchestrator.py` ✅
Orchestrates guidellm tests for each configuration.

**Features**:
- Auto-discover Istio gateway or fallback to direct service
- Execute guidellm with ISL/OSL/user parameters
- Monitor pods for crashes during benchmark
- Dynamic HuggingFace cache directory detection
- Stream logs to console via callback
- Collect metrics from Prometheus/Thanos
- Handle test timeouts and failures

**Environment Variables**:
```bash
HOME_STORAGE_DIR=/mnt/storage  # Storage mount point (set by deploy.sh)
HF_HOME=/path/to/cache         # Optional: Override HuggingFace cache location
                               # If not set, uses: ${HOME_STORAGE_DIR}/.cache/huggingface
                               # Falls back to /tmp/huggingface_cache if mount unavailable
```

**Usage**:
```python
from core.test_orchestrator import TestOrchestrator

orchestrator = TestOrchestrator(
    namespace='llm-d',
    thanos_url='https://thanos-querier...'
)

# Run test with auto-discovery
success, result_file = orchestrator._run_guidellm_test(
    endpoint=None,  # Auto-discover Istio gateway
    config=test_config,
    log_callback=print,
    monitor_pods=True,
    expected_pod_count=16
)
```

### `results_analyzer.py` ⏳
Analyzes results and finds optimal configuration.

**Features**:
- Calculate normalized scores per configuration
- Apply weighted scoring based on optimization goal
- Identify top 3 configurations
- Generate comparison charts
- Export optimal configuration as YAML

**Usage**:
```python
from core.results_analyzer import ResultsAnalyzer

analyzer = ResultsAnalyzer()
scored_configs = analyzer.analyze_results(run_id=1, goal='response_time')
recommendations = analyzer.generate_recommendations(scored_configs)
```

## Design Principles

- **Modularity**: Each module has a single, clear responsibility
- **Type Safety**: Type hints throughout for better code quality
- **Error Handling**: Proper exception handling and logging
- **Testability**: Each module can be tested independently
- **Documentation**: Clear docstrings and inline comments
