// tabs/workloads.js — Parallel Workloads report tab (per-shape results for
// mixed-shape runs). One implementation for live + offline reports.
(function() {
    var H = window.ReportHelpers;

    function entries(data) {
        return (data.all_results || []).filter(function(r) {
            return r.workloads && r.workloads.length &&
                !(r.test_id || r.config_name || '').startsWith('step11-epp-');
        });
    }
    function testKey(e) { return e.test_id || e.config_name || 'x'; }
    function safeId(s) { return String(s).replace(/[^a-z0-9]/gi, ''); }

    function build(data, ctx) {
        var wlEntries = entries(data);
        if (!wlEntries.length) return '';

        var s = '<div class="chart-card" style="border:2px solid #7c3aed;border-left:6px solid #7c3aed;">';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">🧵 Parallel Workloads — per-shape results</div>';
        s += '<div style="padding:12px 20px 4px;color:#1e293b;font-size:0.95em;">Multiple workload shapes ran simultaneously against one deployment. Combined percentiles are request-weighted; the per-workload rows show each shape own latency distribution.</div>';

        wlEntries.forEach(function(e) {
            s += '<div style="margin:14px 0;"><strong style="color:#3730a3;">' + (e.config_name || e.test_id || 'test') + '</strong>';
            s += '<table class="results-table" style="margin-top:8px;"><tr><th>Workload</th><th>ISL</th><th>OSL</th><th>Requests</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P99</th><th>E2E P90</th><th>E2E P99</th><th>Tput (req/s)</th></tr>';
            (e.workloads || []).forEach(function(w) {
                var na = '—';
                s += '<tr><td><strong>' + (w.name || na) + '</strong></td><td>' + (Math.round(w.isl) || na) + '</td><td>' + (Math.round(w.osl) || na) + '</td>' +
                    '<td>' + (w.request_successful != null ? w.request_successful : na) + (w.request_total ? ' / ' + w.request_total : '') + '</td>' +
                    '<td>' + (w.ttft_p50 != null ? Math.round(w.ttft_p50) + 'ms' : na) + '</td>' +
                    '<td>' + (w.ttft_p90 != null ? Math.round(w.ttft_p90) + 'ms' : na) + '</td>' +
                    '<td>' + (w.ttft_p99 != null ? Math.round(w.ttft_p99) + 'ms' : na) + '</td>' +
                    '<td>' + (w.e2e_latency_p90 != null ? w.e2e_latency_p90.toFixed(2) + 's' : na) + '</td>' +
                    '<td>' + (w.e2e_latency_p99 != null ? w.e2e_latency_p99.toFixed(2) + 's' : na) + '</td>' +
                    '<td>' + (w.throughput_mean != null ? w.throughput_mean.toFixed(2) : na) + '</td></tr>';
            });
            // combined row
            s += '<tr style="background:#f5f3ff;font-weight:600;"><td>combined (weighted)</td><td>—</td><td>—</td>' +
                '<td>' + (e.request_successful != null ? e.request_successful : '—') + '</td>' +
                '<td>' + (e.ttft_p50 != null ? Math.round(e.ttft_p50) + 'ms' : '—') + '</td>' +
                '<td>' + (e.ttft_p90 != null ? Math.round(e.ttft_p90) + 'ms' : '—') + '</td>' +
                '<td>' + (e.ttft_p99 != null ? Math.round(e.ttft_p99) + 'ms' : '—') + '</td>' +
                '<td>' + (e.e2e_latency_p90 != null ? e.e2e_latency_p90.toFixed(2) + 's' : '—') + '</td>' +
                '<td>' + (e.e2e_latency_p99 != null ? e.e2e_latency_p99.toFixed(2) + 's' : '—') + '</td>' +
                '<td>' + (e.throughput_mean != null ? e.throughput_mean.toFixed(2) : '—') + '</td></tr>';
            s += '</table>';
            // per-workload TTFT chart
            s += '<div id="' + ctx.chartId('wl-ttft-' + safeId(testKey(e))) + '" style="height:' + Math.max(240, (e.workloads.length + 1) * 60) + 'px;margin:10px 0;"></div>';
            s += '</div>';
        });
        s += '</div>';
        return s;
    }

    function render(data, ctx) {
        entries(data).forEach(function(e) {
            var el = document.getElementById(ctx.chartId('wl-ttft-' + safeId(testKey(e))));
            if (!el || typeof Plotly === 'undefined') return;
            var rows = e.workloads.concat([{ name: 'combined', ttft_p50: e.ttft_p50, ttft_p90: e.ttft_p90, ttft_p99: e.ttft_p99 }]);
            var names = rows.map(function(w) { return w.name; });
            var lMargin = H.leftMarginFor(names, 6.6, 40);
            Plotly.newPlot(el, ['p50', 'p90', 'p99'].map(function(k, ci) {
                return {
                    y: names, x: rows.map(function(w) { return w['ttft_' + k]; }),
                    name: k.toUpperCase(), type: 'bar', orientation: 'h',
                    marker: { color: ['#3b82f6', '#f59e0b', '#dc2626'][ci] }
                };
            }), {
                barmode: 'group', height: Math.max(240, (rows.length + 1) * 60),
                margin: { t: 30, b: 30, l: lMargin, r: 20 },
                xaxis: { title: 'TTFT (ms)' },
                yaxis: { automargin: true, tickfont: H.MONO_10 },
                showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' },
                title: { text: 'TTFT by workload', font: { size: 13 } }
            }, { displayModeBar: false });
        });
    }

    ReportTabs.register({
        id: 'workloads',
        label: 'Workloads',
        icon: '&#128203;',
        position: 'end',
        build: build,
        render: render
    });
})();
