// tabs/concurrency-sweep.js — Concurrency Sweep / Calibrated Load report tab.
// One implementation for live + offline reports.
(function() {
    var _renders = [];
    function build(data, ctx) {
        var s = '';
        _renders = [];
        var charts = data.charts || {};
        var rec = data.recommendation || {};
    if (data.calibrated_qps || data.concurrency_sweep) {
        s += '<div class="chart-card" style="border-left:6px solid #059669;">' +
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

        s += '<div class="chart-card" style="margin-top:16px; border:2px solid #059669; border-left:6px solid #059669;"><div class="chart-card-header" style="background:linear-gradient(135deg,#059669,#10b981);">Step 11: Concurrency Sweep</div>';
        // Capacity info with math breakdown
        if (cal.gpu_sizing) {
            const s = cal.gpu_sizing;
            s += '<div style="padding:12px 20px; background:#ecfdf5; border-bottom:1px solid #6ee7b7; font-size:0.9em; color:#065f46;">';
            s += `<div style="font-weight:700; margin-bottom:8px;">📊 Cluster Capacity Analysis</div>`;
            s += '<table style="width:auto; margin:0; font-size:0.95em; border:none;">';
            s += '<tr style="background:none;"><td style="border:none; padding:2px 16px 2px 0; color:#047857;"><strong>GPU Cost per Request</strong></td><td style="border:none; padding:2px 0;"></td></tr>';
            s += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Prefill</td><td style="border:none; padding:1px 0;">${s.isl} ISL ÷ ${s.prefill_tpsg} TPSG = <strong>${s.prefill_cost} GPU-sec</strong></td></tr>`;
            s += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Decode</td><td style="border:none; padding:1px 0;">${s.osl} OSL ÷ ${s.decode_tpsg} TPSG = <strong>${s.decode_cost} GPU-sec</strong></td></tr>`;
            s += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Total</td><td style="border:none; padding:1px 0;"><strong>${s.total_cost} GPU-sec/request</strong></td></tr>`;
            s += '<tr style="background:none;"><td style="border:none; padding:6px 16px 2px 0; color:#047857;"><strong>Sustainable Throughput</strong></td><td style="border:none; padding:6px 0 2px;"></td></tr>';
            s += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Cluster capacity</td><td style="border:none; padding:1px 0;">${s.total_gpus} GPUs ÷ ${s.total_cost} GPU-sec ÷ ${s.headroom}x headroom = <strong>${s.sustainable_throughput_rps || s.sustainable_qps} req/s</strong> (${s.sustainable_concurrency || '?'} concurrent users)</td></tr>`;
            s += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Concurrency tested</td><td style="border:none; padding:1px 0;"><strong>${s.concurrency} simultaneous requests</strong></td></tr>`;
            s += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Ideal P/D ratio</td><td style="border:none; padding:1px 0;"><strong>${s.ideal_prefill_pct}% prefill</strong></td></tr>`;
            s += '</table></div>';
        } else if (cal.total_gpus_available && cal.requested_rps) {
            s += `<div style="padding:10px 20px; background:#ecfdf5; border-bottom:1px solid #6ee7b7; font-size:0.9em; color:#065f46;">📊 Cluster can sustain <strong>${cal.requested_rps} req/s</strong> with <strong>${cal.total_gpus_available} GPUs</strong>.</div>`;
        }
        s += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Steps 7-8 ran at the original user-requested concurrency which exceeded cluster capacity. This step re-tested the best configurations at a sustainable load to show realistic latency and throughput.</div>';

        // --- Load Analysis per architecture ---
        if (cal.calibration_analysis) {
            const ca = cal.calibration_analysis;
            s += '<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b; margin-top:8px;">Load Analysis (from measured Step 7 data)</div>';
            s += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            s += '<tr><th>Architecture</th><th>Throughput</th><th>Response Time</th><th>Service Time</th><th>Queue Time</th><th>Utilization</th><th>Calibrated Load</th></tr>';
            ['pd', 'aggregated'].forEach(arch => {
                const a = ca[arch];
                if (!a) return;
                const label = arch === 'pd' ? 'PD' : 'Aggregated';
                const queuePct = a.queue_pct || 0;
                const queueColor = queuePct > 80 ? '#dc2626' : (queuePct > 50 ? '#d97706' : '#059669');
                s += `<tr>`;
                s += `<td style="font-weight:700;">${label}</td>`;
                s += `<td>${a.throughput_mean} req/s <span style="color:#94a3b8;">@ c=${a.concurrency_tested}</span></td>`;
                s += `<td>${(a.response_time_s * 1000).toFixed(0)}ms</td>`;
                s += `<td>${a.service_time_ms.toFixed(0)}ms</td>`;
                s += `<td style="color:${queueColor}; font-weight:600;">${a.queue_time_ms.toFixed(0)}ms (${queuePct}%)</td>`;
                s += `<td>${a.utilization_pct}%</td>`;
                s += `<td style="font-weight:700; color:#059669;">${a.calibrated_concurrency} users</td>`;
                s += `</tr>`;
            });
            s += '</table></div>';
            s += '<div style="padding:4px 20px 12px; font-size:0.82em; color:#64748b;">At the tested concurrency, most time is spent in queue — requests wait for GPU availability rather than being processed. The calibrated load reduces concurrency until queue time is reasonable (~2× service time), showing realistic per-request latency.</div>';
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
        s += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b;">Latency Breakdown${rpsLabel}</div>`;
        s += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
        s += '<tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';
        calEntries.forEach(({label, entry}, idx) => {
            const metrics = [
                {name: 'TTFT (ms)', p50: entry.ttft_p50, p90: entry.ttft_p90, p95: entry.ttft_p95, p99: entry.ttft_p99, best: bestTtft, p90key: 'ttft_p90'},
                {name: 'ITL (ms)', p50: entry.itl_p50, p90: entry.itl_p90, p95: entry.itl_p95, p99: entry.itl_p99, best: bestItl, p90key: 'itl_p90'},
            ];
            metrics.forEach((m, mi) => {
                const borderStyle = mi === 0 && idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
                const rowspan = mi === 0 ? ` rowspan="2" style="vertical-align:middle; font-weight:700;${borderStyle}"` : '';
                s += '<tr>';
                if (mi === 0) s += `<td${rowspan}>${label}</td>`;
                s += `<td style="color:#64748b;${borderStyle}">${m.name}</td>`;
                s += `<td style="${borderStyle}">${m.p50 != null ? m.p50 : '-'}</td>`;
                s += `<td style="${hl(entry[m.p90key], m.best)}${borderStyle}">${m.p90 != null ? m.p90 : '-'}</td>`;
                s += `<td style="${borderStyle}">${m.p95 != null ? m.p95 : '-'}</td>`;
                s += `<td style="${borderStyle}">${m.p99 != null ? m.p99 : '-'}</td>`;
                s += '</tr>';
            });
        });
        s += '</table></div>';

        // --- Table 1b: Throughput (mean only) ---
        const bestTputMean = Math.max(...calEntries.map(e => e.entry.throughput_mean || e.entry.throughput_p50 || 0));
        s += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b; margin-top:8px;">Throughput${rpsLabel}</div>`;
        s += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
        s += '<tr><th>Configuration</th><th>Throughput Mean (req/s)</th></tr>';
        calEntries.forEach(({label, entry}, idx) => {
            const tputMean = entry.throughput_mean || entry.throughput_p50 || '-';
            const isBest = tputMean === bestTputMean;
            const style = isBest ? 'color:#059669; font-weight:700;' : '';
            const border = idx > 0 ? ' border-top:1px solid #e2e8f0;' : '';
            s += `<tr><td style="font-weight:700;${border}">${label}</td><td style="${style}${border}">${tputMean} req/s</td></tr>`;
        });
        s += '</table></div>';

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
            s += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b; margin-top:12px;">Overload Impact: ${group.title}</div>`;
            s += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            s += '<tr><th>Configuration</th><th>Load</th><th>TTFT P90</th><th>Throughput Mean</th></tr>';
            group.rows.forEach(function(row) {
                var calTputMean = row.cal.throughput_mean || row.cal.throughput_p50 || '-';
                var overTputMean = row.over.throughput_mean || row.over.throughput_p90 || '-';
                s += `<tr><td><strong>${row.label} (calibrated)</strong></td><td>${calConcurrency}</td><td style="color:#059669; font-weight:700;">${row.cal.ttft_p90} ms</td><td style="color:#059669; font-weight:700;">${calTputMean} req/s</td></tr>`;
                s += `<tr><td><strong>${row.label} (overloaded)</strong></td><td>${origConcurrency}</td><td style="color:#94a3b8;">${row.over.ttft_p90} ms</td><td style="color:#94a3b8;">${overTputMean} req/s</td></tr>`;
            });
            s += '</table></div>';
        });
        s += '</div>';
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
                s += '<div class="chart-card" style="margin-top:16px;">';
                s += '<div class="chart-card-header">' + pct.title + '</div>';
                s += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">' + pct.label + ' across all tested configurations. Each line is a different deployment config. Lower is better.</div>';
                s += '<div id="' + chartId + '" style="width:100%; height:850px;"></div>';
                s += '</div>';

                _renders.push(function() {
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
            s += '<div class="chart-card" style="margin-top:16px;">';
            s += '<div class="chart-card-header">Throughput per GPU vs Concurrency</div>';
            s += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">Token throughput per GPU across all configs. Each line is a different deployment config. Higher is better.</div>';
            s += '<div id="chart-sweep-tput-all" style="width:100%; height:850px;"></div>';
            s += '</div>';

            _renders.push(function() {
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
            s += '<div class="chart-card" style="margin-top:16px;">';
            s += '<div class="chart-card-header">vLLM Token Throughput vs Concurrency</div>';
            s += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">' +
                '<strong>Solid line:</strong> Server total (prompt + generation from Prometheus). ' +
                '<strong>Dashed line:</strong> Output tokens only (generation). ' +
                'The gap reflects prefix cache efficiency — cached prompt tokens inflate the server total.</div>';
            s += '<div id="chart-sweep-vllm-tps" style="width:100%; height:850px;"></div>';
            s += '</div>';

            _renders.push(function() {
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
            s += '<div class="chart-card" style="margin-top:16px;">';
            s += '<div class="chart-card-header">Token Throughput — GPU Computed vs Server Total vs Client</div>';
            s += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">' +
                '<strong>GPU computed:</strong> Prompt tokens actually computed (excludes cached) + generation. ' +
                '<strong>Server total:</strong> All tokens processed by vLLM (incl. cached prefixes). ' +
                '<strong>Client:</strong> Tokens measured by the load tester.</div>';
            s += '<div id="chart-sweep-token-tput" style="width:100%; height:850px;"></div>';
            s += '</div>';

            _renders.push(function() {
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
            s += '<div class="chart-card" style="margin-top:20px;">';
            s += '<div class="chart-card-header">Prefix Cache Hit Rate vs Concurrency</div>';
            s += '<div style="padding:8px 20px 0;color:#1e293b;font-size:0.92em;">How prefix cache hit rate changes with load. Higher hit rates mean more requests served from cached KV data.</div>';
            s += '<div id="chart-sweep-cache-hit' + _sfx + '" style="width:100%;height:850px;"></div></div>';
            _renders.push(function() {
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
                Plotly.newPlot('chart-sweep-cache-hit' + _sfx, traces, {
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
        s += '<div class="chart-card" style="margin-top:20px;"><div class="chart-card-header">Pareto — Throughput vs Interactivity</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Trade-off between per-user token speed (X) and GPU throughput efficiency (Y) across concurrency levels. Top-right is ideal.</div>' +
            '<div class="chart-card-body"><div id="chart-pareto-sweep-interactivity' + _sfx + '" style="width:100%;height:850px;"></div></div></div>';

        s += '<div class="chart-card" style="margin-top:16px;"><div class="chart-card-header">Pareto — Throughput vs TTFT</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Trade-off between time to first token (X, lower/right is better) and GPU throughput efficiency (Y). Top-right is ideal.</div>' +
            '<div class="chart-card-body"><div id="chart-pareto-sweep-ttft' + _sfx + '" style="width:100%;height:850px;"></div></div></div>';

        // --- Architecture Comparison Charts ---
        s += '<div class="chart-card" style="margin-top:20px;"><div class="chart-card-header">Best Envelope — PD vs Aggregated</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Pareto frontier of each architecture\'s best throughput/GPU at each concurrency level. The higher curve is the better architecture.</div>' +
            '<div class="chart-card-body"><div id="chart-sweep-envelope' + _sfx + '" style="width:100%;height:850px;"></div></div></div>';

        s += '<div class="chart-card" style="margin-top:16px;"><div class="chart-card-header">Architecture Comparison — Overlaid</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Combined view: frontier lines for each architecture plus markers showing which won at each concurrency level.</div>' +
            '<div class="chart-card-body"><div id="chart-sweep-overlaid' + _sfx + '" style="width:100%;height:850px;"></div></div></div>';

        // --- vLLM Engine Metrics per config ---
        s += '<div class="chart-card" style="margin-top:16px;"><div class="chart-card-header">vLLM Engine Metrics vs Concurrency</div>';
        s += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">KV cache usage and running requests across concurrency levels per config.</div>';
        s += '<div id="chart-sweep-engine' + _sfx + '" style="width:100%;height:850px;"></div></div>';

        _renders.push(function() {
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
            Plotly.newPlot('chart-sweep-engine' + _sfx, traces, {
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
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #64748b;">';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#475569,#64748b); color:white; font-size:1.1em;">Concurrency Sweep Data</div>';
        s += '<div style="padding:8px 16px;display:flex;gap:16px;font-size:0.8em;color:#64748b;border-bottom:1px solid #e2e8f0;">';
        s += '<span><span style="display:inline-block;width:10px;height:10px;background:#ecfdf5;border:1px solid #059669;border-radius:2px;vertical-align:middle;margin-right:2px;"></span> Standout TTFT P99 improvement vs other architecture</span>';
        s += '</div>';
        var csSweepTblId = 'concurrency-sweep-tbl-' + runId;
        s += '<div class="chart-card-body" style="padding:0;"><table class="results-table" id="' + csSweepTblId + '">';
        s += '<tr>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',0,\'str\')">Architecture &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',1,\'num\')">Users &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',2,\'num\')">TTFT P50 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',3,\'num\')">TTFT P90 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',4,\'num\')">TTFT P95 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',5,\'num\')">TTFT P99 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',6,\'num\')">Throughput &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',7,\'num\')">tok/s/user &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csSweepTblId + '\',8,\'num\')">tok/s/gpu &#x21C5;</th>';
        s += '</tr>';

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
                s += '<tr' + rowStyle + '>';
                s += '<td>' + archLabel + '</td>';
                s += '<td>' + p.concurrency + calBadge + '</td>';
                s += '<td>' + (p.ttft_p50 ? p.ttft_p50.toFixed(0) : '-') + ' ms</td>';
                s += '<td>' + (p.ttft_p90 ? p.ttft_p90.toFixed(0) : '-') + ' ms</td>';
                s += '<td>' + (p.ttft_p95 ? p.ttft_p95.toFixed(0) : '-') + ' ms</td>';
                s += '<td>' + (p.ttft_p99 ? p.ttft_p99.toFixed(0) : '-') + ' ms</td>';
                s += '<td>' + (p.throughput_mean ? p.throughput_mean.toFixed(1) : '-') + ' req/s</td>';
                s += '<td>' + (p.interactivity ? p.interactivity.toFixed(1) : '-') + '</td>';
                s += '<td>' + (p.throughput_per_gpu ? p.throughput_per_gpu.toFixed(0) : '-') + '</td>';
                s += '</tr>';
            });
        });
        s += '</table></div></div>';
    }

    // ============================================================
    // Flush concurrency sweep

        return s;
    }
    ReportTabs.register({
        id: 'calibrated-load', label: 'Concurrency Sweep', icon: '&#9878;',
        position: 'after:latency-search',
        build: build,
        render: function(data, ctx) {
            if (typeof Plotly === 'undefined') return;
            _renders.forEach(function(fn) { try { fn(); } catch (e) { console.warn('concurrency-sweep render failed', e); } });
        }
    });
})();
