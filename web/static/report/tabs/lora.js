// tabs/lora.js — LoRA Serving report tab. One implementation for live + offline.
(function() {

    function loraAffCells(pd) {
        if (!pd || !(pd.total_adapters > 0)) {
            return { single: '—', multi: '—', share: '—', verdict: '—', color: '#64748b' };
        }
        var total = pd.total_adapters;
        var conc = pd.concentrated || 0, scat = pd.scattered || 0;
        var concPct = Math.round(100 * conc / total), scatPct = Math.round(100 * scat / total);
        var share = pd.top_pod_share;
        var verdict = (share === null || share === undefined)
            ? (concPct >= 80 ? 'adapter-aware' : (concPct >= 50 ? 'partial' : 'random'))
            : (share >= 0.9 ? 'adapter-aware' : (share >= 0.5 ? 'partial' : 'random'));
        var color = verdict === 'adapter-aware' ? '#059669' : (verdict === 'partial' ? '#f59e0b' : '#ef4444');
        return {
            single: conc + ' (' + concPct + '%)',
            multi: scat + ' (' + scatPct + '%)',
            share: (share === null || share === undefined) ? '—' : Math.round(100 * share) + '%',
            verdict: verdict, color: color
        };
    }

    function cacheCell(pct) {
        if (pct === undefined || pct === null) return '—';
        var color = pct >= 20 ? '#059669' : (pct >= 5 ? '#f59e0b' : '#ef4444');
        return '<span style="color:' + color + ';font-weight:600;">' + pct.toFixed(1) + '%</span>';
    }

    var _renders = [];

    function build(data, ctx) {
        var charts = data.charts || {};
        var s = '';
        var _sfx = '';
        if (ctx.mode === 'live' && typeof _chartSuffix !== 'undefined') _sfx = _chartSuffix;
        _renders = [];
    if (data.lora_sweep) {
        var lsweep = data.lora_sweep;
        Object.keys(lsweep).forEach(function(skey) {
            var pts = lsweep[skey];
            if (!pts || !pts.length) return;
            var lbl = pts[0].config_label || skey;
            // Sweep levels joined to their adapter-affinity measurements
            // (by test_id) for the Single/Multi-Pod table columns
            var affByTest = {};
            (charts.vllm.lora_pod_details || []).forEach(function(pd) {
                if (pd && pd.test_id) affByTest[pd.test_id] = pd;
            });
            var _baseOnly = ((data.run_config || {}).lora_benchmark_mode === 'base_only');
            var rows = pts.map(function(p) {
                // Capacity-prediction flag: working set exceeds pool
                // residency. Meaningless in base-only mode — no request
                // targets an adapter, so nothing ever loads or swaps.
                var swap = (p.lru_swapping && !_baseOnly) ? ' <span style="color:#ef4444;">⚠️ LRU</span>' : '';
                var aff = loraAffCells(affByTest[p.test_id]);
                var evict = p.adapter_evictions;
                var evictCell = (evict === undefined || evict === null) ? '—'
                    : (evict > 0 ? '<span style="color:#ef4444;font-weight:600;">' + evict + '</span>' : '0');
                var resident = (p.gpu_resident || p.gpu_resident === 0)
                    ? (p.total_slots ? p.gpu_resident + '×' + (p.total_slots / (p.gpu_resident || 1) === Math.round(p.total_slots / (p.gpu_resident || 1)) ? (p.total_slots / (p.gpu_resident || 1)) : '?') : String(p.gpu_resident))
                    : '—';
                return '<tr>' +
                    '<td style="text-align:center;font-weight:700;">' + p.adapter_count + '</td>' +
                    '<td style="text-align:center;">' + resident + swap + '</td>' +
                    '<td style="text-align:center;">' + (p.ttft_p90 || 0).toFixed(0) + ' ms</td>' +
                    '<td style="text-align:center;">' + (p.throughput_mean || 0).toFixed(3) + ' req/s</td>' +
                    '<td style="text-align:center;">' + (p.itl_p90 || 0).toFixed(1) + ' ms</td>' +
                    '<td style="text-align:center;">' + cacheCell(p.cache_hit_pct) + '</td>' +
                    '<td style="text-align:center;">' + (p.output_tps_mean || 0).toFixed(0) + ' tok/s</td>' +
                    '<td style="text-align:center;">' + evictCell + '</td>' +
                    '<td style="text-align:center;color:#059669;">' + aff.single + '</td>' +
                    '<td style="text-align:center;color:#ef4444;">' + aff.multi + '</td>' +
                    '<td style="text-align:center;color:' + aff.color + ';font-weight:600;">' + aff.verdict + '</td>' +
                    '</tr>';
            }).join('');
            var sweepChartId = 'lora-sweep-chart-' + skey.replace(/[^a-z0-9]/gi, '-') + _sfx;
            var affChartId = 'lora-sweep-aff-chart-' + skey.replace(/[^a-z0-9]/gi, '-') + _sfx;
            // Sweep levels joined to their adapter-affinity measurements
            // (by test_id, joined above) — levels without affinity data
            // are skipped
            var affPts = [];
            pts.forEach(function(p) {
                var pd = affByTest[p.test_id];
                if (pd && (pd.total_adapters || 0) > 0) {
                    affPts.push({
                        adapter_count: p.adapter_count,
                        pct: Math.round(100 * (pd.concentrated || 0) / pd.total_adapters),
                        single: pd.concentrated || 0,
                        total: pd.total_adapters
                    });
                }
            });
            var affChartHtml = affPts.length >= 2
                ? '<div id="' + affChartId + '" style="width:100%;height:340px;margin-top:18px;"></div>'
                : '';
            // Adapter eviction chart: estimated loads vs evictions per level
            // (vLLM LRU replayed over the per-pod in-flight sequences). Only
            // when eviction data exists for at least one level.
            var evictChartId = 'lora-sweep-evict-chart-' + skey.replace(/[^a-z0-9]/gi, '-') + _sfx;
            var evictPts = pts.filter(function(p) {
                return p.adapter_evictions !== undefined && p.adapter_evictions !== null;
            });
            var evictChartHtml = evictPts.length >= 2
                ? '<div class="chart-card" style="border-left:6px solid #b91c1c;margin-top:18px;">' +
                    '<div class="chart-card-header" style="background:linear-gradient(135deg,#b91c1c,#ef4444); color:white;">Adapter Evictions — ' + lbl + '</div>' +
                    '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
                    'How often a serving pod had to <strong>drop a GPU-resident adapter to make room for another one</strong> (LRU eviction) during each sweep level. ' +
                    'Adapter weights occupy residency slots (<code>max_loras</code> per pod); loading a new adapter once all slots are taken evicts the least-recently-used one, and the evicted adapter\u2019s next request pays a disk reload (~100\u2013300ms). ' +
                    'vLLM exposes no eviction counter, so this is <strong>estimated by replaying vLLM\u2019s LRU policy</strong> over each pod\u2019s in-flight adapter sequence (<code>vllm:lora_requests_info</code>, 5s scrape resolution — a lower bound).<br>' +
                    '<strong>Gray bar (loads)</strong> — adapter activations from disk. ' +
                    '<strong>Red bar (evictions)</strong> — LRU removals under capacity pressure. ' +
                    '<strong>Dashed line</strong> — pool residency capacity (pods × slots per pod). ' +
                    'Healthy sticky routing keeps <strong>red at zero left of the line</strong> (each adapter lives on one pod, working set = adapters/pods). ' +
                    'Red bars <em>left</em> of the line mean a routing leak — adapters scattered across pods inflated every pod\u2019s working set past its slots. ' +
                    'Red rising <em>right</em> of the line is genuine pool overflow: the working set no longer fits, and every miss thrashes.' +
                    '<div id="' + evictChartId + '" style="width:100%;height:340px;margin-top:10px;"></div>' +
                    '</div></div>'
                : '';
            s += '<div class="chart-card" style="border-left:6px solid #4f46e5;">' +
                '<div class="chart-card-header" style="background:linear-gradient(135deg,#4f46e5,#818cf8); color:white;">LoRA Adapter Sweep — ' + lbl + '</div>' +
                '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
                'Multi-tenant scaling curve: the best configuration re-tested at increasing <strong>adapter counts</strong>, ' +
                'with benchmark requests round-robined across the base model and all adapters. ' +
                '<strong>Reading the chart:</strong> ' +
                '<strong>dotted green line</strong> — throughput (tokens/s, left axis; dotted to distinguish it from the latency traces on the right axis). ' +
                '<strong>Δ% labels</strong> — throughput change vs the 0-adapter baseline (green = improvement, red = degradation). ' +
                '<strong>Red dashed vertical line + shaded region</strong> — the LRU overflow boundary: right of it the adapter working set exceeds total pool residency (Resident Slots product below), so vLLM must swap adapters in and out and latency degrades; the Evictions column measures how much.' +
                '<div id="' + sweepChartId + '" style="width:100%;height:520px;margin-top:10px;"></div>' +
                affChartHtml +
                '<table id="lora-sweep-table-' + skey + _sfx + '" style="width:100%;border-collapse:collapse;margin-top:10px;font-size:0.9em;background:#fff;">' +
                '<tr style="background:#eef2ff;">' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',0,\'num\')">Adapters ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',1,\'num\')">Resident Slots ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',2,\'num\')">TTFT p90 ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',3,\'num\')">Throughput ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',4,\'num\')">ITL p90 ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',5,\'num\')">Cache-Hit ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',6,\'num\')">Output ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',7,\'num\')">Evictions ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',8,\'num\')">Single-Pod ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',9,\'num\')">Multi-Pod ⇅</th>' +
                    '<th style="padding:6px;cursor:pointer;" onclick="sortReportTable(\'lora-sweep-table-' + skey + _sfx + '\',10,\'str\')">Routing Behavior ⇅</th></tr>' +
                rows + '</table>' +
                '<div style="font-size:0.85em;color:#64748b;margin-top:8px;line-height:1.7;">' +
                '<strong>Resident slots</strong> — adapter slots per pod × serving pods (13×8 = 13 slots on each of 8 pods = 104 total pool residency). ' +
                '<strong>⚠️ LRU</strong> — the adapter working set exceeds total pool residency, so vLLM must evict a least-recently-used adapter to load each new one; the Evictions column measures how often that happened. ' +
                '<strong>Cache-hit</strong> — prefix-cache hit rate: vLLM keys cache blocks by (prefix, adapter), so spreading requests across adapters multiplies the key space and collapses the hit rate — every miss re-prefills the full prompt. ' +
                '<strong>Evictions</strong> — estimated LRU removals (vLLM exposes no counter; replayed from the per-pod in-flight sequences).' +
                '</div></div></div>' + evictChartHtml;

            if (affPts.length >= 2) {
                (function(affPts, chartId) {
                    _renders.push(function() {
                        var annotations = affPts.map(function(p) {
                            var col = p.pct >= 80 ? '#059669' : (p.pct >= 50 ? '#d97706' : '#dc2626');
                            return {
                                x: p.adapter_count, y: p.pct, xref: 'x', yref: 'y',
                                text: p.pct + '%', showarrow: true, arrowhead: 2, arrowsize: 0.8,
                                arrowwidth: 1.2, arrowcolor: col, ax: 0, ay: -22,
                                font: { color: col, size: 10 },
                                bgcolor: 'rgba(255,255,255,0.85)', bordercolor: col, borderwidth: 1, borderpad: 1
                            };
                        });
                        Plotly.newPlot(chartId, [
                            {
                                x: affPts.map(function(p) { return p.adapter_count; }),
                                y: affPts.map(function(p) { return p.pct; }),
                                name: 'Single-pod', type: 'scatter', mode: 'lines+markers',
                                line: { color: '#059669', width: 3 },
                                marker: { color: '#059669', size: 9 },
                                hovertemplate: 'Adapters: %{x}<br>Single-pod: %{y}%<extra></extra>'
                            }
                        ], {
                            margin: { l: 60, r: 20, t: 20, b: 55 },
                            xaxis: { title: 'Adapters', gridcolor: '#f1f5f9', zeroline: false },
                            yaxis: { title: '% adapters served by a single pod', range: [0, 108], gridcolor: '#f1f5f9' },
                            showlegend: false,
                            hovermode: 'closest',
                            annotations: annotations
                        }, { displayModeBar: false, responsive: true });
                    });
                })(affPts, affChartId);
            }

            (function(pts, chartId) {
                _renders.push(function() {
                    var x = pts.map(function(p) { return p.adapter_count; });

                    // Throughput delta vs the 0-adapter baseline — floating
                    // labels on the throughput line (red = degradation,
                    // green = improvement)
                    var basePt = null;
                    for (var bi = 0; bi < pts.length; bi++) {
                        if (pts[bi].adapter_count === 0 && (pts[bi].output_tps_mean || 0) > 0) { basePt = pts[bi]; break; }
                    }
                    var deltaLabels = pts.map(function() { return ''; });
                    var deltaAnns = [];
                    if (basePt) {
                        var baseTps = basePt.output_tps_mean;
                        pts.forEach(function(p, i) {
                            if (p.adapter_count === 0 || !(p.output_tps_mean > 0)) return;
                            var delta = (p.output_tps_mean - baseTps) / baseTps * 100;
                            var txt = (delta > 0 ? '+' : '') + delta.toFixed(0) + '%';
                            deltaLabels[i] = '<br>Δ vs 0 adapters: ' + txt;
                            var col = delta > 0.5 ? '#059669' : (delta < -0.5 ? '#dc2626' : '#64748b');
                            deltaAnns.push({
                                x: p.adapter_count, y: p.output_tps_mean, xref: 'x', yref: 'y',
                                text: txt, showarrow: true, arrowhead: 2, arrowsize: 0.9, arrowwidth: 1.5,
                                arrowcolor: col, ax: 0, ay: -32,
                                font: { color: col, size: 11 },
                                bgcolor: 'rgba(255,255,255,0.88)', bordercolor: col, borderwidth: 1, borderpad: 2
                            });
                        });
                    }

                    var traces = [
                        {
                            x: x, y: pts.map(function(p) { return p.output_tps_mean || 0; }),
                            name: 'Throughput', type: 'scatter', mode: 'lines+markers+text',
                            line: { color: '#10b981', width: 3, dash: 'dot' }, marker: { size: 8, symbol: 'diamond' },
                            text: pts.map(function(p) { return p.output_tps_mean ? Math.round(p.output_tps_mean).toLocaleString() : ''; }),
                            textposition: 'top center', textfont: { color: '#059669', size: 10 },
                            customdata: deltaLabels,
                            hovertemplate: 'Adapters: %{x}<br>Throughput: %{y:,.0f} tok/s%{customdata}<extra></extra>'
                        },
                        {
                            x: x, y: pts.map(function(p) { return p.ttft_p90 || 0; }),
                            name: 'TTFT P90', type: 'scatter', mode: 'lines+markers+text', yaxis: 'y2',
                            line: { color: '#3b82f6', width: 3 }, marker: { size: 8 },
                            text: pts.map(function(p) { return p.ttft_p90 ? Math.round(p.ttft_p90) + 'ms' : ''; }),
                            textposition: 'bottom center', textfont: { color: '#3b82f6', size: 9 },
                            hovertemplate: 'Adapters: %{x}<br>TTFT P90: %{y:.0f} ms<extra></extra>'
                        },
                        {
                            x: x, y: pts.map(function(p) { return p.itl_p90 || 0; }),
                            name: 'ITL P90', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                            line: { color: '#f59e0b', width: 3 }, marker: { size: 8 },
                            hovertemplate: 'Adapters: %{x}<br>ITL P90: %{y:.1f} ms<extra></extra>'
                        }
                    ];
                    // Shade the LRU-swapping region (adapters > GPU-resident)
                    var shapes = [];
                    var firstLru = -1;
                    for (var li = 0; li < pts.length; li++) {
                        if (pts[li].lru_swapping) { firstLru = li; break; }
                    }
                    if (firstLru > 0) {
                        var boundary = (pts[firstLru - 1].adapter_count + pts[firstLru].adapter_count) / 2;
                        shapes.push({
                            type: 'line', x0: boundary, x1: boundary, yref: 'paper', y0: 0, y1: 1,
                            line: { color: '#ef4444', width: 2, dash: 'dash' }
                        });
                        shapes.push({
                            type: 'rect', x0: boundary, x1: x[x.length - 1] * 1.03, yref: 'paper', y0: 0, y1: 1,
                            fillcolor: 'rgba(239,68,68,0.08)', line: { width: 0 }
                        });
                    }
                    var resident = pts[0].gpu_resident;
                    var annotations = resident ? [{
                        x: resident, y: 1, yref: 'paper', xanchor: 'left', yanchor: 'top',
                        text: 'GPU-resident: ' + resident, showarrow: false,
                        font: { color: '#64748b', size: 11 },
                        bgcolor: 'rgba(255,255,255,0.8)'
                    }] : [];
                    Plotly.newPlot(chartId, traces, {
                        margin: { l: 70, r: 70, t: 30, b: 45 },
                        xaxis: { title: 'Adapter Count', gridcolor: '#f1f5f9', tickmode: 'array', tickvals: x },
                        yaxis: { title: 'Tokens per second', gridcolor: '#f1f5f9' },
                        yaxis2: { title: 'Latency (ms)', overlaying: 'y', side: 'right' },
                        shapes: shapes, annotations: annotations.concat(deltaAnns),
                        legend: { orientation: 'h', y: 1.12 },
                        hovermode: 'x unified'
                    }, { displayModeBar: false, responsive: true });
                });
            })(pts, sweepChartId);

            // Adapter loads vs evictions per sweep level — the thrashing
            // curve. Bars: gray = adapter loads (activations), red =
            // evictions (LRU removals). Vertical dashed line = pool
            // residency capacity (pods x slots); left of it a sticky
            // working set needs no loads past the first touch.
            (function(pts, chartId) {
                if (pts.length < 2) return;
                _renders.push(function() {
                    var x = pts.map(function(p) { return p.adapter_count; });
                    var loads = pts.map(function(p) { return p.adapter_loads !== undefined && p.adapter_loads !== null ? p.adapter_loads : null; });
                    var evictions = pts.map(function(p) { return p.adapter_evictions; });
                    var capacity = null;
                    pts.forEach(function(p) {
                        if (p.total_slots) capacity = p.total_slots;
                    });
                    var traces = [
                        {
                            x: x, y: loads, name: 'Adapter loads', type: 'bar',
                            marker: { color: 'rgba(100,116,139,0.55)' },
                            hovertemplate: '%{x} adapters<br>Loads: %{y}<extra></extra>'
                        },
                        {
                            x: x, y: evictions, name: 'Evictions', type: 'bar',
                            marker: { color: 'rgba(220,38,38,0.85)' },
                            hovertemplate: '%{x} adapters<br>Evictions: %{y}<extra></extra>'
                        }
                    ];
                    var shapes = [];
                    var annotations = [];
                    if (capacity) {
                        shapes.push({
                            type: 'line', x0: capacity, x1: capacity, y0: 0, y1: 1, yref: 'paper',
                            line: { color: '#b91c1c', width: 2, dash: 'dash' }
                        });
                        annotations.push({
                            x: capacity, y: 1, yref: 'paper', xanchor: 'left', yanchor: 'top',
                            text: 'pool capacity: ' + capacity + ' slots', showarrow: false,
                            font: { color: '#b91c1c', size: 11 },
                            bgcolor: 'rgba(255,255,255,0.85)'
                        });
                    }
                    Plotly.newPlot(chartId, traces, {
                        barmode: 'group',
                        margin: { l: 60, r: 20, t: 30, b: 50 },
                        xaxis: { title: 'Adapter count', gridcolor: '#f1f5f9' },
                        yaxis: { title: 'Adapter loads / evictions', gridcolor: '#f1f5f9' },
                        shapes: shapes, annotations: annotations,
                        legend: { orientation: 'h', y: 1.08 },
                        hovermode: 'x unified'
                    }, { displayModeBar: false, responsive: true });
                });
            })(evictPts, evictChartId);
        });
    }

    // LoRA routing & residency signals (vllm:lora_requests_info + per-pod TTFT)
    if (charts.vllm && charts.vllm.lora && charts.vllm.configs && charts.vllm.configs.length) {
        var loraSig = charts.vllm.lora;
        var hasLoraData = (loraSig.ttft_p99_pod_spread_ms || []).some(function(v) { return v > 0; }) ||
                          (loraSig.adapter_churn || []).some(function(v) { return v > 0; }) ||
                          (loraSig.adapters_per_pod || []).some(function(v) { return v > 0; });
        if (hasLoraData) {
            var cfgLabels = charts.vllm.configs;
            s += '<div class="chart-card" style="border-left:6px solid #7c3aed;">' +
                '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#a78bfa); color:white;">LoRA Routing &amp; Residency</div>' +
                '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
                'Per-configuration LoRA serving signals from vLLM Prometheus metrics. ' +
                '<strong>TTFT P99 spread across pods</strong> — the hot-pod vs cold-pod latency delta; misrouted requests pay a CPU&rarr;GPU adapter swap-in and show up as a large spread. ' +
                '<strong>Adapter churn</strong> — residency set changes from LRU evictions; high churn with few adapters means request scattering. ' +
                '<strong>Adapters per pod</strong> — how many distinct adapters each pod served (concentrated = adapter-aware routing; uniform spread across all pods = random load balancing). ' +
                '<strong>Waiting queue CV</strong> — queue-depth imbalance across pods; a hot-pod overload queues while cold pods idle.' +
                '<div id="lora-routing-chart' + _sfx + '" style="width:100%;height:640px;margin-top:10px;"></div></div></div>';

            _renders.push(function() {
                var cfgCount = cfgLabels.length;
                var dom = [
                    { x: [0, 0.47], y: [0.58, 1] },
                    { x: [0.53, 1], y: [0.58, 1] },
                    { x: [0, 0.47], y: [0.06, 0.48] },
                    { x: [0.53, 1], y: [0.06, 0.48] }
                ];
                function subplot(series, color, name, ytitle, idx, yaxisNum) {
                    return {
                        x: cfgLabels, y: series, name: name, type: 'bar',
                        marker: { color: color },
                        xaxis: 'x' + (idx > 1 ? idx : ''), yaxis: 'y' + yaxisNum,
                        hovertemplate: '%{x}<br>' + name + ': %{y}<extra></extra>'
                    };
                }
                var t1 = subplot(loraSig.ttft_p99_pod_spread_ms || [], '#3b82f6', 'TTFT spread', 'ms', 1, 1);
                var t2 = subplot(loraSig.adapter_churn || [], '#ef4444', 'Churn', 'sets', 2, 2);
                var t3 = subplot(loraSig.adapters_per_pod || [], '#7c3aed', 'Adapters/pod', 'count', 3, 3);
                var t4 = subplot(loraSig.waiting_cv || [], '#f59e0b', 'Queue CV', 'σ/μ', 4, 4);
                var layout = {
                    margin: { l: 55, r: 20, t: 30, b: 90 },
                    showlegend: false, hovermode: 'closest',
                    xaxis: { domain: dom[0].x, anchor: 'y', tickangle: -35, automargin: true },
                    yaxis: { domain: dom[0].y, anchor: 'x', title: { text: 'TTFT P99 spread (ms)' } },
                    xaxis2: { domain: dom[1].x, anchor: 'y2', tickangle: -35, automargin: true },
                    yaxis2: { domain: dom[1].y, anchor: 'x2', title: { text: 'Residency changes' } },
                    xaxis3: { domain: dom[2].x, anchor: 'y3', tickangle: -35, automargin: true },
                    yaxis3: { domain: dom[2].y, anchor: 'x3', title: { text: 'Adapters per pod' } },
                    xaxis4: { domain: dom[3].x, anchor: 'y4', tickangle: -35, automargin: true },
                    yaxis4: { domain: dom[3].y, anchor: 'x4', title: { text: 'Waiting CV' } },
                    annotations: [
                        { xref: 'paper', yref: 'paper', x: 0.23, y: 1.03, showarrow: false, text: 'Routing cost — hot vs cold pod TTFT delta', font: { size: 12, color: '#3b82f6' } },
                        { xref: 'paper', yref: 'paper', x: 0.77, y: 1.03, showarrow: false, text: 'LRU churn — residency set changes', font: { size: 12, color: '#ef4444' } },
                        { xref: 'paper', yref: 'paper', x: 0.23, y: 0.51, showarrow: false, text: 'Adapter concentration per pod', font: { size: 12, color: '#7c3aed' } },
                        { xref: 'paper', yref: 'paper', x: 0.77, y: 0.51, showarrow: false, text: 'Queue imbalance (0 = perfect)', font: { size: 12, color: '#f59e0b' } }
                    ]
                };
                if (cfgCount > 8) {
                    layout.xaxis.tickmode = 'array';
                    layout.xaxis.tickvals = cfgLabels.filter(function(_, i) { return i % Math.ceil(cfgCount / 8) === 0; });
                }
                Plotly.newPlot('lora-routing-chart' + _sfx, [t1, t2, t3, t4], layout, { displayModeBar: false, responsive: true });
            });

            // Adapter affinity summary — one row per config, aggregated from
            // the per-pod breakdowns below. Non-sweep tests only: sweep
            // levels (step14-lora-*) show their affinity columns in the
            // LoRA Adapter Sweep table above, and EPP tuning variants are
            // excluded entirely.
            var affDetails = (charts.vllm.lora_pod_details || []).filter(function(pd) {
                return pd && pd.pods && Object.keys(pd.pods).length && (pd.total_adapters || 0) > 0
                    && !(pd.test_id || '').startsWith('step11-epp-')
                    && !(pd.test_id || '').startsWith('step14-lora-');
            });
            if (affDetails.length) {
                var affRows = affDetails.map(function(pd) {
                    var total = pd.total_adapters || 0;
                    var conc = pd.concentrated || 0;
                    var scat = pd.scattered || 0;
                    var concPct = total ? Math.round(100 * conc / total) : 0;
                    var scatPct = total ? Math.round(100 * scat / total) : 0;
                    var share = pd.top_pod_share;
                    // Verdict prefers the request-weighted top-pod share:
                    // single-pod % degrades when adapters get few requests.
                    var verdict, verdictColor;
                    if (share === null || share === undefined) {
                        verdict = concPct >= 80 ? 'adapter-aware' : (concPct >= 50 ? 'partial' : 'random');
                        verdictColor = concPct >= 80 ? '#059669' : (concPct >= 50 ? '#f59e0b' : '#ef4444');
                    } else {
                        verdict = share >= 0.9 ? 'adapter-aware' : (share >= 0.5 ? 'partial' : 'random');
                        verdictColor = share >= 0.9 ? '#059669' : (share >= 0.5 ? '#f59e0b' : '#ef4444');
                    }
                    var shareCell = (share === null || share === undefined) ? '—' : Math.round(100 * share) + '%';
                    var shareColor = (share === null || share === undefined) ? '#64748b'
                        : (share >= 0.9 ? '#059669' : (share >= 0.5 ? '#f59e0b' : '#ef4444'));
                    var seen = (pd.adapters_seen && pd.adapters_seen !== total) ? total + '/' + pd.adapters_seen : String(total);
                    return '<tr>' +
                        '<td style="padding:6px 10px;font-weight:600;">' + pd.config + '</td>' +
                        '<td style="padding:6px 8px;text-align:center;">' + seen + '</td>' +
                        '<td style="padding:6px 8px;text-align:center;color:#059669;font-weight:600;">' + conc + ' (' + concPct + '%)</td>' +
                        '<td style="padding:6px 8px;text-align:center;color:#ef4444;">' + scat + ' (' + scatPct + '%)</td>' +
                        '<td style="padding:6px 8px;text-align:center;color:' + shareColor + ';font-weight:600;">' + shareCell + '</td>' +
                        '<td style="padding:6px 8px;text-align:center;color:' + verdictColor + ';font-weight:600;">' + verdict + '</td>' +
                        '</tr>';
                }).join('');
                s += '<div class="chart-card" style="border-left:6px solid #059669;">' +
                    '<div class="chart-card-header" style="background:linear-gradient(135deg,#059669,#34d399); color:white;">Adapter Affinity Summary</div>' +
                    '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
                    'How consistently each configuration routes an adapter\u2019s requests to a single pod (non-sweep tests; sweep levels appear in the LoRA Adapter Sweep table). ' +
                    'Counts include only adapters with <strong>≥2 requests</strong> (adapters served once are trivially single-pod — the Adapters column shows measured/seen). ' +
                    '<strong>Single-pod</strong> — the adapter was served entirely by one pod (adapter-aware routing; no cold-pod swap-ins). ' +
                    '<strong>Multi-pod</strong> — the adapter\u2019s requests were scattered across pods (random load balancing; scattered requests pay a CPU\u2192GPU adapter swap-in on cold pods). ' +
                    '<strong>Top-pod share</strong> — request-weighted fraction of each adapter\u2019s requests served by its main pod (100% = fully sticky).' +
                    '<div id="lora-affinity-summary-chart' + _sfx + '" style="width:100%;height:360px;margin-top:10px;"></div>' +
                    '<table id="lora-aff-table' + _sfx + '" style="width:100%;border-collapse:collapse;margin-top:10px;font-size:0.9em;background:#fff;">' +
                    '<tr style="background:#eef2ff;">' +
                    '<th style="padding:6px 10px;text-align:left;cursor:pointer;" onclick="sortReportTable(\'lora-aff-table' + _sfx + '\',0,\'str\')">Configuration ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'lora-aff-table' + _sfx + '\',1,\'num\')">Adapters ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'lora-aff-table' + _sfx + '\',2,\'num\')">Single-Pod ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'lora-aff-table' + _sfx + '\',3,\'num\')">Multi-Pod ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'lora-aff-table' + _sfx + '\',4,\'num\')">Top-Pod Share ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'lora-aff-table' + _sfx + '\',5,\'str\')">Routing Behavior ⇅</th></tr>' +
                    affRows + '</table></div></div>';

                _renders.push(function() {
                    // X = configuration names (sweep levels suffixed with
                    // their adapter count when the label repeats), Y = % of
                    // adapters served consistently by a single pod. One line;
                    // multi-pod is just the complement.
                    var labelCount = {};
                    affDetails.forEach(function(pd) {
                        labelCount[pd.config] = (labelCount[pd.config] || 0) + 1;
                    });
                    var used = {};
                    var cfgs = affDetails.map(function(pd) {
                        var lbl = pd.config;
                        if (labelCount[pd.config] > 1) {
                            lbl = pd.config + ' ·n' + (pd.total_adapters || 0);
                        }
                        used[lbl] = (used[lbl] || 0) + 1;
                        if (used[lbl] > 1) {
                            lbl = lbl + ' #' + used[lbl];
                        }
                        return lbl;
                    });
                    var y = affDetails.map(function(pd) {
                        return pd.total_adapters ? Math.round(100 * (pd.concentrated || 0) / pd.total_adapters) : 0;
                    });
                    var annotations = affDetails.map(function(pd, i) {
                        var col = y[i] >= 80 ? '#059669' : (y[i] >= 50 ? '#d97706' : '#dc2626');
                        return {
                            x: cfgs[i], y: y[i], xref: 'x', yref: 'y',
                            text: y[i] + '%', showarrow: true, arrowhead: 2, arrowsize: 0.8,
                            arrowwidth: 1.2, arrowcolor: col, ax: 0, ay: -22,
                            font: { color: col, size: 10 },
                            bgcolor: 'rgba(255,255,255,0.85)', bordercolor: col, borderwidth: 1, borderpad: 1
                        };
                    });
                    Plotly.newPlot('lora-affinity-summary-chart' + _sfx, [
                        {
                            x: cfgs, y: y, name: 'Single-pod', type: 'scatter', mode: 'lines+markers',
                            line: { color: '#059669', width: 3 },
                            marker: { color: '#059669', size: 9 },
                            hovertemplate: '%{x}<br>Single-pod: %{y}%<extra></extra>'
                        }
                    ], {
                        margin: { l: 60, r: 20, t: 20, b: 100 },
                        xaxis: { tickangle: -35, automargin: true },
                        yaxis: { title: '% adapters served by a single pod', range: [0, 108], gridcolor: '#f1f5f9' },
                        showlegend: false,
                        hovermode: 'closest',
                        annotations: annotations
                    }, { displayModeBar: false, responsive: true });
                });
            } else if ((charts.vllm.lora_pod_details || []).some(function(pd) { return pd && pd.pods; })) {
                // LoRA pods deployed but no measurable affinity — base_only
                // benchmark mode sends no adapter requests, so there is no
                // routing to measure. Explain instead of silently vanishing.
                var _bmMode = (data.run_config || {}).lora_benchmark_mode || 'round_robin';
                s += '<div class="chart-card" style="border-left:6px solid #94a3b8;">' +
                    '<div class="chart-card-header" style="background:linear-gradient(135deg,#64748b,#94a3b8); color:white;">Adapter Affinity Summary</div>' +
                    '<div style="padding:16px 20px; color:#1e293b; font-size:0.95em; line-height:1.6;">' +
                    'No adapter affinity to show' +
                    (_bmMode === 'base_only'
                        ? ' — this run\u2019s Benchmark Mode is <strong>base model only</strong>: every request targets the base model, ' +
                          'so adapter routing is never exercised (the sweep\u2019s Single-Pod columns show \u2014). ' +
                          'The adapters stay mounted on the pods; this run is the LoRA-infrastructure baseline. ' +
                          'Run the same configuration with Benchmark Mode = round-robin for the adapter-targeted side of the comparison.'
                        : ' — no adapter traffic was observed in the collected metrics.') +
                    '</div></div>';
            }
            (charts.vllm.lora_pod_details || []).forEach(function(pd) {
                if (!pd || !pd.pods) return;
                var podNames = Object.keys(pd.pods);
                if (!podNames.length) return;
                var podRows = podNames.map(function(pod) {
                    var p = pd.pods[pod];
                    var distinct = (p.running_adapters_distinct || []).length;
                    var last = p.running_adapters_last || [];
                    var lastStr = last.length
                        ? last.length + ' (' + last.slice(0, 5).join(', ') + (last.length > 5 ? ', …' : '') + ')'
                        : '—';
                    var wait = (p.waiting_adapters_last || []).length;
                    var evict = p.estimated_adapter_evictions;
                    var evictCell = (evict === undefined || evict === null) ? '—'
                        : (evict > 0 ? '<span style="color:#ef4444;font-weight:600;">' + evict + '</span>' : '0');
                    var loadCell = (p.estimated_adapter_loads === undefined || p.estimated_adapter_loads === null)
                        ? '—' : p.estimated_adapter_loads;
                    return '<tr>' +
                        '<td style="font-family:monospace;font-size:0.85em;">' + pod + '</td>' +
                        '<td style="text-align:center;">' + (p.max_lora != null ? p.max_lora : '—') + '</td>' +
                        '<td style="text-align:center;font-weight:600;">' + distinct + '</td>' +
                        '<td style="text-align:center;">' + (p.residency_set_changes || 0) + '</td>' +
                        '<td style="text-align:center;">' + loadCell + '</td>' +
                        '<td style="text-align:center;">' + evictCell + '</td>' +
                        '<td style="font-size:0.82em;color:#64748b;">' + lastStr + '</td>' +
                        '<td style="text-align:center;">' + (wait || '—') + '</td>' +
                        '</tr>';
                }).join('');
                var affTotal = pd.total_adapters || 0;
                var affPct = affTotal ? Math.round(100 * (pd.concentrated || 0) / affTotal) : 0;
                var affLine = affTotal
                    ? '<div style="margin:8px 0 4px;font-size:0.88em;">🔀 <strong>Adapter affinity:</strong> ' +
                      '<span style="color:#059669;font-weight:600;">' + (pd.concentrated || 0) + '</span> of ' + affTotal +
                      ' adapters (' + affPct + '%) served by a single pod — ' +
                      (affPct >= 80 ? 'concentrated (adapter-aware routing)' :
                       affPct >= 50 ? 'partially concentrated' :
                       'scattered across pods (random load balancing)') +
                      '; <span style="color:#ef4444;">' + (pd.scattered || 0) + '</span> served by multiple pods.</div>'
                    : '';
                var bdId = 'lora-pod-bd-' + pd.config.replace(/[^a-z0-9]/gi, '-') + '-n' + (pd.total_adapters || 0) + _sfx;
                s += '<div style="margin-top:16px;border:1.5px solid #e2e8f0;border-radius:8px;overflow:hidden;background:#fff;">' +
                    '<div style="background:#f8fafc;padding:8px 14px;font-weight:600;color:#334155;font-size:0.95em;border-bottom:1px solid #e2e8f0;">' +
                    'Pod Breakdown — ' + pd.config + ((pd.total_adapters || 0) > 0 ? ' · n=' + pd.total_adapters : '') + '</div>' +
                    affLine +
                    '<table id="' + bdId + '" class="pod-bd-table" style="width:100%;border-collapse:collapse;font-size:0.88em;background:#fff;">' +
                    '<tr style="background:#eef2ff;">' +
                    '<th style="padding:6px 10px;text-align:left;cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',0,\'str\')">Pod ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',1,\'num\')">GPU-Resident Cap ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',2,\'num\')">Distinct Adapters Served ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',3,\'num\')">Residency Changes ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',4,\'num\')">Loads ⇅</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',5,\'num\')">Evictions ⇅</th>' +
                    '<th style="padding:6px 8px;text-align:left;">Last Running Set</th>' +
                    '<th style="padding:6px 8px;cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',7,\'num\')">Waiting ⇅</th></tr>' +
                    podRows + '</table></div>';
            });
        }
    }


        return s;
    }

    ReportTabs.register({
        id: 'lora-serving',
        label: 'LoRA Serving',
        icon: '&#127919;',
        position: 'after:cache-sweep',
        build: function(data, ctx) {
            return build(data, ctx);
        },
        render: function(data, ctx) {
            if (typeof Plotly === 'undefined') return;
            _renders.forEach(function(fn) {
                try { fn(); } catch (e) { console.warn('lora render failed', e); }
            });
        }
    });
})();
