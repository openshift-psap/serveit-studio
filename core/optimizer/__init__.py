"""
InfeRecipe Optimizer — split into focused modules:

- config.py         — dataclasses (RecipeOptimizerConfig, OptimalTP, FeasibleSplit)
- pipeline.py       — RecipeOptimizer class, __init__, optimize(), _build_results()
- tp_calibration.py — Steps 2-3: decode/prefill TP sweeps
- pd_search.py      — Steps 4-7: feasible splits, pareto, PD validation
- latency_search.py — Steps 10-11: latency-bounded search, calibrated load
- config_builder.py — Test config creation, vLLM parameter computation
- epp_tuning.py     — Step 9: EPP weight sweep
- dataset.py        — Prefix cache dataset generation
"""

from core.optimizer.config import RecipeOptimizerConfig, OptimalTP, FeasibleSplit, EPConfig
from core.optimizer.pipeline import RecipeOptimizer
