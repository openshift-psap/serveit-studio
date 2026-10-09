"""Unit tests for pod memory sizing with CPU KV offload (Steps 7-8 PD configs).

Covers the fix for pods crashing at startup when cpu_offload_gb is set via
advanced vLLM settings:
1. The run-level config attributes (cpu_offload_gb etc.) are None — the values
   live in advanced_vllm mode/value dicts. The pod memory computation must
   resolve the same effective values the deployed pods use, otherwise memory
   is sized without the offload region (80Gi instead of ~490Gi).
2. Kubelet caps Memory-medium emptyDir tmpfs at the container memory limit,
   and tmpfs pages are charged to the container cgroup — so pod memory must
   cover the shm region (cpu_offload + 210, mirroring template_manager).
3. When free node memory cannot even fit loading buffers + offload, the
   config is skipped instead of deploying a doomed pod.
"""

import types

import pytest

from core.optimizer.pipeline import RecipeOptimizer


def _make_stub(advanced=None, top_level=None, pool_mem=None, pool_cpu=None,
               model_gb=712.0, n_nodes=16):
    """Minimal RecipeOptimizer stand-in exercising _get_pod_resources."""
    cfg_kwargs = {
        'memory_per_pod': None, 'cpu_per_pod': None,
        'selected_nodes': [], 'advanced_vllm': advanced or {},
        'cpu_offload_gb': None, 'weight_cpu_offload_gb': None,
        'shm_size_gb': None, 'host_ipc': False,
    }
    cfg_kwargs.update(top_level or {})
    stub = types.SimpleNamespace(
        config=types.SimpleNamespace(**cfg_kwargs),
        cluster_resources=types.SimpleNamespace(nodes=[
            types.SimpleNamespace(gpus=8, cpu_cores=52, numa_nodes=2)
            for _ in range(n_nodes)
        ]),
        logs=[],
    )
    if pool_mem is not None:
        stub._cached_free_mem_gb = pool_mem
        stub._cached_free_cpus = pool_cpu if pool_cpu is not None else 34.0
    stub.log = lambda msg, level='info': stub.logs.append(msg)
    stub._estimate_model_size_gb = lambda: model_gb
    # Bind the real methods under test.
    stub._effective_offload_settings = RecipeOptimizer._effective_offload_settings.__get__(stub)
    stub._get_pod_resources = RecipeOptimizer._get_pod_resources.__get__(stub)
    return stub


class TestEffectiveOffloadSettings:
    def test_resolves_advanced_custom_cpu_offload(self):
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'custom', 'value': '200'}})
        cpu, weight, host_ipc, shm = stub._effective_offload_settings()
        assert cpu == 200
        assert weight == 0
        assert host_ipc is False
        assert shm == 410  # cpu_offload + 210, mirrors template_manager

    def test_custom_shm_overrides_auto_formula(self):
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '200'},
            'shm_size_gb': {'mode': 'custom', 'value': '600'},
        })
        cpu, _, _, shm = stub._effective_offload_settings()
        assert cpu == 200
        assert shm == 600  # custom shm kept as-is, no +210

    def test_host_ipc_disables_auto_shm(self):
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '200'},
            'host_ipc': True,
        })
        cpu, _, host_ipc, shm = stub._effective_offload_settings()
        assert host_ipc is True
        assert shm == 0  # no tmpfs sizing needed with hostIPC

    def test_weight_offload_resolved(self):
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '100'},
            'weight_cpu_offload_gb': {'mode': 'custom', 'value': '50'},
        })
        cpu, weight, _, _ = stub._effective_offload_settings()
        assert cpu == 100
        assert weight == 50

    def test_falls_back_to_top_level_attrs(self):
        stub = _make_stub(advanced={}, top_level={'cpu_offload_gb': 64})
        cpu, _, _, shm = stub._effective_offload_settings()
        assert cpu == 64
        assert shm == 274

    def test_no_offload_anywhere(self):
        stub = _make_stub(advanced={})
        cpu, weight, _, shm = stub._effective_offload_settings()
        assert cpu == 0 and weight == 0 and shm == 0

    def test_advanced_off_mode_ignored(self):
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'off'}},
                          top_level={'cpu_offload_gb': 32})
        cpu, _, _, shm = stub._effective_offload_settings()
        assert cpu == 32  # falls back to top-level
        assert shm == 242


class TestPodMemorySizing:
    def test_memory_includes_offload_and_shm(self):
        # GLM-5.3 TP8: per-GPU weights ~89GB -> loading buffer 80Gi;
        # cpu_offload 200 -> shm 410 -> memory = 80 + 410 = 490Gi.
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'custom', 'value': '200'}},
                          pool_mem=2000.0, pool_cpu=100.0)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '490Gi'

    def test_memory_capped_to_free_pool(self):
        # Run-41-like pool: 408Gi min-free node -> 367Gi per pod after 0.90.
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'custom', 'value': '200'}},
                          pool_mem=408.2, pool_cpu=33.86)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '367Gi'
        assert any('Capping to 367Gi' in m for m in stub.logs)

    def test_hard_floor_skips_config_when_offload_cannot_fit(self):
        # 90Gi free per pod < 80 loading + 200 offload -> skip, not doom.
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'custom', 'value': '200'}},
                          pool_mem=100.0, pool_cpu=33.0)
        with pytest.raises(RuntimeError, match='Insufficient node memory for cpu_offload'):
            stub._get_pod_resources(tp=8, total_pods=4)
        assert any('Insufficient node memory for CPU KV offload' in m for m in stub.logs)

    def test_memory_without_offload_unchanged(self):
        # Regression: no offload -> loading buffer only (80Gi for GLM-5.3 TP8).
        stub = _make_stub(advanced={}, pool_mem=408.2, pool_cpu=33.86)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '80Gi'

    def test_host_ipc_memory_covers_offload_without_shm_headroom(self):
        # hostIPC: region lives in host /dev/shm, still cgroup-charged but
        # no tmpfs cap applies — memory = loading + offload (no +210).
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '200'},
            'host_ipc': True,
        }, pool_mem=2000.0, pool_cpu=100.0)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '280Gi'

    def test_custom_shm_memory_covers_custom_shm(self):
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '200'},
            'shm_size_gb': {'mode': 'custom', 'value': '600'},
        }, pool_mem=2000.0, pool_cpu=100.0)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '680Gi'  # 80 loading + 600 custom shm

    def test_weight_offload_added_to_memory(self):
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '100'},
            'weight_cpu_offload_gb': {'mode': 'custom', 'value': '50'},
        }, pool_mem=2000.0, pool_cpu=100.0)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '440Gi'  # 80 loading + 50 weight + 310 shm (100+210)
