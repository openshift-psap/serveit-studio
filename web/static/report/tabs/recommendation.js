// tabs/recommendation.js — Recommendation report tab. One implementation for live + offline.
(function() {

    function statCard(val, lbl) {
        return '<div class="stat-card"><div class="val">' + val + '</div><div class="lbl">' + lbl + '</div></div>';
    }

    function build(data, ctx) {
        var rec = data.recommendation || {};
        var summary = data.summary || {};
        var best = summary.best_configs || {};
        var allRes = data.all_results || [];
        var runId = ctx.runId;
        var _sfx = '';
        if (ctx.mode === 'live' && typeof _chartSuffix !== 'undefined') _sfx = _chartSuffix;
        var s = '';


    // Lookup from test_id/config_name to all_results entry (used across multiple sections)
    const _allResLookup = {};
    (data.all_results || []).forEach(function(r) { _allResLookup[r.test_id || r.config_name] = r; });

    // Shared recommendation card builder
    function _buildRecCard(opts) {
        // opts: label, icon, desc, color, archKey, deploy, tput, gpus, conc,
        //       ttft_p90, ttft_p95, ttft_p99, itl_p90, itl_p95, itl_p99,
        //       recId, testId, manifests, runId, extraBadges (array of {text, bg, color}),
        //       extraInfo
        var archColors = { pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed' };
        var aColor = archColors[opts.archKey] || '#64748b';
        var archLabel = (opts.archKey || 'aggregated').toUpperCase();

        var h = '';
        h += '<div style="background:white;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);border:2px solid #cbd5e1;">';

        // Header
        h += '<div style="background:linear-gradient(135deg,' + opts.color + ',' + opts.color + 'cc);padding:10px 14px;color:white;">';
        h += '<div style="display:flex;align-items:center;justify-content:space-between;">';
        h += '<div>';
        h += '<div style="font-weight:700;font-size:0.85em;">' + (opts.icon || '') + ' ' + opts.label + '</div>';
        h += '<div style="display:flex;gap:4px;margin-top:2px;">';
        h += '<div style="font-size:0.65em;background:' + aColor + ';padding:2px 6px;border-radius:10px;font-weight:600;">' + archLabel + '</div>';
        if (opts.extraBadges) {
            opts.extraBadges.forEach(function(b) {
                h += '<div style="font-size:0.65em;background:' + (b.bg || 'rgba(255,255,255,0.2)') + ';color:' + (b.color || 'white') + ';padding:2px 6px;border-radius:10px;font-weight:600;">' + b.text + '</div>';
            });
        }
        h += '</div>';
        h += '</div></div>';
        if (opts.desc) h += '<div style="font-size:0.7em;opacity:0.9;margin-top:1px;">' + opts.desc + '</div>';
        h += '</div>';

        // Body
        h += '<div style="padding:12px 14px;">';
        h += '<div style="font-size:1.1em;font-weight:800;color:#0f172a;margin-bottom:8px;">' + (opts.deploy || '') + '</div>';

        // Metrics line
        h += '<div style="display:flex;gap:12px;font-size:0.85em;color:#475569;margin-bottom:4px;flex-wrap:wrap;">';
        if (opts.tput != null) h += '<span>Throughput: <strong>' + (typeof opts.tput === 'number' ? opts.tput.toFixed(2) + ' req/s' : opts.tput) + '</strong></span>';
        if (opts.gpus) h += '<span>GPUs: <strong>' + opts.gpus + '</strong></span>';
        if (opts.conc) h += '<span style="color:#0ea5e9;font-weight:600;">' + opts.conc + '</span>';
        h += '</div>';
        if (opts.vllm_tps != null) {
            h += '<div style="font-size:0.8em;color:#475569;margin-bottom:4px;">';
            h += '<span>vLLM: <strong style="color:#3b82f6;">' + Math.round(opts.vllm_tps).toLocaleString() + ' tok/s</strong> <span style="font-size:0.85em;color:#94a3b8;">(server total)</span></span>';
            h += '</div>';
        }

        // Extra info line (for EPP weights etc)
        if (opts.extraInfo) h += '<div style="font-size:0.8em;color:#7c3aed;margin-top:2px;">' + opts.extraInfo + '</div>';

        // Percentile table
        var _fmtE2e = function(v) { return v != null ? (v >= 1000 ? (v/1000).toFixed(1) + ' s' : Math.round(v) + ' ms') : '-'; };
        h += '<table style="width:100%;font-size:0.8em;border-collapse:collapse;margin-top:6px;">';
        h += '<tr style="color:#94a3b8;font-weight:600;"><td></td><td>TTFT</td><td>E2E</td><td>ITL</td></tr>';
        if (opts.ttft_p50 != null) h += '<tr><td style="font-weight:600;color:#94a3b8;">P50</td><td style="color:#64748b;">' + Math.round(opts.ttft_p50).toLocaleString() + ' ms</td><td style="color:#64748b;">' + _fmtE2e(opts.e2e_p50) + '</td><td style="color:#64748b;">' + (opts.itl_p50 ? parseFloat(opts.itl_p50).toFixed(2) + ' ms' : '-') + '</td></tr>';
        h += '<tr><td style="font-weight:600;color:#475569;">P90</td><td style="font-weight:700;color:#1e293b;">' + (opts.ttft_p90 != null ? Math.round(opts.ttft_p90).toLocaleString() + ' ms' : '-') + '</td><td>' + _fmtE2e(opts.e2e_p90) + '</td><td>' + (opts.itl_p90 ? parseFloat(opts.itl_p90).toFixed(2) + ' ms' : '-') + '</td></tr>';
        if (opts.ttft_p95) h += '<tr><td style="font-weight:600;color:#475569;">P95</td><td style="color:#64748b;">' + Math.round(opts.ttft_p95).toLocaleString() + ' ms</td><td style="color:#64748b;">' + _fmtE2e(opts.e2e_p95) + '</td><td>' + (opts.itl_p95 ? parseFloat(opts.itl_p95).toFixed(2) + ' ms' : '-') + '</td></tr>';
        if (opts.ttft_p99) h += '<tr><td style="font-weight:600;color:#475569;">P99</td><td style="color:#64748b;">' + Math.round(opts.ttft_p99).toLocaleString() + ' ms</td><td style="color:#64748b;">' + _fmtE2e(opts.e2e_p99) + '</td><td>' + (opts.itl_p99 ? parseFloat(opts.itl_p99).toFixed(2) + ' ms' : '-') + '</td></tr>';
        h += '</table>';

        // Action buttons
        if (opts.recId || (opts.manifests && opts.manifests.length)) {
            h += '<div style="margin-top:10px;padding-top:8px;border-top:1px solid #f1f5f9;">';
            if (opts.recId) {
                h += '<button onclick="applyReportConfig(\'' + opts.recId + '\')" style="width:100%;background:' + opts.color + '0a;border:1.5px solid ' + opts.color + '40;border-radius:6px;padding:5px 8px;font-size:11px;font-weight:600;color:' + opts.color + ';cursor:pointer;margin-bottom:4px;" onmouseover="this.style.background=\'' + opts.color + '15\'" onmouseout="this.style.background=\'' + opts.color + '0a\'">&#128260; Reuse</button>';
                h += '<button onclick="showSingleTestModal(\'' + opts.recId + '\')" style="width:100%;background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:6px;padding:5px 8px;font-size:11px;font-weight:600;color:#475569;cursor:pointer;margin-bottom:4px;" onmouseover="this.style.background=\'#f1f5f9\'" onmouseout="this.style.background=\'#f8fafc\'">&#129514; Test</button>';
            }
            if (opts.manifests && opts.manifests.length && opts.runId && opts.testId) {
                var _ml = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
                h += '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px;">';
                opts.manifests.forEach(function(t) {
                    if (t.includes('service')) return;
                    h += '<a href="/api/run/' + opts.runId + '/config/' + opts.testId + '/manifest/' + t + '" target="_blank" style="color:#0ea5e9;font-size:10px;padding:2px 6px;background:#f0f9ff;border-radius:4px;border:1px solid #bae6fd;font-weight:500;text-decoration:none;">' + (_ml[t] || t) + ' &#8595;</a>';
                });
                h += '</div>';
            }
            h += '</div>';
        }

        h += '</div></div>';
        return h;
    }

    function _tagLabel(name, arch) {
        if (!name || name.indexOf('(') >= 0) return name || '';
        var a = (arch || '').toUpperCase();
        if (a === 'PD') return name + ' (PD)';
        if (a === 'EP') return name + ' (EP)';
        return name + ' (AG)';
    }

    // Filter out calibration, sweep tests, and discarded tests from configuration charts
    var coreResults = (data.all_results || []).filter(function(r) {
        if (r.quality === 'discard') return false;
        var tid = r.test_id || r.config_name || '';
        return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
               tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
    });

    // Build a lookup from test_id -> manifest_types for download links
    const manifestLookup = {};
    const testIdLookup = {};
    (data.all_results || []).forEach(r => {
        const tid = r.test_id || r.config_name;
        if (r.manifest_types && r.manifest_types.length) manifestLookup[tid] = r.manifest_types;
        testIdLookup[r.config_name] = tid;
    });

    // ============================================================
    // GOAL BANNER — what was this run optimizing for
    // ============================================================
    if (rec && rec.goal_info) {
        const gColors = { ttft: '#3b82f6', throughput: '#f59e0b', full_coverage: '#10b981', balanced: '#10b981', aggregated_only: '#64748b', pd_only: '#8b5cf6', ep_only: '#0ea5e9' };
        const gIcons = { ttft: '&#9201;', throughput: '&#9889;', full_coverage: '&#9878;', balanced: '&#9878;', aggregated_only: '&#9634;', pd_only: '&#8644;', ep_only: '&#9881;' };
        const gc = gColors[rec.goal] || '#10b981';
        s += `<div class="chart-card" style="border: 3px solid ${gc}; border-left: 8px solid ${gc};">`;
        s += `<div class="chart-card-header" style="background: ${gc}; color: white; font-size: 1.3em;">`;
        s += `Deployment Recommendation <span style="font-size:0.7em;font-weight:400;opacity:0.85;">&mdash; ${rec.goal_info.name}</span></div>`;
        s += `<div style="background:${gc}dd; color:white; padding:8px 20px; font-size:0.92em; display:flex; flex-wrap:wrap; gap:6px 20px;">`;
        s += `<span>Model: <strong>${rec.model}</strong></span>`;
        var bCpt = rec.workload.chars_per_token || 4.5;
        var bIslC = rec.workload.isl_original_chars || Math.round(rec.workload.isl * bCpt);
        var bOslC = rec.workload.osl_original_chars || Math.round(rec.workload.osl * bCpt);
        var bIslStdC = rec.workload.isl_stdev ? (rec.workload.isl_stdev_original_chars || Math.round(rec.workload.isl_stdev * bCpt)) : null;
        let wlLabel = `Prompt: <strong>${rec.workload.isl.toLocaleString()}</strong>`;
        if (rec.workload.isl_stdev) wlLabel += ` + σ=${rec.workload.isl_stdev.toLocaleString()}`;
        wlLabel += ` <span style="opacity:0.8;">(${bIslC.toLocaleString()} chars)</span>`;
        wlLabel += ` | Output: <strong>${rec.workload.osl.toLocaleString()}</strong>`;
        if (rec.workload.osl_stdev) wlLabel += ` + σ=${rec.workload.osl_stdev.toLocaleString()}`;
        wlLabel += ` <span style="opacity:0.8;">(${bOslC.toLocaleString()} chars)</span>`;
        var _rc = data.run_config || {};
        var _turns = rec.workload.turns > 1 ? rec.workload.turns : (_rc.turns > 1 ? _rc.turns : null);
        if (_turns) wlLabel += ` | Turns: <strong>${_turns}</strong>`;
        if (_rc.first_prompt_tokens) wlLabel += ` | 1st Prompt: <strong>${_rc.first_prompt_tokens.toLocaleString()}</strong>` + (_rc.first_prompt_tokens_stdev ? ` σ=${_rc.first_prompt_tokens_stdev.toLocaleString()}` : '');
        if (_rc.prefix_tokens) wlLabel += ` | Prefix: <strong>${_rc.prefix_tokens.toLocaleString()}</strong>×${_rc.prefix_count || 1}`;
        if (_turns && _rc.turn_delay) wlLabel += ` | Delay: <strong>${_rc.turn_delay}s</strong>` + (_rc.turn_delay_stdev ? ` σ=${_rc.turn_delay_stdev}s` : '');
        s += `<span>${wlLabel}</span>`;
        s += `<span>Users: <strong>${rec.workload.users}</strong></span>`;
        s += `<span>Tests: <strong>${rec.total_tests}</strong></span>`;
        if (rec.total_duration) s += `<span>Duration: <strong>${rec.total_duration}</strong></span>`;
        s += '</div>';
        s += '<div class="chart-card-body" style="padding: 20px;">';
        s += `<p style="color:#334155; margin:0; font-size:0.95em; line-height:1.6;">${rec.goal_info.description}</p>`;
        s += '</div></div>';
    }

    // ============================================================
    // Store recommendation configs for single test re-run
    window._recConfigs = {};

    // RECOMMENDATION — the bottom line
    // ============================================================
    if (rec && rec.recommendations && Object.keys(rec.recommendations).length) {
        s += '<div class="chart-card" style="border: 2px solid #10b981; border-left: 6px solid #10b981;">';
        s += '<div class="chart-card-header" style="background: linear-gradient(135deg, #059669, #10b981); color: white; font-size: 1.1em; font-weight: 800;">Deployment Recommendation &mdash; User-Defined Workload</div>';
        s += '<div style="padding:12px 20px 4px; color:#475569; font-size:0.9em;">Best configurations found at the user-configured concurrency. These results reflect peak-load performance at the workload settings you defined.</div>';
        s += '<div class="chart-card-body" style="padding: 24px;">';

        // Recommendation cards — one per category, best across ALL architectures
        const bp = rec.best_by_percentile || {};
        const selTypes = [
            { key: 'balanced', label: 'Best Balanced', desc: 'Best TTFT-to-throughput ratio — the sweet spot', color: '#059669', icon: '&#9878;' },
            { key: 'lowest_ttft', label: 'Lowest TTFT', desc: 'Fastest time to first token', color: '#3b82f6', icon: '&#9201;' },
            { key: 'lowest_itl', label: 'Lowest ITL', desc: 'Smoothest token streaming — lowest inter-token latency', color: '#0891b2', icon: '&#9881;' },
            { key: 'highest_tput', label: 'Highest Throughput', desc: 'Maximum requests per second', color: '#f59e0b', icon: '&#9889;' },
            { key: 'most_efficient', label: 'Most Efficient', desc: 'Best throughput per GPU — cost optimized', color: '#8b5cf6', icon: '&#128176;' },
        ];
        const fallbackTs = (rec.recommendations.response_time || rec.recommendations.throughput || {}).config?.test_settings;
        const archColors = { pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed' };

        // Find global best per category across all architectures
        const globalBest = {};
        selTypes.forEach(sel => {
            let best = null, bestArch = null;
            ['pd', 'aggregated', 'ep'].forEach(archKey => {
                const p90Data = (bp.p90 || {})[archKey];
                if (!p90Data) return;
                const isNew = p90Data.balanced || p90Data.lowest_ttft || p90Data.highest_tput;
                const cfg = isNew ? p90Data[sel.key] : (sel.key === 'balanced' ? p90Data : null);
                if (!cfg) return;
                const ttft = cfg.ttft_p90 || cfg.ttft || 1e9;
                const tput = cfg.throughput_mean || cfg.throughput || cfg.throughput_p90 || 0;
                const gpus = cfg.gpus || cfg.total_gpus || 1;
                let score;
                if (sel.key === 'lowest_ttft') score = -ttft;
                else if (sel.key === 'highest_tput') score = tput;
                else if (sel.key === 'most_efficient') score = tput / gpus;
                else score = -ttft / Math.max(tput, 0.001);
                if (!best || score > best.score) {
                    best = { cfg, score, archKey };
                    bestArch = archKey;
                }
            });
            if (best) globalBest[sel.key] = best;
        });

        s += `<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px; margin-bottom:20px;">`;

        // _allResLookup already built at outer scope

        const seen = new Set();
        selTypes.forEach(sel => {
            const entry = globalBest[sel.key];
            if (!entry) return;
            const cfg = entry.cfg;
            const archKey = entry.archKey;

            const testId = cfg.test_id || cfg.config_name || '';
            let dupNote = '';
            if (seen.has(testId)) {
                const otherLabels = selTypes.filter(s => s.key !== sel.key && globalBest[s.key] && (globalBest[s.key].cfg.test_id || globalBest[s.key].cfg.config_name) === testId).map(s => s.label);
                if (otherLabels.length) dupNote = otherLabels.join(', ');
                else return;
            }
            seen.add(testId);

                let deploy;
                if (cfg.prefill_pods && cfg.decode_pods) {
                    deploy = cfg.prefill_tp === cfg.decode_tp
                        ? `${cfg.prefill_pods}P+${cfg.decode_pods}D TP=${cfg.prefill_tp || cfg.tp || '?'}`
                        : `${cfg.prefill_pods}P+${cfg.decode_pods}D PTP=${cfg.prefill_tp || cfg.tp || '?'} DTP=${cfg.decode_tp || '?'}`;
                } else {
                    deploy = cfg.config_name;
                }

                const recId = 'rec-' + archKey + '-' + sel.key;
                window._recConfigs[recId] = { ...cfg, architecture: archKey, model: rec.model, image: (data.run_config || {}).image, test_settings: cfg.test_settings || fallbackTs, epp_config: cfg.epp_config };

                const gpus = cfg.gpus || cfg.total_gpus;
                const tputMean = cfg.throughput_mean || cfg.throughput || cfg.throughput_p90 || '-';

                // Extract percentile data from best_by_percentile
                const _p90d = ((bp.p90 || {})[archKey] || {})[sel.key] || (bp.p90 || {})[archKey] || {};
                const _p95d = ((bp.p95 || {})[archKey] || {})[sel.key] || (bp.p95 || {})[archKey] || {};
                const _p99d = ((bp.p99 || {})[archKey] || {})[sel.key] || (bp.p99 || {})[archKey] || {};

                const recTestId = cfg.test_id || testIdLookup[cfg.config_name] || cfg.config_name;
                const recManifests = manifestLookup[recTestId] || cfg.manifest_types || [];

                const badges = [];
                if (dupNote) badges.push({ text: '+ ' + dupNote, bg: 'rgba(255,255,255,0.2)', color: 'white' });

                // Extract vLLM metrics from all_results
                const _resEntry = _allResLookup[recTestId] || {};
                const _mj = _resEntry.metrics_json ? (typeof _resEntry.metrics_json === 'string' ? JSON.parse(_resEntry.metrics_json) : _resEntry.metrics_json) : {};
                const _pm = _mj.prometheus_metrics || {};
                const _vllmTps = ((_pm.vllm_prompt_tokens_rate || {}).avg || 0) + ((_pm.vllm_generation_tokens_rate || {}).avg || 0);
                const _outputTps = _mj.output_tps_mean || _resEntry.output_tps_mean || null;
                if (_resEntry.cache_hit_pct != null) badges.push({ text: _resEntry.cache_hit_pct + '% cache hit', bg: 'rgba(255,255,255,0.2)', color: 'white' });

                // P50 data — not in best_by_percentile, pull from raw metrics_json
                const _p50mj = _mj;
                const _p50d = { ttft_p50: _p50mj.ttft_p50, e2e_p50: _p50mj.e2e_latency_p50 != null ? _p50mj.e2e_latency_p50 * 1000 : null, itl_p50: _p50mj.itl_p50 };

                const userConc = rec.workload ? 'c=' + rec.workload.users : '';
                s += _buildRecCard({
                    label: sel.label, icon: sel.icon, desc: sel.desc, color: sel.color,
                    archKey: archKey, deploy: deploy,
                    tput: tputMean, gpus: gpus || '?', conc: userConc,
                    vllm_tps: _vllmTps > 0 ? _vllmTps : null,
                    ttft_p50: _p50d.ttft || _p50d.ttft_p50,
                    ttft_p90: _p90d.ttft || _p90d.ttft_p90,
                    ttft_p95: _p95d.ttft || _p95d.ttft_p95,
                    ttft_p99: _p99d.ttft || _p99d.ttft_p99,
                    e2e_p50: _p50d.e2e_p50, e2e_p90: _p90d.e2e_p90, e2e_p95: _p95d.e2e_p95, e2e_p99: _p99d.e2e_p99,
                    itl_p50: _p50d.itl || _p50d.itl_p50,
                    itl_p90: _p90d.itl || _p90d.itl_p90,
                    itl_p95: _p95d.itl || _p95d.itl_p95,
                    itl_p99: _p99d.itl || _p99d.itl_p99,
                    recId: recId, testId: recTestId,
                    manifests: recManifests, runId: runId,
                    extraBadges: badges.length ? badges : null
                });
            });

            s += '</div>';

        // Optimal TP values and test counts (outside the grid)
        if (rec.optimal_decode_tp || rec.optimal_prefill_tp || rec.pd_tests_count || rec.ep_tests_count) {
            s += '<div style="background:#f8fafc; border-radius:8px; padding:14px 18px; display:flex; gap:32px; flex-wrap:wrap; font-size:0.9em; margin-top:12px;">';
            if (rec.optimal_decode_tp)
                s += `<div><strong>Optimal Decode TP:</strong> ${rec.optimal_decode_tp.tp} <span style="color:#64748b">(${rec.optimal_decode_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.optimal_prefill_tp)
                s += `<div><strong>Optimal Prefill TP:</strong> ${rec.optimal_prefill_tp.tp} <span style="color:#64748b">(${rec.optimal_prefill_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.pd_tests_count)
                s += `<div><strong>PD Splits Tested:</strong> ${rec.pd_tests_count}</div>`;
            if (rec.ep_tests_count)
                s += `<div><strong>EP Configs Tested:</strong> ${rec.ep_tests_count}</div>`;
            s += '</div>';
        }

        // Constraint notes (filter out restart messages — those aren't real constraints)
        if (rec.constraint_notes && rec.constraint_notes.length) {
            var realConstraints = rec.constraint_notes.filter(function(n) { return n.indexOf('Server restarted') === -1; });
            if (realConstraints.length) {
                s += '<div style="background:#fffbeb; border:2px solid #f59e0b; border-left:6px solid #f59e0b; border-radius:8px; padding:14px 18px; margin-top:12px;">';
                s += '<div style="font-weight:700; color:#92400e; margin-bottom:8px; font-size:0.95em;">&#9888; Configuration Constraints</div>';
                for (const note of realConstraints) {
                    s += `<p style="color:#78350f; margin:0 0 8px; font-size:0.88em; line-height:1.6;">${note}</p>`;
                }
                s += '</div>';
            }
        }

        s += '</div></div>';

    } // end Deployment Recommendation card

    // --- Calibrated Recommendations (Step 11) — separate card ---
    const calBest = data.summary && data.summary.calibrated_best;
    if (calBest) {
        s += '<div class="chart-card" style="margin-top:20px; border:2px solid #0ea5e9; border-left:6px solid #0ea5e9;">';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#0ea5e9,#06b6d4); color:white; font-size:1.1em; font-weight:800;">Deployment Recommendation &mdash; Calibrated Load</div>';
        s += '<div style="padding:12px 20px 4px; color:#475569; font-size:0.9em;">Performance at sustainable production load — calibrated concurrency where queue wait is reasonable. These results reflect realistic production conditions.</div>';
        s += `<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px; padding:16px 20px;">`;

        const calTypes = [
            { key: 'balanced', label: 'Best Balanced', desc: 'Best TTFT-to-throughput ratio at calibrated load', color: '#059669', icon: '&#9878;' },
            { key: 'lowest_ttft', label: 'Lowest TTFT', desc: 'Fastest first token at calibrated load', color: '#3b82f6', icon: '&#9201;' },
            { key: 'lowest_itl', label: 'Lowest ITL', desc: 'Smoothest streaming at calibrated load', color: '#0891b2', icon: '&#9881;' },
            { key: 'highest_tput', label: 'Highest Throughput', desc: 'Maximum req/s at calibrated load', color: '#f59e0b', icon: '&#9889;' },
            { key: 'most_efficient', label: 'Most Efficient', desc: 'Best throughput per GPU at calibrated load', color: '#8b5cf6', icon: '&#128176;' },
        ];
        const calSeen = new Set();
        calTypes.forEach(sel => {
            const cfg = calBest[sel.key];
            if (!cfg) return;

            const calId = cfg.test_id || cfg.config_name || '';
            let dupNote = '';
            if (calSeen.has(calId)) {
                const otherLabels = calTypes.filter(s => s.key !== sel.key && calBest[s.key] && (calBest[s.key].test_id || calBest[s.key].config_name) === calId).map(s => s.label);
                if (otherLabels.length) dupNote = otherLabels.join(', ');
                else return;
            }
            calSeen.add(calId);

            const archKey = cfg.architecture || 'aggregated';

            let deploy;
            if (cfg.prefill_pods && cfg.decode_pods) {
                deploy = `${cfg.prefill_pods}P×TP${cfg.prefill_tp || cfg.tp || '?'} + ${cfg.decode_pods}D×TP${cfg.decode_tp || cfg.tp || '?'}`;
            } else {
                deploy = cfg.name || cfg.config_name;
            }

            const concStr = cfg.concurrency ? `c=${cfg.concurrency}` : '';
            const tput = cfg.throughput_mean || cfg.throughput_p90 || '-';

            const calRecId = 'cal-' + archKey + '-' + sel.key;
            window._recConfigs[calRecId] = { ...cfg, architecture: archKey, model: rec ? rec.model : '', image: (data.run_config || {}).image, test_settings: cfg.test_settings };

            const calTestId = cfg.test_id || testIdLookup[cfg.config_name] || cfg.config_name;
            const calManifests = manifestLookup[calTestId] || cfg.manifest_types || [];

            const badges = [];
            if (dupNote) badges.push({ text: '+ ' + dupNote, bg: 'rgba(255,255,255,0.2)', color: 'white' });

            // Extract vLLM metrics
            const _calResEntry = _allResLookup[calTestId] || {};
            const _calMj = _calResEntry.metrics_json ? (typeof _calResEntry.metrics_json === 'string' ? JSON.parse(_calResEntry.metrics_json) : _calResEntry.metrics_json) : {};
            const _calPm = _calMj.prometheus_metrics || {};
            const _calVllmTps = ((_calPm.vllm_prompt_tokens_rate || {}).avg || 0) + ((_calPm.vllm_generation_tokens_rate || {}).avg || 0);
            const _calOutputTps = _calMj.output_tps_mean || _calResEntry.output_tps_mean || null;
            if (_calResEntry.cache_hit_pct != null) badges.push({ text: _calResEntry.cache_hit_pct + '% cache hit', bg: 'rgba(255,255,255,0.2)', color: 'white' });

            s += _buildRecCard({
                label: sel.label, icon: sel.icon, desc: sel.desc, color: sel.color,
                archKey: archKey, deploy: deploy,
                tput: tput, gpus: cfg.gpus || '?',
                conc: concStr,
                vllm_tps: _calVllmTps > 0 ? _calVllmTps : null,
                ttft_p50: cfg.ttft_p50 || _calMj.ttft_p50, ttft_p90: cfg.ttft_p90, ttft_p95: cfg.ttft_p95, ttft_p99: cfg.ttft_p99,
                e2e_p50: cfg.e2e_p50 || (_calMj.e2e_latency_p50 != null ? _calMj.e2e_latency_p50 * 1000 : null), e2e_p90: cfg.e2e_p90, e2e_p95: cfg.e2e_p95, e2e_p99: cfg.e2e_p99,
                itl_p50: cfg.itl_p50 || _calMj.itl_p50, itl_p90: cfg.itl_p90, itl_p95: cfg.itl_p95, itl_p99: cfg.itl_p99,
                recId: calRecId, testId: calTestId,
                manifests: calManifests, runId: runId,
                extraBadges: badges.length ? badges : null
            });
        });

        s += '</div></div>';
    }

    // Cache Sweep Recommendation
    const cacheBest = data.summary && data.summary.cache_sweep_best;
    if (cacheBest) {
        s += '<div class="chart-card" style="margin-top:20px; border:2px solid #8b5cf6; border-left:6px solid #8b5cf6;">';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6); color:white; font-size:1.1em; font-weight:800;">Deployment Recommendation &mdash; Cache Hit Sweep</div>';
        s += '<div style="padding:12px 20px 4px; color:#475569; font-size:0.9em;">Performance with varying prefix cache hit rates — showing how cache efficiency affects throughput and latency at calibrated load.</div>';
        s += `<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px; padding:16px 20px;">`;

        const cacheTypes = [
            { key: 'balanced', label: 'Best Balanced', desc: 'Best TTFT-to-throughput ratio with cache', color: '#059669', icon: '&#9878;' },
            { key: 'lowest_ttft', label: 'Lowest TTFT', desc: 'Fastest first token with cache', color: '#3b82f6', icon: '&#9201;' },
            { key: 'lowest_itl', label: 'Lowest ITL', desc: 'Smoothest streaming with cache', color: '#0891b2', icon: '&#9881;' },
            { key: 'highest_tput', label: 'Highest Throughput', desc: 'Maximum req/s with cache', color: '#f59e0b', icon: '&#9889;' },
            { key: 'most_efficient', label: 'Most Efficient', desc: 'Best throughput per GPU with cache', color: '#8b5cf6', icon: '&#128176;' },
        ];
        const cacheSeen = new Set();
        cacheTypes.forEach(sel => {
            const cfg = cacheBest[sel.key];
            if (!cfg) return;

            const cacheId = cfg.test_id || cfg.config_name || '';
            let dupNote = '';
            if (cacheSeen.has(cacheId)) {
                const otherLabels = cacheTypes.filter(s => s.key !== sel.key && cacheBest[s.key] && (cacheBest[s.key].test_id || cacheBest[s.key].config_name) === cacheId).map(s => s.label);
                if (otherLabels.length) dupNote = otherLabels.join(', ');
                else return;
            }
            cacheSeen.add(cacheId);

            const archKey = cfg.architecture || 'aggregated';
            let deploy;
            if (cfg.prefill_pods && cfg.decode_pods) {
                deploy = `${cfg.prefill_pods}P×TP${cfg.prefill_tp || cfg.tp || '?'} + ${cfg.decode_pods}D×TP${cfg.decode_tp || cfg.tp || '?'}`;
            } else {
                deploy = cfg.name || cfg.config_name;
            }

            const concStr = cfg.concurrency ? `c=${cfg.concurrency}` : '';
            const tput = cfg.throughput_mean || cfg.throughput_p90 || '-';
            const cacheStr = cfg.cache_hit_pct != null ? cfg.cache_hit_pct + '% cache hit' : '';

            const cacheRecId = 'cache-' + archKey + '-' + sel.key;
            window._recConfigs[cacheRecId] = { ...cfg, architecture: archKey, model: rec ? rec.model : '', image: (data.run_config || {}).image };

            const cacheTestId = cfg.test_id || cfg.config_name;
            const cacheManifests = manifestLookup[cacheTestId] || [];

            const badges = [];
            if (dupNote) badges.push({ text: '+ ' + dupNote, bg: 'rgba(255,255,255,0.2)', color: 'white' });
            if (cacheStr) badges.push({ text: cacheStr, bg: 'rgba(255,255,255,0.2)', color: 'white' });

            const _cacheResEntry = _allResLookup[cacheTestId] || {};
            const _cacheMj = _cacheResEntry.metrics_json ? (typeof _cacheResEntry.metrics_json === 'string' ? JSON.parse(_cacheResEntry.metrics_json) : _cacheResEntry.metrics_json) : {};
            const _cachePm = _cacheMj.prometheus_metrics || {};
            const _cacheVllmTps = ((_cachePm.vllm_prompt_tokens_rate || {}).avg || 0) + ((_cachePm.vllm_generation_tokens_rate || {}).avg || 0);

            s += _buildRecCard({
                label: sel.label, icon: sel.icon, desc: sel.desc, color: sel.color,
                archKey: archKey, deploy: deploy,
                tput: tput, gpus: cfg.gpus || '?',
                conc: concStr,
                vllm_tps: cfg.vllm_tps || (_cacheVllmTps > 0 ? _cacheVllmTps : null),
                ttft_p50: cfg.ttft_p50, ttft_p90: cfg.ttft_p90, ttft_p95: cfg.ttft_p95, ttft_p99: cfg.ttft_p99,
                e2e_p50: cfg.e2e_p50, e2e_p90: cfg.e2e_p90, e2e_p95: cfg.e2e_p95, e2e_p99: cfg.e2e_p99,
                itl_p50: cfg.itl_p50, itl_p90: cfg.itl_p90, itl_p95: cfg.itl_p95, itl_p99: cfg.itl_p99,
                recId: cacheRecId, testId: cacheTestId,
                manifests: cacheManifests, runId: runId,
                extraBadges: badges.length ? badges : null
            });
        });

        s += '</div></div>';
    }


    // ============================================================

    // ============================================================
    // PERCENTILE BREAKDOWN — combined primary vs Aggregated table
    // ============================================================
    if (rec && rec.recommendations) {
        const primaryKey = rec.goal === 'ttft' ? 'response_time' : 'throughput';
        const primaryRec = rec.recommendations[primaryKey];
        const aggBase = rec.aggregated_baseline;
        const primaryArch = primaryRec && primaryRec.architecture ? primaryRec.architecture : 'PD';

        // Build a combined table when we have both primary and Aggregated percentiles
        const hasPrimary = primaryRec && primaryRec.config.percentiles;
        const hasAgg = aggBase && aggBase.percentiles && (!primaryRec || aggBase.config_name !== primaryRec.config.config_name);

        if (hasPrimary) {
            const p = primaryRec.config.percentiles;
            const a = hasAgg ? aggBase.percentiles : null;

            s += `<div class="chart-card"><div class="chart-card-header">Percentile Breakdown: ${primaryArch} vs Aggregated</div>`;
            s += `<div style="padding:10px 20px 4px; color:#4b5563; font-size:0.9em;">Full percentile distribution (P50 through P99) for TTFT, ITL, and Throughput — comparing the best ${primaryArch} configuration against the Aggregated baseline using the same GPU budget. Green-highlighted P90 values indicate the winner for each metric.</div>`;
            s += '<div class="chart-card-body" style="padding:0;">';
            s += '<table class="results-table">';

            s += '<tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';

            const betterLower = (v1, v2) => v1 != null && v2 != null && v1 < v2;
            const betterHigher = (v1, v2) => v1 != null && v2 != null && v1 > v2;

            const metricDefs = [
                { name: 'TTFT (ms)', key: 'ttft', lowerBetter: true },
                { name: 'ITL (ms)', key: 'itl', lowerBetter: true },
                { name: 'Throughput (req/s)', key: 'throughput', lowerBetter: false },
            ];

            const entries = [{label: primaryArch, pctl: p}];
            if (a) entries.push({label: 'Aggregated', pctl: a});

            entries.forEach(({label, pctl}, idx) => {
                metricDefs.forEach((m, mi) => {
                    const d = pctl[m.key];
                    const borderStyle = mi === 0 && idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
                    let p90Style = '';
                    if (a) {
                        const other = idx === 0 ? a[m.key] : p[m.key];
                        const wins = m.lowerBetter ? betterLower(d.p90, other.p90) : betterHigher(d.p90, other.p90);
                        if (wins) p90Style = 'color:#10b981; font-weight:700;';
                    }
                    s += '<tr>';
                    if (mi === 0) s += `<td rowspan="3" style="vertical-align:middle; font-weight:700;${borderStyle}">${label}</td>`;
                    s += `<td style="color:#64748b;${borderStyle}">${m.name}</td>`;
                    s += `<td style="${borderStyle}">${d.p50 ?? '-'}</td>`;
                    s += `<td style="${p90Style}${borderStyle}">${d.p90 ?? '-'}</td>`;
                    s += `<td style="${borderStyle}">${d.p95 ?? '-'}</td>`;
                    s += `<td style="${borderStyle}">${d.p99 ?? '-'}</td>`;
                    s += '</tr>';
                });
            });

            s += '</table></div></div>';
        }
    }

    // --- Summary cards ---
    s += '<div class="charts-summary">';
    s += statCard(summary.successful_tests, 'Successful Tests');
    if (summary.discarded_tests > 0) {
        s += statCard(summary.discarded_tests, 'Discarded Tests', 'not viable');
    }
    if (best.lowest_latency) {
        const ll = best.lowest_latency;
        s += statCard(ll.ttft_p90.toFixed(1) + ' ms', 'Best TTFT P90', ll.name);
        if (ll.ttft_p95) s += statCard(ll.ttft_p95.toFixed(1) + ' ms', 'Best TTFT P95', ll.name);
        if (ll.ttft_p99) s += statCard(ll.ttft_p99.toFixed(1) + ' ms', 'Best TTFT P99', ll.name);
    }
    if (best.lowest_itl) {
        s += statCard(best.lowest_itl.itl_p90.toFixed(2) + ' ms', 'Best ITL P90', best.lowest_itl.name);
    }
    if (best.highest_throughput) {
        const ht = best.highest_throughput;
        const htVal = ht.throughput_mean || ht.throughput_p90;
        s += statCard(htVal.toFixed(2) + ' req/s', 'Best Throughput Mean', ht.name);
    }
    if (best.most_efficient)
        s += statCard(best.most_efficient.efficiency.toFixed(3), 'Best Efficiency (req/s/GPU)', best.most_efficient.name);
    s += '</div>';


        return s;
    }
    ReportTabs.register({
        id: 'recommendation', label: 'Recommendation', icon: '&#9733;',
        position: 'first',
        build: build
    });
})();
