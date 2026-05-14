"""
Latency-Bounded Throughput Maximizer

Finds the maximum throughput (concurrent users) that keeps latency under a
user-defined SLA target at a specified percentile.

Two strategies:
1. LatencyBinarySearch — exponential ramp-up to find the upper bound, then
   bisection to converge.  Used for Step 9.
2. LatencyBoundedOptimizer — Optuna TPE sampler (kept for future use).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Tuple, List, Dict

logger = logging.getLogger(__name__)

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False


@dataclass
class LatencyConstraintConfig:
    """User-defined latency SLA parameters."""
    target_ms: float  # e.g., 500.0
    percentile: str   # 'p50', 'p90', 'p95', 'p99'


@dataclass
class LatencyBoundedResult:
    """Result of the latency-bounded throughput search."""
    optimal_concurrency: int
    achieved_throughput: float  # req/s
    achieved_latency_ms: float  # at the target percentile
    target_latency_ms: float
    target_percentile: str
    n_trials: int
    best_config_source: str  # 'pd' or 'aggregated'


def _get_latency_from_result(result, percentile: str) -> Optional[float]:
    """Extract TTFT at the given percentile from a TestResult."""
    mapping = {
        'p50': 'ttft_p50',
        'p90': 'ttft_p90',
        'p95': 'ttft_p95',
        'p99': 'ttft_p99',
    }
    attr = mapping.get(percentile)
    if attr and result:
        return getattr(result, attr, None)
    return None


@dataclass
class LatencySearchResult:
    """Result of latency binary search for one architecture."""
    architecture: str
    optimal_concurrency: int
    achieved_throughput: float
    achieved_latency_ms: float
    target_latency_ms: float
    target_percentile: str
    n_trials: int
    all_trials: List[Dict]


class LatencyBinarySearch:
    """
    Finds maximum throughput under a latency SLA using exponential search
    followed by bisection.

    Phase 1 — Ramp-up: starting from a known-good concurrency, double
    repeatedly until TTFT exceeds the SLA.  If the initial TTFT is far
    below target, the first jump is proportional to the headroom.

    Phase 2 — Bisection: binary search between the last feasible and
    first infeasible concurrency.  Stops when the gap is < 5% of low.

    Runs independently for each architecture (PD, aggregated).
    """

    def __init__(
        self,
        constraint: LatencyConstraintConfig,
        run_test_fn: Callable,
        create_config_fn: Callable,
        log_fn: Callable,
        stop_check_fn: Callable,
        save_test_fn: Callable,
        completed_tests: dict,
        make_result_from_db_fn: Callable,
        starting_concurrency: int,
        architecture: str,
        db_manager=None,
        run_id: Optional[int] = None,
    ):
        self.constraint = constraint
        self.run_test = run_test_fn
        self.create_config = create_config_fn
        self.log = log_fn
        self.stop_check = stop_check_fn
        self.save_test = save_test_fn
        self.completed_tests = completed_tests
        self.make_result_from_db = make_result_from_db_fn
        self.starting_concurrency = starting_concurrency
        self.architecture = architecture
        self.db_manager = db_manager
        self.run_id = run_id
        self._trial_num = 0
        self._trials: List[Dict] = []
        self._tested_concurrencies: set = set()

    def _load_previous_trials(self):
        """Load previous trials from DB to reconstruct search state on resume.

        Returns (low, high) where:
          low  = highest concurrency that met SLA (or None)
          high = lowest concurrency that failed SLA (or None)
        """
        if not self.db_manager or not self.run_id:
            return None, None

        try:
            rows = self.db_manager.get_latency_search_trials(
                self.run_id, architecture=self.architecture
            )
        except Exception:
            return None, None

        if not rows:
            return None, None

        self.log(f"  ⏩ Resuming: found {len(rows)} previous trials", 'info')

        low = None
        high = None
        for row in rows:
            c = row['concurrency']
            success = bool(row['guidellm_success'])
            meets_sla = bool(row['meets_sla'])

            self._tested_concurrencies.add(c)

            pct_col = f"ttft_{self.constraint.percentile}"
            trial = {
                'trial_number': row['trial_number'],
                'concurrency': c,
                'phase': row['search_phase'],
                'test_id': row['test_id'],
                'success': success,
                'meets_sla': meets_sla,
                'latency': row.get(pct_col) or row.get('ttft_p90'),
                'throughput': row.get('throughput_p90', 0) or 0,
                'result': None,
            }
            self._trials.append(trial)

            if success and meets_sla:
                if low is None or c > low:
                    low = c
            elif success and not meets_sla:
                if high is None or c < high:
                    high = c

            status = "✅" if meets_sla else ("⛔" if success else "❌")
            lat = trial['latency']
            lat_str = f"{lat:.1f}ms" if lat else "N/A"
            self.log(f"    {status} c={c}: TTFT {self.constraint.percentile.upper()}"
                     f"={lat_str} [{row['search_phase']}] (from DB)", 'info')

        self._trial_num = len(rows)

        if low is not None:
            self.log(f"  📊 Reconstructed: low={low}" +
                     (f", high={high}" if high else "") +
                     f" ({len(rows)} trials)", 'info')

        return low, high

    def _test_concurrency(self, concurrency: int, phase: str):
        """Run a benchmark at the given concurrency, return (result, latency, meets_sla)."""
        test_id = f"step9-{self.architecture}-c{concurrency}"

        if test_id in self.completed_tests:
            row = self.completed_tests[test_id]
            result = self.make_result_from_db(row)
            self.log(f"    ⏩ c={concurrency} (resuming from DB)", 'info')
        else:
            config = self.create_config(concurrency, test_id)
            result = self.run_test(config)
            if result and result.guidellm_success:
                self.save_test(config, result)

        latency = None
        meets_sla = False
        success = result is not None and result.guidellm_success

        if success:
            latency = _get_latency_from_result(result, self.constraint.percentile)
            if latency is not None and latency > 0:
                meets_sla = latency <= self.constraint.target_ms
            elif latency is not None and latency <= 0:
                success = False
                self.log(f"    ⚠️  c={concurrency}: zero latency — no valid results parsed", 'warning')

        self._tested_concurrencies.add(concurrency)

        trial = {
            'trial_number': self._trial_num,
            'concurrency': concurrency,
            'phase': phase,
            'test_id': test_id,
            'success': success,
            'meets_sla': meets_sla,
            'latency': latency,
            'throughput': (result.throughput_p90 or 0) if success else 0,
            'result': result,
        }
        self._trials.append(trial)

        if self.db_manager and self.run_id:
            try:
                self.db_manager.insert_latency_search_trial(
                    run_id=self.run_id,
                    architecture=self.architecture,
                    trial_number=self._trial_num,
                    search_phase=phase,
                    concurrency=concurrency,
                    test_id=test_id,
                    guidellm_success=success,
                    meets_sla=meets_sla,
                    result=result if success else None,
                    target_ms=self.constraint.target_ms,
                    target_percentile=self.constraint.percentile,
                )
            except Exception as e:
                logger.warning(f"Failed to save search trial: {e}")

        self._trial_num += 1

        status = "✅" if meets_sla else ("⛔" if success else "❌")
        lat_str = f"{latency:.1f}ms" if latency is not None else "N/A"
        tput_str = f"{trial['throughput']:.2f}" if success else "N/A"
        self.log(f"  {status} c={concurrency}: TTFT {self.constraint.percentile.upper()}"
                 f"={lat_str} (target: {self.constraint.target_ms}ms), "
                 f"throughput={tput_str} req/s  [{phase}]", 'info')

        return result, latency, meets_sla

    def search(self) -> Optional[LatencySearchResult]:
        """Run the full exponential-then-bisect search, with resume support."""
        pct = self.constraint.percentile.upper()
        self.log(f"  🔍 {self.architecture.upper()}: target TTFT {pct} "
                 f"≤ {self.constraint.target_ms}ms, "
                 f"starting at c={self.starting_concurrency}", 'info')

        # --- Resume: reconstruct state from previous trials ---
        prev_low, prev_high = self._load_previous_trials()

        if prev_low is not None and prev_high is not None:
            # We already have a bracket — skip straight to bisection
            low, high = prev_low, prev_high
            self.log(f"  ⏩ Resuming bisection: low={low}, high={high}", 'info')
        elif prev_low is not None:
            # All previous trials passed — continue ramp-up from highest good value
            low = prev_low
            high = None
            c = low * 2
            self.log(f"  ⏩ Resuming ramp-up from c={low}", 'info')

            while not self.stop_check():
                if c in self._tested_concurrencies:
                    c = c * 2
                    continue
                result, latency, meets_sla = self._test_concurrency(c, 'ramp_up')
                if not result or not result.guidellm_success or not meets_sla:
                    high = c
                    break
                low = c
                c = c * 2

            if high is None:
                self.log(f"  ⚠️  Ramp-up reached c={c} without exceeding SLA, "
                         f"using c={low} as best", 'warning')
                return self._build_result()
        elif prev_high is not None:
            # All previous trials failed — ramp down to find a passing value
            high = prev_high
            low = None
            self.log(f"  ⏩ Resuming: all previous failed, "
                     f"ramping down from c={high}", 'info')
            c = max(1, high // 2)
            while c >= 1 and not self.stop_check():
                if c in self._tested_concurrencies:
                    if c == 1:
                        break
                    c = max(1, c // 2)
                    continue
                result, latency, meets_sla = self._test_concurrency(
                    c, 'ramp_down')
                if meets_sla:
                    low = c
                    break
                high = c
                if c == 1:
                    break
                c = max(1, c // 2)
            if low is None:
                self.log(f"  ❌ Cannot meet SLA even at c=1", 'error')
                return self._build_result()
        else:
            # --- Fresh start: Phase 1 ramp-up ---
            low = None
            high = None
            c = self.starting_concurrency

            result, latency, meets_sla = self._test_concurrency(c, 'ramp_up')

            if not result or not result.guidellm_success:
                self.log(f"  ❌ Initial test at c={c} failed", 'error')
                return None

            if not meets_sla:
                self.log(f"  ⚠️  Starting concurrency c={c} exceeds SLA, "
                         f"ramping down to find a passing value", 'warning')
                high = c
                # Ramp down (halving) until we find a concurrency that meets SLA
                c = max(1, c // 2)
                while c >= 1 and not self.stop_check():
                    result, latency, meets_sla = self._test_concurrency(
                        c, 'ramp_down')
                    if meets_sla:
                        low = c
                        break
                    high = c
                    if c == 1:
                        break
                    c = max(1, c // 2)
                if low is None:
                    self.log(f"  ❌ Cannot meet SLA even at c=1", 'error')
                    return self._build_result()
            else:
                low = c
                if latency and latency > 0:
                    headroom_ratio = self.constraint.target_ms / latency
                    if headroom_ratio >= 2:
                        c = int(c * headroom_ratio)
                        self.log(f"    💨 Large headroom ({latency:.0f}ms vs "
                                 f"{self.constraint.target_ms}ms target), "
                                 f"jumping to c={c}", 'info')
                    else:
                        c = c * 2
                else:
                    c = c * 2

                while not self.stop_check():
                    result, latency, meets_sla = self._test_concurrency(
                        c, 'ramp_up')
                    if not result or not result.guidellm_success or not meets_sla:
                        high = c
                        break
                    low = c
                    c = c * 2

                if high is None:
                    self.log(f"  ⚠️  Ramp-up reached c={c} without exceeding "
                             f"SLA, using c={low} as best", 'warning')
                    return self._build_result()

        # --- Phase 2: Bisection ---
        self.log(f"  📐 Bisecting between c={low} and c={high}", 'info')

        while not self.stop_check():
            gap_pct = (high - low) / max(low, 1)
            if gap_pct < 0.05 or (high - low) <= 1:
                break

            mid = (low + high) // 2
            if mid == low or mid == high:
                break

            if mid in self._tested_concurrencies:
                # Already tested this value — use stored result to update bounds
                prev = next((t for t in self._trials if t['concurrency'] == mid), None)
                if prev and prev['success']:
                    if prev['meets_sla']:
                        low = mid
                    else:
                        high = mid
                    continue
                else:
                    high = mid
                    continue

            result, latency, meets_sla = self._test_concurrency(
                mid, 'binary_search')

            if not result or not result.guidellm_success:
                high = mid
                continue

            if meets_sla:
                low = mid
            else:
                high = mid

        return self._build_result()

    def _build_result(self) -> Optional[LatencySearchResult]:
        """Build result from the best feasible trial."""
        feasible = [t for t in self._trials if t['success'] and t['meets_sla']]
        if not feasible:
            self.log(f"  ❌ {self.architecture.upper()}: no concurrency met the SLA", 'error')
            return None

        best = max(feasible, key=lambda t: t['throughput'])
        self.log(f"  🏆 {self.architecture.upper()} optimal: c={best['concurrency']}, "
                 f"throughput={best['throughput']:.2f} req/s, "
                 f"TTFT {self.constraint.percentile.upper()}="
                 f"{best['latency']:.1f}ms", 'decision')

        return LatencySearchResult(
            architecture=self.architecture,
            optimal_concurrency=best['concurrency'],
            achieved_throughput=best['throughput'],
            achieved_latency_ms=best['latency'],
            target_latency_ms=self.constraint.target_ms,
            target_percentile=self.constraint.percentile,
            n_trials=len(self._trials),
            all_trials=self._trials,
        )


class LatencyBoundedOptimizer:
    """
    Finds maximum throughput under a latency constraint using Optuna.

    Strategy:
    1. Start from the best config found in Steps 7-8
    2. Use Optuna TPE sampler to search concurrency levels
    3. For each trial, run a benchmark at the suggested concurrency
    4. Optuna maximizes throughput subject to the latency constraint
    """

    def __init__(
        self,
        constraint: LatencyConstraintConfig,
        run_test_fn: Callable,
        create_config_fn: Callable,
        log_fn: Callable,
        stop_check_fn: Callable,
        save_test_fn: Callable,
        completed_tests: dict,
        make_result_from_db_fn: Callable,
        max_trials: int = 20,
        min_concurrency: int = 1,
        max_concurrency: int = 500,
        db_manager=None,
        run_id: Optional[int] = None,
        optimization_step: str = 'step9_latency_bounded',
    ):
        self.constraint = constraint
        self.run_test = run_test_fn
        self.create_config = create_config_fn
        self.log = log_fn
        self.stop_check = stop_check_fn
        self.save_test = save_test_fn
        self.completed_tests = completed_tests
        self.make_result_from_db = make_result_from_db_fn
        self.max_trials = max_trials
        self.min_concurrency = min_concurrency
        self.max_concurrency = max_concurrency
        self.db_manager = db_manager
        self.run_id = run_id
        self.optimization_step = optimization_step
        self._all_results = []

    def optimize(self) -> Optional[LatencyBoundedResult]:
        if not OPTUNA_AVAILABLE:
            self.log("❌ Optuna not installed — skipping latency-bounded search", 'error')
            self.log("   Install with: pip install optuna", 'info')
            return None

        self.log(f"🎯 Target: TTFT {self.constraint.percentile.upper()} "
                 f"≤ {self.constraint.target_ms}ms", 'info')
        self.log(f"   Search range: {self.min_concurrency}-{self.max_concurrency} "
                 f"concurrent users, up to {self.max_trials} trials", 'info')
        self.log("", 'info')

        study = optuna.create_study(
            direction='maximize',
            sampler=optuna.samplers.TPESampler(seed=42),
            study_name='latency_bounded_throughput'
        )

        def objective(trial):
            if self.stop_check():
                trial.study.stop()
                return float('-inf')

            concurrency = trial.suggest_int(
                'concurrency',
                self.min_concurrency,
                self.max_concurrency
            )

            test_id = f"step9-lbo-c{concurrency}"

            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self.make_result_from_db(row)
                self.log(f"  ⏩ Trial {trial.number+1}: concurrency={concurrency} "
                         f"(resuming from DB)", 'info')
            else:
                config = self.create_config(concurrency, test_id)
                result = self.run_test(config)

                if result and result.guidellm_success:
                    self.save_test(config, result)

            if not result or not result.guidellm_success:
                self.log(f"  ❌ Trial {trial.number+1}: concurrency={concurrency} — "
                         f"test failed", 'warning')
                self._save_trial(
                    trial_number=trial.number, concurrency=concurrency,
                    test_id=test_id, guidellm_success=False,
                    ttft_ms=None, throughput=None,
                    meets_constraint=False, objective_value=float('-inf'),
                    trial_state='FAIL',
                )
                return float('-inf')

            latency = _get_latency_from_result(result, self.constraint.percentile)
            throughput = result.throughput_p90 or result.throughput_p50 or 0

            if latency is None:
                self.log(f"  ⚠️  Trial {trial.number+1}: concurrency={concurrency} — "
                         f"no {self.constraint.percentile} data", 'warning')
                self._save_trial(
                    trial_number=trial.number, concurrency=concurrency,
                    test_id=test_id, guidellm_success=True,
                    ttft_ms=None, throughput=throughput,
                    meets_constraint=False, objective_value=float('-inf'),
                    trial_state='COMPLETE',
                )
                return float('-inf')

            self._all_results.append((concurrency, throughput, latency, result))

            meets_sla = latency <= self.constraint.target_ms
            status = "✅" if meets_sla else "⛔"

            self.log(f"  {status} Trial {trial.number+1}: concurrency={concurrency} → "
                     f"throughput={throughput:.2f} req/s, "
                     f"TTFT {self.constraint.percentile.upper()}={latency:.1f}ms "
                     f"(target: {self.constraint.target_ms}ms)", 'info')

            obj_value = throughput if meets_sla else float('-inf')

            self._save_trial(
                trial_number=trial.number,
                concurrency=concurrency,
                test_id=test_id,
                guidellm_success=True,
                ttft_ms=latency,
                throughput=throughput,
                meets_constraint=meets_sla,
                objective_value=obj_value,
            )

            if not meets_sla:
                return float('-inf')

            return throughput

        study.optimize(objective, n_trials=self.max_trials)

        feasible = [r for r in self._all_results if r[2] <= self.constraint.target_ms]

        if not feasible:
            self.log("", 'info')
            self.log("❌ No concurrency level met the latency SLA", 'error')
            self.log(f"   All {len(self._all_results)} trials exceeded "
                     f"{self.constraint.target_ms}ms at "
                     f"{self.constraint.percentile.upper()}", 'info')
            self._save_study(
                total_trials=len(self._all_results),
                feasible_trials=0,
                best_trial_number=None,
                best_params=None,
                best_throughput=None,
                best_latency_ms=None,
                best_config_source='',
                study_status='NO_FEASIBLE',
            )
            return None

        best = max(feasible, key=lambda r: r[1])
        best_concurrency, best_throughput, best_latency, _ = best

        self.log("", 'info')
        self.log(f"🏆 Optimal: {best_concurrency} concurrent users", 'decision')
        self.log(f"   Throughput: {best_throughput:.2f} req/s", 'success')
        self.log(f"   TTFT {self.constraint.percentile.upper()}: "
                 f"{best_latency:.1f}ms (target: {self.constraint.target_ms}ms)", 'success')
        self.log(f"   Trials: {len(self._all_results)} "
                 f"({len(feasible)} met SLA)", 'info')

        best_trial_num = None
        if study.best_trial:
            best_trial_num = study.best_trial.number

        self._save_study(
            total_trials=len(self._all_results),
            feasible_trials=len(feasible),
            best_trial_number=best_trial_num,
            best_params={'concurrency': best_concurrency},
            best_throughput=best_throughput,
            best_latency_ms=best_latency,
            best_config_source='',
            study_status='COMPLETED',
        )

        return LatencyBoundedResult(
            optimal_concurrency=best_concurrency,
            achieved_throughput=best_throughput,
            achieved_latency_ms=best_latency,
            target_latency_ms=self.constraint.target_ms,
            target_percentile=self.constraint.percentile,
            n_trials=len(self._all_results),
            best_config_source='',
        )

    def _save_trial(
        self,
        trial_number: int,
        concurrency: int,
        test_id: str,
        guidellm_success: bool,
        ttft_ms,
        throughput,
        meets_constraint: bool,
        objective_value: float,
        trial_state: str = 'COMPLETE',
    ):
        """Persist a single trial to the database."""
        if not self.db_manager or not self.run_id:
            return
        try:
            self.db_manager.insert_optuna_trial(
                run_id=self.run_id,
                optimization_step=self.optimization_step,
                trial_number=trial_number,
                trial_params={'concurrency': concurrency},
                test_id=test_id,
                guidellm_success=guidellm_success,
                ttft_ms=ttft_ms,
                throughput=throughput,
                target_percentile=self.constraint.percentile,
                constraint_target_ms=self.constraint.target_ms,
                meets_constraint=meets_constraint,
                objective_value=objective_value,
                trial_state=trial_state,
            )
        except Exception as e:
            logger.warning(f"Failed to save Optuna trial {trial_number}: {e}")

    def _save_study(
        self,
        total_trials: int,
        feasible_trials: int,
        best_trial_number,
        best_params,
        best_throughput,
        best_latency_ms,
        best_config_source: str,
        study_status: str,
    ):
        """Persist the study summary to the database."""
        if not self.db_manager or not self.run_id:
            return
        try:
            self.db_manager.insert_optuna_study(
                run_id=self.run_id,
                optimization_step=self.optimization_step,
                constraint_config={
                    'target_ms': self.constraint.target_ms,
                    'percentile': self.constraint.percentile,
                },
                search_range={
                    'min_concurrency': self.min_concurrency,
                    'max_concurrency': self.max_concurrency,
                },
                total_trials=total_trials,
                feasible_trials=feasible_trials,
                best_trial_number=best_trial_number,
                best_params=best_params,
                best_throughput=best_throughput,
                best_latency_ms=best_latency_ms,
                best_config_source=best_config_source,
                study_status=study_status,
            )
        except Exception as e:
            logger.warning(f"Failed to save Optuna study summary: {e}")
