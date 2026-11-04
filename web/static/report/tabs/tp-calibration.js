// tabs/tp-calibration.js — TP Calibration report tab. One implementation for live + offline.
(function() {

    function fmtSI(v, d) {
        if (v == null) return '-';
        d = d != null ? d : 1;
        if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(d) + 'M';
        if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(d) + 'K';
        return v.toFixed(d);
    }

    function chartCard(title, desc, id) {
        return '<div class="chart-card"><div class="chart-card-header">' + title + '</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' + desc + '</div>' +
            '<div class="chart-card-body"><div id="' + id + '" style="width:100%;height:850px;"></div></div></div>';
    }

    function build(data, ctx) {
        var rec = data.recommendation;
        if (!rec) return '';
        var hasDecodeTP = rec.decode_tp_all && rec.decode_tp_all.length;
        var hasPrefillTP = rec.prefill_tp_all && rec.prefill_tp_all.length;
        if (!hasDecodeTP && !hasPrefillTP) return '';

        var _sfx = (ctx.mode === 'live' && typeof _chartSuffix !== 'undefined') ? _chartSuffix : '';
        var s = '';
        s += '<div class="chart-card" style="border-left:6px solid #0d9488;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#0d9488,#14b8a6); color:white;">TP Calibration</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Tests different <strong>Tensor Parallelism (TP)</strong> values per role to find the minimum viable TP that fits in GPU memory. ' +
            'Lower TP means fewer GPUs per pod, allowing more replicas and higher throughput. Each TP value is tested with a short benchmark to measure baseline latency and token throughput.</div></div>';
        s += '<div class="charts-grid-2col">';
        s += chartCard('Step 2: Decode TP Sweep',
            hasDecodeTP
                ? 'Each TP value was tested with a single decode pod to find the optimal tensor parallelism for decode. The <strong>tokens/s/GPU</strong> metric (bars) shows efficiency — higher is better. The <strong style="color:#ef4444">ITL P90</strong> line shows inter-token latency — lower means smoother streaming.'
                : 'Decode TP calibration chart will appear here once Step 2 completes.',
            ctx.chartId('chart-tp-decode'));
        s += chartCard('Step 3: Prefill TP Sweep',
            hasPrefillTP
                ? 'Each TP value was tested with a single prefill pod to find the optimal tensor parallelism for prefill. The <strong>tokens/s/GPU</strong> metric (bars) shows efficiency — higher is better. The <strong style="color:#ef4444">TTFT P90</strong> line shows time-to-first-token — lower means the model starts responding faster.'
                : 'Prefill TP calibration chart will appear here once Step 3 completes.',
            ctx.chartId('chart-tp-prefill'));
        s += '</div>';
        s += chartCard('TP Calibration: Latency vs GPU Count', 'Combined prefill and decode TP calibration results. TPSG bars show throughput per GPU; the dashed line shows TTFT P90 latency on a log scale.', ctx.chartId('chart-pareto'));

        // Calibration parameters table
        var calRows = [];
        if (hasDecodeTP) {
            rec.decode_tp_all.forEach(function(d) {
                calRows.push({ role: 'Decode', tp: d.tp, isl: d.cal_isl, osl: d.cal_osl, conc: d.cal_concurrency, reqs: d.cal_max_requests, tspg: d.tpsg != null ? d.tpsg : null, ttft: null, itl: d.itl_p90 != null ? d.itl_p90 : null });
            });
        }
        if (hasPrefillTP) {
            rec.prefill_tp_all.forEach(function(d) {
                calRows.push({ role: 'Prefill', tp: d.tp, isl: d.cal_isl, osl: d.cal_osl, conc: d.cal_concurrency, reqs: d.cal_max_requests, tspg: d.tpsg != null ? d.tpsg : null, ttft: d.ttft_p90 != null ? d.ttft_p90 : null, itl: null });
            });
        }
        if (calRows.length) {
            s += '<div style="margin:16px 0;padding:16px 20px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;">';
            s += '<div style="font-weight:700;font-size:0.95em;color:#1e293b;margin-bottom:8px;">Calibration Test Parameters</div>';
            s += '<p style="color:#64748b;font-size:0.82em;margin:0 0 10px;">Each TP value is tested with isolated workloads: <strong>ISL=1</strong> for decode (measures pure token generation) and <strong>OSL=1</strong> for prefill (measures pure prompt processing).</p>';
            s += '<table style="width:100%;font-size:0.85em;text-align:center;"><tr><th>Role</th><th>TP</th><th>ISL</th><th>OSL</th><th>Concurrency</th><th>Max Requests</th><th>TSPG (tok/s/GPU)</th><th>ITL P90</th><th>TTFT P90</th></tr>';
            calRows.forEach(function(r) {
                var cls = r.role === 'Decode' ? ' style="color:#6366f1;text-align:center;"' : ' style="color:#0d9488;text-align:center;"';
                s += '<tr><td' + cls + '><strong>' + r.role + '</strong></td><td>' + r.tp + '</td><td>' + (r.isl != null ? r.isl.toLocaleString() : '-') + '</td><td>' + (r.osl != null ? r.osl.toLocaleString() : '-') + '</td><td>' + (r.conc || '-') + '</td><td>' + (r.reqs || '-') + '</td><td>' + (r.tspg != null ? r.tspg.toLocaleString() : '-') + '</td><td>' + (r.itl != null ? r.itl.toFixed(2) + ' ms' : '-') + '</td><td>' + (r.ttft != null ? r.ttft.toLocaleString() + ' ms' : '-') + '</td></tr>';
            });
            s += '</table></div>';
        }
        return s;
    }

    var LAYOUT = { margin: { t: 10, b: 40, l: 50, r: 20 }, height: 850, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent' };
    var CONFIG = { responsive: true, displayModeBar: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'] };

    function plot(ctx, id, traces, layout) {
        var el = document.getElementById(ctx.chartId(id));
        if (el) Plotly.newPlot(el, traces, layout, CONFIG);
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var rec = data.recommendation;
        if (!rec) return;
        var charts = data.charts || {};

        // Decode TP Sweep
        if (rec.decode_tp_all && rec.decode_tp_all.length) {
            var dtp = rec.decode_tp_all;
            var tpLabels = dtp.map(function(d) { return 'TP=' + d.tp; });
            var tpsgVals = dtp.map(function(d) { return d.tpsg; });
            var bestTpsg = Math.max.apply(null, tpsgVals);
            var barColors = tpsgVals.map(function(v) { return v === bestTpsg ? '#10b981' : '#6366f1'; });
            var itlVals = dtp.map(function(d) { return d.itl_p90 != null ? d.itl_p90 : 0; });
            var traces = [
                { x: tpLabels, y: tpsgVals, name: 'Tokens/s/GPU', type: 'bar', marker: { color: barColors },
                  text: tpsgVals.map(function(v) { return fmtSI(v); }), textposition: 'outside',
                  textfont: { size: 14, color: '#1e293b' }, cliponaxis: false, constraintext: 'none',
                  hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>' }
            ];
            if (itlVals.some(function(v) { return v > 0; })) {
                traces.push({
                    x: tpLabels, y: itlVals, name: 'ITL P90 (ms)', type: 'scatter', mode: 'lines+markers+text', yaxis: 'y2',
                    line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'circle', color: '#ef4444', line: { width: 2, color: 'white' } },
                    text: itlVals.map(function(v, idx) { return dtp[idx].itl_p90 != null ? v.toFixed(1) : ''; }),
                    textposition: 'bottom center', textfont: { size: 14, color: '#ef4444' },
                    hovertemplate: '<b>%{x}</b><br>ITL P90: %{y:.2f} ms<extra></extra>'
                });
            }
            plot(ctx, 'chart-tp-decode', traces, Object.assign({}, LAYOUT, {
                margin: Object.assign({}, LAYOUT.margin, { r: 60 }),
                barmode: 'group', showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' },
                yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left', tickformat: '.2s' },
                yaxis2: { title: 'ITL P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } }
            }));
        }

        // Prefill TP Sweep
        if (rec.prefill_tp_all && rec.prefill_tp_all.length) {
            var ptp = rec.prefill_tp_all;
            var tpLabels = ptp.map(function(d) { return 'TP=' + d.tp; });
            var tpsgVals = ptp.map(function(d) { return d.tpsg; });
            var bestTpsg = Math.max.apply(null, tpsgVals);
            var barColors = tpsgVals.map(function(v) { return v === bestTpsg ? '#10b981' : '#6366f1'; });
            var ttftVals = ptp.map(function(d) { return d.ttft_p90 != null ? d.ttft_p90 : 0; });
            var traces = [
                { x: tpLabels, y: tpsgVals, name: 'Tokens/s/GPU', type: 'bar', marker: { color: barColors },
                  text: tpsgVals.map(function(v) { return fmtSI(v); }), textposition: 'outside',
                  textfont: { size: 14, color: '#1e293b' }, cliponaxis: false, constraintext: 'none',
                  hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>' }
            ];
            if (ttftVals.some(function(v) { return v > 0; })) {
                traces.push({
                    x: tpLabels, y: ttftVals, name: 'TTFT P90 (ms)', type: 'scatter', mode: 'lines+markers+text', yaxis: 'y2',
                    line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'circle', color: '#ef4444', line: { width: 2, color: 'white' } },
                    text: ttftVals.map(function(v, idx) { return ptp[idx].ttft_p90 != null ? Math.round(v).toLocaleString() : ''; }),
                    textposition: 'bottom center', textfont: { size: 14, color: '#ef4444' },
                    hovertemplate: '<b>%{x}</b><br>TTFT P90: %{y:.1f} ms<extra></extra>'
                });
            }
            plot(ctx, 'chart-tp-prefill', traces, Object.assign({}, LAYOUT, {
                margin: Object.assign({}, LAYOUT.margin, { r: 60 }),
                barmode: 'group', showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' },
                yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left', tickformat: '.2s' },
                yaxis2: { title: 'TTFT P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } }
            }));
        }

        // TP Calibration Pareto (TPSG bars + TTFT line)
        if (charts.pareto && charts.pareto.traces && charts.pareto.traces.length) {
            var tpsgLookup = {};
            (rec.decode_tp_all || []).forEach(function(d) { tpsgLookup['decode-' + d.tp] = d.tpsg; });
            (rec.prefill_tp_all || []).forEach(function(d) { tpsgLookup['prefill-' + d.tp] = d.tpsg; });
            var traces = [];
            var roleColors = { decode: '#3b82f6', prefill: '#f59e0b' };
            charts.pareto.traces.forEach(function(t) {
                if (t.yaxis === 'y2') return;
                var role = t.name.toLowerCase().indexOf('decode') >= 0 ? 'decode' : 'prefill';
                var color = roleColors[role];
                var tpsgVals = t.x.map(function(tp) { return tpsgLookup[role + '-' + tp] || 0; });
                traces.push({
                    x: t.x, y: tpsgVals, name: role.charAt(0).toUpperCase() + role.slice(1) + ' TPSG',
                    type: 'bar', marker: { color: color },
                    text: tpsgVals.map(function(v) { return v > 0 ? Math.round(v).toLocaleString() : ''; }),
                    textposition: 'outside', textfont: { size: 13, color: color },
                    cliponaxis: false, constraintext: 'none',
                    hovertemplate: '<b>TP=%{x}</b><br>TPSG: %{y:,.0f} tok/s/GPU<extra></extra>'
                });
                traces.push({
                    x: t.x, y: t.y, name: role.charAt(0).toUpperCase() + role.slice(1) + ' TTFT', yaxis: 'y2',
                    mode: 'markers+lines+text', text: t.text,
                    customdata: t.y.map(function(v) { return v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms'; }),
                    texttemplate: '%{customdata}',
                    textposition: 'top center', textfont: { size: 13, color: '#1e293b' },
                    marker: { size: 10, color: color, symbol: 'circle', line: { width: 2, color: 'white' } },
                    line: { width: 2, dash: 'dash', color: color },
                    hovertemplate: '<b>%{text}</b><extra></extra>'
                });
            });
            var paretoXvals = [];
            charts.pareto.traces.filter(function(t) { return t.yaxis !== 'y2'; }).forEach(function(t) {
                t.x.forEach(function(v) { if (paretoXvals.indexOf(v) < 0) paretoXvals.push(v); });
            });
            paretoXvals.sort(function(a, b) { return a - b; });
            plot(ctx, 'chart-pareto', traces, Object.assign({}, LAYOUT, {
                margin: Object.assign({}, LAYOUT.margin, { r: 60, t: 30 }),
                barmode: 'group',
                xaxis: { title: 'Total GPUs (= TP)', tickvals: paretoXvals },
                yaxis: { title: 'TPSG (tok/s/GPU) — higher is better', side: 'left', tickformat: ',.0f' },
                yaxis2: { title: 'TTFT P90 (ms) — lower is better', side: 'right', overlaying: 'y', type: 'log' },
                showlegend: true
            }));
        }
    }

    ReportTabs.register({
        id: 'tp-calibration',
        label: 'TP Calibration',
        icon: '&#9881;',
        position: 'after:configurations',
        build: build,
        render: render
    });
})();
