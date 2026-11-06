// tabs/resources.js — Pod CPU / Memory tracking
// Per-pod resource usage during each benchmark, rendered as heatmaps
// (pods × time, color = value) plus a per-pod summary table — line charts
// with 8 similar pods were unreadable.
(function() {
    var H = window.ReportHelpers;

    function mj(r) {
        if (r._mj2 === undefined) {
            var raw = r.metrics_json;
            if (typeof raw === 'string') {
                try { r._mj2 = JSON.parse(raw || '{}') || {}; }
                catch (e) { r._mj2 = {}; }
            } else {
                r._mj2 = raw || {};
            }
        }
        return r._mj2;
    }

    function hasSeries(r) {
        var s = mj(r).resource_series;
        return !!(s && s.pods && s.pods.length);
    }

    function entries(data) {
        return (data.all_results || []).filter(function(r) {
            return hasSeries(r) || mj(r).resource_snapshot;
        });
    }

    function safeId(s) { return String(s).replace(/[^a-z0-9]/gi, ''); }
    function key(r) { return r.test_id || r.config_name || 'x'; }
    function shortPod(p) { return String(p).replace(/^aggregated-|^prefill-|^decode-/, ''); }

    function statsOf(series, field) {
        var out = {};
        (series.pods || []).forEach(function(p) {
            var vs = (series[field] || {})[p] || [];
            var nums = vs.filter(function(v) { return v !== null && v !== undefined; });
            if (nums.length) {
                out[p] = {
                    avg: nums.reduce(function(a, b) { return a + b; }, 0) / nums.length,
                    max: Math.max.apply(null, nums),
                    min: Math.min.apply(null, nums)
                };
            }
        });
        return out;
    }

    function fmtMiB(v) {
        return v >= 1024 ? (v / 1024).toFixed(1) + ' GiB' : Math.round(v) + ' MiB';
    }

    function build(data, ctx) {
        var list = entries(data);
        if (!list.length) return '';

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #0891b2;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee);">&#128207; Pod Resources — CPU / Memory Tracking</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Per-pod container memory (working set) and CPU during each benchmark window. ' +
            'Color intensity shows usage — horizontal stripes reveal routing imbalance, ' +
            'darkening over time hints at growth.</div></div>';

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
            var memSt = statsOf(series, 'mem_mib');
            var cpuSt = statsOf(series, 'cpu_mc');

            // Summary badges
            var peakMem = 0, sumAvgMem = 0, nMem = 0, peakCpu = 0;
            Object.keys(memSt).forEach(function(p) {
                peakMem = Math.max(peakMem, memSt[p].max);
                sumAvgMem += memSt[p].avg; nMem++;
            });
            Object.keys(cpuSt).forEach(function(p) { peakCpu = Math.max(peakCpu, cpuSt[p].max); });
            if (nMem) {
                s += '<div style="display:flex; gap:12px; flex-wrap:wrap; margin:12px 20px 4px; font-size:0.85em;">' +
                    '<span style="background:#0891b2; color:#fff; padding:4px 12px; border-radius:12px;">&#128190; Peak pod RSS: ' + fmtMiB(peakMem) + '</span>' +
                    '<span style="background:#0e7490; color:#fff; padding:4px 12px; border-radius:12px;">Fleet avg RSS: ' + fmtMiB(sumAvgMem / nMem) + '/pod</span>' +
                    (peakCpu ? '<span style="background:#7c3aed; color:#fff; padding:4px 12px; border-radius:12px;">&#9889; Peak pod CPU: ' + Math.round(peakCpu) + 'm</span>' : '') +
                    '<span style="background:#155e75; color:#fff; padding:4px 12px; border-radius:12px;">' + series.pods.length + ' pods</span>' +
                    '</div>';
            }

            // Per-pod summary table (scannable numbers)
            var tblId = base + '-tbl';
            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee); font-size:0.9em; margin:14px 20px 0;">Per-Pod Summary</div>';
            s += '<table class="results-table" id="' + tblId + '" style="font-size:0.82em; margin:0 20px 8px; width:calc(100% - 40px);">' +
                '<tr><th>Pod</th><th>Avg RSS</th><th>Peak RSS</th><th>Δ RSS</th><th>Avg CPU</th><th>Peak CPU</th></tr>';
            series.pods.forEach(function(p) {
                var ms = memSt[p] || {}, cs = cpuSt[p] || {};
                var delta = (ms.max !== undefined && ms.min !== undefined) ? ms.max - ms.min : null;
                var deltaPct = (delta !== null && ms.min > 0) ? (100 * delta / ms.min) : null;
                var deltaColor = deltaPct === null ? '#64748b' : (deltaPct > 20 ? '#dc2626' : (deltaPct > 5 ? '#d97706' : '#16a34a'));
                s += '<tr>' +
                    '<td style="font-family:monospace;">' + p + '</td>' +
                    '<td>' + (ms.avg !== undefined ? fmtMiB(ms.avg) : '—') + '</td>' +
                    '<td>' + (ms.max !== undefined ? fmtMiB(ms.max) : '—') + '</td>' +
                    '<td style="color:' + deltaColor + ';">' + (delta !== null ? '+' + fmtMiB(delta) + (deltaPct !== null ? ' (' + deltaPct.toFixed(0) + '%)' : '') : '—') + '</td>' +
                    '<td>' + (cs.avg !== undefined ? Math.round(cs.avg) + 'm' : '—') + '</td>' +
                    '<td>' + (cs.max !== undefined ? Math.round(cs.max) + 'm' : '—') + '</td>' +
                    '</tr>';
            });
            s += '</table>';

            // Heatmaps
            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee); font-size:0.9em; margin:14px 20px 0;">Memory Heatmap (working set, GiB)</div>';
            s += '<div id="' + base + '-mem" style="margin:0 20px; width:calc(100% - 40px);"></div>';
            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#a78bfa); font-size:0.9em; margin:14px 20px 0;">CPU Heatmap (millicores)</div>';
            s += '<div id="' + base + '-cpu" style="margin:0 20px; width:calc(100% - 40px);"></div>';
        } else if (snap) {
            var rows = Object.keys(snap).map(function(p) {
                return '<tr><td style="font-family:monospace;">' + p + '</td>' +
                    '<td>' + fmtMiB(snap[p].rss_mb) + '</td>' +
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
            var pods = series.pods;
            var yLabels = pods.map(shortPod);
            var height = Math.max(180, pods.length * 26 + 70);

            function heatmap(elId, field, colorscale, unit, divisor, fmt) {
                var el = document.getElementById(elId);
                if (!el) return;
                var z = pods.map(function(p) {
                    return ((series[field] || {})[p] || []).map(function(v) {
                        return (v === null || v === undefined) ? null : v / divisor;
                    });
                });
                if (!z.some(function(row) { return row.some(function(v) { return v !== null; }); })) return;
                Plotly.newPlot(el, [{
                    x: xs, y: yLabels, z: z, type: 'heatmap',
                    colorscale: colorscale,
                    colorbar: { title: { text: unit, side: 'right' }, thickness: 12 },
                    hovertemplate: '%{y} @ %{x}s<br>' + fmt + '<extra></extra>',
                    xgap: 1, ygap: 1
                }], {
                    margin: { l: 70, r: 15, t: 8, b: 34 },
                    xaxis: { title: 'seconds into test' },
                    yaxis: { automargin: true, tickfont: H.MONO_9 },
                    plot_bgcolor: '#fff', height: height
                }, { displayModeBar: false, responsive: true });
            }

            heatmap(base + '-mem', 'mem_mib', 'YlGnBu', 'GiB', 1024, '%{z:.1f} GiB');
            heatmap(base + '-cpu', 'cpu_mc', 'Purples', 'mCPU', 1, '%{z:.0f}m');
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
