"""
ServeIt Studio Template Manager

Renders Jinja2 templates for Kubernetes deployments based on test configurations.
Generates YAML manifests for Aggregated, PD, and EP architectures.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import asdict
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from .config_generator import TestConfig
from .networking import compute_network_values

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TemplateManager:
    """Manages Jinja2 templates for Kubernetes deployment manifests."""

    def __init__(self, templates_dir: Optional[str] = None):
        """
        Initialize TemplateManager.

        Args:
            templates_dir: Path to templates directory (default: ./templates)
        """
        if templates_dir is None:
            current_file = Path(__file__).resolve()
            templates_dir = str(current_file.parent / 'templates')

        self.templates_dir = templates_dir
        self.env = Environment(
            loader=FileSystemLoader(templates_dir),
            trim_blocks=True,
            lstrip_blocks=True
        )

        logger.info(f"TemplateManager initialized with templates_dir: {templates_dir}")

    def render_template(self, template_path: str, **kwargs) -> str:
        """
        Render any template by path with provided variables.

        Args:
            template_path: Path to template file relative to templates_dir
            **kwargs: Template variables

        Returns:
            Rendered template as string
        """
        try:
            template = self.env.get_template(template_path)
            return template.render(**kwargs)
        except TemplateNotFound:
            raise FileNotFoundError(f"Template not found: {template_path}")

    def _get_template_path(self, architecture: str) -> str:
        """
        Get the template file path for a given architecture.

        Args:
            architecture: Architecture type ('aggregated', 'pd', 'ep')

        Returns:
            Template file path relative to templates_dir
        """
        if architecture == 'aggregated':
            return 'aggregated/lws.yaml.j2'
        elif architecture in ('pd', 'ep'):
            return {
                'prefill': 'pd/prefill-lws.yaml.j2',
                'decode': 'pd/decode-lws.yaml.j2'
            }
        else:
            raise ValueError(f"Unknown architecture: {architecture}")

    def _prepare_template_vars(self, config: TestConfig) -> Dict:
        """
        Assemble fully-resolved template variables from TestConfig
        and core producers (networking, resources).

        Templates receive a flat dict with no fallback logic needed.
        """
        vars_dict = asdict(config)

        vars_dict['deployment_name'] = f"{config.architecture}-{config.test_id}"
        vars_dict['model_name_label'] = config.model_name.replace('/', '-') if config.model_name else 'unknown'
        vars_dict['nccl_ib_hca_prefix'] = config.nccl_ib_hca.split('_')[0] if config.nccl_ib_hca else 'mlx'

        # Resolve PD role-specific TP
        vars_dict['prefill_tp'] = config.prefill_tp or config.tensor_parallelism
        vars_dict['decode_tp'] = config.decode_tp or config.tensor_parallelism

        # Resolve GPU memory utilization per role
        vars_dict['prefill_gpu_memory_utilization'] = (
            config.prefill_gpu_memory_utilization or config.gpu_memory_utilization
        )
        vars_dict['decode_gpu_memory_utilization'] = (
            config.decode_gpu_memory_utilization or config.gpu_memory_utilization
        )

        # Resolve GPU count per role
        gpus_override = getattr(config, 'gpus_per_pod', None)
        vars_dict['prefill_gpu_count'] = gpus_override or vars_dict['prefill_tp']
        vars_dict['decode_gpu_count'] = gpus_override or vars_dict['decode_tp']
        vars_dict['gpu_count'] = gpus_override or config.tensor_parallelism

        # CPU limit defaults to request
        vars_dict['cpu_limit'] = config.cpu_limit or config.cpu_request

        # Data parallelism (not in TestConfig)
        vars_dict.setdefault('data_parallelism', 1)

        # Routing proxy image — derive from scheduler image
        sched_image = vars_dict.get('scheduler_image') or getattr(config, 'scheduler_image', '') or 'ghcr.io/llm-d/llm-d-inference-scheduler:v0.9.0'
        sched_tag = sched_image.split(':')[-1] if ':' in sched_image else 'v0.9.0'
        vars_dict.setdefault('routing_proxy_image', f'ghcr.io/llm-d/llm-d-router-disagg-sidecar:{sched_tag}')
        vars_dict.setdefault('sidecar_connector_flag', '--kv-connector')

        # Network values from core/networking
        rdma_nics = getattr(config, 'rdma_nics_per_node', 0)
        rdma_resources = getattr(config, 'rdma_device_resources', [])
        network_vals = compute_network_values(
            config.network_type,
            rdma_resources,
            rdma_nics,
            rdma_network_annotation=getattr(config, 'rdma_network_annotation', None),
            selected_dra_classes=getattr(config, 'selected_dra_classes', None),
        )

        # All roles use the same device resources (rdma/ib: 1 is a capacity token,
        # the discovery script handles topology-aware NIC selection)
        network_vals['prefill_extra_device_resources'] = network_vals['extra_device_resources']
        network_vals['decode_extra_device_resources'] = network_vals['extra_device_resources']

        vars_dict.update(network_vals)

        # Per-role EP flags: only enable on roles with TP > 1
        prefill_tp = vars_dict['prefill_tp']
        decode_tp = vars_dict['decode_tp']
        ep = config.enable_expert_parallel
        vars_dict['prefill_enable_expert_parallel'] = ep and prefill_tp > 1
        vars_dict['decode_enable_expert_parallel'] = ep and decode_tp > 1
        vars_dict['prefill_enable_eplb'] = config.enable_eplb and prefill_tp > 1
        vars_dict['decode_enable_eplb'] = config.enable_eplb and decode_tp > 1
        vars_dict['prefill_enable_dbo'] = config.enable_dbo and prefill_tp > 1
        vars_dict['decode_enable_dbo'] = config.enable_dbo and decode_tp > 1
        vars_dict['prefill_moe_backend'] = config.moe_backend if prefill_tp > 1 else None
        vars_dict['decode_moe_backend'] = config.moe_backend if decode_tp > 1 else None
        # Upstream llm-d uses different all2all backends per role:
        # prefill = deepep_high_throughput (optimized for batch prefill)
        # decode = deepep_low_latency (optimized for per-token decode)
        if config.all2all_backend:
            vars_dict['prefill_all2all_backend'] = config.all2all_backend if prefill_tp > 1 else None
            if config.all2all_backend == 'deepep_high_throughput':
                vars_dict['decode_all2all_backend'] = 'deepep_low_latency' if decode_tp > 1 else None
            else:
                vars_dict['decode_all2all_backend'] = config.all2all_backend if decode_tp > 1 else None
        else:
            vars_dict['prefill_all2all_backend'] = None
            vars_dict['decode_all2all_backend'] = None

        vars_dict['moe_dp_chunk_size'] = getattr(config, 'moe_dp_chunk_size', None)
        vars_dict['nvshmem_symmetric_size'] = getattr(config, 'nvshmem_symmetric_size', None)
        vars_dict['num_redundant_experts'] = getattr(config, 'num_redundant_experts', None)

        return vars_dict

    def render_aggregated(self, config: TestConfig) -> str:
        """
        Render aggregated architecture template.

        Args:
            config: Test configuration

        Returns:
            Rendered YAML manifest as string
        """
        if config.architecture != 'aggregated':
            raise ValueError(f"Expected aggregated architecture, got {config.architecture}")

        template_path = self._get_template_path('aggregated')
        template = self.env.get_template(template_path)

        vars_dict = self._prepare_template_vars(config)

        rendered = template.render(**vars_dict)
        logger.info(f"Rendered aggregated template for {config.test_id}")

        return rendered

    def render_pd(self, config: TestConfig) -> Dict[str, str]:
        """
        Render PD architecture templates (prefill and decode).

        IMPORTANT: Returns manifests in deployment order - pods with higher GPU
        requirements FIRST to avoid scheduling traps where larger pods can't find
        nodes after smaller pods spread out.

        Args:
            config: Test configuration

        Returns:
            Dictionary with 'prefill' and 'decode' keys containing rendered YAML
            Ordered by GPU requirement (highest TP first)
        """
        if config.architecture not in ('pd', 'ep'):
            raise ValueError(f"Expected pd or ep architecture, got {config.architecture}")

        template_paths = self._get_template_path(config.architecture)
        prefill_template = self.env.get_template(template_paths['prefill'])
        decode_template = self.env.get_template(template_paths['decode'])

        vars_dict = self._prepare_template_vars(config)

        # Render both templates
        prefill_yaml = prefill_template.render(**vars_dict)
        decode_yaml = decode_template.render(**vars_dict)

        # Determine deployment order based on GPU requirements
        prefill_tp = config.prefill_tp or config.tensor_parallelism
        decode_tp = config.decode_tp or config.tensor_parallelism

        # Deploy pods with HIGHER GPU requirement first to claim full nodes
        # This prevents scheduling deadlocks where small pods spread across all nodes
        # leaving no node with enough GPUs for large pods
        if decode_tp > prefill_tp:
            rendered = {
                'decode': decode_yaml,
                'prefill': prefill_yaml
            }
            logger.info(f"Rendered PD templates for {config.test_id} (decode first: {decode_tp} > {prefill_tp} GPUs)")
        else:
            rendered = {
                'prefill': prefill_yaml,
                'decode': decode_yaml
            }
            logger.info(f"Rendered PD templates for {config.test_id} (prefill first: {prefill_tp} >= {decode_tp} GPUs)")

        return rendered

    def render_ep(self, config: TestConfig) -> Dict[str, str]:
        """
        Render EP architecture templates (uses PD prefill/decode split).

        EP reuses PD templates — the EP-specific flags (expert parallel, EPLB,
        MoE backend) are set in the config and rendered via PD template conditionals.
        """
        if config.architecture != 'ep':
            raise ValueError(f"Expected ep architecture, got {config.architecture}")
        return self.render_pd(config)

    def render_config(self, config: TestConfig) -> Dict[str, str]:
        """
        Render template(s) for any architecture type.

        Args:
            config: Test configuration

        Returns:
            Dictionary of manifest_name -> rendered_yaml
        """
        if config.architecture == 'aggregated':
            # Render both LWS and Service for aggregated
            lws_yaml = self.render_aggregated(config)
            service_template = self.env.get_template('aggregated/service.yaml.j2')
            service_yaml = service_template.render(**self._prepare_template_vars(config))
            return {'lws': lws_yaml, 'service': service_yaml}
        elif config.architecture in ('pd', 'ep'):
            pd_manifests = self.render_pd(config)
            vars_dict = self._prepare_template_vars(config)
            prefill_svc = self.env.get_template('pd/prefill-service.yaml.j2').render(**vars_dict)
            decode_svc = self.env.get_template('pd/decode-service.yaml.j2').render(**vars_dict)

            prefill_tp = config.prefill_tp or config.tensor_parallelism
            decode_tp = config.decode_tp or config.tensor_parallelism

            if decode_tp > prefill_tp:
                pd_manifests['decode-service'] = decode_svc
                pd_manifests['prefill-service'] = prefill_svc
            else:
                pd_manifests['prefill-service'] = prefill_svc
                pd_manifests['decode-service'] = decode_svc

            return pd_manifests
        else:
            raise ValueError(f"Unknown architecture: {config.architecture}")

    def save_manifest(
        self,
        config: TestConfig,
        output_dir: str,
        create_dir: bool = True
    ) -> List[str]:
        """
        Render and save manifest(s) to file(s).

        Args:
            config: Test configuration
            output_dir: Directory to save manifests
            create_dir: Create output directory if it doesn't exist

        Returns:
            List of file paths that were created
        """
        output_path = Path(output_dir)

        if create_dir:
            output_path.mkdir(parents=True, exist_ok=True)

        manifests = self.render_config(config)
        saved_files = []

        for manifest_name, manifest_content in manifests.items():
            # Create filename: <test_id>-<manifest_name>.yaml
            filename = f"{config.test_id}-{manifest_name}.yaml"
            file_path = output_path / filename

            with open(file_path, 'w') as f:
                f.write(manifest_content)

            saved_files.append(str(file_path))
            logger.info(f"Saved manifest to {file_path}")

        return saved_files

    def save_all_manifests(
        self,
        configs: List[TestConfig],
        output_dir: str,
        create_dir: bool = True
    ) -> Dict[str, List[str]]:
        """
        Save manifests for multiple test configurations.

        Args:
            configs: List of test configurations
            output_dir: Directory to save manifests
            create_dir: Create output directory if it doesn't exist

        Returns:
            Dictionary mapping test_id -> list of file paths
        """
        results = {}

        for config in configs:
            saved_files = self.save_manifest(config, output_dir, create_dir)
            results[config.test_id] = saved_files

        logger.info(f"Saved manifests for {len(configs)} configurations to {output_dir}")

        return results


def main():
    """Main entry point for standalone execution."""
    import argparse
    import json
    parser = argparse.ArgumentParser(
        description='Render Kubernetes manifests from test configurations'
    )
    parser.add_argument('--config-file', required=True, help='Path to optimization plan JSON file')
    parser.add_argument('--output-dir', required=True, help='Directory to save manifests')
    parser.add_argument('--templates-dir', help='Templates directory (default: ./templates)')

    args = parser.parse_args()

    # Load optimization plan
    with open(args.config_file, 'r') as f:
        plan_dict = json.load(f)

    # Reconstruct test configs
    from .config_generator import TestConfig
    test_configs = [TestConfig(**cfg) for cfg in plan_dict['test_configs']]

    # Render and save manifests
    manager = TemplateManager(templates_dir=args.templates_dir)
    results = manager.save_all_manifests(test_configs, args.output_dir)

    # Print summary
    print(f"✓ Saved manifests for {len(test_configs)} configurations")
    print(f"✓ Output directory: {args.output_dir}")
    print("\nFiles created:")
    for test_id, files in results.items():
        print(f"  {test_id}:")
        for file_path in files:
            print(f"    - {file_path}")


if __name__ == '__main__':
    main()
