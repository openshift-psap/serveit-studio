"""Regression tests: every optimization strategy that produces a Pareto/EP
front must run Step 11 (Calibrated Load Validation / Concurrency Sweep)
when the user enables it.

Bug: PDOnlyStrategy and EPOnlyStrategy never called
_validate_at_calibrated_load — a pd_only run with the Concurrency Sweep
enabled silently skipped it (run 46, Oct 11 2026)."""

import types



class _FakeOptimizer:
    """Minimal RecipeOptimizer stand-in capturing strategy-driven calls."""

    def __init__(self, calibrated_load_enabled, has_results):
        self.config = types.SimpleNamespace(calibrated_load_enabled=calibrated_load_enabled, total_gpus=32)
        self.pareto_results = [object()] if has_results else []
        self.ep_results = [object()] if has_results else []
        self.calls = []
        self.logs = []

    def log(self, msg, level='info'):
        self.logs.append(msg)

    def _should_stop(self):
        return False

    def _recalculate_achievable_concurrency(self):
        self.calls.append('recalculate')

    def _validate_at_calibrated_load(self):
        self.calls.append('validate')

    def _validate_ep_at_calibrated_load(self):
        self.calls.append('validate_ep')

    def _should_run_latency_bounded_search(self):
        return False

    def _run_latency_bounded_search(self):
        pass

    def _select_tp_pairs(self):
        pass

    def _calculate_feasible_splits(self):
        pass

    def _optimize_pd_splits(self):
        pass


def _strategy_with_mixin(opt, strategy_cls):
    """Attach the no-op helper mixins the strategies expect."""

    class _WithHelpers(strategy_cls):
        def _run_epp_tuning_if_enabled(self):
            pass

        def _run_speculative_if_enabled(self):
            pass

        def _run_cache_sweep_if_enabled(self):
            pass

    return _WithHelpers(opt)


def _run_strategy(strategy_cls, calibrated_load_enabled=True, has_results=True):
    opt = _FakeOptimizer(calibrated_load_enabled, has_results)
    strategy = _strategy_with_mixin(opt, strategy_cls)
    # EPOnlyStrategy borrows ThroughputStrategy internals at execute time;
    # stub the EP config enumeration/test phases (covered elsewhere).
    from core.optimization_strategies import ThroughputStrategy
    saved_calc = ThroughputStrategy._calculate_ep_configs
    saved_test = ThroughputStrategy._test_ep_configs
    saved_validate = ThroughputStrategy._validate_ep_at_calibrated_load
    ThroughputStrategy._calculate_ep_configs = lambda self: None
    ThroughputStrategy._test_ep_configs = lambda self: None
    ThroughputStrategy._validate_ep_at_calibrated_load =         lambda self: self.opt.calls.append('validate_ep')
    try:
        strategy.execute()
    finally:
        ThroughputStrategy._calculate_ep_configs = saved_calc
        ThroughputStrategy._test_ep_configs = saved_test
        ThroughputStrategy._validate_ep_at_calibrated_load = saved_validate
    return opt


class TestPDOnlyStrategyStep11:
    def test_sweep_runs_when_enabled(self):
        from core.optimization_strategies import PDOnlyStrategy
        opt = _run_strategy(PDOnlyStrategy)
        assert 'validate' in opt.calls

    def test_sweep_skipped_when_disabled(self):
        from core.optimization_strategies import PDOnlyStrategy
        opt = _run_strategy(PDOnlyStrategy, calibrated_load_enabled=False)
        assert 'validate' not in opt.calls

    def test_sweep_skipped_without_pareto_results(self):
        from core.optimization_strategies import PDOnlyStrategy
        opt = _run_strategy(PDOnlyStrategy, has_results=False)
        assert 'validate' not in opt.calls


class TestEPOnlyStrategyStep11:
    def test_sweep_runs_when_enabled(self):
        from core.optimization_strategies import EPOnlyStrategy
        opt = _run_strategy(EPOnlyStrategy)
        assert 'validate_ep' in opt.calls

    def test_sweep_skipped_when_disabled(self):
        from core.optimization_strategies import EPOnlyStrategy
        opt = _run_strategy(EPOnlyStrategy, calibrated_load_enabled=False)
        assert 'validate_ep' not in opt.calls
