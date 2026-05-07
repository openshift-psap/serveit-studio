"""
Backward-compatible re-export — all code moved to core/optimizer/ package.

Import from here continues to work:
    from core.recipe_optimizer import RecipeOptimizer, RecipeOptimizerConfig
"""

from core.optimizer.config import RecipeOptimizerConfig, OptimalTP, FeasibleSplit, EPConfig
from core.optimizer.pipeline import RecipeOptimizer
