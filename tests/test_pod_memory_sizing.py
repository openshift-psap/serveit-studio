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
    stub._probe_lora_adapter_size_mb = RecipeOptimizer._probe_lora_adapter_size_mb.__get__(stub)
    stub._lora_staging_gib = RecipeOptimizer._lora_staging_gib.__get__(stub)
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
        assert cpu == 0 and weight == 0 and shm == 2  # min floor at tp=1

    def test_shm_autoscales_with_tp(self):
        # No offload, no override: shm mirrors template_manager's TP-rank
        # autoscaling (1 GiB per rank, min 2) instead of a fixed 2 GiB.
        stub = _make_stub(advanced={})
        _, _, _, shm = stub._effective_offload_settings(tp=4)
        assert shm == 4
        _, _, _, shm = stub._effective_offload_settings(tp=16)
        assert shm == 16

    def test_advanced_off_mode_ignored(self):
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'off'}},
                          top_level={'cpu_offload_gb': 32})
        cpu, _, _, shm = stub._effective_offload_settings()
        assert cpu == 32  # falls back to top-level
        assert shm == 242


class TestPodMemorySizing:
    def test_memory_includes_offload_and_shm(self):
        # GLM-5.3 TP8: per-GPU weights ~89GB -> loading buffer 80Gi;
        # cpu_offload 200 -> shm 410; workers 16 -> memory = 80+16+410 = 506Gi.
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'custom', 'value': '200'}},
                          pool_mem=2000.0, pool_cpu=100.0)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '506Gi'

    def test_memory_capped_to_free_pool(self):
        # Run-41-like pool: 408Gi min-free node -> 367Gi per pod after 0.90.
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'custom', 'value': '200'}},
                          pool_mem=408.2, pool_cpu=33.86)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '367Gi'
        assert any('Capping base to 367Gi' in m for m in stub.logs)

    def test_hard_floor_skips_config_when_offload_cannot_fit(self):
        # 90Gi free per pod < 80 loading + 200 offload -> skip, not doom.
        stub = _make_stub(advanced={'cpu_offload_gb': {'mode': 'custom', 'value': '200'}},
                          pool_mem=100.0, pool_cpu=33.0)
        with pytest.raises(RuntimeError, match='Insufficient node memory for cpu_offload'):
            stub._get_pod_resources(tp=8, total_pods=4)
        assert any('Insufficient node memory' in m for m in stub.logs)

    def test_memory_without_offload_scales_with_tp(self):
        # No offload: loading buffer (80Gi for GLM-5.3 TP8) + worker process
        # overhead (2 GiB/rank = 16Gi) + autoscaled shm (8Gi) = 104Gi.
        stub = _make_stub(advanced={}, pool_mem=408.2, pool_cpu=33.86)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '104Gi'

    def test_small_model_tp4_memory_covers_workers(self):
        # Regression for the psap-fire-athena crash loop: Gemma-4 26B TP4 has
        # small per-GPU weights (16Gi loading floor) — the old formula gave
        # 16Gi total and pods were OOMKilled during CUDA graph capture.
        # Now: 16 loading + 8 workers + 4 shm = 28Gi.
        stub = _make_stub(advanced={}, pool_mem=2000.0, pool_cpu=100.0,
                          model_gb=26.0)
        mem, _ = stub._get_pod_resources(tp=4, total_pods=1)
        assert mem == '28Gi'

    def test_host_ipc_memory_covers_offload_without_shm_headroom(self):
        # hostIPC: region lives in host /dev/shm, still cgroup-charged but
        # no tmpfs cap applies — memory = loading + workers + offload (no +210).
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '200'},
            'host_ipc': True,
        }, pool_mem=2000.0, pool_cpu=100.0)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '296Gi'

    def test_custom_shm_memory_covers_custom_shm(self):
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '200'},
            'shm_size_gb': {'mode': 'custom', 'value': '600'},
        }, pool_mem=2000.0, pool_cpu=100.0)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '696Gi'  # 80 loading + 16 workers + 600 custom shm

    def test_weight_offload_added_to_memory(self):
        stub = _make_stub(advanced={
            'cpu_offload_gb': {'mode': 'custom', 'value': '100'},
            'weight_cpu_offload_gb': {'mode': 'custom', 'value': '50'},
        }, pool_mem=2000.0, pool_cpu=100.0)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '456Gi'  # 80 loading + 16 workers + 50 weight + 310 shm (100+210)


class TestLoraStagingValidation:
    """Run-51 regression: LoRA CPU staging is added by template_manager
    AFTER the free-RAM validation — pods deployed with 343Gi on a node with
    1.7Ti free got stuck Pending. The validation must see the total."""

    def _lora_stub(self, pool_mem=None, pool_cpu=None, **kw):
        top = dict(lora_enabled=True, lora_num_adapters=1000,
                   lora_adapter_size_mb=None, lora_max_rank=16)
        top.update(kw)
        return _make_stub(top_level=top, pool_mem=pool_mem, pool_cpu=pool_cpu)

    def test_staging_uses_measured_adapter_size(self):
        # 1000 adapters x 22MB (probed Gemma-4 reality) = 22 + 4 = 26Gi,
        # not the rank x 20MB estimate (320MB -> 317Gi).
        stub = self._lora_stub(lora_adapter_size_mb=22)
        assert stub._lora_staging_gib() == 26

    def test_staging_zero_when_lora_disabled(self):
        stub = _make_stub(advanced={})
        assert stub._lora_staging_gib() == 0

    def test_staging_falls_back_to_rank_estimate(self):
        # No override, no probe (orchestrator absent -> probe fails) ->
        # rank 16 x 20MB = 320MB per adapter -> (320000+1023)//1024+4 = 317Gi
        stub = self._lora_stub()
        assert stub._lora_staging_gib() == 317

    def test_staging_included_in_free_pool_validation(self):
        # Base 80Gi + staging 26Gi = 106Gi total; free 408Gi -> 367Gi/pod:
        # fits, no cap. Without staging in the math this passed trivially.
        stub = self._lora_stub(lora_adapter_size_mb=22, pool_mem=408.2, pool_cpu=33.86)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '104Gi'  # base only; template_manager adds the 26Gi

    def test_staging_over_free_pool_caps_base(self):
        # Hard floor = 80+16+317 = 413Gi fits (free 418Gi/pod from 465Gi
        # pool), but total 421 > 418: base capped to 418 - 317 = 101Gi so
        # the DEPLOYED total (base + template_manager staging) fits.
        stub = self._lora_stub(pool_mem=465.0, pool_cpu=33.86)
        mem, _ = stub._get_pod_resources(tp=8, total_pods=4)
        assert mem == '101Gi'
        assert any('Capping base to 101Gi' in m for m in stub.logs)

    def test_staging_hard_floor_skips_config(self):
        # Free per pod (90Gi) < loading 80 + workers 16 + staging 317:
        # skip instead of deploying doomed pods.
        stub = self._lora_stub(pool_mem=100.0, pool_cpu=33.0)
        with pytest.raises(RuntimeError, match='Insufficient node memory'):
            stub._get_pod_resources(tp=8, total_pods=4)
        assert any('LoRA staging 317Gi' in m for m in stub.logs)
