// tabs/resources.js — Pod CPU / Memory tracking
// Per-pod resource time series during each benchmark (downsampled from
// Prometheus container metrics) + long-run cycle snapshot trends.
(function() {
    var H = window.ReportHelpers;

    function hasSeries(r) {
        var s = (r.metrics_json || {}).resource_series;
        return s && s.pods && s.pods.length;
    }

    function entries(data) {
        return (data.all_results || []).filter(function(r) {
            return hasSeries(r) || ((r.metrics_json || {}).resource_snapshot);
        });
    }

    function mj(r) {
        if (r._mj2 === undefined) {
            try { r._mj2 = (typeof r.metrics_json === 'string') ? JSON.parse(r.metrics_json || '{}') : (r.metrics_json || {}); }
            catch (e) { r._mj2 = {}; }
        }
        return r._mj2;
    }

    function safeId(s) { return String(s).replace(/[^a-z0-9]/gi, ''); }
    function key(r) { return r.test_id || r.config_name || 'x'; }

    function build(data, ctx) {
        var list = entries(data);
        if (!list.length) return '';

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #0891b2;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee);">&#128207; Pod Resources — CPU / Memory Tracking</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Per-pod container memory (working set) and CPU during each benchmark window, sampled from Prometheus. ' +
            'Watch for step-changes between cycles (leaks) and per-pod imbalance (routing skew).</div></div>';

        list.forEach(function(r) {
            s += buildTest(ctx, r);
        });
        return s;
    }

    function buildTest(ctx, r) {
        var m = mj(r);
        var series = m.resource_series;
        var snap = m.resource_snapshot;
        var base = ctx.chartId('res-' + safeId(key(r)));
        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #06b6d4;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#06b6d4);">' +
            (r.config_name || key(r)) + '</div>';

        if (series && series.pods && series.pods.length) {
            // Summary badges
            var memVals = [], cpuVals = [];
            (series.pods || []).forEach(function(p) {
                (series.mem_mib[p] || []).forEach(function(v) { if (v !== null && v !== undefined) memVals.push(v); });
                (series.cpu_mc[p] || []).forEach(function(v) { if (v !== null && v !== undefined) cpuVals.push(v); });
            });
            function fmtMiB(v) { return v >= 1024 ? (v / 1024).toFixed(1) + ' GiB' : Math.round(v) + ' MiB'; }
            if (memVals.length) {
                s += '<div style="display:flex; gap:12px; flex-wrap:wrap; margin:12px 20px 4px; font-size:0.85em;">' +
                    '<span style="background:#0891b2; color:#fff; padding:4px 12px; border-radius:12px;">&#128190; Peak pod RSS: ' + fmtMiB(Math.max.apply(null, memVals)) + '</span>' +
                    '<span style="background:#0e7490; color:#fff; padding:4px 12px; border-radius:12px;">Avg pod RSS: ' + fmtMiB(memVals.reduce(function(a, b) { return a + b; }, 0) / memVals.length) + '</span>' +
                    (cpuVals.length ? '<span style="background:#7c3aed; color:#fff; padding:4px 12px; border-radius:12px;">&#9889; Peak pod CPU: ' + Math.round(Math.max.apply(null, cpuVals)) + 'm</span>' : '') +
                    '<span style="background:#155e75; color:#fff; padding:4px 12px; border-radius:12px;">' + series.pods.length + ' pods</span>' +
                    '</div>';
            }

            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee); font-size:0.9em; margin:14px 20px 0;">Memory (working set) per pod</div>';
            s += '<div id="' + base + '-mem" style="margin:0 20px; width:calc(100% - 40px);"></div>';
            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#a78bfa); font-size:0.9em; margin:14px 20px 0;">CPU per pod</div>';
            s += '<div id="' + base + '-cpu" style="margin:0 20px; width:calc(100% - 40px);"></div>';
        } else if (snap) {
            var rows = Object.keys(snap).map(function(p) {
                return '<tr><td style="font-family:monospace;">' + p + '</td>' +
                    '<td>' + (snap[p].rss_mb >= 1024 ? (snap[p].rss_mb / 1024).toFixed(1) + ' GiB' : snap[p].rss_mb + ' MiB') + '</td>' +
                    '<td>' + snap[p].cpu_m + 'm</td></tr>';
            }).join('');
            s += '<table class="results-table" style="font-size:0.85em; margin:12px 20px; width:calc(100% - 40px);">' +
                '<tr><th>Pod</th><th>RSS</th><th>CPU</th></tr>' + rows + '</table>';
        }

        s += '</div>';
        return s;
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        entries(data).forEach(function(r) {
            var series = mj(r).resource_series;
            if (!series || !series.pods || !series.pods.length) return;
            var base = ctx.chartId('res-' + safeId(key(r)));
            var xs = series.times || [];

            function plot(elId, field, title, color, unit) {
                var el = document.getElementById(elId);
                if (!el) return;
                var traces = [];
                (series.pods || []).forEach(function(p) {
                    var ys = (series[field] || {})[p];
                    if (ys && ys.some(function(v) { return v !== null && v !== undefined; })) {
                        traces.push({
                            x: xs, y: ys, mode: 'lines', name: p.replace(/^aggregated-|^prefill-|^decode-/, ''),
                            line: { width: 1.5 }
                        });
                    }
                });
                if (!traces.length) return;
                Plotly.newPlot(el, traces, {
                    margin: { l: 60, r: 15, t: 8, b: 32 },
                    xaxis: { title: 'seconds into test' },
                    yaxis: { title: unit },
                    plot_bgcolor: '#fff', height: 260,
                    legend: { orientation: 'h', y: 1.15, font: { size: 9 } },
                    hovermode: 'x unified'
                }, { displayModeBar: false, responsive: true });
            }

            plot(base + '-mem', 'mem_mib', 'Memory', '#0891b2', 'MiB');
            plot(base + '-cpu', 'cpu_mc', 'CPU', '#7c3aed', 'millicores');
        });
    }

    ReportTabs.register({
        id: 'resources',
        label: 'Resources',
        icon: '&#128207;',
        position: 'after:long-run',
        build: build,
        render: render
    });
})();
