"""Native WekaTrace replay wiring (guidellm kind=weka)."""

import json
from types import SimpleNamespace

from core.orchestrator.guidellm import build_wekatrace_data_arg


def _cfg(**kw):
    base = dict(
        max_model_len=40960,
        wekatrace_max_context_len=None,
        wekatrace_max_wait=1.0,
        wekatrace_min_concurrent_sessions=None,
        wekatrace_time_scale=None,
        wekatrace_copies=1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_data_arg_is_native_weka_with_source():
    arg = json.loads(build_wekatrace_data_arg(_cfg(), "/app/traces/cc-traces-weka.jsonl"))
    assert arg["kind"] == "weka"
    assert arg["source"] == {"kind": "json_file", "path": "/app/traces/cc-traces-weka.jsonl"}
    # column contract of the SemiAnalysis/WEKA corpus
    assert arg["timestamp_column"] == "t"
    assert arg["prompt_tokens_column"] == "in"
    assert arg["output_tokens_column"] == "out"
    assert arg["conversation_id_column"] == "id"
    assert arg["hash_ids_column"] == "hash_ids"
    assert arg["hash_id_block_size"] == 64


def test_context_cap_defaults_to_model_window_then_override():
    assert json.loads(build_wekatrace_data_arg(_cfg(), "p"))["max_context_len"] == 40960
    arg = json.loads(build_wekatrace_data_arg(_cfg(wekatrace_max_context_len=131072), "p"))
    assert arg["max_context_len"] == 131072


def test_pacing_knobs_only_when_set():
    arg = json.loads(build_wekatrace_data_arg(_cfg(), "p"))
    assert arg["max_wait"] == 1.0
    assert "min_concurrent_sessions" not in arg
    assert "time_scale" not in arg
    assert "copies" not in arg

    arg = json.loads(build_wekatrace_data_arg(
        _cfg(wekatrace_min_concurrent_sessions=8, wekatrace_time_scale=0.5, wekatrace_copies=2), "p"))
    assert arg["min_concurrent_sessions"] == 8
    assert arg["time_scale"] == 0.5
    assert arg["copies"] == 2


def test_zero_max_wait_is_omitted():
    arg = json.loads(build_wekatrace_data_arg(_cfg(wekatrace_max_wait=0), "p"))
    assert "max_wait" not in arg
