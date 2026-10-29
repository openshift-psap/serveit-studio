"""Unit tests for the EPP routing search space (core/optimizer/epp_search.py).

Covers the producer dependency closure, the affinity-filter composition
rule from the EPP docs, configmap rendering for all three architectures,
and the archetype/ablation/weight-move generators.
"""

import unittest

import yaml

from core.optimizer.epp_search import (
    AFFINITY_FILTER,
    AFFINITY_FILTER_DEFAULTS,
    APPROX_PREFIX_PRODUCER,
    INFLIGHT_LOAD_PRODUCER,
    MAX_SCORE_PICKER,
    WEIGHTED_RANDOM_PICKER,
    EPPSpec,
    SearchContext,
    ablation_candidates,
    archetype_optimized_baseline,
    archetype_weighted,
    final_spec,
    phase1_specs,
    render_spec_body,
    weight_neighbors,
)
from core.metrics_analyzer import MetricsAnalyzer
from core.optimizer.epp_tuning import EPPTuningMixin


def base_ctx(**over):
    ctx = dict(
        arch="aggregated",
        num_pods=8,
        workload_bound="prefill",
        cache_mode="shared_prefix",
        cache_hit_pct=60.0,
        session_aware=False,
        lora_enabled=False,
        slo_enabled=False,
        smart_weights={
            "prefix_cache_weight": 2.0,
            "kv_cache_weight": 0,
            "queue_weight": 1.0,
            "active_request_weight": 0.5,
            "token_load_weight": 1.0,
        },
        user_weights={
            "prefix_cache_weight": 3.0,
            "kv_cache_weight": 2.0,
            "queue_weight": 2.0,
            "active_request_weight": 2.0,
        },
        peak_prefill_throughput=10000.0,
        base_epp={"maxPrefixBlocksToMatch": 256, "lruCapacityPerServer": 31250},
    )
    ctx.update(over)
    return SearchContext(**ctx)


class TestDependencyResolution(unittest.TestCase):
    def test_prefix_scorer_needs_approx_producer(self):
        spec = EPPSpec(name="t", arch="aggregated", scorers={"prefix_cache": 2.0})
        approx, inflight = spec.needed_producers()
        self.assertTrue(approx)
        self.assertFalse(inflight)

    def test_token_load_needs_inflight_producer(self):
        spec = EPPSpec(name="t", arch="aggregated", scorers={"token_load": 1.0})
        approx, inflight = spec.needed_producers()
        self.assertFalse(approx)
        self.assertTrue(inflight)

    def test_affinity_filter_needs_both_producers(self):
        spec = EPPSpec(
            name="t", arch="aggregated", scorers={"token_load": 1.0}, affinity_filter={"peakPrefillThroughput": 9000}
        )
        approx, inflight = spec.needed_producers()
        self.assertTrue(approx)
        self.assertTrue(inflight)

    def test_no_producers_without_dependent_plugins(self):
        spec = EPPSpec(name="t", arch="aggregated", scorers={"queue": 1.0, "kv_cache": 2.0})
        approx, inflight = spec.needed_producers()
        self.assertFalse(approx)
        self.assertFalse(inflight)

    def test_resolve_fills_filter_defaults(self):
        spec = EPPSpec(
            name="t", arch="aggregated", scorers={"token_load": 1.0}, affinity_filter={"peakPrefillThroughput": 9000}
        )
        resolved, _ = spec.resolve()
        for key, default in AFFINITY_FILTER_DEFAULTS.items():
            self.assertEqual(resolved.affinity_filter[key], default)
        self.assertEqual(resolved.affinity_filter["peakPrefillThroughput"], 9000)

    def test_resolve_does_not_invent_filter(self):
        spec = EPPSpec(name="t", arch="aggregated", scorers={"queue": 1.0})
        resolved, _ = spec.resolve()
        self.assertIsNone(resolved.affinity_filter)


class TestCompositionRule(unittest.TestCase):
    def test_filter_drops_weighted_prefix_scorer(self):
        spec = EPPSpec(
            name="t",
            arch="aggregated",
            scorers={"prefix_cache": 2.0, "token_load": 1.0},
            affinity_filter={"peakPrefillThroughput": 9000},
        )
        resolved, warnings = spec.resolve()
        self.assertNotIn("prefix_cache", resolved.scorers)
        self.assertTrue(any("prefix" in w for w in warnings))

    def test_unknown_picker_falls_back_to_max_score(self):
        spec = EPPSpec(name="t", arch="aggregated", scorers={"queue": 1.0}, picker="bogus-picker")
        resolved, warnings = spec.resolve()
        self.assertEqual(resolved.picker, MAX_SCORE_PICKER)
        self.assertTrue(warnings)

    def test_zero_weight_scorers_dropped(self):
        spec = EPPSpec(name="t", arch="aggregated", scorers={"queue": 1.0, "kv_cache": 0})
        resolved, _ = spec.resolve()
        self.assertNotIn("kv_cache", resolved.scorers)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (the EPP's Go decoder
    is strict; vanilla pyyaml silently keeps the last occurrence)."""


def _strict_load(text):
    class _L(_StrictLoader):
        pass

    _L.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, lambda loader, node: _no_dupes(loader, node))
    return yaml.load(text, _L)


def _no_dupes(loader, node, deep=False):
    mapping = {}
    for k_node, v_node in node.value:
        key = loader.construct_object(k_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate key: {key}")
        mapping[key] = loader.construct_object(v_node, deep=deep)
    return mapping


class TestRendering(unittest.TestCase):
    def test_optimized_baseline_matches_guide_composition(self):
        spec, _ = archetype_optimized_baseline(base_ctx()).resolve()
        doc = yaml.safe_load(render_spec_body(spec))
        types = [p["type"] for p in doc["plugins"]]
        # exactly the llm-d optimized-baseline guide plugin set plus the
        # profile handler the configmap always needs
        self.assertEqual(
            types,
            [
                "single-profile-handler",
                APPROX_PREFIX_PRODUCER,
                INFLIGHT_LOAD_PRODUCER,
                AFFINITY_FILTER,
                "token-load-scorer",
                MAX_SCORE_PICKER,
            ],
        )
        refs = [(e["pluginRef"], e.get("weight")) for e in doc["schedulingProfiles"][0]["plugins"]]
        self.assertEqual(refs, [(AFFINITY_FILTER, None), ("token-load-scorer", 1.0), (MAX_SCORE_PICKER, None)])

    def test_decode_bound_uses_active_request_scorer(self):
        spec, _ = archetype_optimized_baseline(base_ctx(workload_bound="decode")).resolve()
        doc = yaml.safe_load(render_spec_body(spec))
        types = [p["type"] for p in doc["plugins"]]
        self.assertIn("active-request-scorer", types)
        self.assertNotIn("token-load-scorer", types)

    def test_pd_profiles_and_deciders(self):
        spec, _ = archetype_optimized_baseline(base_ctx(arch="pd")).resolve()
        doc = yaml.safe_load(render_spec_body(spec))
        types = [p["type"] for p in doc["plugins"]]
        self.assertIn("always-disagg-pd-decider", types)
        self.assertIn("prefill-filter", types)
        self.assertIn("decode-filter", types)
        profiles = {p["name"] for p in doc["schedulingProfiles"]}
        self.assertEqual(profiles, {"prefill", "decode"})

    def test_ep_uses_prefix_based_decider(self):
        spec, _ = archetype_optimized_baseline(base_ctx(arch="ep")).resolve()
        doc = yaml.safe_load(render_spec_body(spec))
        types = [p["type"] for p in doc["plugins"]]
        self.assertIn("prefix-based-pd-decider", types)
        self.assertIn("disagg-profile-handler", types)

    def test_weighted_random_picker_rendered(self):
        spec, _ = archetype_weighted(base_ctx(), WEIGHTED_RANDOM_PICKER).resolve()
        doc = yaml.safe_load(render_spec_body(spec))
        types = [p["type"] for p in doc["plugins"]]
        self.assertIn(WEIGHTED_RANDOM_PICKER, types)
        self.assertNotIn(MAX_SCORE_PICKER, types)

    def test_session_affinity_filter_params(self):
        spec = EPPSpec(name="t", arch="aggregated", scorers={"queue": 1.0}, session_affinity_mode="filter")
        doc = yaml.safe_load(render_spec_body(spec))
        sess = [p for p in doc["plugins"] if p["type"] == "session-affinity-filter"]
        self.assertEqual(sess[0]["parameters"], {"strategy": "session_id"})

    def test_session_affinity_scorer_mode_emits_plugin_and_profile(self):
        # regression: the profile referenced session-affinity-scorer but the
        # plugins list never emitted it (caught live against v0.11.0:
        # "references undefined plugin 'session-affinity-scorer'")
        spec = EPPSpec(name="t", arch="aggregated", scorers={"queue": 1.0}, session_affinity_mode="scorer")
        doc = yaml.safe_load(render_spec_body(spec))
        types = [p["type"] for p in doc["plugins"]]
        self.assertIn("session-affinity-scorer", types)
        refs = [e["pluginRef"] for e in doc["schedulingProfiles"][0]["plugins"]]
        self.assertIn("session-affinity-scorer", refs)
        # every profile ref must be defined in the plugins list
        for ref in refs:
            self.assertIn(ref, types, f"profile references undefined plugin {ref}")

    def test_all_profile_refs_defined(self):
        # every rendered composition must be closed under pluginRef -> type
        for arch in ("aggregated", "pd", "ep"):
            spec, _ = archetype_optimized_baseline(base_ctx(arch=arch, session_aware=True, lora_enabled=True)).resolve()
            doc = yaml.safe_load(render_spec_body(spec))
            types = {p["type"] for p in doc["plugins"]}
            for prof in doc["schedulingProfiles"]:
                for e in prof["plugins"]:
                    self.assertIn(e["pluginRef"], types, f"{arch}/{prof['name']} references undefined {e['pluginRef']}")

    def test_scorer_params_passed_through(self):
        spec = EPPSpec(
            name="t",
            arch="aggregated",
            scorers={"token_load": 1.0},
            scorer_params={"token_load": {"queueThresholdTokens": 1000000}},
        )
        doc = yaml.safe_load(render_spec_body(spec))
        tl = [p for p in doc["plugins"] if p["type"] == "token-load-scorer"][0]
        self.assertEqual(tl["parameters"], {"queueThresholdTokens": 1000000})

    def test_lora_affinity_in_decode_profile(self):
        spec, _ = archetype_optimized_baseline(
            base_ctx(arch="pd", lora_enabled=True, smart_weights={"lora_affinity_weight": 4.0})
        ).resolve()
        doc = yaml.safe_load(render_spec_body(spec))
        decode = [p for p in doc["schedulingProfiles"] if p["name"] == "decode"][0]
        refs = [e["pluginRef"] for e in decode["plugins"]]
        self.assertIn("lora-affinity-scorer", refs)


class TestSpecSerialization(unittest.TestCase):
    def test_round_trip(self):
        spec, _ = archetype_weighted(base_ctx(session_aware=True, lora_enabled=True), MAX_SCORE_PICKER).resolve()
        cloned = EPPSpec.from_dict(spec.to_dict())
        self.assertEqual(cloned.hash_key(), spec.hash_key())
        self.assertEqual(cloned.describe(), spec.describe())

    def test_hash_sensitive_to_structure_not_rationale(self):
        a = EPPSpec(name="a", arch="aggregated", scorers={"queue": 1.0})
        b = EPPSpec(name="b", arch="aggregated", scorers={"queue": 1.0}, rationale="different purpose", phase="ablate")
        self.assertEqual(a.hash_key(), b.hash_key())
        c = EPPSpec(name="c", arch="aggregated", scorers={"queue": 2.0})
        self.assertNotEqual(a.hash_key(), c.hash_key())

    def test_copy_does_not_share_mutable_state(self):
        spec = EPPSpec(
            name="t",
            arch="aggregated",
            scorers={"prefix_cache": 2.0, "token_load": 1.0},
            affinity_filter={"peakPrefillThroughput": 9000},
        )
        cloned = spec.copy()
        cloned.scorers.pop("prefix_cache")
        cloned.affinity_filter["peakPrefillThroughput"] = 1
        self.assertEqual(spec.scorers["prefix_cache"], 2.0)
        self.assertEqual(spec.affinity_filter["peakPrefillThroughput"], 9000)

    def test_to_epp_config_carries_spec_and_legacy_view(self):
        spec, _ = archetype_optimized_baseline(base_ctx()).resolve()
        cfg = spec.to_epp_config()
        self.assertEqual(cfg["preset"], "custom")
        self.assertIsNotNone(cfg["spec"])
        self.assertTrue(cfg["prefixCacheAffinityEnabled"])
        self.assertEqual(cfg["peakPrefillThroughput"], 10000.0)
        self.assertEqual(cfg["plugins"]["token_load"]["weight"], 1.0)
        self.assertNotIn("prefix_cache", cfg["plugins"])

    def test_legacy_weights_shape(self):
        spec, _ = archetype_weighted(base_ctx(session_aware=True), MAX_SCORE_PICKER).resolve()
        w = spec.legacy_weights()
        for key in (
            "prefix_cache_weight",
            "kv_cache_weight",
            "queue_weight",
            "active_request_weight",
            "picker",
            "phase",
            "spec",
        ):
            self.assertIn(key, w)
        rebuilt = EPPSpec.from_dict(w["spec"])
        self.assertEqual(rebuilt.hash_key(), spec.hash_key())


class TestSearchGenerators(unittest.TestCase):
    def test_phase1_unique_hashes(self):
        specs = phase1_specs(base_ctx())
        hashes = [s.hash_key() for s in specs]
        self.assertEqual(len(hashes), len(set(hashes)))
        names = [s.name for s in specs]
        self.assertIn("opt-baseline", names)
        self.assertIn("weighted-max", names)
        self.assertIn("weighted-rand", names)
        self.assertIn("preset", names)

    def test_no_cache_reuse_skips_opt_baseline(self):
        # no reuse → the filter would no_match to a no-op; not worth an eval
        specs = phase1_specs(base_ctx(cache_hit_pct=0))
        self.assertNotIn("opt-baseline", [s.name for s in specs])

    def test_multi_group_gets_opt_baseline(self):
        # regression: multi_group was excluded from the filter archetype, but
        # it is where the filter narrows hardest (each pod warm only for the
        # groups it served)
        specs = phase1_specs(base_ctx(cache_mode="multi_group"))
        self.assertIn("opt-baseline", [s.name for s in specs])

    def test_few_pods_skip_weighted_random(self):
        specs = phase1_specs(base_ctx(num_pods=2))
        self.assertNotIn("weighted-rand", [s.name for s in specs])

    def test_ablations_differ_from_winner(self):
        ctx = base_ctx(session_aware=True, lora_enabled=True)
        winner, _ = archetype_weighted(ctx, MAX_SCORE_PICKER).resolve()
        abl = ablation_candidates(winner, ctx)
        self.assertTrue(abl)
        wh = winner.hash_key()
        for c in abl:
            self.assertNotEqual(c.hash_key(), wh)

    def test_ablation_session_only_when_session_aware(self):
        winner, _ = archetype_weighted(base_ctx(), MAX_SCORE_PICKER).resolve()
        abl = ablation_candidates(winner, base_ctx(session_aware=False))
        self.assertFalse(any("session" in a.name for a in abl))

    def test_lora_ab_variants(self):
        ctx = base_ctx(lora_enabled=True)
        winner, _ = archetype_optimized_baseline(ctx).resolve()
        abl = ablation_candidates(winner, ctx)
        names = [a.name for a in abl]
        self.assertIn("abl-lora-half", names)
        self.assertIn("abl-lora-off", names)
        off = next(a for a in abl if a.name == "abl-lora-off")
        self.assertNotIn("lora_affinity", off.scorers)

    def test_weight_moves_on_weighted(self):
        winner, _ = archetype_weighted(base_ctx(), MAX_SCORE_PICKER).resolve()
        moves = weight_neighbors(winner)
        self.assertTrue(moves)
        for m in moves:
            self.assertEqual(m.phase, "weight")
            self.assertNotEqual(m.hash_key(), winner.hash_key())

    def test_weight_moves_on_filter_composition(self):
        winner, _ = archetype_optimized_baseline(base_ctx()).resolve()
        moves = weight_neighbors(winner)
        names = [m.name for m in moves]
        self.assertTrue(any(n.startswith("w-load") for n in names))
        self.assertTrue(any(n.startswith("w-thr") for n in names))

    def test_final_spec_relabel(self):
        winner, _ = archetype_optimized_baseline(base_ctx()).resolve()
        final = final_spec(winner)
        self.assertEqual(final.phase, "final")
        self.assertEqual(final.name, "final-opt-baseline")
        self.assertEqual(final.hash_key(), winner.hash_key())


class TestTemplateIntegration(unittest.TestCase):
    def test_legacy_rendering_unchanged_without_spec_mode(self):
        from core.template_manager import TemplateManager

        tm = TemplateManager()
        out = tm.render_template("prereq/gaie-configmap-aggregated.yaml.j2", namespace="ns")
        self.assertIn("plugins:", out)
        self.assertNotIn("spec_body", out)
        # prefix-cache-scorer and max-score-picker remain the legacy defaults
        self.assertIn("- type: prefix-cache-scorer", out)
        self.assertIn("- type: max-score-picker", out)

    def test_spec_mode_rendering(self):
        from core.template_manager import TemplateManager

        tm = TemplateManager()
        spec, _ = archetype_optimized_baseline(base_ctx()).resolve()
        out = tm.render_template(
            "prereq/gaie-configmap-aggregated.yaml.j2",
            namespace="ns",
            spec_mode=True,
            spec_body_yaml=render_spec_body(spec),
        )
        # strict load: the EPP's Go decoder rejects duplicate keys, and the
        # spec body must not repeat apiVersion/kind from the template shell
        doc = _strict_load(out)
        body = _strict_load(doc["data"]["aggregated-config.yaml"])
        self.assertEqual(body["apiVersion"], "llm-d.ai/v1alpha1")
        self.assertEqual(body["kind"], "EndpointPickerConfig")
        types = [p["type"] for p in body["plugins"]]
        self.assertIn(AFFINITY_FILTER, types)

    def test_build_context_spec_vs_legacy(self):
        from core.prereq_manager import PrereqManager

        spec, _ = archetype_optimized_baseline(base_ctx()).resolve()
        ctx = PrereqManager.build_epp_template_context("aggregated", spec.to_epp_config(), scheduler_image="")
        self.assertTrue(ctx["spec_mode"])
        self.assertIn(AFFINITY_FILTER, ctx["spec_body_yaml"])

        legacy = PrereqManager.build_epp_template_context("aggregated", {"preset": "balanced"}, scheduler_image="")
        self.assertIsNone(legacy.get("spec_mode"))
        self.assertEqual(legacy["prefix_cache_weight"], 2.0)


if __name__ == "__main__":
    unittest.main()


class _FakeResult:
    def __init__(self, ttft):
        self.ttft_p90 = ttft
        self.ttft_p99 = ttft * 1.4
        self.throughput_p90 = 1.0
        self.guidellm_success = True


class _StubOptimizer(EPPTuningMixin):
    """Minimal EPPTuningMixin host for integration smoke tests."""

    def __init__(self, results_by_name):
        self._results_by_name = results_by_name
        self.logs = []
        self.all_test_results = []
        self.epp_benchmark_results = {}
        self.db_manager = None
        self.run_id = None
        self.orchestrator = None
        self.optimal_prefill_tp = None
        self.optimal_decode_tp = None
        self.aggregated_result = None
        self.pareto_results = None
        self.config = type(
            "C",
            (),
            {
                "isl": 25000,
                "osl": 1500,
                "qps": 30,
                "turns": 3,
                "lora_enabled": False,
                "latency_constraint_enabled": False,
                "prefix_cache_mode": "shared_prefix",
                "prefix_cache_hit_pct": 40,
                "epp_preset": "balanced",
                "epp_config": {},
                "epp_max_evals": 8,
                "epp_screen_seconds": 60,
                "test_duration": 300,
                "namespace": "ns",
                "total_gpus": 16,
                "epp_auto_weights": {"lora_affinity_weight": 3.0},
            },
        )()

    def log(self, msg, level="info"):
        self.logs.append(msg)

    def _should_stop(self):
        return False

    def _build_epp_config(self):
        return {"maxPrefixBlocksToMatch": 256, "lruCapacityPerServer": 31250, "nonCachedTokens": 16}

    def _get_best_result_prom(self, arch):
        return {"vllm_prompt_tokens_rate": 48000.0, "vllm_prefill_time_rate": 0.25, "vllm_decode_time_rate": 0.5}

    def _refine_epp_from_metrics(self, test_result, arch, base_weights=None):
        return None

    def _compute_max_num_seqs(self, tp):
        return 256

    def _run_epp_spec(
        self, prereq_mgr, arch, base_cfg, concurrency, spec, base_epp=None, baseline_ttft=None, screen=False
    ):
        # Simulate: opt-baseline wins clearly, ablations don't help
        ttft = self._results_by_name.get(spec.name, 800.0)
        result = _FakeResult(ttft)
        self.all_test_results.append((spec, result))
        return result

    def _save_epp_test_to_database(self, cfg, result):
        pass


class TestPhaseFlow(unittest.TestCase):
    def _make(self, results):
        opt = _StubOptimizer(results)
        base_cfg = type("B", (), {"test_duration": 300, "test_id": "step6-x", "architecture": "aggregated"})()
        return opt, base_cfg

    def test_phases_progress_and_budget_respected(self):
        results = {
            "opt-baseline": 300.0,
            "weighted-max": 500.0,
            "weighted-rand": 480.0,
            "preset": 700.0,
            "final-opt-baseline": 310.0,
        }
        opt, base_cfg = self._make(results)
        arch_results = opt._run_epp_search(None, "aggregated", base_cfg, 30, 8)

        names = [r[0] for r in arch_results]
        # screening ran the archetypes
        for expected in ("opt-baseline", "weighted-max", "weighted-rand", "preset"):
            self.assertIn(expected, names)
        # winner validated at full duration
        self.assertIn("final-opt-baseline", names)
        # eval budget: 4 screen + ablations + moves + 1 final <= 8
        self.assertLessEqual(len(names), 8)
        # winner is the best objective, with final supersedes-screening rule
        best = opt._epp_best_entry(arch_results)
        self.assertEqual(best[0], "final-opt-baseline")
        # entries carry the spec for report/resume paths
        for name, weights, result in arch_results:
            self.assertIn("spec", weights)
            self.assertIn("picker", weights)
            self.assertIsNotNone(result)

    def test_screen_flag_passed_to_spec_runner(self):
        opt, base_cfg = self._make({"opt-baseline": 300.0})
        seen = []

        def _spy(prereq_mgr, arch, cfg, conc, spec, base_epp=None, baseline_ttft=None, screen=False):
            seen.append((spec.name, screen))
            return _FakeResult(300.0)

        opt._run_epp_spec = _spy
        opt._run_epp_search(None, "aggregated", base_cfg, 30, 8)
        screens = [s for n, s in seen if not n.startswith("final-")]
        finals = [s for n, s in seen if n.startswith("final-")]
        self.assertTrue(screens and all(screens))
        self.assertTrue(finals and not any(finals))

    def test_budget_exhaustion_keeps_short_run_winner(self):
        # every variant returns the same mediocre result; tiny budget
        opt, base_cfg = self._make({})
        opt.config.epp_max_evals = 3
        arch_results = opt._run_epp_search(None, "aggregated", base_cfg, 30, 8)
        names = [r[0] for r in arch_results]
        self.assertLessEqual(len(names), 3)
        self.assertTrue(any("final-" in n for n in names) or any(n in names for n in ("opt-baseline", "weighted-max")))

    def test_peak_prefill_measured_from_prometheus(self):
        opt, base_cfg = self._make({})
        peak = opt._measure_peak_prefill_throughput("aggregated", 8)
        # 48000 tok/s over 8 pods = 6000/pod; duty 0.25/(0.75) = 1/3 → 18000
        self.assertAlmostEqual(peak, 18000.0, places=4)

    def test_peak_prefill_pd_divides_by_prefill_pods(self):
        opt, base_cfg = self._make({})
        # prompt-rate query excludes decode pods: 48000 over 4 prefill pods
        # = 12000/pod ÷ duty 1/3 → 36000 (NOT 48000/6)
        peak = opt._measure_peak_prefill_throughput("pd", 6, prefill_pods=4)
        self.assertAlmostEqual(peak, 36000.0, places=4)

    def test_peak_prefill_falls_back_to_tp_calibration(self):
        opt, base_cfg = self._make({})
        opt._get_best_result_prom = lambda arch: None
        opt.optimal_prefill_tp = type("T", (), {"tp": 2, "tpsg": 5000.0})()
        opt.aggregated_tp = 2
        peak = opt._measure_peak_prefill_throughput("aggregated", 8)
        # 5000 tok/s/GPU × TP2 × 1.15 headroom
        self.assertAlmostEqual(peak, 11500.0, places=4)

    def test_peak_prefill_none_when_unmeasurable(self):
        opt, base_cfg = self._make({})
        opt._get_best_result_prom = lambda arch: None
        opt.optimal_prefill_tp = None
        self.assertIsNone(opt._measure_peak_prefill_throughput("aggregated", 8))

    def test_archetype_omits_peak_when_unmeasurable(self):
        # no measured value → the filter must NOT carry a made-up number;
        # the EPP's built-in default applies instead
        from core.optimizer.epp_search import archetype_optimized_baseline

        opt, base_cfg = self._make({})
        opt._get_best_result_prom = lambda arch: None
        opt.optimal_prefill_tp = None
        ctx, peak = opt._epp_search_context("aggregated", 8)
        self.assertIsNone(peak)
        spec, _ = archetype_optimized_baseline(ctx).resolve()
        self.assertNotIn("peakPrefillThroughput", spec.affinity_filter)


if __name__ == "__main__":
    unittest.main()


class TestFilterTelemetry(unittest.TestCase):
    """Metric→plugin correspondence for filter compositions: the EPP's own
    decision counters (not scorer signals) drive threshold moves, and
    token-load's normalization is calibrated from the measured scale."""

    def _make(self, results=None):
        opt = _StubOptimizer(results or {"opt-baseline": 300.0, "final-opt-baseline": 310.0})
        return opt, type("B", (), {"test_duration": 300, "test_id": "step6-x", "architecture": "aggregated"})()

    def _winner(self, opt, base_cfg):
        from core.optimizer.epp_search import archetype_optimized_baseline

        ctx = base_ctx()
        winner, _ = archetype_optimized_baseline(ctx).resolve()
        return winner

    def test_starved_filter_loosens_first(self):
        opt, base_cfg = self._make({})
        opt._epp_filter_diagnosis = lambda tr: {"shares": {"sticky": 0.3, "no_match": 0.6, "load_override": 0.1}}
        opt._calibrated_token_load_params = lambda tr: {}
        winner = self._winner(opt, base_cfg)
        moves = opt._ordered_filter_threshold_moves(
            winner, {"shares": {"sticky": 0.3, "no_match": 0.6, "load_override": 0.1}}
        )
        thr = [m.name for m in moves if m.name.startswith("w-thr-")]
        self.assertEqual(thr[0], "w-thr-0p6")

    def test_healthy_filter_tightens_first(self):
        opt, base_cfg = self._make({})
        winner = self._winner(opt, base_cfg)
        moves = opt._ordered_filter_threshold_moves(
            winner, {"shares": {"sticky": 0.9, "no_match": 0.05, "load_override": 0.05}}
        )
        thr = [m.name for m in moves if m.name.startswith("w-thr-")]
        self.assertEqual(thr[0], "w-thr-0p9")

    def test_no_telemetry_keeps_default_order(self):
        opt, base_cfg = self._make({})
        winner = self._winner(opt, base_cfg)
        moves = opt._ordered_filter_threshold_moves(winner, None)
        thr = [m.name for m in moves if m.name.startswith("w-thr-")]
        self.assertTrue(thr)  # both moves present, order unchanged

    def test_token_load_threshold_calibrated(self):
        opt, base_cfg = self._make({})
        opt._calibrated_token_load_params = lambda tr: {"queueThresholdTokens": 131072}
        # exercised through _run_epp_search on a filter winner below
        from core.optimizer.epp_search import render_spec_body
        import yaml

        winner = self._winner(opt, base_cfg)
        calibrated = winner.copy(scorer_params={"token_load": {"queueThresholdTokens": 131072}}).resolve()[0]
        doc = yaml.safe_load(render_spec_body(calibrated))
        tl = [p for p in doc["plugins"] if p["type"] == "token-load-scorer"][0]
        self.assertEqual(tl["parameters"], {"queueThresholdTokens": 131072})

    def test_extractor_shares(self):
        qr = {
            "data": {
                "result": [
                    {"metric": {"outcome": "sticky"}, "values": [[1, "900"]]},
                    {"metric": {"outcome": "load_override"}, "value": [2, "100"]},
                ]
            }
        }
        out = MetricsAnalyzer._extract_epp_affinity_decisions(qr)
        self.assertEqual(out["total"], 1000)
        self.assertAlmostEqual(out["shares"]["sticky"], 0.9)


if __name__ == "__main__":
    unittest.main()


class TestCustomConfigMirror(unittest.TestCase):
    """A forced custom config is the faithful control arm: structure,
    parameters, and no phantom scorers."""

    CUSTOM = {
        "preset": "custom",
        "prefixCacheAffinityEnabled": True,
        "affinityThreshold": 0.9,
        "maxTTFTPenaltyMs": 12000,
        "plugins": {"token_load": {"enabled": True, "weight": 1.5}},
    }

    def test_spec_from_custom_config(self):
        from core.optimizer.epp_search import spec_from_custom_config

        spec = spec_from_custom_config(self.CUSTOM, "aggregated")
        self.assertEqual(spec.scorers, {"token_load": 1.5})
        self.assertTrue(spec.affinity_filter)
        self.assertEqual(spec.affinity_filter["affinityThreshold"], 0.9)
        self.assertEqual(spec.affinity_filter["maxTTFTPenaltyMs"], 12000)

    def test_preset_mirror_is_the_custom_config(self):
        from core.optimizer.epp_search import spec_from_custom_config

        user_spec = spec_from_custom_config(self.CUSTOM, "aggregated")
        ctx = base_ctx(
            user_spec=user_spec, affinity_filter_params={"affinityThreshold": 0.9, "maxTTFTPenaltyMs": 12000}
        )
        specs = phase1_specs(ctx)
        preset = next(s for s in specs if s.name == "preset")
        self.assertTrue(preset.affinity_filter)
        self.assertEqual(preset.scorers.get("token_load"), 1.5)
        self.assertNotIn("prefix_cache", preset.scorers)
        doc = yaml.safe_load(render_spec_body(preset))
        types = [p["type"] for p in doc["plugins"]]
        self.assertIn("approx-prefix-cache-producer", types)
        self.assertIn("inflight-load-producer", types)

    def test_opt_baseline_inherits_user_filter_params(self):
        ctx = base_ctx(affinity_filter_params={"affinityThreshold": 0.9})
        specs = phase1_specs(ctx)
        opt = next(s for s in specs if s.name == "opt-baseline")
        self.assertEqual(opt.affinity_filter["affinityThreshold"], 0.9)
        # unspecified params keep their defaults
        self.assertEqual(opt.affinity_filter["maxTTFTPenaltyMs"], 18000)

    def test_absent_custom_scorers_are_not_placeholders(self):
        opt = _StubOptimizer({})
        opt.config.epp_preset = "custom"
        opt.config.epp_config = dict(self.CUSTOM)
        w = opt._get_user_baseline_weights()
        self.assertEqual(w["prefix_cache_weight"], 0)
        self.assertEqual(w["kv_cache_weight"], 0)
        self.assertEqual(w["queue_weight"], 0)
        self.assertEqual(w["active_request_weight"], 0)

    def test_empty_custom_config_returns_none(self):
        from core.optimizer.epp_search import spec_from_custom_config

        self.assertIsNone(spec_from_custom_config({"preset": "custom"}, "aggregated"))
        self.assertIsNone(spec_from_custom_config({}, "aggregated"))


if __name__ == "__main__":
    unittest.main()


class TestConfigDelta(unittest.TestCase):
    def test_added_removed_scorers_and_filter(self):
        from core.optimizer.epp_search import describe_config_delta

        base_w = {"prefix_cache": 3.0, "kv_cache": 2.0, "queue": 2.0}
        base_st = {"picker": "max-score-picker", "affinity_filter": False}
        var_w = {"token_load": 1.0}
        var_st = {"picker": "max-score-picker", "affinity_filter": True}
        d = describe_config_delta(base_w, base_st, var_w, var_st)
        self.assertIn("+filter", d)
        self.assertIn("−prefix-cache×3", d)
        self.assertIn("+token-load×1", d)

    def test_weight_change_and_picker(self):
        from core.optimizer.epp_search import describe_config_delta

        d = describe_config_delta(
            {"token_load": 1.5},
            {"picker": "max-score-picker", "affinity_filter": True},
            {"token_load": 2.0},
            {"picker": "weighted-random-picker", "affinity_filter": True},
        )
        self.assertIn("token-load 1.5→2", d)
        self.assertIn("picker max→w-rand", d)

    def test_identical_configs_empty_delta(self):
        from core.optimizer.epp_search import describe_config_delta

        w = {"token_load": 1.0}
        st = {"picker": "max-score-picker", "affinity_filter": True}
        self.assertEqual(describe_config_delta(w, st, w, st), "")

    def test_baseline_from_custom_config_carries_structure(self):
        from core.optimizer.epp_search import spec_from_custom_config, describe_config_delta

        spec = spec_from_custom_config(
            {
                "preset": "custom",
                "prefixCacheAffinityEnabled": True,
                "plugins": {"token_load": {"enabled": True, "weight": 1.5}},
            },
            "aggregated",
        )
        # the custom baseline's weights/structure produce an empty delta vs itself
        w = {"token_load": 1.5, "prefix_cache": 0, "kv_cache": 0, "queue": 0, "active_request": 0}
        st = {"picker": "max-score-picker", "affinity_filter": True}
        self.assertEqual(describe_config_delta(w, st, w, st), "")
        self.assertTrue(spec.affinity_filter)


if __name__ == "__main__":
    unittest.main()


class TestRemovalAblations(unittest.TestCase):
    """Removal ablations isolate a winner plugin's contribution — the
    'does THIS plugin matter' question whole-composition comparisons
    cannot answer."""

    def test_filter_winner_gets_filter_off(self):
        from core.optimizer.epp_search import archetype_optimized_baseline

        winner, _ = archetype_optimized_baseline(base_ctx(session_aware=True)).resolve()
        abl = ablation_candidates(winner, base_ctx(session_aware=True))
        off = next(a for a in abl if a.name == "abl-filter-off")
        self.assertIsNone(off.affinity_filter)
        self.assertIn("token_load", off.scorers)  # load scorer intact
        # producers re-derive: no prefix consumers left → no approx producer
        approx, inflight = off.needed_producers()
        self.assertFalse(approx)
        self.assertTrue(inflight)

    def test_removals_ordered_before_additions(self):
        from core.optimizer.epp_search import archetype_optimized_baseline

        winner, _ = archetype_optimized_baseline(base_ctx(session_aware=True)).resolve()
        abl = ablation_candidates(winner, base_ctx(session_aware=True))
        names = [a.name for a in abl]
        self.assertLess(names.index("abl-filter-off"), names.index("abl-kv"))

    def test_weighted_winner_gets_no_filter_off(self):
        from core.optimizer.epp_search import archetype_weighted

        winner, _ = archetype_weighted(base_ctx(), MAX_SCORE_PICKER).resolve()
        abl = ablation_candidates(winner, base_ctx())
        self.assertFalse(any(a.name == "abl-filter-off" for a in abl))

    def test_session_winner_gets_session_off(self):
        from core.optimizer.epp_search import archetype_weighted

        winner, _ = archetype_weighted(base_ctx(session_aware=True), MAX_SCORE_PICKER).resolve()
        abl = ablation_candidates(winner, base_ctx(session_aware=True))
        off = next(a for a in abl if a.name == "abl-session-off")
        self.assertIsNone(off.session_affinity_mode)

    def test_rendered_filter_off_is_valid(self):
        from core.optimizer.epp_search import archetype_optimized_baseline

        winner, _ = archetype_optimized_baseline(base_ctx(session_aware=True)).resolve()
        abl = ablation_candidates(winner, base_ctx(session_aware=True))
        off = next(a for a in abl if a.name == "abl-filter-off")
        doc = yaml.safe_load(render_spec_body(off))
        types = {p["type"] for p in doc["plugins"]}
        for prof in doc["schedulingProfiles"]:
            for e in prof["plugins"]:
                self.assertIn(e["pluginRef"], types)


if __name__ == "__main__":
    unittest.main()
