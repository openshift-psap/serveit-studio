// tabs/comparison.js — Architecture Comparison report tab. One implementation for live + offline.
(function() {

    function chartCard(title, desc, id) {
        return '<div class="chart-card"><div class="chart-card-header">' + title + '</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' + desc + '</div>' +
            '<div class="chart-card-body"><div id="' + id + '" style="width:100%;height:850px;"></div></div></div>';
    }

    function coreResults(data) {
        return (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || r.config_name || '';
            return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0;
        });
    }

    function build(data, ctx) {
        var rec = data.recommendation;
        if (!rec) return '';
        var cr = coreResults(data);
        var s = '';

        s += '<div class="chart-card" style="border-left:6px solid #0284c7;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#0284c7,#38bdf8); color:white;">Architecture Comparison</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Compares <strong>PD/EP disaggregated</strong> inference against the <strong>Aggregated</strong> baseline. ' +
            'PD separates prefill and decode into specialized pods so new requests don\'t wait behind ongoing generation.</div></div>';
        s += chartCard('Architecture Comparison', 'Side-by-side comparison of <strong>Aggregated</strong> vs <strong>PD</strong> architectures.', ctx.chartId('chart-arch'));
        s += chartCard('Percentile Comparison: Winner vs Aggregated', 'TTFT and Throughput at each percentile (P50, P90, P95, P99) between the recommended configuration and the Aggregated baseline.', ctx.chartId('chart-percentile-bars'));

        // PD vs Aggregated table
        if (rec.pd_vs_agg) {
            var cmp = rec.pd_vs_agg;
            var ttftColor = cmp.ttft_winner === 'PD' ? '#10b981' : '#f59e0b';
            var tputColor = cmp.throughput_winner === 'PD' ? '#10b981' : '#f59e0b';
            s += '<div class="chart-card" style="margin-top:16px; border:2px solid #6366f1; border-left:6px solid #6366f1;"><div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);">Step 8: PD vs Aggregated Comparison</div>';
            s += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">The best PD configuration was tested head-to-head against an equivalent Aggregated deployment using the same GPU count and full workload.</div>';
            s += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            s += '<tr><th>Metric</th><th>PD (best)</th><th>Aggregated</th><th>Winner</th></tr>';
            s += '<tr><td><strong>TTFT P90</strong></td><td>' + cmp.pd.ttft_p90 + ' ms</td><td>' + cmp.aggregated.ttft_p90 + ' ms</td><td style="color:' + ttftColor + '; font-weight:700;">' + cmp.ttft_winner + ' (' + cmp.ttft_diff_pct + '% better)</td></tr>';
            s += '<tr><td><strong>Throughput Mean</strong></td><td>' + (cmp.pd.throughput_mean || cmp.pd.throughput_p90) + ' req/s</td><td>' + (cmp.aggregated.throughput_mean || cmp.aggregated.throughput_p90) + ' req/s</td><td style="color:' + tputColor + '; font-weight:700;">' + cmp.throughput_winner + ' (' + cmp.throughput_diff_pct + '% better)</td></tr>';
            if (cmp.pd.ttft_p99 && cmp.aggregated.ttft_p99) {
                var p99Color = cmp.ttft_p99_winner === 'PD' ? '#10b981' : '#f59e0b';
                s += '<tr style="border-top:2px solid #e2e8f0;"><td><strong>TTFT P99 (tail)</strong></td><td>' + cmp.pd.ttft_p99 + ' ms</td><td>' + cmp.aggregated.ttft_p99 + ' ms</td><td style="color:' + p99Color + '; font-weight:700;">' + cmp.ttft_p99_winner + ' (' + cmp.ttft_p99_diff_pct + '% better)</td></tr>';
            }
            s += '</table></div>';

            // All PD configs vs baseline table
            if (rec.aggregated_baseline && cr.length > 1) {
                var bl = rec.aggregated_baseline;
                var configs = cr.filter(function(r) {
                    if (r.architecture !== 'PD' || !r.ttft_p90 || !r.throughput_p90) return false;
                    var tid = r.test_id || '';
                    return tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
                });
                if (configs.length && bl.ttft_p90 && bl.throughput_p90) {
                    var tid2 = 'cmp-pd-vs-agg';
                    s += '<div style="padding:16px 20px 4px;"><div style="font-weight:700;font-size:0.95em;color:#1e293b;margin-bottom:4px;">All PD Configurations vs Aggregated Baseline</div>';
                    s += '<table id="' + tid2 + '"><tr>';
                    s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tid2 + '\',0,\'str\')">Configuration &#x21C5;</th>';
                    s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tid2 + '\',1,\'num\')">TTFT P90 &#x21C5;</th>';
                    s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tid2 + '\',2,\'num\')">TTFT vs Agg &#x21C5;</th>';
                    s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tid2 + '\',3,\'num\')">Throughput P90 &#x21C5;</th>';
                    s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tid2 + '\',4,\'num\')">Tput vs Agg &#x21C5;</th></tr>';
                    configs.slice().sort(function(a, b) { return a.ttft_p90 - b.ttft_p90; }).forEach(function(cfg) {
                        var tDiff = ((cfg.ttft_p90 - bl.ttft_p90) / bl.ttft_p90 * 100);
                        var pDiff = ((cfg.throughput_p90 - bl.throughput_p90) / bl.throughput_p90 * 100);
                        var tColor = tDiff < 0 ? '#059669' : '#dc2626';
                        var pColor = pDiff > 0 ? '#059669' : '#dc2626';
                        s += '<tr><td><strong>' + cfg.config_name + '</strong></td><td data-val="' + cfg.ttft_p90 + '">' + cfg.ttft_p90 + ' ms</td><td data-val="' + tDiff.toFixed(1) + '" style="color:' + tColor + ';font-weight:700;">' + (tDiff > 0 ? '+' : '') + tDiff.toFixed(1) + '%</td><td data-val="' + cfg.throughput_p90 + '">' + cfg.throughput_p90 + ' req/s</td><td data-val="' + pDiff.toFixed(1) + '" style="color:' + pColor + ';font-weight:700;">' + (pDiff > 0 ? '+' : '') + pDiff.toFixed(1) + '%</td></tr>';
                    });
                    s += '</table></div>';
                }
            }
            s += '</div>';
        }

        // EP vs Aggregated table
        if (rec.ep_vs_agg) {
            var cmp = rec.ep_vs_agg;
            var ttftColor = cmp.ttft_winner === 'EP' ? '#10b981' : '#f59e0b';
            var tputColor = cmp.throughput_winner === 'EP' ? '#10b981' : '#f59e0b';
            s += '<div class="chart-card" style="margin-top:16px; border:2px solid #10b981; border-left:6px solid #10b981;"><div class="chart-card-header" style="background:linear-gradient(135deg,#10b981,#34d399);">Step 8: EP vs Aggregated Comparison</div>';
            s += '<table><tr><th>Metric</th><th>EP (best)</th><th>Aggregated</th><th>Winner</th></tr>';
            s += '<tr><td><strong>TTFT P90</strong></td><td>' + cmp.ep.ttft_p90 + ' ms</td><td>' + cmp.aggregated.ttft_p90 + ' ms</td><td style="color:' + ttftColor + ';font-weight:700;">' + cmp.ttft_winner + ' (' + cmp.ttft_diff_pct + '% better)</td></tr>';
            s += '<tr><td><strong>Throughput Mean</strong></td><td>' + (cmp.ep.throughput_mean || cmp.ep.throughput_p90) + ' req/s</td><td>' + (cmp.aggregated.throughput_mean || cmp.aggregated.throughput_p90) + ' req/s</td><td style="color:' + tputColor + ';font-weight:700;">' + cmp.throughput_winner + ' (' + cmp.throughput_diff_pct + '% better)</td></tr>';
            s += '</table></div>';
        }

        return s;
    }

    var LAYOUT = { margin: { t: 30, b: 40, l: 50, r: 20 }, height: 850, showlegend: true, plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff' };

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var rec = data.recommendation;
        if (!rec) return;
        var cr = coreResults(data);

        // Percentile Comparison: Winner vs Aggregated (ported from charts.js)
        var pEl = document.getElementById(ctx.chartId('chart-percentile-bars'));
        if (pEl && rec.recommendations) {
            var primaryKey = rec.goal === 'ttft' ? 'response_time' : 'throughput';
            var winner = rec.recommendations[primaryKey];
            var aggBase = rec.aggregated_baseline;
            if (winner && winner.config && winner.config.percentiles && aggBase && aggBase.percentiles) {
                var pp = winner.config.percentiles;
                var ap = aggBase.percentiles;
                var pcts = ['p50', 'p90', 'p95', 'p99'];
                var ttftTraces = [
                    { x: pcts, y: pcts.map(function(p) { return pp.ttft[p]; }), name: 'Winner TTFT', type: 'bar', marker: { color: '#10b981' } },
                    { x: pcts, y: pcts.map(function(p) { return ap.ttft[p]; }), name: 'Aggregated TTFT', type: 'bar', marker: { color: '#f59e0b' } }
                ];
                var tputTraces = [
                    { x: pcts, y: pcts.map(function(p) { return pp.throughput[p]; }), name: 'Winner Throughput', type: 'bar', marker: { color: '#06b6d4', xaxis: 'x2', yaxis: 'y2' } },
                    { x: pcts, y: pcts.map(function(p) { return ap.throughput[p]; }), name: 'Aggregated Throughput', type: 'bar', marker: { color: '#8b5cf6', xaxis: 'x2', yaxis: 'y2' } }
                ];
                Plotly.newPlot(pEl, ttftTraces.concat(tputTraces), {
                    barmode: 'group',
                    xaxis: { title: 'Percentile', domain: [0, 0.45] },
                    yaxis: { title: 'TTFT (ms) — lower is better' },
                    xaxis2: { title: 'Percentile', domain: [0.55, 1], anchor: 'y2' },
                    yaxis2: { title: 'Throughput (req/s) — higher is better', anchor: 'x2' },
                    margin: { l: 60, r: 60, t: 10, b: 40 },
                    height: 350,
                    legend: { orientation: 'h', y: 1.12 },
                    plot_bgcolor: '#fff'
                }, { responsive: true, displayModeBar: false });
            }
        }

        // Architecture comparison chart
        var el = document.getElementById(ctx.chartId('chart-arch'));
        if (el && cr.length > 1) {
            var archData = {};
            cr.forEach(function(r) {
                var arch = r.architecture || 'AGGREGATED';
                if (!archData[arch]) archData[arch] = [];
                archData[arch].push(r);
            });
            var traces = [];
            Object.keys(archData).forEach(function(arch) {
                var pts = archData[arch];
                traces.push({
                    x: pts.map(function(r) { return r.ttft_p90; }),
                    y: pts.map(function(r) { return r.throughput_mean || r.throughput_p90; }),
                    text: pts.map(function(r) { return r.config_name; }),
                    name: arch, mode: 'markers',
                    marker: { size: 14, color: arch === 'PD' ? '#f59e0b' : arch === 'EP' ? '#10b981' : '#3b82f6' },
                    hovertemplate: '<b>%{text}</b><br>TTFT: %{x:.0f}ms<br>Tput: %{y:.2f} req/s<extra></extra>'
                });
            });
            Plotly.newPlot(el, traces, Object.assign({}, LAYOUT, {
                xaxis: { title: 'TTFT P90 (ms) — lower is better' },
                yaxis: { title: 'Throughput Mean (req/s) — higher is better' }
            }), { responsive: true });
        }
    }

    ReportTabs.register({
        id: 'comparison',
        label: 'Comparison',
        icon: '&#8596;',
        position: 'after:configurations',
        build: build,
        render: render
    });
})();
