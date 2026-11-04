// charts.js — Plotly chart rendering for all report visualizations

// Sort EPP table by column index (num = numeric, str = string)
window._sortEppTable = function(th, colIdx, type) {
    const table = th.closest('table');
    const tbody = table;
    const rows = [...table.querySelectorAll('tr')].slice(1); // skip header
    if (!rows.length) return;
    // toggle direction (store on the table element)
    if (table._sortCol === colIdx) table._sortDir = -table._sortDir;
    else { table._sortCol = colIdx; table._sortDir = 1; }
    const dir = table._sortDir;
    rows.sort((a, b) => {
        const ca = a.children[colIdx], cb = b.children[colIdx];
        let va, vb;
        if (type === 'num') {
            va = parseFloat(ca.getAttribute('sort-num') || ca.textContent.replace(/[^0-9.\-]/g, '')) || 0;
            vb = parseFloat(cb.getAttribute('sort-num') || cb.textContent.replace(/[^0-9.\-]/g, '')) || 0;
        } else {
            va = ca.textContent.toLowerCase();
            vb = cb.textContent.toLowerCase();
        }
        if (va < vb) return -1 * dir;
        if (va > vb) return 1 * dir;
        return 0;
    });
    // re-append sorted rows
    rows.forEach(r => table.appendChild(r));
    // visual indicator
    table.querySelectorAll('th').forEach(h => h.style.color = '');
    th.style.color = dir === 1 ? '#059669' : '#dc2626';
};

function renderCharts(data, runId) {
    var content = document.getElementById('charts-content');
    try { _renderChartsImpl(data, runId, content); } catch(e) {
        console.error('renderCharts error:', e);
        if (content) content.innerHTML = '<div class="charts-loading" style="color:red;">Chart render error: ' + e.message + '<br><pre style="white-space:pre-wrap;font-size:11px;">' + e.stack + '</pre></div>';
    }
}
function _renderChartsImpl(data, runId, content) {

    const summary = data.summary;
    const chartQueue = [];
    const charts = data.charts;
    const rec = data.recommendation;

    // Download link handled by tab management
    const dlLink = document.getElementById('chart-download-link');
    if (dlLink) {
        dlLink.style.display = 'inline';
        dlLink.href = '#';
        dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(runId, data); };
    }

    let html = '';
    let secRec = '', secTP = '', secCfg = '', secCmp = '', secStep9 = '', secCal = '', secCacheSweep = '';


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
        html += `<div class="chart-card" style="border: 3px solid ${gc}; border-left: 8px solid ${gc};">`;
        html += `<div class="chart-card-header" style="background: ${gc}; color: white; font-size: 1.3em;">`;
        html += `Deployment Recommendation <span style="font-size:0.7em;font-weight:400;opacity:0.85;">&mdash; ${rec.goal_info.name}</span></div>`;
        html += `<div style="background:${gc}dd; color:white; padding:8px 20px; font-size:0.92em; display:flex; flex-wrap:wrap; gap:6px 20px;">`;
        html += `<span>Model: <strong>${rec.model}</strong></span>`;
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
        html += `<span>${wlLabel}</span>`;
        html += `<span>Users: <strong>${rec.workload.users}</strong></span>`;
        html += `<span>Tests: <strong>${rec.total_tests}</strong></span>`;
        if (rec.total_duration) html += `<span>Duration: <strong>${rec.total_duration}</strong></span>`;
        html += '</div>';
        html += '<div class="chart-card-body" style="padding: 20px;">';
        html += `<p style="color:#334155; margin:0; font-size:0.95em; line-height:1.6;">${rec.goal_info.description}</p>`;
        html += '</div></div>';
    }

    // ============================================================
    // Store recommendation configs for single test re-run
    window._recConfigs = {};

    // RECOMMENDATION — the bottom line
    // ============================================================
    if (rec && rec.recommendations && Object.keys(rec.recommendations).length) {
        html += '<div class="chart-card" style="border: 2px solid #10b981; border-left: 6px solid #10b981;">';
        html += '<div class="chart-card-header" style="background: linear-gradient(135deg, #059669, #10b981); color: white; font-size: 1.1em; font-weight: 800;">Deployment Recommendation &mdash; User-Defined Workload</div>';
        html += '<div style="padding:12px 20px 4px; color:#475569; font-size:0.9em;">Best configurations found at the user-configured concurrency. These results reflect peak-load performance at the workload settings you defined.</div>';
        html += '<div class="chart-card-body" style="padding: 24px;">';

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

        html += `<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px; margin-bottom:20px;">`;

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
                html += _buildRecCard({
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

            html += '</div>';

        // Optimal TP values and test counts (outside the grid)
        if (rec.optimal_decode_tp || rec.optimal_prefill_tp || rec.pd_tests_count || rec.ep_tests_count) {
            html += '<div style="background:#f8fafc; border-radius:8px; padding:14px 18px; display:flex; gap:32px; flex-wrap:wrap; font-size:0.9em; margin-top:12px;">';
            if (rec.optimal_decode_tp)
                html += `<div><strong>Optimal Decode TP:</strong> ${rec.optimal_decode_tp.tp} <span style="color:#64748b">(${rec.optimal_decode_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.optimal_prefill_tp)
                html += `<div><strong>Optimal Prefill TP:</strong> ${rec.optimal_prefill_tp.tp} <span style="color:#64748b">(${rec.optimal_prefill_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.pd_tests_count)
                html += `<div><strong>PD Splits Tested:</strong> ${rec.pd_tests_count}</div>`;
            if (rec.ep_tests_count)
                html += `<div><strong>EP Configs Tested:</strong> ${rec.ep_tests_count}</div>`;
            html += '</div>';
        }

        // Constraint notes (filter out restart messages — those aren't real constraints)
        if (rec.constraint_notes && rec.constraint_notes.length) {
            var realConstraints = rec.constraint_notes.filter(function(n) { return n.indexOf('Server restarted') === -1; });
            if (realConstraints.length) {
                html += '<div style="background:#fffbeb; border:2px solid #f59e0b; border-left:6px solid #f59e0b; border-radius:8px; padding:14px 18px; margin-top:12px;">';
                html += '<div style="font-weight:700; color:#92400e; margin-bottom:8px; font-size:0.95em;">&#9888; Configuration Constraints</div>';
                for (const note of realConstraints) {
                    html += `<p style="color:#78350f; margin:0 0 8px; font-size:0.88em; line-height:1.6;">${note}</p>`;
                }
                html += '</div>';
            }
        }

        html += '</div></div>';

    } // end Deployment Recommendation card

    // --- Calibrated Recommendations (Step 11) — separate card ---
    const calBest = data.summary && data.summary.calibrated_best;
    if (calBest) {
        html += '<div class="chart-card" style="margin-top:20px; border:2px solid #0ea5e9; border-left:6px solid #0ea5e9;">';
        html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#0ea5e9,#06b6d4); color:white; font-size:1.1em; font-weight:800;">Deployment Recommendation &mdash; Calibrated Load</div>';
        html += '<div style="padding:12px 20px 4px; color:#475569; font-size:0.9em;">Performance at sustainable production load — calibrated concurrency where queue wait is reasonable. These results reflect realistic production conditions.</div>';
        html += `<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px; padding:16px 20px;">`;

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

            html += _buildRecCard({
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

        html += '</div></div>';
    }

    // Cache Sweep Recommendation
    const cacheBest = data.summary && data.summary.cache_sweep_best;
    if (cacheBest) {
        html += '<div class="chart-card" style="margin-top:20px; border:2px solid #8b5cf6; border-left:6px solid #8b5cf6;">';
        html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6); color:white; font-size:1.1em; font-weight:800;">Deployment Recommendation &mdash; Cache Hit Sweep</div>';
        html += '<div style="padding:12px 20px 4px; color:#475569; font-size:0.9em;">Performance with varying prefix cache hit rates — showing how cache efficiency affects throughput and latency at calibrated load.</div>';
        html += `<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px; padding:16px 20px;">`;

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

            html += _buildRecCard({
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

        html += '</div></div>';
    }

    // Flush recommendation part 1 (goal banner + deployment cards)
    secRec = html; html = '';

    // ============================================================
    // TP CALIBRATION CHARTS — Step 2 (Decode) & Step 3 (Prefill)
    // ============================================================
    if (rec) {
            const hasDecodeTP = rec.decode_tp_all && rec.decode_tp_all.length;
            const hasPrefillTP = rec.prefill_tp_all && rec.prefill_tp_all.length;
            if (hasDecodeTP || hasPrefillTP) {
                html += '<div class="chart-card" style="border-left:6px solid #0d9488;">' +
                    '<div class="chart-card-header" style="background:linear-gradient(135deg,#0d9488,#14b8a6); color:white;">TP Calibration</div>' +
                    '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
                    'Tests different <strong>Tensor Parallelism (TP)</strong> values per role to find the minimum viable TP that fits in GPU memory. ' +
                    'Lower TP means fewer GPUs per pod, allowing more replicas and higher throughput. Each TP value is tested with a short benchmark to measure baseline latency and token throughput.' +
                    '</div></div>';
                html += '<div class="charts-grid-2col">';
                html += chartCard(
                    'Step 2: Decode TP Sweep',
                    hasDecodeTP
                        ? 'Each TP value was tested with a single decode pod to find the optimal tensor parallelism for decode. The <strong>tokens/s/GPU</strong> metric (bars) shows efficiency — higher is better. The <strong style="color:#ef4444">ITL P90</strong> line shows inter-token latency — lower means smoother streaming. The optimal TP maximizes throughput per GPU.'
                        : 'Decode TP calibration chart will appear here once Step 2 completes.',
                    'chart-tp-decode'
                );
                html += chartCard(
                    'Step 3: Prefill TP Sweep',
                    hasPrefillTP
                        ? 'Each TP value was tested with a single prefill pod to find the optimal tensor parallelism for prefill. The <strong>tokens/s/GPU</strong> metric (bars) shows efficiency — higher is better. The <strong style="color:#ef4444">TTFT P90</strong> line shows time-to-first-token — lower means the model starts responding faster.'
                        : 'Prefill TP calibration chart will appear here once Step 3 completes.',
                    'chart-tp-prefill'
                );
                html += '</div>';

            }
        }

    // Calibration parameters table (after all charts)
    if (rec) {
        var hasDecodeTP2 = rec.decode_tp_all && rec.decode_tp_all.length;
        var hasPrefillTP2 = rec.prefill_tp_all && rec.prefill_tp_all.length;
        var calRows = [];
        if (hasDecodeTP2) {
            rec.decode_tp_all.forEach(function(d) {
                calRows.push({ role: 'Decode', tp: d.tp, isl: d.cal_isl, osl: d.cal_osl, conc: d.cal_concurrency, reqs: d.cal_max_requests, tspg: d.tpsg != null ? d.tpsg : null, ttft: null, itl: d.itl_p90 != null ? d.itl_p90 : null });
            });
        }
        if (hasPrefillTP2) {
            rec.prefill_tp_all.forEach(function(d) {
                calRows.push({ role: 'Prefill', tp: d.tp, isl: d.cal_isl, osl: d.cal_osl, conc: d.cal_concurrency, reqs: d.cal_max_requests, tspg: d.tpsg != null ? d.tpsg : null, ttft: d.ttft_p90 != null ? d.ttft_p90 : null, itl: null });
            });
        }
        if (calRows.length) {
            var calTableId = 'cal-params-table' + _chartSuffix;
            html += '<div id="' + calTableId + '" style="margin:16px 0;padding:16px 20px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;">';
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">';
            html += '<div style="font-weight:700;font-size:0.95em;color:#1e293b;">Calibration Test Parameters</div>';
            html += '<button onclick="downloadCalParamsTablePng(\'' + _chartSuffix + '\')" style="font-size:0.78em;padding:4px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#475569;cursor:pointer;">&#11015; Download PNG</button>';
            html += '</div>';
            html += '<p style="color:#64748b;font-size:0.82em;margin:0 0 10px;">Each TP value is tested with isolated workloads: <strong>ISL=1</strong> for decode (measures pure token generation) and <strong>OSL=1</strong> for prefill (measures pure prompt processing). Concurrency is estimated from KV cache capacity to avoid OOM.</p>';
            html += '<table style="width:100%;font-size:0.85em;text-align:center;border-collapse:collapse;"><tr style="background:#f1f5f9;"><th style="padding:8px 12px;border:1px solid #e2e8f0;">Role</th><th style="padding:8px 12px;border:1px solid #e2e8f0;">TP</th><th style="padding:8px 12px;border:1px solid #e2e8f0;">ISL</th><th style="padding:8px 12px;border:1px solid #e2e8f0;">OSL</th><th style="padding:8px 12px;border:1px solid #e2e8f0;">Concurrency</th><th style="padding:8px 12px;border:1px solid #e2e8f0;">Max Requests</th><th style="padding:8px 12px;border:1px solid #e2e8f0;">TSPG (tok/s/GPU)</th><th style="padding:8px 12px;border:1px solid #e2e8f0;">ITL P90</th><th style="padding:8px 12px;border:1px solid #e2e8f0;">TTFT P90</th></tr>';
            calRows.forEach(function(r) {
                var roleStyle = r.role === 'Decode' ? ' style="color:#6366f1;text-align:center;"' : ' style="color:#0d9488;text-align:center;"';
                var tspgStr = r.tspg != null ? r.tspg.toLocaleString() : '-';
                var ttftStr = r.ttft != null ? r.ttft.toLocaleString() + ' ms' : '-';
                html += '<tr><td' + roleStyle + '><strong>' + r.role + '</strong></td>' +
                    '<td style="padding:8px 12px;border:1px solid #e2e8f0;">' + r.tp + '</td>' +
                    '<td style="padding:8px 12px;border:1px solid #e2e8f0;">' + (r.isl != null ? r.isl.toLocaleString() : '-') + '</td>' +
                    '<td style="padding:8px 12px;border:1px solid #e2e8f0;">' + (r.osl != null ? r.osl.toLocaleString() : '-') + '</td>' +
                    '<td style="padding:8px 12px;border:1px solid #e2e8f0;">' + (r.conc || '-') + '</td>' +
                    '<td style="padding:8px 12px;border:1px solid #e2e8f0;">' + (r.reqs || '-') + '</td>' +
                    '<td style="padding:8px 12px;border:1px solid #e2e8f0;font-weight:600;">' + tspgStr + '</td>' +
                    '<td style="padding:8px 12px;border:1px solid #e2e8f0;">' + (r.itl != null ? r.itl.toLocaleString() + ' ms' : '-') + '</td>' +
                    '<td style="padding:8px 12px;border:1px solid #e2e8f0;">' + ttftStr + '</td></tr>';
            });
            html += '</table></div>';
        }
    }

    // Flush TP calibration
    secTP = html; html = '';

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

            html += `<div class="chart-card"><div class="chart-card-header">Percentile Breakdown: ${primaryArch} vs Aggregated</div>`;
            html += `<div style="padding:10px 20px 4px; color:#4b5563; font-size:0.9em;">Full percentile distribution (P50 through P99) for TTFT, ITL, and Throughput — comparing the best ${primaryArch} configuration against the Aggregated baseline using the same GPU budget. Green-highlighted P90 values indicate the winner for each metric.</div>`;
            html += '<div class="chart-card-body" style="padding:0;">';
            html += '<table class="results-table">';

            html += '<tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';

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
                    html += '<tr>';
                    if (mi === 0) html += `<td rowspan="3" style="vertical-align:middle; font-weight:700;${borderStyle}">${label}</td>`;
                    html += `<td style="color:#64748b;${borderStyle}">${m.name}</td>`;
                    html += `<td style="${borderStyle}">${d.p50 ?? '-'}</td>`;
                    html += `<td style="${p90Style}${borderStyle}">${d.p90 ?? '-'}</td>`;
                    html += `<td style="${borderStyle}">${d.p95 ?? '-'}</td>`;
                    html += `<td style="${borderStyle}">${d.p99 ?? '-'}</td>`;
                    html += '</tr>';
                });
            });

            html += '</table></div></div>';
        }
    }

    // --- Summary cards ---
    const best = summary.best_configs || {};
    html += '<div class="charts-summary">';
    html += statCard(summary.successful_tests, 'Successful Tests');
    if (summary.discarded_tests > 0) {
        html += statCard(summary.discarded_tests, 'Discarded Tests', 'not viable');
    }
    if (best.lowest_latency) {
        const ll = best.lowest_latency;
        html += statCard(ll.ttft_p90.toFixed(1) + ' ms', 'Best TTFT P90', ll.name);
        if (ll.ttft_p95) html += statCard(ll.ttft_p95.toFixed(1) + ' ms', 'Best TTFT P95', ll.name);
        if (ll.ttft_p99) html += statCard(ll.ttft_p99.toFixed(1) + ' ms', 'Best TTFT P99', ll.name);
    }
    if (best.lowest_itl) {
        html += statCard(best.lowest_itl.itl_p90.toFixed(2) + ' ms', 'Best ITL P90', best.lowest_itl.name);
    }
    if (best.highest_throughput) {
        const ht = best.highest_throughput;
        const htVal = ht.throughput_mean || ht.throughput_p90;
        html += statCard(htVal.toFixed(2) + ' req/s', 'Best Throughput Mean', ht.name);
    }
    if (best.most_efficient)
        html += statCard(best.most_efficient.efficiency.toFixed(3), 'Best Efficiency (req/s/GPU)', best.most_efficient.name);
    html += '</div>';

    // Flush recommendation part 2 (percentile breakdown + summary cards)
    secRec += html; html = '';

    // --- Charts with descriptions ---
    const chartDesc = {
        pareto: 'Steps 2 &amp; 3 tested each TP value with a single <strong style="color:#3b82f6">Decode</strong> pod and a single <strong style="color:#f59e0b">Prefill</strong> pod to calibrate tensor parallelism. Points lower and to the left are better (faster response with fewer GPUs). The optimal TP for each role was selected from these results.',
        scatter: 'Each bubble is a tested configuration. The <strong>bubble size</strong> represents GPU count. The ideal configuration is in the <strong>top-left corner</strong> (low latency + high throughput). Hover over any bubble to see the exact configuration details.',
        efficiency: 'Shows how many requests each configuration can serve per GPU. <strong>Higher bars = better value for money.</strong> A configuration with high efficiency means you get more throughput from each GPU you pay for.',
        arch: 'Side-by-side comparison of <strong>Aggregated</strong> (single pool of GPUs) vs <strong>PD</strong> (dedicated prefill and decode GPUs) architectures. Lower TTFT is better for responsiveness. Higher throughput means more users served.'
    };

    // TP Pareto chart → TP Calibration subtab (only if TP data exists)
    if (secTP) secTP += chartCard('TP Calibration: Latency vs GPU Count', chartDesc.pareto, 'chart-pareto');

    // Scatter, efficiency → Configurations subtab
    secCfg += '<div class="chart-card" style="border-left:6px solid #6366f1;">' +
        '<div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#818cf8); color:white;">Configurations</div>' +
        '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
        'All tested configurations across PD, EP, and Aggregated architectures. Charts show TTFT vs Throughput trade-offs at each percentile. ' +
        'The <strong>Pareto Optimal</strong> table highlights configurations that offer the best trade-offs — no other config is better on both latency and throughput simultaneously.' +
        '</div></div>';
    if (rec && rec.best_by_percentile && rec.best_by_percentile.p90) {
        var _bbpSel = rec.best_by_percentile.p90;
        // Mark primary recommendation config names
        var _recLabel = {};
        if (rec.recommendations) {
            var _rtCfg = rec.recommendations.response_time && rec.recommendations.response_time.config;
            var _tpCfg = rec.recommendations.throughput    && rec.recommendations.throughput.config;
            if (_rtCfg) _recLabel[_rtCfg.config_name] = '★ Primary TTFT recommendation';
            if (_tpCfg) _recLabel[_tpCfg.config_name] = '★ Primary throughput recommendation';
        }

        function _fmtMs(v) { return v == null ? '—' : (v >= 1000 ? (v/1000).toFixed(1)+'s' : Math.round(v)+'ms'); }
        function _fmtTput(v) { return v == null ? '—' : v.toFixed(2)+' req/s'; }
        var _tdBase = 'padding:7px 10px;border:1px solid #e2e8f0;text-align:center;';
        var _tdLeft = 'padding:7px 10px;border:1px solid #e2e8f0;text-align:center;';

        var _archDefs = [
            { key: 'pd',          label: 'PD',          color: '#0ea5e9', bg: '#f0f9ff' },
            { key: 'ep',          label: 'EP',          color: '#10b981', bg: '#f0fdf4' },
            { key: 'aggregated',  label: 'Aggregated',  color: '#6366f1', bg: '#eef2ff' }
        ];
        var _catDefs = [
            { key: 'balanced',       label: 'Best balanced'   },
            { key: 'lowest_ttft',    label: 'Best TTFT'       },
            { key: 'highest_tput',   label: 'Best throughput' },
            { key: 'most_efficient', label: 'Most efficient'  },
            { key: 'lowest_itl',     label: 'Best ITL'        }
        ];

        var _anyArch = _archDefs.some(function(a) { return !!_bbpSel[a.key]; });
        if (_anyArch) {
            var _selTableId = 'cfg-selection-table' + _chartSuffix;
            secCfg += '<div class="chart-card">';
            secCfg += '<div class="chart-card-header">Configuration Selection — Top 5 per Architecture</div>';
            secCfg += '<div id="' + _selTableId + '" style="padding:10px 20px 16px;">';
            secCfg += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">';
            secCfg += '<p style="color:#64748b;font-size:0.82em;margin:0;">The optimizer selects up to 5 configurations per architecture for the concurrency sweep — one per category. ★ = final primary recommendation.</p>';
            secCfg += '<button onclick="downloadTableAsPng(\'' + _selTableId + '\',\'configuration-selection.png\')" style="flex-shrink:0;margin-left:12px;font-size:0.78em;padding:4px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#475569;cursor:pointer;">&#11015; Download PNG</button>';
            secCfg += '</div>';

            _archDefs.forEach(function(arch) {
                var ad = _bbpSel[arch.key];
                if (!ad) return;
                secCfg += '<div style="margin-bottom:18px;">';
                secCfg += '<div style="font-weight:700;font-size:0.85em;color:' + arch.color + ';text-transform:uppercase;letter-spacing:0.06em;border-bottom:2px solid ' + arch.color + ';padding-bottom:4px;margin-bottom:6px;">' + arch.label + ' Architecture</div>';
                secCfg += '<table style="width:100%;font-size:0.83em;border-collapse:collapse;table-layout:fixed;">';
                secCfg += '<colgroup><col style="width:20%"><col style="width:22%"><col style="width:13%"><col style="width:11%"><col style="width:17%"><col style="width:17%"></colgroup>';
                var _tdNow = _tdBase + 'white-space:nowrap;';
                secCfg += '<tr style="background:#f1f5f9;">' +
                    '<th style="' + _tdLeft + 'font-weight:700;">Category</th>' +
                    '<th style="' + _tdLeft + 'font-weight:700;">Config</th>' +
                    '<th style="' + _tdNow + 'font-weight:700;">TTFT P90</th>' +
                    '<th style="' + _tdNow + 'font-weight:700;">ITL P90</th>' +
                    '<th style="' + _tdNow + 'font-weight:700;">Throughput</th>' +
                    '<th style="' + _tdNow + 'font-weight:700;">GPUs</th>' +
                    '</tr>';

                // Deduplicate: group categories by config_name, preserving category order
                var _seen = {}, _deduped = [];
                _catDefs.forEach(function(cat) {
                    var e = ad[cat.key];
                    if (!e) return;
                    var cn = e.config_name;
                    if (_seen[cn]) {
                        _seen[cn].cats.push(cat.label);
                    } else {
                        _seen[cn] = { entry: e, cats: [cat.label] };
                        _deduped.push(cn);
                    }
                });
                _deduped.forEach(function(cn) {
                    var e      = _seen[cn].entry;
                    var cats   = _seen[cn].cats;
                    var recLbl = _recLabel[cn];
                    var isRec  = !!recLbl;
                    var rowBg  = isRec ? 'background:#fffbeb;' : '';
                    var catBadges = cats.map(function(lbl) {
                        return '<span style="display:inline-block;background:' + arch.bg + ';color:' + arch.color + ';font-size:0.78em;font-weight:600;padding:1px 7px;border-radius:8px;margin:1px 2px 1px 0;">' + lbl + '</span>';
                    }).join('');
                    var recBadge = isRec ? ' <span style="display:inline-block;background:#fef3c7;color:#b45309;font-size:0.78em;font-weight:700;padding:1px 7px;border-radius:8px;">' + recLbl + '</span>' : '';
                    secCfg += '<tr style="' + rowBg + '">';
                    secCfg += '<td style="' + _tdLeft + '">' + catBadges + recBadge + '</td>';
                    secCfg += '<td style="' + _tdLeft + 'font-weight:' + (isRec?'700':'400') + ';">' + cn + '</td>';
                    secCfg += '<td style="' + _tdNow + '">' + _fmtMs(e.ttft)                         + '</td>';
                    secCfg += '<td style="' + _tdNow + '">' + _fmtMs(e.itl)                          + '</td>';
                    secCfg += '<td style="' + _tdNow + '">' + _fmtTput(e.throughput_mean || e.throughput) + '</td>';
                    secCfg += '<td style="' + _tdNow + '">' + (e.gpus != null ? e.gpus : '—')        + '</td>';
                    secCfg += '</tr>';
                });

                secCfg += '</table></div>';
            });
            secCfg += '</div></div>';
        }
    }
    secCfg += chartCard('Throughput vs Latency', chartDesc.scatter, 'chart-scatter');
    secCfg += chartCard('GPU Efficiency (req/s per GPU)', chartDesc.efficiency, 'chart-efficiency');
    secCfg += chartCard('Per-User Token Throughput (tokens/s per user)', 'Shows how many output tokens each concurrent user receives per second. <strong>Higher = faster streaming for each user.</strong>', 'chart-per-user-efficiency');

    // Cache Hit Rate per configuration
    var cfgCacheData = coreResults.filter(function(r) { return r.cache_hit_pct != null; });
    if (cfgCacheData.length) {
        secCfg += '<div class="chart-card"><div class="chart-card-header">Prefix Cache Hit Rate per Configuration</div>';
        secCfg += '<div style="padding:8px 20px 0;color:#1e293b;font-size:0.92em;">Actual prefix cache hit percentage measured by vLLM during each test. Higher = more requests served from cached KV data.</div>';
        secCfg += '<div class="chart-card-body"><div id="chart-cfg-cache-hit' + _chartSuffix + '" style="width:100%;height:850px;"></div></div></div>';
        var archColors3 = { AGGREGATED: '#1f77b4', PD: '#ff7f0e', EP: '#2ca02c' };
        chartQueue.push(function() {
            var traces = [];
            ['AGGREGATED', 'PD', 'EP'].forEach(function(arch) {
                var ar = cfgCacheData.filter(function(r) { return r.architecture === arch; });
                if (!ar.length) return;
                traces.push({
                    x: ar.map(function(r) { return _tagLabel(r.config_name, r.architecture); }),
                    y: ar.map(function(r) { return r.cache_hit_pct; }),
                    text: ar.map(function(r) { return r.cache_hit_pct.toFixed(1) + '%'; }),
                    textposition: 'outside', textfont: { size: 13 },
                    name: arch, type: 'bar',
                    marker: { color: archColors3[arch] }
                });
            });
            Plotly.newPlot('chart-cfg-cache-hit' + _chartSuffix, traces, {
                ...plotlyLayout, height: 850, barmode: 'group',
                xaxis: { tickangle: -35, gridcolor: '#e2e8f0' },
                yaxis: { title: 'Cache Hit %', gridcolor: '#e2e8f0', range: [0, 100] },
                showlegend: true, legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.9)' },
                margin: { t: 20, b: 120, l: 60, r: 20 },
                plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
            }, { responsive: true });
        });
    }


    // --- PD configurations TTFT + Throughput charts (one per percentile) ---
    if (coreResults.filter(r => r.architecture === 'PD').length) {
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P90)',
            '<strong style="color:#3b82f6">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P90.',
            'chart-pd-ttft-p90'
        );
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P95)',
            '<strong style="color:#dc2626">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P95.',
            'chart-pd-ttft-p95'
        );
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P99)',
            '<strong style="color:#7c3aed">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P99.',
            'chart-pd-ttft-p99'
        );
    }

    // --- Aggregated configurations chart (all percentiles in one chart) ---
    if (coreResults.filter(r => r.architecture === 'AGGREGATED').length > 1) {
        html += chartCard(
            'Aggregated Configurations — TTFT & Throughput (P90 / P95 / P99)',
            'All aggregated configurations with <strong style="color:#3b82f6">TTFT</strong> (bars, lower is better) and <strong style="color:#f59e0b">Throughput Mean</strong> (line, right axis, higher is better) across percentiles.',
            'chart-agg-ttft-all'
        );
    }

    // --- Pareto table ---
    if (charts.pareto.pareto_table.length) {
        html += '<div class="chart-card"><div class="chart-card-header">Pareto Optimal Configurations</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">These configurations represent the <strong>best possible trade-offs</strong>. Each one is optimal for a different balance of speed, throughput, and GPU cost. No other tested configuration beats any of these on all metrics at once.</div>';
        html += '<div class="chart-card-body" style="padding:0;">';
        html += '<table class="results-table"><tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>GPUs</th><th title="Throughput Mean ÷ Total GPUs (req/s per GPU). Higher = better cost-efficiency.">Efficiency<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th><th>Manifests</th></tr>';
        charts.pareto.pareto_table.forEach((p, idx) => {
            let manifestLinks = '-';
            const pTestId = p.test_id || testIdLookup[p.config_name] || p.config_name;
            const mTypes = manifestLookup[pTestId];
            if (mTypes && mTypes.length) {
                const mlabels2 = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
                manifestLinks = mTypes.filter(t => !t.includes('service')).map(t => {
                    return `<a href="/api/run/${runId}/config/${pTestId}/manifest/${t}" title="Download ${t}.yaml" target="_blank" style="color:#0ea5e9; text-decoration:none; font-size:11px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${mlabels2[t] || t} &#8595;</a>`;
                }).join(' ');
            }
            const borderTop = idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
            const tputMeanPareto = p.throughput_mean ?? p.throughput_p90;
            const latencyMetrics = [
                {name: 'TTFT (ms)', p50: p.ttft_p50, p90: p.ttft_p90, p95: p.ttft_p95, p99: p.ttft_p99},
                {name: 'ITL (ms)', p50: p.itl_p50, p90: p.itl_p90, p95: p.itl_p95, p99: p.itl_p99},
            ];
            latencyMetrics.forEach((m, mi) => {
                const rowBorder = mi === 0 && idx > 0 ? borderTop : '';
                html += `<tr class="pareto-row">`;
                if (mi === 0) {
                    const pEppBadge = (pTestId && pTestId.startsWith('step11-epp-')) ? '<br><span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
                    html += `<td rowspan="3" style="vertical-align:middle; font-weight:700;${rowBorder}">${p.config_name}<br><span style="font-weight:400; font-size:0.85em; color:#64748b;">${p.architecture}</span>${pEppBadge}</td>`;
                }
                html += `<td style="color:#64748b;${rowBorder}">${m.name}</td>`;
                html += `<td style="${rowBorder}">${m.p50 ?? '-'}</td>`;
                html += `<td style="${rowBorder}">${m.p90 ?? '-'}</td>`;
                html += `<td style="${rowBorder}">${m.p95 ?? '-'}</td>`;
                html += `<td style="${rowBorder}">${m.p99 ?? '-'}</td>`;
                if (mi === 0) {
                    html += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${p.gpus}</td>`;
                    html += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${p.efficiency}</td>`;
                    html += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${manifestLinks}</td>`;
                }
                html += '</tr>';
            });
            // Throughput mean — single value spanning P50-P99 columns
            html += `<tr class="pareto-row">`;
            html += `<td style="color:#64748b;">Throughput Mean (req/s)</td>`;
            html += `<td colspan="4" style="text-align:center; font-weight:600;">${tputMeanPareto ?? '-'}</td>`;
            html += '</tr>';
        });
        html += '</table></div></div>';
    }

    // --- Token Throughput section ---
    var tokenResults = coreResults.filter(function(r) {
        var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
        var pm = m.prometheus_metrics || {};
        return r.output_tps_mean || m.output_tps_mean || pm.vllm_generation_tokens_rate;
    });
    if (tokenResults.length) {
        var tkTableId = 'token-tput-table-' + runId;
        html += '<div class="chart-card"><div class="chart-card-header">Token Throughput — GPU Computed vs Server Total vs Client</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            '<strong>Blue bar (GPU computed):</strong> Tokens that required actual GPU compute — prompt tokens that were <em>not</em> served from prefix cache, plus generated output tokens. Calculated as: <code>prompt_tps &times; (1 - cache_hit_rate) + generation_tps</code>.<br>' +
            '<strong>Light blue bar (server total):</strong> All tokens processed by vLLM server-side, including cached prefix tokens. Measured via Prometheus (<code>vllm:prompt_tokens + vllm:generation_tokens</code>).<br>' +
            '<strong>Gold bar (client):</strong> All tokens measured client-side by guidellm (<code>prompt_tokens_mean &times; throughput + output_tokens_mean &times; throughput</code>).<br>' +
            '<strong>The gap between blue and light blue reflects prefix cache efficiency.</strong> The gap between light blue and gold reflects measurement differences (network overhead, timing windows).' +
            '</div>';
        html += '<div class="chart-card-body"><div id="chart-token-tput' + cid('') + '" style="height:850px;margin-bottom:16px;"></div></div></div>';

        html += '<div class="chart-card"><div class="chart-card-header">Throughput &amp; Utilization Details</div>';
        html += '<div class="chart-card-body" style="padding:0;">';
        html += '<table class="results-table" id="' + tkTableId + '"><tr>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',1,\'num\')">Prompt tok/s &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">avg (peak)</span></th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',2,\'num\')">Generation tok/s &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">avg (peak)</span></th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',3,\'num\')">Total tok/s &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',4,\'num\')">vLLM req/s &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',5,\'num\')">guidellm req/s &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',6,\'num\')">Avg Running &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',7,\'num\')">Avg Waiting &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',8,\'num\')">Cache Hit &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',9,\'num\')">KV Cache &#x21C5;</th>';
        html += '</tr>';
        tokenResults.forEach(function(r) {
            var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
            var pm = m.prometheus_metrics || {};
            var promptRate = pm.vllm_prompt_tokens_rate || {};
            var genRate = pm.vllm_generation_tokens_rate || {};
            var promptAvg = promptRate.avg != null ? Math.round(promptRate.avg) : null;
            var promptMax = promptRate.max != null ? Math.round(promptRate.max) : null;
            var genAvg = genRate.avg != null ? Math.round(genRate.avg) : null;
            var genMax = genRate.max != null ? Math.round(genRate.max) : null;
            var totalAvg = (promptAvg != null && genAvg != null) ? promptAvg + genAvg : null;
            var vllmReqRate = (pm.vllm_e2e_count_rate || {}).avg;
            var vllmReqStr = vllmReqRate != null ? vllmReqRate.toFixed(2) : 'N/A';
            var guidellmReq = r.throughput_mean != null ? r.throughput_mean.toFixed(2) : 'N/A';
            var avgRunning = (pm.vllm_requests_running || {}).avg;
            var avgWaiting = (pm.vllm_requests_waiting || {}).avg;
            var cacheHit = r.cache_hit_pct != null ? r.cache_hit_pct + '%' : 'N/A';
            var kvUsage = m.kv_cache_usage != null ? (m.kv_cache_usage * 100).toFixed(1) + '%' : 'N/A';
            var na = 'N/A';
            var fmtRate = function(avg, max) { return avg != null ? avg.toLocaleString() + ' (' + (max != null ? max.toLocaleString() : '?') + ')' : na; };
            html += '<tr><td>' + r.config_name + '</td>';
            html += '<td data-val="' + (promptAvg || '') + '">' + fmtRate(promptAvg, promptMax) + '</td>';
            html += '<td data-val="' + (genAvg || '') + '">' + fmtRate(genAvg, genMax) + '</td>';
            html += '<td data-val="' + (totalAvg || '') + '">' + (totalAvg != null ? totalAvg.toLocaleString() : na) + '</td>';
            html += '<td data-val="' + (vllmReqRate || '') + '">' + vllmReqStr + '</td>';
            html += '<td data-val="' + (r.throughput_mean || '') + '">' + guidellmReq + '</td>';
            html += '<td data-val="' + (avgRunning || '') + '">' + (avgRunning != null ? avgRunning.toFixed(1) : na) + '</td>';
            html += '<td data-val="' + (avgWaiting || '') + '">' + (avgWaiting != null ? avgWaiting.toFixed(1) : na) + '</td>';
            html += '<td data-val="' + (r.cache_hit_pct || '') + '">' + cacheHit + '</td>';
            html += '<td data-val="' + (m.kv_cache_usage || '') + '">' + kvUsage + '</td>';
            html += '</tr>';
        });
        html += '</table></div></div>';
    }

    // --- All results table ---
    if (coreResults.length) {
        var allCfgTableId = 'all-configs-table-' + runId;
        html += '<div class="chart-card"><div class="chart-card-header">Latency and Efficiency</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Complete results from every test that ran successfully. <strong>Green highlighted rows</strong> are Pareto optimal (the best trade-offs). Click any column header to sort.</div>';
        html += '<div class="chart-card-body" style="padding:0;">';
        html += '<table class="results-table" id="' + allCfgTableId + '"><tr>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',1,\'str\')">Architecture &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',2,\'num\')">TTFT P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',3,\'num\')">E2E P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',4,\'num\')">TTFT P95 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',5,\'num\')">TTFT P99 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',6,\'num\')">Tput Mean &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',7,\'num\')">ITL P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',8,\'num\')">GPUs &#x21C5;</th>';
        html += '<th style="cursor:pointer;" title="Throughput Mean ÷ Total GPUs (req/s per GPU)" onclick="sortReportTable(\'' + allCfgTableId + '\',9,\'num\')">Efficiency &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th>';
        html += '<th>Manifests</th>';
        html += '</tr>';
        const paretoNames = new Set(charts.pareto.pareto_table.map(p => p.config_name));
        coreResults.forEach((r, idx) => {
            const cls = paretoNames.has(r.config_name) ? ' class="pareto-row"' : '';
            const rTestId = r.test_id || testIdLookup[r.config_name] || r.config_name;
            let manifestLinks = '-';
            if (r.manifest_types && r.manifest_types.length > 0) {
                const mlabels = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
                manifestLinks = r.manifest_types.filter(t => !t.includes('service')).map(t => {
                    return `<a href="/api/run/${runId}/config/${rTestId}/manifest/${t}" title="Download ${t}.yaml" target="_blank" style="color:#0ea5e9; text-decoration:none; font-size:11px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${mlabels[t] || t} &#8595;</a>`;
                }).join(' ');
            }
            const na = 'N/A';
            const eppBadge = (rTestId && rTestId.startsWith('step11-epp-')) ? ' <span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
            const tputMeanVal = r.throughput_mean ?? r.throughput_p90 ?? na;
            const e2eVal = r.e2e_latency_p90 != null ? Math.round(r.e2e_latency_p90) : na;
            html += `<tr${cls}><td>${r.config_name}${eppBadge}</td><td>${r.architecture}</td><td data-val="${r.ttft_p90}">${r.ttft_p90}</td><td data-val="${e2eVal === na ? '' : e2eVal}">${e2eVal}${e2eVal !== na ? ' ms' : ''}</td><td data-val="${r.ttft_p95 ?? ''}">${r.ttft_p95 ?? na}</td><td data-val="${r.ttft_p99 ?? ''}">${r.ttft_p99 ?? na}</td><td data-val="${tputMeanVal}">${tputMeanVal}</td><td data-val="${r.itl_p90 ?? ''}">${r.itl_p90 ?? na}</td><td data-val="${r.gpus}">${r.gpus}</td><td data-val="${r.efficiency}">${r.efficiency}</td><td>${manifestLinks}</td></tr>`;
        });
        html += '</table></div></div>';
    }

    // --- vLLM Engine Metrics for configuration tests ---
    var cfgEngineRes = (data.all_results || []).filter(function(r) {
        if (r.quality === 'discard') return false;
        var tid = r.test_id || '';
        return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
               tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
    });
    if (cfgEngineRes.length >= 1) {
        html += '<div class="chart-card" style="margin-top:16px;"><div class="chart-card-header">vLLM Engine Metrics</div>';
        html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">KV cache usage, running/waiting requests, prefill/decode time across tested configurations.</div>';
        html += '<div id="chart-cfg-engine' + _chartSuffix + '" style="width:100%;"></div></div>';

        chartQueue.push(function() {
            var labels = [], kvAvg = [], kvMax = [], runAvg = [], runMax = [], runSpread = [], waitAvg = [], prefillT = [], decodeT = [], preempt = [];
            cfgEngineRes.forEach(function(r) {
                var mj = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                var pm = mj.prometheus_metrics || {};
                labels.push(_tagLabel(r.config_name, r.architecture));
                var _rAvg = (pm.vllm_requests_running || {}).avg || 0;
                var _rMax = (pm.vllm_requests_running || {}).max || 0;
                kvAvg.push(((pm.vllm_kv_cache_pct || {}).avg || 0) * 100);
                kvMax.push(((pm.vllm_kv_cache_pct || {}).max || 0) * 100);
                runAvg.push(_rAvg);
                runMax.push(_rMax);
                runSpread.push(_rMax - _rAvg);
                waitAvg.push((pm.vllm_requests_waiting || {}).avg || 0);
                prefillT.push((pm.vllm_prefill_time_rate || {}).avg || 0);
                decodeT.push((pm.vllm_decode_time_rate || {}).avg || 0);
                preempt.push((pm.vllm_preemptions_rate || {}).avg || 0);
            });
            var traces = [
                { x: labels, y: kvAvg, name: 'KV Cache Avg %', type: 'bar', marker: { color: '#3b82f6' } },
                { x: labels, y: kvMax, name: 'KV Cache Max %', type: 'bar', marker: { color: '#93c5fd' } },
                { x: labels, y: runAvg, name: 'Avg Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 3 }, marker: { size: 8 } },
                { x: labels, y: runMax, name: 'Max Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 2, dash: 'dash' }, marker: { size: 6, symbol: 'triangle-up' } },
                { x: labels, y: runSpread, name: 'Load Spread', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 2, dash: 'dashdot' }, marker: { size: 6, symbol: 'diamond' } },
                { x: labels, y: waitAvg, name: 'Avg Waiting Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#ef4444', width: 2, dash: 'dot' }, marker: { size: 6 } },
                { x: labels, y: decodeT, name: 'Decode req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#f59e0b', width: 2 }, marker: { size: 6 } },
                { x: labels, y: prefillT, name: 'Prefill req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#8b5cf6', width: 2 }, marker: { size: 6 } },
            ];
            if (preempt.some(function(v) { return v > 0; })) {
                traces.push({ x: labels, y: preempt, name: 'Preemptions/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 3 }, marker: { size: 8, symbol: 'x' } });
            }
            Plotly.newPlot('chart-cfg-engine' + _chartSuffix, traces, {
                barmode: 'group',
                xaxis: { tickangle: -35 },
                yaxis: { title: 'KV Cache %', side: 'left', rangemode: 'tozero', gridcolor: '#e2e8f0' },
                yaxis2: { title: 'Count / Rate', overlaying: 'y', side: 'right', rangemode: 'tozero' },
                legend: { x: 0, y: 1.15, orientation: 'h' },
                margin: { t: 40, b: 140, l: 60, r: 60 },
                plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                hovermode: 'x unified',
                height: 850,
            }, { responsive: true });
        });
    }

    // Flush configurations (PD charts + pareto table + all results)
    secCfg += html; html = '';
    if (!coreResults.length) secCfg = '';

    // Architecture comparison chart + percentile bar chart → Comparison tab (above tables)
    html += '<div class="chart-card" style="border-left:6px solid #0284c7;">' +
        '<div class="chart-card-header" style="background:linear-gradient(135deg,#0284c7,#38bdf8); color:white;">Architecture Comparison</div>' +
        '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
        'Compares <strong>PD/EP disaggregated</strong> inference against the <strong>Aggregated</strong> baseline. ' +
        'PD separates prefill and decode into specialized pods so new requests don\'t wait behind ongoing generation. ' +
        'The percentage change charts show exactly how much each PD configuration improves (or regresses) relative to the best Aggregated result.' +
        '</div></div>';
    html += chartCard('Architecture Comparison',
        'Side-by-side comparison of <strong>Aggregated</strong> (single pool of GPUs) vs <strong>PD</strong> (dedicated prefill and decode GPUs) architectures. Lower TTFT is better for responsiveness. Higher throughput means more users served.',
        'chart-arch');
    html += chartCard('Percentile Comparison: Winner vs Aggregated',
        'Side-by-side bar chart comparing TTFT and Throughput at each percentile (P50, P90, P95, P99) between the recommended configuration and the Aggregated baseline.',
        'chart-percentile-bars');

    // ============================================================
    // STEP 8: Architecture Comparison (separate card)
    // Renders PD vs Agg, EP vs Agg, or both depending on goal
    // ============================================================
    if (rec && (rec.pd_vs_agg || rec.ep_vs_agg)) {
        // --- PD vs Aggregated ---
        if (rec.pd_vs_agg) {
            const cmp = rec.pd_vs_agg;
            const ttftColor = cmp.ttft_winner === 'PD' ? '#10b981' : '#f59e0b';
            const tputColor = cmp.throughput_winner === 'PD' ? '#10b981' : '#f59e0b';
            html += '<div class="chart-card" style="margin-top:16px; border:2px solid #6366f1; border-left:6px solid #6366f1;"><div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);">Step 8: PD vs Aggregated Comparison</div>';
            html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">The best PD configuration was tested head-to-head against an equivalent Aggregated deployment using the same GPU count and full workload. This validates whether PD disaggregation actually helps for this model.</div>';
            html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            html += '<tr><th>Metric</th><th>PD (best)</th><th>Aggregated</th><th>Winner</th></tr>';
            html += `<tr><td><strong>TTFT P90</strong></td><td>${cmp.pd.ttft_p90} ms</td><td>${cmp.aggregated.ttft_p90} ms</td><td style="color:${ttftColor}; font-weight:700;">${cmp.ttft_winner} (${cmp.ttft_diff_pct}% better)</td></tr>`;
            html += `<tr><td><strong>Throughput Mean</strong></td><td>${cmp.pd.throughput_mean || cmp.pd.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_mean || cmp.aggregated.throughput_p90} req/s</td><td style="color:${tputColor}; font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
            if (cmp.pd.ttft_p99 && cmp.aggregated.ttft_p99) {
                const p99Color = cmp.ttft_p99_winner === 'PD' ? '#10b981' : '#f59e0b';
                html += `<tr style="border-top:2px solid #e2e8f0;"><td><strong>TTFT P99 (tail)</strong></td><td>${cmp.pd.ttft_p99} ms</td><td>${cmp.aggregated.ttft_p99} ms</td><td style="color:${p99Color}; font-weight:700;">${cmp.ttft_p99_winner} (${cmp.ttft_p99_diff_pct}% better)</td></tr>`;
            }
            html += '</table></div>';

            // --- % Change chart: All PD configs vs Aggregated baseline ---
            if (rec.aggregated_baseline && coreResults.length > 1) {
                const aggBaseline = rec.aggregated_baseline;
                const baseTtft = aggBaseline.ttft_p90;
                const baseTput = aggBaseline.throughput_mean || aggBaseline.throughput_p90;
                const configs = coreResults.filter(r => r.architecture === 'PD' && r.ttft_p90 && r.throughput_p90);
                if (configs.length && baseTtft && baseTput) {
                    html += '<div style="padding:16px 20px 4px;"><div style="font-weight:700; font-size:0.95em; color:#1e293b; margin-bottom:4px;">All PD Configurations vs Aggregated Baseline</div>';
                    html += '<div style="color:#1e293b; font-size:0.92em; margin-bottom:12px;">Percentage change in TTFT and Throughput relative to the Step 8 Aggregated baseline (' + aggBaseline.config_name + '). For TTFT, negative (green) is better. For Throughput, positive (green) is better.</div>';
                    var pdTableId = 'pd-vs-agg-table-' + runId;
                    html += '<div class="chart-card-body" style="padding:0;"><table class="results-table" id="' + pdTableId + '">';
                    html += '<tr>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',1,\'num\')">TTFT P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',2,\'num\')">TTFT vs Agg &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',3,\'num\')">Throughput Mean &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',4,\'num\')">Tput vs Agg &#x21C5;</th>';
                    html += '</tr>';
                    const sorted = [...configs].sort((a, b) => a.ttft_p90 - b.ttft_p90);
                    for (const cfg of sorted) {
                        const cfgTput = cfg.throughput_mean || cfg.throughput_p90;
                        const ttftPct = ((cfg.ttft_p90 - baseTtft) / baseTtft * 100).toFixed(1);
                        const tputPct = ((cfgTput - baseTput) / baseTput * 100).toFixed(1);
                        const ttftBetter = parseFloat(ttftPct) < 0;
                        const tputBetter = parseFloat(tputPct) > 0;
                        const ttftColor = ttftBetter ? '#059669' : '#dc2626';
                        const tputColor = tputBetter ? '#059669' : '#dc2626';
                        const ttftArrow = ttftBetter ? '&#9660;' : '&#9650;';
                        const tputArrow = tputBetter ? '&#9650;' : '&#9660;';
                        const cmpEppBadge = (cfg.test_id && cfg.test_id.startsWith('step11-epp-')) ? ' <span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
                        html += `<tr><td><strong>${cfg.config_name}</strong>${cmpEppBadge}</td>`;
                        html += `<td data-val="${cfg.ttft_p90}">${cfg.ttft_p90} ms</td>`;
                        html += `<td data-val="${ttftPct}" style="color:${ttftColor}; font-weight:700;">${ttftArrow} ${ttftPct}%</td>`;
                        html += `<td data-val="${cfgTput}">${cfgTput} req/s</td>`;
                        html += `<td data-val="${tputPct}" style="color:${tputColor}; font-weight:700;">${tputArrow} ${tputPct}%</td></tr>`;
                    }
                    html += `<tr class="baseline-row" style="background:#f1f5f9;"><td><strong>${aggBaseline.config_name}</strong> <span style="background:#1f77b4; color:white; font-size:0.65em; padding:1px 5px; border-radius:3px;">BASELINE</span></td>`;
                    html += `<td data-val="${baseTtft}">${baseTtft} ms</td><td data-val="0" style="color:#64748b;">-</td>`;
                    html += `<td data-val="${baseTput}">${baseTput} req/s</td><td data-val="0" style="color:#64748b;">-</td></tr>`;
                    html += '</table></div></div>';
                }
            }
            html += '</div>';
        }

        // --- EP vs Aggregated ---
        if (rec.ep_vs_agg) {
            const cmp = rec.ep_vs_agg;
            const ttftColor = cmp.ttft_winner === 'EP' ? '#10b981' : '#f59e0b';
            const tputColor = cmp.throughput_winner === 'EP' ? '#10b981' : '#f59e0b';
            html += '<div class="chart-card" style="margin-top:16px; border:2px solid #6366f1; border-left:6px solid #6366f1;"><div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);">Step 8: EP vs Aggregated Comparison</div>';
            html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">The best EP (Expert Parallelism) configuration was tested head-to-head against an equivalent Aggregated deployment using the same GPU count and full workload. EP uses EPLB (expert-level prefill load balancing) to distribute work across independent pods.</div>';
            html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            html += '<tr><th>Metric</th><th>EP (best)</th><th>Aggregated</th><th>Winner</th></tr>';
            html += `<tr><td><strong>TTFT P90</strong></td><td>${cmp.ep.ttft_p90} ms</td><td>${cmp.aggregated.ttft_p90} ms</td><td style="color:${ttftColor}; font-weight:700;">${cmp.ttft_winner} (${cmp.ttft_diff_pct}% better)</td></tr>`;
            html += `<tr><td><strong>Throughput Mean</strong></td><td>${cmp.ep.throughput_mean || cmp.ep.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_mean || cmp.aggregated.throughput_p90} req/s</td><td style="color:${tputColor}; font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
            html += '</table></div>';

            // --- % Change chart: All EP configs vs Aggregated baseline ---
            if (rec.aggregated_baseline && rec.ep_all_configs && rec.ep_all_configs.length > 0) {
                const aggBaseline = rec.aggregated_baseline;
                const baseTtft = aggBaseline.ttft_p90;
                const baseTput = aggBaseline.throughput_mean || aggBaseline.throughput_p90;
                if (baseTtft && baseTput) {
                    html += '<div style="padding:16px 20px 4px;"><div style="font-weight:700; font-size:0.95em; color:#1e293b; margin-bottom:4px;">All EP Configurations vs Aggregated Baseline</div>';
                    html += '<div style="color:#1e293b; font-size:0.92em; margin-bottom:12px;">Percentage change in TTFT and Throughput relative to the Step 8 Aggregated baseline. For TTFT, negative (green) is better. For Throughput, positive (green) is better.</div>';
                    var epTableId = 'ep-vs-agg-table-' + runId;
                    html += '<div class="chart-card-body" style="padding:0;"><table class="results-table" id="' + epTableId + '">';
                    html += '<tr>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',1,\'num\')">TTFT P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',2,\'num\')">TTFT vs Agg &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',3,\'num\')">Throughput Mean &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',4,\'num\')">Tput vs Agg &#x21C5;</th>';
                    html += '</tr>';
                    const sorted = [...rec.ep_all_configs].sort((a, b) => (b.throughput_mean||b.throughput_p90||0) - (a.throughput_mean||a.throughput_p90||0));
                    for (const cfg of sorted) {
                        const cfgTput = cfg.throughput_mean || cfg.throughput_p90;
                        if (!cfg.ttft_p90 || !cfgTput) continue;
                        const ttftPct = ((cfg.ttft_p90 - baseTtft) / baseTtft * 100).toFixed(1);
                        const tputPct = ((cfgTput - baseTput) / baseTput * 100).toFixed(1);
                        const ttftBetter = parseFloat(ttftPct) < 0;
                        const tputBetter = parseFloat(tputPct) > 0;
                        const ttftColor = ttftBetter ? '#059669' : '#dc2626';
                        const tputColor = tputBetter ? '#059669' : '#dc2626';
                        const ttftArrow = ttftBetter ? '&#9660;' : '&#9650;';
                        const tputArrow = tputBetter ? '&#9650;' : '&#9660;';
                        const label = cfg.prefill_pods ? `EP ${cfg.prefill_pods}P+${cfg.decode_pods}D PTP=${cfg.prefill_tp} DTP=${cfg.decode_tp}` : `EP TP${cfg.tp} x ${cfg.replicas} replicas`;
                        html += `<tr><td><strong>${label}</strong></td>`;
                        html += `<td data-val="${cfg.ttft_p90}">${cfg.ttft_p90} ms</td>`;
                        html += `<td data-val="${ttftPct}" style="color:${ttftColor}; font-weight:700;">${ttftArrow} ${ttftPct}%</td>`;
                        html += `<td data-val="${cfgTput}">${cfgTput} req/s</td>`;
                        html += `<td data-val="${tputPct}" style="color:${tputColor}; font-weight:700;">${tputArrow} ${tputPct}%</td></tr>`;
                    }
                    html += `<tr class="baseline-row" style="background:#f1f5f9;"><td><strong>${aggBaseline.config_name}</strong> <span style="background:#1f77b4; color:white; font-size:0.65em; padding:1px 5px; border-radius:3px;">BASELINE</span></td>`;
                    html += `<td data-val="${baseTtft}">${baseTtft} ms</td><td data-val="0" style="color:#64748b;">-</td>`;
                    html += `<td data-val="${baseTput}">${baseTput} req/s</td><td data-val="0" style="color:#64748b;">-</td></tr>`;
                    html += '</table></div></div>';
                }
            }
            html += '</div>';
        }
    }

    // Flush comparison (Step 8)
    secCmp = html; html = '';
    var hasCmpData = coreResults.some(r => r.architecture === 'PD' || r.architecture === 'EP') && coreResults.some(r => r.architecture === 'AGGREGATED');
    if (!hasCmpData) secCmp = '';

    // ============================================================
    // STEP 9: Latency-Bounded Throughput Search
    // Binary search over concurrency to find max throughput under SLA
    // ============================================================
    if (data.latency_search && data.latency_search.trials && data.latency_search.trials.length) {
        html += '<div class="chart-card" style="border-left:6px solid #d97706;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#d97706,#f59e0b); color:white;">Latency Search</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Uses <strong>binary search over concurrency</strong> to find the maximum throughput that stays within the TTFT SLA target. ' +
            'Each trial increases or decreases the number of concurrent users based on whether the previous trial met the latency constraint. ' +
            'The result is the highest sustainable load for each architecture under the configured SLA.' +
            '</div></div>';
        const ls = data.latency_search;
        const byArch = ls.by_architecture || {};
        const archKeys = Object.keys(byArch);

        // Get SLA target from first trial
        const firstTrial = ls.trials[0];
        const targetMs = firstTrial.target_ms;
        const targetPct = firstTrial.target_percentile || 'p90';
        const metricKey = 'ttft_' + targetPct;

        html += '<div class="chart-card" style="margin-top:16px; border:2px solid #8b5cf6; border-left:6px solid #8b5cf6;">';
        html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">Step 10: Latency-Bounded Throughput Search</div>';
        html += `<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Binary search over concurrency to find the maximum throughput that keeps TTFT ${targetPct.toUpperCase()} under <strong>${targetMs} ms</strong>.</div>`;

        const archConfigs = ls.arch_configs || {};

        // Summary cards per architecture
        archKeys.forEach((arch, ai) => {
            const trials = byArch[arch];
            // Skip architectures with no valid data
            if (!trials.some(t => t.ttft_p90 != null)) return;
            const passing = trials.filter(t => t.meets_sla);
            const bestPassing = passing.length ? passing.reduce((a, b) => a.concurrency > b.concurrency ? a : b) : null;
            const cfgLabel = archConfigs[arch] || arch.toUpperCase();

            html += `<div style="padding:12px 20px; margin-top:4px;">`;
            html += `<div style="font-weight:700; font-size:1.05em; color:#1e293b; margin-bottom:10px; border-bottom:1px solid #e2e8f0; padding-bottom:6px;">${arch.toUpperCase()}: ${cfgLabel}</div>`;
            if (bestPassing) {
                const latVal = bestPassing[metricKey] != null ? bestPassing[metricKey].toFixed(1) : '-';
                const s9TputKey = 'throughput_' + targetPct;
                const tputVal = bestPassing[s9TputKey] != null ? bestPassing[s9TputKey].toFixed(2) : (bestPassing.throughput_p90 != null ? bestPassing.throughput_p90.toFixed(2) : '-');
                html += '<div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin-bottom:16px;">';
                html += `<div style="background:#f0fdf4; border-radius:10px; padding:16px; text-align:center; border:1px solid #bbf7d0;"><div style="font-size:2em; font-weight:800; color:#059669;">${bestPassing.concurrency}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Optimal Concurrency</div></div>`;
                html += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:2em; font-weight:800; color:#1e293b;">${latVal} <span style="font-size:0.5em; color:#64748b;">ms</span></div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">TTFT ${targetPct.toUpperCase()} at Optimal</div></div>`;
                html += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:2em; font-weight:800; color:#1e293b;">${tputVal}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Throughput ${targetPct.toUpperCase()} (req/s)</div></div>`;
                html += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:2em; font-weight:800; color:#1e293b;">${trials.length}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Tests Run</div></div>`;
                html += '</div>';
            } else {
                html += `<div style="padding:10px 14px; background:#fef2f2; border-radius:8px; color:#991b1b; font-size:0.9em; border:1px solid #fecaca; margin-bottom:12px;">No concurrency level met the SLA target of ${targetMs} ms</div>`;
            }

            // Per-percentile charts (P90, P95, P99)
            html += `<div id="step9-chart-p90-${ai}${_chartSuffix}" style="height:850px; background:#fff; border-radius:8px; border:1px solid #e2e8f0;"></div>`;
            html += `<div id="step9-chart-p95-${ai}${_chartSuffix}" style="height:850px; background:#fff; border-radius:8px; border:1px solid #e2e8f0; margin-top:16px;"></div>`;
            html += `<div id="step9-chart-p99-${ai}${_chartSuffix}" style="height:850px; background:#fff; border-radius:8px; border:1px solid #e2e8f0; margin-top:16px;"></div>`;
            // Legacy div for backward compat (hidden)
            html += `<div id="step9-chart-${ai}${_chartSuffix}" style="display:none;"></div>`;

            // Cost table
            const archTrials = byArch[arch];
            const sortedTrials = [...archTrials].sort((a, b) => a.concurrency - b.concurrency);
            html += '<div style="margin-top:12px; overflow-x:auto;"><table class="results-table" style="font-size:0.85em;">';
            html += '<tr><th>Concurrency</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Throughput P90</th><th>Meets SLA</th><th>Manifests</th></tr>';
            sortedTrials.forEach(t => {
                const slaStyle = t.meets_sla ? 'color:#059669; font-weight:700;' : 'color:#dc2626; font-weight:700;';
                let mLinks = '-';
                if (t.manifest_types && t.manifest_types.length && t.test_id) {
                    mLinks = t.manifest_types.filter(mt => !mt.includes('service')).map(mt =>
                        `<a href="/api/run/${runId}/config/${t.test_id}/manifest/${mt}" title="Download ${mt}.yaml" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${mt}</a>`
                    ).join(' ');
                }
                html += `<tr><td style="font-weight:700;">${t.concurrency}</td>`;
                html += `<td>${t.ttft_p50 != null ? t.ttft_p50.toFixed(1) + ' ms' : '-'}</td>`;
                html += `<td>${t.ttft_p90 != null ? t.ttft_p90.toFixed(1) + ' ms' : '-'}</td>`;
                html += `<td>${t.ttft_p95 != null ? t.ttft_p95.toFixed(1) + ' ms' : '-'}</td>`;
                html += `<td>${t.ttft_p99 != null ? t.ttft_p99.toFixed(1) + ' ms' : '-'}</td>`;
                html += `<td>${t.throughput_p90 != null ? t.throughput_p90.toFixed(2) + ' req/s' : '-'}</td>`;
                html += `<td style="${slaStyle}">${t.meets_sla ? 'Yes' : 'No'}</td>`;
                html += `<td>${mLinks}</td></tr>`;
            });
            html += '</table></div>';

            html += '</div>';
        });

        // Trials table
        html += '<div style="padding:12px 20px 4px; font-weight:700; font-size:0.95em; color:#1e293b; border-top:1px solid #e2e8f0; margin-top:8px;">All Trials</div>';
        html += '<div class="chart-card-body" style="padding:0 20px 16px;"><table class="results-table">';
        const s9TblTputKey = 'throughput_' + targetPct;
        html += `<tr><th>Arch</th><th>#</th><th>Phase</th><th>Concurrency</th><th>TTFT ${targetPct.toUpperCase()}</th><th>Throughput ${targetPct.toUpperCase()}</th><th>Meets SLA</th><th>Manifests</th></tr>`;
        ls.trials.forEach(t => {
            const latVal = t[metricKey] != null ? t[metricKey].toFixed(1) + ' ms' : '-';
            const s9TputRaw = t[s9TblTputKey] != null ? t[s9TblTputKey] : t.throughput_p90;
            const tputVal = s9TputRaw != null ? s9TputRaw.toFixed(2) + ' req/s' : '-';
            const slaStyle = t.meets_sla ? 'color:#059669; font-weight:700;' : 'color:#dc2626; font-weight:700;';
            const slaText = t.meets_sla ? 'Yes' : 'No';
            let mLinks = '-';
            if (t.manifest_types && t.manifest_types.length && t.test_id) {
                mLinks = t.manifest_types.filter(mt => !mt.includes('service')).map(mt =>
                    `<a href="/api/run/${runId}/config/${t.test_id}/manifest/${mt}" title="Download ${mt}.yaml" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${mt}</a>`
                ).join(' ');
            }
            html += `<tr><td>${t.architecture}</td><td>${t.trial_number}</td><td>${t.search_phase}</td>`;
            html += `<td style="font-weight:700;">${t.concurrency}</td>`;
            html += `<td>${latVal}</td><td>${tputVal}</td>`;
            html += `<td style="${slaStyle}">${slaText}</td>`;
            html += `<td>${mLinks}</td></tr>`;
        });
        html += '</table></div></div>';
    }

    // Flush Step 9
    secStep9 = html; html = '';

    // ============================================================
    // STEP 10: Calibrated Load Validation (separate card)
    // Handles PD, EP, or both depending on goal
    // ============================================================
    if (data.calibrated_qps || data.concurrency_sweep) {
        html += '<div class="chart-card" style="border-left:6px solid #059669;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#059669,#34d399); color:white;">Concurrency Sweep</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Validates the best configurations at <strong>increasing concurrency levels</strong> to find the achievable load capacity. ' +
            'Tests run at progressively higher user counts until latency degrades or errors appear. ' +
            'The <strong>calibrated concurrency</strong> (green dashed line on charts) marks the sweet spot — the highest sustainable load where TTFT-to-throughput ratio is optimal, ' +
            'calculated using Little\'s Law from measured queue and service times.' +
            '</div></div>';
    }
    if (data.calibrated_qps) {
        const cal = data.calibrated_qps;
        // Determine primary architecture (PD or EP)
        const primary = cal.pd || cal.ep;
        const primaryKey = cal.pd ? 'pd' : 'ep';
        const primaryLabel = cal.pd ? 'PD' : 'EP';

        html += '<div class="chart-card" style="margin-top:16px; border:2px solid #059669; border-left:6px solid #059669;"><div class="chart-card-header" style="background:linear-gradient(135deg,#059669,#10b981);">Step 11: Concurrency Sweep</div>';
        // Capacity info with math breakdown
        if (cal.gpu_sizing) {
            const s = cal.gpu_sizing;
            html += '<div style="padding:12px 20px; background:#ecfdf5; border-bottom:1px solid #6ee7b7; font-size:0.9em; color:#065f46;">';
            html += `<div style="font-weight:700; margin-bottom:8px;">📊 Cluster Capacity Analysis</div>`;
            html += '<table style="width:auto; margin:0; font-size:0.95em; border:none;">';
            html += '<tr style="background:none;"><td style="border:none; padding:2px 16px 2px 0; color:#047857;"><strong>GPU Cost per Request</strong></td><td style="border:none; padding:2px 0;"></td></tr>';
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Prefill</td><td style="border:none; padding:1px 0;">${s.isl} ISL ÷ ${s.prefill_tpsg} TPSG = <strong>${s.prefill_cost} GPU-sec</strong></td></tr>`;
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Decode</td><td style="border:none; padding:1px 0;">${s.osl} OSL ÷ ${s.decode_tpsg} TPSG = <strong>${s.decode_cost} GPU-sec</strong></td></tr>`;
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Total</td><td style="border:none; padding:1px 0;"><strong>${s.total_cost} GPU-sec/request</strong></td></tr>`;
            html += '<tr style="background:none;"><td style="border:none; padding:6px 16px 2px 0; color:#047857;"><strong>Sustainable Throughput</strong></td><td style="border:none; padding:6px 0 2px;"></td></tr>';
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Cluster capacity</td><td style="border:none; padding:1px 0;">${s.total_gpus} GPUs ÷ ${s.total_cost} GPU-sec ÷ ${s.headroom}x headroom = <strong>${s.sustainable_throughput_rps || s.sustainable_qps} req/s</strong> (${s.sustainable_concurrency || '?'} concurrent users)</td></tr>`;
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Concurrency tested</td><td style="border:none; padding:1px 0;"><strong>${s.concurrency} simultaneous requests</strong></td></tr>`;
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Ideal P/D ratio</td><td style="border:none; padding:1px 0;"><strong>${s.ideal_prefill_pct}% prefill</strong></td></tr>`;
            html += '</table></div>';
        } else if (cal.total_gpus_available && cal.requested_rps) {
            html += `<div style="padding:10px 20px; background:#ecfdf5; border-bottom:1px solid #6ee7b7; font-size:0.9em; color:#065f46;">📊 Cluster can sustain <strong>${cal.requested_rps} req/s</strong> with <strong>${cal.total_gpus_available} GPUs</strong>.</div>`;
        }
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Steps 7-8 ran at the original user-requested concurrency which exceeded cluster capacity. This step re-tested the best configurations at a sustainable load to show realistic latency and throughput.</div>';

        // --- Load Analysis per architecture ---
        if (cal.calibration_analysis) {
            const ca = cal.calibration_analysis;
            html += '<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b; margin-top:8px;">Load Analysis (from measured Step 7 data)</div>';
            html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            html += '<tr><th>Architecture</th><th>Throughput</th><th>Response Time</th><th>Service Time</th><th>Queue Time</th><th>Utilization</th><th>Calibrated Load</th></tr>';
            ['pd', 'aggregated'].forEach(arch => {
                const a = ca[arch];
                if (!a) return;
                const label = arch === 'pd' ? 'PD' : 'Aggregated';
                const queuePct = a.queue_pct || 0;
                const queueColor = queuePct > 80 ? '#dc2626' : (queuePct > 50 ? '#d97706' : '#059669');
                html += `<tr>`;
                html += `<td style="font-weight:700;">${label}</td>`;
                html += `<td>${a.throughput_mean} req/s <span style="color:#94a3b8;">@ c=${a.concurrency_tested}</span></td>`;
                html += `<td>${(a.response_time_s * 1000).toFixed(0)}ms</td>`;
                html += `<td>${a.service_time_ms.toFixed(0)}ms</td>`;
                html += `<td style="color:${queueColor}; font-weight:600;">${a.queue_time_ms.toFixed(0)}ms (${queuePct}%)</td>`;
                html += `<td>${a.utilization_pct}%</td>`;
                html += `<td style="font-weight:700; color:#059669;">${a.calibrated_concurrency} users</td>`;
                html += `</tr>`;
            });
            html += '</table></div>';
            html += '<div style="padding:4px 20px 12px; font-size:0.82em; color:#64748b;">At the tested concurrency, most time is spent in queue — requests wait for GPU availability rather than being processed. The calibrated load reduces concurrency until queue time is reasonable (~2× service time), showing realistic per-request latency.</div>';
        }

        // --- Table 1: Percentile Breakdown at Calibrated Load ---
        const isBalanced = !!(cal.pd && cal.ep);
        const requestedRps = cal.requested_rps != null ? cal.requested_rps : null;
        const rpsLabel = requestedRps != null ? ` at ${Math.round(requestedRps)} concurrent` : '';

        // Collect entries
        const calEntries = [];
        if (cal.pd) calEntries.push({label: 'PD', entry: cal.pd});
        if (cal.epp_pd) calEntries.push({label: 'PD (EPP Tuned)', entry: cal.epp_pd});
        if (cal.aggregated) calEntries.push({label: 'Aggregated', entry: cal.aggregated});
        if (cal.epp_agg) calEntries.push({label: 'Aggregated (EPP Tuned)', entry: cal.epp_agg});
        if (isBalanced && cal.ep) calEntries.push({label: 'EP', entry: cal.ep});

        // Helper: find best P90 value per metric for highlighting
        function findBest(metric, lowerIsBetter) {
            const vals = calEntries.map(e => e.entry[metric]).filter(v => v != null);
            if (!vals.length) return null;
            return lowerIsBetter ? Math.min(...vals) : Math.max(...vals);
        }
        const bestTtft = findBest('ttft_p90', true);
        const bestItl = findBest('itl_p90', true);
        const hl = (val, best) => val != null && val === best ? 'color:#059669; font-weight:700;' : '';

        // --- Table 1a: TTFT + ITL Percentile Breakdown ---
        html += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b;">Latency Breakdown${rpsLabel}</div>`;
        html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
        html += '<tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';
        calEntries.forEach(({label, entry}, idx) => {
            const metrics = [
                {name: 'TTFT (ms)', p50: entry.ttft_p50, p90: entry.ttft_p90, p95: entry.ttft_p95, p99: entry.ttft_p99, best: bestTtft, p90key: 'ttft_p90'},
                {name: 'ITL (ms)', p50: entry.itl_p50, p90: entry.itl_p90, p95: entry.itl_p95, p99: entry.itl_p99, best: bestItl, p90key: 'itl_p90'},
            ];
            metrics.forEach((m, mi) => {
                const borderStyle = mi === 0 && idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
                const rowspan = mi === 0 ? ` rowspan="2" style="vertical-align:middle; font-weight:700;${borderStyle}"` : '';
                html += '<tr>';
                if (mi === 0) html += `<td${rowspan}>${label}</td>`;
                html += `<td style="color:#64748b;${borderStyle}">${m.name}</td>`;
                html += `<td style="${borderStyle}">${m.p50 != null ? m.p50 : '-'}</td>`;
                html += `<td style="${hl(entry[m.p90key], m.best)}${borderStyle}">${m.p90 != null ? m.p90 : '-'}</td>`;
                html += `<td style="${borderStyle}">${m.p95 != null ? m.p95 : '-'}</td>`;
                html += `<td style="${borderStyle}">${m.p99 != null ? m.p99 : '-'}</td>`;
                html += '</tr>';
            });
        });
        html += '</table></div>';

        // --- Table 1b: Throughput (mean only) ---
        const bestTputMean = Math.max(...calEntries.map(e => e.entry.throughput_mean || e.entry.throughput_p50 || 0));
        html += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b; margin-top:8px;">Throughput${rpsLabel}</div>`;
        html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
        html += '<tr><th>Configuration</th><th>Throughput Mean (req/s)</th></tr>';
        calEntries.forEach(({label, entry}, idx) => {
            const tputMean = entry.throughput_mean || entry.throughput_p50 || '-';
            const isBest = tputMean === bestTputMean;
            const style = isBest ? 'color:#059669; font-weight:700;' : '';
            const border = idx > 0 ? ' border-top:1px solid #e2e8f0;' : '';
            html += `<tr><td style="font-weight:700;${border}">${label}</td><td style="${style}${border}">${tputMean} req/s</td></tr>`;
        });
        html += '</table></div>';

        // --- Table 2: Overload Impact ---
        const origConcurrency = cal.concurrency != null ? `${cal.concurrency} concurrent` : '-';
        const calConcurrency = requestedRps != null ? `${Math.round(requestedRps)} concurrent` : '-';
        // Group overload comparisons by architecture family
        const overloadGroups = [];
        if (cal.pd && cal.overloaded_pd) {
            var group = {title: 'PD', rows: [{label: 'PD', cal: cal.pd, over: cal.overloaded_pd}]};
            if (cal.epp_pd) group.rows.push({label: 'PD (EPP Tuned)', cal: cal.epp_pd, over: cal.overloaded_pd});
            overloadGroups.push(group);
        }
        if (cal.aggregated && cal.overloaded_agg) {
            var group = {title: 'Aggregated', rows: [{label: 'Aggregated', cal: cal.aggregated, over: cal.overloaded_agg}]};
            if (cal.epp_agg) group.rows.push({label: 'Aggregated (EPP Tuned)', cal: cal.epp_agg, over: cal.overloaded_agg});
            overloadGroups.push(group);
        }
        overloadGroups.forEach(function(group) {
            html += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b; margin-top:12px;">Overload Impact: ${group.title}</div>`;
            html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            html += '<tr><th>Configuration</th><th>Load</th><th>TTFT P90</th><th>Throughput Mean</th></tr>';
            group.rows.forEach(function(row) {
                var calTputMean = row.cal.throughput_mean || row.cal.throughput_p50 || '-';
                var overTputMean = row.over.throughput_mean || row.over.throughput_p90 || '-';
                html += `<tr><td><strong>${row.label} (calibrated)</strong></td><td>${calConcurrency}</td><td style="color:#059669; font-weight:700;">${row.cal.ttft_p90} ms</td><td style="color:#059669; font-weight:700;">${calTputMean} req/s</td></tr>`;
                html += `<tr><td><strong>${row.label} (overloaded)</strong></td><td>${origConcurrency}</td><td style="color:#94a3b8;">${row.over.ttft_p90} ms</td><td style="color:#94a3b8;">${overTputMean} req/s</td></tr>`;
            });
            html += '</table></div>';
        });
        html += '</div>';
    }

    // Concurrency Sweep Charts — one section per architecture
    // Build config labels for sweep chart titles (used by both concurrency and cache sweeps)
    // Derive from all_results by matching sweep test_ids
    var sweepConfigLabels = {};
    function findSweepConfigLabel(testIdPrefix) {
        var match = (data.all_results || []).find(function(r) {
            return r.test_id && r.test_id.indexOf(testIdPrefix) === 0;
        });
        if (!match) return '';
        if (match.architecture === 'PD' || match.architecture === 'EP') {
            return match.prefill_pods + 'P×TP' + match.prefill_tp + ' + ' + match.decode_pods + 'D×TP' + match.decode_tp;
        }
        var reps = match.replicas || (match.gpus && match.tp ? Math.floor(match.gpus / match.tp) : '?');
        return reps + '×TP' + (match.tp || '?') + ((match.pp && match.pp > 1) ? ' PPx' + match.pp : '');
    }
    sweepConfigLabels.pd = findSweepConfigLabel('step11-sweep-pd');
    sweepConfigLabels.aggregated = findSweepConfigLabel('step11-sweep-aggregated');
    sweepConfigLabels.ep = findSweepConfigLabel('step11-sweep-ep');
    // Also try cache sweep test_ids for cache-only runs
    if (!sweepConfigLabels.pd) sweepConfigLabels.pd = findSweepConfigLabel('step13-cache-pd');
    if (!sweepConfigLabels.aggregated) sweepConfigLabels.aggregated = findSweepConfigLabel('step13-cache-aggregated');
    if (!sweepConfigLabels.ep) sweepConfigLabels.ep = findSweepConfigLabel('step13-cache-ep');

    if (data.concurrency_sweep) {
        // Filter discarded tests from sweep charts
        var _discIds = {};
        (data.all_results || []).forEach(function(r) { if (r.quality === 'discard') _discIds[r.test_id || r.config_name] = true; });
        var sweep = {};
        Object.keys(data.concurrency_sweep).forEach(function(k) {
            var pts = data.concurrency_sweep[k];
            if (pts) sweep[k] = pts.filter(function(p) { return !_discIds[p.test_id || '']; });
        });
        var pctColors = { ttft_p90: '#3b82f6', ttft_p95: '#f59e0b', ttft_p99: '#ef4444' };
        var archIdx = 0;
        function sweepLabel(key) {
            if (key.startsWith('pd')) return 'PD';
            if (key.startsWith('agg')) return 'AG';
            if (key.startsWith('ep')) return 'EP';
            return key;
        }

        // Collect all configs with their labels and colors
        var allSweepConfigs = [];
        var configColors = ['#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16', '#f97316', '#6366f1'];
        var colorIdx = 0;
        Object.keys(sweep).forEach(function(arch) {
            var points = sweep[arch];
            if (!points || !points.length) return;
            var rawLabel = points[0] && points[0].config_label ? points[0].config_label : sweepLabel(arch);
            var archTag = arch.startsWith('pd') ? ' (PD)' : arch.startsWith('ep') ? ' (EP)' : ' (AG)';
            var label = rawLabel.indexOf('(') >= 0 ? rawLabel : rawLabel + archTag;
            allSweepConfigs.push({ key: arch, label: label, points: points, color: configColors[colorIdx % configColors.length] });
            colorIdx++;
        });

        if (allSweepConfigs.length > 0) {
            // --- TTFT charts: one per percentile, all configs as lines ---
            [{ key: 'ttft_p90', label: 'TTFT P90', title: 'TTFT P90 vs Concurrency' },
             { key: 'ttft_p95', label: 'TTFT P95', title: 'TTFT P95 vs Concurrency' },
             { key: 'ttft_p99', label: 'TTFT P99', title: 'TTFT P99 vs Concurrency' }].forEach(function(pct) {
                var chartId = 'chart-sweep-' + pct.key;
                html += '<div class="chart-card" style="margin-top:16px;">';
                html += '<div class="chart-card-header">' + pct.title + '</div>';
                html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">' + pct.label + ' across all tested configurations. Each line is a different deployment config. Lower is better.</div>';
                html += '<div id="' + chartId + '" style="width:100%; height:850px;"></div>';
                html += '</div>';

                chartQueue.push(function() {
                    var traces = [];
                    var calShapes = [];
                    allSweepConfigs.forEach(function(cfg) {
                        var hasData = cfg.points.some(function(p) { return p[pct.key]; });
                        if (!hasData) return;
                        traces.push({
                            x: cfg.points.map(function(p) { return p.concurrency; }),
                            y: cfg.points.map(function(p) { return p[pct.key] || 0; }),
                            text: cfg.points.map(function(p) { return Math.round(p[pct.key] || 0).toLocaleString(); }),
                            textposition: 'top center', textfont: { size: 13, color: cfg.color },
                            mode: 'lines+markers+text', name: cfg.label,
                            line: { color: cfg.color, width: 3 },
                            marker: { size: 8 },
                            hovertemplate: '<b>' + cfg.label + '</b><br>%{x} users<br>' + pct.label + ': %{y:.0f}ms<extra></extra>'
                        });
                        cfg.points.forEach(function(p) {
                            if (p.is_calibrated) {
                                var calIdx = calShapes.length;
                                calShapes.push({ type: 'line', x0: p.concurrency, x1: p.concurrency, y0: 0, y1: 1, yref: 'paper',
                                    line: { color: cfg.color, width: 2, dash: 'dash' } });
                                calShapes._annotations = calShapes._annotations || [];
                                calShapes._annotations.push({
                                    x: p.concurrency, y: 1.0 - calIdx * 0.06, yref: 'paper',
                                    text: cfg.label + ' c=' + p.concurrency,
                                    showarrow: false, xshift: 8,
                                    font: { size: 12, color: cfg.color }, xanchor: 'left'
                                });
                            }
                        });
                    });
                    if (calShapes.length) {
                        traces.push({ x: [calShapes[0].x0, calShapes[0].x0], y: [null, null], mode: 'lines',
                            name: 'Calibrated', line: { color: '#059669', width: 1.5, dash: 'dash' }, showlegend: true });
                    }
                    var allXvals = []; traces.forEach(function(t){ if(t.x) t.x.forEach(function(v){ if(allXvals.indexOf(v)===-1) allXvals.push(v); }); }); allXvals.sort(function(a,b){return a-b;});
                    Plotly.newPlot(chartId, traces, {
                        xaxis: { title: 'Concurrent Users', gridcolor: '#e2e8f0', tickmode: 'array', tickvals: allXvals },
                        yaxis: { title: pct.label + ' (ms)', gridcolor: '#e2e8f0' },
                        plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                        margin: { t: 20, b: 60, l: 70, r: 20 },
                        legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.9)' },
                        shapes: calShapes, annotations: calShapes._annotations || [], hovermode: 'closest'
                    }, { responsive: true });
                });
            });

            // --- Throughput per GPU chart: all configs as lines ---
            html += '<div class="chart-card" style="margin-top:16px;">';
            html += '<div class="chart-card-header">Throughput per GPU vs Concurrency</div>';
            html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">Token throughput per GPU across all configs. Each line is a different deployment config. Higher is better.</div>';
            html += '<div id="chart-sweep-tput-all" style="width:100%; height:850px;"></div>';
            html += '</div>';

            chartQueue.push(function() {
                var traces = [];
                var calShapes = [];
                allSweepConfigs.forEach(function(cfg) {
                    traces.push({
                        x: cfg.points.map(function(p) { return p.concurrency; }),
                        y: cfg.points.map(function(p) { return p.throughput_per_gpu; }),
                        text: cfg.points.map(function(p) { return Math.round(p.throughput_per_gpu).toLocaleString(); }),
                        textposition: 'top center', textfont: { size: 13, color: cfg.color },
                        mode: 'lines+markers+text', name: cfg.label,
                        line: { color: cfg.color, width: 3 },
                        marker: { size: 8 },
                        hovertemplate: '<b>' + cfg.label + '</b><br>%{x} users<br>%{y:.0f} tok/s/gpu<extra></extra>'
                    });
                    cfg.points.forEach(function(p) {
                        if (p.is_calibrated) {
                            var calIdx = calShapes.length;
                            calShapes.push({ type: 'line', x0: p.concurrency, x1: p.concurrency, y0: 0, y1: 1, yref: 'paper',
                                line: { color: cfg.color, width: 2, dash: 'dash' } });
                            calShapes._annotations = calShapes._annotations || [];
                            calShapes._annotations.push({
                                x: p.concurrency, y: 1.0 - calIdx * 0.06, yref: 'paper',
                                text: cfg.label + ' c=' + p.concurrency,
                                showarrow: false, xshift: 8,
                                font: { size: 12, color: cfg.color }, xanchor: 'left'
                            });
                        }
                    });
                });
                if (calShapes.length) {
                    traces.push({ x: [calShapes[0].x0, calShapes[0].x0], y: [null, null], mode: 'lines',
                        name: 'Calibrated', line: { color: '#059669', width: 1.5, dash: 'dash' }, showlegend: true });
                }
                var allXvals2 = []; traces.forEach(function(t){ if(t.x) t.x.forEach(function(v){ if(allXvals2.indexOf(v)===-1) allXvals2.push(v); }); }); allXvals2.sort(function(a,b){return a-b;});
                Plotly.newPlot('chart-sweep-tput-all', traces, {
                    xaxis: { title: 'Concurrent Users', gridcolor: '#e2e8f0', tickmode: 'array', tickvals: allXvals2 },
                    yaxis: { title: 'Token Throughput per GPU (tok/s/gpu)', gridcolor: '#e2e8f0' },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    margin: { t: 20, b: 60, l: 70, r: 20 },
                    legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.9)' },
                    shapes: calShapes, annotations: calShapes._annotations || [], hovermode: 'closest'
                }, { responsive: true });
            });
        }

        // --- vLLM Token Throughput chart (GPU Computed vs Server Total) per config ---
        if (allSweepConfigs.some(function(cfg) { return cfg.points.some(function(p) { return p.vllm_tps; }); })) {
            html += '<div class="chart-card" style="margin-top:16px;">';
            html += '<div class="chart-card-header">vLLM Token Throughput vs Concurrency</div>';
            html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">' +
                '<strong>Solid line:</strong> Server total (prompt + generation from Prometheus). ' +
                '<strong>Dashed line:</strong> Output tokens only (generation). ' +
                'The gap reflects prefix cache efficiency — cached prompt tokens inflate the server total.</div>';
            html += '<div id="chart-sweep-vllm-tps" style="width:100%; height:850px;"></div>';
            html += '</div>';

            chartQueue.push(function() {
                var vtTraces = [];
                allSweepConfigs.forEach(function(cfg) {
                    vtTraces.push({
                        x: cfg.points.map(function(p) { return p.concurrency; }),
                        y: cfg.points.map(function(p) { return p.vllm_tps || 0; }),
                        name: cfg.label + ' (server total)',
                        mode: 'lines+markers', line: { color: cfg.color, width: 3 }, marker: { size: 8, color: cfg.color },
                        hovertemplate: '<b>' + cfg.label + '</b><br>%{x} users<br>Server: %{y:,.0f} tok/s<extra></extra>'
                    });
                    vtTraces.push({
                        x: cfg.points.map(function(p) { return p.concurrency; }),
                        y: cfg.points.map(function(p) { return p.output_tps_mean || 0; }),
                        name: cfg.label + ' (output only)',
                        mode: 'lines+markers', line: { color: cfg.color, width: 2, dash: 'dot' }, marker: { size: 6, color: cfg.color },
                        hovertemplate: '<b>' + cfg.label + '</b><br>%{x} users<br>Output: %{y:,.0f} tok/s<extra></extra>',
                        showlegend: false
                    });
                });
                Plotly.newPlot('chart-sweep-vllm-tps', vtTraces, {
                    xaxis: { title: 'Concurrent Users', gridcolor: '#e2e8f0' },
                    yaxis: { title: 'Total Tokens/s', gridcolor: '#e2e8f0' },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    margin: { t: 20, b: 60, l: 70, r: 20 },
                    legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.9)' },
                    hovermode: 'closest'
                }, { responsive: true });
            });
        }

        // --- Token Throughput: GPU Computed vs Server Total vs Client (sweep tests only) ---
        var sweepTkRes = (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || '';
            if (tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0) return false;
            var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
            var pm = m.prometheus_metrics || {};
            return (pm.vllm_generation_tokens_rate || {}).avg || r.output_tps_mean;
        });
        if (sweepTkRes.length) {
            html += '<div class="chart-card" style="margin-top:16px;">';
            html += '<div class="chart-card-header">Token Throughput — GPU Computed vs Server Total vs Client</div>';
            html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">' +
                '<strong>GPU computed:</strong> Prompt tokens actually computed (excludes cached) + generation. ' +
                '<strong>Server total:</strong> All tokens processed by vLLM (incl. cached prefixes). ' +
                '<strong>Client:</strong> Tokens measured by the load tester.</div>';
            html += '<div id="chart-sweep-token-tput" style="width:100%; height:850px;"></div>';
            html += '</div>';

            chartQueue.push(function() {
                sweepTkRes.sort(function(a, b) {
                    var aArch = a.architecture || ''; var bArch = b.architecture || '';
                    if (aArch !== bArch) return aArch.localeCompare(bArch);
                    return (a.concurrency || 0) - (b.concurrency || 0);
                });
                var tkLabels = sweepTkRes.map(function(r) {
                    return _tagLabel(r.config_name, r.architecture) + ' c=' + (r.concurrency || '?');
                });
                var tkComputed = [], tkServer = [], tkClient = [];
                sweepTkRes.forEach(function(r) {
                    var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                    var pm = m.prometheus_metrics || {};
                    var pAvg = (pm.vllm_prompt_tokens_rate || {}).avg || 0;
                    var gAvg = (pm.vllm_generation_tokens_rate || {}).avg || 0;
                    tkServer.push(Math.round(pAvg + gAvg));
                    var cacheHits = (pm.vllm_prefix_cache_hits_rate || {}).avg || 0;
                    var cacheQueries = (pm.vllm_prefix_cache_queries_rate || {}).avg || 0;
                    var cacheHitRate = cacheQueries > 0 ? cacheHits / cacheQueries : 0;
                    tkComputed.push(Math.round(pAvg * (1 - cacheHitRate)) + Math.round(gAvg));
                    var tput = m.throughput_mean || r.throughput_mean || 0;
                    var pTps = (m.prompt_tokens_mean || 0) * tput;
                    var gTps = (m.output_tokens_mean || 0) * tput;
                    tkClient.push(Math.round(pTps + gTps));
                });
                Plotly.newPlot('chart-sweep-token-tput', [
                    { x: tkLabels, y: tkComputed, name: 'GPU computed', type: 'bar', marker: { color: '#3b82f6' }, text: tkComputed.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 12, color: '#1e293b' } },
                    { x: tkLabels, y: tkServer, name: 'Server total (incl. cached)', type: 'bar', marker: { color: '#93c5fd' }, text: tkServer.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 12, color: '#1e293b' } },
                    { x: tkLabels, y: tkClient, name: 'Client (guidellm)', type: 'bar', marker: { color: '#f59e0b' }, text: tkClient.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 12, color: '#1e293b' } }
                ], {
                    ...plotlyLayout, height: 850, margin: { t: 60, b: 160, l: 60, r: 20 },
                    barmode: 'group', xaxis: { tickangle: -45 },
                    yaxis: { title: 'Total Tokens/s (Prompt + Generation)' },
                    showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' }
                }, { responsive: true });
            });
        }

        // --- Cache Hit Rate vs Concurrency ---
        var hasSweepCacheHit = false;
        Object.keys(sweep).forEach(function(k) { if (sweep[k] && sweep[k].some(function(p) { return p.cache_hit_pct != null; })) hasSweepCacheHit = true; });
        if (hasSweepCacheHit) {
            html += '<div class="chart-card" style="margin-top:20px;">';
            html += '<div class="chart-card-header">Prefix Cache Hit Rate vs Concurrency</div>';
            html += '<div style="padding:8px 20px 0;color:#1e293b;font-size:0.92em;">How prefix cache hit rate changes with load. Higher hit rates mean more requests served from cached KV data.</div>';
            html += '<div id="chart-sweep-cache-hit' + _chartSuffix + '" style="width:100%;height:850px;"></div></div>';
            chartQueue.push(function() {
                var traces = [];
                var cIdx = 0;
                var colors = ['#059669', '#3b82f6', '#f59e0b', '#8b5cf6', '#dc2626', '#0ea5e9'];
                Object.keys(sweep).forEach(function(k) {
                    var pts = sweep[k];
                    if (!pts || !pts.some(function(p) { return p.cache_hit_pct != null; })) return;
                    var label = pts[0] && pts[0].config_label ? pts[0].config_label : k;
                    var color = colors[cIdx % colors.length]; cIdx++;
                    traces.push({
                        x: pts.map(function(p) { return p.concurrency; }),
                        y: pts.map(function(p) { return p.cache_hit_pct != null ? p.cache_hit_pct : 0; }),
                        text: pts.map(function(p) { return p.cache_hit_pct != null ? p.cache_hit_pct.toFixed(1) + '%' : ''; }),
                        textposition: 'top center', textfont: { size: 13, color: color },
                        mode: 'lines+markers+text', name: label,
                        line: { color: color, width: 3 }, marker: { size: 8 }
                    });
                });
                Plotly.newPlot('chart-sweep-cache-hit' + _chartSuffix, traces, {
                    ...plotlyLayout, height: 850,
                    xaxis: { title: 'Concurrency', gridcolor: '#e2e8f0' },
                    yaxis: { title: 'Cache Hit %', gridcolor: '#e2e8f0', range: [0, 100] },
                    showlegend: true, legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.9)' },
                    margin: { t: 20, b: 60, l: 60, r: 20 },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                }, { responsive: true });
            });
        }

        // --- Sweep Pareto charts ---
        html += '<div class="chart-card" style="margin-top:20px;"><div class="chart-card-header">Pareto — Throughput vs Interactivity</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Trade-off between per-user token speed (X) and GPU throughput efficiency (Y) across concurrency levels. Top-right is ideal.</div>' +
            '<div class="chart-card-body"><div id="chart-pareto-sweep-interactivity' + _chartSuffix + '" style="width:100%;height:850px;"></div></div></div>';

        html += '<div class="chart-card" style="margin-top:16px;"><div class="chart-card-header">Pareto — Throughput vs TTFT</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Trade-off between time to first token (X, lower/right is better) and GPU throughput efficiency (Y). Top-right is ideal.</div>' +
            '<div class="chart-card-body"><div id="chart-pareto-sweep-ttft' + _chartSuffix + '" style="width:100%;height:850px;"></div></div></div>';

        // --- Architecture Comparison Charts ---
        html += '<div class="chart-card" style="margin-top:20px;"><div class="chart-card-header">Best Envelope — PD vs Aggregated</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Pareto frontier of each architecture\'s best throughput/GPU at each concurrency level. The higher curve is the better architecture.</div>' +
            '<div class="chart-card-body"><div id="chart-sweep-envelope' + _chartSuffix + '" style="width:100%;height:850px;"></div></div></div>';

        html += '<div class="chart-card" style="margin-top:16px;"><div class="chart-card-header">Architecture Comparison — Overlaid</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Combined view: frontier lines for each architecture plus markers showing which won at each concurrency level.</div>' +
            '<div class="chart-card-body"><div id="chart-sweep-overlaid' + _chartSuffix + '" style="width:100%;height:850px;"></div></div></div>';

        // --- vLLM Engine Metrics per config ---
        html += '<div class="chart-card" style="margin-top:16px;"><div class="chart-card-header">vLLM Engine Metrics vs Concurrency</div>';
        html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">KV cache usage and running requests across concurrency levels per config.</div>';
        html += '<div id="chart-sweep-engine' + _chartSuffix + '" style="width:100%;height:850px;"></div></div>';

        chartQueue.push(function() {
            var _arLookup = {};
            (data.all_results || []).forEach(function(r) { _arLookup[r.test_id] = r; });
            var labels = [], tickColors = [], kvAvg = [], kvMax = [], runAvg = [], runMax = [], runSpread = [], waitAvg = [], prefillT = [], decodeT = [], preempt = [];
            allSweepConfigs.forEach(function(cfg) {
                cfg.points.forEach(function(p) {
                    var r = _arLookup[p.test_id] || {};
                    var mj = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                    var pm = mj.prometheus_metrics || {};
                    labels.push(cfg.label + ' c=' + p.concurrency);
                    tickColors.push(cfg.color);
                    kvAvg.push(((pm.vllm_kv_cache_pct || {}).avg || 0) * 100);
                    kvMax.push(((pm.vllm_kv_cache_pct || {}).max || 0) * 100);
                    runAvg.push((pm.vllm_requests_running || {}).avg || 0);
                    runMax.push((pm.vllm_requests_running || {}).max || 0);
                    var _rAvg = (pm.vllm_requests_running || {}).avg || 0;
                    var _rMax = (pm.vllm_requests_running || {}).max || 0;
                    runSpread.push(_rMax - _rAvg);
                    waitAvg.push((pm.vllm_requests_waiting || {}).avg || 0);
                    prefillT.push((pm.vllm_prefill_time_rate || {}).avg || 0);
                    decodeT.push((pm.vllm_decode_time_rate || {}).avg || 0);
                    preempt.push((pm.vllm_preemptions_rate || {}).avg || 0);
                });
            });
            if (!labels.length) return;
            var traces = [
                { x: labels, y: kvAvg, name: 'KV Cache Avg %', type: 'bar', marker: { color: '#3b82f6' } },
                { x: labels, y: kvMax, name: 'KV Cache Max %', type: 'bar', marker: { color: '#93c5fd' } },
                { x: labels, y: runAvg, name: 'Avg Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 3 }, marker: { size: 8 } },
                { x: labels, y: runMax, name: 'Max Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 2, dash: 'dash' }, marker: { size: 6, symbol: 'triangle-up' } },
                { x: labels, y: runSpread, name: 'Load Spread', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 2, dash: 'dashdot' }, marker: { size: 6, symbol: 'diamond' } },
                { x: labels, y: waitAvg, name: 'Avg Waiting Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#ef4444', width: 2, dash: 'dot' }, marker: { size: 6 } },
                { x: labels, y: decodeT, name: 'Decode req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#f59e0b', width: 2 }, marker: { size: 6 } },
                { x: labels, y: prefillT, name: 'Prefill req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#8b5cf6', width: 2 }, marker: { size: 6 } },
            ];
            if (preempt.some(function(v) { return v > 0; })) {
                traces.push({ x: labels, y: preempt, name: 'Preemptions/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 3 }, marker: { size: 8, symbol: 'x' } });
            }
            // Use numeric x positions so we can color tick labels via annotations
            var xIdx = labels.map(function(_, i) { return i; });
            traces.forEach(function(t) { t.x = xIdx; });
            var tickAnns = labels.map(function(lbl, i) {
                return {
                    x: i, y: -0.02, xref: 'x', yref: 'paper', text: lbl,
                    showarrow: false, font: { size: 14, color: tickColors[i] || '#333', weight: 700 },
                    textangle: -45, xanchor: 'right', yanchor: 'top'
                };
            });
            Plotly.newPlot('chart-sweep-engine' + _chartSuffix, traces, {
                barmode: 'group',
                xaxis: { showticklabels: false, title: '' },
                yaxis: { title: 'KV Cache %', side: 'left', rangemode: 'tozero', gridcolor: '#e2e8f0' },
                yaxis2: { title: 'Count / Rate', overlaying: 'y', side: 'right', rangemode: 'tozero' },
                legend: { x: 0, y: 1.15, orientation: 'h' },
                margin: { t: 40, b: 320, l: 60, r: 60 },
                plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                hovermode: 'x unified',
                height: 850,
                annotations: tickAnns,
            }, { responsive: true, toImageButtonOptions: { format: 'png', height: 1400, width: 1600, scale: 2 } });
        });

        // --- Sweep Results Table ---
        html += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #64748b;">';
        html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#475569,#64748b); color:white; font-size:1.1em;">Concurrency Sweep Data</div>';
        html += '<div style="padding:8px 16px;display:flex;gap:16px;font-size:0.8em;color:#64748b;border-bottom:1px solid #e2e8f0;">';
        html += '<span><span style="display:inline-block;width:10px;height:10px;background:#ecfdf5;border:1px solid #059669;border-radius:2px;vertical-align:middle;margin-right:2px;"></span> Standout TTFT P99 improvement vs other architecture</span>';
        html += '</div>';
        var csSweepTblId = 'concurrency-sweep-tbl-' + runId;
        html += '<div class="chart-card-body" style="padding:0;"><table class="results-table" id="' + csSweepTblId + '">';
        html += '<tr>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',0,\'str\')">Architecture &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',1,\'num\')">Users &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',2,\'num\')">TTFT P50 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',3,\'num\')">TTFT P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',4,\'num\')">TTFT P95 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',5,\'num\')">TTFT P99 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',6,\'num\')">Throughput &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',7,\'num\')">tok/s/user &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',8,\'num\')">tok/s/gpu &#x21C5;</th>';
        html += '</tr>';

        // Build lookup: concurrency → {arch: point} for cross-arch comparison
        var byConcurrency = {};
        Object.keys(sweep).forEach(function(arch) {
            var points = sweep[arch];
            if (!points || !points.length) return;
            points.forEach(function(p) {
                if (!byConcurrency[p.concurrency]) byConcurrency[p.concurrency] = {};
                byConcurrency[p.concurrency][arch] = p;
            });
        });

        // Find best TTFT P99 per concurrency across archs, compute improvement %
        var winners = {};
        Object.keys(byConcurrency).forEach(function(c) {
            var entries = byConcurrency[c];
            var archs = Object.keys(entries);
            if (archs.length < 2) return;
            var hasDisagg = archs.some(function(a) { return a === 'pd' || a === 'ep'; });
            var hasAgg = archs.indexOf('aggregated') >= 0;
            var hasPdAndEp = archs.indexOf('pd') >= 0 && archs.indexOf('ep') >= 0;
            if (!((hasDisagg && hasAgg) || hasPdAndEp)) return;
            var bestTtft = null;
            archs.forEach(function(a) {
                if (entries[a].ttft_p99 > 0 && (!bestTtft || entries[a].ttft_p99 < entries[bestTtft].ttft_p99)) bestTtft = a;
            });
            if (bestTtft) winners[c] = bestTtft;
        });

        // Compute all TTFT improvement percentages to find the standout threshold
        var allTtftPcts = [];
        Object.keys(winners).forEach(function(c) {
            var best = byConcurrency[c][winners[c]];
            Object.keys(byConcurrency[c]).forEach(function(a) {
                if (a !== winners[c] && byConcurrency[c][a].ttft_p99 > 0) {
                    allTtftPcts.push((byConcurrency[c][a].ttft_p99 - best.ttft_p99) / byConcurrency[c][a].ttft_p99 * 100);
                }
            });
        });
        function percentile75(arr) {
            if (!arr.length) return Infinity;
            var s = arr.slice().sort(function(a, b) { return a - b; });
            return s[Math.min(Math.floor(s.length * 0.75), s.length - 1)];
        }
        var ttftThreshold = percentile75(allTtftPcts);

        Object.keys(sweep).forEach(function(arch) {
            var points = sweep[arch];
            if (!points || !points.length) return;
            var archBase = arch.startsWith('pd') ? 'PD' : (arch.startsWith('agg') ? 'AG' : 'EP');
            var configLbl = (points[0] && points[0].config_label) ? points[0].config_label : '';
            var archLabel = configLbl ? archBase + ' (' + configLbl + ')' : archBase;
            points.forEach(function(p) {
                var isWinner = winners[p.concurrency] === arch;
                var isStandout = false;
                if (isWinner && byConcurrency[p.concurrency]) {
                    Object.keys(byConcurrency[p.concurrency]).forEach(function(a) {
                        if (a !== arch && byConcurrency[p.concurrency][a].ttft_p99 > 0) {
                            var pct = (byConcurrency[p.concurrency][a].ttft_p99 - p.ttft_p99) / byConcurrency[p.concurrency][a].ttft_p99 * 100;
                            if (pct >= ttftThreshold) isStandout = true;
                        }
                    });
                }

                var calBadge = p.is_calibrated ? ' <span style="background:#059669;color:white;font-size:0.7em;padding:1px 5px;border-radius:3px;">calibrated</span>' : '';
                var rowStyle = isStandout ? ' style="background:#ecfdf5;"' : '';
                html += '<tr' + rowStyle + '>';
                html += '<td>' + archLabel + '</td>';
                html += '<td>' + p.concurrency + calBadge + '</td>';
                html += '<td>' + (p.ttft_p50 ? p.ttft_p50.toFixed(0) : '-') + ' ms</td>';
                html += '<td>' + (p.ttft_p90 ? p.ttft_p90.toFixed(0) : '-') + ' ms</td>';
                html += '<td>' + (p.ttft_p95 ? p.ttft_p95.toFixed(0) : '-') + ' ms</td>';
                html += '<td>' + (p.ttft_p99 ? p.ttft_p99.toFixed(0) : '-') + ' ms</td>';
                html += '<td>' + (p.throughput_mean ? p.throughput_mean.toFixed(1) : '-') + ' req/s</td>';
                html += '<td>' + (p.interactivity ? p.interactivity.toFixed(1) : '-') + '</td>';
                html += '<td>' + (p.throughput_per_gpu ? p.throughput_per_gpu.toFixed(0) : '-') + '</td>';
                html += '</tr>';
            });
        });
        html += '</table></div></div>';
    }

    // ============================================================
    // Flush concurrency sweep
    secCal = html; html = '';


    if (data.cache_sweep) {
        html += '<div class="chart-card" style="border-left:6px solid #7c3aed;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#a78bfa); color:white;">Cache Hit Sweep</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Tests the best configurations across different <strong>prefix cache hit ratios</strong> (0% to 100%). ' +
            'Higher cache hit rates mean more prompt tokens are reused from cache, reducing prefill compute and improving TTFT. ' +
            'The charts show how latency and throughput change as the cache hit rate increases, with actual vLLM cache hit rates from Prometheus.' +
            '</div></div>';
        const csweep = data.cache_sweep;
        const csArchLabels = {pd: 'PD', aggregated: 'Aggregated', ep: 'EP',
                              pd_calibrated: 'PD (calibrated)', aggregated_calibrated: 'Aggregated (calibrated)', ep_calibrated: 'EP (calibrated)'};
        const csArchColors = {pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed',
                              pd_calibrated: '#60a5fa', aggregated_calibrated: '#34d399', ep_calibrated: '#a78bfa'};
        const csPctColors = { ttft_p90: '#3b82f6', ttft_p95: '#f59e0b', ttft_p99: '#ef4444' };

        Object.keys(csweep).forEach(function(arch) {
            var pts = csweep[arch];
            if (!pts || !pts.length) return;
            var csArchKey = arch.startsWith('pd') ? 'pd' : arch.startsWith('agg') ? 'aggregated' : arch.startsWith('ep') ? 'ep' : arch;
            var csLabel = csArchLabels[csArchKey] || arch;
            var csColor = csArchColors[csArchKey] || '#888';

            var csConfigLbl = '';
            for (var _ci = 0; _ci < pts.length && !csConfigLbl; _ci++) {
                csConfigLbl = pts[_ci].config_label || '';
            }
            if (!csConfigLbl) {
                var _csBase = arch.replace('_calibrated','');
                csConfigLbl = sweepConfigLabels[_csBase] || '';
                if (!csConfigLbl && data.concurrency_sweep && data.concurrency_sweep[_csBase]) {
                    var _csPts = data.concurrency_sweep[_csBase];
                    csConfigLbl = (_csPts[0] && _csPts[0].config_label) ? _csPts[0].config_label : '';
                }
            }
            if (!csConfigLbl) {
                var _tpMatch = arch.match(/tp(\d+)/);
                if (_tpMatch) {
                    var _tp = _tpMatch[1];
                    if (csArchKey === 'aggregated') {
                        var _totalPods = Math.floor((pts[0] && pts[0].gpus || 8) / parseInt(_tp));
                        csConfigLbl = _totalPods + '×TP' + _tp;
                    } else {
                        csConfigLbl = 'TP' + _tp;
                    }
                } else {
                    csConfigLbl = arch;
                }
            }
            var csConfigSuffix = csConfigLbl ? ' — ' + csConfigLbl : '';
            html += '<div class="chart-card" style="margin-top:20px; border:2px solid ' + csColor + '; border-left:6px solid ' + csColor + ';">';
            html += '<div class="chart-card-header" style="background:linear-gradient(135deg,' + csColor + ',' + csColor + '99); color:white; font-size:1.2em;">' + csLabel + csConfigSuffix + ' — Cache Hit Sweep</div>';

            // --- TTFT P90/P95/P99 on same chart ---
            var csTtftId = 'cache-sweep-ttft-' + arch;
            html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">TTFT percentiles &amp; throughput vs cache hit ratio. Lower TTFT is better, higher throughput is better. Subplot shows actual vs configured cache hit rate from vLLM.</div>';
            html += '<div id="' + csTtftId + '" style="width:100%;height:850px;"></div>';

            var hasActualHitRate = pts.some(function(p) { return p.actual_hit_rate != null; });

            chartQueue.push(function() {
                var traces = [];
                // Main chart: TTFT P90/P95/P99 + Throughput on right axis
                [{ key: 'ttft_p90', label: 'TTFT P90' },
                 { key: 'ttft_p95', label: 'TTFT P95' },
                 { key: 'ttft_p99', label: 'TTFT P99' }].forEach(function(pct) {
                    if (!pts[0][pct.key]) return;
                    traces.push({
                        x: pts.map(function(p) { return p.hit_pct; }),
                        y: pts.map(function(p) { return p[pct.key] || 0; }),
                        text: pts.map(function(p) { return Math.round(p[pct.key] || 0).toLocaleString(); }),
                        textposition: 'top center', textfont: { size: 13, color: csPctColors[pct.key] },
                        mode: 'lines+markers+text', name: pct.label,
                        line: { color: csPctColors[pct.key], width: 3 },
                        marker: { size: 8 },
                        hovertemplate: 'Cache Hit: %{x}%<br>' + pct.label + ': %{y:.0f}ms<extra></extra>'
                    });
                });
                // Throughput on right axis
                traces.push({
                    x: pts.map(function(p) { return p.hit_pct; }),
                    y: pts.map(function(p) { return p.throughput_mean || 0; }),
                    text: pts.map(function(p) { return (p.throughput_mean || 0).toFixed(1); }),
                    textposition: 'top center', textfont: { size: 13, color: '#d97706' },
                    mode: 'lines+markers+text', name: 'Throughput Mean',
                    yaxis: 'y2',
                    line: { color: '#d97706', width: 3, dash: 'dash' },
                    marker: { size: 8, symbol: 'diamond', color: '#d97706' },
                    hovertemplate: 'Cache Hit: %{x}%<br>Throughput: %{y:.1f} req/s<extra></extra>'
                });

                // Subplot: Actual Cache Hit Rate (hits/queries from vLLM Prometheus)
                if (hasActualHitRate) {
                    traces.push({
                        x: pts.map(function(p) { return p.hit_pct; }),
                        y: pts.map(function(p) { return p.actual_hit_rate != null ? p.actual_hit_rate : 0; }),
                        text: pts.map(function(p) { return p.actual_hit_rate != null ? p.actual_hit_rate.toFixed(1) + '%' : ''; }),
                        textposition: 'top center', textfont: { size: 13, color: '#059669' },
                        xaxis: 'x2', yaxis: 'y3',
                        mode: 'lines+markers+text', name: 'Actual Hit % (hits/queries)',
                        line: { color: '#059669', width: 3 },
                        marker: { size: 8 },
                        hovertemplate: 'Configured: %{x}%<br>Actual: %{y:.1f}%' +
                            '<br>Hits rate: ' + '%{customdata[0]}' + '/s' +
                            '<br>Queries rate: ' + '%{customdata[1]}' + '/s<extra></extra>',
                        customdata: pts.map(function(p) {
                            return [p.cache_hits_rate != null ? p.cache_hits_rate.toFixed(0) : '-',
                                    p.cache_queries_rate != null ? p.cache_queries_rate.toFixed(0) : '-'];
                        })
                    });
                }

                var xTickVals = pts.map(function(p) { return p.hit_pct; });
                var layout = {
                    xaxis: { range: [-5, 105], gridcolor: '#e2e8f0', domain: [0, 1], tickvals: xTickVals, ticktext: xTickVals.map(function(v) { return v + '%'; }) },
                    yaxis: { title: 'TTFT (ms)', gridcolor: '#e2e8f0', titlefont: { color: '#3b82f6' }, tickfont: { color: '#3b82f6' },
                             domain: hasActualHitRate ? [0.3, 1] : [0, 1] },
                    yaxis2: { title: 'Throughput (req/s)', side: 'right', overlaying: 'y',
                              titlefont: { color: '#d97706' }, tickfont: { color: '#d97706' } },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    margin: { t: 20, b: 60, l: 70, r: 70 },
                    legend: { x: 0, y: 1.15, orientation: 'h', bgcolor: 'rgba(255,255,255,0.9)' },
                    hovermode: 'closest'
                };

                if (hasActualHitRate) {
                    layout.xaxis2 = { title: 'Cache Hit %', range: [-5, 105], gridcolor: '#e2e8f0', anchor: 'y3', tickvals: xTickVals, ticktext: xTickVals.map(function(v) { return v + '%'; }) };
                    layout.yaxis3 = { title: 'Actual Hit %', range: [-5, 105], gridcolor: '#e2e8f0', domain: [0, 0.22],
                                      titlefont: { color: '#059669' }, tickfont: { color: '#059669' } };
                }

                Plotly.newPlot(csTtftId, traces, layout, { responsive: true });
            });

            // --- Data table ---
            var csCacheTblId = 'cache-sweep-tbl-' + arch + '-' + runId;
            html += '<div style="padding:12px 20px;"><div style="overflow-x:auto;"><table class="results-table" id="' + csCacheTblId + '" style="font-size:0.85em;">';
            html += '<tr>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',0,\'num\')">Cache Hit % &#x21C5;</th>';
            var _col = 0;
            function _th(label) { html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',' + (_col++) + ',\'num\')">' + label + ' &#x21C5;</th>'; }
            _th('Actual Hit %'); _th('Concurrency');
            _th('TTFT P50'); _th('TTFT P90'); _th('TTFT P95'); _th('TTFT P99');
            _th('Throughput'); _th('vLLM tok/s'); _th('ITL P90');
            _th('E2E P50'); _th('E2E P90');
            _th('KV Cache %'); _th('Running'); _th('Run Max'); _th('Load Spread'); _th('Waiting');
            _th('Prefill t'); _th('Decode t');
            html += '</tr>';

            var _csTblLookup = {};
            (data.all_results || []).forEach(function(r) { _csTblLookup[r.test_id] = r; });

            pts.forEach(function(p) {
                var _r = _csTblLookup[p.test_id] || {};
                var _mj = _r.metrics_json ? (typeof _r.metrics_json === 'string' ? JSON.parse(_r.metrics_json) : _r.metrics_json) : {};
                var _pm = _mj.prometheus_metrics || {};
                var _kvAvg = ((_pm.vllm_kv_cache_pct || {}).avg || 0) * 100;
                var _running = (_pm.vllm_requests_running || {}).avg || 0;
                var _runMax = (_pm.vllm_requests_running || {}).max || 0;
                var _runMin = (_pm.vllm_requests_running || {}).min || 0;
                var _waiting = (_pm.vllm_requests_waiting || {}).avg || 0;
                var _routingCV = (_pm.vllm_routing_cv || {}).avg || null;
                var _prefillT = (_pm.vllm_prefill_time_rate || {}).avg || 0;
                var _decodeT = (_pm.vllm_decode_time_rate || {}).avg || 0;
                var _e2eP50 = (_pm.vllm_e2e_p50 || {}).avg || _mj.e2e_latency_p50 || null;
                var _e2eP90 = (_pm.vllm_e2e_p90 || {}).avg || _mj.e2e_latency_p90 || null;
                var fmtE2e = function(v) { return v != null ? (v >= 1 ? v.toFixed(1) + 's' : Math.round(v * 1000) + 'ms') : '-'; };
                html += '<tr>';
                html += '<td>' + p.hit_pct + '%</td>';
                html += '<td>' + (p.actual_hit_rate != null ? p.actual_hit_rate.toFixed(1) + '%' : '-') + '</td>';
                html += '<td>' + (p.concurrency || '-') + '</td>';
                html += '<td>' + (p.ttft_p50 ? p.ttft_p50.toFixed(0) : '-') + '</td>';
                html += '<td>' + (p.ttft_p90 ? p.ttft_p90.toFixed(0) : '-') + '</td>';
                html += '<td>' + (p.ttft_p95 ? p.ttft_p95.toFixed(0) : '-') + '</td>';
                html += '<td>' + (p.ttft_p99 ? p.ttft_p99.toFixed(0) : '-') + '</td>';
                html += '<td>' + (p.throughput_mean ? p.throughput_mean.toFixed(2) : '-') + '</td>';
                html += '<td>' + (p.vllm_tps ? p.vllm_tps.toLocaleString() : (p.output_tps_mean ? p.output_tps_mean.toFixed(0) : '-')) + '</td>';
                html += '<td>' + (p.itl_p90 ? p.itl_p90.toFixed(1) : '-') + '</td>';
                html += '<td>' + fmtE2e(_e2eP50) + '</td>';
                html += '<td>' + fmtE2e(_e2eP90) + '</td>';
                html += '<td>' + _kvAvg.toFixed(1) + '%</td>';
                html += '<td>' + _running.toFixed(1) + '</td>';
                html += '<td>' + _runMax.toFixed(0) + '</td>';
                html += '<td>' + (_runMax - _running).toFixed(1) + '</td>';
                html += '<td>' + _waiting.toFixed(1) + '</td>';
                html += '<td>' + _prefillT.toFixed(3) + '</td>';
                html += '<td>' + _decodeT.toFixed(2) + '</td>';
                html += '</tr>';
            });
            html += '</table>';
            html += '<div style="padding:8px 20px 12px; font-size:0.85em; color:#64748b; line-height:1.7;">' +
                '<strong>KV Cache Avg/Max %</strong>: How full the KV cache is across pods. Low values mean KV capacity is not the bottleneck.<br>' +
                '<strong>Avg Running Reqs</strong>: Average requests actively being processed per pod over time (flattened across all pods and all timestamps).<br>' +
                '<strong>Max Running Reqs</strong>: Peak concurrent requests on a single pod at any point during the test. Large gap between avg and max indicates EPP routing imbalance (e.g. prefix affinity concentrating requests on one pod).<br>' +
                '<strong>Load Spread</strong>: Gap between peak and average running requests (Max-Avg). High values indicate traffic bursts — requests arriving simultaneously rather than staggered.<br>' +
                '<strong>Avg Waiting Reqs</strong>: Requests queued waiting for a processing slot. High values indicate saturation.<br>' +
                '<strong>Decode req-s/s</strong>: Total accumulated decode time across all concurrent requests per wall-second. Proportional to running request count, not GPU count.<br>' +
                '<strong>Prefill req-s/s</strong>: Total accumulated prefill time across all concurrent requests per wall-second. Low values with high cache hit mean prefill is served from cache.</div>' +
            '</div></div>';

            // --- vLLM Engine Metrics chart for this config ---
            var csEngineId = 'cache-sweep-engine-' + arch;
            html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b; margin-top:12px;">vLLM engine metrics across cache hit levels — KV cache usage, running/waiting requests, prefill/decode GPU time, and preemptions.</div>';
            html += '<div id="' + csEngineId + '" style="width:100%;height:850px;"></div>';

            chartQueue.push((function(chartId, points) { return function() {
                var _arLookup = {};
                (data.all_results || []).forEach(function(r) { _arLookup[r.test_id] = r; });
                var xVals = [], kvAvg = [], kvMax = [], runAvg = [], runMax = [], runSpread = [], waitAvg = [], prefillT = [], decodeT = [], preempt = [];
                points.forEach(function(p) {
                    var tid = p.test_id || '';
                    var r = _arLookup[tid] || {};
                    var mj = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                    var pm = mj.prometheus_metrics || {};
                    xVals.push(p.hit_pct + '%');
                    kvAvg.push(((pm.vllm_kv_cache_pct || {}).avg || 0) * 100);
                    kvMax.push(((pm.vllm_kv_cache_pct || {}).max || 0) * 100);
                    runAvg.push((pm.vllm_requests_running || {}).avg || 0);
                    runMax.push((pm.vllm_requests_running || {}).max || 0);
                    runSpread.push(((pm.vllm_requests_running || {}).max || 0) - ((pm.vllm_requests_running || {}).avg || 0));
                    waitAvg.push((pm.vllm_requests_waiting || {}).avg || 0);
                    prefillT.push((pm.vllm_prefill_time_rate || {}).avg || 0);
                    decodeT.push((pm.vllm_decode_time_rate || {}).avg || 0);
                    preempt.push((pm.vllm_preemptions_rate || {}).avg || 0);
                });
                if (!xVals.length) return;
                var traces = [
                    { x: xVals, y: kvAvg, name: 'KV Cache Avg %', type: 'bar', marker: { color: '#3b82f6' } },
                    { x: xVals, y: kvMax, name: 'KV Cache Max %', type: 'bar', marker: { color: '#93c5fd' } },
                    { x: xVals, y: runAvg, name: 'Avg Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 3 }, marker: { size: 8 } },
                    { x: xVals, y: runMax, name: 'Max Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 2, dash: 'dash' }, marker: { size: 6, symbol: 'triangle-up' } },
                    { x: xVals, y: runSpread, name: 'Load Spread', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 2, dash: 'dashdot' }, marker: { size: 6, symbol: 'diamond' } },
                    { x: xVals, y: waitAvg, name: 'Avg Waiting Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#ef4444', width: 2, dash: 'dot' }, marker: { size: 6 } },
                    { x: xVals, y: decodeT, name: 'Decode req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#f59e0b', width: 2 }, marker: { size: 6 } },
                    { x: xVals, y: prefillT, name: 'Prefill req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#8b5cf6', width: 2 }, marker: { size: 6 } },
                ];
                if (preempt.some(function(v) { return v > 0; })) {
                    traces.push({ x: xVals, y: preempt, name: 'Preemptions/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 3 }, marker: { size: 8, symbol: 'x' } });
                }
                Plotly.newPlot(chartId, traces, {
                    barmode: 'group',
                    xaxis: { title: 'Cache Hit %' },
                    yaxis: { title: 'KV Cache %', side: 'left', rangemode: 'tozero', gridcolor: '#e2e8f0' },
                    yaxis2: { title: 'Count / Rate', overlaying: 'y', side: 'right', rangemode: 'tozero' },
                    legend: { x: 0, y: 1.15, orientation: 'h' },
                    margin: { t: 40, b: 60, l: 60, r: 60 },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    hovermode: 'x unified',
                }, { responsive: true });
            }; })(csEngineId, pts));

            html += '</div>';
        });
    }

    // Flush cache sweep
    secCacheSweep = html; html = '';

    // ============================================================



    // === Build Estimator section ===
    const wl = data.recommendation ? data.recommendation.workload : {};
    const estSuffix = _chartSuffix;
    var wlDesc = 'Tested with <strong>' + (wl.users || '?') + ' users</strong>, ISL <strong>' + (wl.isl || '?') + '</strong>';
    if (wl.isl_stdev) wlDesc += '&plusmn;' + wl.isl_stdev;
    wlDesc += ', OSL <strong>' + (wl.osl || '?') + '</strong>';
    if (wl.osl_stdev) wlDesc += '&plusmn;' + wl.osl_stdev;
    if (wl.turns > 1) wlDesc += ', <strong>' + wl.turns + ' turns</strong>';
    wlDesc += '.';
    // === Build subtab structure ===
    const subtabDefs = [];

    if (secRec) subtabDefs.push({ id: 'recommendation', label: 'Recommendation', icon: '&#9733;' });
    if (secTP) subtabDefs.push({ id: 'tp-calibration', label: 'TP Calibration', icon: '&#9881;' });
    if (secCfg) subtabDefs.push({ id: 'configurations', label: 'Configurations', icon: '&#9776;' });
    if (secCmp) subtabDefs.push({ id: 'comparison', label: 'Comparison', icon: '&#8596;' });
    if (secStep9) subtabDefs.push({ id: 'latency-search', label: 'Latency Search', icon: '&#128269;' });
    if (secCal) subtabDefs.push({ id: 'calibrated-load', label: 'Concurrency Sweep', icon: '&#9878;' });
    if (secCacheSweep) subtabDefs.push({ id: 'cache-sweep', label: 'Cache Sweep', icon: '&#128451;' });

    const sectionMap = {
        'recommendation': secRec, 'tp-calibration': secTP, 'configurations': secCfg,
        'comparison': secCmp, 'latency-search': secStep9,
        'calibrated-load': secCal, 'cache-sweep': secCacheSweep
    };

    // ── Modular tabs (web/static/report/tabs/) ── injected by position ──
    if (typeof ReportTabs !== 'undefined') {
        const _liveCtx = (typeof ReportCtx !== 'undefined') ? ReportCtx.live(runId) : null;
        if (_liveCtx) {
            ReportTabs.all().forEach(function(tab) {
                const tabHtml = ReportTabs.buildTab(tab, data, _liveCtx, chartQueue);
                if (!tabHtml) return;
                const def = { id: tab.id, label: tab.label, icon: tab.icon };
                const pos = tab.position || 'end';
                let at = -1;
                if (pos.indexOf('before:') === 0) at = subtabDefs.findIndex(d => d.id === pos.slice(7));
                else if (pos.indexOf('after:') === 0) { at = subtabDefs.findIndex(d => d.id === pos.slice(6)); if (at >= 0) at++; }
                if (at >= 0) subtabDefs.splice(at, 0, def); else subtabDefs.push(def);
                sectionMap[tab.id] = tabHtml;
            });
        }
    }

    if (subtabDefs.length > 1) {
        html += '<div class="report-subtabs-container">';
        html += '<div class="report-subtab-bar">';
        subtabDefs.forEach((st, i) => {
            html += `<div class="report-subtab${i === 0 ? ' active' : ''}" data-subtab="${st.id}${_chartSuffix}">${st.icon} ${st.label}</div>`;
        });
        html += '</div>';
        subtabDefs.forEach((st) => {
            html += `<div class="report-subtab-pane" data-subtab-pane="${st.id}${_chartSuffix}">${sectionMap[st.id]}</div>`;
        });
        html += '</div>';
    } else {
        for (const sec of Object.values(sectionMap)) html += sec;
    }

    // Global report-table stylesheet — normalizes every table in the report
    // (width, padding, alignment, borders, zebra striping) regardless of the
    // inline styles they were built with
    html = '<style>' +
        '#charts-content table { width: 100% !important; border-collapse: collapse !important; margin: 10px 0 0 0 !important; font-size: 0.9em !important; background: #fff !important; }' +
        '#charts-content th { background: #eef2ff !important; color: #334155 !important; padding: 7px 10px !important; text-align: left !important; border-top: 1px solid #c7d2fe !important; border-bottom: 2px solid #c7d2fe !important; white-space: nowrap !important; font-weight: 600 !important; }' +
        '#charts-content td { padding: 6px 10px !important; text-align: left !important; border-bottom: 1px solid #e2e8f0 !important; vertical-align: top !important; }' +
        '#charts-content tr:nth-child(even) td { background: #f8fafc !important; }' +
        '#charts-content tr:hover td { background: #eef2ff !important; }' +
        '#charts-content td[style*="monospace"] { white-space: nowrap; }' +
        '#charts-content .pod-bd-table { table-layout: fixed !important; }' +
        '#charts-content .pod-bd-table th { white-space: normal !important; }' +
        '#charts-content .pod-bd-table th:nth-child(1), #charts-content .pod-bd-table td:nth-child(1) { width: 13%; }' +
        '#charts-content .pod-bd-table th:nth-child(2), #charts-content .pod-bd-table td:nth-child(2) { width: 7%; }' +
        '#charts-content .pod-bd-table th:nth-child(3), #charts-content .pod-bd-table td:nth-child(3) { width: 9%; }' +
        '#charts-content .pod-bd-table th:nth-child(4), #charts-content .pod-bd-table td:nth-child(4) { width: 9%; }' +
        '#charts-content .pod-bd-table th:nth-child(5), #charts-content .pod-bd-table td:nth-child(5) { width: 6%; }' +
        '#charts-content .pod-bd-table th:nth-child(6), #charts-content .pod-bd-table td:nth-child(6) { width: 6%; }' +
        '#charts-content .pod-bd-table th:nth-child(7), #charts-content .pod-bd-table td:nth-child(7) { width: 41%; }' +
        '#charts-content .pod-bd-table th:nth-child(8), #charts-content .pod-bd-table td:nth-child(8) { width: 9%; }' +
        '</style>' + html;

    content.innerHTML = html;

    // --- Plotly chart config (must be before EPP chart rendering) ---
    const plotlyLayout = { margin: { t: 10, b: 40, l: 50, r: 20 }, height: 850, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent', font: { family: 'Inter, sans-serif' } };
    // Safe Plotly wrapper — skips if the DOM element doesn't exist (hidden tab)
    function safePlot(id, data, layout, config) { var el = document.getElementById(id); if (el) Plotly.newPlot(id, data, layout, config); }
    const plotlyConfig = { responsive: true, displayModeBar: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'], toImageButtonOptions: { format: 'png', height: 780, width: 1200, scale: 2 } };

    // Render EPP tuning charts now (before subtabs-ready hides non-active panes)
    if (window._eppChartRenders && window._eppChartRenders.length) {
        window._eppChartRenders.forEach(fn => fn());
        window._eppChartRenders = [];
    }

    // Render parallel-workload charts
    if (window._wlChartRenders && window._wlChartRenders.length) {
        window._wlChartRenders.forEach(fn => { try { fn(); } catch (e) {} });
        window._wlChartRenders = [];
    }

    // Render estimator methodology explanation
    updateEstimatorScaling(_chartSuffix);

    // Pareto frontier — TPSG bars (left) + TTFT line (right), no ITL
    if (charts.pareto.traces.length) {
        var tpsgLookup = {};
        if (rec) {
            (rec.decode_tp_all || []).forEach(function(d) { tpsgLookup['decode-' + d.tp] = d.tpsg; });
            (rec.prefill_tp_all || []).forEach(function(d) { tpsgLookup['prefill-' + d.tp] = d.tpsg; });
        }
        var traces = [];
        var roleColors = { decode: '#3b82f6', prefill: '#f59e0b' };
        charts.pareto.traces.forEach(function(t) {
            if (t.yaxis === 'y2') return;
            var role = t.name.toLowerCase().indexOf('decode') >= 0 ? 'decode' : 'prefill';
            var color = roleColors[role];
            var tpsgVals = t.x.map(function(tp) { return tpsgLookup[role + '-' + tp] || 0; });
            traces.push({
                x: t.x, y: tpsgVals, name: role.charAt(0).toUpperCase() + role.slice(1) + ' TPSG',
                type: 'bar',
                marker: { color: color },
                text: tpsgVals.map(function(v) { return v > 0 ? Math.round(v).toLocaleString() : ''; }),
                textposition: 'outside', textfont: { size: 13, color: color },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>TP=%{x}</b><br>TPSG: %{y:,.0f} tok/s/GPU<extra></extra>'
            });
            traces.push({
                x: t.x, y: t.y, name: role.charAt(0).toUpperCase() + role.slice(1) + ' TTFT', yaxis: 'y2',
                mode: 'markers+lines+text', text: t.text,
                customdata: t.y.map(function(v) { return v >= 1000 ? (v/1000).toFixed(1) + 's' : Math.round(v) + 'ms'; }),
                texttemplate: '%{customdata}',
                textposition: 'top center',
                textfont: { size: 13, color: '#1e293b' },
                marker: { size: 10, color: color, symbol: 'circle', line: { width: 2, color: 'white' } },
                line: { width: 2, dash: 'dash', color: color },
                hovertemplate: '<b>%{text}</b><extra></extra>'
            });
        });
        const paretoXvals = [...new Set(charts.pareto.traces.filter(t => t.yaxis !== 'y2').flatMap(t => t.x))].sort((a, b) => a - b);
        safePlot(cid('chart-pareto'), traces, {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60, t: 30 },
            barmode: 'group',
            xaxis: { title: 'Total GPUs (= TP)', tickvals: paretoXvals },
            yaxis: { title: 'TPSG (tok/s/GPU) — higher is better', side: 'left', tickformat: ',.0f' },
            yaxis2: { title: 'TTFT P90 (ms) — lower is better', side: 'right', overlaying: 'y', type: 'log' },
            showlegend: true
        }, plotlyConfig);
    }

    // Scatter
    if (charts.scatter.traces.length) {
        var filteredScatter = charts.scatter.traces.map(function(t) {
            var keep = [];
            var tids = t.test_ids || [];
            tids.forEach(function(tid, i) {
                if (tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0) keep.push(i);
            });
            return { x: keep.map(i => t.x[i]), y: keep.map(i => t.y[i]), text: keep.map(i => t.text[i]), name: t.name, sizes: keep.map(i => (t.sizes || [])[i]), color: t.color };
        }).filter(t => t.x.length);
        var allSizes = filteredScatter.flatMap(t => t.sizes || []);
        var maxSize = Math.max.apply(null, allSizes) || 1;
        const traces = filteredScatter.map(t => ({
            x: t.x, y: t.y, text: t.text, name: t.name,
            mode: 'markers',
            marker: { size: (t.sizes || []).map(s => 8 + (s / maxSize) * 22), color: t.color, opacity: 0.7, line: { width: 1, color: 'white' } },
            hovertemplate: '<b>%{text}</b><extra></extra>'
        }));
        safePlot(cid('chart-scatter'), traces, { ...plotlyLayout, xaxis: { title: 'TTFT P90 (ms) - lower is better' }, yaxis: { title: 'Throughput Mean (req/s) - higher is better' }, showlegend: true }, plotlyConfig);
    }

    // Efficiency bar
    if (charts.efficiency.configs.length) {
        var effConfigs = charts.efficiency.configs;
        var effValues = charts.efficiency.values;
        var effColors = charts.efficiency.colors;
        if (effConfigs.length) {
            safePlot(cid('chart-efficiency'), [{
                x: effConfigs, y: effValues,
                type: 'bar', marker: { color: effColors },
                text: effValues.map(v => v != null ? v.toFixed(3) : ''),
                textposition: 'outside', textfont: { size: 14, color: '#333' },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>%{x}</b><br>%{y:.3f} req/s/GPU<extra></extra>'
            }], { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 120 }, xaxis: { tickangle: -45 }, yaxis: { title: 'Mean req/s per GPU - higher is better' } }, plotlyConfig);
        }
    }

    // Per-user efficiency bar
    if (charts.per_user_efficiency && charts.per_user_efficiency.configs.length) {
        var puConfigs = charts.per_user_efficiency.configs;
        var puValues = charts.per_user_efficiency.values;
        var puColors = charts.per_user_efficiency.colors;
        safePlot(cid('chart-per-user-efficiency'), [{
            x: puConfigs, y: puValues,
            type: 'bar', marker: { color: puColors },
            text: puValues.map(v => v != null ? v.toFixed(1) : ''),
            textposition: 'outside', textfont: { size: 14, color: '#333' },
            cliponaxis: false, constraintext: 'none',
            hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/user<extra></extra>'
        }], { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 120 }, xaxis: { tickangle: -45 }, yaxis: { title: 'Output tokens/s per concurrent user - higher is better' } }, plotlyConfig);
    }

    // Token Throughput charts (Configurations tab)
    if (document.getElementById(cid('chart-token-tput'))) {
        var tkRes = (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || '';
            if (tid.indexOf('step2-') === 0 || tid.indexOf('step3-') === 0 ||
                tid.indexOf('step11-') === 0 || tid.indexOf('step12-') === 0 || tid.indexOf('step13-') === 0) return false;
            var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
            var pm = m.prometheus_metrics || {};
            return (pm.vllm_generation_tokens_rate || {}).avg || r.output_tps_mean;
        });
        if (tkRes.length) {
            var tkLabels = tkRes.map(function(r) { return _tagLabel(r.config_name, r.architecture); });
            var tkComputed = [], tkServer = [], tkGuidellmTotal = [];
            tkRes.forEach(function(r) {
                var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                var pm = m.prometheus_metrics || {};
                var pAvg = (pm.vllm_prompt_tokens_rate || {}).avg || 0;
                var gAvg = (pm.vllm_generation_tokens_rate || {}).avg || 0;
                var serverTotal = Math.round(pAvg + gAvg);
                tkServer.push(serverTotal);
                // Compute truly computed tok/s: subtract cached prompt tokens
                var cacheHits = (pm.vllm_prefix_cache_hits_rate || {}).avg || 0;
                var cacheQueries = (pm.vllm_prefix_cache_queries_rate || {}).avg || 0;
                var cacheHitRate = cacheQueries > 0 ? cacheHits / cacheQueries : 0;
                var computedPrompt = Math.round(pAvg * (1 - cacheHitRate));
                tkComputed.push(computedPrompt + Math.round(gAvg));
                // guidellm total = prompt_tokens_mean * throughput + output_tokens_mean * throughput
                var tput = m.throughput_mean || r.throughput_mean || 0;
                var pTps = (m.prompt_tokens_mean || 0) * tput;
                var gTps = (m.output_tokens_mean || 0) * tput;
                tkGuidellmTotal.push(Math.round(pTps + gTps));
            });
            safePlot(cid('chart-token-tput'), [
                { x: tkLabels, y: tkComputed, name: 'GPU computed', type: 'bar', marker: { color: '#3b82f6' }, text: tkComputed.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } },
                { x: tkLabels, y: tkServer, name: 'Server total (incl. cached)', type: 'bar', marker: { color: '#93c5fd' }, text: tkServer.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } },
                { x: tkLabels, y: tkGuidellmTotal, name: 'Client (guidellm)', type: 'bar', marker: { color: '#f59e0b' }, text: tkGuidellmTotal.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } }
            ], { ...plotlyLayout, height: 850, margin: { t: 60, b: 140, l: 60, r: 20 }, barmode: 'group', xaxis: { tickangle: -35 }, yaxis: { title: 'Total Tokens/s (Prompt + Generation)' }, showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' } }, plotlyConfig);
        }
    }

    // --- Concurrency Sweep Pareto charts (rendered into Concurrency Sweep tab) ---
    if (data.concurrency_sweep && document.getElementById(cid('chart-pareto-sweep-interactivity'))) {
        // Build set of discarded test_ids to filter from sweep charts
        var discardedIds = {};
        (data.all_results || []).forEach(function(r) {
            if (r.quality === 'discard') discardedIds[r.test_id || r.config_name] = true;
        });
        // Filter sweep data — remove points whose test_id matches a discarded test
        var csw = {};
        Object.keys(data.concurrency_sweep).forEach(function(arch) {
            var pts = data.concurrency_sweep[arch];
            if (!pts) return;
            csw[arch] = pts.filter(function(p) {
                var tid = p.test_id || '';
                return !discardedIds[tid];
            });
        });
        function sweepArchLabel(key) {
            if (key.startsWith('pd')) return 'PD';
            if (key.startsWith('agg')) return 'AGGREGATED';
            if (key.startsWith('ep')) return 'EP';
            return key.toUpperCase();
        }

        function swFrontierLine(points) {
            if (!points.length) return [];
            var sorted = points.slice().sort(function(a, b) { return b.nx - a.nx; });
            var frontier = [];
            var maxY = -Infinity;
            for (var i = 0; i < sorted.length; i++) {
                if (sorted[i].ny > maxY) { frontier.push(sorted[i]); maxY = sorted[i].ny; }
            }
            frontier.sort(function(a, b) { return a.nx - b.nx; });
            return frontier;
        }

        var swAllPts = [];
        Object.keys(csw).forEach(function(arch) {
            var pts = csw[arch];
            if (!pts || !pts.length) return;
            var archLabel = sweepArchLabel(arch);
            var archTag = arch.startsWith('pd') ? ' (PD)' : arch.startsWith('ep') ? ' (EP)' : ' (AG)';
            pts.forEach(function(p) {
                var interPerUser = (p.vllm_tps || p.output_tps_mean || 0) / (p.concurrency || 1);
                var tputGpu = p.throughput_per_gpu || 0;
                var cfgLabel = p.config_label || archLabel;
                if (cfgLabel.indexOf('(') < 0) cfgLabel += archTag;
                if (interPerUser > 0 && tputGpu > 0) {
                    swAllPts.push({
                        x: interPerUser, y: tputGpu, nx: interPerUser, ny: tputGpu,
                        label: cfgLabel + ' c=' + p.concurrency,
                        arch: archLabel, conc: p.concurrency
                    });
                }
            });
        });

        var swParetoConfig = {
            responsive: true, displayModeBar: true,
            modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
            toImageButtonOptions: { format: 'png', height: 1200, width: 1600, scale: 2 }
        };

        if (swAllPts.length > 1) {
            var swAgg = swAllPts.filter(function(p) { return p.arch === 'AGGREGATED'; });
            var swPd = swAllPts.filter(function(p) { return p.arch === 'PD' || p.arch === 'EP'; });
            var swAggPareto = swFrontierLine(swAgg);
            var swPdPareto = swFrontierLine(swPd);

            // Group points by config for individual legend entries
            var swCfgGroups = {};
            swAllPts.forEach(function(p) {
                var key = p.label.replace(/ c=\d+$/, '');
                if (!swCfgGroups[key]) swCfgGroups[key] = { pts: [], arch: p.arch };
                swCfgGroups[key].pts.push(p);
            });
            var swCfgColors = [
                '#dc2626', '#f59e0b', '#ea580c', '#db2777',
                '#2563eb', '#059669', '#7c3aed', '#0891b2', '#65a30d', '#6d28d9'
            ];
            var swTraces = [];
            var swColorIdx = 0;
            Object.keys(swCfgGroups).sort().forEach(function(key) {
                var g = swCfgGroups[key];
                var color = swCfgColors[swColorIdx % swCfgColors.length];
                swColorIdx++;
                swTraces.push({
                    x: g.pts.map(function(p) { return p.nx; }), y: g.pts.map(function(p) { return p.ny; }),
                    customdata: g.pts.map(function(p) { return p.label + '<br>' + p.x.toFixed(1) + ' tok/s/user<br>' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                    name: key, mode: 'markers',
                    marker: { color: color, size: 12 },
                    hovertemplate: '<b>%{customdata}</b><extra></extra>'
                });
            });
            // Pareto frontier lines (aggregated vs PD)
            if (swAggPareto.length > 1) {
                swTraces.push({
                    x: swAggPareto.map(function(p) { return p.nx; }), y: swAggPareto.map(function(p) { return p.ny; }),
                    name: 'Aggregated — Frontier', mode: 'lines',
                    line: { color: '#dc2626', width: 3, dash: 'dot' }, showlegend: true
                });
            }
            if (swPdPareto.length > 1) {
                swTraces.push({
                    x: swPdPareto.map(function(p) { return p.nx; }), y: swPdPareto.map(function(p) { return p.ny; }),
                    name: 'Disaggregation — Frontier', mode: 'lines',
                    line: { color: '#2563eb', width: 3, dash: 'dot' }, showlegend: true
                });
            }

            var swAnnotations = [];
            var swIdx = 0;
            var swPositions = [
                [80, -15], [-80, -15], [70, -45], [-70, -45],
                [90, -30], [-90, -30], [60, -60], [-60, -60],
                [100, -10], [-100, -10], [50, -50], [-50, -50]
            ];
            function addSwLabels(points, color) {
                points.forEach(function(p) {
                    var cfgName = p.label.replace(/ c=\d+$/, '');
                    var pos = swPositions[swIdx % swPositions.length];
                    swIdx++;
                    swAnnotations.push({
                        x: p.nx, y: p.ny,
                        text: cfgName + '<br>' + p.x.toFixed(1) + ' tok/s/user<br>' + p.y.toFixed(0) + ' tok/s/GPU',
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: color,
                        ax: pos[0], ay: pos[1],
                        font: { size: 13, color: color },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2,
                    });
                });
            }
            addSwLabels(swAggPareto, '#dc2626');
            addSwLabels(swPdPareto, '#2563eb');

            safePlot(cid('chart-pareto-sweep-interactivity'), swTraces, {
                ...plotlyLayout, height: 850,
                xaxis: { title: 'Interactivity (tok/s/user)', gridcolor: '#d1d5db' },
                yaxis: { title: 'Token Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db' },
                showlegend: true,
                legend: { x: 1.02, y: 1, xanchor: 'left', bgcolor: 'rgba(255,255,255,0.95)', bordercolor: '#e2e8f0', borderwidth: 1 },
                margin: { t: 40, b: 70, l: 70, r: 160 },
                plot_bgcolor: 'white', paper_bgcolor: 'white',
                annotations: swAnnotations,
            }, swParetoConfig);
        }

        // Shared color function for envelope charts
        var _envColorList = ['#dc2626', '#2563eb', '#059669', '#f59e0b', '#7c3aed', '#db2777', '#0891b2', '#65a30d', '#ea580c', '#6d28d9'];
        var _envColorMap = {};
        var _envCIdx = 0;
        function getEnvColor(label) {
            if (!_envColorMap[label]) { _envColorMap[label] = _envColorList[_envCIdx % _envColorList.length]; _envCIdx++; }
            return _envColorMap[label];
        }

        // --- Best TTFT vs Concurrency (envelope style, dual axis with throughput) ---
        if (document.getElementById(cid('chart-pareto-sweep-ttft'))) {
            var bestTtftByConc = {};
            Object.keys(csw).forEach(function(arch) {
                var pts = csw[arch];
                if (!pts) return;
                pts.forEach(function(p) {
                    var c = p.concurrency;
                    var ttft = p.ttft_p90 || 0;
                    var tput = p.throughput_mean || 0;
                    var label = p.config_label || sweepArchLabel(arch);
                    var _at = arch.startsWith('pd') ? ' (PD)' : arch.startsWith('ep') ? ' (EP)' : ' (AG)';
                    if (label.indexOf('(') < 0) label += _at;
                    if (ttft > 0 && (!bestTtftByConc[c] || ttft < bestTtftByConc[c].ttft)) {
                        bestTtftByConc[c] = { ttft: ttft, tput: tput, label: label };
                    }
                });
            });
            var ttftConcs = Object.keys(bestTtftByConc).map(Number).sort(function(a,b){return a-b;});
            if (ttftConcs.length > 1) {
                var ttftXs = [], ttftYs = [], tputYs = [], ttftColors = [], ttftTexts = [];
                ttftConcs.forEach(function(c) {
                    var b = bestTtftByConc[c];
                    ttftXs.push(c); ttftYs.push(b.ttft); tputYs.push(b.tput);
                    ttftColors.push(getEnvColor(b.label));
                    ttftTexts.push(b.label + '<br>TTFT: ' + b.ttft.toFixed(0) + 'ms<br>Tput: ' + b.tput.toFixed(3) + ' req/s');
                });
                var ttftTraces = [
                    { x: ttftXs, y: ttftYs, text: ttftTexts, name: 'Best TTFT P90', mode: 'lines+markers',
                      line: { color: '#3b82f6', width: 3 }, marker: { color: ttftColors, size: 14, line: { color: '#fff', width: 2 } },
                      yaxis: 'y', hovertemplate: '<b>%{text}</b><extra></extra>' },
                    { x: ttftXs, y: tputYs, name: 'Throughput (req/s)', mode: 'lines+markers',
                      line: { color: '#f59e0b', width: 2, dash: 'dot' }, marker: { color: '#f59e0b', size: 6 },
                      yaxis: 'y2', hovertemplate: '<b>c=%{x}</b><br>%{y:.3f} req/s<extra></extra>' }
                ];
                var ttftAnnotations = [];
                ttftConcs.forEach(function(c, i) {
                    var b = bestTtftByConc[c];
                    var angles = [[-70,-20],[70,-20],[-50,-40],[50,-40],[-80,-10],[80,-10],[-60,-50],[60,-50]];
                    var a = angles[i % angles.length];
                    ttftAnnotations.push({
                        x: c, y: b.ttft, text: b.label,
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: getEnvColor(b.label),
                        ax: a[0], ay: a[1], font: { size: 13, color: getEnvColor(b.label) },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2,
                    });
                });
                safePlot(cid('chart-pareto-sweep-ttft'), ttftTraces, {
                    ...plotlyLayout, height: 850,
                    xaxis: { title: 'Concurrent Users', gridcolor: '#d1d5db', tickmode: 'array', tickvals: ttftConcs },
                    yaxis: { title: 'TTFT P90 (ms)', gridcolor: '#d1d5db', titlefont: { color: '#3b82f6' } },
                    yaxis2: { title: 'Throughput (req/s)', overlaying: 'y', side: 'right', gridcolor: 'transparent', titlefont: { color: '#f59e0b' } },
                    showlegend: true,
                    legend: { x: 0.02, y: 0.98, bgcolor: 'rgba(255,255,255,0.9)', bordercolor: '#e2e8f0', borderwidth: 1 },
                    plot_bgcolor: 'white', paper_bgcolor: 'white',
                    annotations: ttftAnnotations,
                }, swParetoConfig);
            }
        }

        // --- Architecture Comparison Charts (PD vs Aggregated at same concurrency) ---
        var archKeys = Object.keys(csw);
        var hasMultiArch = archKeys.length >= 2;
        if (hasMultiArch) {
            // Build best result per concurrency per arch
            var bestByConc = {};
            archKeys.forEach(function(arch) {
                var pts = csw[arch];
                if (!pts) return;
                var archLabel = sweepArchLabel(arch);
                var isDisagg = arch.startsWith('pd') || arch.startsWith('ep');
                pts.forEach(function(p) {
                    var c = p.concurrency;
                    if (!bestByConc[c]) bestByConc[c] = {};
                    var cat = isDisagg ? 'disagg' : 'agg';
                    if (!bestByConc[c][cat] || p.throughput_per_gpu > bestByConc[c][cat].throughput_per_gpu) {
                        var _lbl = p.config_label || archLabel;
                        var _at2 = arch.startsWith('pd') ? ' (PD)' : arch.startsWith('ep') ? ' (EP)' : ' (AG)';
                        if (_lbl.indexOf('(') < 0) _lbl += _at2;
                        bestByConc[c][cat] = { tputGpu: p.throughput_per_gpu, ttft: p.ttft_p90, label: _lbl, arch: archLabel, conc: c };
                    }
                });
            });

            // Build best-overall per concurrency (across ALL configs)
            var bestOverall = {};
            archKeys.forEach(function(arch) {
                var pts = csw[arch];
                if (!pts) return;
                pts.forEach(function(p) {
                    var c = p.concurrency;
                    var tputGpu = p.throughput_per_gpu || 0;
                    var label = p.config_label || sweepArchLabel(arch);
                    if (!bestOverall[c] || tputGpu > bestOverall[c].tputGpu) {
                        bestOverall[c] = { tputGpu: tputGpu, label: label, arch: sweepArchLabel(arch), conc: c };
                    }
                });
            });

            // Chart 1: Best config per concurrency
            if (document.getElementById(cid('chart-sweep-envelope'))) {
                var concs = Object.keys(bestOverall).map(Number).sort(function(a,b){return a-b;});
                var xs = [], ys = [], colors = [], texts = [];
                concs.forEach(function(c) {
                    var b = bestOverall[c];
                    xs.push(c); ys.push(b.tputGpu);
                    colors.push(getEnvColor(b.label));
                    texts.push(b.label + '<br>c=' + c + '<br>' + b.tputGpu.toFixed(0) + ' tok/s/GPU');
                });
                var envTraces = [{
                    x: xs, y: ys, text: texts,
                    name: 'Best Config', mode: 'lines+markers',
                    line: { color: '#475569', width: 2 },
                    marker: { color: colors, size: 14, line: { color: '#fff', width: 2 } },
                    hovertemplate: '<b>%{text}</b><extra></extra>'
                }];
                var envAnnotations = [];
                concs.forEach(function(c, i) {
                    var b = bestOverall[c];
                    var angles = [[-70,-20],[70,-20],[-50,-40],[50,-40],[-80,-10],[80,-10],[-60,-50],[60,-50]];
                    var a = angles[i % angles.length];
                    envAnnotations.push({
                        x: c, y: b.tputGpu, text: b.label,
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: getEnvColor(b.label),
                        ax: a[0], ay: a[1],
                        font: { size: 13, color: getEnvColor(b.label) },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2,
                    });
                });
                var envXvals = concs.slice();
                safePlot(cid('chart-sweep-envelope'), envTraces, {
                    ...plotlyLayout, height: 850,
                    xaxis: { title: 'Concurrent Users', gridcolor: '#d1d5db', tickmode: 'array', tickvals: envXvals },
                    yaxis: { title: 'Best Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db' },
                    showlegend: false,
                    margin: { t: 40, b: 70, l: 70, r: 40 },
                    plot_bgcolor: 'white', paper_bgcolor: 'white',
                    annotations: envAnnotations,
                }, swParetoConfig);
            }

            // Chart 3: Overlaid — same best-config line + winner comparison
            if (document.getElementById(cid('chart-sweep-overlaid'))) {
                var olConcs = Object.keys(bestOverall).map(Number).sort(function(a,b){return a-b;});
                var olXs = [], olYs = [], olColors = [], olTexts = [];
                olConcs.forEach(function(c) {
                    var b = bestOverall[c];
                    olXs.push(c); olYs.push(b.tputGpu);
                    olColors.push(getEnvColor(b.label));
                    olTexts.push(b.label);
                });
                var olTraces = [{
                    x: olXs, y: olYs, text: olTexts,
                    name: 'Best Config', mode: 'lines+markers',
                    line: { color: '#475569', width: 2 },
                    marker: { color: olColors, size: 14, line: { color: '#fff', width: 2 } },
                    hovertemplate: '<b>%{text}</b><br>c=%{x}<br>%{y:.0f} tok/s/GPU<extra></extra>'
                }];
                // Envelope lines for PD vs Agg comparison
                ['agg', 'disagg'].forEach(function(cat) {
                    var eColor = cat === 'agg' ? '#dc2626' : '#2563eb';
                    var eName = cat === 'agg' ? 'Aggregated — Best' : 'PD/EP — Best';
                    var exs = [], eys = [];
                    Object.keys(bestByConc).sort(function(a,b){return a-b;}).forEach(function(c) {
                        if (bestByConc[c][cat]) { exs.push(parseInt(c)); eys.push(bestByConc[c][cat].tputGpu); }
                    });
                    if (exs.length > 1) {
                        olTraces.push({ x: exs, y: eys, name: eName, mode: 'lines',
                            line: { color: eColor, width: 2, dash: 'dot' }, showlegend: true });
                    }
                });
                var olAnnotations = [];
                olConcs.forEach(function(c, i) {
                    var b = bestOverall[c];
                    var side = i % 2 === 0 ? 1 : -1;
                    olAnnotations.push({
                        x: c, y: b.tputGpu, text: b.label,
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: getEnvColor(b.label),
                        ax: 60 * side, ay: -25 - (i % 3) * 15,
                        font: { size: 13, color: getEnvColor(b.label) },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2,
                    });
                });
                var olXvals = olConcs.slice();
                safePlot(cid('chart-sweep-overlaid'), olTraces, {
                    ...plotlyLayout, height: 850,
                    xaxis: { title: 'Concurrent Users', gridcolor: '#d1d5db', tickmode: 'array', tickvals: olXvals },
                    yaxis: { title: 'Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db' },
                    showlegend: true,
                    legend: { x: 0.02, y: 0.98, bgcolor: 'rgba(255,255,255,0.9)', bordercolor: '#e2e8f0', borderwidth: 1 },
                    plot_bgcolor: 'white', paper_bgcolor: 'white',
                    annotations: olAnnotations,
                }, swParetoConfig);
            }
        }
    }

    // Architecture comparison — use subplots side by side instead of overlaying
    if (charts.architecture.architectures.length && document.getElementById(cid('chart-arch'))) {
        const arch = charts.architecture;
        const archLabels = arch.architectures;
        const ttftTrace = {
            x: archLabels, y: arch.avg_ttft, type: 'bar',
            marker: { color: '#3b82f6' },
            text: arch.avg_ttft.map(v => fmtSI(v) + ' ms'), textposition: 'auto',
            name: 'Avg TTFT P90', xaxis: 'x', yaxis: 'y'
        };
        const bestTtftTrace = {
            x: archLabels, y: arch.best_ttft, type: 'bar',
            marker: { color: '#93c5fd' },
            text: arch.best_ttft.map(v => fmtSI(v) + ' ms'), textposition: 'auto',
            name: 'Best TTFT P90', xaxis: 'x', yaxis: 'y'
        };
        const tputTrace = {
            x: archLabels, y: arch.avg_throughput, type: 'bar',
            marker: { color: '#f59e0b' },
            text: arch.avg_throughput.map(v => v.toFixed(2) + ' req/s'), textposition: 'auto',
            name: 'Avg Throughput P90', xaxis: 'x2', yaxis: 'y2'
        };
        safePlot(cid('chart-arch'), [ttftTrace, bestTtftTrace, tputTrace], {
            ...plotlyLayout,
            margin: { t: 30, b: 50, l: 60, r: 60 },
            barmode: 'group',
            showlegend: true, legend: { x: 0, y: 1.18, orientation: 'h' },
            xaxis: { domain: [0, 0.45], title: '' },
            yaxis: { title: 'TTFT (ms) - lower is better', titlefont: { color: '#3b82f6' } },
            xaxis2: { domain: [0.55, 1], title: '', anchor: 'y2' },
            yaxis2: { title: 'Throughput Mean (req/s) - higher is better', anchor: 'x2', titlefont: { color: '#f59e0b' } },
        }, plotlyConfig);
    }

    // Percentile comparison bar chart (Winner vs Aggregated)
    if (rec && rec.recommendations && document.getElementById(cid('chart-percentile-bars'))) {
        const primaryKey = rec.goal === 'ttft' ? 'response_time' : 'throughput';
        const primaryRec = rec.recommendations[primaryKey];
        const aggBase = rec.aggregated_baseline;
        if (primaryRec && primaryRec.config.percentiles && aggBase && aggBase.percentiles) {
            const pp = primaryRec.config.percentiles;
            const ap = aggBase.percentiles;
            const primaryArch = primaryRec.architecture || 'PD';
            const pctls = ['p50', 'p90', 'p95', 'p99'];
            const ttftTraces = [
                { x: pctls, y: pctls.map(p => pp.ttft[p]), name: primaryArch + ' TTFT', type: 'bar', marker: { color: '#3b82f6' } },
                { x: pctls, y: pctls.map(p => ap.ttft[p]), name: 'Aggregated TTFT', type: 'bar', marker: { color: '#94a3b8' } },
            ];
            const tputTraces = [
                { x: pctls, y: pctls.map(p => pp.throughput[p]), name: primaryArch + ' Throughput', type: 'bar', marker: { color: '#10b981' }, xaxis: 'x2', yaxis: 'y2' },
                { x: pctls, y: pctls.map(p => ap.throughput[p]), name: 'Aggregated Throughput', type: 'bar', marker: { color: '#d1d5db' }, xaxis: 'x2', yaxis: 'y2' },
            ];
            safePlot(cid('chart-percentile-bars'), [...ttftTraces, ...tputTraces], {
                ...plotlyLayout,
                margin: { t: 30, b: 50, l: 60, r: 60 },
                barmode: 'group',
                showlegend: true, legend: { x: 0, y: 1.18, orientation: 'h' },
                xaxis: { domain: [0, 0.45], title: 'Percentile' },
                yaxis: { title: 'TTFT (ms) — lower is better', titlefont: { color: '#3b82f6' } },
                xaxis2: { domain: [0.55, 1], title: 'Percentile', anchor: 'y2' },
                yaxis2: { title: 'Throughput (req/s) — higher is better', anchor: 'x2', titlefont: { color: '#10b981' } },
            }, plotlyConfig);
        }
    }

    // TP Calibration charts (Step 2 decode, Step 3 prefill)
    // Render empty chart when no data so the card isn't blank
    if (rec && document.getElementById(cid('chart-tp-decode')) && !(rec.decode_tp_all && rec.decode_tp_all.length)) {
        safePlot(cid('chart-tp-decode'), [], {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            xaxis: { title: 'TP Value' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left' },
            yaxis2: { title: 'ITL P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }
    if (rec && document.getElementById(cid('chart-tp-prefill')) && !(rec.prefill_tp_all && rec.prefill_tp_all.length)) {
        safePlot(cid('chart-tp-prefill'), [], {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            xaxis: { title: 'TP Value' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left' },
            yaxis2: { title: 'TTFT P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }
    if (rec && rec.decode_tp_all && rec.decode_tp_all.length && document.getElementById(cid('chart-tp-decode'))) {
        const dtp = rec.decode_tp_all;
        const tpLabels = dtp.map(d => `TP=${d.tp}`);
        const tpsgVals = dtp.map(d => d.tpsg);
        const bestTpsg = Math.max(...tpsgVals);
        const barColors = tpsgVals.map(v => v === bestTpsg ? '#10b981' : '#6366f1');
        const itlVals = dtp.map(d => d.itl_p90 != null ? d.itl_p90 : 0);
        const traces = [
            { x: tpLabels, y: tpsgVals, name: 'Tokens/s/GPU', type: 'bar', marker: { color: barColors },
              text: tpsgVals.map(v => fmtSI(v)), textposition: 'outside',
              textfont: { size: 14, color: '#1e293b' }, cliponaxis: false, constraintext: 'none',
              hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>' },
        ];
        if (itlVals.some(v => v > 0)) {
            traces.push({
                x: tpLabels, y: itlVals, name: 'ITL P90 (ms)', type: 'scatter', mode: 'lines+markers+text', yaxis: 'y2',
                line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'circle', color: '#ef4444', line: { width: 2, color: 'white' } },
                text: itlVals.map((v, idx) => dtp[idx].itl_p90 != null ? v.toFixed(1) : ''),
                textposition: 'bottom center', textfont: { size: 14, color: '#ef4444' },
                hovertemplate: '<b>%{x}</b><br>ITL P90: %{y:.2f} ms<extra></extra>',
            });
        }
        safePlot(cid('chart-tp-decode'), traces, {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            barmode: 'group', showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left', tickformat: '.2s' },
            yaxis2: { title: 'ITL P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }

    if (rec && rec.prefill_tp_all && rec.prefill_tp_all.length && document.getElementById(cid('chart-tp-prefill'))) {
        const ptp = rec.prefill_tp_all;
        const tpLabels = ptp.map(d => `TP=${d.tp}`);
        const tpsgVals = ptp.map(d => d.tpsg);
        const bestTpsg = Math.max(...tpsgVals);
        const barColors = tpsgVals.map(v => v === bestTpsg ? '#10b981' : '#6366f1');
        const ttftVals = ptp.map(d => d.ttft_p90 != null ? d.ttft_p90 : 0);
        const traces = [
            { x: tpLabels, y: tpsgVals, name: 'Tokens/s/GPU', type: 'bar', marker: { color: barColors },
              text: tpsgVals.map(v => fmtSI(v)), textposition: 'outside',
              textfont: { size: 14, color: '#1e293b' }, cliponaxis: false, constraintext: 'none',
              hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>' },
        ];
        if (ttftVals.some(v => v > 0)) {
            traces.push({
                x: tpLabels, y: ttftVals, name: 'TTFT P90 (ms)', type: 'scatter', mode: 'lines+markers+text', yaxis: 'y2',
                line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'circle', color: '#ef4444', line: { width: 2, color: 'white' } },
                text: ttftVals.map((v, idx) => ptp[idx].ttft_p90 != null ? Math.round(v).toLocaleString() : ''),
                textposition: 'bottom center', textfont: { size: 14, color: '#ef4444' },
                hovertemplate: '<b>%{x}</b><br>TTFT P90: %{y:.1f} ms<extra></extra>',
            });
        }
        safePlot(cid('chart-tp-prefill'), traces, {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            barmode: 'group', showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left', tickformat: '.2s' },
            yaxis2: { title: 'TTFT P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }

    // PD configurations TTFT charts (one per percentile)
    [{key:'p90',field:'ttft_p90',itlField:'itl_p90',color:'#3b82f6',chartId:'chart-pd-ttft-p90'},
     {key:'p95',field:'ttft_p95',itlField:'itl_p95',color:'#dc2626',chartId:'chart-pd-ttft-p95'},
     {key:'p99',field:'ttft_p99',itlField:'itl_p99',color:'#7c3aed',chartId:'chart-pd-ttft-p99'}
    ].forEach(function(pctl) {
        var archResults = coreResults.filter(r => r.architecture === 'PD');
        if (!archResults.length) return;
        var sorted = archResults.slice().sort((a, b) => a.prefill_pods - b.prefill_pods);
        var labels = sorted.map(r => _tagLabel(r.config_name, r.architecture));
        var ttftVals = sorted.map(r => r[pctl.field]);
        var tputVals = sorted.map(r => r.throughput_mean || r.throughput_p90);
        var itlVals = sorted.map(r => r[pctl.itlField] != null ? r[pctl.itlField] : null);
        var hasItl = itlVals.some(v => v != null);
        var validTtft = ttftVals.filter(v => v != null);
        if (!validTtft.length) return;
        var bestTtftIdx = ttftVals.indexOf(Math.min.apply(null, validTtft));
        var bestTputIdx = tputVals.indexOf(Math.max.apply(null, tputVals));
        var pLabel = pctl.key.toUpperCase();

        var el = document.getElementById(cid(pctl.chartId));
        if (!el) return;

        var traces = [
            { x: labels, y: ttftVals, type: 'scatter', mode: 'lines+markers', name: 'TTFT ' + pLabel,
              line: { color: pctl.color, width: 3, shape: 'spline' },
              marker: { color: pctl.color, size: 12, symbol: 'circle', line: { width: 2, color: 'white' } },
              fill: 'tozeroy', fillcolor: pctl.color + '14' },
            { x: [labels[bestTtftIdx]], y: [validTtft[0]], type: 'scatter', mode: 'markers', name: 'Best TTFT',
              marker: { color: '#10b981', size: 22, symbol: 'circle', line: { width: 3, color: 'white' } }, showlegend: true },
            { x: labels, y: tputVals, type: 'scatter', mode: 'lines+markers', name: 'Throughput Mean', yaxis: 'y2',
              line: { color: '#f59e0b', width: 3, shape: 'spline' },
              marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } } },
            { x: [labels[bestTputIdx]], y: [tputVals[bestTputIdx]], type: 'scatter', mode: 'markers', name: 'Best Throughput', yaxis: 'y2',
              marker: { color: '#e11d48', size: 22, symbol: 'diamond', line: { width: 3, color: 'white' } }, showlegend: true },
        ];

        var layout = {
            height: 850,
            margin: { t: 30, b: 180, l: 60, r: 60 },
            xaxis: { title: { text: 'Configuration', standoff: 20 }, tickangle: -40, anchor: hasItl ? 'y3' : 'y' },
            yaxis: { title: 'TTFT ' + pLabel + ' (ms)', side: 'left', titlefont: { color: pctl.color }, tickfont: { color: pctl.color }, tickformat: '.2s', domain: hasItl ? [0.28, 1] : [0, 1] },
            yaxis2: { title: 'Throughput Mean (req/s)', side: 'right', overlaying: 'y', titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
            showlegend: true,
            legend: { x: 0, y: 1.12, orientation: 'h' },
        };

        if (hasItl) {
            var validItl = itlVals.filter(v => v != null);
            var bestItlIdx = itlVals.indexOf(Math.min.apply(null, validItl));
            traces.push(
                { x: labels, y: itlVals, type: 'scatter', mode: 'lines+markers', name: 'ITL ' + pLabel, yaxis: 'y3',
                  line: { color: '#ef4444', width: 2, shape: 'spline' },
                  marker: { color: '#ef4444', size: 8, symbol: 'square', line: { width: 1, color: 'white' } }, connectgaps: true },
                { x: [labels[bestItlIdx]], y: [itlVals[bestItlIdx]], type: 'scatter', mode: 'markers', name: 'Best ITL', yaxis: 'y3',
                  marker: { color: '#10b981', size: 16, symbol: 'square', line: { width: 2, color: 'white' } }, showlegend: true }
            );
            layout.yaxis3 = { title: 'ITL ' + pLabel + ' (ms)', side: 'left', titlefont: { color: '#ef4444', size: 11 }, tickfont: { color: '#ef4444', size: 10 }, domain: [0, 0.22] };
        }

        Plotly.newPlot(el, traces, layout, { ...plotlyConfig, toImageButtonOptions: { format: 'png', width: 1400, height: 850, scale: 2 } });
    });

    // ============================================================
    // Aggregated configurations chart (all percentiles in one grouped bar chart)
    // ============================================================
    const aggResults = coreResults.filter(r => r.architecture === 'AGGREGATED' && r.ttft_p90);
    if (aggResults.length > 1 && document.getElementById(cid('chart-agg-ttft-all'))) {
        const aggSorted = [...aggResults].sort((a, b) => (a.tp || 1) - (b.tp || 1));
        const aggLabels = aggSorted.map(r => `${r.replicas || Math.floor(r.gpus / (r.tp || 1))}×TP${r.tp || '?'}${(r.pp && r.pp > 1) ? ' PPx' + r.pp : ''}`);
        const aggColors = { p90: '#3b82f6', p95: '#dc2626', p99: '#7c3aed' };
        const traces = [];
        ['p90', 'p95', 'p99'].forEach(p => {
            traces.push({
                x: aggLabels,
                y: aggSorted.map(r => r['ttft_' + p]),
                name: `TTFT ${p.toUpperCase()}`,
                type: 'bar',
                marker: { color: aggColors[p], opacity: 0.85 },
                hovertemplate: `%{x}<br>TTFT ${p.toUpperCase()}: %{y:.1f} ms<extra></extra>`,
            });
        });
        const aggTputVals = aggSorted.map(r => r.throughput_mean || r.throughput_p90);
        traces.push({
            x: aggLabels, y: aggTputVals,
            name: 'Throughput Mean',
            type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
            line: { color: '#f59e0b', width: 3, shape: 'spline' },
            marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } },
            hovertemplate: 'Throughput Mean: %{y:.2f} req/s<extra></extra>',
        });
        const itlDashes = { p90: 'solid', p95: 'dash', p99: 'dot' };
        const itlSymbols = { p90: 'square', p95: 'square-open', p99: 'square-open-dot' };
        const hasAggItl = aggSorted.some(r => r.itl_p90 != null);
        if (hasAggItl) {
            ['p90', 'p95', 'p99'].forEach(p => {
                traces.push({
                    x: aggLabels,
                    y: aggSorted.map(r => r['itl_' + p]),
                    name: `ITL ${p.toUpperCase()}`,
                    type: 'scatter', mode: 'lines+markers', yaxis: 'y3',
                    line: { color: '#ef4444', width: 2, dash: itlDashes[p] },
                    marker: { color: '#ef4444', size: 7, symbol: itlSymbols[p] },
                    hovertemplate: `ITL ${p.toUpperCase()}: %{y:.2f} ms<extra></extra>`,
                    connectgaps: true,
                });
            });
        }
        safePlot(cid('chart-agg-ttft-all'), traces, {
            ...plotlyLayout,
            height: 850,
            barmode: 'group',
            margin: { t: 30, b: 80, l: 60, r: hasAggItl ? 110 : 60 },
            xaxis: { title: 'Aggregated Configuration', domain: hasAggItl ? [0, 0.96] : [0, 1] },
            yaxis: { title: 'TTFT (ms) — lower is better', side: 'left', tickformat: '.2s' },
            yaxis2: { title: 'Throughput Mean (req/s)', side: 'right', overlaying: 'y', anchor: hasAggItl ? 'free' : 'x', position: hasAggItl ? 0.97 : 1, titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
            ...(hasAggItl ? { yaxis3: { title: 'ITL (ms)', side: 'right', overlaying: 'y', anchor: 'free', position: 1, titlefont: { color: '#ef4444', size: 11 }, tickfont: { color: '#ef4444', size: 10 } } } : {}),
            showlegend: true,
            legend: { x: 0, y: 1.18, orientation: 'h' },
        }, plotlyConfig);
    }

    // ============================================================
    // STEP 9: Latency Search — Plotly rendering
    // ============================================================
    if (data.latency_search && data.latency_search.by_architecture) {
        const byArch = data.latency_search.by_architecture;
        const archKeys = Object.keys(byArch);
        const s9ArchConfigs = data.latency_search.arch_configs || {};
        const firstTrial = data.latency_search.trials[0];
        const targetMs = firstTrial.target_ms;
        const targetPct = firstTrial.target_percentile || 'p90';
        const metricKey = 'ttft_' + targetPct;

        archKeys.forEach((arch, ai) => {
            const trials = byArch[arch];
            const cfgLabel = s9ArchConfigs[arch] || arch.toUpperCase();

            // Skip architectures with no valid data
            if (!trials.some(t => t.ttft_p90 != null)) return;

            // Sort by concurrency for clean x-axis
            const sorted = [...trials].filter(t => t.ttft_p90 != null).sort((a, b) => a.concurrency - b.concurrency);
            const xLabels = sorted.map(t => `c=${t.concurrency}`);

            const pctlCharts = [
                { key: 'p90', ttftField: 'ttft_p90', tputField: 'throughput_mean', color: '#3b82f6', divId: `step9-chart-p90-${ai}` },
                { key: 'p95', ttftField: 'ttft_p95', tputField: 'throughput_mean', color: '#dc2626', divId: `step9-chart-p95-${ai}` },
                { key: 'p99', ttftField: 'ttft_p99', tputField: 'throughput_mean', color: '#7c3aed', divId: `step9-chart-p99-${ai}` },
            ];

            pctlCharts.forEach(pctl => {
                const el = document.getElementById(cid(pctl.divId));
                if (!el) return;
                const pLabel = pctl.key.toUpperCase();
                const latencies = sorted.map(t => t[pctl.ttftField]);
                const throughputs = sorted.map(t => t[pctl.tputField] || t.throughput_mean || t.throughput_p90);

                const hoverTexts = sorted.map((t, i) =>
                    `<b>${cfgLabel} c=${t.concurrency}</b><br>` +
                    `TTFT ${pLabel}: <b>${latencies[i] != null ? latencies[i].toFixed(1) : '-'} ms</b><br>` +
                    `Throughput Mean: ${throughputs[i] != null ? throughputs[i].toFixed(2) : '-'} req/s<br>` +
                    `SLA (${targetPct.toUpperCase()}): ${t.meets_sla ? '<span style="color:#10b981">PASS</span>' : '<span style="color:#ef4444">FAIL</span>'}`
                );

                // SLA markers based on target percentile (only color the target pctl chart)
                const isTargetPctl = pctl.key === targetPct;
                const markerColors = isTargetPctl
                    ? sorted.map(t => t.meets_sla ? '#10b981' : '#ef4444')
                    : sorted.map(() => pctl.color);

                // Find best passing (highest throughput meeting SLA) — only for target percentile
                let bestIdx = -1;
                if (isTargetPctl) {
                    let bestTput = -1;
                    sorted.forEach((t, i) => {
                        if (t.meets_sla && throughputs[i] != null && throughputs[i] > bestTput) {
                            bestTput = throughputs[i];
                            bestIdx = i;
                        }
                    });
                } else {
                    // For non-target percentiles, mark the best TTFT
                    let bestTtft = Infinity;
                    latencies.forEach((v, i) => { if (v != null && v < bestTtft) { bestTtft = v; bestIdx = i; } });
                }

                const traces = [
                    {
                        x: xLabels, y: latencies, name: `TTFT ${pLabel}`,
                        type: 'scatter', mode: 'lines+markers',
                        line: { color: pctl.color, width: 3, shape: 'spline' },
                        marker: { color: markerColors, size: 12, symbol: 'circle', line: { width: 2, color: 'white' } },
                        hovertext: hoverTexts, hoverinfo: 'text',
                        fill: 'tozeroy', fillcolor: pctl.color + '14',
                    },
                    {
                        x: xLabels, y: throughputs, name: `Throughput Mean`,
                        type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                        line: { color: '#f59e0b', width: 3, shape: 'spline' },
                        marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } },
                        hovertemplate: `c=%{x}: %{y:.2f} req/s<extra></extra>`,
                    },
                ];

                if (bestIdx >= 0) {
                    traces.push({
                        x: [xLabels[bestIdx]], y: [latencies[bestIdx]],
                        name: isTargetPctl ? 'Best (meets SLA)' : `Best TTFT ${pLabel}`,
                        type: 'scatter', mode: 'markers',
                        marker: { color: '#10b981', size: 22, symbol: 'circle', line: { width: 3, color: 'white' } },
                        hovertext: [hoverTexts[bestIdx]], hoverinfo: 'text', showlegend: true,
                    });
                    traces.push({
                        x: [xLabels[bestIdx]], y: [throughputs[bestIdx]],
                        name: isTargetPctl ? 'Best Throughput (meets SLA)' : `Best Throughput ${pLabel}`,
                        type: 'scatter', mode: 'markers', yaxis: 'y2',
                        marker: { color: '#e11d48', size: 22, symbol: 'diamond', line: { width: 3, color: 'white' } },
                        hovertext: [hoverTexts[bestIdx]], hoverinfo: 'text', showlegend: true,
                    });
                }

                // SLA line (shown on all percentile charts for reference)
                const shapes = [];
                const chartAnnotations = [];
                const slaLabel = isTargetPctl ? `SLA Target (${targetPct.toUpperCase()}): ${targetMs} ms` : `SLA Target (${targetPct.toUpperCase()}): ${targetMs} ms`;
                shapes.push({
                    type: 'line', x0: -0.5, x1: xLabels.length - 0.5,
                    y0: targetMs, y1: targetMs, yref: 'y',
                    line: { color: '#ef4444', width: isTargetPctl ? 2 : 1.5, dash: 'dash' },
                });
                chartAnnotations.push({
                    x: xLabels.length - 1, y: targetMs, yref: 'y',
                    text: slaLabel, showarrow: false,
                    font: { color: '#ef4444', size: 11, weight: 700 },
                    xanchor: 'right', yanchor: 'bottom', yshift: 5,
                    bgcolor: 'rgba(255,255,255,0.85)',
                });

                Plotly.newPlot(el, traces, {
                    ...plotlyLayout, height: 850,
                    margin: { t: 30, b: 80, l: 60, r: 60 },
                    xaxis: { title: 'Concurrent Users' },
                    yaxis: { title: `TTFT ${pLabel} (ms) — lower is better`, side: 'left', titlefont: { color: pctl.color }, tickfont: { color: pctl.color }, tickformat: '.2s' },
                    yaxis2: { title: `Throughput Mean (req/s) — higher is better`, side: 'right', overlaying: 'y', titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
                    showlegend: true, legend: { x: 0, y: 1.18, orientation: 'h' },
                    shapes, annotations: chartAnnotations,
                }, plotlyConfig);
            });
        });
    }

    // ============================================================
    // vLLM ENGINE METRICS — Plotly rendering
    // ============================================================
   if (charts.vllm && charts.vllm.configs.length) {
        const vllm = charts.vllm;
        const vllmLayout = { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 100 }, showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' } };
        const pColors = { p50: '#60a5fa', p90: '#3b82f6', p95: '#f59e0b', p99: '#ef4444' };
        const pctiles = ['P50', 'P90', 'P95', 'P99'];

        // Sort configs by architecture group then P90 value
        function sortedIndices(p90arr, cfgs) {
            var indices = cfgs.map(function(_, i) { return i; });
            var archOrder = { 'aggregated': 0, 'pd': 1, 'ep': 2 };
            function getArch(label) {
                if (label.indexOf('(AG)') >= 0 || label.indexOf('(aggregated)') >= 0) return 'aggregated';
                if (label.indexOf('(EP)') >= 0 || label.indexOf('(ep)') >= 0) return 'ep';
                return 'pd';
            }
            indices.sort(function(a, b) {
                var aa = archOrder[getArch(cfgs[a])] || 1, ba = archOrder[getArch(cfgs[b])] || 1;
                if (aa !== ba) return aa - ba;
                return (p90arr[a] || 0) - (p90arr[b] || 0);
            });
            return indices;
        }
        function reorder(arr, idx) { return idx.map(function(i) { return arr[i]; }); }
        // Group configs by architecture, sort by P90 within each
        var archColors = { aggregated: '#059669', pd: '#2563eb', ep: '#7c3aed' };
        var archLabels = { aggregated: 'Aggregated', pd: 'PD', ep: 'EP' };
        function getArch(label) {
            if (label.indexOf('(aggregated)') >= 0) return 'aggregated';
            if (label.indexOf('(ep)') >= 0) return 'ep';
            return 'pd';
        }
        function makeArchLines(data, unit) {
            var byArch = {};
            var bestByCfg = {};
            vllm.configs.forEach(function(cfg, i) {
                var existing = bestByCfg[cfg];
                if (!existing || (data.p90[i] || 1e9) < (existing.p90 || 1e9)) {
                    bestByCfg[cfg] = { cfg: cfg, p50: data.p50[i], p90: data.p90[i], p95: data.p95[i], p99: data.p99[i] };
                }
            });
            Object.keys(bestByCfg).forEach(function(cfg) {
                var a = getArch(cfg);
                if (!byArch[a]) byArch[a] = [];
                byArch[a].push(bestByCfg[cfg]);
            });
            var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = byArch[a];
                if (!pts || !pts.length) return;
                pts.sort(function(x, y) { return (x.p90 || 0) - (y.p90 || 0); });
                var color = archColors[a];
                var label = archLabels[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return p.p90; }),
                    name: label + ' P90', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>P90: %{y:,.0f} ' + unit + '<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return p.p99; }),
                    name: label + ' P99', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color, symbol: 'diamond' },
                    hovertemplate: '<b>%{x}</b><br>P99: %{y:,.0f} ' + unit + '<extra></extra>' });
            });
            return traces;
        }

        // Chart 1: TTFT — lines per architecture, sorted by P90
        safePlot(cid('chart-vllm-ttft'), makeArchLines(vllm.ttft, 'ms'),
            { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'TTFT (ms) — lower is better' } }, plotlyConfig);

        // Chart 2: ITL — lines per architecture, sorted by P90
        safePlot(cid('chart-vllm-itl'), makeArchLines(vllm.itl, 'ms'),
            { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'ITL (ms) — lower is better' } }, plotlyConfig);

        // Chart 3: E2E Latency — lines per architecture, sorted by P90
        safePlot(cid('chart-vllm-e2e'), makeArchLines(vllm.e2e, 's'),
            { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'E2E Latency (s) — lower is better' } }, plotlyConfig);

        // Dedup helper for non-percentile metrics: best by lowest P90-equivalent (or first seen)
        function dedupByConfig(arrays) {
            var best = {};
            vllm.configs.forEach(function(cfg, i) {
                if (!best[cfg]) { best[cfg] = i; }
            });
            var cfgs = Object.keys(best);
            var byArch = {};
            cfgs.forEach(function(cfg) {
                var a = getArch(cfg);
                if (!byArch[a]) byArch[a] = [];
                byArch[a].push({ cfg: cfg, idx: best[cfg] });
            });
            return { cfgs: cfgs, byArch: byArch };
        }

        // Chart 4: Token Throughput — lines per architecture
        (function() {
            var dd = dedupByConfig();
            var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a];
                if (!pts || !pts.length) return;
                pts.sort(function(x, y) { return (vllm.token_rates.generation[x.idx] || 0) - (vllm.token_rates.generation[y.idx] || 0); });
                var color = archColors[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.token_rates.prompt[p.idx]; }),
                    name: archLabels[a] + ' Prompt', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                    hovertemplate: '<b>%{x}</b><br>Prompt: %{y:,.0f} tok/s<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.token_rates.generation[p.idx]; }),
                    name: archLabels[a] + ' Generation', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Generation: %{y:,.0f} tok/s<extra></extra>' });
            });
            safePlot(cid('chart-vllm-tokens'), traces, { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Tokens/second — higher is better' } }, plotlyConfig);
        })();

        // Chart 5: Request Queue & KV Cache — lines per architecture
        (function() {
            var dd = dedupByConfig();
            var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a];
                if (!pts || !pts.length) return;
                var color = archColors[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.request_state.running[p.idx]; }),
                    name: archLabels[a] + ' Running', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Running: %{y:.1f}<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.request_state.waiting[p.idx]; }),
                    name: archLabels[a] + ' Waiting', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color, symbol: 'diamond' },
                    hovertemplate: '<b>%{x}</b><br>Waiting: %{y:.1f}<extra></extra>' });
            });
            traces.push({ x: dd.cfgs, y: dd.cfgs.map(function(c) { return vllm.request_state.kv_cache[dedupByConfig().byArch[getArch(c)].find(function(p){return p.cfg===c}).idx]; }),
                name: 'KV Cache %', mode: 'lines+markers', yaxis: 'y2', line: { color: '#f59e0b', width: 2 }, marker: { size: 10, color: '#f59e0b', symbol: 'diamond' },
                hovertemplate: '<b>%{x}</b><br>KV Cache: %{y:.1f}%<extra></extra>' });
            safePlot(cid('chart-vllm-queue'), traces, {
                ...vllmLayout, margin: { ...vllmLayout.margin, r: 60 }, xaxis: { tickangle: -35 },
                yaxis: { title: 'Request Count (avg)', side: 'left' },
                yaxis2: { title: 'KV Cache Usage (%)', side: 'right', overlaying: 'y', range: [0, 105], titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
            }, plotlyConfig);
        })();

        // Chart 6: Processing Time — lines per architecture
        (function() {
            var dd = dedupByConfig();
            var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a];
                if (!pts || !pts.length) return;
                var color = archColors[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.time_breakdown.prefill[p.idx]; }),
                    name: archLabels[a] + ' Prefill', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Prefill: %{y:.2f}<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.time_breakdown.decode[p.idx]; }),
                    name: archLabels[a] + ' Decode', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                    hovertemplate: '<b>%{x}</b><br>Decode: %{y:.2f}<extra></extra>' });
            });
            var allPts = [];
            Object.keys(dd.byArch).forEach(function(a) { dd.byArch[a].forEach(function(p) { allPts.push(p); }); });
            traces.push({ x: allPts.map(function(p) { return p.cfg; }), y: allPts.map(function(p) { return vllm.time_breakdown.preemptions[p.idx]; }),
                name: 'Preemptions/s', mode: 'markers', yaxis: 'y2', marker: { size: 10, color: '#ef4444', symbol: 'triangle-up' },
                hovertemplate: '<b>%{x}</b><br>Preemptions: %{y:.1f}/s<extra></extra>' });
            safePlot(cid('chart-vllm-time'), traces, {
                ...vllmLayout, margin: { ...vllmLayout.margin, r: 60 }, xaxis: { tickangle: -35 },
                yaxis: { title: 'Time Rate (s/s)', side: 'left' },
                yaxis2: { title: 'Preemptions/s', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
            }, plotlyConfig);
        })();

        // Chart 7: Pod Network Throughput — lines per architecture
        if (vllm.network && vllm.network.pod_tx.some(v => v > 0)) {
            var dd = dedupByConfig();
            var netTraces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a];
                if (!pts || !pts.length) return;
                var color = archColors[a];
                netTraces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.network.pod_tx[p.idx]; }),
                    name: archLabels[a] + ' TX', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>TX: %{y:.2f} MB/s<extra></extra>' });
                netTraces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.network.pod_rx[p.idx]; }),
                    name: archLabels[a] + ' RX', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                    hovertemplate: '<b>%{x}</b><br>RX: %{y:.2f} MB/s<extra></extra>' });
            });
            safePlot(cid('chart-net-pod'), netTraces, { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Throughput (MB/s)' } }, plotlyConfig);
        }

        // Chart 8: NIXL KV Transfer Throughput — lines per architecture
        var nixlData = vllm.network.nixl_tx || [];
        var ibData = vllm.network.ib_rx || [];
        if (nixlData.some(v => v > 0) || ibData.some(v => v > 0)) {
            var dd8 = dedupByConfig();
            var nixlTraces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd8.byArch[a];
                if (!pts || !pts.length) return;
                var color = archColors[a];
                if (nixlData.some(v => v > 0)) {
                    nixlTraces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return nixlData[p.idx]; }),
                        name: archLabels[a] + ' NIXL TX', mode: 'lines+markers+text',
                        text: pts.map(function(p) { var v = nixlData[p.idx]; return v > 0 ? v.toFixed(2) : ''; }),
                        textposition: 'top center', textfont: { size: 13, color: color },
                        line: { color: color, width: 3 }, marker: { size: 10, color: color },
                        hovertemplate: '<b>%{x}</b><br>NIXL TX: %{y:.2f} GB/s<extra></extra>' });
                }
                if (ibData.some(v => v > 0)) {
                    nixlTraces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return ibData[p.idx]; }),
                        name: archLabels[a] + ' IB RX', mode: 'lines+markers',
                        line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                        hovertemplate: '<b>%{x}</b><br>IB RX: %{y:.2f} GB/s<extra></extra>' });
                }
            });
            safePlot(cid('chart-net-ib'), nixlTraces,
                { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Throughput (GB/s)' } }, plotlyConfig);
        }
    }

    // Execute deferred chart renders (concurrency sweep, cache sweep)
    chartQueue.forEach(function(item) {
        if (typeof item === 'function') {
            try { item(); } catch(e) { console.warn('Deferred chart render failed:', e); }
        } else if (item && item.id && item.traces) {
            var el = document.getElementById(item.id);
            if (el) Plotly.newPlot(el, item.traces, item.layout || {}, { responsive: true });
        }
    });

    // Initialize subtab switching (activates first pane, hides others)
    initReportSubtabs(content);

    // Resize all charts in the active pane after tabs are set up
    setTimeout(function() {
        content.querySelectorAll('.js-plotly-plot').forEach(function(plot) {
            if (plot.offsetParent !== null) Plotly.Plots.resize(plot);
        });
    }, 100);
}

