// tabs/inference-profiles.js — Inference Pod Profiles (py-spy sampling profiler)
// Separate from EPP profiles. Shows Python-level function activity from the
// running vLLM server process in each inference pod.
(function() {
    var H = window.ReportHelpers;

    function entries(data) {
        return (data.all_results || []).filter(function(r) {
            return r.pprof && r.pprof.torch && r.pprof.torch.length;
        });
    }

    function testKey(e) { return e.test_id || e.config_name || 'x'; }
    function safeId(s) { return String(s).replace(/[^a-z0-9]/gi, ''); }

    function build(data, ctx) {
        var profEntries = entries(data);
        if (!profEntries.length) return '';

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #e65100;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ff9800);">🖥️ Inference Pod Profiles — py-spy</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Sampling profiler (py-spy) attached to the running vLLM server process in each pod during the benchmark load window. ' +
            'Shows which Python functions consume CPU time — tokenization, sampling, detokenization, and the main event loop. ' +
            'GPU/CUDA activity is not captured by py-spy (see vLLM Metrics tab for GPU utilization).</div></div>';

        profEntries.forEach(function(e) {
            s += buildTest(ctx, e, e.pprof.torch);
        });
        return s;
    }

    function buildTest(ctx, e, torchPods) {
        var base = ctx.chartId('inf-' + safeId(testKey(e)));
        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #f57c00;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ef6c00);">' +
            (e.config_name || e.test_id || '') + '</div>';

        // Per-pod operator charts (collapsible for performance)
        var totalOps = torchPods.reduce(function(s2, tp) {
            return s2 + Math.min((tp.operators || []).length, 10);
        }, 0);

        if (totalOps > 20) {
            s += '<details style="margin:8px 20px;"><summary style="cursor:pointer;font-weight:600;font-size:0.85em;color:#e65100;">' +
                'Per-pod function breakdown (' + totalOps + ' total rows — click to expand)</summary>';
        }

        torchPods.forEach(function(tp, vi) {
            var chartId = base + '-pod-' + vi;
            var top = (tp.operators || [])[0] || {};
            s += '<div style="margin:6px 20px; padding:8px 12px; background:#fff3e0; border-radius:6px; border-left:3px solid #e65100;">';
            s += '<div style="display:flex; justify-content:space-between; align-items:center; font-size:0.85em; margin-bottom:4px; flex-wrap:wrap; gap:6px;">';
            s += '<span style="font-family:monospace; font-weight:600; color:#334155;">' + (tp.pod || '?') + '</span>';
            s += '<span style="color:#475569; font-size:0.85em;">' +
                (tp.total_samples || 0) + ' samples · ' +
                ((tp.total_cpu_ms || 0) / 1000).toFixed(1) + 's CPU' +
                (top.name ? ' · top: ' + H.truncate(top.name, 30) : '') +
                '</span>';
            s += '</div>';
            s += '<div id="' + chartId + '" style="width:100%; height:' + Math.max(150, Math.min((tp.operators || []).length, 8) * 24 + 40) + 'px;"></div>';
            s += '</div>';
        });

        if (totalOps > 20) s += '</details>';

        // Summary table (top operators across all pods)
        var allOps = {};
        torchPods.forEach(function(tp) {
            (tp.operators || []).forEach(function(op) {
                if (!allOps[op.name]) allOps[op.name] = { name: op.name, count: 0, pods: 0 };
                allOps[op.name].count += op.calls || 0;
                allOps[op.name].pods++;
            });
        });
        var sorted = Object.values(allOps).sort(function(a, b) { return b.count - a.count; }).slice(0, 15);
        var totalSamples = torchPods.reduce(function(s2, tp) { return s2 + (tp.total_samples || 0); }, 0);

        if (sorted.length) {
            var tblId = base + '-tbl';
            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ff9800); font-size:0.9em; margin:12px 20px 0;">Top Functions Across All Pods</div>';
            s += '<table class="results-table" id="' + tblId + '" style="font-size:0.82em; margin:0 20px 8px;">' +
                '<tr><th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',0,\'str\')">Function &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',1,\'num\')">Samples &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',2,\'num\')">% of Total &#x21C5;</th>' +
                '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',3,\'num\')">Pods &#x21C5;</th></tr>';
            sorted.forEach(function(op) {
                var pct = totalSamples ? (100 * op.count / totalSamples).toFixed(1) : '0';
                s += '<tr><td style="font-family:monospace;font-size:0.85em;word-break:break-all;">' + op.name + '</td>' +
                    '<td data-val="' + op.count + '">' + op.count + '</td>' +
                    '<td data-val="' + pct + '">' + pct + '%</td>' +
                    '<td data-val="' + op.pods + '">' + op.pods + '/8</td></tr>';
            });
            s += '</table>';
        }

        // Add download links for torch traces
        var tFiles = (e.pprof.files || []).filter(function(f) { return f.kind === 'torch'; });
        if (tFiles.length) {
            s += '<div style="margin:8px 20px; display:flex; gap:6px; flex-wrap:wrap;">';
            tFiles.forEach(function(f) {
                var key = f.pod + '-' + f.ptype + '.json';
                s += ctx.profileAnchor(testKey(e), key,
                    f.pod + ' trace &#8595; (' + (f.size / 1024).toFixed(0) + 'KB)',
                    'Download py-spy trace for ' + f.pod);
            });
            s += '</div>';
        }

        s += '</div>';
        return s;
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var profEntries = entries(data);
        profEntries.forEach(function(e) {
            var base = ctx.chartId('inf-' + safeId(testKey(e)));
            (e.pprof.torch || []).forEach(function(tp, vi) {
                var el = document.getElementById(base + '-pod-' + vi);
                if (!el || !tp.operators || !tp.operators.length) return;
                var ops = tp.operators.slice(0, 8);
                var labels = ops.map(function(o) { return H.truncate(o.name, 50); }).reverse();
                var lM = H.leftMarginFor(labels, 6.6, 25);
                Plotly.newPlot(el, [{
                    y: labels,
                    x: ops.map(function(o) { return o.sample_pct || 0; }).reverse(),
                    type: 'bar', orientation: 'h',
                    marker: { color: '#e65100' },
                    text: ops.map(function(o) { return (o.sample_pct || 0).toFixed(1) + '%'; }).reverse(),
                    textposition: 'outside', textfont: { size: 10, color: '#334155' },
                    cliponaxis: false, constraintext: 'none',
                    hovertemplate: '<b>%{y}</b><br>%{x:.1f}% of samples<extra></extra>'
                }], {
                    margin: { l: lM, r: 60, t: 5, b: 25 },
                    xaxis: { title: '% of samples' },
                    yaxis: { automargin: true, tickfont: H.MONO_9 },
                    height: Math.max(150, ops.length * 24 + 40),
                    plot_bgcolor: '#fff'
                }, { displayModeBar: false, responsive: true });
            });
        });
    }

    ReportTabs.register({
        id: 'inference-profiles',
        label: 'Inference Profiles',
        icon: '&#128295;',
        position: 'after:epp-profiles',
        build: build,
        render: render
    });
})();
