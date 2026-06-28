// charts.js — Plotly chart rendering for all report visualizations

function renderCharts(data, runId) {
    const content = document.getElementById('charts-content');
    const summary = data.summary;
    const chartQueue = [];
    const charts = data.charts;
    const rec = data.recommendation;

    // Download link handled by tab management
    const dlLink = document.getElementById('chart-download-link');
    dlLink.style.display = 'inline';
    dlLink.href = '#';
    dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(runId, data); };

    let html = '';
    let secRec = '', secTP = '', secCfg = '', secCmp = '', secStep9 = '', secCal = '', secCacheSweep = '', secVLLM = '', secTestCfg = '', secEppTuning = '', secDeployTiming = '';

    // Filter out sweep tests (concurrency/cache) from configuration charts
    var coreResults = (data.all_results || []).filter(function(r) {
        var tid = r.test_id || r.config_name || '';
        return tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
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
        const gColors = { ttft: '#3b82f6', throughput: '#f59e0b', balanced: '#10b981', aggregated_only: '#64748b', pd_only: '#8b5cf6', ep_only: '#0ea5e9' };
        const gIcons = { ttft: '&#9201;', throughput: '&#9889;', balanced: '&#9878;', aggregated_only: '&#9634;', pd_only: '&#8644;', ep_only: '&#9881;' };
        const gc = gColors[rec.goal] || '#10b981';
        html += `<div class="chart-card" style="border: 3px solid ${gc}; border-left: 8px solid ${gc};">`;
        html += `<div class="chart-card-header" style="background: ${gc}; color: white; font-size: 1.3em;">`;
        html += `${gIcons[rec.goal] || ''} ${rec.goal_info.name}</div>`;
        html += `<div style="background:${gc}dd; color:white; padding:8px 20px; font-size:0.92em; display:flex; flex-wrap:wrap; gap:6px 20px;">`;
        html += `<span>Model: <strong>${rec.model}</strong></span>`;
        let wlLabel = `ISL: <strong>${rec.workload.isl}</strong>`;
        if (rec.workload.isl_stdev) wlLabel += ` (σ=${rec.workload.isl_stdev})`;
        wlLabel += ` | OSL: <strong>${rec.workload.osl}</strong>`;
        if (rec.workload.osl_stdev) wlLabel += ` (σ=${rec.workload.osl_stdev})`;
        if (rec.workload.turns && rec.workload.turns > 1) wlLabel += ` | Turns: <strong>${rec.workload.turns}</strong>`;
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
        html += '<div class="chart-card-header" style="background: linear-gradient(135deg, #059669, #10b981); color: white; font-size: 1.1em; font-weight: 800;">Deployment Recommendation (Steps 6-8)</div>';
        html += '<div style="padding:12px 20px 4px; color:#475569; font-size:0.9em;">Best configurations found during optimization. Each architecture\'s best TTFT is shown at P90, P95, and P99 — the config on the left has the lowest latency at that percentile.</div>';
        html += '<div class="chart-card-body" style="padding: 24px;">';

        // Recommendation cards — 2 columns sorted by TTFT (best left, second right), P90/P95/P99 stacked
        const bp = rec.best_by_percentile || {};
        const selTypes = [
            { key: 'balanced', label: 'Best Balanced', desc: 'Best TTFT-to-throughput ratio — the sweet spot', color: '#059669', icon: '&#9878;' },
            { key: 'lowest_ttft', label: 'Lowest TTFT', desc: 'Fastest time to first token', color: '#3b82f6', icon: '&#9201;' },
            { key: 'highest_tput', label: 'Highest Throughput', desc: 'Maximum requests per second', color: '#f59e0b', icon: '&#9889;' },
        ];
        const fallbackTs = (rec.recommendations.response_time || rec.recommendations.throughput || {}).config?.test_settings;
        const archColors = { pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed' };

        // Loop: architecture → 3 cards in a row (balanced, lowest TTFT, highest throughput)
        ['pd', 'aggregated', 'ep'].forEach(archKey => {
            const hasData = ['p90', 'p95', 'p99'].some(p => (bp[p] || {})[archKey]);
            if (!hasData) return;

            const archLabel = archKey.toUpperCase();
            const aColor = archColors[archKey] || '#64748b';
            html += `<div style="font-weight:800; font-size:1.1em; color:${aColor}; margin:20px 0 10px; border-bottom:2px solid ${aColor}; padding-bottom:6px;">${archLabel} Configurations</div>`;
            html += `<div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; margin-bottom:16px;">`;

            const p90Data = (bp.p90 || {})[archKey];
            if (!p90Data) { html += '</div>'; return; }
            const isNewFormat = p90Data.balanced || p90Data.lowest_ttft || p90Data.highest_tput;

            const seen = new Set();
            selTypes.forEach(sel => {
                const cfg = isNewFormat ? p90Data[sel.key] : (sel.key === 'balanced' ? p90Data : null);
                if (!cfg) { html += '<div></div>'; return; }

                const testId = cfg.test_id || cfg.config_name || '';
                let dupNote = '';
                if (seen.has(testId)) { html += '<div></div>'; return; }
                seen.add(testId);
                if (isNewFormat) {
                    const otherMatches = selTypes.filter(s => s.key !== sel.key && p90Data[s.key] && (p90Data[s.key].test_id || p90Data[s.key].config_name) === testId);
                    if (otherMatches.length) dupNote = ' (also ' + otherMatches.map(s => s.label).join(', ') + ')';
                }

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

                html += `<div style="background:white; border:2px solid ${sel.color}; border-top:4px solid ${sel.color}; border-radius:10px; padding:14px;">`;
                html += `<div style="font-weight:800; color:${sel.color}; font-size:0.82em; text-transform:uppercase; margin-bottom:6px;">${sel.icon} ${sel.label}</div>`;
                if (dupNote) html += `<div style="color:#94a3b8;font-size:0.75em;margin-bottom:4px;">${dupNote}</div>`;
                html += `<div style="font-size:0.78em; color:#64748b; margin-bottom:8px;">${sel.desc}</div>`;
                html += `<div style="font-size:1.15em; font-weight:800; color:#1e293b; margin-bottom:6px;">${deploy}</div>`;

                const gpus = cfg.gpus || cfg.total_gpus;
                const tputMean = cfg.throughput_mean || cfg.throughput || cfg.throughput_p90 || '-';
                html += `<div style="font-size:0.85em; color:#475569; margin-bottom:8px;">Tput: <strong>${tputMean} req/s</strong> | ${gpus} GPUs</div>`;

                // P90/P95/P99 table
                html += '<table style="width:100%; border-collapse:collapse; font-size:0.82em;">';
                html += '<tr style="background:#f8fafc;"><th style="text-align:left;padding:3px 6px;color:#64748b;font-weight:600;"></th><th style="text-align:right;padding:3px 6px;color:#64748b;font-weight:600;">TTFT</th><th style="text-align:right;padding:3px 6px;color:#64748b;font-weight:600;">ITL</th></tr>';
                ['p90', 'p95', 'p99'].forEach(p => {
                    const pCfg = isNewFormat ? ((bp[p] || {})[archKey] || {})[sel.key] : (bp[p] || {})[archKey];
                    const ttft = pCfg ? (pCfg.ttft || pCfg['ttft_' + p]) : null;
                    const itl = pCfg ? (pCfg['itl_' + p] || pCfg.itl) : null;
                    html += `<tr style="border-top:1px solid #e2e8f0;"><td style="padding:3px 6px;font-weight:600;">${p.toUpperCase()}</td>`;
                    html += `<td style="text-align:right;padding:3px 6px;">${ttft != null ? ttft + ' ms' : '-'}</td>`;
                    html += `<td style="text-align:right;padding:3px 6px;">${itl != null ? itl + ' ms' : '-'}</td></tr>`;
                });
                html += '</table>';

                // Action buttons
                html += `<div style="display:flex;gap:4px;margin-top:8px;">`;
                html += `<button onclick="applyReportConfig('${recId}')" style="flex:1;background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 6px;cursor:pointer;color:#6b7280;font-size:12px;transition:all 0.15s;" onmouseover="this.style.borderColor='#2563eb';this.style.color='#2563eb';this.style.background='#eff6ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#128260; Reuse</button>`;
                html += `<button onclick="showSingleTestModal('${recId}')" style="flex:1;background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 6px;cursor:pointer;color:#6b7280;font-size:12px;transition:all 0.15s;" onmouseover="this.style.borderColor='#8b5cf6';this.style.color='#8b5cf6';this.style.background='#f5f3ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#129514; Test</button>`;
                html += `</div>`;

                // YAML downloads
                const recTestId = cfg.test_id || testIdLookup[cfg.config_name] || cfg.config_name;
                const recManifests = manifestLookup[recTestId] || cfg.manifest_types || [];
                if (recManifests.length) {
                    html += '<div style="margin-top:6px; padding-top:6px; border-top:1px solid #e2e8f0;">';
                    html += '<span style="font-size:0.72em; color:#64748b;">YAML: </span>';
                    recManifests.filter(t => !t.includes('service')).forEach(t => {
                        html += `<a href="/api/run/${runId}/config/${recTestId}/manifest/${t}" style="color:#0ea5e9; text-decoration:none; font-size:11px; padding:1px 5px; background:#f0f9ff; border-radius:3px; border:1px solid #bae6fd; margin:1px;">${t}</a>`;
                    });
                    html += '</div>';
                }
                html += '</div>';
            });

            html += '</div>';
        });

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

        // Constraint notes
        if (rec.constraint_notes && rec.constraint_notes.length) {
            html += '<div style="background:#fffbeb; border:2px solid #f59e0b; border-left:6px solid #f59e0b; border-radius:8px; padding:14px 18px; margin-top:12px;">';
            html += '<div style="font-weight:700; color:#92400e; margin-bottom:8px; font-size:0.95em;">&#9888; Configuration Constraints</div>';
            for (const note of rec.constraint_notes) {
                html += `<p style="color:#78350f; margin:0 0 8px; font-size:0.88em; line-height:1.6;">${note}</p>`;
            }
            html += '</div>';
        }

        // Grab test_settings from the first available P90 recommendation for EPP cards
        const _baseTestSettings = (rec.recommendations.response_time || rec.recommendations.throughput || {}).config?.test_settings || {};
        const _baseEppConfig = (rec.recommendations.response_time || rec.recommendations.throughput || {}).config?.epp_config || null;

        // --- EPP-Optimized Recommendation (from Step 11) ---
        if (data.epp_tuning && data.epp_tuning.by_architecture) {
            const eppArch = data.epp_tuning.by_architecture;
            const hasEppData = Object.values(eppArch).some(t => t && t.length > 0);
            if (hasEppData) {
                html += '<div style="margin-top:24px; border:2px solid #7c3aed; border-left:6px solid #7c3aed; border-radius:10px; overflow:hidden;">';
                html += '<div style="background:linear-gradient(135deg,#7c3aed,#8b5cf6); color:white; padding:14px 20px; font-size:1.1em; font-weight:800;">EPP-Optimized Recommendation (Step 9)</div>';
                html += '<div style="padding:12px 20px 4px; color:#475569; font-size:0.9em;">These results use the same deployment as above but with tuned EPP scoring weights. The gateway routes requests more efficiently, improving latency without changing the inference pods.</div>';
                html += '<div style="padding:16px 20px;">';

                // Render row by row for each percentile — both architectures sorted by TTFT
                ['p90', 'p95', 'p99'].forEach(p => {
                    const pLabel = p.toUpperCase();
                    html += `<div style="display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:12px;">`;

                    // Collect best EPP result per architecture at this percentile
                    const candidates = [];
                    ['aggregated', 'pd'].forEach(arch => {
                        const trials = eppArch[arch] || [];
                        if (!trials.length) return;
                        const best = trials.reduce((a, b) => ((a[`ttft_${p}`] || Infinity) < (b[`ttft_${p}`] || Infinity)) ? a : b);
                        if (best && best[`ttft_${p}`]) {
                            candidates.push({ arch, best });
                        }
                    });
                    // Sort by TTFT — best (lowest) first
                    candidates.sort((a, b) => (a.best[`ttft_${p}`] || Infinity) - (b.best[`ttft_${p}`] || Infinity));

                    candidates.forEach(({arch, best}, ci) => {
                        const w = best.weights || {};
                        const archLabel = arch.toUpperCase();
                        const eppId = `epp-${arch}-${p}`;
                        const recArch = arch === 'pd' ? 'pd' : 'aggregated';
                        window._recConfigs[eppId] = { ...best, architecture: recArch, test_settings: _baseTestSettings };

                        const borderStyle = ci === 0 ? '3px solid #7c3aed; border-left:6px solid #7c3aed' : '2px solid #7c3aed40; border-left:5px solid #7c3aed80';
                        const winnerBadge = ci === 0 ? '<span style="background:#059669; color:white; font-size:0.65em; padding:2px 6px; border-radius:3px; margin-left:6px;">BEST TTFT</span>' : '';

                        let deployLabel;
                        if ((arch === 'pd' || arch === 'ep') && best.prefill_pods) {
                            deployLabel = best.prefill_tp === best.decode_tp
                                ? `${best.prefill_pods}P+${best.decode_pods}D TP=${best.prefill_tp || best.tp || '?'}`
                                : `${best.prefill_pods}P+${best.decode_pods}D PTP=${best.prefill_tp || best.tp || '?'} DTP=${best.decode_tp || '?'}`;
                        } else if (best.replicas) {
                            deployLabel = `${best.replicas} Aggregated pods, TP=${best.tp || '?'}`;
                        } else {
                            deployLabel = best.config_name;
                        }

                        html += `<div style="background:white; border:${borderStyle}; border-radius:10px; padding:16px; position:relative;">`;
                        html += `<div style="position:absolute;top:12px;right:12px;display:flex;gap:4px;">`;
                        html += `<button onclick="applyReportConfig('${eppId}')" title="Use this configuration" style="background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 8px;cursor:pointer;color:#6b7280;font-size:14px;display:flex;align-items:center;gap:4px;transition:all 0.15s;" onmouseover="this.style.borderColor='#2563eb';this.style.color='#2563eb';this.style.background='#eff6ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#128260; Reuse</button>`;
                        html += `<button onclick="showSingleTestModal('${eppId}')" title="Run this exact configuration" style="background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 8px;cursor:pointer;color:#6b7280;font-size:14px;display:flex;align-items:center;gap:4px;transition:all 0.15s;" onmouseover="this.style.borderColor='#8b5cf6';this.style.color='#8b5cf6';this.style.background='#f5f3ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#129514; Test</button>`;
                        html += `</div>`;
                        html += `<div style="font-weight:800; color:#7c3aed; font-size:0.85em; text-transform:uppercase; margin-bottom:8px;">&#9201; TTFT ${pLabel} <span style="background:#7c3aed; color:white; font-size:0.7em; padding:2px 8px; border-radius:4px; margin-left:6px;">EPP TUNED</span> <span style="background:#64748b; color:white; font-size:0.65em; padding:2px 6px; border-radius:3px; margin-left:4px;">${archLabel}</span>${winnerBadge}</div>`;
                        html += `<div style="font-size:1.3em; font-weight:800; color:#1e293b; margin-bottom:4px;">${deployLabel}</div>`;
                        const concStr = best.concurrency ? ` | c=${best.concurrency}` : '';
                        const tputMean = best.throughput_mean || best.throughput_p90;
                        html += `<div style="font-size:0.9em; color:#475569;">TTFT ${pLabel}: <strong>${best[`ttft_${p}`]} ms</strong> | Throughput Mean: <strong>${tputMean} req/s</strong>${concStr}</div>`;
                        html += `<div style="font-size:0.8em; color:#7c3aed; margin-top:4px;">EPP: ${best.name} (${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'})</div>`;
                        if (best.manifest_types && best.manifest_types.length) {
                            html += '<div style="margin-top:8px;">';
                            best.manifest_types.forEach(t => {
                                html += `<a href="/api/run/${runId}/config/${best.test_id}/manifest/${t}" style="color:#7c3aed;text-decoration:none;font-size:11px;padding:2px 6px;background:#f5f3ff;border-radius:4px;border:1px solid #c4b5fd;display:inline-block;">${t}</a> `;
                            });
                            html += '</div>';
                        }
                        html += '</div>';
                    });
                    // Fill empty slot if only one architecture
                    if (candidates.length < 2) html += '<div></div>';

                    html += '</div>'; // Close row grid
                });

                html += '</div></div>';
            }
        }

        html += '</div></div>';

    } // end Deployment Recommendation card

    // Flush recommendation part 1 (goal banner + deployment cards)
    secRec = html; html = '';

    // ============================================================
    // TP CALIBRATION CHARTS — Step 2 (Decode) & Step 3 (Prefill)
    // ============================================================
    html += '<div class="chart-card" style="border-left:6px solid #0d9488;">' +
        '<div class="chart-card-header" style="background:linear-gradient(135deg,#0d9488,#14b8a6); color:white;">TP Calibration</div>' +
        '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
        'Tests different <strong>Tensor Parallelism (TP)</strong> values per role to find the minimum viable TP that fits in GPU memory. ' +
        'Lower TP means fewer GPUs per pod, allowing more replicas and higher throughput. Each TP value is tested with a short benchmark to measure baseline latency and token throughput.' +
        '</div></div>';
    if (rec) {
            const hasDecodeTP = rec.decode_tp_all && rec.decode_tp_all.length;
            const hasPrefillTP = rec.prefill_tp_all && rec.prefill_tp_all.length;
            if (hasDecodeTP || hasPrefillTP) {
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
    html += statCard(summary.successful_tests, 'Successful Tests', `${summary.total_tests} total`);
    if (best.lowest_latency) {
        const ll = best.lowest_latency;
        html += statCard(ll.ttft_p90.toFixed(1) + ' ms', 'Best TTFT P90', ll.name);
        if (ll.ttft_p95) html += statCard(ll.ttft_p95.toFixed(1) + ' ms', 'Best TTFT P95', ll.name);
        if (ll.ttft_p99) html += statCard(ll.ttft_p99.toFixed(1) + ' ms', 'Best TTFT P99', ll.name);
    }
    if (best.highest_throughput) {
        const ht = best.highest_throughput;
        const htVal = ht.throughput_mean || ht.throughput_p90;
        html += statCard(htVal.toFixed(2) + ' req/s', 'Best Throughput Mean', ht.name);
    }
    if (best.most_efficient)
        html += statCard(best.most_efficient.efficiency.toFixed(3) + ' req/s/GPU', 'Best Efficiency', best.most_efficient.name);
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

    // TP Pareto chart → TP Calibration subtab
    secTP += chartCard('TP Calibration: Latency vs GPU Count', chartDesc.pareto, 'chart-pareto');

    // Scatter, efficiency → Configurations subtab
    secCfg += '<div class="chart-card" style="border-left:6px solid #6366f1;">' +
        '<div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#818cf8); color:white;">Configurations</div>' +
        '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
        'All tested configurations across PD, EP, and Aggregated architectures. Charts show TTFT vs Throughput trade-offs at each percentile. ' +
        'The <strong>Pareto Optimal</strong> table highlights configurations that offer the best trade-offs — no other config is better on both latency and throughput simultaneously.' +
        '</div></div>';
    secCfg += chartCard('Throughput vs Latency', chartDesc.scatter, 'chart-scatter');
    secCfg += chartCard('GPU Efficiency (req/s per GPU)', chartDesc.efficiency, 'chart-efficiency');

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
                manifestLinks = mTypes.filter(t => !t.includes('service')).map(t => {
                    return `<a href="/api/run/${runId}/config/${pTestId}/manifest/${t}" title="Download ${t}.yaml" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${t}</a>`;
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

    // --- All results table ---
    if (coreResults.length) {
        var allCfgTableId = 'all-configs-table-' + runId;
        html += '<div class="chart-card"><div class="chart-card-header">All Successful Configurations</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Complete results from every test that ran successfully. <strong>Green highlighted rows</strong> are Pareto optimal (the best trade-offs). Click any column header to sort.</div>';
        html += '<div class="chart-card-body" style="padding:0;">';
        html += '<table class="results-table" id="' + allCfgTableId + '"><tr>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',1,\'str\')">Architecture &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',2,\'num\')">TTFT P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',3,\'num\')">TTFT P95 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',4,\'num\')">TTFT P99 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',5,\'num\')">Tput Mean &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',6,\'num\')">ITL P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',7,\'num\')">GPUs &#x21C5;</th>';
        html += '<th style="cursor:pointer;" title="Throughput Mean ÷ Total GPUs (req/s per GPU)" onclick="sortReportTable(\'' + allCfgTableId + '\',8,\'num\')">Efficiency &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th>';
        html += '<th>Manifests</th>';
        html += '</tr>';
        const paretoNames = new Set(charts.pareto.pareto_table.map(p => p.config_name));
        coreResults.forEach((r, idx) => {
            const cls = paretoNames.has(r.config_name) ? ' class="pareto-row"' : '';
            const rTestId = r.test_id || testIdLookup[r.config_name] || r.config_name;
            let manifestLinks = '-';
            if (r.manifest_types && r.manifest_types.length > 0) {
                manifestLinks = r.manifest_types.filter(t => !t.includes('service')).map(t => {
                    return `<a href="/api/run/${runId}/config/${rTestId}/manifest/${t}" title="Download ${t}.yaml" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${t}</a>`;
                }).join(' ');
            }
            const na = 'N/A';
            const eppBadge = (rTestId && rTestId.startsWith('step11-epp-')) ? ' <span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
            const tputMeanVal = r.throughput_mean ?? r.throughput_p90 ?? na;
            html += `<tr${cls}><td>${r.config_name}${eppBadge}</td><td>${r.architecture}</td><td data-val="${r.ttft_p90}">${r.ttft_p90}</td><td data-val="${r.ttft_p95 ?? ''}">${r.ttft_p95 ?? na}</td><td data-val="${r.ttft_p99 ?? ''}">${r.ttft_p99 ?? na}</td><td data-val="${tputMeanVal}">${tputMeanVal}</td><td data-val="${r.itl_p90 ?? ''}">${r.itl_p90 ?? na}</td><td data-val="${r.gpus}">${r.gpus}</td><td data-val="${r.efficiency}">${r.efficiency}</td><td>${manifestLinks}</td></tr>`;
        });
        html += '</table></div></div>';
    }

    // Flush configurations (PD charts + pareto table + all results)
    secCfg += html; html = '';

    // ============================================================
    // USER DEFINED TEST SETTINGS tab — run-level configuration
    // ============================================================
    if (data.run_config) {
        const rc = data.run_config;
        const na = 'N/A';
        const adv = rc.advanced_vllm || {};
        const advVal = (key, fallback) => { const s = adv[key]; return s && s.mode === 'custom' && s.value != null ? s.value : (fallback != null ? fallback : 'auto'); };
        const advToggle = (key, fallback) => { const s = adv[key]; return s ? (s.mode === 'on' ? 'On' : s.mode === 'off' ? 'Off' : fallback) : fallback; };

        html += '<div class="chart-card" style="border-left:6px solid #64748b;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#64748b,#94a3b8); color:white;">Test Settings</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Complete configuration used for this optimization run — workload parameters, search strategy, infrastructure details, and component versions. ' +
            'These settings apply to every test; only the architecture, TP values, and pod counts vary between configurations.' +
            '</div></div>';
        html += '<div class="chart-card"><div class="chart-card-header">User Defined Test Settings</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.92em;">All settings configured for this optimization run. These apply to every test — only the architecture, TP values, and pod counts vary between tests.</div>';
        html += '<div class="chart-card-body" style="padding:16px 20px;">';
        html += '<div style="display:grid;grid-template-columns:1fr;gap:16px;">';

        function settingsTable(title, color, rows) {
            let t = `<table class="results-table" style="font-size:0.85em;margin-bottom:16px;">`;
            t += `<tr><th colspan="2" style="background:${color};text-align:center;font-size:1.05em;">${title}</th></tr>`;
            rows.forEach(function(r) {
                if (r) t += `<tr><td style="color:#64748b;width:40%;">${r[0]}</td><td><strong>${r[1]}</strong></td></tr>`;
            });
            t += '</table>';
            return t;
        }

        // Settings tables
        html += settingsTable('Workload', '#059669', [
            ['Model', rc.model_name || na],
            ['ISL', rc.isl + (rc.isl_stdev ? ' (&sigma;=' + rc.isl_stdev + ')' : '')],
            ['OSL', rc.osl + (rc.osl_stdev ? ' (&sigma;=' + rc.osl_stdev + ')' : '')],
            ['Concurrent Users', rc.qps != null ? Math.round(rc.qps) : na],
            ['Rate Type', rc.rate_type || 'concurrent'],
            ['Test Duration', (rc.test_duration || 300) + 's'],
            ['Stop Mode', rc.stop_mode || 'duration'],
            rc.max_requests ? ['Max Requests', rc.max_requests] : null,
            rc.turns > 1 ? ['Turns', rc.turns] : null,
            ['Workload Mode', rc.workload_mode || 'synthetic'],
            rc.dataset_source ? ['Dataset', '<span style="word-break:break-all;font-size:0.9em;">' + rc.dataset_source + '</span>'] : null,
            rc.prefix_cache_hit_pct > 0 ? ['Prefix Cache Hit', rc.prefix_cache_hit_pct + '%'] : null,
        ]);
        html += settingsTable('Search Strategy', '#4f46e5', [
            ['Optimization Goal', (rc.objective || 'ttft').toUpperCase()],
            ['Total GPUs', rc.total_gpus || na],
            ['TP Options', (rc.tp_options || []).join(', ') || na],
            ['TP Pair Breadth', 'Top-' + (rc.tp_pair_top_n || 4)],
            ['P/D Ratio Search', rc.pd_search_mode === 'exhaustive' ? 'Exhaustive' : 'Adaptive'],
            ['Auto-Scale Concurrency', rc.use_achievable_qps ? 'Yes' : 'No'],
            ['Headroom', (rc.headroom || 1.3) + 'x'],
            ['Latency SLA', rc.latency_constraint_enabled ? rc.latency_constraint_ms + 'ms @ ' + rc.latency_constraint_percentile : 'Disabled'],
        ]);
        html += settingsTable('Infrastructure', '#d97706', [
            ['Inference Image', '<span style="word-break:break-all;font-size:0.9em;">' + (rc.image || na) + '</span>'],
            ['Scheduler Image', '<span style="word-break:break-all;font-size:0.9em;">' + (rc.scheduler_image || na) + '</span>'],
            ['Namespace', rc.namespace || na],
            ['PVC', rc.pvc_name || na],
            ['Network Type', rc.network_type || na],
            ['NCCL IB HCA', rc.nccl_ib_hca || na],
            rc.rdma_nics_per_node ? ['RDMA NICs/Node', rc.rdma_nics_per_node] : null,
        ]);
        // Infrastructure Versions
        if (data.infra_versions && Object.keys(data.infra_versions).length > 0) {
            var iv = data.infra_versions;
            var versionLabels = {
                openshift: 'OpenShift', k8s: 'Kubernetes',
                gpu_operator: 'GPU Operator', gpu_driver: 'GPU Driver', cuda_runtime: 'CUDA Runtime',
                network_operator: 'Network Operator', mofed: 'MOFED/DOCA',
                istio: 'Istio', service_mesh: 'Service Mesh', epp: 'EPP Scheduler',
                nfd: 'NFD', lws: 'LWS'
            };
            var vRows = [];
            Object.keys(iv).forEach(function(k) { vRows.push([versionLabels[k] || k, iv[k]]); });
            html += settingsTable('Component Versions', '#059669', vRows);
        }
        const vllmCustomEnabled = rc.advanced_vllm_custom_enabled !== false;
        const rv = rc._resolved || {};
        const hasPdTests = (data.all_results || []).some(r => r.architecture === 'PD');
        const hasAggTests = (data.all_results || []).some(r => r.architecture === 'AGGREGATED');
        const hasEpTests = (data.all_results || []).some(r => r.architecture === 'EP');

        // GPU Memory Utilization display
        let gmuDisplay = '-';
        if (hasPdTests || hasEpTests) {
            let gmuParts = [];
            if (rv.prefill_gpu_memory_utilization) gmuParts.push(`P=${rv.prefill_gpu_memory_utilization}`);
            if (rv.decode_gpu_memory_utilization) gmuParts.push(`D=${rv.decode_gpu_memory_utilization}`);
            if (hasAggTests) {
                const aggTc = ((data.all_results || []).find(r => r.architecture === 'AGGREGATED') || {}).test_config || {};
                if (aggTc.gpu_memory_utilization) gmuParts.push(`Agg=${aggTc.gpu_memory_utilization}`);
            }
            gmuDisplay = gmuParts.join(', ') || '-';
        } else {
            gmuDisplay = rv.gpu_memory_utilization || rc.gpu_memory_utilization || '-';
        }

        var vllmRows = [
            !vllmCustomEnabled ? ['Mode', '<span style="color:#059669;">Upstream defaults (no tuning)</span>'] : null,
            ['Max Model Len', rv.max_model_len || rc.max_model_len || '-'],
            ['GPU Memory Utilization', gmuDisplay],
            rv.gpu_vram_gb ? ['GPU VRAM', rv.gpu_vram_gb.toFixed(1) + ' GB'] : null,
            ['Block Size', rv.block_size || '-'],
            ['Max Num Seqs', (rv.max_num_seqs || '256') + (rv.decode_max_num_seqs ? ' (decode: ' + rv.decode_max_num_seqs + ')' : '')],
            ['Max Batched Tokens', rv.max_num_batched_tokens || rv.max_model_len || '-'],
            ['Prefix Caching', rv.enable_prefix_caching === true ? 'Enabled' : (rv.enable_prefix_caching === false ? 'Disabled' : '-')],
            ['Expert Parallel', hasEpTests ? 'Enabled (EP) / Disabled (PD, Agg)' : (rv.enable_expert_parallel === true ? 'Enabled' : 'Disabled')],
            ['Trust Remote Code', rv.trust_remote_code === true ? 'Enabled' : 'Disabled'],
        ];
        if (vllmCustomEnabled) {
            const adv2 = rc.advanced_vllm || {};
            if (adv2.dtype) vllmRows.push(['Dtype', adv2.dtype]);
            if (adv2.kv_cache_dtype) vllmRows.push(['KV Cache Dtype', adv2.kv_cache_dtype]);
            if (adv2.pipeline_parallel_size) vllmRows.push(['Pipeline Parallel', adv2.pipeline_parallel_size]);
        }
        html += settingsTable('Advanced vLLM Settings', '#7c3aed', vllmRows);

        // EPP Configuration
        const eppCustomEnabled = rc.epp_custom_enabled !== false;
        const eppPresetLabels = {balanced:'Balanced', cache_optimized:'Cache Optimized', queue_balanced:'Queue Balanced', latency_aware:'Latency Aware', custom:'Custom'};
        var eppRows = [
            ['Scoring Preset', eppCustomEnabled ? (eppPresetLabels[rc.epp_preset] || rc.epp_preset || 'Balanced') : 'llm-d upstream'],
            ['EPP Tuning (Step 9)', rc.epp_benchmark ? 'Enabled' : 'Disabled'],
        ];
        if (rc.epp_config) {
            const ec = rc.epp_config;
            if (ec.maxPrefixBlocksToMatch) eppRows.push(['Max Prefix Blocks', ec.maxPrefixBlocksToMatch]);
            if (ec.lruCapacityPerServer) eppRows.push(['LRU Capacity/Server', ec.lruCapacityPerServer]);
        }
        html += settingsTable('EPP Configuration', '#6d28d9', eppRows);

        // Per-Architecture Tuning Comparison Table
        const archConfigs = {};
        const archOrder = [];
        for (const r of (data.all_results || [])) {
            const arch = (r.architecture || '').toUpperCase();
            if (archConfigs[arch]) continue;
            let tc = null;
            if (r.test_config) {
                try { tc = typeof r.test_config === 'string' ? JSON.parse(r.test_config) : r.test_config; } catch(e) {}
            }
            if (tc) { archConfigs[arch] = tc; archOrder.push(arch); }
        }

        if (archOrder.length > 0) {
            html += '<div style="margin-top:24px;">';
            html += '<div style="font-weight:700;font-size:1.05em;color:#1e293b;margin-bottom:8px;padding:8px 12px;background:#ecfdf5;border:1px solid #059669;border-radius:4px;text-align:center;">Tuned Settings vs Upstream Defaults</div>';
            html += '<div style="font-size:0.85em;color:#64748b;margin-bottom:12px;text-align:center;">Green = auto-tuned by ServeIt Studio. Gray = upstream default (unchanged).</div>';

            const hasEp = archOrder.includes('EP');
            const na = '<span style="color:#cbd5e1;">N/A</span>';

            const _eppPresets = {
                balanced: {prefix_cache_weight:3, kv_cache_weight:2, queue_weight:2, active_request_weight:2, decode_prefix_cache_weight:1, decode_active_request_weight:3},
                cache_optimized: {prefix_cache_weight:5, kv_cache_weight:1, queue_weight:2, active_request_weight:1, decode_prefix_cache_weight:1, decode_active_request_weight:3},
                queue_balanced: {prefix_cache_weight:1, kv_cache_weight:1, queue_weight:3, active_request_weight:3, decode_prefix_cache_weight:1, decode_active_request_weight:3},
                latency_aware: {prefix_cache_weight:3, kv_cache_weight:2, queue_weight:2, active_request_weight:2, decode_prefix_cache_weight:1, decode_active_request_weight:3},
            };
            function _eppW(tc) {
                if (!tc.epp_config) return null;
                const preset = tc.epp_config.preset || rc.epp_preset || 'balanced';
                return _eppPresets[preset] || _eppPresets.balanced;
            }

            function getVal(tc, key) { return tc[key] != null ? String(tc[key]) : null; }
            function pdVal(tc, pKey, dKey) {
                const p = tc[pKey], d = tc[dKey];
                if (p != null && d != null && p !== d) return `P=${p} D=${d}`;
                return p != null ? String(p) : (d != null ? String(d) : null);
            }
            function boolVal(tc, key) { return tc[key] === true ? 'On' : tc[key] === false ? 'Off' : null; }

            const sections = [
                { title: 'Memory & Batching', params: [
                    { label: 'gpu-memory-utilization', def: '0.90', get: tc => pdVal(tc, 'prefill_gpu_memory_utilization', 'decode_gpu_memory_utilization') || getVal(tc, 'gpu_memory_utilization') },
                    { label: 'max-model-len', def: 'auto', get: tc => getVal(tc, 'max_model_len') },
                    { label: 'max-num-seqs', def: '256', get: tc => pdVal(tc, 'prefill_max_num_seqs', 'decode_max_num_seqs') || getVal(tc, 'max_num_seqs') },
                    { label: 'max-num-batched-tokens', def: 'auto', get: tc => getVal(tc, 'max_num_batched_tokens') },
                    { label: 'block-size', def: '16', get: tc => getVal(tc, 'block_size') },
                    { label: 'kv-cache-memory-bytes', def: 'auto', get: tc => getVal(tc, 'kv_cache_memory_bytes'), pd_only: true },
                ]},
                { title: 'Precision & Compute', params: [
                    { label: 'dtype', def: 'auto', get: tc => getVal(tc, 'dtype') },
                    { label: 'kv-cache-dtype', def: 'auto', get: tc => getVal(tc, 'kv_cache_dtype') },
                    { label: 'pipeline-parallel-size', def: '1', get: tc => getVal(tc, 'pipeline_parallel_size') },
                ]},
                { title: 'Feature Flags', params: [
                    { label: 'enable-prefix-caching', def: 'Off', get: tc => boolVal(tc, 'enable_prefix_caching') },
                    { label: 'enable-expert-parallel', def: 'Off', get: tc => boolVal(tc, 'enable_expert_parallel') },
                    { label: 'enable-dbo', def: 'Off', get: tc => boolVal(tc, 'enable_dbo') },
                    { label: 'enable-eplb', def: 'Off', get: tc => boolVal(tc, 'enable_eplb') },
                    { label: 'trust-remote-code', def: 'Off', get: tc => boolVal(tc, 'trust_remote_code') },
                ]},
                { title: 'MoE / Expert Parallel', params: [
                    { label: 'moe-dp-chunk-size', def: '256', get: tc => getVal(tc, 'moe_dp_chunk_size'), ep_only: true },
                    { label: 'all2all-backend', def: 'auto', get: tc => getVal(tc, 'all2all_backend') || (tc.decode_all2all_backend ? `HT / ${tc.decode_all2all_backend}` : null), ep_only: true },
                    { label: 'moe-backend', def: 'auto', get: tc => getVal(tc, 'moe_backend'), ep_only: true },
                    { label: 'use-deep-gemm', def: 'auto', get: tc => tc.use_deep_gemm === true ? 'On' : tc.use_deep_gemm === false ? 'Off' : null },
                    { label: 'dbo-prefill-threshold', def: '32', get: tc => getVal(tc, 'dbo_prefill_token_threshold') },
                    { label: 'dbo-decode-threshold', def: '32', get: tc => getVal(tc, 'dbo_decode_token_threshold') },
                    { label: 'num-redundant-experts', def: '32', get: tc => getVal(tc, 'num_redundant_experts'), ep_only: true },
                    { label: 'NVSHMEM_SYMMETRIC_SIZE', def: '16G', get: tc => getVal(tc, 'nvshmem_symmetric_size'), ep_only: true },
                ]},
                { title: 'EPP Prefill Routing Weights', params: [
                    { label: 'prefix-cache-weight', def: '3', get: tc => { const w = _eppW(tc); return w ? String(w.prefix_cache_weight) : null; } },
                    { label: 'kv-cache-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.kv_cache_weight) : null; } },
                    { label: 'queue-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.queue_weight) : null; } },
                    { label: 'active-request-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.active_request_weight) : null; } },
                ]},
                { title: 'EPP Decode Routing Weights', params: [
                    { label: 'decode-prefix-cache-weight', def: '3', get: tc => { const w = _eppW(tc); return w ? String(w.decode_prefix_cache_weight) : null; } },
                    { label: 'decode-active-request-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.decode_active_request_weight) : null; } },
                ]},
                { title: 'EPP Auto-Calculated', params: [
                    { label: 'maxPrefixBlocksToMatch', def: 'auto', get: tc => tc.epp_config ? String(tc.epp_config.max_prefix_blocks || tc.epp_config.maxPrefixBlocksToMatch || '-') : null },
                    { label: 'lruCapacityPerServer', def: 'auto', get: tc => tc.epp_config ? String(tc.epp_config.lru_capacity || tc.epp_config.lruCapacityPerServer || '-') : null },
                    { label: 'nonCachedTokens', def: '16', get: tc => tc.epp_config ? String(tc.epp_config.non_cached_tokens || tc.epp_config.nonCachedTokens || '-') : null },
                ]},
            ];

            html += '<div style="overflow-x:auto;"><table class="results-table" style="font-size:0.85em;">';
            html += '<tr><th style="text-align:left;min-width:180px;">Parameter</th><th style="min-width:80px;">Default</th>';
            for (const arch of archOrder) {
                const color = arch === 'AGGREGATED' ? '#6366f1' : arch === 'PD' ? '#0ea5e9' : '#10b981';
                html += `<th style="color:${color};min-width:100px;">${arch}</th>`;
            }
            html += '</tr>';

            for (const section of sections) {
                html += `<tr><td colspan="${2 + archOrder.length}" style="background:#f1f5f9;font-weight:700;color:#475569;padding:6px 10px;font-size:0.95em;">${section.title}</td></tr>`;
                for (const param of section.params) {
                    if (param.ep_only && !hasEp) continue;
                    html += `<tr><td style="color:#334155;padding-left:16px;"><code style="font-size:0.9em;">${param.label}</code></td>`;
                    html += `<td style="color:#94a3b8;text-align:center;">${param.def}</td>`;
                    for (const arch of archOrder) {
                        const tc = archConfigs[arch];
                        if (param.ep_only && arch !== 'EP') { html += `<td style="text-align:center;">${na}</td>`; continue; }
                        if (param.pd_only && arch === 'AGGREGATED') { html += `<td style="text-align:center;">${na}</td>`; continue; }
                        const val = param.get(tc);
                        const display = val || param.def;
                        const changed = val && val !== param.def && val !== 'null';
                        const style = changed ? 'font-weight:700;color:#059669;' : '';
                        html += `<td style="text-align:center;${style}">${display}</td>`;
                    }
                    html += '</tr>';
                }
            }

            html += '</table></div></div>';
        }

        html += '</div></div></div>';
        secTestCfg = html; html = '';
    }

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
            html += `<div id="step9-chart-p90-${ai}${_chartSuffix}" style="height:500px; background:#fff; border-radius:8px; border:1px solid #e2e8f0;"></div>`;
            html += `<div id="step9-chart-p95-${ai}${_chartSuffix}" style="height:500px; background:#fff; border-radius:8px; border:1px solid #e2e8f0; margin-top:16px;"></div>`;
            html += `<div id="step9-chart-p99-${ai}${_chartSuffix}" style="height:500px; background:#fff; border-radius:8px; border:1px solid #e2e8f0; margin-top:16px;"></div>`;
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
        return reps + '×TP' + (match.tp || '?');
    }
    sweepConfigLabels.pd = findSweepConfigLabel('step11-sweep-pd');
    sweepConfigLabels.aggregated = findSweepConfigLabel('step11-sweep-aggregated');
    sweepConfigLabels.ep = findSweepConfigLabel('step11-sweep-ep');
    // Also try cache sweep test_ids for cache-only runs
    if (!sweepConfigLabels.pd) sweepConfigLabels.pd = findSweepConfigLabel('step13-cache-pd');
    if (!sweepConfigLabels.aggregated) sweepConfigLabels.aggregated = findSweepConfigLabel('step13-cache-aggregated');
    if (!sweepConfigLabels.ep) sweepConfigLabels.ep = findSweepConfigLabel('step13-cache-ep');

    if (data.concurrency_sweep) {
        const sweep = data.concurrency_sweep;
        var archColors = { pd: '#10b981', aggregated: '#6366f1', ep: '#f59e0b' };
        var archLabelsCS = { pd: 'PD', aggregated: 'Aggregated', ep: 'EP' };
        var pctColors = { ttft_p90: '#3b82f6', ttft_p95: '#f59e0b', ttft_p99: '#ef4444' };
        var archIdx = 0;

        Object.keys(sweep).forEach(function(arch) {
            var points = sweep[arch];
            if (!points || !points.length) return;
            var label = archLabelsCS[arch] || arch;
            var color = archColors[arch] || '#888';
            archIdx++;

            // --- Architecture header ---
            var configLabel = (points[0] && points[0].config_label) ? points[0].config_label : (sweepConfigLabels[arch] || '');
            var configSuffix = configLabel ? ' — ' + configLabel : '';
            html += '<div class="chart-card" style="margin-top:20px; border:2px solid ' + color + '; border-left:6px solid ' + color + ';">';
            html += '<div class="chart-card-header" style="background:linear-gradient(135deg,' + color + ',' + color + '99); color:white; font-size:1.2em;">' + label + configSuffix + ' — Concurrency Sweep</div>';

            // --- TTFT P90/P95/P99 on same chart ---
            var ttftChartId = 'chart-sweep-ttft-' + arch;
            html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">TTFT percentiles vs concurrent users. Lower is better.</div>';
            html += '<div id="' + ttftChartId + '" style="width:100%; height:400px;"></div>';

            chartQueue.push(function() {
                var traces = [];
                var calShapes = [];
                [{ key: 'ttft_p90', label: 'P90' },
                 { key: 'ttft_p95', label: 'P95' },
                 { key: 'ttft_p99', label: 'P99' }].forEach(function(pct) {
                    if (!points[0][pct.key]) return;
                    traces.push({
                        x: points.map(function(p) { return p.concurrency; }),
                        y: points.map(function(p) { return p[pct.key] || 0; }),
                        text: points.map(function(p) { return Math.round(p[pct.key] || 0).toLocaleString(); }),
                        textposition: 'top center', textfont: { size: 9, color: pctColors[pct.key] },
                        mode: 'lines+markers+text', name: pct.label,
                        line: { color: pctColors[pct.key], width: 3 },
                        marker: { size: 8 },
                        hovertemplate: '<b>%{x} users</b><br>' + pct.label + ': %{y:.0f}ms<extra></extra>'
                    });
                });
                points.forEach(function(p) {
                    if (p.is_calibrated) {
                        calShapes.push({ type: 'line', x0: p.concurrency, x1: p.concurrency, y0: 0, y1: 1, yref: 'paper',
                            line: { color: '#059669', width: 1.5, dash: 'dash' } });
                        traces.push({ x: [p.concurrency, p.concurrency], y: [null, null], mode: 'lines', name: 'Calibrated (' + p.concurrency + ' users)',
                            line: { color: '#059669', width: 1.5, dash: 'dash' }, showlegend: true });
                    }
                });
                Plotly.newPlot(ttftChartId, traces, {
                    xaxis: { title: 'Concurrent Users', gridcolor: '#e2e8f0' },
                    yaxis: { title: 'TTFT (ms)', gridcolor: '#e2e8f0' },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    margin: { t: 20, b: 60, l: 70, r: 20 },
                    legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.9)' },
                    shapes: calShapes, hovermode: 'closest'
                }, { responsive: true });
            });

            // --- Throughput Mean vs Concurrency ---
            var tputChartId = 'chart-sweep-tput-' + arch;
            html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">Token throughput per GPU vs concurrent users. Higher is better.</div>';
            html += '<div id="' + tputChartId + '" style="width:100%; height:400px;"></div>';

            chartQueue.push(function() {
                var calShapes = [];
                var calTraces = [];
                points.forEach(function(p) {
                    if (p.is_calibrated) {
                        calShapes.push({ type: 'line', x0: p.concurrency, x1: p.concurrency, y0: 0, y1: 1, yref: 'paper',
                            line: { color: '#059669', width: 1.5, dash: 'dash' } });
                        calTraces.push({ x: [p.concurrency, p.concurrency], y: [null, null], mode: 'lines', name: 'Calibrated (' + p.concurrency + ' users)',
                            line: { color: '#059669', width: 1.5, dash: 'dash' }, showlegend: true });
                    }
                });
                Plotly.newPlot(tputChartId, [{
                    x: points.map(function(p) { return p.concurrency; }),
                    y: points.map(function(p) { return p.throughput_per_gpu; }),
                    text: points.map(function(p) { return Math.round(p.throughput_per_gpu).toLocaleString(); }),
                    textposition: 'top center', textfont: { size: 9, color: '#334155' },
                    mode: 'lines+markers+text', name: 'Throughput/GPU',
                    line: { color: color, width: 3 },
                    marker: { size: points.map(function(p) { return p.is_calibrated ? 14 : 8; }),
                              color: points.map(function(p) { return p.is_calibrated ? '#fff' : color; }),
                              line: { color: color, width: points.map(function(p) { return p.is_calibrated ? 3 : 0; }) } },
                    hovertemplate: '<b>%{x} users</b><br>%{y:.0f} tok/s/gpu<extra></extra>'
                }].concat(calTraces), {
                    xaxis: { title: 'Concurrent Users', gridcolor: '#e2e8f0' },
                    yaxis: { title: 'Token Throughput per GPU (tok/s/gpu)', gridcolor: '#e2e8f0' },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    margin: { t: 20, b: 60, l: 70, r: 20 },
                    legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.9)' },
                    shapes: calShapes, hovermode: 'closest'
                }, { responsive: true });
            });

            html += '</div>';
        });

        // --- Sweep Results Table ---
        html += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #64748b;">';
        html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#475569,#64748b); color:white; font-size:1.1em;">Concurrency Sweep Data</div>';
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
        Object.keys(sweep).forEach(function(arch) {
            var points = sweep[arch];
            if (!points || !points.length) return;
            var archLabel = arch === 'pd' ? 'PD' : (arch === 'aggregated' ? 'Aggregated' : 'EP');
            points.forEach(function(p, idx) {
                var calMark = p.is_calibrated ? ' style="background:#ecfdf5; font-weight:700;"' : '';
                var calBadge = p.is_calibrated ? ' <span style="background:#059669;color:white;font-size:0.7em;padding:1px 5px;border-radius:3px;">calibrated</span>' : '';
                html += '<tr' + calMark + '>';
                html += '<td>' + (idx === 0 ? '<strong>' + archLabel + '</strong>' : '') + '</td>';
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

    // CACHE HIT SWEEP CHARTS (Step 13)
    // ============================================================
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
            var csLabel = csArchLabels[arch] || arch;
            var csColor = csArchColors[arch] || '#888';

            var csConfigLbl = (pts[0] && pts[0].config_label) ? pts[0].config_label : (sweepConfigLabels[arch.replace('_calibrated','')] || '');
            var csConfigSuffix = csConfigLbl ? ' — ' + csConfigLbl : '';
            html += '<div class="chart-card" style="margin-top:20px; border:2px solid ' + csColor + '; border-left:6px solid ' + csColor + ';">';
            html += '<div class="chart-card-header" style="background:linear-gradient(135deg,' + csColor + ',' + csColor + '99); color:white; font-size:1.2em;">' + csLabel + csConfigSuffix + ' — Cache Hit Sweep</div>';

            // --- TTFT P90/P95/P99 on same chart ---
            var csTtftId = 'cache-sweep-ttft-' + arch;
            html += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">TTFT percentiles &amp; throughput vs cache hit ratio. Lower TTFT is better, higher throughput is better. Subplot shows actual vs configured cache hit rate from vLLM.</div>';
            html += '<div id="' + csTtftId + '" style="width:100%;height:550px;"></div>';

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
                        textposition: 'top center', textfont: { size: 9, color: csPctColors[pct.key] },
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
                    textposition: 'top center', textfont: { size: 9, color: '#d97706' },
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
                        textposition: 'top center', textfont: { size: 9, color: '#059669' },
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
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',1,\'num\')">Actual Hit % &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',2,\'num\')">Concurrency &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',3,\'num\')">TTFT P50 &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',4,\'num\')">TTFT P90 &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',5,\'num\')">TTFT P95 &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',6,\'num\')">TTFT P99 &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',7,\'num\')">Throughput &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',8,\'num\')">Output tok/s &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',9,\'num\')">ITL P90 &#x21C5;</th>';
            html += '</tr>';
            pts.forEach(function(p) {
                html += '<tr>';
                html += '<td>' + p.hit_pct + '%</td>';
                html += '<td>' + (p.actual_hit_rate != null ? p.actual_hit_rate.toFixed(1) + '%' : '-') + '</td>';
                html += '<td>' + (p.concurrency || '-') + '</td>';
                html += '<td>' + (p.ttft_p50 ? p.ttft_p50.toFixed(0) : '-') + '</td>';
                html += '<td>' + (p.ttft_p90 ? p.ttft_p90.toFixed(0) : '-') + '</td>';
                html += '<td>' + (p.ttft_p95 ? p.ttft_p95.toFixed(0) : '-') + '</td>';
                html += '<td>' + (p.ttft_p99 ? p.ttft_p99.toFixed(0) : '-') + '</td>';
                html += '<td>' + (p.throughput_mean ? p.throughput_mean.toFixed(2) : '-') + '</td>';
                html += '<td>' + (p.output_tps_mean ? p.output_tps_mean.toFixed(1) : '-') + '</td>';
                html += '<td>' + (p.itl_p90 ? p.itl_p90.toFixed(1) : '-') + '</td>';
                html += '</tr>';
            });
            html += '</table></div></div>';
            html += '</div>';
        });
    }

    // Flush cache sweep
    secCacheSweep = html; html = '';

    // ============================================================
    // DEPLOYMENT TIMING (model load times per config)
    // ============================================================
    if (data.all_results && data.all_results.length > 0) {
        html += '<div class="chart-card" style="border-left:6px solid #475569;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#475569,#64748b); color:white;">Deploy Timing</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Pod startup and <strong>model loading times</strong> for each tested configuration. ' +
            'Larger TP values typically load faster (fewer replicas), while smaller TP with more replicas takes longer as each pod loads the model independently.' +
            '</div></div>';
        var timingData = [];
        data.all_results.forEach(function(r) {
            var mj = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
            var loadTime = mj.model_load_time_s;
            if (loadTime != null && loadTime > 0) {
                timingData.push({ name: r.config_name, load_s: loadTime, arch: r.architecture, pods: (r.prefill_pods || 0) + (r.decode_pods || 0) || r.replicas || 0 });
            }
        });

        if (timingData.length > 0) {
            html += '<div class="chart-card" style="margin-top:24px; border-left:6px solid #d97706;">';
            html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#d97706,#b45309); color:white; font-size:1.2em;">Deployment Timing</div>';
            html += '<div style="padding:8px 20px; color:#1e293b; font-size:0.95em;">Time from pod deployment to model fully loaded and serving. Includes scheduling, image pull, model weight loading, and CUDA graph capture.</div>';

            // Bar chart
            html += '<div id="chart-deploy-timing" style="width:100%;height:400px;"></div>';
            var archColorsDT = { AGGREGATED: '#6366f1', PD: '#10b981', EP: '#f59e0b' };
            chartQueue.push(function() {
                var sorted = timingData.sort(function(a, b) { return b.load_s - a.load_s; });
                Plotly.newPlot('chart-deploy-timing', [{
                    x: sorted.map(function(d) { return d.name; }),
                    y: sorted.map(function(d) { return d.load_s; }),
                    type: 'bar',
                    marker: { color: sorted.map(function(d) { return archColorsDT[d.arch] || '#94a3b8'; }) },
                    text: sorted.map(function(d) { return d.load_s + 's'; }),
                    textposition: 'outside',
                    textfont: { size: 10, color: '#334155' },
                    cliponaxis: false,
                    constraintext: 'none',
                    hovertemplate: '<b>%{x}</b><br>Load time: %{y}s<extra></extra>'
                }], {
                    xaxis: { tickangle: -35 },
                    yaxis: { title: 'Model Load Time (seconds)' },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    margin: { t: 20, b: 120, l: 70, r: 20 }
                }, { responsive: true });
            });

            // Data table
            var dtTblId = 'deploy-timing-tbl-' + runId;
            html += '<div style="padding:12px 20px;"><table class="results-table" id="' + dtTblId + '" style="font-size:0.85em;">';
            html += '<tr>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + dtTblId + '\',0,\'str\')">Configuration &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + dtTblId + '\',1,\'str\')">Architecture &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + dtTblId + '\',2,\'num\')">Pods &#x21C5;</th>';
            html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + dtTblId + '\',3,\'num\')">Model Load Time &#x21C5;</th>';
            html += '</tr>';
            timingData.sort(function(a, b) { return a.load_s - b.load_s; }).forEach(function(d) {
                var mins = Math.floor(d.load_s / 60);
                var secs = d.load_s % 60;
                var timeStr = mins > 0 ? mins + 'm ' + secs + 's' : secs + 's';
                html += '<tr><td>' + d.name + '</td><td>' + d.arch + '</td><td>' + d.pods + '</td><td>' + timeStr + '</td></tr>';
            });
            html += '</table></div>';
            html += '</div>';
        }
    }
    secDeployTiming = html; html = '';

    // ============================================================
    // vLLM ENGINE METRICS CHARTS
    // ============================================================
    if (charts.vllm && charts.vllm.configs.length) {
        html += '<div class="chart-card" style="margin-top:24px; border-left:6px solid #8b5cf6;">';
        html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#6366f1); color:white; font-size:1.2em;">vLLM Engine Metrics</div>';
        html += '<div style="padding:8px 20px; color:#1e293b; font-size:0.95em;">Prometheus metrics collected from the vLLM engine during each test. These metrics show engine-level behavior across all configurations — latency distributions, token throughput, request queuing, and processing time breakdown.</div>';
        html += '</div>';

        html += chartCard(
            'vLLM TTFT Percentiles',
            'Time-to-First-Token as reported by the vLLM engine histogram (averaged over the test window). Lower bars mean faster first-token delivery. Compare P50 (typical) vs P99 (worst-case) across configurations.',
            'chart-vllm-ttft'
        );
        html += chartCard(
            'vLLM ITL Percentiles',
            'Inter-Token Latency from vLLM engine histograms. This is the delay between consecutive generated tokens — it determines how "smooth" streaming feels to the user. Lower is better.',
            'chart-vllm-itl'
        );
        html += chartCard(
            'vLLM E2E Request Latency',
            'End-to-end request latency from vLLM (includes TTFT + all token generation). Shows the full time a request spends in the engine. Compare tail latency (P99) across configurations to spot saturation.',
            'chart-vllm-e2e'
        );
        html += chartCard(
            'Token Throughput',
            'Average prompt (input) and generation (output) token processing rates across all pods. Higher bars = more tokens processed per second. Generation rate directly impacts how many users can be served concurrently.',
            'chart-vllm-tokens'
        );
        html += chartCard(
            'Request Queue & KV Cache',
            'Average concurrent requests running and waiting in queue, plus KV cache utilization (%). High waiting counts indicate the engine is saturated. High KV cache usage means the model is near memory capacity.',
            'chart-vllm-queue'
        );
        html += chartCard(
            'Processing Time & Preemptions',
            'How engine time is split between prefill (prompt processing), decode (token generation), and queuing. Preemptions show how often the engine evicts running requests to make room — high preemptions indicate memory pressure.',
            'chart-vllm-time'
        );

        // Network throughput row
        if (charts.vllm.network && charts.vllm.network.pod_tx.some(v => v > 0)) {
            const hasIB = charts.vllm.network.ib_rx.some(v => v > 0);
            html += chartCard(
                'Pod Network Throughput',
                'Average network transmit (TX) and receive (RX) rates aggregated across all pods in each configuration. Higher TX indicates more data being sent to clients (generated tokens). Higher RX reflects incoming requests and model weight loading.',
                'chart-net-pod'
            );
            if (hasIB) {
                html += chartCard(
                    'InfiniBand RDMA Throughput',
                    'InfiniBand receive throughput across pods. In PD configurations this captures KV cache transfer from prefill to decode pods over RDMA. Higher values indicate more data flowing through the high-speed interconnect.',
                    'chart-net-ib'
                );
            }
        }
    }

    // Flush vLLM metrics
    secVLLM = html; html = '';

    // === Build Estimator section ===
    const wl = data.recommendation ? data.recommendation.workload : {};
    const estSuffix = _chartSuffix;
    let secEst = '<div class="chart-card" style="margin-top:16px; border:2px solid #d97706; border-left:6px solid #d97706;">' +
        '<div class="chart-card-header" style="background:linear-gradient(135deg,#d97706,#b45309);">GPU Estimator</div>' +
        '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">' +
            'Estimate how many GPUs each tested configuration would need for a different workload. ' +
            'Tested with <strong>' + (wl.users || '?') + ' users</strong>, ISL <strong>' + (wl.isl || '?') + '</strong>, OSL <strong>' + (wl.osl || '?') + '</strong>.' +
        '</div>' +
        '<div style="padding:12px 20px 0;">' +
            '<div class="estimator-form">' +
                '<div class="estimator-row">' +
                    '<div class="estimator-group">' +
                        '<div class="estimator-group-label">Workload</div>' +
                        '<label class="estimator-field">' +
                            '<span>Concurrency</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#128101;</span><input type="number" id="est-concurrency' + estSuffix + '" value="' + (wl.users || 100) + '" min="1"><span class="estimator-unit">users</span></div>' +
                        '</label>' +
                        '<label class="estimator-field">' +
                            '<span>ISL</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#128229;</span><input type="number" id="est-isl' + estSuffix + '" value="' + (wl.isl || 1024) + '" min="1"><span class="estimator-unit">tokens</span></div>' +
                        '</label>' +
                        '<label class="estimator-field">' +
                            '<span>OSL</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#128228;</span><input type="number" id="est-osl' + estSuffix + '" value="' + (wl.osl || 256) + '" min="1"><span class="estimator-unit">tokens</span></div>' +
                        '</label>' +
                    '</div>' +
                    '<div class="estimator-group">' +
                        '<div class="estimator-group-label">Variance</div>' +
                        '<label class="estimator-field">' +
                            '<span>ISL StdDev</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#177;</span><input type="number" id="est-isl-stdev' + estSuffix + '" value="' + (wl.isl_stdev || 0) + '" min="0"><span class="estimator-unit">tokens</span></div>' +
                        '</label>' +
                        '<label class="estimator-field">' +
                            '<span>OSL StdDev</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#177;</span><input type="number" id="est-osl-stdev' + estSuffix + '" value="' + (wl.osl_stdev || 0) + '" min="0"><span class="estimator-unit">tokens</span></div>' +
                        '</label>' +
                    '</div>' +
                    '<div class="estimator-group">' +
                        '<div class="estimator-group-label">' +
                            '<label style="cursor:pointer;display:flex;align-items:center;gap:6px;">' +
                                '<input type="checkbox" id="est-turns-toggle' + estSuffix + '"' + ((wl.turns || 1) > 1 ? ' checked' : '') + ' onchange="document.getElementById(\'est-turns' + estSuffix + '\').disabled=!this.checked;"> Multi-turn' +
                            '</label>' +
                        '</div>' +
                        '<label class="estimator-field">' +
                            '<span>Turns</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#128260;</span><input type="number" id="est-turns' + estSuffix + '" value="' + (wl.turns || 1) + '" min="1"' + ((wl.turns || 1) <= 1 ? ' disabled' : '') + '><span class="estimator-unit">per user</span></div>' +
                        '</label>' +
                    '</div>' +
                    '<div class="estimator-group">' +
                        '<div class="estimator-group-label">' +
                            '<label style="cursor:pointer;display:flex;align-items:center;gap:6px;">' +
                                '<input type="checkbox" id="est-sla-toggle' + estSuffix + '" onchange="document.getElementById(\'est-sla-ms' + estSuffix + '\').disabled=!this.checked;document.getElementById(\'est-sla-pctl' + estSuffix + '\').disabled=!this.checked;"> Latency SLA' +
                            '</label>' +
                        '</div>' +
                        '<label class="estimator-field">' +
                            '<span>Target TTFT</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#9201;</span><input type="number" id="est-sla-ms' + estSuffix + '" value="500" min="1" disabled><span class="estimator-unit">ms</span></div>' +
                        '</label>' +
                        '<label class="estimator-field">' +
                            '<span>Percentile</span>' +
                            '<select id="est-sla-pctl' + estSuffix + '" disabled style="padding:10px 12px;border:2px solid #e2e8f0;border-radius:10px;font-size:0.95em;background:#f8fafc;">' +
                                '<option value="p90">P90</option><option value="p95">P95</option><option value="p99" selected>P99</option>' +
                            '</select>' +
                        '</label>' +
                    '</div>' +
                '</div>' +
            '</div>' +
            '<div id="est-scaling-info' + estSuffix + '" style="background:#fffbeb;border-top:1px solid #fde68a;border-bottom:1px solid #fde68a;padding:14px 20px;margin:0 -20px;font-size:0.9em;color:#92400e;width:calc(100% + 40px);"></div>' +
            '<div style="padding:0 20px;"><button class="action-button" onclick="runEstimator(\'' + estSuffix + '\')" style="margin-top:16px;padding:10px 28px;font-size:0.95em;border-radius:8px;background:linear-gradient(135deg,#d97706,#b45309);border:none;color:white;cursor:pointer;font-weight:600;box-shadow:0 2px 8px rgba(217,119,6,0.3);">Estimate</button></div>' +
            '<div id="est-results' + estSuffix + '"></div>' +
            '<div id="est-chart' + estSuffix + '" style="width:100%;height:400px;margin-top:16px;"></div>' +
        '</div></div>';

    // === Build subtab structure ===
    const subtabDefs = [];
    // Build EPP Tuning section — per-architecture charts + tables
    if (data.epp_tuning && data.epp_tuning.by_architecture) {
        const eppData = data.epp_tuning;
        const eppTargetMs = eppData.target_ms;
        const eppTargetPct = eppData.target_percentile || 'p99';
        let eppHtml = '';

        Object.keys(eppData.by_architecture).forEach((arch, archIdx) => {
            const trials = eppData.by_architecture[arch];
            const archLabel = arch.toUpperCase();
            if (!trials || !trials.length) return;
            const eppCardId = `epp-${arch}-${runId}`;

            eppHtml += `<div class="chart-card" style="margin-top:${archIdx > 0 ? '16' : '0'}px; border:2px solid #7c3aed; border-left:6px solid #7c3aed;">`;
            eppHtml += `<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">Step 9: EPP Tuning — ${archLabel}</div>`;
            eppHtml += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Same deployment, different EPP scoring weights. Each test swapped only the gateway configmap (~10s) to isolate the impact of request routing.</div>';
            eppHtml += '<div style="padding:8px 20px 12px; font-size:0.82em; color:#64748b;">' +
                '<table style="width:100%;border-collapse:collapse;font-size:1em;">' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">baseline</td><td style="padding:4px 0;color:#64748b;">Step 7 result with upstream llm-d default weights. No EPP change — used as the reference point.</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">smart-derived</td><td style="padding:4px 0;color:#64748b;">Weights computed mathematically from measured Prometheus metrics (cache hit rate, KV pressure, queue depth, active requests).</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">smart-refined</td><td style="padding:4px 0;color:#64748b;">Refined from smart-derived using metrics collected during the first EPP test — a second iteration of the formula.</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">balanced-fallback</td><td style="padding:4px 0;color:#64748b;">Equal weights (2:2:2:2), tested as safety net when smart weights degrade performance vs baseline.</td></tr>' +
                '</table></div>';

            // Summary cards
            const bestTrial = trials.reduce((a, b) => (a.ttft_p90 || Infinity) < (b.ttft_p90 || Infinity) ? a : b);
            eppHtml += '<div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; padding:12px 20px;">';
            eppHtml += `<div style="background:#f0fdf4; border-radius:10px; padding:16px; text-align:center; border:1px solid #bbf7d0;"><div style="font-size:1.5em; font-weight:800; color:#059669;">${bestTrial.name}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Best Strategy</div></div>`;
            eppHtml += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:1.5em; font-weight:800; color:#1e293b;">${bestTrial.ttft_p90 || 'N/A'} <span style="font-size:0.5em; color:#64748b;">ms</span></div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">TTFT P90</div></div>`;
            eppHtml += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:1.5em; font-weight:800; color:#1e293b;">${bestTrial.throughput_p90 || 'N/A'}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Throughput P90 (req/s)</div></div>`;
            const bw = bestTrial.weights || {};
            const weightsStr = `${bw.prefix_cache || 0}:${bw.kv_cache || 0}:${bw.queue || 0}:${bw.active_request || 0}`;
            eppHtml += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:1.2em; font-weight:800; color:#1e293b;">${weightsStr}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">EPP Weights (Cache:KV:Queue:Active)</div></div>`;
            eppHtml += '</div>';

            // Charts P90, P95, P99 with per-percentile summary
            const baselineTrial = trials.find(t => t.is_baseline);
            const pctls = [{key:'p90',label:'P90',color:'#3b82f6'},{key:'p95',label:'P95',color:'#dc2626'},{key:'p99',label:'P99',color:'#7c3aed'}];
            pctls.forEach(pctl => {
                eppHtml += `<div id="${eppCardId}-${pctl.key}${_chartSuffix}" style="height:400px; margin:8px 20px; background:#fff; border-radius:8px; border:1px solid #e2e8f0;"></div>`;
                eppHtml += `<div style="padding:4px 20px 12px; font-size:0.85em; line-height:1.7; color:#1e293b;">`;
                trials.forEach(t => {
                    const ttftKey = `ttft_${pctl.key}`;
                    const ttft = t[ttftKey] != null ? t[ttftKey].toFixed(0) + 'ms' : 'N/A';
                    const tput = t.throughput_mean != null ? t.throughput_mean.toFixed(1) : (t.throughput_p90 != null ? t.throughput_p90.toFixed(1) : 'N/A');
                    let ttftDelta = '', tputDelta = '';
                    if (baselineTrial && !t.is_baseline && baselineTrial[ttftKey] && t[ttftKey]) {
                        const ttftPct = ((t[ttftKey] - baselineTrial[ttftKey]) / baselineTrial[ttftKey] * 100);
                        if (ttftPct < -0.5) ttftDelta = ` <span style="color:#059669;">(${Math.abs(ttftPct).toFixed(1)}% faster)</span>`;
                        else if (ttftPct > 0.5) ttftDelta = ` <span style="color:#dc2626;">(+${ttftPct.toFixed(1)}% slower)</span>`;
                        else ttftDelta = ` <span style="color:#6b7280;">(0%)</span>`;
                    }
                    if (baselineTrial && !t.is_baseline && baselineTrial.throughput_mean && t.throughput_mean) {
                        const tputPct = ((t.throughput_mean - baselineTrial.throughput_mean) / baselineTrial.throughput_mean * 100);
                        if (tputPct > 0.5) tputDelta = ` <span style="color:#059669;">(+${tputPct.toFixed(1)}%)</span>`;
                        else if (tputPct < -0.5) tputDelta = ` <span style="color:#dc2626;">(${tputPct.toFixed(1)}%)</span>`;
                        else tputDelta = ` <span style="color:#6b7280;">(0%)</span>`;
                    }
                    const icon = t === bestTrial && !t.is_baseline ? '⭐' : (t.is_baseline ? '📊' : '🔧');
                    const w = t.weights || {};
                    const wStr = `${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'}:${w.active_request || 0}`;
                    eppHtml += `<div>${icon} <strong>${t.name}</strong> (${wStr}): TTFT ${pctl.label}=${ttft}${ttftDelta}, Throughput=${tput} req/s${tputDelta}</div>`;
                });
                eppHtml += '</div>';
            });

            // Results table
            eppHtml += '<div style="padding:0 20px 16px;"><table class="results-table">';
            eppHtml += '<tr><th>Strategy</th><th>Weights (C:K:Q:A)</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Tput P90</th><th>ITL P90</th><th>EPP Config</th></tr>';
            trials.forEach(e => {
                const isBest = e === bestTrial && !e.is_baseline;
                const isBase = e.is_baseline;
                const cls = isBest ? ' class="pareto-row"' : (isBase ? ' style="background:#f8fafc;color:#64748b;font-style:italic;"' : '');
                const w = e.weights || {};
                const wStr = `${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'}:${w.active_request || 0}`;
                const na = 'N/A';
                let ml = '-';
                if (e.manifest_types && e.manifest_types.length > 0) {
                    ml = e.manifest_types.map(t => `<a href="/api/run/${runId}/config/${e.test_id}/manifest/${t}" style="color:#7c3aed;text-decoration:none;font-size:11px;padding:2px 6px;background:#f5f3ff;border-radius:4px;border:1px solid #c4b5fd;display:inline-block;">${t}</a>`).join(' ');
                }
                const label = isBase ? `<span style="color:#94a3b8;">${e.name}</span>` : `<strong>${e.name}</strong>`;
                eppHtml += `<tr${cls}><td>${label}${isBest ? ' ⭐' : ''}</td><td>${wStr}</td>`;
                eppHtml += `<td>${e.ttft_p50 ?? na}</td><td>${e.ttft_p90 ?? na}</td><td>${e.ttft_p95 ?? na}</td><td>${e.ttft_p99 ?? na}</td>`;
                eppHtml += `<td>${e.throughput_p90 ?? na}</td><td>${e.itl_p90 ?? na}</td><td>${ml}</td></tr>`;
            });
            eppHtml += '</table></div></div>';

            // Collect chart render functions — will execute after DOM update
            if (!window._eppChartRenders) window._eppChartRenders = [];
            window._eppChartRenders.push(() => {
                pctls.forEach(pctl => {
                    const el = document.getElementById(`${eppCardId}-${pctl.key}${_chartSuffix}`);
                    if (!el) return;
                    const xLabels = trials.map(t => t.name);
                    const latencies = trials.map(t => t[`ttft_${pctl.key}`]);
                    const throughputs = trials.map(t => t.throughput_mean || t.throughput_p90);
                    const bestIdx = latencies.indexOf(Math.min(...latencies.filter(v => v != null)));
                    const markerColors = latencies.map((v, i) => {
                        if (eppTargetMs && v != null) return v <= eppTargetMs ? '#10b981' : '#ef4444';
                        return i === bestIdx ? '#10b981' : pctl.color;
                    });
                    const latText = latencies.map(v => v != null ? v.toFixed(0) + 'ms' : '');
                    const tputText = throughputs.map(v => v != null ? v.toFixed(1) : '');
                    const traces = [
                        {x: xLabels, y: latencies, name: `TTFT ${pctl.label}`, type: 'scatter', mode: 'lines+markers+text',
                         line: {color: pctl.color, width: 3, shape: 'spline'},
                         marker: {color: markerColors, size: 12, symbol: 'circle', line: {width: 2, color: 'white'}},
                         text: latText, textposition: 'top center', textfont: {size: 11, color: pctl.color},
                         fill: 'tozeroy', fillcolor: pctl.color + '14'},
                        {x: xLabels, y: throughputs, name: `Throughput Mean`, type: 'scatter', mode: 'lines+markers+text', yaxis: 'y2',
                         line: {color: '#f59e0b', width: 3, shape: 'spline'},
                         marker: {color: '#f59e0b', size: 10, symbol: 'diamond', line: {width: 2, color: 'white'}},
                         text: tputText, textposition: 'bottom center', textfont: {size: 10, color: '#f59e0b'}},
                    ];
                    if (bestIdx >= 0) {
                        traces.push({x: [xLabels[bestIdx]], y: [latencies[bestIdx]], name: 'Best EPP', type: 'scatter', mode: 'markers',
                            marker: {color: '#10b981', size: 22, symbol: 'circle', line: {width: 3, color: 'white'}}, showlegend: true});
                    }
                    // Baseline from Step 6/7 (before EPP tuning) — horizontal reference lines
                    const baseline = (eppData.baselines || {})[arch];
                    if (baseline) {
                        const blTtft = baseline[`ttft_${pctl.key}`];
                        const blTput = baseline.throughput_mean || baseline.throughput_p90;
                        if (blTtft != null) {
                            traces.push({x: xLabels, y: xLabels.map(() => blTtft), name: `Baseline TTFT (${baseline.config_name})`, type: 'scatter', mode: 'lines',
                                line: {color: '#1e40af', width: 2, dash: 'dot'},
                                showlegend: true, hovertemplate: `Baseline: ${blTtft.toFixed(0)}ms<extra></extra>`});
                        }
                        if (blTput != null) {
                            traces.push({x: xLabels, y: xLabels.map(() => blTput), name: `Baseline Throughput`, type: 'scatter', mode: 'lines', yaxis: 'y2',
                                line: {color: '#92400e', width: 2, dash: 'dot'},
                                showlegend: true, hovertemplate: `Baseline: ${blTput.toFixed(1)} req/s<extra></extra>`});
                        }
                    }
                    const shapes = [];
                    const annotations = [];
                    if (eppTargetMs) {
                        shapes.push({type: 'line', x0: -0.5, x1: xLabels.length - 0.5, y0: eppTargetMs, y1: eppTargetMs, yref: 'y',
                            line: {color: '#ef4444', width: pctl.key === eppTargetPct ? 2 : 1.5, dash: 'dash'}});
                        annotations.push({x: xLabels.length - 1, y: eppTargetMs, yref: 'y',
                            text: `SLA (${eppTargetPct.toUpperCase()}): ${eppTargetMs} ms`, showarrow: false,
                            font: {color: '#ef4444', size: 11}, xanchor: 'right', yanchor: 'bottom', yshift: 5, bgcolor: 'rgba(255,255,255,0.85)'});
                    }
                    Plotly.newPlot(el, traces, {
                        ...plotlyLayout, height: 400, margin: {t: 30, b: 80, l: 60, r: 60},
                        xaxis: {title: 'EPP Strategy'},
                        yaxis: {title: `TTFT ${pctl.label} (ms)`, side: 'left', titlefont: {color: pctl.color}, tickfont: {color: pctl.color}},
                        yaxis2: {title: `Throughput Mean (req/s)`, side: 'right', overlaying: 'y', titlefont: {color: '#f59e0b'}, tickfont: {color: '#f59e0b'}},
                        showlegend: true, legend: {x: 0, y: 1.18, orientation: 'h'}, shapes, annotations,
                    }, plotlyConfig);
                });
            });
        });

        // Show skipped architectures
        if (eppData.skipped_architectures && eppData.skipped_architectures.length) {
            eppData.skipped_architectures.forEach(arch => {
                const archLabel = arch.toUpperCase();
                eppHtml += `<div class="chart-card" style="margin-top:16px; border:2px solid #7c3aed40; border-left:6px solid #7c3aed80;">`;
                eppHtml += `<div class="chart-card-header" style="background:linear-gradient(135deg,#94a3b8,#64748b);">Step 9: EPP Tuning — ${archLabel}</div>`;
                eppHtml += '<div style="padding:24px; text-align:center; color:#64748b;">';
                eppHtml += '<div style="font-size:1.5em; margin-bottom:8px;">&#9989;</div>';
                eppHtml += '<div style="font-weight:700; font-size:1em; color:#1e293b; margin-bottom:6px;">EPP tuning skipped — user preset is already optimal</div>';
                eppHtml += '<div style="font-size:0.88em; max-width:500px; margin:0 auto;">Measured Prometheus metrics (cache hit rate, KV pressure, queue depth, active requests) confirm the selected EPP weights are well-matched for this architecture. No adjustment would improve routing.</div>';
                eppHtml += '</div></div>';
            });
        }

        secEppTuning = eppHtml;
    }

    if (secRec) subtabDefs.push({ id: 'recommendation', label: 'Recommendation', icon: '&#9733;' });
    if (secTP) subtabDefs.push({ id: 'tp-calibration', label: 'TP Calibration', icon: '&#9881;' });
    if (secCfg) subtabDefs.push({ id: 'configurations', label: 'Configurations', icon: '&#9776;' });
    if (secCmp) subtabDefs.push({ id: 'comparison', label: 'Comparison', icon: '&#8596;' });
    if (secStep9) subtabDefs.push({ id: 'latency-search', label: 'Latency Search', icon: '&#128269;' });
    if (secCal) subtabDefs.push({ id: 'calibrated-load', label: 'Concurrency Sweep', icon: '&#9878;' });
    if (secCacheSweep) subtabDefs.push({ id: 'cache-sweep', label: 'Cache Sweep', icon: '&#128451;' });
    if (secDeployTiming) subtabDefs.push({ id: 'deploy-timing', label: 'Deploy Timing', icon: '&#9202;' });
    if (secVLLM) subtabDefs.push({ id: 'vllm-metrics', label: 'vLLM Metrics', icon: '&#9889;' });
    if (secEppTuning) subtabDefs.push({ id: 'epp-tuning', label: 'EPP Tuning', icon: '&#9881;' });
    if (secTestCfg) subtabDefs.push({ id: 'test-settings', label: 'Test Settings', icon: '&#9881;' });
    subtabDefs.push({ id: 'estimator', label: 'Estimator', icon: '&#128200;' });

    const sectionMap = {
        'recommendation': secRec, 'tp-calibration': secTP, 'configurations': secCfg,
        'test-settings': secTestCfg, 'comparison': secCmp, 'latency-search': secStep9,
        'calibrated-load': secCal, 'cache-sweep': secCacheSweep, 'deploy-timing': secDeployTiming, 'vllm-metrics': secVLLM, 'epp-tuning': secEppTuning,
        'estimator': secEst
    };

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

    content.innerHTML = html;

    // --- Plotly chart config (must be before EPP chart rendering) ---
    const plotlyLayout = { margin: { t: 10, b: 40, l: 50, r: 20 }, height: 430, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent', font: { family: 'Inter, sans-serif' } };
    const plotlyConfig = { responsive: true, displayModeBar: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'], toImageButtonOptions: { format: 'png', height: 600, width: 1200, scale: 2 } };

    // Render EPP tuning charts now (before subtabs-ready hides non-active panes)
    if (window._eppChartRenders && window._eppChartRenders.length) {
        window._eppChartRenders.forEach(fn => fn());
        window._eppChartRenders = [];
    }

    // Render estimator methodology explanation
    updateEstimatorScaling(_chartSuffix);

    // Pareto frontier
    if (charts.pareto.traces.length) {
        const traces = charts.pareto.traces.map(t => ({
            x: t.x, y: t.y, text: t.text, name: t.name,
            yaxis: t.yaxis || 'y',
            mode: 'markers+lines',
            marker: { size: t.yaxis === 'y2' ? 10 : 14, color: t.color, symbol: t.yaxis === 'y2' ? 'circle' : 'diamond', line: { width: 2, color: 'white' } },
            line: { width: t.yaxis === 'y2' ? 2 : 2, dash: t.yaxis === 'y2' ? 'dash' : 'dot' },
            hovertemplate: '<b>%{text}</b><extra></extra>'
        }));
        const paretoXvals = [...new Set(traces.flatMap(t => t.x))].sort((a, b) => a - b);
        Plotly.newPlot(cid('chart-pareto'), traces, {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            xaxis: { title: 'Total GPUs', tickvals: paretoXvals },
            yaxis: { title: 'TTFT P90 (ms) — lower is better', side: 'left' },
            yaxis2: { title: 'ITL P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
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
        Plotly.newPlot(cid('chart-scatter'), traces, { ...plotlyLayout, xaxis: { title: 'TTFT P90 (ms) - lower is better' }, yaxis: { title: 'Throughput Mean (req/s) - higher is better' }, showlegend: true }, plotlyConfig);
    }

    // Efficiency bar (filter out sweep tests)
    if (charts.efficiency.configs.length) {
        var effTids = charts.efficiency.test_ids || [];
        var effIdx = [];
        charts.efficiency.configs.forEach(function(c, i) {
            var tid = effTids[i] || '';
            if (tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0) effIdx.push(i);
        });
        var effConfigs = effIdx.map(i => charts.efficiency.configs[i]);
        var effValues = effIdx.map(i => charts.efficiency.values[i]);
        var effColors = effIdx.map(i => charts.efficiency.colors[i]);
        if (effConfigs.length) {
            Plotly.newPlot(cid('chart-efficiency'), [{
                x: effConfigs, y: effValues,
                type: 'bar', marker: { color: effColors },
                text: effValues.map(v => v != null ? v.toFixed(3) : ''),
                textposition: 'outside', textfont: { size: 11, color: '#333' },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>%{x}</b><br>%{y:.3f} req/s/GPU<extra></extra>'
            }], { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 120 }, xaxis: { tickangle: -45 }, yaxis: { title: 'Mean req/s per GPU - higher is better' } }, plotlyConfig);
        }
    }

    // Architecture comparison — use subplots side by side instead of overlaying
    if (charts.architecture.architectures.length) {
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
        Plotly.newPlot(cid('chart-arch'), [ttftTrace, bestTtftTrace, tputTrace], {
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
            Plotly.newPlot(cid('chart-percentile-bars'), [...ttftTraces, ...tputTraces], {
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
        Plotly.newPlot(cid('chart-tp-decode'), [], {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            xaxis: { title: 'TP Value' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left' },
            yaxis2: { title: 'ITL P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }
    if (rec && document.getElementById(cid('chart-tp-prefill')) && !(rec.prefill_tp_all && rec.prefill_tp_all.length)) {
        Plotly.newPlot(cid('chart-tp-prefill'), [], {
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
              textfont: { size: 11, color: '#1e293b' }, cliponaxis: false, constraintext: 'none',
              hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>' },
        ];
        if (itlVals.some(v => v > 0)) {
            traces.push({
                x: tpLabels, y: itlVals, name: 'ITL P90 (ms)', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'circle', color: '#ef4444', line: { width: 2, color: 'white' } },
                hovertemplate: '<b>%{x}</b><br>ITL P90: %{y:.2f} ms<extra></extra>',
            });
        }
        Plotly.newPlot(cid('chart-tp-decode'), traces, {
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
              textfont: { size: 11, color: '#1e293b' }, cliponaxis: false, constraintext: 'none',
              hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>' },
        ];
        if (ttftVals.some(v => v > 0)) {
            traces.push({
                x: tpLabels, y: ttftVals, name: 'TTFT P90 (ms)', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'circle', color: '#ef4444', line: { width: 2, color: 'white' } },
                hovertemplate: '<b>%{x}</b><br>TTFT P90: %{y:.1f} ms<extra></extra>',
            });
        }
        Plotly.newPlot(cid('chart-tp-prefill'), traces, {
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
        var labels = sorted.map(r => r.prefill_pods + 'P:' + r.decode_pods + 'D');
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
            height: hasItl ? 620 : 500,
            margin: { t: 30, b: 60, l: 60, r: 60 },
            xaxis: { title: 'Prefill : Decode Pod Ratio', anchor: hasItl ? 'y3' : 'y' },
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

        Plotly.newPlot(el, traces, layout, plotlyConfig);
    });

    // ============================================================
    // Aggregated configurations chart (all percentiles in one grouped bar chart)
    // ============================================================
    const aggResults = coreResults.filter(r => r.architecture === 'AGGREGATED' && r.ttft_p90);
    if (aggResults.length > 1 && document.getElementById(cid('chart-agg-ttft-all'))) {
        const aggSorted = [...aggResults].sort((a, b) => (a.tp || 1) - (b.tp || 1));
        const aggLabels = aggSorted.map(r => `${r.replicas || Math.floor(r.gpus / (r.tp || 1))}×TP${r.tp || '?'}`);
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
        Plotly.newPlot(cid('chart-agg-ttft-all'), traces, {
            ...plotlyLayout,
            height: hasAggItl ? 520 : 450,
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
                    ...plotlyLayout, height: 500,
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
        const vllmLayout = { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 100 }, barmode: 'group', showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' } };
        const pColors = { p50: '#60a5fa', p90: '#3b82f6', p95: '#f59e0b', p99: '#ef4444' };

        const barText = (vals, fmt) => vals.map(v => v != null ? (fmt === 'int' ? Math.round(v).toLocaleString() : v.toFixed(1)) : '');
        const barTextCfg = { textposition: 'outside', textfont: { size: 10, color: '#334155' }, cliponaxis: false, constraintext: 'none' };

        // Chart 1: TTFT Percentiles (grouped bar)
        Plotly.newPlot(cid('chart-vllm-ttft'), [
            { x: vllm.configs, y: vllm.ttft.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 }, text: barText(vllm.ttft.p50, 'int'), ...barTextCfg },
            { x: vllm.configs, y: vllm.ttft.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 }, text: barText(vllm.ttft.p90, 'int'), ...barTextCfg },
            { x: vllm.configs, y: vllm.ttft.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 }, text: barText(vllm.ttft.p95, 'int'), ...barTextCfg },
            { x: vllm.configs, y: vllm.ttft.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 }, text: barText(vllm.ttft.p99, 'int'), ...barTextCfg },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'TTFT (ms) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 2: ITL Percentiles (grouped bar)
        Plotly.newPlot(cid('chart-vllm-itl'), [
            { x: vllm.configs, y: vllm.itl.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 }, text: barText(vllm.itl.p50), ...barTextCfg },
            { x: vllm.configs, y: vllm.itl.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 }, text: barText(vllm.itl.p90), ...barTextCfg },
            { x: vllm.configs, y: vllm.itl.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 }, text: barText(vllm.itl.p95), ...barTextCfg },
            { x: vllm.configs, y: vllm.itl.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 }, text: barText(vllm.itl.p99), ...barTextCfg },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'ITL (ms) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 3: E2E Latency (grouped bar)
        Plotly.newPlot(cid('chart-vllm-e2e'), [
            { x: vllm.configs, y: vllm.e2e.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 }, text: barText(vllm.e2e.p50), ...barTextCfg },
            { x: vllm.configs, y: vllm.e2e.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 }, text: barText(vllm.e2e.p90), ...barTextCfg },
            { x: vllm.configs, y: vllm.e2e.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 }, text: barText(vllm.e2e.p95), ...barTextCfg },
            { x: vllm.configs, y: vllm.e2e.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 }, text: barText(vllm.e2e.p99), ...barTextCfg },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'E2E Latency (seconds) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 4: Token Throughput (grouped bar)
        Plotly.newPlot(cid('chart-vllm-tokens'), [
            { x: vllm.configs, y: vllm.token_rates.prompt, name: 'Prompt Tokens/s', type: 'bar', marker: { color: '#6366f1' },
              text: barText(vllm.token_rates.prompt, 'int'), ...barTextCfg,
              hovertemplate: '<b>%{x}</b><br>Prompt: %{y:.0f} tokens/s<extra></extra>' },
            { x: vllm.configs, y: vllm.token_rates.generation, name: 'Generation Tokens/s', type: 'bar', marker: { color: '#10b981' },
              text: barText(vllm.token_rates.generation, 'int'), ...barTextCfg,
              hovertemplate: '<b>%{x}</b><br>Generation: %{y:.0f} tokens/s<extra></extra>' },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Tokens/second — higher is better' } }, plotlyConfig);

        // Chart 5: Request Queue & KV Cache (dual axis)
        Plotly.newPlot(cid('chart-vllm-queue'), [
            { x: vllm.configs, y: vllm.request_state.running, name: 'Avg Running', type: 'bar', marker: { color: '#3b82f6' },
              text: barText(vllm.request_state.running), ...barTextCfg,
              hovertemplate: '<b>%{x}</b><br>Running: %{y:.1f}<extra></extra>' },
            { x: vllm.configs, y: vllm.request_state.waiting, name: 'Avg Waiting', type: 'bar', marker: { color: '#ef4444' },
              text: barText(vllm.request_state.waiting), ...barTextCfg,
              hovertemplate: '<b>%{x}</b><br>Waiting: %{y:.1f}<extra></extra>' },
            { x: vllm.configs, y: vllm.request_state.kv_cache, name: 'KV Cache %', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
              line: { color: '#f59e0b', width: 3 }, marker: { size: 10, symbol: 'diamond', color: '#f59e0b', line: { width: 2, color: 'white' } },
              hovertemplate: '<b>%{x}</b><br>KV Cache: %{y:.1f}%<extra></extra>' },
        ], {
            ...vllmLayout,
            margin: { ...vllmLayout.margin, r: 60 },
            xaxis: { tickangle: -35 },
            yaxis: { title: 'Request Count (avg)', side: 'left' },
            yaxis2: { title: 'KV Cache Usage (%)', side: 'right', overlaying: 'y', range: [0, 105], titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
        }, plotlyConfig);

        // Chart 6: Processing Time Breakdown (stacked bar) + Preemptions line
        Plotly.newPlot(cid('chart-vllm-time'), [
            { x: vllm.configs, y: vllm.time_breakdown.prefill, name: 'Prefill Time', type: 'bar', marker: { color: '#6366f1' },
              hovertemplate: '<b>%{x}</b><br>Prefill: %{y:.2f}<extra></extra>' },
            { x: vllm.configs, y: vllm.time_breakdown.decode, name: 'Decode Time', type: 'bar', marker: { color: '#3b82f6' },
              hovertemplate: '<b>%{x}</b><br>Decode: %{y:.2f}<extra></extra>' },
            { x: vllm.configs, y: vllm.time_breakdown.queue, name: 'Queue Time', type: 'bar', marker: { color: '#94a3b8' },
              hovertemplate: '<b>%{x}</b><br>Queue: %{y:.2f}<extra></extra>' },
            { x: vllm.configs, y: vllm.time_breakdown.preemptions, name: 'Preemptions/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
              line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'triangle-up', color: '#ef4444', line: { width: 2, color: 'white' } },
              hovertemplate: '<b>%{x}</b><br>Preemptions: %{y:.1f}/s<extra></extra>' },
            { x: vllm.configs, y: vllm.time_breakdown.waiting || [], name: 'Requests Waiting', type: 'scatter', mode: 'lines+markers', yaxis: 'y3',
              line: { color: '#f59e0b', width: 3, dash: 'dash' }, marker: { size: 8, symbol: 'circle', color: '#f59e0b', line: { width: 2, color: 'white' } },
              hovertemplate: '<b>%{x}</b><br>Waiting: %{y:.1f} avg<extra></extra>' },
        ], {
            ...vllmLayout,
            barmode: 'stack',
            margin: { ...vllmLayout.margin, r: 120 },
            xaxis: { tickangle: -35 },
            yaxis: { title: 'Time Rate (s/s)', side: 'left' },
            yaxis2: { title: 'Preemptions/s', side: 'right', overlaying: 'y', position: 0.95, titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
            yaxis3: { title: 'Requests Waiting', side: 'right', overlaying: 'y', position: 1.0, titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
        }, plotlyConfig);

        // Chart 7: Pod Network Throughput
        if (vllm.network && vllm.network.pod_tx.some(v => v > 0)) {
            Plotly.newPlot(cid('chart-net-pod'), [
                { x: vllm.configs, y: vllm.network.pod_tx, name: 'TX (MB/s)', type: 'bar', marker: { color: '#3b82f6' },
                  hovertemplate: '<b>%{x}</b><br>TX: %{y:.2f} MB/s<extra></extra>' },
                { x: vllm.configs, y: vllm.network.pod_rx, name: 'RX (MB/s)', type: 'bar', marker: { color: '#10b981' },
                  hovertemplate: '<b>%{x}</b><br>RX: %{y:.2f} MB/s<extra></extra>' },
            ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Throughput (MB/s)' } }, plotlyConfig);
        }

        // Chart 8: InfiniBand RDMA Throughput
        if (vllm.network && vllm.network.ib_rx.some(v => v > 0)) {
            Plotly.newPlot(cid('chart-net-ib'), [
                { x: vllm.configs, y: vllm.network.ib_rx, name: 'IB RX (GB/s)', type: 'bar',
                  marker: { color: vllm.network.ib_rx.map(v => v > 0 ? '#8b5cf6' : '#cbd5e1') },
                  text: vllm.network.ib_rx.map(v => v > 0 ? v.toFixed(2) : ''),
                  textposition: 'outside', textfont: { size: 11, color: '#1e293b' },
                  cliponaxis: false, constraintext: 'none',
                  hovertemplate: '<b>%{x}</b><br>IB RX: %{y:.2f} GB/s<extra></extra>' },
            ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'RDMA Throughput (GB/s)' } }, plotlyConfig);
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

