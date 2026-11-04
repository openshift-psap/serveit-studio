// tabs/pod-profiles.js — Pod Profiles report tab (EPP + vLLM pprof captures)
//
// First modular tab: ONE implementation for the live report and the
// downloaded offline report (differences absorbed by ctx). Shows, per
// test: the EPP profile card and the consolidated inference-pod view
// (hot-pod detection at a glance), plus the cross-test EPP CPU%
// comparison.
//
// Chart-label hardening (the fix for clipped/overlapping y labels):
// every y axis gets an explicit monospace tickfont and a left margin
// computed from the longest label — no dependence on the page's
// inherited font, which consumed fixed margins whole.
(function() {
    var H = window.ReportHelpers;

    function entries(data) {
        return (data.all_results || []).filter(function(r) {
            return r.pprof && (r.pprof.epp || (r.pprof.vllm && r.pprof.vllm.length));
        });
    }

    function testKey(e) { return e.test_id || e.config_name || 'x'; }
    function safeId(s) { return String(s).replace(/[^a-z0-9]/gi, ''); }

    function podLabel(p) {
        var short = (p.pod || '?').replace(/^[a-z0-9-]*?-([a-z0-9]+-[a-z0-9]+)$/, '$1');
        return short + ' · ' + H.shortNode(p.node);
    }

    // ── build: section HTML ─────────────────────────────────────────────

    function build(data, ctx) {
        var profEntries = entries(data);
        if (!profEntries.length) return '';

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #0891b2;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee);">🔬 Pod Profiles — pprof captures during benchmark load</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'CPU profiles captured from the serving pods <strong>during the benchmark load window</strong> (Go pprof, 10ms sample resolution), plus heap and goroutine snapshots after the run. ' +
            'EPP profiles are always collected; vLLM pod profiles require the profiling toggle. ' +
            'Download the raw <code>.prof</code> files for deep analysis with <code>go tool pprof</code>.</div></div>';

        profEntries.forEach(function(e) {
            s += buildTest(data, ctx, e);
        });

        // Cross-test EPP CPU% comparison
        var eppTests = profEntries.filter(function(e) { return e.pprof.epp && e.pprof.epp.cpu_pct != null; });
        if (eppTests.length >= 2) {
            var xId = ctx.chartId('pprof-epp-cmp');
            s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #0891b2;">' +
                '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee);">EPP CPU% Across Tests</div>' +
                '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em;">EPP router CPU utilization during the load window per test — spot routing-stack regressions at a glance.</div>' +
                '<div id="' + xId + '" style="width:100%; height:' + Math.max(300, eppTests.length * 44 + 80) + 'px;"></div></div>';
        }
        return s;
    }

    function buildTest(data, ctx, e) {
        var pp = e.pprof;
        var s = '';
        s += '<div style="margin-top:16px; border:1.5px solid #e2e8f0; border-radius:8px; overflow:hidden; background:#fff;">';
        s += '<div style="background:#f0f9ff; padding:10px 14px; font-weight:700; color:#0c4a6e; border-bottom:1px solid #e2e8f0;">' +
            (e.config_name || e.name || e.test_id) +
            ' <span style="font-weight:400;color:#64748b;font-size:0.85em;">(' + e.architecture + ')</span></div>';

        if (pp.epp) s += buildEppCard(ctx, e, pp.epp);
        if (pp.vllm && pp.vllm.length) s += buildVllmBlock(ctx, e, pp);

        s += '</div>';
        return s;
    }

    function buildEppCard(ctx, e, ep) {
        var base = ctx.chartId('pprof-epp-' + safeId(testKey(e)));
        var tblId = base + '-tbl';
        var s = '';
        s += '<div style="margin:14px; border-left:4px solid #7c3aed; padding-left:12px;">';
        s += '<div style="font-weight:700; color:#7c3aed; font-size:0.95em;">EPP — ' + (ep.pod || 'endpoint-picker') +
            (ep.node ? ' <span style="font-weight:400;color:#94a3b8;font-size:0.85em;">· node ' + H.shortNode(ep.node) + '</span>' : '') + '</div>';
        s += '<div style="display:flex; gap:16px; flex-wrap:wrap; margin:8px 0; font-size:0.88em; color:#475569;">';
        if (ep.cpu_pct != null) s += '<span>CPU during load: <strong style="color:' + (ep.cpu_pct > 50 ? '#dc2626' : ep.cpu_pct > 20 ? '#d97706' : '#059669') + ';">' + ep.cpu_pct + '%</strong></span>';
        if (ep.cpu_time_ms != null) s += '<span>Total CPU: <strong>' + (ep.cpu_time_ms / 1000).toFixed(1) + 's</strong></span>';
        if (ep.duration_s) s += '<span>Window: <strong>' + ep.duration_s + 's</strong></span>';
        if (ep.sample_count) s += '<span>Samples: <strong>' + ep.sample_count.toLocaleString() + '</strong></span>';
        s += '</div>';
        s += '<div id="' + base + '" style="width:100%; height:420px; margin:6px 0;"></div>';

        if (ep.functions && ep.functions.length) {
            s += '<table class="results-table" id="' + tblId + '" style="font-size:0.85em;">' +
                '<tr><th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',0,\'str\')">Function &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',1,\'num\')">Flat ms &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',2,\'num\')">Flat % &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',3,\'num\')">Cum ms &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',4,\'num\')">Cum % &#x21C5;</th></tr>';
            ep.functions.slice(0, 15).forEach(function(f) {
                s += '<tr><td style="font-family:monospace;font-size:0.85em;word-break:break-all;">' + f.name + '</td>' +
                    '<td data-val="' + f.flat_ms + '">' + f.flat_ms.toFixed(1) + '</td>' +
                    '<td data-val="' + f.flat_pct + '">' + f.flat_pct.toFixed(2) + '%</td>' +
                    '<td data-val="' + f.cum_ms + '">' + f.cum_ms.toFixed(1) + '</td>' +
                    '<td data-val="' + f.cum_pct + '" style="font-weight:600;">' + f.cum_pct.toFixed(2) + '%</td></tr>';
            });
            s += '</table>';
        }

        var eFiles = (e.pprof.files || []).filter(function(f) { return f.kind === 'epp'; });
        if (eFiles.length) {
            s += '<div style="margin-top:8px; display:flex; gap:6px; flex-wrap:wrap;">';
            eFiles.forEach(function(f) {
                var key = f.pod + '-' + f.ptype + '.prof';
                s += ctx.profileAnchor(testKey(e), key,
                    f.ptype + '.prof &#8595; (' + (f.size / 1024).toFixed(0) + 'KB)',
                    'Download ' + key + ' (' + (f.size / 1024).toFixed(0) + ' KB)');
            });
            s += '</div>';
        }
        s += '</div>';
        return s;
    }

    function buildVllmBlock(ctx, e, pp) {
        var vllmPods = pp.vllm;
        var base = ctx.chartId('pprof-vllm-' + safeId(testKey(e)));
        var cpuId = base + '-cpu';
        var heatId = base + '-heat';
        var tblId = base + '-tbl';
        var nodes = [];
        vllmPods.forEach(function(p) { if (nodes.indexOf(p.node || '?') < 0) nodes.push(p.node || '?'); });
        nodes.sort();
        var meanCpu = vllmPods.reduce(function(s2, p) { return s2 + (p.cpu_pct || 0); }, 0) / vllmPods.length;
        var hotThreshold = Math.max(meanCpu * 1.3, meanCpu + 2);
        var hotPods = vllmPods.filter(function(p) { return (p.cpu_pct || 0) > hotThreshold; });
        var cpuVals = vllmPods.map(function(p) { return p.cpu_pct || 0; });
        var maxCpu = Math.max.apply(null, cpuVals), minCpu = Math.min.apply(null, cpuVals);
        var totalRows = vllmPods.reduce(function(s2, p) { return s2 + Math.min((p.functions || []).length, 10); }, 0);

        var s = '';
        s += '<div style="margin:14px; border-left:4px solid #10b981; padding-left:12px;">';
        s += '<div style="font-weight:700; color:#047857; font-size:0.95em;">Inference Pods — ' + vllmPods.length + ' pod(s) on ' + nodes.length + ' node(s)' +
            (hotPods.length
                ? ' <span style="color:#dc2626;">🔥 ' + hotPods.length + ' hot pod(s) &gt; ' + hotThreshold.toFixed(1) + '% (1.3× mean)</span>'
                : ' <span style="color:#059669;">✓ balanced</span>') + '</div>';
        s += '<div style="display:flex; gap:16px; flex-wrap:wrap; margin:8px 0; font-size:0.88em; color:#475569;">';
        s += '<span>Mean CPU: <strong>' + meanCpu.toFixed(1) + '%</strong></span>';
        s += '<span>Min: <strong>' + minCpu.toFixed(1) + '%</strong></span>';
        s += '<span>Max: <strong style="color:' + (maxCpu > hotThreshold ? '#dc2626' : '#059669') + ';">' + maxCpu.toFixed(1) + '%</strong></span>';
        s += '<span>Spread: <strong>' + (maxCpu - minCpu).toFixed(1) + 'pp</strong></span>';
        if (vllmPods[0] && vllmPods[0].duration_s) s += '<span>Window: <strong>' + vllmPods[0].duration_s + 's</strong></span>';
        s += '</div>';

        // node legend
        var nodePalette = ['#0891b2', '#7c3aed', '#059669', '#d97706', '#2563eb', '#be185d', '#65a30d', '#475569'];
        var nodeColor = {};
        nodes.forEach(function(n, i) { nodeColor[n] = nodePalette[i % nodePalette.length]; });
        s += '<div style="display:flex; gap:10px; flex-wrap:wrap; margin:4px 0 8px; font-size:0.8em;">';
        nodes.forEach(function(n) {
            s += '<span style="display:inline-flex;align-items:center;gap:4px;color:#475569;">' +
                '<span style="width:10px;height:10px;border-radius:2px;background:' + nodeColor[n] + ';display:inline-block;"></span>' + n + '</span>';
        });
        s += '</div>';

        s += '<div style="font-weight:600; font-size:0.85em; color:#334155; margin:10px 0 2px;">CPU% by pod during load ' +
            '<span style="font-weight:400;color:#64748b;">— dashed line = mean; 🔥 = running hot (1.3× mean)</span></div>';
        s += '<div id="' + cpuId + '" style="width:100%; height:' + Math.max(200, vllmPods.length * 28 + 70) + 'px;"></div>';

        s += '<div style="font-weight:600; font-size:0.85em; color:#334155; margin:16px 0 2px;">Top functions × pods ' +
            '<span style="font-weight:400;color:#64748b;">— cumulative CPU% per pod; dark cells = hot function on that pod</span></div>';
        s += '<div id="' + heatId + '" style="width:100%;"></div>';

        if (totalRows) {
            s += '<details style="margin-top:10px;"><summary style="cursor:pointer;font-weight:600;font-size:0.85em;color:#047857;">Per-function detail (' + totalRows + ' rows — top 10 per pod)</summary>';
            s += '<table class="results-table" id="' + tblId + '" style="font-size:0.82em;">' +
                '<tr><th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',0,\'str\')">Pod &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',1,\'str\')">Node &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',2,\'str\')">Function &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',3,\'num\')">Flat ms &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',4,\'num\')">Flat % &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',5,\'num\')">Cum ms &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',6,\'num\')">Cum % &#x21C5;</th></tr>';
            vllmPods.forEach(function(p) {
                (p.functions || []).slice(0, 10).forEach(function(f) {
                    s += '<tr><td style="font-family:monospace;">' + p.pod + '</td>' +
                        '<td>' + H.shortNode(p.node) + '</td>' +
                        '<td style="font-family:monospace;font-size:0.85em;word-break:break-all;">' + f.name + '</td>' +
                        '<td data-val="' + f.flat_ms + '">' + f.flat_ms.toFixed(1) + '</td>' +
                        '<td data-val="' + f.flat_pct + '">' + f.flat_pct.toFixed(2) + '%</td>' +
                        '<td data-val="' + f.cum_ms + '">' + f.cum_ms.toFixed(1) + '</td>' +
                        '<td data-val="' + f.cum_pct + '" style="font-weight:600;">' + f.cum_pct.toFixed(2) + '%</td></tr>';
                });
            });
            s += '</table></details>';
        }

        var vFiles = (e.pprof.files || []).filter(function(f) { return f.kind === 'vllm'; });
        if (vFiles.length) {
            s += '<div style="margin-top:8px; display:flex; gap:6px; flex-wrap:wrap;">';
            vFiles.forEach(function(f) {
                var key = f.pod + '-' + f.ptype + '.prof';
                s += ctx.profileAnchor(testKey(e), key,
                    f.ptype + '.prof · ' + H.shortNode(f.node) + ' &#8595;',
                    'Download ' + key + ' (' + (f.size / 1024).toFixed(0) + ' KB)');
            });
            s += '</div>';
        }
        s += '</div>';
        return s;
    }

    // ── render: Plotly charts ───────────────────────────────────────────

    function render(data, ctx) {
        var profEntries = entries(data);
        profEntries.forEach(function(e) {
            renderEppChart(ctx, e, e.pprof.epp);
            if (e.pprof.vllm && e.pprof.vllm.length) renderVllmCharts(ctx, e, e.pprof.vllm);
        });
        renderCrossTest(ctx, profEntries);
    }

    function renderEppChart(ctx, e, ep) {
        if (!ep || !ep.functions || !ep.functions.length) return;
        var el = document.getElementById(ctx.chartId('pprof-epp-' + safeId(testKey(e))));
        if (!el || typeof Plotly === 'undefined') return;
        var fns = ep.functions.slice(0, 12);
        var labels = fns.map(function(f) { return H.truncate(f.name, 55); }).reverse();
        // Label fix: explicit monospace tickfont + margin computed from the
        // longest label (10px mono ≈ 6.6px/char)
        var lMargin = H.leftMarginFor(labels, 6.6, 30);
        Plotly.newPlot(el, [{
            y: labels,
            x: fns.map(function(f) { return f.cum_ms; }).reverse(),
            type: 'bar', orientation: 'h',
            marker: { color: fns.map(function(f) { return f.cum_pct > 20 ? '#7c3aed' : f.cum_pct > 10 ? '#a78bfa' : '#c4b5fd'; }).reverse() },
            text: fns.map(function(f) { return f.cum_ms.toFixed(0) + 'ms (' + f.cum_pct.toFixed(1) + '%)'; }).reverse(),
            textposition: 'outside', textfont: { size: 11, color: '#334155' },
            cliponaxis: false, constraintext: 'none',
            hovertemplate: '<b>%{y}</b><br>cum: %{x:.1f} ms<extra></extra>'
        }], {
            margin: { l: lMargin, r: 90, t: 10, b: 40 },
            xaxis: { title: 'Cumulative CPU (ms)' },
            yaxis: { automargin: true, tickfont: H.MONO_10 },
            height: Math.max(280, fns.length * 30 + 60),
            plot_bgcolor: '#fafafa'
        }, { displayModeBar: false, responsive: true });
    }

    function renderVllmCharts(ctx, e, vllmPods) {
        var nodes = [];
        vllmPods.forEach(function(p) { if (nodes.indexOf(p.node || '?') < 0) nodes.push(p.node || '?'); });
        nodes.sort();
        var nodePalette = ['#0891b2', '#7c3aed', '#059669', '#d97706', '#2563eb', '#be185d', '#65a30d', '#475569'];
        var nodeColor = {};
        nodes.forEach(function(n, i) { nodeColor[n] = nodePalette[i % nodePalette.length]; });
        var meanCpu = vllmPods.reduce(function(s2, p) { return s2 + (p.cpu_pct || 0); }, 0) / vllmPods.length;
        var hotThreshold = Math.max(meanCpu * 1.3, meanCpu + 2);

        // CPU% by pod — the hot-pod view
        var cpuEl = document.getElementById(ctx.chartId('pprof-vllm-' + safeId(testKey(e)) + '-cpu'));
        if (cpuEl && typeof Plotly !== 'undefined') {
            var byCpu = vllmPods.slice().sort(function(a, b) { return (b.cpu_pct || 0) - (a.cpu_pct || 0); });
            var hot = function(p) { return (p.cpu_pct || 0) > hotThreshold; };
            var cpuLabels = byCpu.map(function(p) { return (hot(p) ? '🔥 ' : '') + podLabel(p); }).reverse();
            var lMargin = H.leftMarginFor(cpuLabels, 6.6, 60);
            Plotly.newPlot(cpuEl, [{
                y: cpuLabels,
                x: byCpu.map(function(p) { return p.cpu_pct || 0; }).reverse(),
                type: 'bar', orientation: 'h',
                marker: {
                    color: byCpu.map(function(p) { return nodeColor[p.node || '?']; }).reverse(),
                    line: {
                        color: byCpu.map(function(p) { return hot(p) ? '#dc2626' : 'transparent'; }).reverse(),
                        width: byCpu.map(function(p) { return hot(p) ? 2 : 0; }).reverse()
                    }
                },
                text: byCpu.map(function(p) { return (p.cpu_pct || 0).toFixed(1) + '%'; }).reverse(),
                textposition: 'outside', textfont: { size: 11 },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>%{y}</b><br>CPU during load: %{x:.1f}%<extra></extra>'
            }], {
                margin: { l: lMargin, r: 60, t: 10, b: 40 },
                xaxis: { title: 'CPU during load (%)' },
                yaxis: { automargin: true, tickfont: H.MONO_10 },
                height: Math.max(200, byCpu.length * 28 + 70),
                shapes: [{ type: 'line', x0: meanCpu, x1: meanCpu, y0: 0, y1: 1, yref: 'paper', line: { color: '#64748b', width: 1.5, dash: 'dash' } }],
                annotations: [{ x: meanCpu, y: 1, yref: 'paper', xanchor: 'left', yanchor: 'bottom', text: 'mean ' + meanCpu.toFixed(1) + '%', showarrow: false, font: { size: 10, color: '#64748b' } }],
                plot_bgcolor: '#fafafa'
            }, { displayModeBar: false, responsive: true });
        }

        // functions × pods heatmap
        var heatEl = document.getElementById(ctx.chartId('pprof-vllm-' + safeId(testKey(e)) + '-heat'));
        if (heatEl && typeof Plotly !== 'undefined') {
            var fnMax = {};
            vllmPods.forEach(function(p) { (p.functions || []).forEach(function(f) { fnMax[f.name] = Math.max(fnMax[f.name] || 0, f.cum_pct); }); });
            var topFns = Object.keys(fnMax).sort(function(a, b) { return fnMax[b] - fnMax[a]; }).slice(0, 20);
            if (!topFns.length) return;
            var fnL = {};
            vllmPods.forEach(function(p, j) { fnL[j] = {}; (p.functions || []).forEach(function(f) { fnL[j][f.name] = f; }); });
            var z = topFns.map(function(fn) {
                return vllmPods.map(function(p, j) { var f = fnL[j][fn]; return f ? f.cum_pct : null; });
            });
            var hv = topFns.map(function(fn) {
                return vllmPods.map(function(p, j) {
                    var f = fnL[j][fn];
                    return f ? H.truncate(fn, 50) + '<br>' + p.pod + ' (' + H.shortNode(p.node) + ')<br>cum: ' + f.cum_ms.toFixed(0) + 'ms (' + f.cum_pct.toFixed(1) + '%)' : '';
                });
            });
            Plotly.newPlot(heatEl, [{
                z: z, x: vllmPods.map(podLabel),
                y: topFns.map(function(f) { return H.truncate(f, 58); }),
                type: 'heatmap',
                colorscale: [[0, '#f0fdf4'], [0.15, '#bbf7d0'], [0.35, '#fde047'], [0.6, '#fb923c'], [0.85, '#ef4444'], [1, '#991b1b']],
                zmin: 0, hoverongaps: false,
                customdata: hv, hovertemplate: '%{customdata}<extra></extra>',
                colorbar: { title: { text: 'cum %', side: 'right' }, thickness: 12 }
            }], {
                margin: { l: 10, r: 10, t: 10, b: 70 },
                xaxis: { tickangle: -35, automargin: true, tickfont: H.MONO_10 },
                yaxis: { automargin: true, tickfont: H.MONO_9 },
                height: Math.max(320, topFns.length * 24 + 120),
                plot_bgcolor: '#fff'
            }, { displayModeBar: false, responsive: true });
        }
    }

    function renderCrossTest(ctx, profEntries) {
        var eppTests = profEntries.filter(function(e) { return e.pprof.epp && e.pprof.epp.cpu_pct != null; });
        if (eppTests.length < 2) return;
        var el = document.getElementById(ctx.chartId('pprof-epp-cmp'));
        if (!el || typeof Plotly === 'undefined') return;
        var sorted = eppTests.slice().sort(function(a, b) { return (a.pprof.epp.cpu_pct || 0) - (b.pprof.epp.cpu_pct || 0); });
        var labels = sorted.map(function(e) { return e.config_name || e.name || e.test_id; }).reverse();
        // Label fix: explicit 12px monospace tickfont + margin computed from
        // the longest label (12px mono ≈ 8px/char) — labels can no longer
        // collide with the bars regardless of the page's inherited font.
        var lMargin = H.leftMarginFor(labels, 8, 50);
        Plotly.newPlot(el, [{
            y: labels,
            x: sorted.map(function(e) { return e.pprof.epp.cpu_pct; }).reverse(),
            type: 'bar', orientation: 'h',
            marker: { color: sorted.map(function(e) { return H.cpuColor(e.pprof.epp.cpu_pct); }).reverse() },
            text: sorted.map(function(e) { return e.pprof.epp.cpu_pct + '%'; }).reverse(),
            textposition: 'outside', textfont: { size: 12 },
            cliponaxis: false, constraintext: 'none',
            hovertemplate: '<b>%{y}</b><br>EPP CPU: %{x:.1f}%<extra></extra>'
        }], {
            margin: { l: lMargin, r: 60, t: 10, b: 40 },
            xaxis: { title: 'EPP CPU during load (%)' },
            yaxis: { automargin: true, tickfont: H.MONO_12 },
            height: Math.max(300, sorted.length * 44 + 80)
        }, { displayModeBar: false, responsive: true });
    }

    ReportTabs.register({
        id: 'pod-profiles',
        label: 'Pod Profiles',
        icon: '&#128300;',
        position: 'before:estimator',
        build: build,
        render: render
    });
})();
