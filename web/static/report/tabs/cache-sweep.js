// tabs/cache-sweep.js — Cache Hit Sweep report tab. One implementation for live + offline.
(function() {
    var _renders = [];
    function build(data, ctx) {
        var s = '';
        _renders = [];
        var charts = data.charts || {};
    if (data.cache_sweep) {
        s += '<div class="chart-card" style="border-left:6px solid #7c3aed;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#a78bfa); color:white;">Cache Hit Sweep</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Tests the best configurations across different <strong>prefix cache hit ratios</strong> (0% to 100%). ' +
            'Higher cache hit rates mean more prompt tokens are reused from cache, reducing prefill compute and improving TTFT. ' +
            'The charts show how latency and throughput change as the cache hit rate increases, with actual vLLM cache hit rates from Prometheus.' +
            '</div></div>';
        const csweep = data.cache_sweep;
        const csArchLabels = {pd: 'PD', aggregated: 'Aggregated', ep: 'EP',
                              pd_calibrated: 'PD (calibrated)', aggregated_calibrated: 'Aggregated (calibrated)', ep_calibrated: 'EP (calibrated)'};
        const csArchColors = {pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed',
                              pd_calibrated: '#60a5fa', aggregated_calibrated: '#34d399', ep_calibrated: '#a78bfa'};
        const csPctColors = { ttft_p90: '#3b82f6', ttft_p95: '#f59e0b', ttft_p99: '#ef4444' };

        Object.keys(csweep).forEach(function(arch) {
            var pts = csweep[arch];
            if (!pts || !pts.length) return;
            var csArchKey = arch.startsWith('pd') ? 'pd' : arch.startsWith('agg') ? 'aggregated' : arch.startsWith('ep') ? 'ep' : arch;
            var csLabel = csArchLabels[csArchKey] || arch;
            var csColor = csArchColors[csArchKey] || '#888';

            var csConfigLbl = '';
            for (var _ci = 0; _ci < pts.length && !csConfigLbl; _ci++) {
                csConfigLbl = pts[_ci].config_label || '';
            }
            if (!csConfigLbl) {
                var _csBase = arch.replace('_calibrated','');
                csConfigLbl = sweepConfigLabels[_csBase] || '';
                if (!csConfigLbl && data.concurrency_sweep && data.concurrency_sweep[_csBase]) {
                    var _csPts = data.concurrency_sweep[_csBase];
                    csConfigLbl = (_csPts[0] && _csPts[0].config_label) ? _csPts[0].config_label : '';
                }
            }
            if (!csConfigLbl) {
                var _tpMatch = arch.match(/tp(\d+)/);
                if (_tpMatch) {
                    var _tp = _tpMatch[1];
                    if (csArchKey === 'aggregated') {
                        var _totalPods = Math.floor((pts[0] && pts[0].gpus || 8) / parseInt(_tp));
                        csConfigLbl = _totalPods + '×TP' + _tp;
                    } else {
                        csConfigLbl = 'TP' + _tp;
                    }
                } else {
                    csConfigLbl = arch;
                }
            }
            var csConfigSuffix = csConfigLbl ? ' — ' + csConfigLbl : '';
            s += '<div class="chart-card" style="margin-top:20px; border:2px solid ' + csColor + '; border-left:6px solid ' + csColor + ';">';
            s += '<div class="chart-card-header" style="background:linear-gradient(135deg,' + csColor + ',' + csColor + '99); color:white; font-size:1.2em;">' + csLabel + csConfigSuffix + ' — Cache Hit Sweep</div>';

            // --- TTFT P90/P95/P99 on same chart ---
            var csTtftId = 'cache-sweep-ttft-' + arch;
            s += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">TTFT percentiles &amp; throughput vs cache hit ratio. Lower TTFT is better, higher throughput is better. Subplot shows actual vs configured cache hit rate from vLLM.</div>';
            s += '<div id="' + csTtftId + '" style="width:100%;height:850px;"></div>';

            var hasActualHitRate = pts.some(function(p) { return p.actual_hit_rate != null; });

            _renders.push(function() {
                var traces = [];
                // Main chart: TTFT P90/P95/P99 + Throughput on right axis
                [{ key: 'ttft_p90', label: 'TTFT P90' },
                 { key: 'ttft_p95', label: 'TTFT P95' },
                 { key: 'ttft_p99', label: 'TTFT P99' }].forEach(function(pct) {
                    if (!pts[0][pct.key]) return;
                    traces.push({
                        x: pts.map(function(p) { return p.hit_pct; }),
                        y: pts.map(function(p) { return p[pct.key] || 0; }),
                        text: pts.map(function(p) { return Math.round(p[pct.key] || 0).toLocaleString(); }),
                        textposition: 'top center', textfont: { size: 13, color: csPctColors[pct.key] },
                        mode: 'lines+markers+text', name: pct.label,
                        line: { color: csPctColors[pct.key], width: 3 },
                        marker: { size: 8 },
                        hovertemplate: 'Cache Hit: %{x}%<br>' + pct.label + ': %{y:.0f}ms<extra></extra>'
                    });
                });
                // Throughput on right axis
                traces.push({
                    x: pts.map(function(p) { return p.hit_pct; }),
                    y: pts.map(function(p) { return p.throughput_mean || 0; }),
                    text: pts.map(function(p) { return (p.throughput_mean || 0).toFixed(1); }),
                    textposition: 'top center', textfont: { size: 13, color: '#d97706' },
                    mode: 'lines+markers+text', name: 'Throughput Mean',
                    yaxis: 'y2',
                    line: { color: '#d97706', width: 3, dash: 'dash' },
                    marker: { size: 8, symbol: 'diamond', color: '#d97706' },
                    hovertemplate: 'Cache Hit: %{x}%<br>Throughput: %{y:.1f} req/s<extra></extra>'
                });

                // Subplot: Actual Cache Hit Rate (hits/queries from vLLM Prometheus)
                if (hasActualHitRate) {
                    traces.push({
                        x: pts.map(function(p) { return p.hit_pct; }),
                        y: pts.map(function(p) { return p.actual_hit_rate != null ? p.actual_hit_rate : 0; }),
                        text: pts.map(function(p) { return p.actual_hit_rate != null ? p.actual_hit_rate.toFixed(1) + '%' : ''; }),
                        textposition: 'top center', textfont: { size: 13, color: '#059669' },
                        xaxis: 'x2', yaxis: 'y3',
                        mode: 'lines+markers+text', name: 'Actual Hit % (hits/queries)',
                        line: { color: '#059669', width: 3 },
                        marker: { size: 8 },
                        hovertemplate: 'Configured: %{x}%<br>Actual: %{y:.1f}%' +
                            '<br>Hits rate: ' + '%{customdata[0]}' + '/s' +
                            '<br>Queries rate: ' + '%{customdata[1]}' + '/s<extra></extra>',
                        customdata: pts.map(function(p) {
                            return [p.cache_hits_rate != null ? p.cache_hits_rate.toFixed(0) : '-',
                                    p.cache_queries_rate != null ? p.cache_queries_rate.toFixed(0) : '-'];
                        })
                    });
                }

                var xTickVals = pts.map(function(p) { return p.hit_pct; });
                var layout = {
                    xaxis: { range: [-5, 105], gridcolor: '#e2e8f0', domain: [0, 1], tickvals: xTickVals, ticktext: xTickVals.map(function(v) { return v + '%'; }) },
                    yaxis: { title: 'TTFT (ms)', gridcolor: '#e2e8f0', titlefont: { color: '#3b82f6' }, tickfont: { color: '#3b82f6' },
                             domain: hasActualHitRate ? [0.3, 1] : [0, 1] },
                    yaxis2: { title: 'Throughput (req/s)', side: 'right', overlaying: 'y',
                              titlefont: { color: '#d97706' }, tickfont: { color: '#d97706' } },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    margin: { t: 20, b: 60, l: 70, r: 70 },
                    legend: { x: 0, y: 1.15, orientation: 'h', bgcolor: 'rgba(255,255,255,0.9)' },
                    hovermode: 'closest'
                };

                if (hasActualHitRate) {
                    layout.xaxis2 = { title: 'Cache Hit %', range: [-5, 105], gridcolor: '#e2e8f0', anchor: 'y3', tickvals: xTickVals, ticktext: xTickVals.map(function(v) { return v + '%'; }) };
                    layout.yaxis3 = { title: 'Actual Hit %', range: [-5, 105], gridcolor: '#e2e8f0', domain: [0, 0.22],
                                      titlefont: { color: '#059669' }, tickfont: { color: '#059669' } };
                }

                Plotly.newPlot(csTtftId, traces, layout, { responsive: true });
            });

            // --- Data table ---
            var csCacheTblId = 'cache-sweep-tbl-' + arch + '-' + ctx.runId;
            s += '<div style="padding:12px 20px;"><div style="overflow-x:auto;"><table class="results-table" id="' + csCacheTblId + '" style="font-size:0.85em;">';
            s += '<tr>';
            s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',0,\'num\')">Cache Hit % &#x21C5;</th>';
            var _col = 0;
            function _th(label) { s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + csCacheTblId + '\',' + (_col++) + ',\'num\')">' + label + ' &#x21C5;</th>'; }
            _th('Actual Hit %'); _th('Concurrency');
            _th('TTFT P50'); _th('TTFT P90'); _th('TTFT P95'); _th('TTFT P99');
            _th('Throughput'); _th('vLLM tok/s'); _th('ITL P90');
            _th('E2E P50'); _th('E2E P90');
            _th('KV Cache %'); _th('Running'); _th('Run Max'); _th('Load Spread'); _th('Waiting');
            _th('Prefill t'); _th('Decode t');
            s += '</tr>';

            var _csTblLookup = {};
            (data.all_results || []).forEach(function(r) { _csTblLookup[r.test_id] = r; });

            pts.forEach(function(p) {
                var _r = _csTblLookup[p.test_id] || {};
                var _mj = _r.metrics_json ? (typeof _r.metrics_json === 'string' ? JSON.parse(_r.metrics_json) : _r.metrics_json) : {};
                var _pm = _mj.prometheus_metrics || {};
                var _kvAvg = ((_pm.vllm_kv_cache_pct || {}).avg || 0) * 100;
                var _running = (_pm.vllm_requests_running || {}).avg || 0;
                var _runMax = (_pm.vllm_requests_running || {}).max || 0;
                var _runMin = (_pm.vllm_requests_running || {}).min || 0;
                var _waiting = (_pm.vllm_requests_waiting || {}).avg || 0;
                var _routingCV = (_pm.vllm_routing_cv || {}).avg || null;
                var _prefillT = (_pm.vllm_prefill_time_rate || {}).avg || 0;
                var _decodeT = (_pm.vllm_decode_time_rate || {}).avg || 0;
                var _e2eP50 = (_pm.vllm_e2e_p50 || {}).avg || _mj.e2e_latency_p50 || null;
                var _e2eP90 = (_pm.vllm_e2e_p90 || {}).avg || _mj.e2e_latency_p90 || null;
                var fmtE2e = function(v) { return v != null ? (v >= 1 ? v.toFixed(1) + 's' : Math.round(v * 1000) + 'ms') : '-'; };
                s += '<tr>';
                s += '<td>' + p.hit_pct + '%</td>';
                s += '<td>' + (p.actual_hit_rate != null ? p.actual_hit_rate.toFixed(1) + '%' : '-') + '</td>';
                s += '<td>' + (p.concurrency || '-') + '</td>';
                s += '<td>' + (p.ttft_p50 ? p.ttft_p50.toFixed(0) : '-') + '</td>';
                s += '<td>' + (p.ttft_p90 ? p.ttft_p90.toFixed(0) : '-') + '</td>';
                s += '<td>' + (p.ttft_p95 ? p.ttft_p95.toFixed(0) : '-') + '</td>';
                s += '<td>' + (p.ttft_p99 ? p.ttft_p99.toFixed(0) : '-') + '</td>';
                s += '<td>' + (p.throughput_mean ? p.throughput_mean.toFixed(2) : '-') + '</td>';
                s += '<td>' + (p.vllm_tps ? p.vllm_tps.toLocaleString() : (p.output_tps_mean ? p.output_tps_mean.toFixed(0) : '-')) + '</td>';
                s += '<td>' + (p.itl_p90 ? p.itl_p90.toFixed(1) : '-') + '</td>';
                s += '<td>' + fmtE2e(_e2eP50) + '</td>';
                s += '<td>' + fmtE2e(_e2eP90) + '</td>';
                s += '<td>' + _kvAvg.toFixed(1) + '%</td>';
                s += '<td>' + _running.toFixed(1) + '</td>';
                s += '<td>' + _runMax.toFixed(0) + '</td>';
                s += '<td>' + (_runMax - _running).toFixed(1) + '</td>';
                s += '<td>' + _waiting.toFixed(1) + '</td>';
                s += '<td>' + _prefillT.toFixed(3) + '</td>';
                s += '<td>' + _decodeT.toFixed(2) + '</td>';
                s += '</tr>';
            });
            s += '</table>';
            s += '<div style="padding:8px 20px 12px; font-size:0.85em; color:#64748b; line-height:1.7;">' +
                '<strong>KV Cache Avg/Max %</strong>: How full the KV cache is across pods. Low values mean KV capacity is not the bottleneck.<br>' +
                '<strong>Avg Running Reqs</strong>: Average requests actively being processed per pod over time (flattened across all pods and all timestamps).<br>' +
                '<strong>Max Running Reqs</strong>: Peak concurrent requests on a single pod at any point during the test. Large gap between avg and max indicates EPP routing imbalance (e.g. prefix affinity concentrating requests on one pod).<br>' +
                '<strong>Load Spread</strong>: Gap between peak and average running requests (Max-Avg). High values indicate traffic bursts — requests arriving simultaneously rather than staggered.<br>' +
                '<strong>Avg Waiting Reqs</strong>: Requests queued waiting for a processing slot. High values indicate saturation.<br>' +
                '<strong>Decode req-s/s</strong>: Total accumulated decode time across all concurrent requests per wall-second. Proportional to running request count, not GPU count.<br>' +
                '<strong>Prefill req-s/s</strong>: Total accumulated prefill time across all concurrent requests per wall-second. Low values with high cache hit mean prefill is served from cache.</div>' +
            '</div></div>';

            // --- vLLM Engine Metrics chart for this config ---
            var csEngineId = 'cache-sweep-engine-' + arch;
            s += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b; margin-top:12px;">vLLM engine metrics across cache hit levels — KV cache usage, running/waiting requests, prefill/decode GPU time, and preemptions.</div>';
            s += '<div id="' + csEngineId + '" style="width:100%;height:850px;"></div>';

            _renders.push((function(chartId, points) { return function() {
                var _arLookup = {};
                (data.all_results || []).forEach(function(r) { _arLookup[r.test_id] = r; });
                var xVals = [], kvAvg = [], kvMax = [], runAvg = [], runMax = [], runSpread = [], waitAvg = [], prefillT = [], decodeT = [], preempt = [];
                points.forEach(function(p) {
                    var tid = p.test_id || '';
                    var r = _arLookup[tid] || {};
                    var mj = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                    var pm = mj.prometheus_metrics || {};
                    xVals.push(p.hit_pct + '%');
                    kvAvg.push(((pm.vllm_kv_cache_pct || {}).avg || 0) * 100);
                    kvMax.push(((pm.vllm_kv_cache_pct || {}).max || 0) * 100);
                    runAvg.push((pm.vllm_requests_running || {}).avg || 0);
                    runMax.push((pm.vllm_requests_running || {}).max || 0);
                    runSpread.push(((pm.vllm_requests_running || {}).max || 0) - ((pm.vllm_requests_running || {}).avg || 0));
                    waitAvg.push((pm.vllm_requests_waiting || {}).avg || 0);
                    prefillT.push((pm.vllm_prefill_time_rate || {}).avg || 0);
                    decodeT.push((pm.vllm_decode_time_rate || {}).avg || 0);
                    preempt.push((pm.vllm_preemptions_rate || {}).avg || 0);
                });
                if (!xVals.length) return;
                var traces = [
                    { x: xVals, y: kvAvg, name: 'KV Cache Avg %', type: 'bar', marker: { color: '#3b82f6' } },
                    { x: xVals, y: kvMax, name: 'KV Cache Max %', type: 'bar', marker: { color: '#93c5fd' } },
                    { x: xVals, y: runAvg, name: 'Avg Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 3 }, marker: { size: 8 } },
                    { x: xVals, y: runMax, name: 'Max Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 2, dash: 'dash' }, marker: { size: 6, symbol: 'triangle-up' } },
                    { x: xVals, y: runSpread, name: 'Load Spread', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 2, dash: 'dashdot' }, marker: { size: 6, symbol: 'diamond' } },
                    { x: xVals, y: waitAvg, name: 'Avg Waiting Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#ef4444', width: 2, dash: 'dot' }, marker: { size: 6 } },
                    { x: xVals, y: decodeT, name: 'Decode req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#f59e0b', width: 2 }, marker: { size: 6 } },
                    { x: xVals, y: prefillT, name: 'Prefill req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#8b5cf6', width: 2 }, marker: { size: 6 } },
                ];
                if (preempt.some(function(v) { return v > 0; })) {
                    traces.push({ x: xVals, y: preempt, name: 'Preemptions/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 3 }, marker: { size: 8, symbol: 'x' } });
                }
                Plotly.newPlot(chartId, traces, {
                    barmode: 'group',
                    xaxis: { title: 'Cache Hit %' },
                    yaxis: { title: 'KV Cache %', side: 'left', rangemode: 'tozero', gridcolor: '#e2e8f0' },
                    yaxis2: { title: 'Count / Rate', overlaying: 'y', side: 'right', rangemode: 'tozero' },
                    legend: { x: 0, y: 1.15, orientation: 'h' },
                    margin: { t: 40, b: 60, l: 60, r: 60 },
                    plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                    hovermode: 'x unified',
                }, { responsive: true });
            }; })(csEngineId, pts));

            s += '</div>';
        });
    }


        return s;
    }
    ReportTabs.register({
        id: 'cache-sweep', label: 'Cache Sweep', icon: '&#128451;',
        position: 'after:calibrated-load',
        build: build,
        render: function(data, ctx) {
            if (typeof Plotly === 'undefined') return;
            _renders.forEach(function(fn) { try { fn(); } catch (e) { console.warn('cache-sweep render failed', e); } });
        }
    });
})();
