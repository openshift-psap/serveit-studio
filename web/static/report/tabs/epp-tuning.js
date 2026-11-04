// tabs/epp-tuning.js — EPP Tuning report tab. One implementation for live + offline.
(function() {
    var _renders = [];
    function build(data, ctx) {
        var s = '';
        var _sfx = '';
        if (ctx.mode === 'live' && typeof _chartSuffix !== 'undefined') _sfx = _chartSuffix;
        _renders = [];
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
            const eppCardId = `epp-${arch}-${ctx.runId}`;

            s += `<div class="chart-card" style="margin-top:${archIdx > 0 ? '16' : '0'}px; border:2px solid #7c3aed; border-left:6px solid #7c3aed;">`;
            s += `<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">Step 9: EPP Tuning — ${archLabel}</div>`;
            s += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Same deployment, different EPP routing configurations. Each test swapped only the gateway configmap (~10s) to isolate the impact of request routing.</div>';
            s += '<div style="padding:8px 20px 12px; font-size:0.82em; color:#64748b;">' +
                '<table style="width:100%;border-collapse:collapse;font-size:1em;">' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">opt-baseline <span style="font-weight:400;color:#94a3b8;">(screen)</span></td><td style="padding:4px 0;color:#64748b;">llm-d optimized-baseline guide: prefix-cache-affinity-filter gates candidates to sticky endpoints, a single load scorer (token-load for prefill-bound, active-request for decode-bound) ranks the survivors.</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">weighted-max <span style="font-weight:400;color:#94a3b8;">(screen)</span></td><td style="padding:4px 0;color:#64748b;">Weighted multi-scorer stack with smart-derived weights and the deterministic max-score picker.</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">weighted-rand <span style="font-weight:400;color:#94a3b8;">(screen)</span></td><td style="padding:4px 0;color:#64748b;">Same weighted stack with the probabilistic weighted-random picker — spreads load instead of always picking the argmax.</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">preset <span style="font-weight:400;color:#94a3b8;">(screen)</span></td><td style="padding:4px 0;color:#64748b;">The user-selected preset structure — control arm for the screening phase.</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">abl-* <span style="font-weight:400;color:#94a3b8;">(ablate)</span></td><td style="padding:4px 0;color:#64748b;">One-at-a-time plugin add/remove on the screening winner. Removals come first (abl-filter-off, abl-session-off — does the winner\'s defining structure actually pay?); then additions (session-affinity, no-hit-lru, kv-cache, LoRA-affinity A/B).</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">w-* <span style="font-weight:400;color:#94a3b8;">(weight)</span></td><td style="padding:4px 0;color:#64748b;">Coordinate-descent moves: ±1 on the top scorer weights, or the affinity threshold / load weight for filter compositions.</td></tr>' +
                '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">final-* <span style="font-weight:400;color:#94a3b8;">(final)</span></td><td style="padding:4px 0;color:#64748b;">Full-duration validation of the best variant — the apples-to-apples comparison against the baseline.</td></tr>' +
                '</table></div>';

            // Summary cards
            // Best VARIANT — the as-run baseline is the reference row, not a candidate
            const _variants = trials.filter(t => !t.is_baseline);
            const bestTrial = (_variants.length ? _variants : trials).reduce((a, b) => (a.ttft_p90 || Infinity) < (b.ttft_p90 || Infinity) ? a : b);
            s += '<div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; padding:12px 20px;">';
            s += `<div style="background:#f0fdf4; border-radius:10px; padding:16px; text-align:center; border:1px solid #bbf7d0;"><div style="font-size:1.5em; font-weight:800; color:#059669;">${bestTrial.name}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Best Strategy</div></div>`;
            s += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:1.5em; font-weight:800; color:#1e293b;">${bestTrial.ttft_p90 || 'N/A'} <span style="font-size:0.5em; color:#64748b;">ms</span></div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">TTFT P90</div></div>`;
            s += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:1.5em; font-weight:800; color:#1e293b;">${(bestTrial.structure && bestTrial.structure.tokens_per_second) ? Math.round(bestTrial.structure.tokens_per_second) + ' tok/s' : (bestTrial.throughput_p90 || 'N/A')}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Tokens/s</div></div>`;
            const bw = bestTrial.weights || {};
            const bStruct = bestTrial.structure || {};
            let bestComp;
            if (bStruct.affinity_filter) {
                const loadScorer = bw.token_load ? `token-load×${bw.token_load}` : (bw.active_request ? `active-req×${bw.active_request}` : '');
                bestComp = `filter + ${loadScorer}`;
            } else {
                bestComp = `${bw.prefix_cache || 0}:${bw.kv_cache || 0}:${bw.queue || 0}:${bw.active_request || 0}` + (bw.token_load ? ` +tl${bw.token_load}` : '') + (bw.lora_affinity ? ` +lora${bw.lora_affinity}` : '');
            }
            bestComp += ` · ${(bStruct.picker || 'max-score-picker').replace('-picker', ' pick')}`;
            s += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:1.2em; font-weight:800; color:#1e293b;">${bestComp}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Best Routing Composition</div></div>`;
            s += '</div>';

            // Charts P90, P95, P99 with per-percentile summary
            const baselineTrial = trials.find(t => t.is_baseline);
            const pctls = [{key:'p90',label:'P90',color:'#3b82f6'},{key:'p95',label:'P95',color:'#dc2626'},{key:'p99',label:'P99',color:'#7c3aed'}];
            pctls.forEach(pctl => {
                s += `<div class="chart-card" style="margin:16px 20px;">`;
                s += `<div class="chart-card-header" style="background:linear-gradient(135deg,${pctl.color},${pctl.color}cc);">TTFT ${pctl.label} per Strategy — lower is better</div>`;
                s += `<div style="padding:6px 16px; color:#64748b; font-size:0.85em;">SLA target: ${eppTargetMs}ms at ${eppTargetPct.toUpperCase()} — bars under the dashed line meet the SLA.</div>`;
                s += `<div id="${eppCardId}-${pctl.key}${_sfx}" style="height:850px; margin:8px 20px; background:#fff; border-radius:8px; border:1px solid #e2e8f0;"></div>`;
                s += `</div>`;
                s += `<div style="padding:4px 20px 12px; font-size:0.85em; line-height:1.7; color:#1e293b;">`;
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
                    if (baselineTrial && !t.is_baseline) {
                        const bt = baselineTrial.structure && baselineTrial.structure.tokens_per_second;
                        const tt = t.structure && t.structure.tokens_per_second;
                        if (bt && tt) {
                            // tok/s delta — same window-exact metric as the displayed number
                            const tokPct = ((tt - bt) / bt * 100);
                            if (tokPct > 0.5) tputDelta = ` <span style="color:#059669;">(+${tokPct.toFixed(1)}%)</span>`;
                            else if (tokPct < -0.5) tputDelta = ` <span style="color:#dc2626;">(${tokPct.toFixed(1)}%)</span>`;
                            else tputDelta = ` <span style="color:#6b7280;">(±0%)</span>`;
                        } else if (baselineTrial.throughput_mean && t.throughput_mean) {
                            const tputPct = ((t.throughput_mean - baselineTrial.throughput_mean) / baselineTrial.throughput_mean * 100);
                            if (tputPct > 0.5) tputDelta = ` <span style="color:#059669;">(+${tputPct.toFixed(1)}% req/s)</span>`;
                            else if (tputPct < -0.5) tputDelta = ` <span style="color:#dc2626;">(${tputPct.toFixed(1)}% req/s)</span>`;
                            else tputDelta = ` <span style="color:#6b7280;">(0%)</span>`;
                        }
                    }
                    const icon = t.is_baseline ? '📊' : '🔧';
                    const w = t.weights || {};
                    const st = t.structure || {};
                    let wStr;
                    if (st.affinity_filter) {
                        wStr = 'affinity-filter + ' + (w.token_load ? `token-load×${w.token_load}` : (w.active_request ? `active-req×${w.active_request}` : 'load'));
                    } else {
                        wStr = `${w.prefix_cache ?? '?'}:${!w.kv_cache ? 'off' : w.kv_cache}:${w.queue ?? '?'}:${w.active_request ?? 0}` + (w.token_load ? ` +tl${w.token_load}` : '') + (w.lora_affinity ? ` +lora${w.lora_affinity}` : (w.lora_affinity === 0 && t.name && t.name.indexOf('off') >= 0 ? ' +lora-off' : ''));
                    }
                    if (st.picker === 'weighted-random-picker') wStr += ' [wr]';
                    const durTag = t.duration ? ` <span style="color:#94a3b8;">(${t.duration}s)</span>` : '';
                    const tokTag = t.structure && t.structure.tokens_per_second ? ` <span style="color:#059669;font-size:0.85em;">${Math.round(t.structure.tokens_per_second)} tok/s</span>` : '';
const deltaTag = (t.delta_vs_baseline && !t.is_baseline) ? ` <span style="color:#7c3aed;font-size:0.85em;">[${t.delta_vs_baseline}]</span>` : '';
                    s += `<div>${icon} <strong>${t.name}</strong> <span style="color:#94a3b8;font-size:0.85em;">[${t.phase || 'screen'}]</span> (${wStr})${durTag}${deltaTag}: TTFT ${pctl.label}=${ttft}${ttftDelta}, ${(t.structure && t.structure.tokens_per_second) ? Math.round(t.structure.tokens_per_second) + ' tok/s' : tput + ' req/s'}${tputDelta}</div>`;
                });
                s += '</div>';
            });

            // Results table
            s += '<div style="padding:0 20px 16px;"><table class="results-table">';
            s += `<tr><th onclick="_sortEppTable(this,0,'str')" style="cursor:pointer;">Strategy ⇅</th><th>Phase</th><th onclick="_sortEppTable(this,2,'str')" style="cursor:pointer;">Routing Composition ⇅</th><th>Δ vs Baseline</th><th onclick="_sortEppTable(this,4,'num')" style="cursor:pointer;">Tok/s ⇅</th><th onclick="_sortEppTable(this,5,'num')" style="cursor:pointer;">TTFT P50 ⇅</th><th onclick="_sortEppTable(this,6,'num')" style="cursor:pointer;">TTFT P90 ⇅</th><th onclick="_sortEppTable(this,7,'num')" style="cursor:pointer;">TTFT P95 ⇅</th><th onclick="_sortEppTable(this,8,'num')" style="cursor:pointer;">TTFT P99 ⇅</th><th onclick="_sortEppTable(this,9,'num')" style="cursor:pointer;">ITL P90 ⇅</th><th>EPP Config</th></tr>`;
            trials.forEach(e => {
                const isBest = e === bestTrial && !e.is_baseline;
                const isBase = e.is_baseline;
                const cls = isBest ? ' class="pareto-row"' : (isBase ? ' style="background:#f8fafc;color:#64748b;font-style:italic;"' : '');
                const w = e.weights || {};
                const st = e.structure || {};
                let wStr;
                if (st.affinity_filter) {
                    wStr = 'affinity-filter + ' + (w.token_load ? `token-load×${w.token_load}` : (w.active_request ? `active-req×${w.active_request}` : 'load'));
                } else {
                    wStr = `${w.prefix_cache ?? '?'}:${!w.kv_cache ? 'off' : w.kv_cache}:${w.queue ?? '?'}:${w.active_request ?? 0}` + (w.token_load ? ` +tl${w.token_load}` : '') + (w.lora_affinity ? ` +lora${w.lora_affinity}` : '');
                }
                const pickerBadge = st.picker === 'weighted-random-picker'
                    ? ' <span style="background:#0891b2;color:white;font-size:0.7em;padding:1px 5px;border-radius:3px;" title="weighted-random-picker: picks a pod randomly, weighted by score">w-rand pick</span>'
                    : (st.picker === 'max-score-picker' ? ' <span style="background:#475569;color:white;font-size:0.7em;padding:1px 5px;border-radius:3px;" title="max-score-picker: always picks the highest-scored pod">max pick</span>' : '');
                const phaseColors = {screen:'#7c3aed', ablate:'#d97706', weight:'#0891b2', final:'#059669'};
                const phaseColor = phaseColors[e.phase] || '#64748b';
                const phaseBadge = e.phase ? `<span style="background:${phaseColor};color:white;font-size:0.7em;padding:1px 6px;border-radius:3px;">${e.phase}</span>` : '—';
                const na = 'N/A';
                let ml = '-';
                if (e.manifest_types && e.manifest_types.length > 0) {
                    ml = e.manifest_types.map(t => `<a href="/api/run/${ctx.runId}/config/${e.test_id}/manifest/${t}" style="color:#7c3aed;text-decoration:none;font-size:11px;padding:2px 6px;background:#f5f3ff;border-radius:4px;border:1px solid #c4b5fd;display:inline-block;">${t}</a>`).join(' ');
                }
                const label = isBase ? `<span style="color:#94a3b8;">${e.name}</span>` : `<strong>${e.name}</strong>`;
                const deltaStr = e.delta_vs_baseline
                    ? `<span style="font-family:monospace;font-size:0.85em;color:#7c3aed;">${e.delta_vs_baseline}</span>`
                    : (isBase ? '<span style="color:#94a3b8;">—</span>' : '');
                const tokCell = e.structure && e.structure.tokens_per_second
                    ? `<strong>${Math.round(e.structure.tokens_per_second)}</strong>`
                    : '—';
                s += `<tr${cls}><td>${label}</td><td>${phaseBadge}</td><td>${wStr}${pickerBadge}</td><td>${deltaStr}</td><td sort-num="${e.structure && e.structure.tokens_per_second || 0}">${tokCell}</td>`;
                s += `<td sort-num="${e.ttft_p50 || 0}">${e.ttft_p50 ?? na}</td><td sort-num="${e.ttft_p90 || 0}">${e.ttft_p90 ?? na}</td><td sort-num="${e.ttft_p95 || 0}">${e.ttft_p95 ?? na}</td><td sort-num="${e.ttft_p99 || 0}">${e.ttft_p99 ?? na}</td>`;
                s += `<td sort-num="${e.itl_p90 || 0}">${e.itl_p90 ?? na}</td><td>${ml}</td></tr>`;
            });
            s += '</table></div>';

            // Legend — composition notation, icons, phases
            s += `<div style="padding:10px 16px; margin:0 20px 12px; font-size:0.78em; line-height:1.8; color:#64748b; background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px;">
                <div style="font-weight:600; color:#475569; margin-bottom:2px;">Legend</div>
                <div><b>Icons:</b> 📊 your config as-run (reference row) &nbsp;·&nbsp; 🔧 search-generated variant &nbsp;·&nbsp; highlighted row = best variant (lowest objective TTFT)</div>
                <div><b>Phases:</b> screen = short ranking run &nbsp;·&nbsp; ablate = plugin added/removed from the winner &nbsp;·&nbsp; weight = scorer weight moved &nbsp;·&nbsp; final = full-duration validation of the winner</div>
                <div><b>Composition:</b> "affinity-filter + scorer×W" — the prefix-cache-affinity-filter gates requests to cache-warm pods, then the scorer ranks survivors with weight W (omitted W = 1.0; "load" = token-load-scorer)</div>
                <div><b>Picker</b> (final stage, chooses the pod): <b>max pick</b> = max-score-picker, always takes the highest-scored pod &nbsp;·&nbsp; <b>w-rand pick</b> = weighted-random-picker, random pick weighted by score</div>
                <div><b>Weighted stack:</b> "a:b:c:d" = prefix-cache : kv-cache : queue : active-request scorer weights ("off" = disabled)</div>
                <div><b>Tok/s:</b> total tokens (prompt+output) ÷ benchmark elapsed time — window-exact and comparable across all rows</div>
            </div>`;

            s += '</div>';

            // Collect chart render functions — will execute after DOM update
            if (!window._eppChartRenders) window._eppChartRenders = [];
            _renders.push(() => {
                pctls.forEach(pctl => {
                    const el = document.getElementById(`${eppCardId}-${pctl.key}${_sfx}`);
                    if (!el) return;
                    const xLabels = trials.map(t => t.name);
                    const latencies = trials.map(t => t[`ttft_${pctl.key}`]);
                    const throughputs = trials.map(t => (t.structure && t.structure.tokens_per_second) || t.throughput_mean || t.throughput_p90);
                    const bestIdx = latencies.indexOf(Math.min(...latencies.filter(v => v != null)));
                    const markerColors = latencies.map((v, i) => {
                        if (eppTargetMs && v != null) return v <= eppTargetMs ? '#10b981' : '#ef4444';
                        return i === bestIdx ? '#10b981' : pctl.color;
                    });
                    const latText = latencies.map(v => v != null ? v.toFixed(0) + 'ms' : '');
                    const tputText = throughputs.map(v => v != null ? (v > 100 ? Math.round(v) : v.toFixed(1)) : '');
                    const traces = [
                        {x: xLabels, y: latencies, name: `TTFT ${pctl.label}`, type: 'scatter', mode: 'lines+markers+text',
                         line: {color: pctl.color, width: 3, shape: 'spline'},
                         marker: {color: markerColors, size: 12, symbol: 'circle', line: {width: 2, color: 'white'}},
                         text: latText, textposition: 'top center', textfont: {size: 14, color: pctl.color},
                         fill: 'tozeroy', fillcolor: pctl.color + '14'},
                        {x: xLabels, y: throughputs, name: `Tokens/s`, type: 'scatter', mode: 'lines+markers+text', yaxis: 'y2',
                         line: {color: '#f59e0b', width: 3, shape: 'spline'},
                         marker: {color: '#f59e0b', size: 10, symbol: 'diamond', line: {width: 2, color: 'white'}},
                         text: tputText, textposition: 'bottom center', textfont: {size: 13, color: '#f59e0b'}},
                    ];
                    if (bestIdx >= 0) {
                        traces.push({x: [xLabels[bestIdx]], y: [latencies[bestIdx]], name: 'Best EPP', type: 'scatter', mode: 'markers',
                            marker: {color: '#10b981', size: 22, symbol: 'circle', line: {width: 3, color: 'white'}}, showlegend: true});
                    }
                    // Baseline from Step 6/7 (before EPP tuning) — horizontal reference lines
                    const baseline = (eppData.baselines || {})[arch];
                    if (baseline) {
                        const blTtft = baseline[`ttft_${pctl.key}`];
                        const blTput = (baseline.structure && baseline.structure.tokens_per_second) || baseline.throughput_mean || baseline.throughput_p90;
                        if (blTtft != null) {
                            traces.push({x: xLabels, y: xLabels.map(() => blTtft), name: `Baseline TTFT (${baseline.config_name})`, type: 'scatter', mode: 'lines',
                                line: {color: '#1e40af', width: 2, dash: 'dot'},
                                showlegend: true, hovertemplate: `Baseline: ${blTtft.toFixed(0)}ms<extra></extra>`});
                        }
                        if (blTput != null) {
                            traces.push({x: xLabels, y: xLabels.map(() => blTput), name: `Baseline Throughput`, type: 'scatter', mode: 'lines', yaxis: 'y2',
                                line: {color: '#92400e', width: 2, dash: 'dot'},
                                showlegend: true, hovertemplate: `Baseline: ${blTput > 100 ? Math.round(blTput) + ' tok/s' : blTput.toFixed(1)}<extra></extra>`});
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
                        height: 850, margin: {t: 30, b: 80, l: 60, r: 60},
                        xaxis: {title: 'EPP Strategy'},
                        yaxis: {title: `TTFT ${pctl.label} (ms)`, side: 'left', titlefont: {color: pctl.color}, tickfont: {color: pctl.color}},
                        yaxis2: {title: `Tokens/s`, side: 'right', overlaying: 'y', titlefont: {color: '#f59e0b'}, tickfont: {color: '#f59e0b'}},
                        showlegend: true, legend: {x: 0, y: 1.18, orientation: 'h'}, shapes, annotations,
                    }, { responsive: true, displayModeBar: false });
                });
            });
                // E2E per workload (guidellm) — one trace per workload shape
            const eppWlCardId = `${eppCardId}-wl-e2e${_sfx}`;
            s += `<div class="chart-card" style="margin:16px 20px;">`;
            s += `<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#06b6d4);">E2E P90 per Workload (guidellm)</div>`;
            s += `<div style="padding:6px 16px; color:#64748b; font-size:0.85em; line-height:1.5;">Each workload shape's own request duration per strategy. W1 (3K-token outputs) sits ~15&times; above W3; differences between strategies per shape are the routing signal.</div>`;
            s += `<div id="${eppWlCardId}" style="height:600px; margin:8px 20px; background:#fff; border-radius:8px; border:1px solid #e2e8f0;"></div>`;
            s += `</div>`;
            _renders.push(() => {
                const wlEl = document.getElementById(eppWlCardId);
                if (!wlEl) return;
                const wlNames = [];
                trials.forEach(t => (t.workloads || []).forEach(w => { if (!wlNames.includes(w.name)) wlNames.push(w.name); }));
                const colors = ['#3b82f6', '#dc2626', '#059669', '#f59e0b', '#7c3aed'];
                const traces = wlNames.map((wlName, i) => {
                    const vals = trials.map(t => {
                        const w = (t.workloads || []).find(w => w.name === wlName);
                        return w && w.e2e_latency_p90 != null ? w.e2e_latency_p90 : null;
                    });
                    return {
                        x: trials.map(t => t.name), y: vals, name: wlName,
                        type: 'scatter', mode: 'lines+markers+text',
                        text: vals.map(v => v != null ? v.toFixed(1) + 's' : ''),
                        textposition: 'top center', textfont: {size: 11, color: colors[i % colors.length]},
                        line: {color: colors[i % colors.length], width: 3},
                        marker: {size: 9, color: colors[i % colors.length]},
                        hovertemplate: `<b>%{x}</b><br>${wlName} E2E P90: %{y:.1f}s<extra></extra>`,
                        connectgaps: true,
                    };
                }).filter(tr => tr.y.some(v => v != null));
                if (traces.length) {
                    Plotly.newPlot(wlEl, traces, {
                        height: 600, margin: {t: 30, b: 90, l: 60, r: 40},
                        xaxis: {title: 'EPP Strategy', tickangle: -25},
                        yaxis: {title: 'E2E P90 (s)', tickformat: ',.1f'},
                        showlegend: true, legend: {x: 0, y: 1.12, orientation: 'h'},
                    }, { responsive: true, displayModeBar: false });
                }
            });

        });

        // Show skipped architectures
        if (eppData.skipped_architectures && eppData.skipped_architectures.length) {
            eppData.skipped_architectures.forEach(entry => {
                const arch = typeof entry === 'string' ? entry : entry.arch;
                const reason = typeof entry === 'string' ? 'not_tested' : (entry.reason || 'not_tested');
                const archLabel = arch.toUpperCase();
                let icon = '&#9989;';
                let title = 'EPP tuning skipped';
                let desc = 'No EPP weight variations were tested for this architecture. The current EPP preset was used as-is.';
                if (reason === 'single_pod') {
                    icon = '&#128279;';
                    title = 'EPP tuning skipped — single pod';
                    desc = 'The best configuration for this architecture uses only 1 pod. EPP routing requires multiple pods to distribute requests between — with a single backend there is nothing to optimize.';
                } else if (reason === 'weights_match') {
                    icon = '&#9989;';
                    title = 'EPP tuning skipped — preset is optimal';
                    desc = 'Smart weight derivation analyzed the measured Prometheus metrics (cache hit rate, KV pressure, queue depth) and concluded the current EPP preset weights are already optimal. No test needed.';
                } else if (reason === 'no_metrics') {
                    icon = '&#9888;';
                    title = 'EPP tuning skipped — no Prometheus metrics';
                    desc = 'Prometheus/Thanos metrics were not available for this architecture. EPP weight derivation requires measured KV cache pressure, queue depth, and cache hit rate data. Check that Thanos is accessible and User Workload Monitoring is enabled.';
                }
                s += `<div class="chart-card" style="margin-top:16px; border:2px solid #7c3aed40; border-left:6px solid #7c3aed80;">`;
                s += `<div class="chart-card-header" style="background:linear-gradient(135deg,#94a3b8,#64748b);">Step 9: EPP Tuning — ${archLabel}</div>`;
                s += '<div style="padding:24px; text-align:center; color:#64748b;">';
                s += `<div style="font-size:1.5em; margin-bottom:8px;">${icon}</div>`;
                s += `<div style="font-weight:700; font-size:1em; color:#1e293b; margin-bottom:6px;">${title}</div>`;
                s += `<div style="font-size:0.88em; max-width:500px; margin:0 auto;">${desc}</div>`;
                s += '</div></div>';
            });
        }
        }

        return s;
    }

    ReportTabs.register({
        id: 'epp-tuning', label: 'EPP Tuning', icon: '&#9881;',
        position: 'after:vllm-metrics',
        build: function(data, ctx) {
            if (!data.epp_tuning || !data.epp_tuning.by_architecture) return '';
            return build(data, ctx);
        },
        render: function(data, ctx) {
            if (typeof Plotly === 'undefined') return;
            _renders.forEach(function(fn) { try { fn(); } catch (e) { console.warn('epp render failed', e); } });
        }
    });
})();
