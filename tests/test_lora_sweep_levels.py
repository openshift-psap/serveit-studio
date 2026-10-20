"""Tests for LoRA sweep level generation — explicit, step, and even modes.

Covers the three mutually exclusive modes of _get_lora_sweep_levels:
1. Explicit levels (counts or % of N) — wins over everything
2. Step mode (lora_sweep_step): count ("25") or % of N ("50%") —
   generates lora_sweep_count levels starting at 0; may exceed N
3. Even (default): count levels evenly spaced up to N
"""

import types

from core.optimizer.lora_sweep import LoraSweepMixin


class _Stub:
    def __init__(self, n, levels=None, count=5, step=None):
        self.config = types.SimpleNamespace(
            lora_num_adapters=n, lora_sweep_levels=levels,
            lora_sweep_count=count, lora_sweep_step=step)


_Stub._get_lora_sweep_levels = LoraSweepMixin._get_lora_sweep_levels


class TestExplicitLevels:
    def test_counts(self):
        assert _Stub(1000, levels=['100', '400', '1000'])._get_lora_sweep_levels() == [100, 400, 1000]

    def test_percentages(self):
        assert _Stub(1000, levels=['20%', '40%', '100%'])._get_lora_sweep_levels() == [200, 400, 1000]

    def test_mixed(self):
        assert _Stub(1000, levels=['5%', '300'])._get_lora_sweep_levels() == [50, 300]

    def test_explicit_wins_over_step(self):
        assert _Stub(100, levels=['20%', '40%'], step='50%')._get_lora_sweep_levels() == [20, 40]


class TestStepMode:
    def test_percent_step_user_example(self):
        # The design example: N=100, count=5, step=50% -> 0,50,100,150,200
        assert _Stub(100, step='50%')._get_lora_sweep_levels() == [0, 50, 100, 150, 200]

    def test_percent_step_30(self):
        assert _Stub(100, step='30%')._get_lora_sweep_levels() == [0, 30, 60, 90, 120]

    def test_absolute_step(self):
        assert _Stub(100, step='25')._get_lweep_levels() if False else \
            _Stub(100, step='25')._get_lora_sweep_levels() == [0, 25, 50, 75, 100]

    def test_count_respected(self):
        assert _Stub(100, count=3, step='50%')._get_lora_sweep_levels() == [0, 50, 100]

    def test_levels_beyond_n(self):
        # Levels may exceed N (mounts the max; needs that many staged)
        assert _Stub(100, count=6, step='50%')._get_lora_sweep_levels() == [0, 50, 100, 150, 200, 250]

    def test_percent_step_without_n_falls_back(self):
        # N unknown: % step is meaningless — fall back to doubling
        assert _Stub(0, step='50%')._get_lora_sweep_levels() == [1, 2, 4, 8, 16]

    def test_level_zero_is_base_only(self):
        # Level 0 flows through the existing guards as base-model-only:
        # template (lora_n > 0), guidellm (_ln > 0), staging (n <= 0),
        # max_loras (n <= 0) all handle 0 naturally
        levels = _Stub(100, step='50%')._get_lora_sweep_levels()
        assert levels[0] == 0


class TestEvenMode:
    def test_default_unchanged(self):
        assert _Stub(1000)._get_lora_sweep_levels() == [200, 400, 600, 800, 1000]

    def test_small_n_doubling(self):
        # N=10 < count*2=10 is false (10 >= 10), so evenly-spaced applies:
        # [2, 4, 6, 8, 10] with nearest-5 rounding -> [1, 5, 10] after dedup
        assert _Stub(10)._get_lora_sweep_levels() == [1, 5, 10]

    def test_step_empty_string_ignored(self):
        assert _Stub(1000, step='')._get_lora_sweep_levels() == [200, 400, 600, 800, 1000]
