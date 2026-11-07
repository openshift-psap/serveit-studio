// tabs/long-run.js — Long Run (Soak) trends
// Groups long-run cycle configs (config_name ends -cNNN) by deployment,
// showing per-cycle metrics and resource trends for leak detection.
(function() {
    var H = window.ReportHelpers;

    var CYCLE_RE = /-c(\d+)$/;

    function rawName(r) {
        // test_id carries the raw config name; config_name is the display label
        return r.test_id || r.config_name || '';
    }

    function cycles(data) {
        var out = [];
        (data.all_results || []).forEach(function(r) {
            var m = rawName(r).match(CYCLE_RE);
            if (m) {
                r._cycle = parseInt(m[1], 10);
                r._base = rawName(r).replace(CYCLE_RE, '');
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

    function eppHeap(r) {
        var h = (mj(r).pprof || {}).epp_heap;
        return h ? h.inuse_mb : null;
    }

    function anonTotal(r) {
        var snap = mj(r).resource_snapshot;
        if (!snap) return null;
        var t = 0, n = 0;
        Object.keys(snap).forEach(function(p) {
            // A real vLLM server process has hundreds of MB anonymous; tiny
            // values mean the smaps read hit the wrong process (exec shell) —
            // treat as missing so one bad cycle can't break the trend
            if (snap[p].anon_mb != null && snap[p].anon_mb >= 50) { t += snap[p].anon_mb; n++; }
        });
        return n ? t : null;
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
            'One deployment, the same workload every cycle. Pods are never restarted between cycles — ' +
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
        // Leak signal: ANONYMOUS memory (proc smaps) is the real one — total
        // RSS includes file-backed page cache (weights mmap, CUDA libs) that
        // grows with cache warming, not leaks. Fall back to RSS only when
        // anon data is absent (older runs). Require >= 5% total growth to
        // avoid flagging noise.
        var anonSeries = rows.map(anonTotal).filter(function(v) { return v !== null; });
        var rssSeries = rows.map(rssTotal).filter(function(v) { return v !== null; });
        var leak = false, leakSeries = null, leakLabel = '';
        if (anonSeries.length >= 3) {
            var monotonic = anonSeries.every(function(v, i) { return i === 0 || v >= anonSeries[i - 1] * 0.995; });
            var growth = (anonSeries[anonSeries.length - 1] / anonSeries[0]) - 1;
            if (monotonic && growth >= 0.05) {
                leak = true; leakSeries = anonSeries;
                leakLabel = 'Anonymous memory';
            }
        } else if (rssSeries.length >= 3) {
            var monotonic = rssSeries.every(function(v, i) { return i === 0 || v >= rssSeries[i - 1] * 0.995; });
            var growth = (rssSeries[rssSeries.length - 1] / rssSeries[0]) - 1;
            if (monotonic && growth >= 0.05) {
                leak = true; leakSeries = rssSeries;
                leakLabel = 'Pod RSS (no anon data — may be page cache)';
            }
        }
        // EPP heap growth (Go pprof inuse_space) — require >= 20% growth
        var heapSeries = rows.map(eppHeap).filter(function(v) { return v !== null; });
        var heapLeak = false;
        if (heapSeries.length >= 3) {
            var hMono = heapSeries.every(function(v, i) { return i === 0 || v >= heapSeries[i - 1] * 0.98; });
            var hGrowth = (heapSeries[heapSeries.length - 1] / heapSeries[0]) - 1;
            heapLeak = hMono && hGrowth >= 0.20;
        }
        if (leak) {
            s += '<div style="margin:10px 20px; padding:8px 14px; background:#fef2f2; border:1px solid #fca5a5; ' +
                'border-radius:8px; color:#991b1b; font-size:0.88em;">' +
                '&#9888;&#65039; Possible memory leak: ' + leakLabel + ' grew monotonically across cycles ' +
                '(' + leakSeries[0] + ' MiB &#8594; ' + leakSeries[leakSeries.length - 1] + ' MiB, ' +
                '+' + (((leakSeries[leakSeries.length - 1] / leakSeries[0]) - 1) * 100).toFixed(1) + '%)</div>';
        } else if (anonSeries.length >= 3) {
            var aGrowth = (anonSeries[anonSeries.length - 1] / anonSeries[0]) - 1;
            var aPct = (aGrowth >= 0 ? '+' : '') + (aGrowth * 100).toFixed(1) + '%';
            if (aGrowth < 0.05) {
                s += '<div style="margin:10px 20px; padding:8px 14px; background:#f0fdf4; border:1px solid #86efac; ' +
                    'border-radius:8px; color:#166534; font-size:0.88em;">' +
                    '&#10003; No memory leak: anonymous memory stable across cycles ' +
                    '(' + anonSeries[0] + ' MiB &#8594; ' + anonSeries[anonSeries.length - 1] + ' MiB, ' + aPct + ')</div>';
            } else {
                s += '<div style="margin:10px 20px; padding:8px 14px; background:#fffbeb; border:1px solid #fcd34d; ' +
                    'border-radius:8px; color:#92400e; font-size:0.88em;">' +
                    '&#9888;&#65039; Anonymous memory grew ' + aPct + ' across cycles (not monotonic) — ' +
                    'watch the RSS trend; intermittent growth can still be a slow leak</div>';
            }
        }
        if (heapLeak) {
            s += '<div style="margin:10px 20px; padding:8px 14px; background:#fef2f2; border:1px solid #fca5a5; ' +
                'border-radius:8px; color:#991b1b; font-size:0.88em;">' +
                '&#9888;&#65039; EPP heap growth: inuse_space grew monotonically across cycles ' +
                '(' + heapSeries[0] + ' MB &#8594; ' + heapSeries[heapSeries.length - 1] + ' MB, ' +
                '+' + (((heapSeries[heapSeries.length - 1] / heapSeries[0]) - 1) * 100).toFixed(1) + '%)</div>';
        }

        // Trend charts row
        s += '<div style="display:flex; gap:10px; flex-wrap:wrap; margin:12px 20px;">';
        s += '<div id="' + id + '-ttft" style="flex:1; min-width:280px;"></div>';
        s += '<div id="' + id + '-tps" style="flex:1; min-width:280px;"></div>';
        s += '<div id="' + id + '-rss" style="flex:1; min-width:280px;"></div>';
        s += '<div id="' + id + '-heap" style="flex:1; min-width:280px;"></div>';
        s += '</div>';

        // Per-cycle table
        var tblId = id + '-tbl';
        s += '<table class="results-table" id="' + tblId + '" style="font-size:0.82em; margin:8px 20px; width:calc(100% - 40px);">' +
            '<tr><th>Cycle</th><th>ISL</th><th>OSL</th><th>Users</th><th>Hit %</th>' +
            '<th>TTFT p90</th><th>ITL p90</th><th>Req/s</th><th>Pod RSS</th><th>Anon</th><th>EPP Heap</th><th>Pod CPU</th><th>Status</th></tr>';
        rows.forEach(function(r) {
            var c = tc(r), m = mj(r);
            var rss = rssTotal(r), cpu = cpuTotal(r);
            var ok = r.status === 'completed' || m.success;
            s += '<tr>' +
                '<td style="font-weight:600;">' + (r.config_name || ('#' + r._cycle)) + '</td>' +
                '<td>' + (c.isl != null ? c.isl : '—') + '</td>' +
                '<td>' + (c.osl != null ? c.osl : '—') + '</td>' +
                '<td>' + (c.num_users != null ? c.num_users : '—') + '</td>' +
                '<td>' + (c.prefix_cache_hit_pct != null ? c.prefix_cache_hit_pct : '—') + '</td>' +
                '<td data-val="' + (r.ttft_p90 || 0) + '">' + (r.ttft_p90 != null ? fmt(r.ttft_p90, 0) + 'ms' : '—') + '</td>' +
                '<td data-val="' + (r.itl_p90 || 0) + '">' + (r.itl_p90 != null ? fmt(r.itl_p90, 1) + 'ms' : '—') + '</td>' +
                '<td data-val="' + (m.throughput_mean || 0) + '">' + (m.throughput_mean != null ? fmt(m.throughput_mean, 2) : '—') + '</td>' +
                '<td data-val="' + (rss || 0) + '">' + (rss != null ? (rss / 1024).toFixed(1) + ' GiB' : '—') + '</td>' +
                '<td data-val="' + (anonTotal(r) || 0) + '">' + (anonTotal(r) != null ? (anonTotal(r) / 1024).toFixed(1) + ' GiB' : '—') + '</td>' +
                '<td data-val="' + (eppHeap(r) || 0) + '">' + (eppHeap(r) != null ? fmt(eppHeap(r), 1) + ' MB' : '—') + '</td>' +
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
            line(id + '-heap', rs.map(function(r) { return eppHeap(r); }),
                 'EPP Heap inuse (MB) — Go pprof', '#ea580c', 'MB');
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
