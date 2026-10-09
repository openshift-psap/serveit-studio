"""Unit tests for DSpark speculative-config plumbing.

Covers the fix for the step-7 crash where every serving pod crashlooped with
"SpeculativeConfig: mask_token_id — Unexpected keyword argument":
1. Extra spec keys that belong to the draft model's HF config are dropped
   from the --speculative-config JSON (this vLLM's pydantic SpeculativeConfig
   rejects unknown top-level keys and has no draft-HF override hook; the
   draft model's own config.json must carry them).
2. DSpark with 'Speculative model' on auto resolves the known external
   draft for the target family instead of falling back to the target model.
3. DSpark defaults the spec-level KV dtype to bfloat16 (fp8 KV crashes the
   draft) unless the user set one explicitly.
"""

import json
import types

from core.config_generator import TestConfig
from core.optimizer.config_builder import ConfigBuilderMixin, DSPARK_DRAFT_MODELS
from core.template_manager import TemplateManager


def _cfg(**kw):
    base = dict(
        test_id='t1', architecture='pd', model_name='zai-org/GLM-5.3',
        namespace='ns', isl=100, osl=100, num_users=10,
        tensor_parallelism=8, replicas=2,
    )
    base.update(kw)
    return TestConfig(**base)


class _StubBuilder(ConfigBuilderMixin):
    def __init__(self, advanced):
        self.config = types.SimpleNamespace(
            advanced_vllm=advanced, advanced_vllm_custom_enabled=True)
        self.logs = []
        self._requires_fp8_kv_cache = False

    def log(self, msg, level='info'):
        self.logs.append(msg)


# ── template: draft-HF keys dropped ─────────────────────────────────

def test_draft_hf_keys_dropped_from_spec_json():
    tm = TemplateManager()
    cfg = _cfg(
        speculative_method='dspark', speculative_num_tokens=3,
        speculative_model='RedHatAI/GLM-5.3-speculator.dspark',
        prefill_speculative_num_tokens=1,
        speculative_extra_config=[
            {'key': 'mask_token_id', 'value': 154822},
            {'key': 'rejection_sample_method', 'value': 'block'},
        ])
    v = tm._prepare_template_vars(cfg)

    spec = json.loads(v['speculative_config_json'])
    assert 'mask_token_id' not in spec, 'top-level mask_token_id crashes SpeculativeConfig'
    assert 'draft_hf_overrides' not in spec, 'not a SpeculativeConfig field on this vLLM'
    assert spec['rejection_sample_method'] == 'block'
    assert spec['method'] == 'dspark'
    assert spec['model'] == 'RedHatAI/GLM-5.3-speculator.dspark'

    prefill_spec = json.loads(v['prefill_speculative_config_json'])
    assert 'mask_token_id' not in prefill_spec
    assert prefill_spec['num_speculative_tokens'] == 1


def test_regular_extra_keys_stay_top_level():
    tm = TemplateManager()
    cfg = _cfg(
        speculative_method='mtp', speculative_num_tokens=3,
        speculative_extra_config=[
            {'key': 'parallel_drafting', 'value': 'true'},
            {'key': 'num_speculative_tokens_per_batch_size', 'value': '2'},
        ])
    spec = json.loads(tm._prepare_template_vars(cfg)['speculative_config_json'])
    assert 'draft_hf_overrides' not in spec
    assert spec['parallel_drafting'] is True
    assert spec['num_speculative_tokens_per_batch_size'] == 2


# ── config builder: drop + DSpark draft + KV defaults ───────────────

def test_dspark_draft_hf_keys_dropped_with_warning():
    stub = _StubBuilder({
        'speculative_method': 'dspark',
        'speculative_extra_config': [{'key': 'mask_token_id', 'value': 154822}],
    })
    cfg = stub._apply_advanced_vllm(_cfg())
    keys = [e['key'] for e in (cfg.speculative_extra_config or [])]
    assert 'mask_token_id' not in keys
    assert any('Dropped draft-HF keys' in m and 'mask_token_id' in m for m in stub.logs)


def test_dspark_auto_resolves_draft_model_and_kv_dtype():
    stub = _StubBuilder({'speculative_method': 'dspark'})
    cfg = stub._apply_advanced_vllm(_cfg())
    assert cfg.speculative_model == DSPARK_DRAFT_MODELS['glm-5.3']
    assert {'key': 'kv_cache_dtype', 'value': 'bfloat16'} in cfg.speculative_extra_config
    assert any('DSpark draft model' in m for m in stub.logs)


def test_dspark_explicit_model_respected():
    stub = _StubBuilder({
        'speculative_method': 'dspark',
        'speculative_model': {'mode': 'custom', 'value': 'my/draft'},
    })
    cfg = stub._apply_advanced_vllm(_cfg())
    assert cfg.speculative_model == 'my/draft'


def test_dspark_user_kv_dtype_not_overridden():
    stub = _StubBuilder({
        'speculative_method': 'dspark',
        'speculative_extra_config': [{'key': 'kv_cache_dtype', 'value': 'fp8'}],
    })
    cfg = stub._apply_advanced_vllm(_cfg())
    kv = [e for e in cfg.speculative_extra_config if e['key'] == 'kv_cache_dtype']
    assert len(kv) == 1 and kv[0]['value'] == 'fp8'


def test_dspark_unknown_family_warns():
    stub = _StubBuilder({'speculative_method': 'dspark'})
    cfg = _cfg(model_name='org/Unknown-Model')
    stub._apply_advanced_vllm(cfg)
    assert cfg.speculative_model is None
    assert any('requires an external draft model' in m for m in stub.logs)


def test_non_dspark_method_untouched():
    stub = _StubBuilder({
        'speculative_method': 'mtp',
        'num_speculative_tokens': {'mode': 'custom', 'value': 3},
    })
    cfg = stub._apply_advanced_vllm(_cfg())
    assert cfg.speculative_method == 'mtp'
    assert cfg.speculative_model is None
    assert cfg.speculative_extra_config is None


def test_dspark_spec_json_renders_valid_for_user_run41_config():
    """End-to-end: run 41's exact advanced_vllm spec settings must produce
    a spec JSON this vLLM's SpeculativeConfig accepts — no top-level
    mask_token_id, speculator draft model, bfloat16 draft KV."""
    stub = _StubBuilder({
        'speculative_method': 'dspark',
        'num_speculative_tokens': {'mode': 'custom', 'value': 3},
        'prefill_speculative_num_tokens': {'mode': 'custom', 'value': 1},
        'speculative_model': {'mode': 'auto'},
        'speculative_extra_config': [{'key': 'mask_token_id', 'value': 154822}],
    })
    cfg = stub._apply_advanced_vllm(_cfg())
    tm = TemplateManager()
    spec = json.loads(tm._prepare_template_vars(cfg)['speculative_config_json'])

    assert 'mask_token_id' not in spec
    assert 'draft_hf_overrides' not in spec
    assert spec['model'] == 'RedHatAI/GLM-5.3-speculator.dspark'
    assert spec['kv_cache_dtype'] == 'bfloat16'
    assert spec['num_speculative_tokens'] == 3

    prefill_spec = json.loads(tm._prepare_template_vars(cfg)['prefill_speculative_config_json'])
    assert prefill_spec['num_speculative_tokens'] == 1
    assert 'mask_token_id' not in prefill_spec
