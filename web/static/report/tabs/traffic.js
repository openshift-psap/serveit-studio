// tabs/traffic.js — Traffic report tab. One implementation for live + offline.
(function() {

    function coreResults(data) {
        return (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || r.config_name || '';
            return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0;
        });
    }

    function build(data, ctx) {
        if (!data.all_results || !data.all_results.length) return '';
        var charts = data.charts || {};
        var s = '';
        var stepGroups = {
            'TP Calibration': { prefix: ['step2-', 'step3-'], results: [] },
            'Aggregated': { prefix: ['step6-'], results: [] },
            'PD/EP Splits': { prefix: ['step7-'], results: [] },
            'EPP Tuning': { prefix: ['step9-', 'step11-epp-'], results: [] },
            'Concurrency Sweep': { prefix: ['step11-sweep-'], results: [] },
            'Cache Sweep': { prefix: ['step13-'], results: [] },
        };
        data.all_results.forEach(function(r) {
            var tid = r.test_id || r.config_name || '';
            Object.keys(stepGroups).forEach(function(group) {
                stepGroups[group].prefix.forEach(function(pfx) {
                    if (tid.indexOf(pfx) === 0) stepGroups[group].results.push(r);
                });
            });
        });

        var chartEntries = [];
        function splitByConfig(results) {
            var byConfig = {};
            results.forEach(function(r) {
                var tid = r.test_id || '';
                var cfgMatch = tid.replace('step11-sweep-', '').replace(/-c\d+$/, '');
                var label = r.config_name || cfgMatch;
                if (!byConfig[cfgMatch]) byConfig[cfgMatch] = { label: label, results: [] };
                byConfig[cfgMatch].results.push(r);
            });
            return byConfig;
        }

        Object.keys(stepGroups).forEach(function(group) {
            var results = stepGroups[group].results;
            if (!results.length) return;
            if (group === 'Concurrency Sweep') {
                var byConfig = splitByConfig(results);
                Object.keys(byConfig).sort().forEach(function(cfgKey) {
                    var cfg = byConfig[cfgKey];
                    var hasErrors = cfg.results.some(function(r) { return (r.request_errored || 0) > 0 || (r.nixl_errors || 0) > 0; });
                    var cid = ctx.chartId('chart-traffic-sweep-' + cfgKey.replace(/[^a-z0-9]/g, '-'));
                    chartEntries.push({ id: cid, results: cfg.results, hasErrors: hasErrors, isSweep: true });
                    s += '<div class="chart-card" style="margin-top:16px;' + (hasErrors ? 'border-left:4px solid #dc2626;' : '') + '">';
                    s += '<div class="chart-card-header">Sweep: ' + cfg.label + (hasErrors ? ' <span style="color:#dc2626;font-size:0.8em;">&#9888; errors</span>' : '') + '</div>';
                    s += '<div id="' + cid + '" style="width:100%;height:850px;"></div></div>';
                });
            } else {
                var hasErrors = results.some(function(r) { return (r.request_errored || 0) > 0 || (r.nixl_errors || 0) > 0; });
                var cid = ctx.chartId('chart-traffic-' + group.toLowerCase().replace(/[^a-z]/g, '-'));
                chartEntries.push({ id: cid, results: results, hasErrors: hasErrors });
                s += '<div class="chart-card" style="margin-top:16px;' + (hasErrors ? 'border-left:4px solid #dc2626;' : '') + '">';
                s += '<div class="chart-card-header">' + group + ' Traffic' + (hasErrors ? ' <span style="color:#dc2626;font-size:0.8em;">&#9888; errors detected</span>' : '') + '</div>';
                s += '<div style="padding:4px 16px;font-size:0.82em;color:#64748b;">Request totals, HTTP errors, and NIXL transfer errors per test configuration.</div>';
                s += '<div id="' + cid + '" style="width:100%;height:850px;"></div></div>';
            }
        });

        if (!s) return '';
        // store chart entries for render
        build._chartEntries = chartEntries;

        // Traffic Overview stat cards
        var totalReqs = 0, totalErrs = 0, totalNixlErrs = 0;
        coreResults(data).forEach(function(r) {
            totalReqs += r.request_total || 0;
            totalErrs += r.request_errored || 0;
            totalNixlErrs += r.nixl_errors || 0;
        });
        var errRate = totalReqs > 0 ? (totalErrs / totalReqs * 100).toFixed(2) : '0';
        var overviewHtml = '<div class="chart-card" style="border-left:6px solid #475569;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#475569,#64748b); color:white;">Traffic Overview</div>' +
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;padding:20px;">' +
            '<div style="text-align:center;"><div style="font-size:2em;font-weight:800;color:#1e293b;">' + totalReqs.toLocaleString() + '</div><div style="color:#64748b;font-size:0.85em;">Total Requests</div></div>' +
            '<div style="text-align:center;"><div style="font-size:2em;font-weight:800;color:' + (totalErrs > 0 ? '#dc2626' : '#1e293b') + ';">' + totalErrs.toLocaleString() + '</div><div style="color:#64748b;font-size:0.85em;">HTTP Request Errors</div></div>';
        if (totalNixlErrs > 0) {
            overviewHtml += '<div style="text-align:center;"><div style="font-size:2em;font-weight:800;color:#f59e0b;">' + totalNixlErrs.toLocaleString() + '</div><div style="color:#64748b;font-size:0.85em;">NIXL Transfer Retries</div></div>';
        }
        overviewHtml += '<div style="text-align:center;"><div style="font-size:2em;font-weight:800;color:#1e293b;">' + errRate + '%</div><div style="color:#64748b;font-size:0.85em;">Error Rate</div></div>' +
            '</div></div>';
        return overviewHtml + s;
    }

    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var entries = build._chartEntries || [];
        entries.forEach(function(tc) {
            var el = document.getElementById(tc.id);
            if (!el) return;
            if (tc.isSweep) {
                tc.results.sort(function(a, b) {
                    var ca = parseInt((a.test_id || '').match(/c(\d+)$/)?.[1] || '0');
                    var cb = parseInt((b.test_id || '').match(/c(\d+)$/)?.[1] || '0');
                    return ca - cb;
                });
            }
            var labels = tc.results.map(function(r) {
                var tid = r.test_id || '';
                if (tid.indexOf('step11-sweep-') === 0) {
                    var cMatch = tid.match(/c(\d+)$/);
                    return cMatch ? 'c=' + cMatch[1] : tid.replace('step11-sweep-', '');
                }
                return tid.replace(/^step\d+-/, '');
            });
            var totals = tc.results.map(function(r) { return r.request_total || 0; });
            var errors = tc.results.map(function(r) { return r.request_errored || 0; });
            var nixl = tc.results.map(function(r) { return r.nixl_errors || 0; });
            var degraded = tc.results.map(function(r) { return r.nixl_degraded ? 1 : 0; });
            var hasDegraded = degraded.some(function(d) { return d; });
            var traces = [
                { x: labels, y: totals, name: 'Total Requests', mode: 'lines+markers+text',
                  text: totals.map(function(v) { return v > 0 ? v.toLocaleString() : ''; }),
                  textposition: 'top center', textfont: { size: 13, color: '#059669' },
                  line: { color: '#059669', width: 2 }, marker: { size: 8 } },
                { x: labels, y: errors, name: 'HTTP Errors', mode: 'lines+markers+text',
                  text: errors.map(function(v) { return v > 0 ? v.toLocaleString() : ''; }),
                  textposition: 'top center', textfont: { size: 13, color: '#dc2626' },
                  line: { color: '#dc2626', width: 2 }, marker: { size: 8 } },
                { x: labels, y: nixl, name: 'NIXL Retries', mode: 'lines+markers+text',
                  text: nixl.map(function(v) { return v > 0 ? v.toLocaleString() : ''; }),
                  textposition: 'top center', textfont: { size: 13, color: '#f59e0b' },
                  line: { color: '#f59e0b', width: 2 }, marker: { size: 8 } }
            ];
            if (hasDegraded) {
                var dgLabels = [], dgY = [], dgText = [];
                tc.results.forEach(function(r, i) {
                    if (r.nixl_degraded) { dgLabels.push(labels[i]); dgY.push(totals[i]); dgText.push('DEGRADED — results unreliable'); }
                });
                traces.push({ x: dgLabels, y: dgY, text: dgText, name: 'NIXL Degraded', mode: 'markers',
                    marker: { color: '#dc2626', size: 16, symbol: 'x', line: { width: 3, color: '#dc2626' } },
                    hovertemplate: '<b>%{x}</b><br>%{text}<extra></extra>' });
            }
            Plotly.newPlot(el, traces, {
                height: 850,
                xaxis: { title: '', gridcolor: '#e2e8f0', tickangle: -35, automargin: true },
                yaxis: { title: 'Count', gridcolor: '#e2e8f0' },
                showlegend: true, legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.9)' },
                margin: { t: 20, b: 100, l: 60, r: 20 },
                plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff'
            }, { responsive: true });
        });
    }

    ReportTabs.register({ id: 'traffic', label: 'Traffic', icon: '&#128678;', position: 'end', build: build, render: render });
})();
