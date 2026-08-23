// report.js — Tabbed report management, estimator, tab switching

// ===== TABBED REPORT MANAGEMENT =====
var reportTabs = [];
var activeTabId = null;
var tabDataCache = {};
var _tabCounter = 0;
var _chartSuffix = '';
function cid(id) { return id + _chartSuffix; }

document.getElementById('chart-add-btn').addEventListener('click', () => {
    const runId = document.getElementById('chart-run-select').value;
    if (runId) addReportTab(runId);
});

document.getElementById('chart-compare-btn').addEventListener('click', function() { generateComparison(); });

function loadRunList() {
    fetch('/api/runs')
        .then(r => r.json())
        .then(runs => {
            const sel = document.getElementById('chart-run-select');
            sel.innerHTML = '';
            if (!runs.length) {
                sel.innerHTML = '<option value="">No runs found</option>';
                return;
            }
            runs.forEach(run => {
                const opt = document.createElement('option');
                opt.value = run.id;
                const modelShort = (run.model || '').split('/').pop() || '?';
                const goalMap = { ttft: 'TTFT', throughput: 'Throughput', balanced: 'Full Coverage' };
                const goal = goalMap[(run.goal || '').toLowerCase()] || run.goal || '?';
                let workload = '';
                if (run.isl && run.osl) {
                    let rcfg = null;
                    try { rcfg = run.config_json ? JSON.parse(run.config_json) : null; } catch(e) {}
                    const cpt2 = (rcfg && rcfg.chars_per_token) || 4.5;
                    const dIsl = (rcfg && rcfg.isl_original_chars) || Math.round(run.isl * cpt2);
                    const dOsl = (rcfg && rcfg.osl_original_chars) || Math.round(run.osl * cpt2);
                    const dIslStd = run.isl_stdev ? ((rcfg && rcfg.isl_stdev_original_chars) || Math.round(run.isl_stdev * cpt2)) : null;
                    workload = `${run.isl}`;
                    if (run.isl_stdev) workload += `+${run.isl_stdev}`;
                    workload += ` (${dIsl.toLocaleString()} chars)`;
                    workload += ` / ${run.osl} (${dOsl.toLocaleString()} chars)`;
                    if (run.turns && run.turns > 1) workload += ` ${run.turns}T`;
                }
                const users = run.num_users ? `${run.num_users}u` : '';
                const gpus = run.max_gpus ? `${run.max_gpus}GPU` : '';
                const date = run.created_at ? new Date(run.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
                const _st = run.status || 'unknown';
                const statusLabel = _st.indexOf('completed') === 0 ? '\u2705 completed' : _st === 'running' ? '\u23F3 running' : _st === 'stopped' ? '\u23F9 stopped' : _st === 'interrupted' ? '\u23F9 interrupted' : '\u274C ' + _st;
                const desc = run.notes ? `"${run.notes}"` : '';
                let imgTag = '';
                if (run.config_json) {
                    try { const rc = JSON.parse(run.config_json); imgTag = rc.image ? rc.image.split(':').pop() : ''; } catch(e) {}
                }
                const parts = [`#${run.id}`, desc, goal, modelShort, workload, users, gpus, imgTag, statusLabel, date].filter(Boolean);
                opt.textContent = parts.join(' | ');
                sel.appendChild(opt);
            });
            // No auto-load — let the user choose
        })
        .catch(err => {
            document.getElementById('charts-content').innerHTML =
                `<div class="charts-loading">Failed to load runs: ${err.message}</div>`;
        });
}

function addReportTab(runId) {
    // If tab for this run already exists, just switch to it
    const existing = reportTabs.find(t => t.runId == runId && !t.isComparison);
    if (existing) { switchReportTab(existing.id); return; }

    const tabId = 'rt' + (++_tabCounter);
    const sel = document.getElementById('chart-run-select');
    const opt = sel.querySelector('option[value="' + runId + '"]');
    const label = opt ? opt.textContent : 'Run #' + runId;

    reportTabs.push({ id: tabId, runId: runId, label: label, isComparison: false });

    // Remove placeholder text if present
    const placeholder = document.querySelector('#charts-content > .charts-loading');
    if (placeholder) placeholder.remove();

    // Create panel
    const panel = document.createElement('div');
    panel.id = 'panel-' + tabId;
    panel.className = 'report-tab-panel';
    panel.innerHTML = '<div class="charts-loading">Loading charts...</div>';
    document.getElementById('charts-content').appendChild(panel);

    updateTabBar();
    switchReportTab(tabId);

    // Fetch data and render
    fetch('/api/runs/' + runId + '/charts')
        .then(r => {
            if (!r.ok) {
                return r.json().catch(() => ({})).then(j => {
                    var msg = j.error || 'No results found for this run';
                    throw new Error(msg);
                });
            }
            return r.json();
        })
        .then(data => {
            if (data.error) {
                panel.innerHTML = '<div class="charts-loading">' + data.error + '</div>';
                return;
            }
            tabDataCache[tabId] = data;
            try {
                renderChartsInPanel(data, runId, tabId);
            } catch (renderErr) {
                console.error('Chart render error:', renderErr);
                panel.innerHTML = '<div class="charts-loading">Render error: ' + renderErr.message + '</div>';
                return;
            }
            // Update download link if this is still the active tab
            if (activeTabId === tabId) {
                const dlLink = document.getElementById('chart-download-link');
                dlLink.style.display = 'inline';
                dlLink.href = '#';
                dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(runId, data); };
            }
        })
        .catch(err => {
            panel.innerHTML = '<div class="charts-loading">' + err.message + '</div>';
        });
}

function renderChartsInPanel(data, runId, tabId) {
    const panel = document.getElementById('panel-' + tabId);
    const origContent = document.getElementById('charts-content');
    _chartSuffix = '-' + tabId;

    // Temporarily swap IDs so renderCharts writes to the panel
    panel.id = 'charts-content';
    origContent.id = '_charts-content-swap';

    renderCharts(data, runId);

    // Restore IDs
    panel.id = 'panel-' + tabId;
    origContent.id = 'charts-content';
    _chartSuffix = '';
}

function updateTabBar() {
    const bar = document.getElementById('report-tab-bar');
    const compareBtn = document.getElementById('chart-compare-btn');
    const runTabs = reportTabs.filter(t => !t.isComparison);

    if (reportTabs.length === 0) {
        bar.style.display = 'none';
        compareBtn.style.display = 'none';
        return;
    }

    bar.style.display = 'flex';
    compareBtn.style.display = runTabs.length >= 2 ? 'inline-block' : 'none';

    bar.innerHTML = '';
    reportTabs.forEach(tab => {
        const el = document.createElement('div');
        el.className = 'report-tab' + (tab.id === activeTabId ? ' active' : '') + (tab.isComparison ? ' compare-tab' : '');
        const shortLabel = tab.isComparison ? 'Compare' : '#' + tab.runId;
        el.innerHTML = '<span class="tab-label" title="' + (tab.label || '').replace(/"/g, '&quot;') + '">' + shortLabel + '</span><span class="tab-close" data-tab-id="' + tab.id + '">&times;</span>';
        el.addEventListener('click', (e) => {
            if (!e.target.classList.contains('tab-close')) switchReportTab(tab.id);
        });
        el.querySelector('.tab-close').addEventListener('click', (e) => {
            e.stopPropagation();
            closeReportTab(tab.id);
        });
        bar.appendChild(el);
    });

}

function updateEstimatorScaling(suffix) {
    var tabId = suffix.replace(/^-/, '');
    var data = tabDataCache[tabId];
    if (!data) return;
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

function runEstimator(suffix) {
    var tabId = suffix.replace(/^-/, '');
    var data = tabDataCache[tabId];
    if (!data) return;

    var targetTput = parseFloat(document.getElementById('est-tput-target' + suffix).value) || 100;
    var ttftTarget = parseFloat(document.getElementById('est-ttft-target' + suffix).value) || 500;
    var ttftPctl = document.getElementById('est-ttft-pctl' + suffix).value || 'p99';
    var itlTarget = parseFloat(document.getElementById('est-itl-target' + suffix).value) || 20;
    var itlPctl = document.getElementById('est-itl-pctl' + suffix).value || 'p90';

    var allResults = (data.all_results || []).filter(function(r) {
        return r.quality !== 'discard' && r.gpus > 0 && (r.throughput_mean > 0 || r.throughput_p90 > 0);
    });

    var bestByConfig = {};
    allResults.forEach(function(r) {
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
        var gpusPerReplica = r.gpus;
        var replicas = Math.ceil(targetTput / tput);
        var totalGpus = replicas * gpusPerReplica;

        var ttftVal = ttftPctl === 'p50' ? r.ttft_p50 : (ttftPctl === 'p90' ? r.ttft_p90 : (ttftPctl === 'p95' ? r.ttft_p95 : r.ttft_p99));
        var ttftMeets = ttftVal != null ? ttftVal <= ttftTarget : null;

        var itlVal = itlPctl === 'p50' ? r.itl_p50 : (itlPctl === 'p90' ? r.itl_p90 : (itlPctl === 'p95' ? r.itl_p95 : r.itl_p99));
        var itlMeets = itlVal != null && itlVal > 0 ? itlVal <= itlTarget : null;

        results.push({
            config_name: key, architecture: (r.architecture || 'UNKNOWN').toUpperCase(),
            gpus_per_replica: gpusPerReplica, replicas: replicas, total_gpus: totalGpus,
            measured_tput: tput, target_tput: targetTput,
            ttft_p50: r.ttft_p50, ttft_p90: r.ttft_p90, ttft_p95: r.ttft_p95, ttft_p99: r.ttft_p99,
            ttft_val: ttftVal, ttft_meets: ttftMeets, ttft_target: ttftTarget, ttft_pctl: ttftPctl,
            itl_p90: r.itl_p90, itl_p95: r.itl_p95, itl_p99: r.itl_p99,
            itl_val: itlVal, itl_meets: itlMeets, itl_target: itlTarget, itl_pctl: itlPctl,
            concurrency: r.concurrency,
        });
    });

    results.sort(function(a, b) { return a.total_gpus - b.total_gpus; });
    renderEstimatorResults(results, suffix);
}

function renderEstimatorResults(results, suffix) {
    _lastEstResults[suffix] = results;
    if (!results.length) {
        document.getElementById('est-results' + suffix).innerHTML = '<p style="color:#6b7280;padding:16px;">No valid configurations to estimate.</p>';
        return;
    }

    var bestGpus = results[0].total_gpus;
    var sep = 'border-left:3px solid #e2e8f0;';
    var fmtMs = function(v) { return v != null ? Math.round(v).toLocaleString() : '-'; };

    var t = '<table class="estimator-table" style="margin-top:16px;"><thead>';
    t += '<tr><th>Configuration</th><th>Arch</th><th>GPUs/replica</th><th>Measured req/s</th>';
    t += '<th style="' + sep + '">Replicas</th><th><strong>Total GPUs</strong></th>';
    t += '<th style="' + sep + '">TTFT ' + results[0].ttft_pctl.toUpperCase() + '</th>';
    t += '<th>ITL ' + results[0].itl_pctl.toUpperCase() + '</th>';
    t += '<th style="' + sep + '">SLA Status</th></tr></thead><tbody>';

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
        t += '<td>' + r.config_name + '</td>';
        t += '<td><span class="arch-badge arch-' + r.architecture.toLowerCase() + '">' + r.architecture + '</span></td>';
        t += '<td>' + r.gpus_per_replica + '</td>';
        t += '<td>' + r.measured_tput.toFixed(2) + '</td>';
        t += '<td style="' + sep + '">' + r.replicas + '</td>';
        t += '<td><strong>' + r.total_gpus.toLocaleString() + '</strong></td>';
        t += '<td style="' + sep + 'color:' + ttftColor + ';font-weight:600;">' + fmtMs(r.ttft_val) + ' ms</td>';
        t += '<td style="color:' + itlColor + ';font-weight:600;">' + (r.itl_val != null ? r.itl_val.toFixed(2) : '-') + ' ms</td>';
        t += '<td style="' + sep + '">' + slaStatus + '</td>';
        t += '</tr>';
    });
    t += '</tbody></table>';

    document.getElementById('est-results' + suffix).innerHTML = t;

    var labels = results.map(function(r) { return r.config_name; });
    var barColors = results.map(function(r) {
        var passes = r.ttft_meets !== false && r.itl_meets !== false;
        if (r.total_gpus === bestGpus && passes) return '#059669';
        if (!passes) return '#ef4444';
        return '#d97706';
    });
    Plotly.newPlot('est-chart' + suffix, [{
        type: 'bar', orientation: 'h',
        y: labels, x: results.map(function(r) { return r.total_gpus; }),
        text: results.map(function(r) {
            var t = r.total_gpus.toLocaleString() + ' GPUs (' + r.replicas + ' replicas)';
            if (r.ttft_meets === false) t += ' [TTFT FAIL]';
            if (r.itl_meets === false) t += ' [ITL FAIL]';
            return t;
        }),
        textposition: 'outside',
        marker: { color: barColors },
        hovertemplate: '<b>%{y}</b><br>%{x} GPUs<br>%{text}<extra></extra>',
    }], {
        title: { text: 'GPUs needed for ' + results[0].target_tput + ' req/s', font: { size: 15 } },
        xaxis: { title: 'Total GPUs' },
        yaxis: { automargin: true },
        margin: { l: 200, r: 180, t: 50, b: 40 },
        height: Math.max(300, results.length * 60 + 80),
    }, { responsive: true, displayModeBar: false });

}

var _lastEstResults = {};
function downloadEstimatorReport(suffix) {
    var results = _lastEstResults[suffix];
    if (!results || !results.length) { alert('Run the estimator first'); return; }
    var bestGpus = results[0].total_gpus;
    var chartEl = document.getElementById('est-chart' + suffix);
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
}

function switchReportTab(tabId) {
    activeTabId = tabId;
    // Toggle panel visibility
    document.querySelectorAll('#charts-content > .report-tab-panel').forEach(p => {
        p.classList.toggle('active', p.id === 'panel-' + tabId);
    });
    updateTabBar();

    // Update download link
    const tab = reportTabs.find(t => t.id === tabId);
    const dlLink = document.getElementById('chart-download-link');
    if (tab && !tab.isComparison && tabDataCache[tabId]) {
        dlLink.style.display = 'inline';
        dlLink.href = '#';
        dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(tab.runId, tabDataCache[tabId]); };
    } else {
        dlLink.style.display = 'none';
    }
}

function closeReportTab(tabId) {
    const idx = reportTabs.findIndex(t => t.id === tabId);
    if (idx === -1) return;
    reportTabs.splice(idx, 1);
    delete tabDataCache[tabId];

    const panel = document.getElementById('panel-' + tabId);
    if (panel) panel.remove();

    if (activeTabId === tabId) {
        if (reportTabs.length > 0) {
            switchReportTab(reportTabs[Math.min(idx, reportTabs.length - 1)].id);
        } else {
            activeTabId = null;
            document.getElementById('chart-download-link').style.display = 'none';
            // Show placeholder
            const content = document.getElementById('charts-content');
            if (!content.querySelector('.charts-loading')) {
                const ph = document.createElement('div');
                ph.className = 'charts-loading';
                ph.textContent = 'Select a run and click + Add to view results';
                content.appendChild(ph);
            }
        }
    }
    updateTabBar();
}

function fmtSI(v, decimals) {
    if (v == null) return '-';
    const d = decimals != null ? decimals : 1;
    if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(d) + 'M';
    if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(d) + 'K';
    return v.toFixed(d);
}

function arrowAnnotations(xs, ys, opts) {
    const color = (opts && opts.color) || '#333';
    const decimals = opts && opts.decimals != null ? opts.decimals : 1;
    const suffix = (opts && opts.suffix) || '';
    const yref = (opts && opts.yref) || 'y';
    const s = (opts && opts.spread) || 30;
    const offsets = [
        { ax: 0,          ay: -s        },
        { ax:  s * 0.9,   ay:  s * 0.7  },
        { ax: -s * 0.8,   ay: -s * 1.2  },
        { ax:  s * 1.1,   ay: -s * 0.5  },
        { ax: 0,          ay:  s * 1.1   },
        { ax: -s,         ay:  s * 0.8   },
        { ax:  s * 1.3,   ay: -s * 1.3   },
        { ax: -s * 1.2,   ay:  s * 1.3   },
    ];
    return ys.map((v, i) => {
        if (v == null) return null;
        const o = offsets[i % offsets.length];
        return {
            x: xs[i], y: v, xref: 'x', yref: yref,
            text: fmtSI(v, decimals) + suffix,
            showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: '#94a3b8',
            ax: o.ax, ay: o.ay,
            font: { size: 10, color: color },
            borderpad: 2,
        };
    }).filter(Boolean);
}

