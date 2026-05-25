// charts.js — Plotly chart rendering for all report visualizations

function renderCharts(data, runId) {
    const content = document.getElementById('charts-content');
    const summary = data.summary;
    const charts = data.charts;
    const rec = data.recommendation;

    // Download link handled by tab management
    const dlLink = document.getElementById('chart-download-link');
    dlLink.style.display = 'inline';
    dlLink.href = '#';
    dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(runId, data); };

    let html = '';
    let secRec = '', secTP = '', secCfg = '', secCmp = '', secStep9 = '', secCal = '', secVLLM = '', secTestCfg = '', secEppTuning = '';

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
        html += '<div class="chart-card-header" style="background: linear-gradient(135deg, #ecfdf5, #d1fae5); font-size: 1.2em;">';
        html += 'Deployment Recommendation</div>';
        html += '<div class="chart-card-body" style="padding: 24px;">';

        // Recommendation cards — 2 columns (Response Time left, Throughput right), P90/P95/P99 stacked
        const goalIcons = { response_time: '&#9201;', throughput: '&#9889;' };
        const goalColors = { response_time: '#3b82f6', throughput: '#f59e0b' };
        const goalExplain = {
            response_time: 'Best for chatbots, real-time assistants, and interactive applications where users are waiting for a reply. This configuration minimizes the delay before the model starts generating its response.',
            throughput: 'Best for batch processing, API services, and high-volume workloads where you need to handle the most requests per second. Users may wait slightly longer per request, but the system serves more users overall.',
        };
        const bp = rec.best_by_percentile || {};
        const pctls = ['p90', 'p95', 'p99'];
        const goalOrder = ['response_time', 'throughput'];

        // Render row by row: each row has 2 cards (Response Time + Throughput) at the same percentile
        pctls.forEach((p, pi) => {
            html += '<div style="display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:12px;">';

            for (const key of goalOrder) {
                const r = rec.recommendations[key];
                if (!r) { html += '<div></div>'; continue; }
                const c = r.config;
                const isPrimary = (rec.goal === 'ttft' && key === 'response_time') || (rec.goal === 'throughput' && key === 'throughput');
                const archKey = (r.architecture || '').toLowerCase() === 'pd' ? 'pd' : 'aggregated';

                let cardConfig, cardDeploy, cardArch;
                if (pi === 0) {
                    cardConfig = c;
                    cardDeploy = r.deploy;
                    cardArch = r.architecture;
                } else {
                    const bpData = (bp[p] || {})[archKey];
                    if (!bpData) { html += '<div></div>'; continue; }
                    cardConfig = bpData;
                    cardDeploy = bpData.config_name;
                    cardArch = archKey.toUpperCase();
                }

                const border = (pi === 0 && isPrimary) ? `3px solid ${goalColors[key]}` : `2px solid ${goalColors[key]}40`;
                const badge = (pi === 0 && isPrimary) ? `<span style="background:${goalColors[key]}; color:white; font-size:0.7em; padding:2px 8px; border-radius:4px; margin-left:8px;">PRIMARY</span>` : '';
                const archBadge = cardArch ? `<span style="background:#64748b; color:white; font-size:0.65em; padding:2px 6px; border-radius:3px; margin-left:6px;">${cardArch}</span>` : '';
                const pLabel = p.toUpperCase();

                const recId = 'rec-' + key + '-' + p;
                const recArch = (cardArch || '').toLowerCase() === 'pd' ? 'pd' : ((cardArch || '').toLowerCase() === 'ep' ? 'ep' : 'aggregated');
                window._recConfigs[recId] = { ...cardConfig, architecture: recArch, test_settings: c.test_settings, epp_config: cardConfig.epp_config || c.epp_config };

                html += `<div style="background:white; border:${border}; border-radius:10px; padding:16px; position:relative;">`;
                html += `<div style="font-weight:800; color:${goalColors[key]}; font-size:0.85em; text-transform:uppercase; margin-bottom:8px;">${goalIcons[key] || ''} ${r.goal} — ${pLabel}${badge}${archBadge}</div>`;
                html += `<div style="position:absolute;top:12px;right:12px;display:flex;gap:4px;">`;
                html += `<button onclick="applyReportConfig('${recId}')" title="Use this configuration as starting point" style="background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 8px;cursor:pointer;color:#6b7280;font-size:14px;display:flex;align-items:center;gap:4px;transition:all 0.15s;" onmouseover="this.style.borderColor='#2563eb';this.style.color='#2563eb';this.style.background='#eff6ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#128260; Reuse</button>`;
                html += `<button onclick="showSingleTestModal('${recId}')" title="Run this exact configuration" style="background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 8px;cursor:pointer;color:#6b7280;font-size:14px;display:flex;align-items:center;gap:4px;transition:all 0.15s;" onmouseover="this.style.borderColor='#8b5cf6';this.style.color='#8b5cf6';this.style.background='#f5f3ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#129514; Test</button>`;
                html += `</div>`;
                html += `<div style="font-size:1.4em; font-weight:800; color:#1e293b; margin-bottom:4px;">${cardDeploy}</div>`;

                const ttftVal = pi === 0 ? c.ttft_p90 : cardConfig.ttft;
                const tputVal = pi === 0 ? c.throughput_p90 : cardConfig.throughput;
                const tputMean = pi === 0 ? c.throughput_mean : cardConfig.throughput_mean;
                const gpus = pi === 0 ? c.gpus : cardConfig.gpus;
                const conc = pi === 0 ? c.concurrency : cardConfig.concurrency;
                const ratio = pi === 0 && c.ratio && c.decode_pods > 0 ? `P:D ratio ${c.ratio} | ` : '';
                const userConc = rec.workload ? rec.workload.users : null;
                const concStr = conc ? ` | c=${conc}${userConc && userConc !== conc ? ' (from ' + userConc + ')' : ''}` : '';
                const meanStr = tputMean ? ` | Throughput Mean: <strong>${tputMean} req/s</strong>` : '';

                html += `<div style="font-size:0.9em; color:#475569;">${ratio}TTFT ${pLabel}: <strong>${ttftVal} ms</strong> | Throughput ${pLabel}: <strong>${tputVal} req/s</strong>${meanStr} | ${gpus} GPUs${concStr}</div>`;

                if (pi === 0) {
                    html += `<div style="font-size:0.82em; color:#64748b; margin-top:8px; line-height:1.5;">${goalExplain[key] || ''}</div>`;
                }
                const recTestId = pi === 0 ? (c.test_id || testIdLookup[c.config_name] || c.config_name) : (cardConfig.test_id || testIdLookup[cardConfig.config_name] || cardConfig.config_name);
                const recManifests = pi === 0 ? manifestLookup[recTestId] : (cardConfig.manifest_types || []);
                if (recManifests && recManifests.length) {
                    html += '<div style="margin-top:10px; padding-top:8px; border-top:1px solid #e2e8f0;">';
                    html += '<span style="font-size:0.78em; color:#64748b; margin-right:6px;">Download YAML:</span>';
                    recManifests.filter(t => !t.includes('service')).forEach(t => {
                        html += `<a href="/api/run/${runId}/config/${recTestId}/manifest/${t}" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:2px; display:inline-block;">${t}</a>`;
                    });
                    html += '</div>';
                }
                html += '</div>';
            }
            html += '</div>'; // Close row grid
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

                // Render row by row for each percentile
                ['p90', 'p95', 'p99'].forEach(p => {
                    const pLabel = p.toUpperCase();
                    html += `<div style="display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:12px;">`;

                    // Best aggregated EPP result at this percentile
                    const aggTrials = eppArch['aggregated'] || [];
                    const aggBest = aggTrials.length ? aggTrials.reduce((a, b) => ((a[`ttft_${p}`] || Infinity) < (b[`ttft_${p}`] || Infinity)) ? a : b) : null;
                    if (aggBest && aggBest[`ttft_${p}`]) {
                        const w = aggBest.weights || {};
                        const eppAggId = 'epp-agg-' + p;
                        window._recConfigs[eppAggId] = { ...aggBest, architecture: 'aggregated', test_settings: _baseTestSettings };
                        html += `<div style="background:white; border:2px solid #7c3aed40; border-radius:10px; padding:16px; position:relative;">`;
                        html += `<div style="position:absolute;top:12px;right:12px;display:flex;gap:4px;">`;
                        html += `<button onclick="applyReportConfig('${eppAggId}')" title="Use this configuration as starting point" style="background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 8px;cursor:pointer;color:#6b7280;font-size:14px;display:flex;align-items:center;gap:4px;transition:all 0.15s;" onmouseover="this.style.borderColor='#2563eb';this.style.color='#2563eb';this.style.background='#eff6ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#128260; Reuse</button>`;
                        html += `<button onclick="showSingleTestModal('${eppAggId}')" title="Run this exact configuration" style="background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 8px;cursor:pointer;color:#6b7280;font-size:14px;display:flex;align-items:center;gap:4px;transition:all 0.15s;" onmouseover="this.style.borderColor='#8b5cf6';this.style.color='#8b5cf6';this.style.background='#f5f3ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#129514; Test</button>`;
                        html += `</div>`;
                        html += `<div style="font-weight:800; color:#7c3aed; font-size:0.85em; text-transform:uppercase; margin-bottom:8px;">&#9201; TTFT ${pLabel} <span style="background:#7c3aed; color:white; font-size:0.7em; padding:2px 8px; border-radius:4px; margin-left:6px;">EPP TUNED</span> <span style="background:#64748b; color:white; font-size:0.65em; padding:2px 6px; border-radius:3px; margin-left:4px;">AGGREGATED</span></div>`;
                        html += `<div style="font-size:1.3em; font-weight:800; color:#1e293b; margin-bottom:4px;">${aggBest.config_name}</div>`;
                        const aggConcStr = aggBest.concurrency ? ` | c=${aggBest.concurrency}` : '';
                        const aggMeanStr = aggBest.throughput_mean ? ` | Mean: <strong>${aggBest.throughput_mean} req/s</strong>` : '';
                        html += `<div style="font-size:0.9em; color:#475569;">TTFT ${pLabel}: <strong>${aggBest[`ttft_${p}`]} ms</strong> | Throughput: <strong>${aggBest[`throughput_${p}`] || aggBest.throughput_p90} req/s</strong>${aggMeanStr}${aggConcStr}</div>`;
                        html += `<div style="font-size:0.8em; color:#7c3aed; margin-top:4px;">EPP: ${aggBest.name} (${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'})</div>`;
                        if (aggBest.manifest_types && aggBest.manifest_types.length) {
                            html += '<div style="margin-top:8px;">';
                            aggBest.manifest_types.forEach(t => {
                                html += `<a href="/api/run/${runId}/config/${aggBest.test_id}/manifest/${t}" style="color:#7c3aed;text-decoration:none;font-size:11px;padding:2px 6px;background:#f5f3ff;border-radius:4px;border:1px solid #c4b5fd;display:inline-block;">${t}</a> `;
                            });
                            html += '</div>';
                        }
                        html += '</div>';
                    } else {
                        html += '<div></div>';
                    }

                    // Best PD EPP result at this percentile
                    const pdTrials = eppArch['pd'] || [];
                    const pdBest = pdTrials.length ? pdTrials.reduce((a, b) => ((a[`ttft_${p}`] || Infinity) < (b[`ttft_${p}`] || Infinity)) ? a : b) : null;
                    if (pdBest && pdBest[`ttft_${p}`]) {
                        const w = pdBest.weights || {};
                        const eppPdId = 'epp-pd-' + p;
                        window._recConfigs[eppPdId] = { ...pdBest, architecture: 'pd', test_settings: _baseTestSettings };
                        html += `<div style="background:white; border:2px solid #7c3aed40; border-radius:10px; padding:16px; position:relative;">`;
                        html += `<div style="position:absolute;top:12px;right:12px;display:flex;gap:4px;">`;
                        html += `<button onclick="applyReportConfig('${eppPdId}')" title="Use this configuration as starting point" style="background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 8px;cursor:pointer;color:#6b7280;font-size:14px;display:flex;align-items:center;gap:4px;transition:all 0.15s;" onmouseover="this.style.borderColor='#2563eb';this.style.color='#2563eb';this.style.background='#eff6ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#128260; Reuse</button>`;
                        html += `<button onclick="showSingleTestModal('${eppPdId}')" title="Run this exact configuration" style="background:none;border:1.5px solid #d1d5db;border-radius:6px;padding:4px 8px;cursor:pointer;color:#6b7280;font-size:14px;display:flex;align-items:center;gap:4px;transition:all 0.15s;" onmouseover="this.style.borderColor='#8b5cf6';this.style.color='#8b5cf6';this.style.background='#f5f3ff'" onmouseout="this.style.borderColor='#d1d5db';this.style.color='#6b7280';this.style.background='none'">&#129514; Test</button>`;
                        html += `</div>`;
                        html += `<div style="font-weight:800; color:#7c3aed; font-size:0.85em; text-transform:uppercase; margin-bottom:8px;">&#9889; THROUGHPUT ${pLabel} <span style="background:#7c3aed; color:white; font-size:0.7em; padding:2px 8px; border-radius:4px; margin-left:6px;">EPP TUNED</span> <span style="background:#64748b; color:white; font-size:0.65em; padding:2px 6px; border-radius:3px; margin-left:4px;">PD</span></div>`;
                        html += `<div style="font-size:1.3em; font-weight:800; color:#1e293b; margin-bottom:4px;">${pdBest.config_name}</div>`;
                        const pdConcStr = pdBest.concurrency ? ` | c=${pdBest.concurrency}` : '';
                        const pdMeanStr = pdBest.throughput_mean ? ` | Mean: <strong>${pdBest.throughput_mean} req/s</strong>` : '';
                        html += `<div style="font-size:0.9em; color:#475569;">TTFT ${pLabel}: <strong>${pdBest[`ttft_${p}`]} ms</strong> | Throughput: <strong>${pdBest[`throughput_${p}`] || pdBest.throughput_p90} req/s</strong>${pdMeanStr}${pdConcStr}</div>`;
                        html += `<div style="font-size:0.8em; color:#7c3aed; margin-top:4px;">EPP: ${pdBest.name} (${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'})</div>`;
                        if (pdBest.manifest_types && pdBest.manifest_types.length) {
                            html += '<div style="margin-top:8px;">';
                            pdBest.manifest_types.forEach(t => {
                                html += `<a href="/api/run/${runId}/config/${pdBest.test_id}/manifest/${t}" style="color:#7c3aed;text-decoration:none;font-size:11px;padding:2px 6px;background:#f5f3ff;border-radius:4px;border:1px solid #c4b5fd;display:inline-block;">${t}</a> `;
                            });
                            html += '</div>';
                        }
                        html += '</div>';
                    } else {
                        html += '<div></div>';
                    }

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
        html += statCard(ht.throughput_p90.toFixed(2) + ' req/s', 'Best Throughput P90', ht.name);
        if (ht.throughput_p95) html += statCard(ht.throughput_p95.toFixed(2) + ' req/s', 'Best Throughput P95', ht.name);
        if (ht.throughput_p99) html += statCard(ht.throughput_p99.toFixed(2) + ' req/s', 'Best Throughput P99', ht.name);
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
    secCfg += chartCard('Throughput vs Latency', chartDesc.scatter, 'chart-scatter');
    secCfg += chartCard('GPU Efficiency (req/s per GPU)', chartDesc.efficiency, 'chart-efficiency');

    // --- PD configurations TTFT + Throughput charts (one per percentile) ---
    if (data.all_results.filter(r => r.architecture === 'PD').length) {
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P90)',
            '<strong style="color:#3b82f6">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P90. The <strong style="color:#10b981">green point</strong> marks the best TTFT, the <strong style="color:#e11d48">pink point</strong> marks the best throughput.',
            'chart-pd-ttft-p90'
        );
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P95)',
            '<strong style="color:#3b82f6">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P95. Captures tail latency beyond P90.',
            'chart-pd-ttft-p95'
        );
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P99)',
            '<strong style="color:#3b82f6">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P99. Shows worst-case tail latency.',
            'chart-pd-ttft-p99'
        );
    }

    // --- Pareto table ---
    if (charts.pareto.pareto_table.length) {
        html += '<div class="chart-card"><div class="chart-card-header">Pareto Optimal Configurations</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">These configurations represent the <strong>best possible trade-offs</strong>. Each one is optimal for a different balance of speed, throughput, and GPU cost. No other tested configuration beats any of these on all metrics at once.</div>';
        html += '<div class="chart-card-body" style="padding:0;">';
        html += '<table class="results-table"><tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>GPUs</th><th title="Throughput P90 ÷ Total GPUs (req/s per GPU). Higher = better cost-efficiency.">Efficiency<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th><th>Manifests</th></tr>';
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
            const metrics = [
                {name: 'TTFT (ms)', p50: p.ttft_p50, p90: p.ttft_p90, p95: p.ttft_p95, p99: p.ttft_p99},
                {name: 'ITL (ms)', p50: p.itl_p50, p90: p.itl_p90, p95: p.itl_p95, p99: p.itl_p99},
                {name: 'Throughput (req/s)', p50: p.throughput_p50, p90: p.throughput_p90, p95: p.throughput_p95, p99: p.throughput_p99},
            ];
            metrics.forEach((m, mi) => {
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
        });
        html += '</table></div></div>';
    }

    // --- All results table ---
    if (data.all_results.length) {
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
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',5,\'num\')">Tput P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',6,\'num\')">Tput P95 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',7,\'num\')">Tput P99 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',8,\'num\')">ITL P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',9,\'num\')">GPUs &#x21C5;</th>';
        html += '<th style="cursor:pointer;" title="Throughput P90 ÷ Total GPUs (req/s per GPU)" onclick="sortReportTable(\'' + allCfgTableId + '\',10,\'num\')">Efficiency &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th>';
        html += '<th>Manifests</th>';
        html += '</tr>';
        const paretoNames = new Set(charts.pareto.pareto_table.map(p => p.config_name));
        data.all_results.forEach((r, idx) => {
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
            html += `<tr${cls}><td>${r.config_name}${eppBadge}</td><td>${r.architecture}</td><td data-val="${r.ttft_p90}">${r.ttft_p90}</td><td data-val="${r.ttft_p95 ?? ''}">${r.ttft_p95 ?? na}</td><td data-val="${r.ttft_p99 ?? ''}">${r.ttft_p99 ?? na}</td><td data-val="${r.throughput_p90}">${r.throughput_p90}</td><td data-val="${r.throughput_p95 ?? ''}">${r.throughput_p95 ?? na}</td><td data-val="${r.throughput_p99 ?? ''}">${r.throughput_p99 ?? na}</td><td data-val="${r.itl_p90 ?? ''}">${r.itl_p90 ?? na}</td><td data-val="${r.gpus}">${r.gpus}</td><td data-val="${r.efficiency}">${r.efficiency}</td><td>${manifestLinks}</td></tr>`;
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

        html += '<div class="chart-card"><div class="chart-card-header">User Defined Test Settings</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">All settings configured for this optimization run. These apply to every test — only the architecture, TP values, and pod counts vary between tests.</div>';
        html += '<div class="chart-card-body" style="padding:16px 20px;">';
        html += '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:24px;font-size:0.9em;">';

        // Left column: Workload + Search Strategy
        html += '<div>';
        // Workload
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #10b981;padding-bottom:4px;">Workload</div><div style="line-height:2.2;margin-bottom:20px;">';
        html += `<div><span style="color:#64748b;">Model:</span> <strong>${rc.model_name || na}</strong></div>`;
        html += `<div><span style="color:#64748b;">ISL:</span> ${rc.isl}${rc.isl_stdev ? ' (&sigma;=' + rc.isl_stdev + ')' : ''}</div>`;
        html += `<div><span style="color:#64748b;">OSL:</span> ${rc.osl}${rc.osl_stdev ? ' (&sigma;=' + rc.osl_stdev + ')' : ''}</div>`;
        html += `<div><span style="color:#64748b;">Concurrent Users:</span> ${rc.qps != null ? Math.round(rc.qps) : na}</div>`;
        html += `<div><span style="color:#64748b;">Rate Type:</span> ${rc.rate_type || 'concurrent'}</div>`;
        html += `<div><span style="color:#64748b;">Test Duration:</span> ${rc.test_duration || 300}s</div>`;
        html += `<div><span style="color:#64748b;">Stop Mode:</span> ${rc.stop_mode || 'duration'}</div>`;
        if (rc.max_requests) html += `<div><span style="color:#64748b;">Max Requests:</span> ${rc.max_requests}</div>`;
        if (rc.turns > 1) html += `<div><span style="color:#64748b;">Turns:</span> ${rc.turns}</div>`;
        html += `<div><span style="color:#64748b;">Workload Mode:</span> ${rc.workload_mode || 'synthetic'}</div>`;
        if (rc.dataset_source) html += `<div><span style="color:#64748b;">Dataset:</span> <span style="word-break:break-all;">${rc.dataset_source}</span></div>`;
        if (rc.dataset_column) html += `<div><span style="color:#64748b;">Dataset Column:</span> ${rc.dataset_column}</div>`;
        if (rc.prefix_cache_hit_pct > 0) html += `<div><span style="color:#64748b;">Prefix Cache Hit:</span> ${rc.prefix_cache_hit_pct}%</div>`;
        html += '</div>';
        // Search Strategy
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #6366f1;padding-bottom:4px;">Search Strategy</div><div style="line-height:2.2;">';
        html += `<div><span style="color:#64748b;">Optimization Goal:</span> <strong>${(rc.objective || 'ttft').toUpperCase()}</strong></div>`;
        html += `<div><span style="color:#64748b;">Total GPUs:</span> ${rc.total_gpus || na}</div>`;
        html += `<div><span style="color:#64748b;">TP Options:</span> ${(rc.tp_options || []).join(', ') || na}</div>`;
        html += `<div><span style="color:#64748b;">TP Pair Breadth:</span> Top-${rc.tp_pair_top_n || 4}</div>`;
        html += `<div><span style="color:#64748b;">P/D Ratio Search:</span> ${rc.pd_search_mode === 'exhaustive' ? 'Exhaustive' : 'Smart'}</div>`;
        html += `<div><span style="color:#64748b;">Use Achievable QPS:</span> ${rc.use_achievable_qps ? 'Yes' : 'No'}</div>`;
        html += `<div><span style="color:#64748b;">Headroom:</span> ${rc.headroom || 1.3}x</div>`;
        if (rc.latency_constraint_enabled) {
            html += `<div><span style="color:#64748b;">Latency SLA:</span> ${rc.latency_constraint_ms}ms @ ${rc.latency_constraint_percentile}</div>`;
        } else {
            html += `<div><span style="color:#64748b;">Latency SLA:</span> Disabled</div>`;
        }
        html += '</div>';
        html += '</div>';

        // Right column: Infrastructure + Advanced vLLM Settings
        html += '<div>';
        // Infrastructure
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #f59e0b;padding-bottom:4px;">Infrastructure</div><div style="line-height:2.2;margin-bottom:20px;">';
        html += `<div><span style="color:#64748b;">Inference Image:</span> <span style="word-break:break-all;font-size:0.9em;">${rc.image || na}</span></div>`;
        html += `<div><span style="color:#64748b;">Scheduler Image:</span> <span style="word-break:break-all;font-size:0.9em;">${rc.scheduler_image || na}</span></div>`;
        html += `<div><span style="color:#64748b;">Namespace:</span> ${rc.namespace || na}</div>`;
        html += `<div><span style="color:#64748b;">PVC:</span> ${rc.pvc_name || na}</div>`;
        html += `<div><span style="color:#64748b;">Network Type:</span> ${rc.network_type || na}</div>`;
        html += `<div><span style="color:#64748b;">NCCL IB HCA:</span> ${rc.nccl_ib_hca || na}</div>`;
        if (rc.rdma_nics_per_node) html += `<div><span style="color:#64748b;">RDMA NICs/Node:</span> ${rc.rdma_nics_per_node}</div>`;
        html += '</div>';
        // Advanced vLLM Settings
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #8b5cf6;padding-bottom:4px;">Advanced vLLM Settings</div><div style="line-height:2.2;">';
        const vllmCustomEnabled = rc.advanced_vllm_custom_enabled !== false;
        if (!vllmCustomEnabled) {
            html += '<div style="color:#059669;font-style:italic;margin-bottom:8px;">Using upstream vLLM defaults — no overrides applied.</div>';
            html += `<div><span style="color:#64748b;">Max Model Len:</span> ${rc.max_model_len || 'auto (from ISL+OSL)'}</div>`;
            html += `<div><span style="color:#64748b;">GPU Memory Utilization:</span> auto (calculated from model size + GPU VRAM)</div>`;
            html += `<div><span style="color:#64748b;">Block Size:</span> auto (next power of 2 of √(ISL+OSL))</div>`;
            html += `<div><span style="color:#64748b;">Max Num Seqs:</span> auto (from KV cache capacity)</div>`;
            html += `<div><span style="color:#64748b;">Max Batched Tokens:</span> vLLM default</div>`;
            html += `<div><span style="color:#64748b;">Dtype:</span> auto (from model config)</div>`;
            html += `<div><span style="color:#64748b;">KV Cache Dtype:</span> auto (same as model dtype)</div>`;
            html += `<div><span style="color:#64748b;">Prefix Caching:</span> Enabled</div>`;
            html += `<div><span style="color:#64748b;">Trust Remote Code:</span> Enabled</div>`;
            html += `<div><span style="color:#64748b;">Expert Parallel:</span> auto (enabled for MoE with TP &gt; 1)</div>`;
        } else {
            html += `<div><span style="color:#64748b;">Max Model Len:</span> ${advVal('max_model_len', rc.max_model_len)}</div>`;
            html += `<div><span style="color:#64748b;">GPU Memory Utilization:</span> ${advVal('gpu_memory_utilization', rc.gpu_memory_utilization)}</div>`;
            html += `<div><span style="color:#64748b;">Block Size:</span> ${advVal('block_size', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">Max Num Seqs:</span> ${advVal('max_num_seqs', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">Max Batched Tokens:</span> ${advVal('max_num_batched_tokens', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">Dtype:</span> ${advVal('dtype', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">KV Cache Dtype:</span> ${advVal('kv_cache_dtype', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">Pipeline Parallel:</span> ${advVal('pipeline_parallel_size', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">Tool Call Parser:</span> ${advVal('tool_call_parser', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">Reasoning Parser:</span> ${advVal('reasoning_parser', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">Chat Template Format:</span> ${advVal('chat_template_content_format', 'auto')}</div>`;
            html += `<div><span style="color:#64748b;">Prefix Caching:</span> ${advToggle('enable_prefix_caching', 'On (auto)')}</div>`;
            html += `<div><span style="color:#64748b;">Expert Parallel:</span> ${advToggle('enable_expert_parallel', 'Off (auto)')}</div>`;
            html += `<div><span style="color:#64748b;">Custom All-Reduce:</span> ${advToggle('disable_custom_all_reduce', 'Enabled (auto)')}</div>`;
            html += `<div><span style="color:#64748b;">Trust Remote Code:</span> ${advToggle('trust_remote_code', 'On (auto)')}</div>`;
            html += `<div><span style="color:#64748b;">Disable Log Requests:</span> ${advToggle('disable_log_requests', 'On (auto)')}</div>`;
            html += `<div><span style="color:#64748b;">Auto Tool Choice:</span> ${advToggle('enable_auto_tool_choice', 'Off (auto)')}</div>`;
            html += `<div><span style="color:#64748b;">vLLM Debug Logs:</span> ${advToggle('vllm_debug_logs', 'Off (auto)')}</div>`;
            html += `<div><span style="color:#64748b;">NCCL Debug Logs:</span> ${advToggle('nccl_debug_logs', 'Off (auto)')}</div>`;
        }
        html += '</div>';
        // EPP Configuration
        const eppCustomEnabled = rc.epp_custom_enabled !== false;
        const eppPresetLabels = {balanced:'Balanced', cache_optimized:'Cache Optimized', queue_balanced:'Queue Balanced', latency_aware:'Latency Aware', custom:'Custom'};
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #7c3aed;padding-bottom:4px;">EPP Configuration</div><div style="line-height:2.2;">';
        if (!eppCustomEnabled) {
            html += '<div style="color:#059669;font-style:italic;margin-bottom:8px;">Using upstream llm-d EPP defaults — no overrides applied.</div>';
            html += '<div><span style="color:#64748b;">Routing:</span> disagg-headers-handler → always-disagg-pd-decider</div>';
            html += '<div><span style="color:#64748b;">Prefill Scorers:</span> prefix-cache (w:3), queue (w:2), kv-cache-utilization (w:2)</div>';
            html += '<div><span style="color:#64748b;">Decode Scorers:</span> active-request (w:2), prefix-cache (w:3)</div>';
        }
        html += `<div><span style="color:#64748b;">Scoring Preset:</span> <strong>${eppCustomEnabled ? (eppPresetLabels[rc.epp_preset] || rc.epp_preset || 'Balanced') : 'llm-d upstream'}</strong></div>`;
        html += `<div><span style="color:#64748b;">EPP Tuning (Step 9):</span> ${rc.epp_benchmark ? 'Enabled' : 'Disabled'}</div>`;
        if (rc.epp_config) {
            const ec = rc.epp_config;
            if (ec.maxPrefixBlocksToMatch) html += `<div><span style="color:#64748b;">Max Prefix Blocks:</span> ${ec.maxPrefixBlocksToMatch}</div>`;
            if (ec.lruCapacityPerServer) html += `<div><span style="color:#64748b;">LRU Capacity/Server:</span> ${ec.lruCapacityPerServer}</div>`;
            if (ec.nonCachedTokens) html += `<div><span style="color:#64748b;">Non-Cached Tokens:</span> ${ec.nonCachedTokens}</div>`;
        }
        html += '</div>';
        html += '</div>';

        html += '</div></div></div>';
        secTestCfg = html; html = '';
    }

    // Architecture comparison chart + percentile bar chart → Comparison tab (above tables)
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
            html += `<tr><td><strong>Throughput P90</strong></td><td>${cmp.pd.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_p90} req/s</td><td style="color:${tputColor}; font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
            if (cmp.pd.ttft_p99 && cmp.aggregated.ttft_p99) {
                const p99Color = cmp.ttft_p99_winner === 'PD' ? '#10b981' : '#f59e0b';
                html += `<tr style="border-top:2px solid #e2e8f0;"><td><strong>TTFT P99 (tail)</strong></td><td>${cmp.pd.ttft_p99} ms</td><td>${cmp.aggregated.ttft_p99} ms</td><td style="color:${p99Color}; font-weight:700;">${cmp.ttft_p99_winner} (${cmp.ttft_p99_diff_pct}% better)</td></tr>`;
            }
            html += '</table></div>';

            // --- % Change chart: All PD configs vs Aggregated baseline ---
            if (rec.aggregated_baseline && data.all_results && data.all_results.length > 1) {
                const aggBaseline = rec.aggregated_baseline;
                const baseTtft = aggBaseline.ttft_p90;
                const baseTput = aggBaseline.throughput_p90;
                const configs = data.all_results.filter(r => r.architecture === 'PD' && r.ttft_p90 && r.throughput_p90);
                if (configs.length && baseTtft && baseTput) {
                    html += '<div style="padding:16px 20px 4px;"><div style="font-weight:700; font-size:0.95em; color:#1e293b; margin-bottom:4px;">All PD Configurations vs Aggregated Baseline</div>';
                    html += '<div style="color:#1e293b; font-size:0.92em; margin-bottom:12px;">Percentage change in TTFT and Throughput relative to the Step 8 Aggregated baseline (' + aggBaseline.config_name + '). For TTFT, negative (green) is better. For Throughput, positive (green) is better.</div>';
                    var pdTableId = 'pd-vs-agg-table-' + runId;
                    html += '<div class="chart-card-body" style="padding:0;"><table class="results-table" id="' + pdTableId + '">';
                    html += '<tr>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',1,\'num\')">TTFT P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',2,\'num\')">TTFT vs Agg &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',3,\'num\')">Throughput P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',4,\'num\')">Tput vs Agg &#x21C5;</th>';
                    html += '</tr>';
                    const sorted = [...configs].sort((a, b) => a.ttft_p90 - b.ttft_p90);
                    for (const cfg of sorted) {
                        const ttftPct = ((cfg.ttft_p90 - baseTtft) / baseTtft * 100).toFixed(1);
                        const tputPct = ((cfg.throughput_p90 - baseTput) / baseTput * 100).toFixed(1);
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
                        html += `<td data-val="${cfg.throughput_p90}">${cfg.throughput_p90} req/s</td>`;
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
            html += `<tr><td><strong>Throughput P90</strong></td><td>${cmp.ep.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_p90} req/s</td><td style="color:${tputColor}; font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
            html += '</table></div>';

            // --- % Change chart: All EP configs vs Aggregated baseline ---
            if (rec.aggregated_baseline && rec.ep_all_configs && rec.ep_all_configs.length > 0) {
                const aggBaseline = rec.aggregated_baseline;
                const baseTtft = aggBaseline.ttft_p90;
                const baseTput = aggBaseline.throughput_p90;
                if (baseTtft && baseTput) {
                    html += '<div style="padding:16px 20px 4px;"><div style="font-weight:700; font-size:0.95em; color:#1e293b; margin-bottom:4px;">All EP Configurations vs Aggregated Baseline</div>';
                    html += '<div style="color:#1e293b; font-size:0.92em; margin-bottom:12px;">Percentage change in TTFT and Throughput relative to the Step 8 Aggregated baseline. For TTFT, negative (green) is better. For Throughput, positive (green) is better.</div>';
                    var epTableId = 'ep-vs-agg-table-' + runId;
                    html += '<div class="chart-card-body" style="padding:0;"><table class="results-table" id="' + epTableId + '">';
                    html += '<tr>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',1,\'num\')">TTFT P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',2,\'num\')">TTFT vs Agg &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',3,\'num\')">Throughput P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',4,\'num\')">Tput vs Agg &#x21C5;</th>';
                    html += '</tr>';
                    const sorted = [...rec.ep_all_configs].sort((a, b) => (b.throughput_p90||0) - (a.throughput_p90||0));
                    for (const cfg of sorted) {
                        if (!cfg.ttft_p90 || !cfg.throughput_p90) continue;
                        const ttftPct = ((cfg.ttft_p90 - baseTtft) / baseTtft * 100).toFixed(1);
                        const tputPct = ((cfg.throughput_p90 - baseTput) / baseTput * 100).toFixed(1);
                        const ttftBetter = parseFloat(ttftPct) < 0;
                        const tputBetter = parseFloat(tputPct) > 0;
                        const ttftColor = ttftBetter ? '#059669' : '#dc2626';
                        const tputColor = tputBetter ? '#059669' : '#dc2626';
                        const ttftArrow = ttftBetter ? '&#9660;' : '&#9650;';
                        const tputArrow = tputBetter ? '&#9650;' : '&#9660;';
                        const label = `EP TP${cfg.tp} x ${cfg.replicas} replicas`;
                        html += `<tr><td><strong>${label}</strong></td>`;
                        html += `<td data-val="${cfg.ttft_p90}">${cfg.ttft_p90} ms</td>`;
                        html += `<td data-val="${ttftPct}" style="color:${ttftColor}; font-weight:700;">${ttftArrow} ${ttftPct}%</td>`;
                        html += `<td data-val="${cfg.throughput_p90}">${cfg.throughput_p90} req/s</td>`;
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
    if (data.calibrated_qps) {
        const cal = data.calibrated_qps;
        // Determine primary architecture (PD or EP)
        const primary = cal.pd || cal.ep;
        const primaryKey = cal.pd ? 'pd' : 'ep';
        const primaryLabel = cal.pd ? 'PD' : 'EP';

        html += '<div class="chart-card" style="margin-top:16px; border:2px solid #059669; border-left:6px solid #059669;"><div class="chart-card-header" style="background:linear-gradient(135deg,#059669,#10b981);">Step 11: Calibrated Load Validation</div>';
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

        // --- Table 1: Percentile Breakdown at Calibrated Load ---
        const isBalanced = !!(cal.pd && cal.ep);
        const requestedRps = cal.requested_rps != null ? cal.requested_rps : null;
        const rpsLabel = requestedRps != null ? ` at ${Math.round(requestedRps)} concurrent` : '';

        // Collect entries
        const calEntries = [];
        if (cal.pd) calEntries.push({label: 'PD', entry: cal.pd});
        if (cal.aggregated) calEntries.push({label: 'Aggregated', entry: cal.aggregated});
        if (isBalanced && cal.ep) calEntries.push({label: 'EP', entry: cal.ep});

        const tableTitle = calEntries.length > 1
            ? 'Percentile Breakdown: ' + calEntries.map(e => e.label).join(' vs ') + rpsLabel
            : 'Percentile Breakdown' + rpsLabel;

        html += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b;">${tableTitle}</div>`;
        html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
        html += '<tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';

        // Helper: find best P90 value per metric for highlighting
        function findBest(metric, lowerIsBetter) {
            const vals = calEntries.map(e => e.entry[metric]).filter(v => v != null);
            if (!vals.length) return null;
            return lowerIsBetter ? Math.min(...vals) : Math.max(...vals);
        }
        const bestTtft = findBest('ttft_p90', true);
        const bestTput = findBest('throughput_p90', false);
        const bestItl = findBest('itl_p90', true);
        const hl = (val, best) => val != null && val === best ? 'color:#059669; font-weight:700;' : '';

        const fmt = (v, unit) => v != null ? `${v} ${unit}` : '-';

        calEntries.forEach(({label, entry}, idx) => {
            const metrics = [
                {name: 'TTFT (ms)', p50: entry.ttft_p50, p90: entry.ttft_p90, p95: entry.ttft_p95, p99: entry.ttft_p99, best: bestTtft, p90key: 'ttft_p90', unit: ''},
                {name: 'ITL (ms)', p50: entry.itl_p50, p90: entry.itl_p90, p95: entry.itl_p95, p99: entry.itl_p99, best: bestItl, p90key: 'itl_p90', unit: ''},
                {name: 'Throughput (req/s)', p50: entry.throughput_p50, p90: entry.throughput_p90, p95: entry.throughput_p95, p99: entry.throughput_p99, best: bestTput, p90key: 'throughput_p90', unit: ''},
            ];
            metrics.forEach((m, mi) => {
                const borderStyle = mi === 0 && idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
                const rowspan = mi === 0 ? ` rowspan="3" style="vertical-align:middle; font-weight:700;${borderStyle}"` : '';
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

        // --- Table 2: Overload Impact ---
        const overloadKey = cal.overloaded_pd ? 'overloaded_pd' : (cal.overloaded_ep ? 'overloaded_ep' : null);
        const overloadData = overloadKey ? cal[overloadKey] : null;
        if (overloadData) {
            const origConcurrency = cal.concurrency != null ? `${cal.concurrency} concurrent` : '-';
            const calConcurrency = requestedRps != null ? `${Math.round(requestedRps)} concurrent` : '-';
            html += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b; margin-top:8px;">Overload Impact: ${primaryLabel} at Calibrated vs Overloaded Load</div>`;
            html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            html += '<tr><th>Configuration</th><th>Load</th><th>TTFT P90</th><th>Throughput P90</th></tr>';
            html += `<tr><td><strong>${primaryLabel} (calibrated)</strong></td><td>${calConcurrency}</td><td style="color:#059669; font-weight:700;">${primary.ttft_p90} ms</td><td style="color:#059669; font-weight:700;">${primary.throughput_p90} req/s</td></tr>`;
            html += `<tr><td><strong>${primaryLabel} (overloaded)</strong></td><td>${origConcurrency}</td><td style="color:#94a3b8;">${overloadData.ttft_p90} ms</td><td style="color:#94a3b8;">${overloadData.throughput_p90} req/s</td></tr>`;
            html += '</table></div>';
        }
        html += '</div>';
    }

    // Flush calibrated load (Step 10)
    secCal = html; html = '';

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
            if (!trials || !trials.length) return;
            const archLabel = arch.toUpperCase();
            const eppCardId = `epp-${arch}-${runId}`;

            eppHtml += `<div class="chart-card" style="margin-top:${archIdx > 0 ? '16' : '0'}px; border:2px solid #7c3aed; border-left:6px solid #7c3aed;">`;
            eppHtml += `<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">Step 9: EPP Tuning — ${archLabel}</div>`;
            eppHtml += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Same deployment, different EPP scoring weights. Each test swapped only the gateway configmap (~10s) to isolate the impact of request routing.</div>';

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

            // Charts P90, P95, P99
            const pctls = [{key:'p90',label:'P90',color:'#3b82f6'},{key:'p95',label:'P95',color:'#dc2626'},{key:'p99',label:'P99',color:'#7c3aed'}];
            pctls.forEach(pctl => {
                eppHtml += `<div id="${eppCardId}-${pctl.key}${_chartSuffix}" style="height:400px; margin:8px 20px; background:#fff; border-radius:8px; border:1px solid #e2e8f0;"></div>`;
            });

            // Summary below charts
            const baselineTrial = trials.find(t => t.is_baseline);
            eppHtml += '<div style="padding:8px 20px 16px; font-size:0.9em; line-height:1.8; color:#1e293b;">';
            trials.forEach(t => {
                const ttft = t.ttft_p90 != null ? t.ttft_p90.toFixed(0) + 'ms' : 'N/A';
                const tput = t.throughput_mean != null ? t.throughput_mean.toFixed(1) : (t.throughput_p90 != null ? t.throughput_p90.toFixed(1) : 'N/A');
                let comparison = '';
                if (baselineTrial && !t.is_baseline && baselineTrial.ttft_p90 && t.ttft_p90 && baselineTrial.throughput_mean && t.throughput_mean) {
                    const ttftPct = ((t.ttft_p90 - baselineTrial.ttft_p90) / baselineTrial.ttft_p90 * 100);
                    const tputPct = ((t.throughput_mean - baselineTrial.throughput_mean) / baselineTrial.throughput_mean * 100);
                    let ttftStr, tputStr;
                    if (Math.abs(ttftPct) < 5) { ttftStr = 'similar TTFT'; }
                    else if (ttftPct < 0) { ttftStr = `<span style="color:#059669;">${Math.abs(ttftPct).toFixed(0)}% faster</span>`; }
                    else { ttftStr = `<span style="color:#dc2626;">${ttftPct.toFixed(0)}% slower</span>`; }
                    if (Math.abs(tputPct) < 5) { tputStr = 'similar throughput'; }
                    else if (tputPct > 0) { tputStr = `<span style="color:#059669;">${tputPct.toFixed(0)}% higher throughput</span>`; }
                    else { tputStr = `<span style="color:#dc2626;">${Math.abs(tputPct).toFixed(0)}% lower throughput</span>`; }
                    comparison = ` — ${ttftStr}, ${tputStr}`;
                }
                const icon = t === bestTrial && !t.is_baseline ? '⭐' : (t.is_baseline ? '📊' : '🔧');
                const w = t.weights || {};
                const wStr = `${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'}:${w.active_request || 0}`;
                eppHtml += `<div>${icon} <strong>${t.name}</strong> (${wStr}): TTFT P90=${ttft}, Throughput=${tput} req/s${comparison}</div>`;
            });
            eppHtml += '</div>';

            // Results table
            eppHtml += '<div style="padding:0 20px 16px;"><table class="results-table">';
            eppHtml += '<tr><th>Strategy</th><th>Weights (C:K:Q)</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Tput P90</th><th>ITL P90</th><th>EPP Config</th></tr>';
            trials.forEach(e => {
                const isBest = e === bestTrial && !e.is_baseline;
                const isBase = e.is_baseline;
                const cls = isBest ? ' class="pareto-row"' : (isBase ? ' style="background:#f8fafc;color:#64748b;font-style:italic;"' : '');
                const w = e.weights || {};
                const wStr = `${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'}`;
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

        secEppTuning = eppHtml;
    }

    if (secRec) subtabDefs.push({ id: 'recommendation', label: 'Recommendation', icon: '&#9733;' });
    if (secTP) subtabDefs.push({ id: 'tp-calibration', label: 'TP Calibration', icon: '&#9881;' });
    if (secCfg) subtabDefs.push({ id: 'configurations', label: 'Configurations', icon: '&#9776;' });
    if (secCmp) subtabDefs.push({ id: 'comparison', label: 'Comparison', icon: '&#8596;' });
    if (secStep9) subtabDefs.push({ id: 'latency-search', label: 'Latency Search', icon: '&#128269;' });
    if (secCal) subtabDefs.push({ id: 'calibrated-load', label: 'Calibrated Load', icon: '&#9878;' });
    if (secVLLM) subtabDefs.push({ id: 'vllm-metrics', label: 'vLLM Metrics', icon: '&#9889;' });
    if (secEppTuning) subtabDefs.push({ id: 'epp-tuning', label: 'EPP Tuning', icon: '&#9881;' });
    if (secTestCfg) subtabDefs.push({ id: 'test-settings', label: 'Test Settings', icon: '&#9881;' });
    subtabDefs.push({ id: 'estimator', label: 'Estimator', icon: '&#128200;' });

    const sectionMap = {
        'recommendation': secRec, 'tp-calibration': secTP, 'configurations': secCfg,
        'test-settings': secTestCfg, 'comparison': secCmp, 'latency-search': secStep9,
        'calibrated-load': secCal, 'vllm-metrics': secVLLM, 'epp-tuning': secEppTuning,
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
        const traces = charts.scatter.traces.map(t => ({
            x: t.x, y: t.y, text: t.text, name: t.name,
            mode: 'markers',
            marker: { size: t.sizes, color: t.color, opacity: 0.7, line: { width: 1, color: 'white' } },
            hovertemplate: '<b>%{text}</b><extra></extra>'
        }));
        Plotly.newPlot(cid('chart-scatter'), traces, { ...plotlyLayout, xaxis: { title: 'TTFT P90 (ms) - lower is better' }, yaxis: { title: 'Throughput Mean (req/s) - higher is better' }, showlegend: true }, plotlyConfig);
    }

    // Efficiency bar
    if (charts.efficiency.configs.length) {
        Plotly.newPlot(cid('chart-efficiency'), [{
            x: charts.efficiency.configs, y: charts.efficiency.values,
            type: 'bar', marker: { color: charts.efficiency.colors },
            text: charts.efficiency.values.map(v => v != null ? v.toFixed(3) : ''),
            textposition: 'outside', textfont: { size: 11, color: '#333' },
            cliponaxis: false, constraintext: 'none',
            hovertemplate: '<b>%{x}</b><br>%{y:.3f} req/s/GPU<extra></extra>'
        }], { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 120 }, xaxis: { tickangle: -45 }, yaxis: { title: 'Mean req/s per GPU - higher is better' } }, plotlyConfig);
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
    const pdResults = data.all_results.filter(r => r.architecture === 'PD');
    if (pdResults.length) {
        const sorted = [...pdResults].sort((a, b) => a.prefill_pods - b.prefill_pods);
        const labels = sorted.map(r => `${r.prefill_pods}P : ${r.decode_pods}D`);
        const aggBase = rec ? rec.aggregated_baseline : null;

        const ttftPercentiles = [
            { key: 'p90', field: 'ttft_p90', tputField: 'throughput_mean', color: '#3b82f6', chartId: 'chart-pd-ttft-p90' },
            { key: 'p95', field: 'ttft_p95', tputField: 'throughput_mean', color: '#dc2626', chartId: 'chart-pd-ttft-p95' },
            { key: 'p99', field: 'ttft_p99', tputField: 'throughput_mean', color: '#7c3aed', chartId: 'chart-pd-ttft-p99' },
        ];

        ttftPercentiles.forEach(pctl => {
            const ttftVals = sorted.map(r => r[pctl.field]);
            const tputVals = sorted.map(r => r[pctl.tputField] || r.throughput_p90);
            const bestTtft = Math.min(...ttftVals);
            const bestTtftIdx = ttftVals.indexOf(bestTtft);
            const bestTput = Math.max(...tputVals);
            const bestTputIdx = tputVals.indexOf(bestTput);
            const pLabel = pctl.key.toUpperCase();

            const hoverText = sorted.map(r =>
                `<b>${r.prefill_pods} Prefill pods</b> (TP=${r.prefill_tp})<br>` +
                `<b>${r.decode_pods} Decode pods</b> (TP=${r.decode_tp})<br>` +
                `TTFT ${pLabel}: <b>${r[pctl.field].toFixed(1)} ms</b><br>` +
                `Throughput Mean: ${r[pctl.tputField] || r.throughput_p90} req/s<br>` +
                `Total GPUs: ${r.gpus}`
            );

            const shapes = [];
            const annotations = [];
            const aggTtft = aggBase ? aggBase[pctl.field] : null;
            const aggTput = aggBase ? aggBase[pctl.tputField] : null;
            if (aggTtft) {
                shapes.push({ type: 'line', x0: -0.5, x1: labels.length - 0.5, y0: aggTtft, y1: aggTtft, yref: 'y', line: { color: pctl.color, width: 2, dash: 'dash' } });
                annotations.push({ x: 0, y: aggTtft, yref: 'y', text: `Agg TTFT ${pLabel}: ${fmtSI(aggTtft)} ms`, showarrow: false, font: { color: pctl.color, size: 11, weight: 700 }, xanchor: 'left', yanchor: 'bottom', yshift: 5, bgcolor: 'rgba(255,255,255,0.85)' });
            }
            if (aggTput) {
                shapes.push({ type: 'line', x0: -0.5, x1: labels.length - 0.5, y0: aggTput, y1: aggTput, yref: 'y2', line: { color: '#f59e0b', width: 2, dash: 'dash' } });
                annotations.push({ x: labels.length - 1, y: aggTput, yref: 'y2', text: `Agg Tput ${pLabel}: ${aggTput} req/s`, showarrow: false, font: { color: '#f59e0b', size: 11, weight: 700 }, xanchor: 'right', yanchor: 'bottom', yshift: 5, bgcolor: 'rgba(255,255,255,0.85)' });
            }

            // Add EPP TUNED annotations for EPP-tuned points
            sorted.forEach((r, i) => {
                if (r.test_id && r.test_id.startsWith('step11-epp-')) {
                    annotations.push({ x: labels[i], y: ttftVals[i], yref: 'y', text: '<b>EPP TUNED</b>', showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: '#7c3aed', ax: 55, ay: 0, font: { size: 9, color: 'white' }, bgcolor: '#7c3aed', borderpad: 3, bordercolor: '#7c3aed', borderwidth: 1 });
                    annotations.push({ x: labels[i], y: tputVals[i], yref: 'y2', text: '<b>EPP TUNED</b>', showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: '#7c3aed', ax: 55, ay: 0, font: { size: 9, color: 'white' }, bgcolor: '#7c3aed', borderpad: 3, bordercolor: '#7c3aed', borderwidth: 1 });
                }
            });
            Plotly.newPlot(cid(pctl.chartId), [
                {
                    x: labels, y: ttftVals, name: `TTFT ${pLabel}`,
                    type: 'scatter', mode: 'lines+markers',
                    line: { color: pctl.color, width: 3, shape: 'spline' },
                    marker: { color: pctl.color, size: 12, symbol: 'circle', line: { width: 2, color: 'white' } },
                    hovertext: hoverText, hoverinfo: 'text',
                    fill: 'tozeroy', fillcolor: pctl.color + '14',
                },
                {
                    x: [labels[bestTtftIdx]], y: [bestTtft], name: `Best TTFT`,
                    type: 'scatter', mode: 'markers',
                    marker: { color: '#10b981', size: 22, symbol: 'circle', line: { width: 3, color: 'white' } },
                    hovertext: [hoverText[bestTtftIdx]], hoverinfo: 'text',
                    showlegend: true,
                },
                {
                    x: labels, y: tputVals, name: `Throughput Mean`,
                    type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                    line: { color: '#f59e0b', width: 3, shape: 'spline' },
                    marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } },
                    hovertemplate: `Throughput Mean: %{y:.2f} req/s<extra></extra>`,
                },
                {
                    x: [labels[bestTputIdx]], y: [tputVals[bestTputIdx]], name: `Best Throughput`,
                    type: 'scatter', mode: 'markers', yaxis: 'y2',
                    marker: { color: '#e11d48', size: 22, symbol: 'diamond', line: { width: 3, color: 'white' } },
                    hovertext: [hoverText[bestTputIdx]], hoverinfo: 'text',
                    showlegend: true,
                },
            ], {
                ...plotlyLayout,
                height: 500,
                margin: { t: 30, b: 80, l: 60, r: 60 },
                xaxis: { title: 'Prefill : Decode Pod Ratio' },
                yaxis: { title: `TTFT ${pLabel} (ms) — lower is better`, side: 'left', titlefont: { color: pctl.color }, tickfont: { color: pctl.color }, tickformat: '.2s' },
                yaxis2: { title: `Throughput Mean (req/s) — higher is better`, side: 'right', overlaying: 'y', titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
                showlegend: true,
                legend: { x: 0, y: 1.18, orientation: 'h' },
                shapes: shapes,
                annotations: annotations,
            }, plotlyConfig);
        });
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

        // Chart 1: TTFT Percentiles (grouped bar)
        Plotly.newPlot(cid('chart-vllm-ttft'), [
            { x: vllm.configs, y: vllm.ttft.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 } },
            { x: vllm.configs, y: vllm.ttft.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 } },
            { x: vllm.configs, y: vllm.ttft.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 } },
            { x: vllm.configs, y: vllm.ttft.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 } },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'TTFT (ms) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 2: ITL Percentiles (grouped bar)
        Plotly.newPlot(cid('chart-vllm-itl'), [
            { x: vllm.configs, y: vllm.itl.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 } },
            { x: vllm.configs, y: vllm.itl.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 } },
            { x: vllm.configs, y: vllm.itl.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 } },
            { x: vllm.configs, y: vllm.itl.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 } },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'ITL (ms) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 3: E2E Latency (grouped bar)
        Plotly.newPlot(cid('chart-vllm-e2e'), [
            { x: vllm.configs, y: vllm.e2e.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 } },
            { x: vllm.configs, y: vllm.e2e.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 } },
            { x: vllm.configs, y: vllm.e2e.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 } },
            { x: vllm.configs, y: vllm.e2e.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 } },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'E2E Latency (seconds) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 4: Token Throughput (grouped bar)
        Plotly.newPlot(cid('chart-vllm-tokens'), [
            { x: vllm.configs, y: vllm.token_rates.prompt, name: 'Prompt Tokens/s', type: 'bar', marker: { color: '#6366f1' },
              hovertemplate: '<b>%{x}</b><br>Prompt: %{y:.0f} tokens/s<extra></extra>' },
            { x: vllm.configs, y: vllm.token_rates.generation, name: 'Generation Tokens/s', type: 'bar', marker: { color: '#10b981' },
              hovertemplate: '<b>%{x}</b><br>Generation: %{y:.0f} tokens/s<extra></extra>' },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Tokens/second — higher is better' } }, plotlyConfig);

        // Chart 5: Request Queue & KV Cache (dual axis)
        Plotly.newPlot(cid('chart-vllm-queue'), [
            { x: vllm.configs, y: vllm.request_state.running, name: 'Avg Running', type: 'bar', marker: { color: '#3b82f6' },
              hovertemplate: '<b>%{x}</b><br>Running: %{y:.1f}<extra></extra>' },
            { x: vllm.configs, y: vllm.request_state.waiting, name: 'Avg Waiting', type: 'bar', marker: { color: '#ef4444' },
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

    // Initialize subtab switching (activates first pane, hides others)
    initReportSubtabs(content);
}

