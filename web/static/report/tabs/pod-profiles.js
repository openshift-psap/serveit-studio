// tabs/pod-profiles.js — Pod Profiles report tab (EPP + vLLM pprof captures)
//
// Structure:
//   1. EPP Router section (dedicated) — per-test top functions chart + CPU% table
//   2. Inference Pods section (per test) — per-pod function charts + hot-pod bar + heatmap
//   3. Cross-test EPP CPU% comparison
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

    function fmtMs(v) {
        if (v == null) return '-';
        return v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms';
    }

    function barColor(pct, main, mid, light) {
        return pct > 20 ? main : (pct > 10 ? mid : light);
    }

    // ════════════════════════════════════════════════════════════════
    // BUILD
    // ════════════════════════════════════════════════════════════════

    function build(data, ctx) {
        var profEntries = entries(data);
        if (!profEntries.length) return '';

        var s = '';

        // ── Header card ──
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #0891b2;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee);">🔬 Pod Profiles — pprof captures during benchmark load</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'CPU profiles captured from serving pods <strong>during the benchmark load window</strong> (Go pprof, 10ms sample resolution), plus heap and goroutine snapshots after the run. ' +
            'EPP profiles are always collected; vLLM pod profiles require the profiling toggle. ' +
            'Download raw <code>.prof</code> files for analysis with <code>go tool pprof</code>.</div></div>';

        // ── DEDICATED EPP ROUTER SECTION ──
        var eppTests = profEntries.filter(function(e) { return e.pprof.epp && e.pprof.epp.cpu_pct != null; });
        if (eppTests.length) {
            s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #7c3aed;">' +
                '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#a78bfa);">🔀 EPP Router — Endpoint Picker Profiles</div>' +
                '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
                'The EPP (Endpoint Picker) routes every inference request to a serving pod. ' +
                'These charts show where the EPP spends CPU during the load window — ' +
                'the function breakdown reveals whether time goes to routing decisions, metrics parsing, or Go runtime overhead.</div>';

            // Per-test EPP chart — one bar chart per test showing top functions
            eppTests.forEach(function(e, idx) {
                var ep = e.pprof.epp;
                var chartId = ctx.chartId('pprof-epp-fn-' + safeId(testKey(e)) + '-' + idx);
                var dlBase = ctx.chartId('pprof-epp-' + safeId(testKey(e)));

                s += '<div style="margin:14px 0; padding:12px; background:#faf5ff; border-radius:8px; border:1px solid #e9d5ff;">';
                s += '<div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:8px;">';
                s += '<div style="font-weight:700; color:#6b21a8; font-size:0.92em;">' +
                    (e.config_name || e.name || e.test_id) +
                    ' <span style="font-weight:400;color:#94a3b8;font-size:0.85em;">· ' + (ep.pod || '') +
                    (ep.node ? ' · node ' + H.shortNode(ep.node) : '') + '</span></div>';
                s += '<div style="display:flex; gap:12px; font-size:0.85em; color:#475569;">';
                if (ep.cpu_pct != null) s += '<span>CPU: <strong style="color:' + (ep.cpu_pct > 50 ? '#dc2626' : ep.cpu_pct > 20 ? '#d97706' : '#059669') + ';">' + ep.cpu_pct + '%</strong></span>';
                if (ep.cpu_time_ms != null) s += '<span>Total: <strong>' + (ep.cpu_time_ms / 1000).toFixed(1) + 's</strong></span>';
                if (ep.sample_count) s += '<span>Samples: <strong>' + ep.sample_count.toLocaleString() + '</strong></span>';
                s += '</div></div>';

                // Top functions bar chart
                s += '<div id="' + chartId + '" style="width:100%; height:400px;"></div>';

                // Function table
                if (ep.functions && ep.functions.length) {
                    var tblId = ctx.chartId('pprof-epp-tbl-' + safeId(testKey(e)) + '-' + idx);
                    s += '<details style="margin-top:8px;"><summary style="cursor:pointer;font-weight:600;font-size:0.85em;color:#6b21a8;">Top functions (' + Math.min(ep.functions.length, 15) + ' rows)</summary>';
                    s += '<table class="results-table" id="' + tblId + '" style="font-size:0.82em;">' +
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
                    s += '</table></details>';
                }

                // Downloads
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
            });

            s += '</div>';
        }

        // ── INFERENCE PODS SECTION (per test) ──
        var vllmTests = profEntries.filter(function(e) { return e.pprof.vllm && e.pprof.vllm.length; });
        vllmTests.forEach(function(e) {
            s += buildVllmSection(ctx, e, e.pprof);
        });

        // ── CROSS-TEST EPP CPU% COMPARISON ──
        if (eppTests.length >= 2) {
            var xId = ctx.chartId('pprof-epp-cmp');
            s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #0891b2;">' +
                '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee);">EPP CPU% Across Tests</div>' +
                '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em;">EPP router CPU utilization during the load window per test — spot routing-stack regressions at a glance.</div>' +
                '<div id="' + xId + '" style="width:100%; height:' + Math.max(300, eppTests.length * 44 + 80) + 'px;"></div></div>';
        }

        return s;
    }

    // ════════════════════════════════════════════════════════════════
    // INFERENCE PODS SECTION
    // ════════════════════════════════════════════════════════════════

    function buildVllmSection(ctx, e, pp) {
        var vllmPods = pp.vllm;
        var base = ctx.chartId('pprof-vllm-' + safeId(testKey(e)));
        var cpuId = base + '-cpu';
        var heatId = base + '-heat';
        var nodes = [];
        vllmPods.forEach(function(p) { if (nodes.indexOf(p.node || '?') < 0) nodes.push(p.node || '?'); });
        nodes.sort();
        var meanCpu = vllmPods.reduce(function(s2, p) { return s2 + (p.cpu_pct || 0); }, 0) / vllmPods.length;
        var hotThreshold = Math.max(meanCpu * 1.3, meanCpu + 2);
        var hotPods = vllmPods.filter(function(p) { return (p.cpu_pct || 0) > hotThreshold; });
        var cpuVals = vllmPods.map(function(p) { return p.cpu_pct || 0; });
        var maxCpu = Math.max.apply(null, cpuVals), minCpu = Math.min.apply(null, cpuVals);

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #10b981;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#059669,#34d399);">🖥️ Inference Pods — ' + (e.config_name || e.test_id || '') + '</div>';
        s += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            vllmPods.length + ' pod(s) on ' + nodes.length + ' node(s)' +
            (hotPods.length ? ' — <span style="color:#dc2626;">🔥 ' + hotPods.length + ' hot pod(s) > ' + hotThreshold.toFixed(1) + '% (1.3× mean)</span>' : ' — <span style="color:#059669;">✓ balanced</span>') +
            '</div>';

        // Stats bar
        s += '<div style="display:flex; gap:16px; flex-wrap:wrap; margin:8px 20px; font-size:0.88em; color:#475569;">' +
            '<span>Mean CPU: <strong>' + meanCpu.toFixed(1) + '%</strong></span>' +
            '<span>Min: <strong>' + minCpu.toFixed(1) + '%</strong></span>' +
            '<span>Max: <strong style="color:' + (maxCpu > hotThreshold ? '#dc2626' : '#059669') + ';">' + maxCpu.toFixed(1) + '%</strong></span>' +
            '<span>Spread: <strong>' + (maxCpu - minCpu).toFixed(1) + 'pp</strong></span>' +
            '</div>';

        // Node legend
        var nodePalette = ['#0891b2', '#7c3aed', '#059669', '#d97706', '#2563eb', '#be185d', '#65a30d', '#475569'];
        var nodeColor = {};
        nodes.forEach(function(n, i) { nodeColor[n] = nodePalette[i % nodePalette.length]; });
        s += '<div style="display:flex; gap:10px; flex-wrap:wrap; margin:4px 20px 8px; font-size:0.8em;">';
        nodes.forEach(function(n) {
            s += '<span style="display:inline-flex;align-items:center;gap:4px;color:#475569;">' +
                '<span style="width:10px;height:10px;border-radius:2px;background:' + nodeColor[n] + ';display:inline-block;"></span>' + n + '</span>';
        });
        s += '</div>';

        // CPU% by pod (hot-pod view)
        s += '<div style="font-weight:600; font-size:0.85em; color:#334155; margin:10px 20px 2px;">CPU% by pod during load ' +
            '<span style="font-weight:400;color:#64748b;">— dashed line = mean; 🔥 = running hot</span></div>';
        s += '<div id="' + cpuId + '" style="width:100%; height:' + Math.max(200, vllmPods.length * 28 + 70) + 'px; margin:0 20px;"></div>';

        // PER-POD FUNCTION BREAKDOWN CHARTS (one per pod)
        s += '<div style="font-weight:600; font-size:0.85em; color:#334155; margin:16px 20px 2px;">Per-pod function breakdown ' +
            '<span style="font-weight:400;color:#64748b;">— top CPU consumers per pod</span></div>';
        vllmPods.forEach(function(vp, vi) {
            var podChartId = base + '-pod-' + vi;
            var hot = (vp.cpu_pct || 0) > hotThreshold;
            s += '<div style="margin:8px 20px; padding:8px 12px; background:' + (hot ? '#fef2f2' : '#f8fafc') + '; border-radius:6px; border-left:3px solid ' + (hot ? '#dc2626' : nodeColor[vp.node || '?'] || '#94a3b8') + ';">';
            s += '<div style="display:flex; justify-content:space-between; align-items:center; font-size:0.85em; margin-bottom:4px;">';
            s += '<span style="font-family:monospace; font-weight:600; color:#334155;">' + (hot ? '🔥 ' : '') + (vp.pod || '?') +
                ' <span style="color:#94a3b8; font-weight:400;">· ' + H.shortNode(vp.node) + '</span></span>';
            s += '<span style="color:' + (hot ? '#dc2626' : '#475569') + '; font-weight:600;">CPU: ' + (vp.cpu_pct || 0).toFixed(1) + '%</span>';
            s += '</div>';
            s += '<div id="' + podChartId + '" style="width:100%; height:' + Math.max(160, Math.min(8, (vp.functions || []).length) * 26 + 40) + 'px;"></div>';
            s += '</div>';
        });

        // Heatmap: functions × pods
        s += '<div style="font-weight:600; font-size:0.85em; color:#334155; margin:16px 20px 2px;">Top functions × pods ' +
            '<span style="font-weight:400;color:#64748b;">— cumulative CPU% per pod; dark cells = hot function on that pod</span></div>';
        s += '<div id="' + heatId + '" style="width:100%; margin:0 20px;"></div>';

        // Merged detail table
        var totalRows = vllmPods.reduce(function(s2, p) { return s2 + Math.min((p.functions || []).length, 10); }, 0);
        if (totalRows) {
            var tblId = base + '-tbl';
            s += '<details style="margin:10px 20px;"><summary style="cursor:pointer;font-weight:600;font-size:0.85em;color:#047857;">Per-function detail (' + totalRows + ' rows — top 10 per pod)</summary>';
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

        // Downloads
        var vFiles = (e.pprof.files || []).filter(function(f) { return f.kind === 'vllm'; });
        if (vFiles.length) {
            s += '<div style="margin:8px 20px; display:flex; gap:6px; flex-wrap:wrap;">';
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

    // ════════════════════════════════════════════════════════════════
    // RENDER
    // ════════════════════════════════════════════════════════════════

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var profEntries = entries(data);

        // EPP per-test function charts
        var eppTests = profEntries.filter(function(e) { return e.pprof.epp && e.pprof.epp.cpu_pct != null; });
        eppTests.forEach(function(e, idx) {
            renderEppFunctionChart(ctx, e, e.pprof.epp, idx);
        });

        // Inference pod charts
        profEntries.forEach(function(e) {
            if (e.pprof.vllm && e.pprof.vllm.length) renderVllmCharts(ctx, e, e.pprof.vllm);
        });

        // Cross-test comparison
        if (eppTests.length >= 2) renderCrossTest(ctx, eppTests);
    }

    function renderEppFunctionChart(ctx, e, ep, idx) {
        if (!ep || !ep.functions || !ep.functions.length) return;
        var el = document.getElementById(ctx.chartId('pprof-epp-fn-' + safeId(testKey(e)) + '-' + idx));
        if (!el) return;
        var fns = ep.functions.slice(0, 12);
        var labels = fns.map(function(f) { return H.truncate(f.name, 55); }).reverse();
        var lMargin = H.leftMarginFor(labels, 6.6, 30);
        Plotly.newPlot(el, [{
            y: labels,
            x: fns.map(function(f) { return f.cum_ms; }).reverse(),
            type: 'bar', orientation: 'h',
            marker: { color: fns.map(function(f) { return barColor(f.cum_pct, '#7c3aed', '#a78bfa', '#c4b5fd'); }).reverse() },
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
        var base = ctx.chartId('pprof-vllm-' + safeId(testKey(e)));
        var nodes = [];
        vllmPods.forEach(function(p) { if (nodes.indexOf(p.node || '?') < 0) nodes.push(p.node || '?'); });
        nodes.sort();
        var nodePalette = ['#0891b2', '#7c3aed', '#059669', '#d97706', '#2563eb', '#be185d', '#65a30d', '#475569'];
        var nodeColor = {};
        nodes.forEach(function(n, i) { nodeColor[n] = nodePalette[i % nodePalette.length]; });
        var meanCpu = vllmPods.reduce(function(s2, p) { return s2 + (p.cpu_pct || 0); }, 0) / vllmPods.length;
        var hotThreshold = Math.max(meanCpu * 1.3, meanCpu + 2);

        // ── CPU% by pod ──
        var cpuEl = document.getElementById(base + '-cpu');
        if (cpuEl) {
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

        // ── PER-POD FUNCTION BREAKDOWN CHARTS ──
        vllmPods.forEach(function(vp, vi) {
            var podEl = document.getElementById(base + '-pod-' + vi);
            if (!podEl || !vp.functions || !vp.functions.length) return;
            var fns = vp.functions.slice(0, 8);
            var labels = fns.map(function(f) { return H.truncate(f.name, 50); }).reverse();
            var lM = H.leftMarginFor(labels, 6.6, 25);
            Plotly.newPlot(podEl, [{
                y: labels,
                x: fns.map(function(f) { return f.cum_ms; }).reverse(),
                type: 'bar', orientation: 'h',
                marker: { color: fns.map(function(f) { return barColor(f.cum_pct, '#059669', '#34d399', '#a7f3d0'); }).reverse() },
                text: fns.map(function(f) { return f.cum_ms.toFixed(0) + 'ms'; }).reverse(),
                textposition: 'outside', textfont: { size: 10, color: '#334155' },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>%{y}</b><br>cum: %{x:.1f} ms<extra></extra>'
            }], {
                margin: { l: lM, r: 70, t: 5, b: 30 },
                xaxis: { title: '' },
                yaxis: { automargin: true, tickfont: H.MONO_9 },
                height: Math.max(160, fns.length * 26 + 40),
                plot_bgcolor: '#fff'
            }, { displayModeBar: false, responsive: true });
        });

        // ── Heatmap: functions × pods ──
        var heatEl = document.getElementById(base + '-heat');
        if (heatEl) {
            var fnMax = {};
            vllmPods.forEach(function(p) { (p.functions || []).forEach(function(f) { fnMax[f.name] = Math.max(fnMax[f.name] || 0, f.cum_pct); }); });
            var topFns = Object.keys(fnMax).sort(function(a, b) { return fnMax[b] - fnMax[a]; }).slice(0, 20);
            if (topFns.length) {
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
    }

    function renderCrossTest(ctx, eppTests) {
        var el = document.getElementById(ctx.chartId('pprof-epp-cmp'));
        if (!el) return;
        var sorted = eppTests.slice().sort(function(a, b) { return (a.pprof.epp.cpu_pct || 0) - (b.pprof.epp.cpu_pct || 0); });
        // Differentiate duplicate config names by appending the test_id suffix —
        // Plotly merges bars with identical y values (sums them into one bar)
        var labelCount = {};
        sorted.forEach(function(e) {
            var n = e.config_name || e.name || e.test_id;
            labelCount[n] = (labelCount[n] || 0) + 1;
        });
        var labels = sorted.map(function(e) {
            var n = e.config_name || e.name || e.test_id;
            if (labelCount[n] > 1) {
                var tid = (e.test_id || '').replace('step11-epp-aggregated-', '');
                return n + ' (' + tid + ')';
            }
            return n;
        }).reverse();
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
