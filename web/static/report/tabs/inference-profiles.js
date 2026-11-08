// tabs/inference-profiles.js — Inference Profiles (long-run cycles only)
//
// Cross-cycle comparison of the per-cycle torch.profiler summaries
// (CPU ops + CUDA kernels, aggregated across pods): per-cycle GPU/CPU
// totals and top-kernel/operator tables with per-cycle columns, so
// drift between iterations of the SAME workload is visible.
// Only appears for long-run runs (cycle configs, -cNNN test ids).
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

    function torchOf(r) {
        var t = (mj(r).pprof || {}).torch;
        return (t || []).filter(function(p) { return p.parsed; });
    }

    function groups(data) {
        var byBase = {};
        (data.all_results || []).forEach(function(r) {
            var m = (r.test_id || '').match(CYCLE_RE);
            if (!m) return;
            var pods = torchOf(r);
            if (!pods.length) return;
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

    // Shorten C++ template noise for table labels
    function shortName(name, max) {
        var s = String(name || '?');
        s = s.replace(/void /g, '').replace(/kernel_cutlass_kernel_vllm/g, 'cutlass.');
        if (/cutlass_3x_gemm_sm90_fp8/.test(s)) {
            var m = s.match(/C<(\d+)>,\s*cute::C<(\d+)>/);
            s = 'cutlass_gemm_fp8' + (m ? ' [' + m[1] + 'x' + m[2] + ']' : '') + ' GemmKernel';
        } else {
            var fm = s.match(/flash_attncuteflash_fwd_(sm90|combine)/);
            if (fm) s = 'cutlass.flash_attn_fwd_' + fm[1];
        }
        if (/triton_red_fused_/.test(s)) {
            s = 'triton.' + s.replace(/^.*triton_red_fused_/, '').split('_').slice(0, 6).join('_');
        }
        s = s.replace(/at::native::\(anonymous namespace\)::/g, 'at::native::')
             .replace(/flashinfer::sampling::/g, 'sampling.')
             .replace(/\([^)]*\)/g, '');
        var prev = null;
        while (prev !== s) { prev = s; s = s.replace(/<[^<>]*>/g, ''); }
        s = s.replace(/\s+/g, ' ').replace(/^[\s:,]+|[\s:,]+$/g, '');
        return H.truncate(s, max || 48);
    }

    // Aggregate a per-cycle metric across pods: {name -> {ms, n}}
    function aggKernels(pods, field) {
        var out = {};
        pods.forEach(function(p) {
            (p[field] || []).forEach(function(k) {
                var st = out[k.name];
                if (!st) out[k.name] = { name: k.name, ms: 0, n: 0 };
                out[k.name].ms += k.total_ms || 0;
                out[k.name].n += k.count || 0;
            });
        });
        return out;
    }

    function build(data, ctx) {
        var gs = groups(data);
        var bases = Object.keys(gs);
        if (!bases.length) return '';

        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #e65100;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ff9800);">&#128295; Inference Profiles — Cross-Cycle Comparison</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'torch.profiler summaries (CPU operators + CUDA kernels, summed across pods), one per long-run ' +
            'cycle of the identical workload. Per-cycle columns expose kernel-level drift between iterations — ' +
            'a kernel whose time grows cycle over cycle while the load is fixed is a regression signal.</div></div>';

        bases.forEach(function(base) {
            s += buildGroup(ctx, base, gs[base]);
        });
        return s;
    }

    function buildGroup(ctx, base, rows) {
        var id = ctx.chartId('inf-' + safeId(base));
        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #f57c00;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ef6c00);">' +
            base + ' — ' + rows.length + ' cycles</div>';

        // Totals badges (summed across cycles for context)
        var totGpu = 0, totCpu = 0, totK = 0;
        rows.forEach(function(r) {
            torchOf(r).forEach(function(p) {
                totGpu += p.total_cuda_ms || 0;
                totCpu += p.total_cpu_ms || 0;
                totK += p.kernel_count || 0;
            });
        });
        s += '<div style="display:flex; gap:12px; flex-wrap:wrap; margin:12px 20px 4px; font-size:0.85em;">' +
            '<span style="background:#1565c0; color:#fff; padding:4px 12px; border-radius:12px;">&#127752; GPU time (all cycles): ' + (totGpu / 1000).toFixed(1) + 's</span>' +
            '<span style="background:#6a1b9a; color:#fff; padding:4px 12px; border-radius:12px;">&#128187; CPU time: ' + (totCpu / 1000).toFixed(1) + 's</span>' +
            '<span style="background:#37474f; color:#fff; padding:4px 12px; border-radius:12px;">&#9889; ' + (totK / 1000).toFixed(0) + 'K kernels</span>' +
            '</div>';

        // Per-cycle totals chart
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#e65100,#ff9800); font-size:0.9em; margin:14px 20px 0;">GPU vs CPU Time per Cycle (sum across pods)</div>';
        s += '<div id="' + id + '-pods" style="margin:0 20px; width:calc(100% - 40px);"></div>';

        // Top kernels cross-cycle table
        s += buildComparisonTable(ctx, id, rows, 'top_kernels', 'Top GPU Kernels per Cycle (CUDA ms, all pods)',
                                  '#1565c0', 'ms');
        // Top CPU ops cross-cycle table
        s += buildComparisonTable(ctx, id, rows, 'top_cpu_ops', 'Top CPU Operations per Cycle (ms, all pods)',
                                  '#6a1b9a', 'ms');

        s += '</div>';
        return s;
    }

    function buildComparisonTable(ctx, id, rows, field, title, color, unit) {
        // Union of entries across cycles, ranked by the LAST cycle's total ms
        var stats = {};
        rows.forEach(function(r) {
            var cyc = r._cycle;
            var agg = aggKernels(torchOf(r), field);
            Object.keys(agg).forEach(function(name) {
                var st = stats[name];
                if (!st) st = stats[name] = { name: name, byCycle: {} };
                st.byCycle[cyc] = agg[name];
            });
        });
        var lastCycle = rows[rows.length - 1]._cycle;
        var list = Object.keys(stats).map(function(k) { return stats[k]; });
        list.sort(function(a, b) {
            var av = (a.byCycle[lastCycle] || {}).ms || 0;
            var bv = (b.byCycle[lastCycle] || {}).ms || 0;
            return bv - av;
        });
        list = list.slice(0, 12);
        if (!list.length) return '';

        var s = '';
        var tblId = id + '-' + field;
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,' + color + ',#' +
             (field === 'top_kernels' ? '42a5f5' : 'ab47bc') + '); font-size:0.9em; margin:14px 20px 0;">' + title + '</div>';
        // 50 cycles = ~52 columns — scroll horizontally, name column pinned
        s += '<div style="margin:0 20px 8px; overflow-x:auto; max-width:calc(100% - 40px);">';
        s += '<table class="results-table" id="' + tblId + '" style="font-size:0.8em; width:max-content; min-width:100%;">' +
            '<tr><th style="position:sticky; left:0; z-index:2; min-width:240px;">' + (field === 'top_kernels' ? 'Kernel' : 'Operation') + '</th>';
        rows.forEach(function(r) { s += '<th>c' + r._cycle + '</th>'; });
        s += '<th>&#916;%</th></tr>';

        list.forEach(function(st) {
            var first = null, last = null;
            s += '<tr><td style="font-family:monospace; font-size:0.82em; position:sticky; left:0; background:#fff; z-index:1; white-space:nowrap;" title="' +
                 String(st.name).replace(/"/g, '&quot;') + '">' + shortName(st.name, 45) + '</td>';
            rows.forEach(function(r) {
                var e = st.byCycle[r._cycle];
                var ms = e ? e.ms : null;
                if (ms !== null) {
                    if (first === null) first = ms;
                    last = ms;
                }
                s += '<td style="text-align:right;" data-val="' + (ms || 0) + '">' +
                     (ms !== null ? (ms >= 1000 ? (ms / 1000).toFixed(2) + 's' : Math.round(ms) + unit) : '—') + '</td>';
            });
            var delta = (first !== null && last !== null && first > 0) ? (100 * (last - first) / first) : null;
            var dColor = delta === null ? '#64748b' : (delta > 30 ? '#dc2626' : (delta < -30 ? '#16a34a' : '#64748b'));
            s += '<td style="text-align:right; color:' + dColor + ';">' +
                 (delta !== null ? (delta > 0 ? '+' : '') + delta.toFixed(0) + '%' : '—') + '</td></tr>';
        });
        s += '</table></div>';
        return s;
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var gs = groups(data);
        Object.keys(gs).forEach(function(base) {
            var rows = gs[base];
            var id = ctx.chartId('inf-' + safeId(base));
            var el = document.getElementById(id + '-pods');
            if (!el) return;
            var xs = rows.map(function(r) { return 'c' + r._cycle; });
            var gpu = rows.map(function(r) {
                return torchOf(r).reduce(function(a, p) { return a + (p.total_cuda_ms || 0); }, 0) / 1000;
            });
            var cpu = rows.map(function(r) {
                return torchOf(r).reduce(function(a, p) { return a + (p.total_cpu_ms || 0); }, 0) / 1000;
            });
            Plotly.newPlot(el, [
                { x: xs, y: gpu, name: 'GPU (CUDA)', type: 'bar', marker: { color: '#1565c0' } },
                { x: xs, y: cpu, name: 'CPU', type: 'bar', marker: { color: '#6a1b9a' } }
            ], {
                barmode: 'group',
                margin: { l: 55, r: 20, t: 8, b: 28 },
                yaxis: { title: 'seconds' },
                plot_bgcolor: '#fff', height: 230,
                legend: { orientation: 'h', y: 1.15 }
            }, { displayModeBar: false, responsive: true });
        });
    }

    ReportTabs.register({
        id: 'inference-profiles',
        label: 'Inference Profiles',
        icon: '&#128295;',
        position: 'after:pod-profiles',
        build: build,
        render: render
    });
})();
