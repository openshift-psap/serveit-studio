"""Step 12: Speculative decoding comparison."""

import copy
from typing import Optional


class SpeculativeMixin:
    """Mixin providing speculative decoding comparison for RecipeOptimizer."""

    def _should_run_speculative(self) -> bool:
        """Check if Step 12 should run."""
        if self.config.speculative_config_enabled:
            return True
        if self.config.speculative_config_method:
            return True
        return getattr(self, '_supports_mtp', False)

    def _get_speculative_method(self) -> str:
        if self.config.speculative_config_method:
            return self.config.speculative_config_method
        if getattr(self, '_supports_mtp', False):
            return 'mtp'
        return 'mtp'

    def _run_speculative_comparison(self):
        """Step 12: Re-test best configs with speculative decoding enabled.

        Takes the winning aggregated and PD configs, enables speculative
        decoding, runs benchmarks, and compares against the non-speculative
        baselines from earlier steps.
        """
        method = self._get_speculative_method()
        num_tokens = self.config.speculative_config_num_tokens or 3

        self.log("\n" + "=" * 80, 'info')
        self.log("STEP 12: Speculative Decoding Comparison", 'decision')
        self.log("=" * 80, 'info')
        self.log(f"  Method: {method}, num_speculative_tokens: {num_tokens}", 'info')

        if self._should_stop():
            return

        self.speculative_results = {}
        configs_to_test = []

        if self.aggregated_result and self.aggregated_tp:
            agg_cfg = self._create_aggregated_config(
                tp=self.aggregated_tp,
                num_gpus=self.config.total_gpus,
                isl=self.config.isl,
                osl=self.config.osl,
                test_id=f"step12-spec-aggregated-{method}",
                use_concurrency=True,
            )
            configs_to_test.append(('aggregated', agg_cfg, self.aggregated_result))

        if self.pareto_results:
            best_split, best_pd_result = min(
                self.pareto_results,
                key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9
            )
            pd_cfg = self._create_pd_config(best_split)
            pd_cfg.test_id = f"step12-spec-pd-{method}"
            configs_to_test.append(('pd', pd_cfg, best_pd_result))

        if not configs_to_test:
            self.log("  ⚠️  No successful configs to test with speculative decoding", 'warning')
            return

        for arch, cfg, baseline_result in configs_to_test:
            if self._should_stop():
                break

            self.log(f"\n  --- Speculative: {arch.upper()} ({method}, n={num_tokens}) ---", 'decision')

            cfg.speculative_method = method
            cfg.speculative_num_tokens = num_tokens

            baseline_ttft = baseline_result.ttft_p90 or 0
            baseline_tput = baseline_result.throughput_p90 or 0
            self.log(f"  Baseline: TTFT p90={baseline_ttft:.1f}ms, Throughput p90={baseline_tput:.2f} req/s", 'info')

            result = self.orchestrator.run_test(
                cfg,
                cleanup=True,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop,
            )

            if result and result.guidellm_success:
                spec_ttft = result.ttft_p90 or 0
                spec_tput = result.throughput_p90 or 0

                ttft_change = ((spec_ttft - baseline_ttft) / baseline_ttft * 100) if baseline_ttft > 0 else 0
                tput_change = ((spec_tput - baseline_tput) / baseline_tput * 100) if baseline_tput > 0 else 0

                ttft_icon = "✅" if ttft_change < -5 else ("⚠️" if ttft_change > 5 else "➡️")
                tput_icon = "✅" if tput_change > 5 else ("⚠️" if tput_change < -5 else "➡️")

                self.log(f"  {ttft_icon} TTFT p90: {spec_ttft:.1f}ms ({ttft_change:+.1f}% vs baseline)", 'success' if ttft_change < 0 else 'info')
                self.log(f"  {tput_icon} Throughput p90: {spec_tput:.2f} req/s ({tput_change:+.1f}% vs baseline)", 'success' if tput_change > 0 else 'info')

                self.speculative_results[arch] = {
                    'method': method,
                    'num_tokens': num_tokens,
                    'baseline_ttft_p90': baseline_ttft,
                    'baseline_throughput_p90': baseline_tput,
                    'spec_ttft_p90': spec_ttft,
                    'spec_throughput_p90': spec_tput,
                    'ttft_change_pct': round(ttft_change, 1),
                    'tput_change_pct': round(tput_change, 1),
                    'result': result,
                }

                self.all_test_results.append((cfg, result))
                self._save_test_to_database(cfg, result)
            else:
                self.log(f"  ❌ Speculative test failed for {arch}", 'error')
                if result:
                    self.all_test_results.append((cfg, result))
                    self._save_test_to_database(cfg, result)

        if self.speculative_results:
            self.log("\n  📊 Speculative Decoding Summary:", 'decision')
            for arch, data in self.speculative_results.items():
                verdict = "BETTER" if data['ttft_change_pct'] < -5 or data['tput_change_pct'] > 5 else "NO IMPROVEMENT"
                self.log(f"    {arch.upper()}: {verdict} — TTFT {data['ttft_change_pct']:+.1f}%, Throughput {data['tput_change_pct']:+.1f}%", 'info')
