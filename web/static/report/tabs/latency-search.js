// tabs/latency-search.js — Latency Search report tab. One implementation for live + offline.
(function() {

    function build(data, ctx) {
        if (!data.latency_search || !data.latency_search.trials || !data.latency_search.trials.length) return '';
        var ls = data.latency_search;
        var byArch = ls.by_architecture || {};
        var archKeys = Object.keys(byArch);
        var firstTrial = ls.trials[0];
        var targetMs = firstTrial.target_ms;
        var targetPct = firstTrial.target_percentile || 'p90';
        var metricKey = 'ttft_' + targetPct;
        var archConfigs = ls.arch_configs || {};
        var s = '';

        s += '<div class="chart-card" style="border-left:6px solid #d97706;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#d97706,#f59e0b); color:white;">Latency Search</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Uses <strong>binary search over concurrency</strong> to find the maximum throughput that stays within the TTFT SLA target.</div></div>';

        s += '<div class="chart-card" style="margin-top:16px; border:2px solid #8b5cf6; border-left:6px solid #8b5cf6;">';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">Step 10: Latency-Bounded Throughput Search</div>';
        s += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Binary search over concurrency to find the maximum throughput that keeps TTFT ' + targetPct.toUpperCase() + ' under <strong>' + targetMs + ' ms</strong>.</div>';

        archKeys.forEach(function(arch, ai) {
            var trials = byArch[arch];
            if (!trials.some(function(t) { return t.ttft_p90 != null; })) return;
            var passing = trials.filter(function(t) { return t.meets_sla; });
            var bestPassing = passing.length ? passing.reduce(function(a, b) { return a.concurrency > b.concurrency ? a : b; }) : null;
            var cfgLabel = archConfigs[arch] || arch.toUpperCase();

            s += '<div style="padding:12px 20px;">';
            s += '<div style="font-weight:700;font-size:1.05em;color:#1e293b;margin-bottom:10px;border-bottom:1px solid #e2e8f0;padding-bottom:6px;">' + arch.toUpperCase() + ': ' + cfgLabel + '</div>';

            if (bestPassing) {
                var latVal = bestPassing[metricKey] != null ? bestPassing[metricKey].toFixed(1) : '-';
                var tputKey = 'throughput_' + targetPct;
                var tputVal = bestPassing[tputKey] != null ? bestPassing[tputKey].toFixed(2) : (bestPassing.throughput_p90 != null ? bestPassing.throughput_p90.toFixed(2) : '-');
                s += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;">';
                s += '<div style="background:#f0fdf4;border-radius:10px;padding:16px;text-align:center;border:1px solid #bbf7d0;"><div style="font-size:2em;font-weight:800;color:#059669;">' + bestPassing.concurrency + '</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Optimal Concurrency</div></div>';
                s += '<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:2em;font-weight:800;color:#1e293b;">' + latVal + ' <span style="font-size:0.5em;color:#64748b;">ms</span></div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">TTFT ' + targetPct.toUpperCase() + ' at Optimal</div></div>';
                s += '<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:2em;font-weight:800;color:#1e293b;">' + tputVal + '</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Throughput ' + targetPct.toUpperCase() + ' (req/s)</div></div>';
                s += '<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:2em;font-weight:800;color:#1e293b;">' + trials.length + '</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Tests Run</div></div>';
                s += '</div>';
            } else {
                s += '<div style="margin:8px 0 12px;padding:10px 14px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;color:#991b1b;font-size:0.9em;">&#9888; No concurrency level met the SLA target for ' + arch.toUpperCase() + '.</div>';
            }

            // Chart divs
            ['p90', 'p95', 'p99'].forEach(function(pctl) {
                s += '<div id="' + ctx.chartId('chart-step9-' + pctl + '-' + ai) + '" style="height:850px;background:#fff;border-radius:8px;border:1px solid #e2e8f0;margin-top:12px;"></div>';
            });

            // Trial table
            var sorted = trials.slice().sort(function(a, b) { return a.concurrency - b.concurrency; });
            s += '<div style="margin-top:12px;overflow-x:auto;"><table style="font-size:0.85em;"><tr><th>Concurrency</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Throughput P90</th><th>SLA</th></tr>';
            sorted.forEach(function(t) {
                var sla = t.meets_sla ? '<span style="color:#059669;">Yes</span>' : '<span style="color:#dc2626;">No</span>';
                s += '<tr><td style="font-weight:700;">' + t.concurrency + '</td><td>' + (t.ttft_p50 != null ? t.ttft_p50.toFixed(1) : '-') + '</td><td>' + (t.ttft_p90 != null ? t.ttft_p90.toFixed(1) : '-') + '</td><td>' + (t.ttft_p95 != null ? t.ttft_p95.toFixed(1) : '-') + '</td><td>' + (t.ttft_p99 != null ? t.ttft_p99.toFixed(1) : '-') + '</td><td>' + (t.throughput_p90 != null ? t.throughput_p90.toFixed(2) : '-') + '</td><td>' + sla + '</td></tr>';
            });
            s += '</table></div></div>';
        });

        // All Trials table
        s += '<div style="padding:12px 20px 4px;font-weight:700;font-size:0.95em;color:#1e293b;border-top:1px solid #e2e8f0;margin-top:8px;">All Trials</div>';
        s += '<div style="padding:0 20px 16px;overflow-x:auto;"><table style="font-size:0.85em;"><tr><th>Arch</th><th>#</th><th>Phase</th><th>Concurrency</th><th>TTFT ' + targetPct.toUpperCase() + '</th><th>Throughput ' + targetPct.toUpperCase() + '</th><th>Meets SLA</th></tr>';
        ls.trials.forEach(function(t) {
            var latVal = t[metricKey] != null ? t[metricKey].toFixed(1) + ' ms' : '-';
            var s9TputRaw = t['throughput_' + targetPct] != null ? t['throughput_' + targetPct] : t.throughput_p90;
            var tputVal = s9TputRaw != null ? s9TputRaw.toFixed(2) + ' req/s' : '-';
            var slaStyle = t.meets_sla ? 'color:#059669; font-weight:700;' : 'color:#dc2626; font-weight:700;';
            s += '<tr><td>' + (t.architecture || '-') + '</td><td>' + (t.trial_number || '-') + '</td><td>' + (t.search_phase || '-') + '</td><td style="font-weight:700;">' + t.concurrency + '</td><td>' + latVal + '</td><td>' + tputVal + '</td><td style="' + slaStyle + '">' + (t.meets_sla ? 'Yes' : 'No') + '</td></tr>';
        });
        s += '</table></div>';
        s += '</div>';
        return s;
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        if (!data.latency_search || !data.latency_search.trials || !data.latency_search.trials.length) return;
        var ls = data.latency_search;
        var byArch = ls.by_architecture || {};
        var firstTrial = ls.trials[0];
        var targetPct = firstTrial.target_percentile || 'p90';

        Object.keys(byArch).forEach(function(arch, ai) {
            var trials = byArch[arch];
            if (!trials.some(function(t) { return t.ttft_p90 != null; })) return;

            ['p90', 'p95', 'p99'].forEach(function(pctl) {
                var el = document.getElementById(ctx.chartId('chart-step9-' + pctl + '-' + ai));
                if (!el) return;
                var sorted = trials.slice().sort(function(a, b) { return a.concurrency - b.concurrency; });
                var ttft = sorted.map(function(t) { return t['ttft_' + pctl]; });
                var tput = sorted.map(function(t) { return t.throughput_mean || t.throughput_p90; });
                var labels = sorted.map(function(t) { return 'c=' + t.concurrency; });
                var target = firstTrial.target_ms;
                var best = Math.min.apply(null, ttft.filter(function(v) { return v != null; }));
                var traces = [
                    { x: labels, y: ttft, name: 'TTFT ' + pctl.toUpperCase(), type: 'scatter', mode: 'lines+markers',
                      line: { color: '#3b82f6', width: 3, shape: 'spline' }, fill: 'tozeroy', fillcolor: '#3b82f614',
                      marker: { size: 10, color: ttft.map(function(v) { return v === best ? '#10b981' : '#3b82f6'; }) } },
                    { x: labels, y: tput, name: 'Throughput Mean', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                      line: { color: '#f59e0b', width: 3, shape: 'spline' }, marker: { color: '#f59e0b', size: 10, symbol: 'diamond' } }
                ];
                // SLA target line
                if (target) {
                    traces.push({ x: [labels[0], labels[labels.length - 1]], y: [target, target], name: 'SLA Target', mode: 'lines',
                        line: { color: '#dc2626', width: 2, dash: 'dash' }, showlegend: true });
                }
                Plotly.newPlot(el, traces, {
                    height: 850, margin: { t: 30, b: 60, l: 60, r: 60 },
                    xaxis: { title: 'Concurrency' },
                    yaxis: { title: 'TTFT ' + pctl.toUpperCase() + ' (ms)' },
                    yaxis2: { title: 'Throughput (req/s)', overlaying: 'y', side: 'right' },
                    showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' }
                }, { responsive: true, displayModeBar: false });
            });
        });
    }

    ReportTabs.register({
        id: 'latency-search',
        label: 'Latency Search',
        icon: '&#128269;',
        position: 'after:comparison',
        build: build,
        render: render
    });
})();
