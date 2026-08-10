"""
Backward-compatible re-export — all code moved to core/optimizer/ package.

Import from here continues to work:
    from core.recipe_optimizer import RecipeOptimizer, RecipeOptimizerConfig
"""

from core.optimizer.pipeline import RecipeOptimizer  # noqa: F401
from core.optimizer.config import RecipeOptimizerConfig, FeasibleSplit  # noqa: F401
