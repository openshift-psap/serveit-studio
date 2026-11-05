// tabs/inference-profiles.js — Inference Pod Profiles (torch.profiler)
// vLLM's built-in torch profiler captures CPU operators and CUDA kernels
// during the benchmark window. Traces are parsed in-pod into compact
// summaries (top kernels, CPU ops, totals) and visualized here — the raw
// 30-80MB traces are never stored.
(function() {
    var H = window.ReportHelpers;

    function entries(data) {
        return (data.all_results || []).filter(function(r) {
            return r.pprof && r.pprof.torch && r.pprof.torch.length;
        });
    }

    function testKey(e) { return e.test_id || e.config_name || 'x'; }
    function safeId(s) { return String(s).replace(/[^a-z0-9]/gi, ''); }
    function fmtMs(ms) { return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms'; }
    function fmtK(n) { return n >= 1000 ? (n / 1000).toFixed(1) + 'K' : String(n); }
    function shortName(name, max) {
        var s = String(name || '?');
        s = s.replace(/void /g, '').replace(/kernel_cutlass_kernel_vllm/g, 'cutlass.');
        s = s.replace(/cutlass::device_kernel</g, '');
        s = s.replace(/vllm::cutlass_3x_gemm_sm90_fp8<[^>]*>/g, 'cutlass_gemm_fp8');
        s = s.replace(/cutlass::float_e4m3_t/g, 'fp8').replace(/cutlass::bfloat16_t/g, 'bf16');
        s = s.replace(/cute::tuple<[^>]*>/g, '');
        s = s.replace(/cutlass::gemm::[A-Za-z]+/g, '');
        s = s.replace(/cutlass::epilogue::[A-Za-z]+/g, '');
        s = s.replace(/<\s*,?\s*>/g, '').replace(/<>+/g, '');
        s = s.replace(/\s+/g, ' ').replace(/^[\s:,]+|[\s:,]+$/g, '');
        return H.truncate(s, max || 42);
    }

    function build(data, ctx) {
        var profEntries = entries(data);
        if (!profEntries.length) return '';

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #e65100;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ff9800);">&#128295; Inference Pod Profiles — torch.profiler (CPU + CUDA)</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'vLLM\'s built-in <a href="https://docs.pytorch.org/tutorials/recipes/recipe_profiler.html" target="_blank" style="color:#0e7490;">torch profiler</a> ' +
            'ran on each inference pod during the benchmark window (bounded to 500 engine steps after ramp-up), capturing ' +
            '<b>Python/CPU operators</b> and <b>CUDA GPU kernels</b>. Traces are parsed in-pod into compact summaries — ' +
            'the charts below show where GPU and CPU time was spent per pod.</div></div>';

        profEntries.forEach(function(e) {
            s += buildTest(ctx, e);
        });
        return s;
    }

    function buildTest(ctx, e) {
        var key = testKey(e);
        var base = ctx.chartId('inf-' + safeId(key));
        var pods = (e.pprof.torch || []).filter(function(p) { return p.parsed; });
        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #f57c00;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ef6c00);">' +
            (e.config_name || e.test_id || '') + '</div>';

        if (!pods.length) {
            s += '<div style="margin:10px 20px; color:#64748b; font-size:0.88em;">No parsed profiles for this test.</div></div>';
            return s;
        }

        var totCuda = 0, totCpu = 0, totKernels = 0, totEvents = 0;
        pods.forEach(function(p) {
            totCuda += p.total_cuda_ms || 0;
            totCpu += p.total_cpu_ms || 0;
            totKernels += p.kernel_count || 0;
            totEvents += p.total_events || 0;
        });

        s += '<div style="display:flex; gap:14px; flex-wrap:wrap; margin:12px 20px 4px; font-size:0.85em;">' +
            '<span style="background:#1565c0; color:#fff; padding:4px 12px; border-radius:12px;">&#127752; GPU time: ' + fmtMs(totCuda) + '</span>' +
            '<span style="background:#6a1b9a; color:#fff; padding:4px 12px; border-radius:12px;">&#128187; CPU time: ' + fmtMs(totCpu) + '</span>' +
            '<span style="background:#37474f; color:#fff; padding:4px 12px; border-radius:12px;">&#9889; ' + fmtK(totKernels) + ' kernels</span>' +
            '<span style="background:#546e7a; color:#fff; padding:4px 12px; border-radius:12px;">&#128300; ' + fmtK(totEvents) + ' events</span>' +
            '<span style="background:#455a64; color:#fff; padding:4px 12px; border-radius:12px;">' + pods.length + ' pods</span>' +
            '</div>';

        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ff9800); font-size:0.9em; margin:14px 20px 0;">GPU vs CPU Time per Pod</div>';
        s += '<div id="' + base + '-pods" style="margin:0 20px; width:calc(100% - 40px);"></div>';

        var kern = {};
        pods.forEach(function(p) {
            (p.top_kernels || []).forEach(function(k) {
                if (!kern[k.name]) kern[k.name] = { name: k.name, ms: 0, n: 0 };
                kern[k.name].ms += k.total_ms || 0;
                kern[k.name].n += k.count || 0;
            });
        });
        var topK = Object.values(kern).sort(function(a, b) { return b.ms - a.ms; }).slice(0, 12);
        if (topK.length) {
            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#1565c0,#42a5f5); font-size:0.9em; margin:14px 20px 0;">Top GPU Kernels (all pods, by total time)</div>';
            s += '<div id="' + base + '-kern" style="margin:0 20px; width:calc(100% - 40px);"></div>';
        }

        var cops = {};
        pods.forEach(function(p) {
            (p.top_cpu_ops || []).forEach(function(k) {
                if (!cops[k.name]) cops[k.name] = { name: k.name, ms: 0, n: 0 };
                cops[k.name].ms += k.total_ms || 0;
                cops[k.name].n += k.count || 0;
            });
        });
        var topC = Object.values(cops).sort(function(a, b) { return b.ms - a.ms; }).slice(0, 12);
        if (topC.length) {
            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#6a1b9a,#ab47bc); font-size:0.9em; margin:14px 20px 0;">Top CPU Operations (all pods)</div>';
            s += '<div id="' + base + '-cpu" style="margin:0 20px; width:calc(100% - 40px);"></div>';
        }

        var tblId = base + '-tbl';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ff9800); font-size:0.9em; margin:14px 20px 0;">Per-Pod Summary</div>';
        s += '<table class="results-table" id="' + tblId + '" style="font-size:0.82em; margin:0 20px 8px; width:calc(100% - 40px);">' +
            '<tr><th>Pod</th><th>GPU Time</th><th>CPU Time</th><th>Kernels</th><th>Events</th><th>Top Kernel</th></tr>';
        pods.forEach(function(p) {
            var tk = (p.top_kernels || [])[0] || {};
            s += '<tr><td style="font-family:monospace;">' + (p.pod || '?') + '</td>' +
                '<td>' + fmtMs(p.total_cuda_ms || 0) + '</td>' +
                '<td>' + fmtMs(p.total_cpu_ms || 0) + '</td>' +
                '<td data-val="' + (p.kernel_count || 0) + '">' + fmtK(p.kernel_count || 0) + '</td>' +
                '<td data-val="' + (p.total_events || 0) + '">' + fmtK(p.total_events || 0) + '</td>' +
                '<td style="font-family:monospace; font-size:0.8em;">' + shortName(tk.name, 38) +
                ' <span style="color:#64748b;">(' + (tk.pct || 0) + '%)</span></td></tr>';
        });
        s += '</table>';

        s += '</div>';
        return s;
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        entries(data).forEach(function(e) {
            var base = ctx.chartId('inf-' + safeId(testKey(e)));
            var pods = (e.pprof.torch || []).filter(function(p) { return p.parsed; });
            if (!pods.length) return;

            var el = document.getElementById(base + '-pods');
            if (el) {
                var labels = pods.map(function(p) { return (p.pod || '?').replace(/^aggregated-|^pd-/, ''); });
                var lM = H.leftMarginFor(labels, 6.6, 22);
                Plotly.newPlot(el, [
                    { y: labels, x: pods.map(function(p) { return (p.total_cuda_ms || 0) / 1000; }),
                      name: 'GPU (CUDA)', type: 'bar', orientation: 'h', marker: { color: '#1565c0' } },
                    { y: labels, x: pods.map(function(p) { return (p.total_cpu_ms || 0) / 1000; }),
                      name: 'CPU', type: 'bar', orientation: 'h', marker: { color: '#6a1b9a' } }
                ], {
                    barmode: 'group',
                    margin: { l: lM, r: 30, t: 5, b: 28 },
                    xaxis: { title: 'seconds' },
                    yaxis: { automargin: true, tickfont: H.MONO_9 },
                    height: Math.max(160, pods.length * 22 + 50),
                    plot_bgcolor: '#fff', legend: { orientation: 'h', y: 1.12 }
                }, { displayModeBar: false, responsive: true });
            }

            var kern = {};
            pods.forEach(function(p) {
                (p.top_kernels || []).forEach(function(k) {
                    if (!kern[k.name]) kern[k.name] = { name: k.name, ms: 0, n: 0 };
                    kern[k.name].ms += k.total_ms || 0;
                    kern[k.name].n += k.count || 0;
                });
            });
            var topK = Object.values(kern).sort(function(a, b) { return b.ms - a.ms; }).slice(0, 10);
            el = document.getElementById(base + '-kern');
            if (el && topK.length) {
                var kLabels = topK.map(function(k) { return shortName(k.name, 40); }).reverse();
                var kM = H.leftMarginFor(kLabels, 6.6, 25);
                Plotly.newPlot(el, [{
                    y: kLabels,
                    x: topK.map(function(k) { return k.ms / 1000; }).reverse(),
                    type: 'bar', orientation: 'h', marker: { color: '#1565c0' },
                    text: topK.map(function(k) { return (k.ms / 1000).toFixed(2) + 's · ' + fmtK(k.n) + 'x'; }).reverse(),
                    textposition: 'outside', textfont: { size: 10, color: '#334155' },
                    cliponaxis: false, constraintext: 'none',
                    hovertemplate: '<b>%{y}</b><br>%{x:.2f}s GPU time<extra></extra>'
                }], {
                    margin: { l: kM, r: 90, t: 5, b: 28 },
                    xaxis: { title: 'GPU seconds' },
                    yaxis: { automargin: true, tickfont: H.MONO_9 },
                    height: Math.max(150, topK.length * 24 + 45),
                    plot_bgcolor: '#fff'
                }, { displayModeBar: false, responsive: true });
            }

            var cops = {};
            pods.forEach(function(p) {
                (p.top_cpu_ops || []).forEach(function(k) {
                    if (!cops[k.name]) cops[k.name] = { name: k.name, ms: 0, n: 0 };
                    cops[k.name].ms += k.total_ms || 0;
                    cops[k.name].n += k.count || 0;
                });
            });
            var topC = Object.values(cops).sort(function(a, b) { return b.ms - a.ms; }).slice(0, 10);
            el = document.getElementById(base + '-cpu');
            if (el && topC.length) {
                var cLabels = topC.map(function(k) { return shortName(k.name, 40); }).reverse();
                var cM = H.leftMarginFor(cLabels, 6.6, 25);
                Plotly.newPlot(el, [{
                    y: cLabels,
                    x: topC.map(function(k) { return k.ms / 1000; }).reverse(),
                    type: 'bar', orientation: 'h', marker: { color: '#6a1b9a' },
                    text: topC.map(function(k) { return (k.ms / 1000).toFixed(2) + 's · ' + fmtK(k.n) + 'x'; }).reverse(),
                    textposition: 'outside', textfont: { size: 10, color: '#334155' },
                    cliponaxis: false, constraintext: 'none',
                    hovertemplate: '<b>%{y}</b><br>%{x:.2f}s CPU time<extra></extra>'
                }], {
                    margin: { l: cM, r: 90, t: 5, b: 28 },
                    xaxis: { title: 'CPU seconds' },
                    yaxis: { automargin: true, tickfont: H.MONO_9 },
                    height: Math.max(150, topC.length * 24 + 45),
                    plot_bgcolor: '#fff'
                }, { displayModeBar: false, responsive: true });
            }
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
