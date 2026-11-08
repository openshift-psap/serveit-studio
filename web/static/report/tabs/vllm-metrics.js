// tabs/vllm-metrics.js — vLLM Engine Metrics report tab. One implementation for live + offline.
(function() {

    var ARCH_COLORS = { aggregated: '#3b82f6', pd: '#f59e0b', ep: '#10b981' };
    var ARCH_LABELS = { aggregated: 'Aggregated', pd: 'PD', ep: 'EP' };
    var VLLM_LAYOUT = { margin: { t: 30, b: 100, l: 60, r: 20 }, height: 850, showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' }, plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff' };

    function getArch(cfg) {
        var c = (cfg || '').toLowerCase();
        if (c.indexOf('pd') >= 0) return 'pd';
        if (c.indexOf('ep') >= 0) return 'ep';
        return 'aggregated';
    }

    function build(data, ctx) {
        var vllm = (data.charts || {}).vllm;
        if (!vllm || !vllm.configs || !vllm.configs.length) return '';
        var chartCard = function(title, desc, id) {
            return '<div class="chart-card"><div class="chart-card-header">' + title + '</div>' +
                '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' + desc + '</div>' +
                '<div class="chart-card-body"><div id="' + ctx.chartId(id) + '" style="width:100%;height:850px;"></div></div></div>';
        };
        var s = '<div class="chart-card" style="margin-top:24px; border-left:6px solid #8b5cf6;">';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#6366f1); color:white; font-size:1.2em;">vLLM Engine Metrics</div>';
        s += '<div style="padding:8px 20px; color:#1e293b; font-size:0.95em;">Prometheus metrics collected from the vLLM engine during each test. These metrics show engine-level behavior across all configurations — latency distributions, token throughput, request queuing, and processing time breakdown.</div>';
        s += '</div>';
        s += chartCard('vLLM TTFT Percentiles', 'Time-to-First-Token as reported by the vLLM engine histogram (averaged over the test window). Lower bars mean faster first-token delivery. Compare P50 (typical) vs P99 (worst-case) across configurations.', 'chart-vllm-ttft');
        s += chartCard('vLLM ITL Percentiles', 'Inter-Token Latency from vLLM engine histograms. This is the delay between consecutive generated tokens — it determines how "smooth" streaming feels to the user. Lower is better.', 'chart-vllm-itl');
        s += chartCard('vLLM E2E Request Latency', 'End-to-end request latency from vLLM (includes TTFT + all token generation). Shows the full time a request spends in the engine. Compare tail latency (P99) across configurations to spot saturation.', 'chart-vllm-e2e');
        s += chartCard('Token Throughput', 'Average prompt (input) and generation (output) token processing rates across all pods. Higher bars = more tokens processed per second. Generation rate directly impacts how many users can be served concurrently.', 'chart-vllm-tokens');
        s += chartCard('Request Queue & KV Cache', 'Average concurrent requests running and waiting in queue, plus KV cache utilization (%). High waiting counts indicate the engine is saturated. High KV cache usage means the model is near memory capacity.', 'chart-vllm-queue');
        s += chartCard('Processing Time & Preemptions', 'How engine time is split between prefill (prompt processing), decode (token generation), and queuing. Preemptions show how often the engine evicts running requests to make room — high preemptions indicate memory pressure.', 'chart-vllm-time');
        return s;
    }

    function plot(ctx, id, traces, layout) {
        var el = document.getElementById(ctx.chartId(id));
        if (el) Plotly.newPlot(el, traces, layout, { responsive: true });
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var vllm = (data.charts || {}).vllm;
        if (!vllm || !vllm.configs || !vllm.configs.length) return;

        function makeArchLines(metric, unit) {
            var byArch = {};
            var bestByCfg = {};
            vllm.configs.forEach(function(cfg, i) {
                var existing = bestByCfg[cfg];
                if (!existing || (metric.p90[i] || 1e9) < (existing.p90 || 1e9)) {
                    bestByCfg[cfg] = { cfg: cfg, p50: metric.p50[i], p90: metric.p90[i], p95: metric.p95[i], p99: metric.p99[i] };
                }
            });
            Object.keys(bestByCfg).forEach(function(cfg) {
                var a = getArch(cfg);
                if (!byArch[a]) byArch[a] = [];
                byArch[a].push(bestByCfg[cfg]);
            });
            var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = byArch[a];
                if (!pts || !pts.length) return;
                var color = ARCH_COLORS[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return p.p90; }),
                    name: ARCH_LABELS[a] + ' P90', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>P90: %{y:,.0f} ' + unit + '<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return p.p99; }),
                    name: ARCH_LABELS[a] + ' P99', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color, symbol: 'diamond' },
                    hovertemplate: '<b>%{x}</b><br>P99: %{y:,.0f} ' + unit + '<extra></extra>' });
            });
            return traces;
        }

        function dedupByConfig() {
            var best = {};
            vllm.configs.forEach(function(cfg, i) { if (!best[cfg]) best[cfg] = i; });
            var cfgs = Object.keys(best);
            var byArch = {};
            cfgs.forEach(function(cfg) {
                var a = getArch(cfg);
                if (!byArch[a]) byArch[a] = [];
                byArch[a].push({ cfg: cfg, idx: best[cfg] });
            });
            return { cfgs: cfgs, byArch: byArch };
        }

        plot(ctx, 'chart-vllm-ttft', makeArchLines(vllm.ttft, 'ms'),
            Object.assign({}, VLLM_LAYOUT, { xaxis: { tickangle: -35, automargin: true }, yaxis: { title: 'TTFT (ms) — lower is better' } }));
        plot(ctx, 'chart-vllm-itl', makeArchLines(vllm.itl, 'ms'),
            Object.assign({}, VLLM_LAYOUT, { xaxis: { tickangle: -35, automargin: true }, yaxis: { title: 'ITL (ms) — lower is better' } }));
        plot(ctx, 'chart-vllm-e2e', makeArchLines(vllm.e2e, 's'),
            Object.assign({}, VLLM_LAYOUT, { xaxis: { tickangle: -35, automargin: true }, yaxis: { title: 'E2E Latency (s) — lower is better' } }));

        (function() {
            var dd = dedupByConfig(); var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a]; if (!pts || !pts.length) return;
                var color = ARCH_COLORS[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.token_rates.prompt[p.idx]; }),
                    name: ARCH_LABELS[a] + ' Prompt', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                    hovertemplate: '<b>%{x}</b><br>Prompt: %{y:,.0f} tok/s<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.token_rates.generation[p.idx]; }),
                    name: ARCH_LABELS[a] + ' Generation', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Generation: %{y:,.0f} tok/s<extra></extra>' });
            });
            plot(ctx, 'chart-vllm-tokens', traces, Object.assign({}, VLLM_LAYOUT, { xaxis: { tickangle: -35, automargin: true }, yaxis: { title: 'Tokens/second — higher is better' } }));
        })();

        (function() {
            var dd = dedupByConfig(); var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a]; if (!pts || !pts.length) return;
                var color = ARCH_COLORS[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.request_state.running[p.idx]; }),
                    name: ARCH_LABELS[a] + ' Running', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Running: %{y:.1f}<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.request_state.waiting[p.idx]; }),
                    name: ARCH_LABELS[a] + ' Waiting', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color, symbol: 'diamond' },
                    hovertemplate: '<b>%{x}</b><br>Waiting: %{y:.1f}<extra></extra>' });
            });
            traces.push({ x: dd.cfgs, y: dd.cfgs.map(function(c) { return vllm.request_state.kv_cache[dedupByConfig().byArch[getArch(c)].find(function(p) { return p.cfg === c; }).idx]; }),
                name: 'KV Cache %', mode: 'lines+markers', yaxis: 'y2', line: { color: '#f59e0b', width: 2 }, marker: { size: 10, color: '#f59e0b', symbol: 'diamond' },
                hovertemplate: '<b>%{x}</b><br>KV Cache: %{y:.1f}%<extra></extra>' });
            plot(ctx, 'chart-vllm-queue', traces, Object.assign({}, VLLM_LAYOUT, {
                margin: Object.assign({}, VLLM_LAYOUT.margin, { r: 60 }), xaxis: { tickangle: -35, automargin: true },
                yaxis: { title: 'Request Count (avg)', side: 'left' },
                yaxis2: { title: 'KV Cache Usage (%)', side: 'right', overlaying: 'y', range: [0, 105], titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } }
            }));
        })();

        (function() {
            var dd = dedupByConfig(); var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a]; if (!pts || !pts.length) return;
                var color = ARCH_COLORS[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.time_breakdown.prefill[p.idx]; }),
                    name: ARCH_LABELS[a] + ' Prefill', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Prefill: %{y:.2f}<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.time_breakdown.decode[p.idx]; }),
                    name: ARCH_LABELS[a] + ' Decode', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                    hovertemplate: '<b>%{x}</b><br>Decode: %{y:.2f}<extra></extra>' });
            });
            var allPts = [];
            Object.keys(dd.byArch).forEach(function(a) { dd.byArch[a].forEach(function(p) { allPts.push(p); }); });
            traces.push({ x: allPts.map(function(p) { return p.cfg; }), y: allPts.map(function(p) { return vllm.time_breakdown.preemptions[p.idx]; }),
                name: 'Preemptions/s', mode: 'markers', yaxis: 'y2', marker: { size: 10, color: '#ef4444', symbol: 'triangle-up' },
                hovertemplate: '<b>%{x}</b><br>Preemptions: %{y:.1f}/s<extra></extra>' });
            plot(ctx, 'chart-vllm-time', traces, Object.assign({}, VLLM_LAYOUT, {
                margin: Object.assign({}, VLLM_LAYOUT.margin, { r: 60 }), xaxis: { tickangle: -35, automargin: true },
                yaxis: { title: 'Time Rate (s/s)', side: 'left' },
                yaxis2: { title: 'Preemptions/s', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } }
            }));
        })();
    }

    ReportTabs.register({ id: 'vllm-metrics', label: 'vLLM Metrics', icon: '&#9889;', position: 'after:lora-serving', build: build, render: render });
})();
