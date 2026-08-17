"""Verify all modules import without errors."""

import importlib
import pytest

MODULES = [
    'core.utils',
    'core.resource_calculator',
    'core.cloud_constraints',
    'core.config_generator',
    'core.metrics_analyzer',
    'core.metrics_collector',
    'core.progress_tracker',
    'core.networking',
    'core.networking.base',
    'core.networking.dra',
    'core.networking.nad',
    'core.networking.shared_device',
    'core.networking.sriov',
    'core.orchestrator',
    'core.orchestrator.parser',
    'core.orchestrator.result',
    'core.providers',
    'core.providers.base',
    'core.providers.factory',
    'core.providers.aws',
    'core.providers.azure',
    'core.providers.baremetal',
    'core.providers.coreweave',
    'core.providers.gcp',
    'core.providers.ibm_cloud',
]

MODULES_NEED_INFRA = [
    'core',
    'core.database_manager',
    'core.optimization_strategies',
    'core.pod_error_scanner',
    'core.report_analysis',
    'core.report_data',
    'core.report_generator',
    'core.report_renderer',
    'core.recipe_optimizer',
    'core.user_defined_tuning',
    'core.optimizer',
    'core.optimizer.config',
    'core.optimizer.config_builder',
    'core.optimizer.dataset',
    'core.optimizer.epp_tuning',
    'core.optimizer.cache_sweep',
    'core.optimizer.latency_search',
    'core.optimizer.pd_search',
    'core.optimizer.speculative',
    'core.optimizer.tp_calibration',
    'core.optimizer.pipeline',
    'core.cleanup_manager',
    'core.deployment_manager',
    'core.k8s_utils',
    'core.prereq_manager',
    'core.system_scanner',
    'core.template_manager',
    'core.test_planner',
    'core.test_orchestrator',
    'core.version_scanner',
    'core.web_deployer',
    'core.mlflow_exporter',
    'core.orchestrator.guidellm',
    'core.orchestrator.runner',
    'web',
    'web.app_context',
    'web.auth',
    'web.database',
    'web.optimization',
    'web.realtime',
    'web.routes_api',
    'web.server',
    'launcher',
    'launcher.app',
    'launcher.auth',
    'launcher.cluster_scanner',
    'launcher.database',
    'launcher.instance_manager',
]


@pytest.mark.parametrize('module', MODULES)
def test_import_module(module):
    importlib.import_module(module)


@pytest.mark.parametrize('module', MODULES_NEED_INFRA)
def test_import_infra_module(module):
    try:
        importlib.import_module(module)
    except (OSError, RuntimeError, ImportError, AssertionError) as e:
        msg = str(e).lower()
        if any(k in msg for k in ('kubeconfig', 'connection', 'read-only file system', '/mnt',
                                   'setup method', 'can no longer be called', 'first request')):
            pytest.skip(f'Skipped: requires runtime environment — {e}')
        raise
