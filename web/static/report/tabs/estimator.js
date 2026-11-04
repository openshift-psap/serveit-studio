// tabs/estimator.js — GPU Estimator report tab. One implementation for live + offline.
// Faithful port of the original estimator UI: styled form groups, methodology
// explanation, Estimate + Download Report buttons, results table and bar chart.
(function() {

    var _lastData = null;

    function build(data, ctx) {
        _lastData = data;
        var wl = data.recommendation ? data.recommendation.workload : {};
        var wlDesc = 'Tested with <strong>' + (wl.users || '?') + ' users</strong>, ISL <strong>' + (wl.isl || '?') + '</strong>';
        if (wl.isl_stdev) wlDesc += '&plusmn;' + wl.isl_stdev;
        wlDesc += ', OSL <strong>' + (wl.osl || '?') + '</strong>';
        if (wl.osl_stdev) wlDesc += '&plusmn;' + wl.osl_stdev;
        if (wl.turns > 1) wlDesc += ', <strong>' + wl.turns + ' turns</strong>';
        wlDesc += '.';

        var s = '<div class="chart-card" style="margin-top:16px; border:2px solid #d97706; border-left:6px solid #d97706;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#d97706,#b45309);">GPU Estimator</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">' +
                'Estimate how many GPUs/replicas are needed to meet your SLA targets using the measured performance data. ' + wlDesc +
            '</div>' +
            '<div style="padding:12px 20px 0;">' +
                '<div class="estimator-form">' +
                    '<div class="estimator-row">' +
                        '<div class="estimator-group">' +
                            '<div class="estimator-group-label">Throughput SLA</div>' +
                            '<label class="estimator-field">' +
                                '<span>Target Throughput</span>' +
                                '<div class="estimator-input-wrap"><span class="estimator-icon">&#9889;</span><input type="number" id="' + ctx.chartId('est-tput-target') + '" value="100" min="1" step="1"><span class="estimator-unit">req/s</span></div>' +
                            '</label>' +
                        '</div>' +
                        '<div class="estimator-group">' +
                            '<div class="estimator-group-label">TTFT SLA</div>' +
                            '<label class="estimator-field">' +
                                '<span>Max TTFT</span>' +
                                '<div class="estimator-input-wrap"><span class="estimator-icon">&#9201;</span><input type="number" id="' + ctx.chartId('est-ttft-target') + '" value="500" min="1"><span class="estimator-unit">ms</span></div>' +
                            '</label>' +
                            '<label class="estimator-field">' +
                                '<span>Percentile</span>' +
                                '<select id="' + ctx.chartId('est-ttft-pctl') + '" style="padding:10px 12px;border:2px solid #e2e8f0;border-radius:10px;font-size:0.95em;background:#f8fafc;">' +
                                    '<option value="p90">P90</option><option value="p95">P95</option><option value="p99" selected>P99</option>' +
                                '</select>' +
                            '</label>' +
                        '</div>' +
                        '<div class="estimator-group">' +
                            '<div class="estimator-group-label">ITL SLA</div>' +
                            '<label class="estimator-field">' +
                                '<span>Max ITL</span>' +
                                '<div class="estimator-input-wrap"><span class="estimator-icon">&#9881;</span><input type="number" id="' + ctx.chartId('est-itl-target') + '" value="20" min="0.1" step="0.1"><span class="estimator-unit">ms</span></div>' +
                            '</label>' +
                            '<label class="estimator-field">' +
                                '<span>Percentile</span>' +
                                '<select id="' + ctx.chartId('est-itl-pctl') + '" style="padding:10px 12px;border:2px solid #e2e8f0;border-radius:10px;font-size:0.95em;background:#f8fafc;">' +
                                    '<option value="p90" selected>P90</option><option value="p95">P95</option><option value="p99">P99</option>' +
                                '</select>' +
                            '</label>' +
                        '</div>' +
                    '</div>' +
                '</div>' +
                '<div id="' + ctx.chartId('est-scaling-info') + '" style="background:#fffbeb;border-top:1px solid #fde68a;border-bottom:1px solid #fde68a;padding:14px 20px;margin:0 -20px;font-size:0.9em;color:#92400e;width:calc(100% + 40px);"></div>' +
                '<div style="padding:0 20px;display:flex;gap:12px;align-items:center;">' +
                    '<button class="action-button" onclick="runEstimatorTab(\'' + ctx.chartId('') + '\')" style="margin-top:16px;padding:10px 28px;font-size:0.95em;border-radius:8px;background:linear-gradient(135deg,#d97706,#b45309);border:none;color:white;cursor:pointer;font-weight:600;box-shadow:0 2px 8px rgba(217,119,6,0.3);">Estimate</button>' +
                    '<button onclick="downloadEstimatorReportTab(\'' + ctx.chartId('') + '\')" style="margin-top:16px;padding:10px 24px;font-size:0.9em;border-radius:8px;background:#f1f5f9;border:1px solid #cbd5e1;color:#475569;cursor:pointer;font-weight:600;">Download Report</button>' +
                '</div>' +
                '<div id="' + ctx.chartId('est-results') + '"></div>' +
                '<div id="' + ctx.chartId('est-chart') + '" style="width:100%;margin-top:16px;"></div>' +
            '</div></div>';
        return s;
    }

    // Fill the methodology explanation box
    function fillScalingInfo(suffix) {
        var el = document.getElementById('est-scaling-info' + suffix);
        if (!el) return;
        var bullets = '';
        bullets += '<li><strong>Throughput SLA</strong>: Replicas = ceil(target req/s &divide; measured req/s per replica). GPUs = replicas &times; GPUs per replica.</li>';
        bullets += '<li><strong>TTFT SLA</strong>: Filters configs where measured TTFT at the chosen percentile exceeds the target. Configs that fail are marked.</li>';
        bullets += '<li><strong>ITL SLA</strong>: Filters configs where measured ITL at the chosen percentile exceeds the target. Configs that fail are marked.</li>';
        bullets += '<li>Uses the <strong>best measured throughput</strong> per config from concurrency sweep data — no workload scaling or estimation.</li>';
        el.innerHTML = '<div style="text-align:left;"><strong>How GPU estimation works</strong>' +
            '<ul style="margin:8px 0 0 16px;padding:0;list-style:disc;line-height:1.7;">' + bullets + '</ul></div>';
    }

    function render(data, ctx) {
        // Fill the methodology box on render
        var suffix = ctx.mode === 'live' ? (typeof _chartSuffix !== 'undefined' ? _chartSuffix : '') : '';
        fillScalingInfo(suffix);
    }

    // The estimator is interactive — triggered by the Estimate button.
    window.runEstimatorTab = function(suffix) {
        if (typeof Plotly === 'undefined') return;
        var allResults = (_lastData && _lastData.all_results) || [];
        if (!allResults.length) return;

        var targetTput = parseFloat(document.getElementById('est-tput-target' + suffix).value) || 100;
        var ttftTarget = parseFloat(document.getElementById('est-ttft-target' + suffix).value) || 500;
        var ttftPctl = document.getElementById('est-ttft-pctl' + suffix).value || 'p99';
        var itlTarget = parseFloat(document.getElementById('est-itl-target' + suffix).value) || 20;
        var itlPctl = document.getElementById('est-itl-pctl' + suffix).value || 'p90';

        var valid = allResults.filter(function(r) {
            return r.quality !== 'discard' && r.gpus > 0 && (r.throughput_mean > 0 || r.throughput_p90 > 0);
        });
        var bestByConfig = {};
        valid.forEach(function(r) {
            var tid = r.test_id || '';
            if (tid.indexOf('step2-') === 0 || tid.indexOf('step3-') === 0) return;
            var key = r.config_name || tid;
            var tput = r.throughput_mean || r.throughput_p90 || 0;
            if (!bestByConfig[key] || tput > (bestByConfig[key].throughput_mean || bestByConfig[key].throughput_p90 || 0)) {
                bestByConfig[key] = r;
            }
        });

        var results = [];
        Object.keys(bestByConfig).forEach(function(key) {
            var r = bestByConfig[key];
            var tput = r.throughput_mean || r.throughput_p90 || 0;
            if (tput <= 0) return;
            var replicas = Math.ceil(targetTput / tput);
            var totalGpus = replicas * r.gpus;
            var ttftVal = ttftPctl === 'p90' ? r.ttft_p90 : (ttftPctl === 'p95' ? r.ttft_p95 : r.ttft_p99);
            var ttftMeets = ttftVal != null ? ttftVal <= ttftTarget : null;
            var itlVal = itlPctl === 'p90' ? r.itl_p90 : (itlPctl === 'p95' ? r.itl_p95 : r.itl_p99);
            var itlMeets = itlVal != null && itlVal > 0 ? itlVal <= itlTarget : null;
            results.push({ config_name: key, architecture: (r.architecture || 'UNKNOWN').toUpperCase(),
                gpus_per_replica: r.gpus, replicas: replicas, total_gpus: totalGpus, measured_tput: tput,
                ttft_val: ttftVal, ttft_meets: ttftMeets, itl_val: itlVal, itl_meets: itlMeets });
        });
        results.sort(function(a, b) { return a.total_gpus - b.total_gpus; });

        var box = document.getElementById('est-results' + suffix);
        var chartEl = document.getElementById('est-chart' + suffix);
        if (!results.length) { box.innerHTML = '<p style="color:#6b7280;padding:16px;">No valid configurations to estimate.</p>'; return; }

        var bestGpus = results[0].total_gpus;
        var sep = 'border-left:3px solid #e2e8f0;';
        var fmtMs = function(v) { return v != null ? Math.round(v).toLocaleString() : '-'; };
        var t = '<table class="estimator-table" style="margin-top:16px;"><thead>';
        t += '<tr><th>Configuration</th><th>Arch</th><th>GPUs/replica</th><th>Measured req/s</th>';
        t += '<th style="' + sep + '">Replicas</th><th><strong>Total GPUs</strong></th>';
        t += '<th style="' + sep + '">TTFT ' + ttftPctl.toUpperCase() + '</th><th>ITL ' + itlPctl.toUpperCase() + '</th><th style="' + sep + '">SLA Status</th></tr></thead><tbody>';
        results.forEach(function(r) {
            var isBest = r.total_gpus === bestGpus && r.ttft_meets !== false && r.itl_meets !== false;
            var ttftColor = r.ttft_meets === true ? '#059669' : (r.ttft_meets === false ? '#ef4444' : '#64748b');
            var itlColor = r.itl_meets === true ? '#059669' : (r.itl_meets === false ? '#ef4444' : '#64748b');
            var slaStatus = '';
            if (r.ttft_meets === false && r.itl_meets === false) slaStatus = '<span style="color:#ef4444;font-weight:700;">TTFT + ITL FAIL</span>';
            else if (r.ttft_meets === false) slaStatus = '<span style="color:#ef4444;font-weight:700;">TTFT FAIL</span>';
            else if (r.itl_meets === false) slaStatus = '<span style="color:#ef4444;font-weight:700;">ITL FAIL</span>';
            else slaStatus = '<span style="color:#059669;font-weight:700;">PASS</span>';
            t += '<tr class="' + (isBest ? 'estimator-best' : '') + '" style="' + (r.ttft_meets === false || r.itl_meets === false ? 'opacity:0.6;' : '') + '">';
            t += '<td>' + r.config_name + '</td><td>' + r.architecture + '</td><td>' + r.gpus_per_replica + '</td><td>' + r.measured_tput.toFixed(2) + '</td>';
            t += '<td>' + r.replicas + '</td><td><strong>' + r.total_gpus.toLocaleString() + '</strong></td>';
            t += '<td style="color:' + ttftColor + ';font-weight:600;">' + fmtMs(r.ttft_val) + ' ms</td>';
            t += '<td style="color:' + itlColor + ';font-weight:600;">' + (r.itl_val != null ? r.itl_val.toFixed(2) : '-') + ' ms</td>';
            t += '<td>' + slaStatus + '</td></tr>';
        });
        t += '</tbody></table>';
        box.innerHTML = t;

        var labels = results.map(function(r) { return r.config_name; });
        var barColors = results.map(function(r) {
            var passes = r.ttft_meets !== false && r.itl_meets !== false;
            if (r.total_gpus === bestGpus && passes) return '#059669';
            if (!passes) return '#ef4444';
            return '#d97706';
        });
        Plotly.newPlot(chartEl, [{
            type: 'bar', orientation: 'h',
            y: labels, x: results.map(function(r) { return r.total_gpus; }),
            text: results.map(function(r) {
                var t2 = r.total_gpus.toLocaleString() + ' GPUs (' + r.replicas + ' replicas)';
                if (r.ttft_meets === false) t2 += ' [TTFT FAIL]';
                if (r.itl_meets === false) t2 += ' [ITL FAIL]';
                return t2;
            }),
            textposition: 'outside', marker: { color: barColors },
            hovertemplate: '<b>%{y}</b><br>%{x} GPUs<br>%{text}<extra></extra>'
        }], {
            title: { text: 'GPUs needed for ' + targetTput + ' req/s', font: { size: 15 } },
            xaxis: { title: 'Total GPUs' }, yaxis: { automargin: true },
            margin: { l: 200, r: 180, t: 50, b: 40 },
            height: Math.max(600, results.length * 60 + 80)
        }, { responsive: true, displayModeBar: false });
    };

    // Download the estimator results as a standalone HTML report
    window.downloadEstimatorReportTab = function(suffix) {
        var results = window._lastEstResults || [];
        if (!results.length) { alert('Run the estimator first'); return; }
        var bestGpus = results[0].total_gpus;
        var chartEl = document.getElementById('est-chart' + suffix);
        if (!chartEl) return;
        Plotly.toImage(chartEl, { format: 'svg', width: 1200, height: Math.max(400, results.length * 60 + 80) }).then(function(chartSvg) {
            var html = '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>GPU Estimate Report</title>';
            html += '<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:1200px;margin:0 auto;padding:20px;color:#1e293b;}';
            html += 'table{width:100%;border-collapse:collapse;font-size:0.9em;margin:16px 0;}th,td{padding:8px 12px;border:1px solid #e2e8f0;text-align:left;}';
            html += 'th{background:#f8fafc;font-weight:700;}.best{background:#f0fdf4;}.fail{opacity:0.6;}';
            html += '.pass{color:#059669;font-weight:700;}.ttft-fail{color:#ef4444;font-weight:700;}</style></head><body>';
            html += '<h1 style="color:#b45309;">GPU Estimate Report</h1>';
            html += '<p style="color:#64748b;">Target: <strong>' + results[0].target_tput + ' req/s</strong> | TTFT SLA: <strong>' + results[0].ttft_target + 'ms ' + results[0].ttft_pctl.toUpperCase() + '</strong> | ITL SLA: <strong>' + results[0].itl_target + 'ms ' + results[0].itl_pctl.toUpperCase() + '</strong></p>';
            html += '<table><thead><tr><th>Configuration</th><th>Arch</th><th>GPUs/replica</th><th>Measured req/s</th><th>Replicas</th><th>Total GPUs</th><th>TTFT ' + results[0].ttft_pctl.toUpperCase() + '</th><th>ITL ' + results[0].itl_pctl.toUpperCase() + '</th><th>SLA</th></tr></thead><tbody>';
            results.forEach(function(r) {
                var cls = (r.total_gpus === bestGpus && r.ttft_meets !== false && r.itl_meets !== false) ? ' class="best"' : (r.ttft_meets === false || r.itl_meets === false ? ' class="fail"' : '');
                var sla = r.ttft_meets === false ? 'TTFT FAIL' : (r.itl_meets === false ? 'ITL FAIL' : 'PASS');
                var slaCls = sla === 'PASS' ? 'pass' : 'ttft-fail';
                html += '<tr' + cls + '><td>' + r.config_name + '</td><td>' + r.architecture + '</td><td>' + r.gpus_per_replica + '</td><td>' + r.measured_tput.toFixed(2) + '</td><td>' + r.replicas + '</td><td><strong>' + r.total_gpus.toLocaleString() + '</strong></td>';
                html += '<td>' + (r.ttft_val != null ? Math.round(r.ttft_val) + ' ms' : '-') + '</td>';
                html += '<td>' + (r.itl_val != null ? r.itl_val.toFixed(2) + ' ms' : '-') + '</td>';
                html += '<td class="' + slaCls + '">' + sla + '</td></tr>';
            });
            html += '</tbody></table>';
            html += '<img src="' + chartSvg + '" style="width:100%;margin-top:20px;">';
            html += '<p style="color:#94a3b8;font-size:0.8em;margin-top:20px;">Generated ' + new Date().toLocaleString() + '</p>';
            html += '</body></html>';
            var blob = new Blob([html], { type: 'text/html' });
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'gpu-estimate-' + results[0].target_tput + 'rps.html';
            a.click();
            URL.revokeObjectURL(a.href);
        });
    };

    // Store results for the download report
    var _origRunEstimator = window.runEstimatorTab;
    window.runEstimatorTab = function(suffix) {
        _origRunEstimator(suffix);
        // Capture results for download
        var allResults = (_lastData && _lastData.all_results) || [];
        var targetTput = parseFloat(document.getElementById('est-tput-target' + suffix).value) || 100;
        var ttftTarget = parseFloat(document.getElementById('est-ttft-target' + suffix).value) || 500;
        var ttftPctl = document.getElementById('est-ttft-pctl' + suffix).value || 'p99';
        var itlTarget = parseFloat(document.getElementById('est-itl-target' + suffix).value) || 20;
        var itlPctl = document.getElementById('est-itl-pctl' + suffix).value || 'p90';
        var valid = allResults.filter(function(r) { return r.quality !== 'discard' && r.gpus > 0 && (r.throughput_mean > 0 || r.throughput_p90 > 0); });
        var bestByConfig = {};
        valid.forEach(function(r) {
            var tid = r.test_id || '';
            if (tid.indexOf('step2-') === 0 || tid.indexOf('step3-') === 0) return;
            var key = r.config_name || tid;
            var tput = r.throughput_mean || r.throughput_p90 || 0;
            if (!bestByConfig[key] || tput > (bestByConfig[key].throughput_mean || bestByConfig[key].throughput_p90 || 0)) bestByConfig[key] = r;
        });
        var results = [];
        Object.keys(bestByConfig).forEach(function(key) {
            var r = bestByConfig[key];
            var tput = r.throughput_mean || r.throughput_p90 || 0;
            if (tput <= 0) return;
            results.push({ config_name: key, architecture: (r.architecture || 'UNKNOWN').toUpperCase(),
                gpus_per_replica: r.gpus, replicas: Math.ceil(targetTput / tput), total_gpus: Math.ceil(targetTput / tput) * r.gpus,
                measured_tput: tput, target_tput: targetTput,
                ttft_val: r['ttft_' + ttftPctl], ttft_meets: r['ttft_' + ttftPctl] != null ? r['ttft_' + ttftPctl] <= ttftTarget : null, ttft_target: ttftTarget, ttft_pctl: ttftPctl,
                itl_val: r['itl_' + itlPctl], itl_meets: r['itl_' + itlPctl] != null ? r['itl_' + itlPctl] <= itlTarget : null, itl_target: itlTarget, itl_pctl: itlPctl });
        });
        results.sort(function(a, b) { return a.total_gpus - b.total_gpus; });
        window._lastEstResults = results;
    };

    ReportTabs.register({
        id: 'estimator',
        label: 'Estimator',
        icon: '&#128200;',
        position: 'end',
        build: build,
        render: render
    });
})();
