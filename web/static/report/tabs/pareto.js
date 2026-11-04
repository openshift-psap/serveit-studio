// tabs/pareto.js — Pareto Frontier report tab (throughput vs interactivity /
// TTFT / ITL frontiers). One implementation for live + offline reports.
(function() {

    function _tagLabel(name, arch) {
        if (!name || name.indexOf('(') >= 0) return name || '';
        var a = (arch || '').toUpperCase();
        if (a === 'PD') return name + ' (PD)';
        if (a === 'EP') return name + ' (EP)';
        return name + ' (AG)';
    }

    function hasBothArchitectures(data) {
        var rs = (data.all_results || []).filter(function(r) { return r.quality !== 'discard'; });
        return rs.some(function(r) { return r.architecture === 'PD' || r.architecture === 'EP'; }) &&
               rs.some(function(r) { return r.architecture === 'AGGREGATED'; });
    }

    function build(data, ctx) {
        if (!hasBothArchitectures(data)) return '';
        var s = '';
        s += '<div class="chart-card" style="border-left:6px solid #8b5cf6;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#8b5cf6,#a78bfa); color:white;">Pareto Frontier Analysis</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Pareto frontiers show the best achievable trade-offs between competing objectives. ' +
            'Points on the frontier cannot be improved on one axis without sacrificing the other. ' +
            '<strong style="color:#3b82f6">Blue = PD/EP</strong> (disaggregated), <strong style="color:#ef4444">Red = Aggregated</strong> (co-located). ' +
            'The architecture whose frontier is further to the top-right dominates.</div></div>';
        s += '<div class="chart-card"><div class="chart-card-header">Pareto — Throughput vs Interactivity</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Trade-off between how fast each user gets tokens (X) and how efficiently GPUs are used (Y). Top-right is ideal.</div>' +
            '<div class="chart-card-body"><div id="' + ctx.chartId('chart-pareto-frontier') + '" style="width:100%;height:850px;"></div></div></div>';
        s += '<div class="chart-card"><div class="chart-card-header">Pareto — Throughput vs TTFT</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Trade-off between time to first token (X, lower is better) and throughput efficiency (Y, higher is better). Top-left is ideal.</div>' +
            '<div class="chart-card-body"><div id="' + ctx.chartId('chart-pareto-ttft') + '" style="width:100%;height:850px;"></div></div></div>';
        s += '<div class="chart-card"><div class="chart-card-header">Pareto — Throughput vs ITL</div>' +
            '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' +
            'Trade-off between inter-token latency (X, lower is better) and throughput efficiency (Y, higher is better). Top-left means fast streaming with high throughput.</div>' +
            '<div class="chart-card-body"><div id="' + ctx.chartId('chart-pareto-itl') + '" style="width:100%;height:850px;"></div></div></div>';
        return s;
    }

    var LAYOUT = { margin: { t: 10, b: 40, l: 50, r: 20 }, height: 850, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent', font: { family: 'Inter, sans-serif' } };
    var CONFIG = {
        responsive: true, displayModeBar: true,
        modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
        toImageButtonOptions: { format: 'png', height: 1200, width: 1600, scale: 2 }
    };

    function plot(ctx, baseId, traces, layout) {
        var el = document.getElementById(ctx.chartId(baseId));
        if (el) Plotly.newPlot(el, traces, layout, CONFIG);
    }

    function getVllmTotalTps(r) {
        var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
        var pm = (m && typeof m === 'object') ? (m.prometheus_metrics || {}) : {};
        var pAvg = (pm.vllm_prompt_tokens_rate || {}).avg || 0;
        var gAvg = (pm.vllm_generation_tokens_rate || {}).avg || 0;
        return (pAvg + gAvg > 0) ? (pAvg + gAvg) : 0;
    }

    function coreResults(data) {
        return (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || r.config_name || '';
            return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0;
        });
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var rec = data.recommendation || {};
        var runOsl = (rec && rec.workload) ? rec.workload.osl : 100;

        // ── Throughput–Interactivity Pareto Frontier (normalized 0-1) ──
        var allCfgResults = (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || r.config_name || '';
            return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
                   (r.output_tps_mean > 0 || r.throughput_mean > 0);
        });

        function makeRawPoints(results) {
            return results.map(function(r) {
                var tput = r.throughput_mean || r.throughput_p90 || 0;
                var conc = r.concurrency || 100;
                var totalOutputTps = getVllmTotalTps(r) || r.output_tps_mean || (tput * runOsl);
                var interactivity = totalOutputTps / conc;
                var tpsGpu = totalOutputTps / (r.gpus || 1);
                var cfgKey;
                if (r.architecture === 'AGGREGATED') {
                    var total = (r.prefill_pods || 0) + (r.decode_pods || 0);
                    cfgKey = total + '×TP' + (r.tp || '?') + ' (AG' + ((r.pp && r.pp > 1) ? ' PPx' + r.pp : '') + ')';
                } else {
                    var _tag = r.architecture === 'EP' ? 'EP' : 'PD';
                    cfgKey = (r.prefill_pods || 0) + 'P+' + (r.decode_pods || 0) + 'D TP=' + (r.tp || '?') + ' (' + _tag + ')';
                }
                return { x: interactivity, y: tpsGpu, nx: interactivity, ny: tpsGpu,
                         label: _tagLabel(r.config_name, r.architecture), arch: r.architecture, conc: conc, cfgKey: cfgKey };
            }).filter(function(p) { return p.x > 0 && p.y > 0; });
        }

        var allPoints = makeRawPoints(allCfgResults);
        if (allPoints.length > 1) {
            function frontierLine(points) {
                if (!points.length) return [];
                var sorted = points.slice().sort(function(a, b) { return b.nx - a.nx; });
                var frontier = [];
                var maxY = -Infinity;
                for (var i = 0; i < sorted.length; i++) {
                    if (sorted[i].ny > maxY) { frontier.push(sorted[i]); maxY = sorted[i].ny; }
                }
                frontier.sort(function(a, b) { return a.nx - b.nx; });
                return frontier;
            }

            var cfgGroups = {};
            allPoints.forEach(function(p) {
                if (!cfgGroups[p.cfgKey]) cfgGroups[p.cfgKey] = [];
                cfgGroups[p.cfgKey].push(p);
            });

            var cfgColors = ['#dc2626', '#2563eb', '#059669', '#d97706', '#7c3aed', '#db2777', '#0891b2', '#65a30d', '#ea580c', '#6d28d9'];
            var traces = [];
            var paretoAnnotations = [];
            var colorIdx = 0;

            Object.keys(cfgGroups).sort().forEach(function(key) {
                var pts = cfgGroups[key];
                var color = cfgColors[colorIdx % cfgColors.length];
                colorIdx++;
                traces.push({
                    x: pts.map(function(p) { return p.nx; }), y: pts.map(function(p) { return p.ny; }),
                    text: pts.map(function(p) { return p.label + '<br>' + p.x.toFixed(1) + ' tok/s/user<br>' + p.y.toFixed(0) + ' tok/s/GPU<br>c=' + p.conc; }),
                    name: key, mode: 'markers',
                    marker: { color: color, size: 14 },
                    hovertemplate: '<b>%{text}</b><extra></extra>'
                });
            });

            var pdAll = allPoints.filter(function(p) { return p.arch === 'PD' || p.arch === 'EP'; });
            var aggAll = allPoints.filter(function(p) { return p.arch === 'AGGREGATED'; });
            var aggPareto = frontierLine(aggAll);
            var pdPareto = frontierLine(pdAll);

            if (aggPareto.length > 1) {
                traces.push({
                    x: aggPareto.map(function(p) { return p.nx; }), y: aggPareto.map(function(p) { return p.ny; }),
                    name: 'Aggregated — Frontier', mode: 'lines',
                    line: { color: '#dc2626', width: 3, dash: 'dot' },
                    hoverinfo: 'skip'
                });
            }
            if (pdPareto.length > 1) {
                traces.push({
                    x: pdPareto.map(function(p) { return p.nx; }), y: pdPareto.map(function(p) { return p.ny; }),
                    name: 'Disaggregation — Frontier', mode: 'lines+markers',
                    line: { color: '#2563eb', width: 4 },
                    marker: { color: '#2563eb', size: 8 },
                    hovertemplate: '<b>%{text}</b><extra></extra>',
                    text: pdPareto.map(function(p) { return p.cfgKey + '<br>' + p.nx.toFixed(1) + ' tok/s/user<br>' + p.ny.toFixed(0) + ' tok/s/GPU'; })
                });
            }

            function addFrontierAnnotations(points, color) {
                var positions = [[80, -15], [-80, -15], [70, -45], [-70, -45], [90, -30], [-90, -30], [60, -60], [-60, -60], [100, -10], [-100, -10], [50, -50], [-50, -50]];
                points.forEach(function(p, i) {
                    if (!p.label) return;
                    var pos = positions[i % positions.length];
                    paretoAnnotations.push({
                        x: p.nx, y: p.ny, text: p.cfgKey + '<br>c=' + p.conc,
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: color,
                        ax: pos[0], ay: pos[1],
                        font: { size: 13, color: color },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2
                    });
                });
            }
            addFrontierAnnotations(aggPareto, '#dc2626');
            addFrontierAnnotations(pdPareto, '#2563eb');

            plot(ctx, 'chart-pareto-frontier', traces, Object.assign({}, LAYOUT, {
                height: 850,
                xaxis: { title: 'Interactivity (tok/s/user)', gridcolor: '#d1d5db', rangemode: 'tozero' },
                yaxis: { title: 'Token Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db', rangemode: 'tozero' },
                showlegend: true,
                legend: { x: 1.02, y: 1, xanchor: 'left', bgcolor: 'rgba(255,255,255,0.95)', bordercolor: '#e2e8f0', borderwidth: 1 },
                margin: { t: 40, b: 70, l: 70, r: 220 },
                plot_bgcolor: 'white', paper_bgcolor: 'white',
                annotations: paretoAnnotations
            }));

            // ── TTFT vs Throughput Pareto chart ──
            var ttftAllResults = (data.all_results || []).filter(function(r) {
                var tid = r.test_id || r.config_name || '';
                return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
                       r.ttft_p90 > 0 && (r.throughput_mean > 0 || r.throughput_p90 > 0);
            });

            var ttftAllPts = ttftAllResults.map(function(r) {
                var tput = r.throughput_mean || r.throughput_p90 || 0;
                var totalTps = getVllmTotalTps(r) || r.output_tps_mean || (tput * runOsl);
                var tpsGpu = totalTps / (r.gpus || 1);
                return { x: r.ttft_p90, y: tpsGpu, label: _tagLabel(r.config_name, r.architecture), arch: r.architecture };
            }).filter(function(p) { return p.x > 0 && p.y > 0; });

            if (ttftAllPts.length > 1) {
                ttftAllPts.forEach(function(p) { p.nx = p.x; p.ny = p.y; });
                var ttftPd = ttftAllPts.filter(function(p) { return p.arch === 'PD' || p.arch === 'EP'; });
                var ttftAgg = ttftAllPts.filter(function(p) { return p.arch === 'AGGREGATED'; });

                function ttftFrontierLine(points) {
                    if (!points.length) return [];
                    var sorted = points.slice().sort(function(a, b) { return a.nx - b.nx; });
                    var frontier = [];
                    var maxY = -Infinity;
                    for (var i = 0; i < sorted.length; i++) {
                        if (sorted[i].ny > maxY) { frontier.push(sorted[i]); maxY = sorted[i].ny; }
                    }
                    frontier.sort(function(a, b) { return a.nx - b.nx; });
                    return frontier;
                }

                var ttftPdPareto = ttftFrontierLine(ttftPd);
                var ttftAggPareto = ttftFrontierLine(ttftAgg);

                var allX = ttftAllPts.map(function(p) { return p.nx; }).sort(function(a, b) { return a - b; });
                var p95Idx = Math.min(Math.floor(allX.length * 0.95), allX.length - 1);
                var xMax = allX[p95Idx] * 1.2;
                var frontierMaxX = 0;
                ttftAggPareto.concat(ttftPdPareto).forEach(function(p) { if (p.nx > frontierMaxX) frontierMaxX = p.nx; });
                if (frontierMaxX > xMax) xMax = frontierMaxX * 1.1;

                var ttftTraces = [];
                if (ttftAgg.length) {
                    var visAgg = ttftAgg.filter(function(p) { return p.nx <= xMax; });
                    ttftTraces.push({
                        x: visAgg.map(function(p) { return p.nx; }), y: visAgg.map(function(p) { return p.ny; }),
                        text: visAgg.map(function(p) { return p.label + '<br>TTFT P90: ' + p.x.toFixed(0) + 'ms<br>' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                        name: 'Aggregated', mode: 'markers',
                        marker: { color: '#fca5a5', size: 14, opacity: 0.5 },
                        hovertemplate: '<b>%{text}</b><extra></extra>'
                    });
                }
                if (ttftPd.length) {
                    var visPd = ttftPd.filter(function(p) { return p.nx <= xMax; });
                    ttftTraces.push({
                        x: visPd.map(function(p) { return p.nx; }), y: visPd.map(function(p) { return p.ny; }),
                        text: visPd.map(function(p) { return p.label + '<br>TTFT P90: ' + p.x.toFixed(0) + 'ms<br>' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                        name: 'Disaggregation', mode: 'markers',
                        marker: { color: '#93c5fd', size: 14, opacity: 0.5 },
                        hovertemplate: '<b>%{text}</b><extra></extra>'
                    });
                }
                if (ttftAggPareto.length) {
                    ttftTraces.push({
                        x: ttftAggPareto.map(function(p) { return p.nx; }), y: ttftAggPareto.map(function(p) { return p.ny; }),
                        text: ttftAggPareto.map(function(p) { return p.label + '<br>' + p.x.toFixed(0) + 'ms / ' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                        name: 'Aggregated — Frontier', mode: ttftAggPareto.length > 1 ? 'lines+markers+text' : 'markers+text',
                        line: { color: '#dc2626', width: 3 }, marker: { color: '#dc2626', size: 8 },
                        textposition: 'top right', textfont: { size: 13, color: '#dc2626' },
                        hovertemplate: '<b>%{text}</b><extra></extra>'
                    });
                }
                if (ttftPdPareto.length) {
                    ttftTraces.push({
                        x: ttftPdPareto.map(function(p) { return p.nx; }), y: ttftPdPareto.map(function(p) { return p.ny; }),
                        text: ttftPdPareto.map(function(p) { return p.label + '<br>' + p.x.toFixed(0) + 'ms / ' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                        name: 'Disaggregation — Frontier', mode: ttftPdPareto.length > 1 ? 'lines+markers+text' : 'markers+text',
                        line: { color: '#2563eb', width: 3 }, marker: { color: '#2563eb', size: 8 },
                        textposition: 'top left', textfont: { size: 13, color: '#2563eb' },
                        hovertemplate: '<b>%{text}</b><extra></extra>'
                    });
                }

                plot(ctx, 'chart-pareto-ttft', ttftTraces, Object.assign({}, LAYOUT, {
                    height: 850,
                    xaxis: { title: 'TTFT P90 (ms)', gridcolor: '#d1d5db', range: [0, xMax] },
                    yaxis: { title: 'Token Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db', rangemode: 'tozero' },
                    showlegend: true,
                    legend: { x: 1.02, y: 1, xanchor: 'left', bgcolor: 'rgba(255,255,255,0.95)', bordercolor: '#e2e8f0', borderwidth: 1 },
                    margin: { t: 40, b: 70, l: 70, r: 220 },
                    plot_bgcolor: 'white', paper_bgcolor: 'white'
                }));
            }

            // ── ITL vs Throughput Pareto chart ──
            var itlPts = (data.all_results || []).filter(function(r) {
                var tid = r.test_id || r.config_name || '';
                return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
                       r.itl_p90 > 0 && (r.throughput_mean > 0 || r.throughput_p90 > 0);
            }).map(function(r) {
                var tput = r.throughput_mean || r.throughput_p90 || 0;
                var conc = r.concurrency || 100;
                var totalTps = getVllmTotalTps(r) || r.output_tps_mean || (tput * runOsl);
                var tpsGpu = totalTps / (r.gpus || 1);
                return { x: r.itl_p90, y: tpsGpu, nx: r.itl_p90, ny: tpsGpu, label: _tagLabel(r.config_name, r.architecture), arch: r.architecture, conc: conc };
            }).filter(function(p) { return p.x > 0 && p.y > 0; });

            if (itlPts.length > 1) {
                var itlPd = itlPts.filter(function(p) { return p.arch === 'PD' || p.arch === 'EP'; });
                var itlAgg = itlPts.filter(function(p) { return p.arch === 'AGGREGATED'; });

                function itlFrontier(points) {
                    if (!points.length) return [];
                    var sorted = points.slice().sort(function(a, b) { return a.x - b.x; });
                    var frontier = [];
                    var maxY = -Infinity;
                    for (var i = 0; i < sorted.length; i++) {
                        if (sorted[i].y > maxY) { frontier.push(sorted[i]); maxY = sorted[i].y; }
                    }
                    return frontier;
                }

                var itlAggF = itlFrontier(itlAgg);
                var itlPdF = itlFrontier(itlPd);
                var itlTraces = [];

                if (itlAgg.length) {
                    itlTraces.push({
                        x: itlAgg.map(function(p) { return p.x; }), y: itlAgg.map(function(p) { return p.y; }),
                        text: itlAgg.map(function(p) { return p.label + '<br>ITL P90: ' + p.x.toFixed(2) + 'ms<br>' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                        name: 'Aggregated', mode: 'markers',
                        marker: { color: '#fca5a5', size: 14, opacity: 0.5 },
                        hovertemplate: '<b>%{text}</b><extra></extra>'
                    });
                }
                if (itlPd.length) {
                    itlTraces.push({
                        x: itlPd.map(function(p) { return p.x; }), y: itlPd.map(function(p) { return p.y; }),
                        text: itlPd.map(function(p) { return p.label + '<br>ITL P90: ' + p.x.toFixed(2) + 'ms<br>' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                        name: 'Disaggregation', mode: 'markers',
                        marker: { color: '#93c5fd', size: 14, opacity: 0.5 },
                        hovertemplate: '<b>%{text}</b><extra></extra>'
                    });
                }
                if (itlAggF.length) {
                    itlTraces.push({
                        x: itlAggF.map(function(p) { return p.x; }), y: itlAggF.map(function(p) { return p.y; }),
                        text: itlAggF.map(function(p) { return p.label + '<br>' + p.x.toFixed(2) + 'ms / ' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                        name: 'Aggregated — Frontier', mode: itlAggF.length > 1 ? 'lines+markers+text' : 'markers+text',
                        line: { color: '#dc2626', width: 3 }, marker: { color: '#dc2626', size: 8 },
                        textposition: 'top right', textfont: { size: 13, color: '#dc2626' },
                        hovertemplate: '<b>%{text}</b><extra></extra>'
                    });
                }
                if (itlPdF.length) {
                    itlTraces.push({
                        x: itlPdF.map(function(p) { return p.x; }), y: itlPdF.map(function(p) { return p.y; }),
                        text: itlPdF.map(function(p) { return p.label + '<br>' + p.x.toFixed(2) + 'ms / ' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                        name: 'Disaggregation — Frontier', mode: itlPdF.length > 1 ? 'lines+markers+text' : 'markers+text',
                        line: { color: '#2563eb', width: 3 }, marker: { color: '#2563eb', size: 8 },
                        textposition: 'top left', textfont: { size: 13, color: '#2563eb' },
                        hovertemplate: '<b>%{text}</b><extra></extra>'
                    });
                }

                plot(ctx, 'chart-pareto-itl', itlTraces, Object.assign({}, LAYOUT, {
                    height: 850,
                    xaxis: { title: 'ITL P90 (ms)', gridcolor: '#d1d5db' },
                    yaxis: { title: 'Token Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db', rangemode: 'tozero' },
                    showlegend: true,
                    legend: { x: 1.02, y: 1, xanchor: 'left', bgcolor: 'rgba(255,255,255,0.95)', bordercolor: '#e2e8f0', borderwidth: 1 },
                    margin: { t: 40, b: 70, l: 70, r: 220 },
                    plot_bgcolor: 'white', paper_bgcolor: 'white'
                }));
            }
        }
    }

    ReportTabs.register({
        id: 'pareto-frontier',
        label: 'Pareto Frontier',
        icon: '&#128200;',
        position: 'before:traffic',
        build: build,
        render: render
    });
})();
