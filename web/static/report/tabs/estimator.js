// tabs/estimator.js — GPU Estimator report tab. One implementation for live + offline.
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
            '</div>';
        s += '<div style="padding:12px 20px 0;">';
        s += '<div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-end;margin:12px 0;">';
        s += '<div><div style="font-weight:700;font-size:0.85em;color:#64748b;margin-bottom:6px;">Throughput SLA</div>' +
            '<label style="display:block;font-size:0.85em;color:#475569;">Target Throughput ' +
            '<input type="number" id="' + ctx.chartId('est-tput-target') + '" value="100" min="1" step="1" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;width:110px;"> req/s</label></div>';
        s += '<div><div style="font-weight:700;font-size:0.85em;color:#64748b;margin-bottom:6px;">TTFT SLA</div>' +
            '<label style="display:block;font-size:0.85em;color:#475569;">Max TTFT ' +
            '<input type="number" id="' + ctx.chartId('est-ttft-target') + '" value="500" min="1" step="1" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;width:100px;"> ms</label>' +
            '<label style="display:block;font-size:0.85em;color:#475569;margin-top:6px;">Percentile ' +
            '<select id="' + ctx.chartId('est-ttft-pctl') + '" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;">' +
            '<option value="p90">P90</option><option value="p95">P95</option><option value="p99" selected>P99</option></select></label></div>';
        s += '<div><div style="font-weight:700;font-size:0.85em;color:#64748b;margin-bottom:6px;">ITL SLA</div>' +
            '<label style="display:block;font-size:0.85em;color:#475569;">Max ITL ' +
            '<input type="number" id="' + ctx.chartId('est-itl-target') + '" value="20" min="0.1" step="0.1" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;width:100px;"> ms</label>' +
            '<label style="display:block;font-size:0.85em;color:#475569;margin-top:6px;">Percentile ' +
            '<select id="' + ctx.chartId('est-itl-pctl') + '" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;">' +
            '<option value="p90" selected>P90</option><option value="p95">P95</option><option value="p99">P99</option></select></label></div>';
        s += '<button onclick="runEstimatorTab(\'' + ctx.chartId('') + '\')" style="padding:10px 28px;font-size:0.95em;border-radius:8px;background:linear-gradient(135deg,#d97706,#b45309);border:none;color:white;cursor:pointer;font-weight:600;">Estimate</button>';
        s += '</div>';
        s += '<div id="' + ctx.chartId('est-results') + '"></div>';
        s += '<div id="' + ctx.chartId('est-chart') + '" style="width:100%;margin-top:16px;"></div>';
        s += '</div></div>';
        return s;
    }

    // The estimator render is interactive — triggered by the Estimate button.
    // Expose the runner as a global so the inline onclick can reach it.
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
        var t = '<table class="results-table"><thead><tr><th>Configuration</th><th>Arch</th><th>GPUs/replica</th><th>Measured req/s</th>' +
            '<th style="' + sep + '">Replicas</th><th><strong>Total GPUs</strong></th>' +
            '<th style="' + sep + '">TTFT ' + ttftPctl.toUpperCase() + '</th><th>ITL ' + itlPctl.toUpperCase() + '</th><th style="' + sep + '">SLA Status</th></tr></thead><tbody>';
        results.forEach(function(r) {
            var isBest = r.total_gpus === bestGpus && r.ttft_meets !== false && r.itl_meets !== false;
            var ttftColor = r.ttft_meets === true ? '#059669' : (r.ttft_meets === false ? '#ef4444' : '#64748b');
            var itlColor = r.itl_meets === true ? '#059669' : (r.itl_meets === false ? '#ef4444' : '#64748b');
            var slaStatus = '';
            if (r.ttft_meets === false && r.itl_meets === false) slaStatus = '<span style="color:#ef4444;font-weight:700;">TTFT + ITL FAIL</span>';
            else if (r.ttft_meets === false) slaStatus = '<span style="color:#ef4444;font-weight:700;">TTFT FAIL</span>';
            else if (r.itl_meets === false) slaStatus = '<span style="color:#ef4444;font-weight:700;">ITL FAIL</span>';
            else slaStatus = '<span style="color:#059669;font-weight:700;">PASS</span>';
            t += '<tr style="' + (isBest ? 'background:#f0fdf4;font-weight:600;' : '') + (r.ttft_meets === false || r.itl_meets === false ? 'opacity:0.6;' : '') + '">' +
                '<td>' + r.config_name + '</td><td>' + r.architecture + '</td><td>' + r.gpus_per_replica + '</td><td>' + r.measured_tput.toFixed(2) + '</td>' +
                '<td>' + r.replicas + '</td><td><strong>' + r.total_gpus.toLocaleString() + '</strong></td>' +
                '<td style="color:' + ttftColor + ';font-weight:600;">' + fmtMs(r.ttft_val) + ' ms</td>' +
                '<td style="color:' + itlColor + ';font-weight:600;">' + (r.itl_val != null ? r.itl_val.toFixed(2) : '-') + ' ms</td><td>' + slaStatus + '</td></tr>';
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

    ReportTabs.register({ id: 'estimator', label: 'Estimator', icon: '&#128200;', position: 'end', build: build });
})();
