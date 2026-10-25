"""LoRA adapter-count sweep.

Maps how the best configuration performs as the number of served LoRA
adapters grows — the multi-tenant scaling curve. Each level deploys the
same architecture with N adapters mounted (agent-01..agent-N), the
benchmark round-robins requests across base + adapters, and the GPU
residency is auto-sized per level (so the curve shows where LRU
swapping begins to dominate once N exceeds the VRAM-derived cap).

Runs as Step 14, after the concurrency sweep (uses its calibrated
concurrency when available).
"""

import logging
from typing import List

logger = logging.getLogger(__name__)


class LoraSweepMixin:
    """Mixin providing the LoRA adapter-count sweep for RecipeOptimizer."""

    def _get_lora_sweep_levels(self) -> List[int]:
        """Adapter counts to test: explicit list, step mode, or evenly spaced.

        Three mutually exclusive modes (explicit wins, then step, then even):

        1. Explicit levels — absolute counts (100, 400, 1000) or percentages
           of N (20%, 40%, 100%): with N=1000, "20%,40%,100%" becomes
           [200, 400, 1000].
        2. Step mode — lora_sweep_step is a count ("25") or % of N ("50%").
           Generates `lora_sweep_count` levels starting at 0:
           N=100, count=5, step=50% -> [0, 50, 100, 150, 200]. Levels may
           exceed N (mounts the max level; requires that many adapters on
           disk). Level 0 = base model only (LoRA overhead baseline).
        3. Even (default) — count levels evenly spaced up to N:
           N/count, 2N/count, ..., N.
        """
        n_total = int(getattr(self.config, 'lora_num_adapters', 0) or 0)
        explicit = getattr(self.config, 'lora_sweep_levels', None)
        if explicit:
            levels = set()
            for entry in explicit:
                entry = str(entry).strip()
                if entry.endswith('%'):
                    if not n_total:
                        continue
                    pct = float(entry[:-1].strip())
                    levels.add(max(1, round(n_total * pct / 100)))
                else:
                    val = int(entry)
                    # 0 = base-model-only level (LoRA overhead baseline),
                    # same as step mode's first level — allow it
                    if val >= 0:
                        levels.add(val)
            if levels:
                return sorted(levels)
        count = int(getattr(self.config, 'lora_sweep_count', 5) or 5)
        step_raw = getattr(self.config, 'lora_sweep_step', None)
        if step_raw:
            step_raw = str(step_raw).strip()
            if step_raw.endswith('%'):
                if not n_total:
                    logger.warning("lora_sweep_step=%s but lora_num_adapters unknown — ignoring", step_raw)
                else:
                    pct = float(step_raw[:-1].strip())
                    step = max(1, round(n_total * pct / 100))
                    return [i * step for i in range(count)]
            else:
                step = int(step_raw)
                if step > 0:
                    return [i * step for i in range(count)]
        # When N is known and large, generate evenly-spaced levels as
        # fractions of N (N/count, 2N/count, ..., N). Doubling from 1
        # (1, 2, 4, 8...) never reaches the interesting range for
        # large adapter counts.
        if n_total and n_total >= count * 2:
            step = n_total // count
            levels = []
            for i in range(count):
                if i == count - 1:
                    levels.append(n_total)
                else:
                    # Round to nearest 5 for clean, readable levels
                    val = step * (i + 1)
                    levels.append(max(1, round(val / 5) * 5))
            return sorted(set(levels))
        # Small N (or N unknown): doubling from 1
        levels = [1]
        while len(levels) < count:
            levels.append(levels[-1] * 2)
        return levels

    def _pick_lora_sweep_config(self):
        """Pick the best config to sweep: top concurrency-sweep point, else
        the best result among EP/PD/Aggregated.

        Returns (kind, payload, concurrency, gpus, label) where payload is a
        FeasibleSplit for pd/ep or a tp int for aggregated, or None.
        """
        # Prefer the concurrency sweep's best point (it carries calibrated load)
        sweep_results = getattr(self, 'concurrency_sweep_results', {}) or {}
        best = None
        for sweep_key, points in sweep_results.items():
            viable = [p for p in points if (p.get('throughput_mean') or 0) > 0]
            if not viable:
                continue
            top = max(viable, key=lambda p: p.get('throughput_mean') or 0)
            if best is None or (top.get('throughput_mean') or 0) > best[0]:
                best = (top.get('throughput_mean'), sweep_key, top)

        if best is not None:
            _, sweep_key, top = best
            cal_c = int(top.get('concurrency') or self.config.qps)
            gpus = int(top.get('gpus') or self.config.total_gpus)
            label = top.get('config_label') or sweep_key
            # Reconstruct the split/tp from the sweep results' source results
            if sweep_key.startswith('pd-') or sweep_key.startswith('ep-'):
                for split, result in (list(self.pareto_results) + list(self.ep_results)):
                    tag = ('pd' if split in [s for s, _ in self.pareto_results]
                           else 'ep')
                    key = f"{tag}-{split.prefill_pods}p{split.decode_pods}d"
                    if key == sweep_key:
                        kind = 'pd' if sweep_key.startswith('pd-') else 'ep'
                        return kind, split, cal_c, gpus, label
            elif sweep_key.startswith('agg-') and getattr(self, 'aggregated_tp', None):
                return 'agg', self.aggregated_tp, cal_c, gpus, label

        # Fall back to the best measured result overall
        candidates = []
        for results, kind in ((self.ep_results, 'ep'), (self.pareto_results, 'pd')):
            for split, result in results:
                tput = result.throughput_mean or result.throughput_p90 or 0
                if tput > 0:
                    candidates.append((tput, kind, split))
        if getattr(self, 'aggregated_result', None):
            tput = (self.aggregated_result.throughput_mean
                    or self.aggregated_result.throughput_p90 or 0)
            if tput > 0 and getattr(self, 'aggregated_tp', None):
                candidates.append((tput, 'agg', self.aggregated_tp))
        if not candidates:
            return None
        _, kind, payload = max(candidates, key=lambda c: c[0])
        # Prefer the tested aggregated config's own GPU count over the
        # cluster total — single tests may use a subset of the cluster
        gpus = (payload.total_gpus if kind in ('pd', 'ep')
                else (getattr(self, 'aggregated_gpus', None) or self.config.total_gpus))
        label = (f"{payload.prefill_pods}P+{payload.decode_pods}D"
                 if kind in ('pd', 'ep') else f"TP{payload}")
        return kind, payload, int(self.config.qps), gpus, label

    def _run_lora_adapter_sweep(self):
        """Step 14: sweep adapter counts on the best configuration."""
        if not getattr(self.config, 'lora_enabled', False):
            self.log("  ⏩ LoRA sweep skipped — LoRA serving not enabled", 'info')
            return
        if not getattr(self.config, 'lora_sweep_enabled', False):
            return

        picked = self._pick_lora_sweep_config()
        if picked is None:
            self.log("  ⚠️  LoRA sweep skipped — no viable configuration found", 'warning')
            return
        kind, payload, concurrency, gpus, label = picked

        levels = self._get_lora_sweep_levels()
        self.log("", 'info')
        self.log("=" * 60, 'info')
        self.log(f"LoRA Adapter Sweep — {label} at {len(levels)} adapter levels", 'decision')
        self.log("=" * 60, 'info')
        self.log(f"   Levels: {levels} (each level = 1 benchmark run, "
                 f"round-robin across base + N adapters)", 'info')

        if not hasattr(self, 'lora_sweep_results'):
            self.lora_sweep_results = {}
        sweep_key = f"{kind}-{label}".lower().replace(' ', '-')
        import re as _re
        sweep_key = _re.sub(r'[^a-z0-9-]', '', sweep_key)
        results = self.lora_sweep_results.setdefault(sweep_key, [])

        for n_adapters in levels:
            if self._should_stop():
                break
            test_id = f"step14-lora-{sweep_key}-n{n_adapters}"

            # Resume support
            existing = None
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                existing = self._make_test_result_from_db(row)
                self.log(f"  ⏩ n={n_adapters}: resuming from DB", 'info')

            if existing is not None:
                result = existing
                cfg = None
            else:
                # Build the config for this adapter count
                if kind in ('pd', 'ep'):
                    current_split = payload
                    cfg = (self._create_pd_config(current_split)
                           if kind == 'pd'
                           else self._create_ep_config(split=current_split))
                else:
                    current_tp = payload
                    agg_pp = getattr(self, 'aggregated_pp', None) or 1
                    cfg = self._create_aggregated_config(
                        tp=current_tp, num_gpus=gpus,
                        isl=self.config.isl, osl=self.config.osl,
                        test_id=test_id, use_concurrency=True,
                        pipeline_parallel_size=agg_pp)
                cfg.test_id = test_id
                cfg.lora_enabled = True
                cfg.lora_num_adapters = n_adapters
                # Re-size residency for this level (the builder clamped
                # against the run-level adapter count)
                tp_for_calc = (payload.prefill_tp if kind in ('pd', 'ep')
                               else payload)
                sweep_pods = (payload.decode_pods if kind in ('pd', 'ep')
                              else max(1, gpus // max(payload or 1, 1)))
                cfg.lora_max_loras = self._compute_lora_max_loras(
                    tp_for_calc, n_override=n_adapters, num_pods=sweep_pods)
                cfg.num_users = int(concurrency)
                cfg.request_rate = int(concurrency)

                result = self.orchestrator.run_test(
                    cfg, cleanup=True,
                    log_callback=lambda msg, _t=test_id: self.log(
                        f"    {msg}" if not msg.startswith(('  ', '\n', '=')) else msg,
                        'info'),
                    stop_check=self._should_stop,
                )
                self.all_test_results.append((cfg, result))
                self._save_test_to_database(cfg, result)
                self._check_pod_errors(cfg, result)
                self._check_request_errors(cfg, result)

            if not result or not result.guidellm_success:
                self.log(f"  ❌ n={n_adapters}: test failed", 'warning')
                continue

            tput = result.throughput_mean or result.throughput_p90 or 0
            ttft = result.ttft_p90 or 0
            # Residency actually used at this level (from the rendered config
            # when fresh; recomputed for resumed results)
            max_loras = (getattr(cfg, 'lora_max_loras', None)
                         if cfg is not None else None)
            if max_loras is None:
                try:
                    tp_for_calc = (payload.prefill_tp if kind in ('pd', 'ep')
                                   else payload)
                    sweep_pods = (payload.decode_pods if kind in ('pd', 'ep')
                                  else max(1, gpus // max(payload or 1, 1)))
                    max_loras = self._compute_lora_max_loras(
                        tp_for_calc, n_override=n_adapters, num_pods=sweep_pods)
                except Exception:
                    max_loras = None
            # Total pool residency: pods x slots per pod. LRU swapping only
            # begins when the adapter working set exceeds the pool total —
            # comparing n against per-pod slots flags every level past the
            # first even when all adapters fit across the pods.
            sweep_pods_count = None
            try:
                if kind in ('pd', 'ep'):
                    sweep_pods_count = payload.decode_pods
                else:
                    sweep_pods_count = max(1, gpus // max(payload or 1, 1))
            except Exception:
                sweep_pods_count = None
            total_slots = (max_loras or 0) * (sweep_pods_count or 0)
            swapping = bool(total_slots and n_adapters > total_slots)
            # Estimated adapter evictions and cache-hit rate: analyzed
            # metrics land on the DATABASE row (TestResult carries only the
            # raw metrics_file), so read the row back after the save above.
            evictions = 0
            adapter_loads = 0
            cache_hit_pct = None
            try:
                import json as _j
                _prom = {}
                if self.db_manager and self.run_id:
                    with self.db_manager.get_connection() as _conn:
                        _row = _conn.execute(
                            'SELECT metrics_json FROM test_configurations '
                            'WHERE run_id=? AND config_name=?',
                            (self.run_id, test_id)).fetchone()
                    if _row and _row[0]:
                        _prom = (_j.loads(_row[0]).get('prometheus_metrics') or {})
                _pods = ((_prom.get('vllm_lora_adapter_info') or {})
                         .get('pods') or {})
                evictions = sum(int(p.get('estimated_adapter_evictions') or 0)
                                for p in _pods.values())
                adapter_loads = sum(int(p.get('estimated_adapter_loads') or 0)
                                    for p in _pods.values())
                # Prefix-cache hit rate (hits/queries rate ratio) — the
                # dominant TTFT factor: the cache is keyed by (prefix,
                # adapter), so adapter traffic multiplies the key space.
                _q = (_prom.get('vllm_prefix_cache_queries_rate') or {}).get('avg') or 0
                _h = (_prom.get('vllm_prefix_cache_hits_rate') or {}).get('avg') or 0
                if _q:
                    cache_hit_pct = round(100.0 * _h / _q, 1)
            except Exception:
                pass

            self.log(f"  ✅ n={n_adapters}: TTFT p90={ttft:.0f}ms, "
                     f"Throughput={tput:.3f} req/s"
                     f"{f', resident={max_loras}x{sweep_pods_count}={total_slots} slots (LRU swapping, ~{evictions} evictions)' if swapping else ''}",
                     'success')
            results.append({
                'adapter_count': n_adapters,
                'gpu_resident': max_loras,
                'lru_swapping': swapping,
                'total_slots': total_slots or None,
                'adapter_evictions': evictions,
                'adapter_loads': adapter_loads,
                'cache_hit_pct': cache_hit_pct,
                'throughput_mean': round(tput, 3),
                'output_tps_mean': round(result.output_tps_mean or 0, 1),
                'ttft_p50': round(result.ttft_p50 or 0, 1),
                'ttft_p90': round(ttft, 1),
                'itl_p90': round(result.itl_p90 or 0, 1),
                'concurrency': int(concurrency),
                'gpus': gpus,
                'config_label': label,
                'test_id': test_id,
            })
            self._save_sweep_progress()

        if results:
            self.log("", 'info')
            self.log(f"📊 LoRA scaling curve ({label}): "
                     f"{results[0]['adapter_count']}→{results[-1]['adapter_count']} "
                     f"adapters — throughput "
                     f"{results[0]['throughput_mean']}→{results[-1]['throughput_mean']} "
                     f"req/s", 'decision')
