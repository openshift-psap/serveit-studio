// tabs/pod-profiles.js — EPP Profiles (long-run cycles only)
//
// Cross-cycle comparison of the EPP Go pprof CPU profiles: cpu% trend per
// cycle and a top-functions table with per-cycle columns, so function-level
// drift between iterations of the SAME workload is visible at a glance.
// Only appears for long-run runs (cycle configs, -cNNN test ids) — comparing
// profiles across different configurations is meaningless.
(function() {
    var H = window.ReportHelpers;

    var CYCLE_RE = /-c(\d+)$/;

    function mj(r) {
        if (r._mj === undefined) {
            var raw = r.metrics_json;
            if (typeof raw === 'string') {
                try { r._mj = JSON.parse(raw || '{}') || {}; }
                catch (e) { r._mj = {}; }
            } else {
                r._mj = raw || {};
            }
        }
        return r._mj;
    }

    function eppOf(r) {
        return (mj(r).pprof || {}).epp;
    }

    // Long-run cycle rows that carry an EPP profile
    function groups(data) {
        var byBase = {};
        (data.all_results || []).forEach(function(r) {
            var m = (r.test_id || '').match(CYCLE_RE);
            var epp = eppOf(r);
            if (!m || !epp) return;
            r._cycle = parseInt(m[1], 10);
            var base = (r.test_id || '').replace(CYCLE_RE, '');
            (byBase[base] = byBase[base] || []).push(r);
        });
        Object.keys(byBase).forEach(function(b) {
            byBase[b].sort(function(a, c) { return a._cycle - c._cycle; });
        });
        return byBase;
    }

    function safeId(s) { return String(s).replace(/[^a-z0-9]/gi, ''); }

    function build(data, ctx) {
        var gs = groups(data);
        var bases = Object.keys(gs);
        if (!bases.length) return '';

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #7c3aed;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#c084fc);">&#128300; EPP Profiles — Cross-Cycle Comparison</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Go pprof CPU profiles of the EPP router, one per long-run cycle of the identical workload. ' +
            'The cpu% trend and per-cycle function columns expose router-level drift between iterations — ' +
            'a function whose share grows cycle over cycle is a hot spot worth investigating.</div></div>';

        bases.forEach(function(base) {
            s += buildGroup(ctx, base, gs[base]);
        });
        return s;
    }

    function buildGroup(ctx, base, rows) {
        var id = ctx.chartId('epp-' + safeId(base));
        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #8b5cf6;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">' +
            base + ' — ' + rows.length + ' cycles</div>';

        // cpu% trend chart
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#a78bfa); font-size:0.9em; margin:14px 20px 0;">EPP CPU% per Cycle</div>';
        s += '<div id="' + id + '-cpu" style="margin:0 20px; width:calc(100% - 40px);"></div>';

        // Top-functions cross-cycle table
        // Union of functions across cycles, ranked by the LAST cycle's cum_pct
        var fnStats = {};
        rows.forEach(function(r) {
            var cyc = r._cycle;
            (eppOf(r).functions || []).forEach(function(f) {
                var st = fnStats[f.name];
                if (!st) { st = fnStats[f.name] = { name: f.name, byCycle: {} }; }
                st.byCycle[cyc] = f;
            });
        });
        var lastCycle = rows[rows.length - 1]._cycle;
        var fns = Object.keys(fnStats).map(function(k) { return fnStats[k]; });
        fns.sort(function(a, b) {
            var av = (a.byCycle[lastCycle] || {}).cum_pct || 0;
            var bv = (b.byCycle[lastCycle] || {}).cum_pct || 0;
            return bv - av;
        });
        fns = fns.slice(0, 15);

        var tblId = id + '-tbl';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#c084fc); font-size:0.9em; margin:14px 20px 0;">Top Functions per Cycle (cumulative %)</div>';
        // 50 cycles = ~52 columns — scroll horizontally, function name pinned
        s += '<div style="margin:0 20px 8px; overflow-x:auto; max-width:calc(100% - 40px);">';
        s += '<table class="results-table" id="' + tblId + '" style="font-size:0.8em; width:max-content; min-width:100%;">' +
            '<tr><th style="position:sticky; left:0; z-index:2; min-width:220px;">Function</th>';
        rows.forEach(function(r) {
            s += '<th>c' + r._cycle + '</th>';
        });
        s += '<th>&#916; pp</th></tr>';

        fns.forEach(function(st) {
            var first = null, last = null;
            s += '<tr><td style="font-family:monospace; font-size:0.85em; position:sticky; left:0; background:#fff; z-index:1; white-space:nowrap;">' + H.truncate(st.name, 55) + '</td>';
            rows.forEach(function(r) {
                var f = st.byCycle[r._cycle];
                var pct = f ? f.cum_pct : null;
                if (pct !== null) {
                    if (first === null) first = pct;
                    last = pct;
                }
                s += '<td style="text-align:right;" data-val="' + (pct || 0) + '">' +
                     (pct !== null ? pct.toFixed(1) + '%' : '—') + '</td>';
            });
            var delta = (first !== null && last !== null) ? last - first : null;
            var dColor = delta === null ? '#64748b' : (delta > 5 ? '#dc2626' : (delta < -5 ? '#16a34a' : '#64748b'));
            s += '<td style="text-align:right; color:' + dColor + ';">' +
                 (delta !== null ? (delta > 0 ? '+' : '') + delta.toFixed(1) : '—') + '</td></tr>';
        });
        s += '</table></div>';

        // Per-cycle summary line
        s += '<div style="margin:4px 20px 12px; color:#64748b; font-size:0.8em;">' +
            rows.map(function(r) {
                var e = eppOf(r);
                return 'c' + r._cycle + ': ' + (e.cpu_pct != null ? e.cpu_pct.toFixed(2) + '% CPU' : 'n/a') +
                       ' (' + Math.round((e.cpu_time_ms || 0) / 1000) + 's / ' + (e.duration_s || 0).toFixed(0) + 's window)';
            }).join(' · ') + '</div>';

        s += '</div>';
        return s;
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var gs = groups(data);
        Object.keys(gs).forEach(function(base) {
            var rows = gs[base];
            var id = ctx.chartId('epp-' + safeId(base));
            var el = document.getElementById(id + '-cpu');
            if (!el) return;
            var xs = rows.map(function(r) { return 'c' + r._cycle; });
            var ys = rows.map(function(r) { return eppOf(r).cpu_pct; });
            if (!ys.some(function(v) { return v != null; })) return;
            Plotly.newPlot(el, [{
                x: xs, y: ys, type: 'bar',
                marker: { color: ys.map(function(v) { return v > 50 ? '#dc2626' : (v > 20 ? '#d97706' : '#7c3aed'); }) },
                text: ys.map(function(v) { return v != null ? v.toFixed(2) + '%' : '—'; }),
                textposition: 'outside', textfont: { size: 10, color: '#334155' },
                cliponaxis: false, constraintext: 'none'
            }], {
                margin: { l: 50, r: 30, t: 8, b: 28 },
                yaxis: { title: 'CPU %', ticksuffix: '%' },
                plot_bgcolor: '#fff', height: 220
            }, { displayModeBar: false, responsive: true });
        });
    }

    ReportTabs.register({
        id: 'pod-profiles',
        label: 'EPP Profiles',
        icon: '&#128300;',
        position: 'after:traffic',
        build: build,
        render: render
    });
})();
