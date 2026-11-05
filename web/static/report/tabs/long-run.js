// tabs/long-run.js — Long Run (Soak) trends
// Groups long-run cycle configs (config_name ends -cNNN) by deployment,
// showing per-cycle metrics and resource trends for leak detection.
(function() {
    var H = window.ReportHelpers;

    var CYCLE_RE = /-c(\d+)$/;

    function cycles(data) {
        var out = [];
        (data.all_results || []).forEach(function(r) {
            var m = (r.config_name || r.test_id || '').match(CYCLE_RE);
            if (m) {
                r._cycle = parseInt(m[1], 10);
                r._base = (r.config_name || r.test_id).replace(CYCLE_RE, '');
                out.push(r);
            }
        });
        out.sort(function(a, b) { return a._cycle - b._cycle; });
        return out;
    }

    function byBase(rows) {
        var g = {};
        rows.forEach(function(r) { (g[r._base] = g[r._base] || []).push(r); });
        return g;
    }

    function mj(r) {
        if (r._mj === undefined) {
            try { r._mj = JSON.parse(r.metrics_json || '{}') || {}; }
            catch (e) { r._mj = {}; }
        }
        return r._mj;
    }

    function tc(r) {
        if (r._tc === undefined) {
            try { r._tc = JSON.parse(r.test_config_json || '{}') || {}; }
            catch (e) { r._tc = {}; }
        }
        return r._tc;
    }

    function rssTotal(r) {
        var snap = mj(r).resource_snapshot;
        if (!snap) return null;
        var t = 0;
        Object.keys(snap).forEach(function(p) { t += snap[p].rss_mb || 0; });
        return t;
    }

    function cpuTotal(r) {
        var snap = mj(r).resource_snapshot;
        if (!snap) return null;
        var t = 0;
        Object.keys(snap).forEach(function(p) { t += snap[p].cpu_m || 0; });
        return t;
    }

    function fmt(v, d) {
        if (v === null || v === undefined) return '—';
        return v.toFixed(d === undefined ? 1 : d);
    }

    function build(data, ctx) {
        var rows = cycles(data);
        if (!rows.length) return '';
        var groups = byBase(rows);

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #7c3aed;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#a78bfa);">&#128260; Long Run (Soak) — Cycle Trends</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'One deployment, cycled workloads with randomized shapes. Pods are never restarted between cycles — ' +
            'watch for monotonic growth in pod RSS (memory leak) or TTFT drift (degradation).</div></div>';

        Object.keys(groups).forEach(function(base) {
            s += buildGroup(ctx, base, groups[base]);
        });
        return s;
    }

    function buildGroup(ctx, base, rows) {
        var id = ctx.chartId('lr-' + base.replace(/[^a-z0-9]/gi, ''));
        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #8b5cf6;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">' +
            base + ' — ' + rows.length + ' cycles</div>';

        // Leak flag: monotonic RSS growth across >=4 cycles
        var rssSeries = rows.map(rssTotal).filter(function(v) { return v !== null; });
        var leak = false;
        if (rssSeries.length >= 3) {
            leak = rssSeries.every(function(v, i) { return i === 0 || v >= rssSeries[i - 1] * 0.995; });
        }
        if (leak) {
            s += '<div style="margin:10px 20px; padding:8px 14px; background:#fef2f2; border:1px solid #fca5a5; ' +
                'border-radius:8px; color:#991b1b; font-size:0.88em;">' +
                '&#9888;&#65039; Possible memory leak: pod RSS grew monotonically across cycles ' +
                '(' + rssSeries[0] + ' MiB &#8594; ' + rssSeries[rssSeries.length - 1] + ' MiB, ' +
                '+' + (((rssSeries[rssSeries.length - 1] / rssSeries[0]) - 1) * 100).toFixed(1) + '%)</div>';
        }

        // Trend charts row
        s += '<div style="display:flex; gap:10px; flex-wrap:wrap; margin:12px 20px;">';
        s += '<div id="' + id + '-ttft" style="flex:1; min-width:280px;"></div>';
        s += '<div id="' + id + '-tps" style="flex:1; min-width:280px;"></div>';
        s += '<div id="' + id + '-rss" style="flex:1; min-width:280px;"></div>';
        s += '</div>';

        // Per-cycle table
        var tblId = id + '-tbl';
        s += '<table class="results-table" id="' + tblId + '" style="font-size:0.82em; margin:8px 20px; width:calc(100% - 40px);">' +
            '<tr><th>Cycle</th><th>ISL</th><th>OSL</th><th>Users</th><th>Hit %</th>' +
            '<th>TTFT p90</th><th>ITL p90</th><th>Req/s</th><th>Pod RSS</th><th>Pod CPU</th><th>Status</th></tr>';
        rows.forEach(function(r) {
            var c = tc(r), m = mj(r);
            var rss = rssTotal(r), cpu = cpuTotal(r);
            var ok = r.status === 'completed' || m.success;
            s += '<tr>' +
                '<td style="font-weight:600;">#' + r._cycle + '</td>' +
                '<td>' + (c.isl != null ? c.isl : '—') + '</td>' +
                '<td>' + (c.osl != null ? c.osl : '—') + '</td>' +
                '<td>' + (c.num_users != null ? c.num_users : '—') + '</td>' +
                '<td>' + (c.prefix_cache_hit_pct != null ? c.prefix_cache_hit_pct : '—') + '</td>' +
                '<td data-val="' + (r.ttft_p90 || 0) + '">' + (r.ttft_p90 != null ? fmt(r.ttft_p90, 0) + 'ms' : '—') + '</td>' +
                '<td data-val="' + (r.itl_p90 || 0) + '">' + (r.itl_p90 != null ? fmt(r.itl_p90, 1) + 'ms' : '—') + '</td>' +
                '<td data-val="' + (m.throughput_mean || 0) + '">' + (m.throughput_mean != null ? fmt(m.throughput_mean, 2) : '—') + '</td>' +
                '<td data-val="' + (rss || 0) + '">' + (rss != null ? (rss / 1024).toFixed(1) + ' GiB' : '—') + '</td>' +
                '<td data-val="' + (cpu || 0) + '">' + (cpu != null ? fmt(cpu, 0) + 'm' : '—') + '</td>' +
                '<td>' + (ok ? '<span style="color:#16a34a;">&#10003;</span>' :
                    '<span style="color:#dc2626;">&#10007;</span>') + '</td></tr>';
        });
        s += '</table></div>';
        return s;
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var rows = cycles(data);
        var groups = byBase(rows);

        Object.keys(groups).forEach(function(base) {
            var id = ctx.chartId('lr-' + base.replace(/[^a-z0-9]/gi, ''));
            var rs = groups[base];
            var xs = rs.map(function(r) { return '#' + r._cycle; });

            function line(elId, ys, title, color, unit) {
                var el = document.getElementById(elId);
                if (!el) return;
                var hasData = ys.some(function(v) { return v !== null; });
                if (!hasData) return;
                Plotly.newPlot(el, [{
                    x: xs, y: ys, type: 'scatter', mode: 'lines+markers',
                    marker: { color: color }, line: { color: color }
                }], {
                    margin: { l: 55, r: 15, t: 30, b: 30 },
                    title: { text: title, font: { size: 12 } },
                    xaxis: { title: 'cycle' },
                    yaxis: { title: unit },
                    plot_bgcolor: '#fff', height: 240
                }, { displayModeBar: false, responsive: true });
            }

            line(id + '-ttft', rs.map(function(r) { return r.ttft_p90 || null; }),
                 'TTFT p90 (ms) — drift check', '#2563eb', 'ms');
            line(id + '-tps', rs.map(function(r) { return mj(r).throughput_mean || null; }),
                 'Throughput (req/s)', '#059669', 'req/s');
            line(id + '-rss', rs.map(function(r) { return rssTotal(r); }),
                 'Total Pod RSS (MiB) — leak check', '#dc2626', 'MiB');
        });
    }

    ReportTabs.register({
        id: 'long-run',
        label: 'Long Run',
        icon: '&#128260;',
        position: 'after:configurations',
        build: build,
        render: render
    });
})();
