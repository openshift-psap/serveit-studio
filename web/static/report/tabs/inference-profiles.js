// tabs/inference-profiles.js — Inference Pod Profiles (torch.profiler)
// vLLM's built-in torch profiler captures CPU (Python/aten ops) AND CUDA
// kernel activity during the benchmark window. Traces are Chrome trace
// format (.json.gz), viewable at ui.perfetto.dev or chrome://tracing.
(function() {
    var H = window.ReportHelpers;

    function entries(data) {
        return (data.all_results || []).filter(function(r) {
            return r.pprof && ((r.pprof.torch && r.pprof.torch.length) ||
                (r.pprof.files || []).some(function(f) { return f.kind === 'torch'; }));
        });
    }

    function testKey(e) { return e.test_id || e.config_name || 'x'; }
    function safeId(s) { return String(s).replace(/[^a-z0-9]/gi, ''); }
    function fmtKB(b) { return b > 1048576 ? (b / 1048576).toFixed(1) + 'MB' : (b / 1024).toFixed(0) + 'KB'; }
    function traceKind(name) {
        if (/^rank\d+/.test(name)) return { label: 'GPU (CUDA + CPU worker)', color: '#1565c0', icon: '&#127752;' };
        if (/async_llm/.test(name)) return { label: 'CPU (frontend / scheduler)', color: '#6a1b9a', icon: '&#128187;' };
        return { label: 'trace', color: '#37474f', icon: '&#128190;' };
    }

    function build(data, ctx) {
        var profEntries = entries(data);
        if (!profEntries.length) return '';

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #e65100;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ff9800);">&#128295; Inference Pod Profiles — torch.profiler (CPU + CUDA)</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'vLLM\'s built-in <a href="https://docs.pytorch.org/tutorials/recipes/recipe_profiler.html" target="_blank" style="color:#0e7490;">torch profiler</a> ran on each inference pod during the benchmark load window. ' +
            'It records both <b>Python/CPU operators</b> and <b>CUDA GPU kernels</b>. ' +
            'Each pod produces two trace files: <span style="font-family:monospace;color:#1565c0;">rank*</span> (GPU worker) and ' +
            '<span style="font-family:monospace;color:#6a1b9a;">async_llm*</span> (CPU frontend). ' +
            'Traces are Chrome trace format (gzipped) — open them at ' +
            '<a href="https://ui.perfetto.dev" target="_blank" style="color:#0e7490;">ui.perfetto.dev</a> or chrome://tracing ' +
            'for the full timeline, flame graph, and per-kernel statistics.</div></div>';

        profEntries.forEach(function(e) {
            s += buildTest(ctx, e);
        });
        return s;
    }

    function buildTest(ctx, e) {
        var key = testKey(e);
        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #f57c00;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ef6c00);">' +
            (e.config_name || e.test_id || '') + '</div>';

        var torchPods = e.pprof.torch || [];
        var tFiles = (e.pprof.files || []).filter(function(f) { return f.kind === 'torch'; });

        if (tFiles.length) {
            s += '<table class="results-table" style="font-size:0.82em; margin:12px 20px 8px; width:calc(100% - 40px);">' +
                '<tr><th>Pod</th><th>Trace</th><th>Type</th><th>Size</th><th>Download</th></tr>';
            tFiles.forEach(function(f) {
                var fname = (f.path || '').split('/').pop() || 'trace';
                var kind = traceKind(fname);
                var url = '/api/run/' + ctx.runId + '/config/' + encodeURIComponent(key) +
                    '/torch-trace/' + f.path;
                s += '<tr>' +
                    '<td style="font-family:monospace;">' + (f.pod || '?') + '</td>' +
                    '<td style="font-family:monospace; font-size:0.8em; word-break:break-all; max-width:280px;">' + fname + '</td>' +
                    '<td style="color:' + kind.color + '; white-space:nowrap;">' + kind.icon + ' ' + kind.label + '</td>' +
                    '<td>' + fmtKB(f.size || 0) + '</td>' +
                    '<td>' + (ctx.mode === 'live'
                        ? '<a href="' + url + '" style="color:#0e7490;text-decoration:none;font-size:11px;padding:3px 8px;background:#ecfeff;border-radius:4px;border:1px solid #a5f3fc;">&#8595; trace.gz</a>'
                        : '<span style="color:#94a3b8;font-size:0.85em;">server: ' + f.path + '</span>') +
                    '</td></tr>';
            });
            s += '</table>';
        } else if (torchPods.length) {
            // Traces listed in summaries but not copied to artifacts
            s += '<div style="margin:8px 20px; color:#475569; font-size:0.88em;">' +
                torchPods.length + ' pod(s) profiled — traces on pods at /tmp/torch_traces/</div>';
        }

        var totalTraces = tFiles.length;
        var gpuTraces = tFiles.filter(function(f) {
            return /^rank\d+/.test((f.path || '').split('/').pop() || '');
        }).length;
        s += '<div style="margin:4px 20px 12px; color:#64748b; font-size:0.82em;">' +
            totalTraces + ' trace file(s) collected (' + gpuTraces + ' GPU, ' + (totalTraces - gpuTraces) + ' CPU) — ' +
            'open in <a href="https://ui.perfetto.dev" target="_blank" style="color:#0e7490;">Perfetto</a> for analysis</div>';

        s += '</div>';
        return s;
    }

    function render(data, ctx) {
        // Traces are downloaded and viewed externally — no in-report charts
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
