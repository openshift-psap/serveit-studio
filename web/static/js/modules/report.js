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
                const goalMap = { ttft: 'TTFT', throughput: 'Throughput', balanced: 'Balanced' };
                const goal = goalMap[(run.goal || '').toLowerCase()] || run.goal || '?';
                let workload = '';
                if (run.isl && run.osl) {
                    workload = `ISL=${run.isl}`;
                    if (run.isl_stdev) workload += `±${run.isl_stdev}`;
                    workload += ` OSL=${run.osl}`;
                    if (run.osl_stdev) workload += `±${run.osl_stdev}`;
                    if (run.turns && run.turns > 1) workload += ` ${run.turns}T`;
                }
                const users = run.num_users ? `${run.num_users}u` : '';
                const gpus = run.max_gpus ? `${run.max_gpus}GPU` : '';
                const date = run.created_at ? new Date(run.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
                const isCompleted = run.status === 'completed';
                const statusLabel = isCompleted ? '\u2705 completed' : '\u274C ' + (run.status || 'unknown');
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
    console.log('[charts] Fetching /api/runs/' + runId + '/charts');
    fetch('/api/runs/' + runId + '/charts')
        .then(r => {
            console.log('[charts] Response:', r.status, r.headers.get('content-type'));
            if (!r.ok) {
                return r.text().then(t => { throw new Error('HTTP ' + r.status + ': ' + t.substring(0, 200)); });
            }
            return r.json();
        })
        .then(data => {
            console.log('[charts] Data received, keys:', Object.keys(data), 'all_results:', (data.all_results||[]).length);
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
            panel.innerHTML = '<div class="charts-loading">Error: ' + err.message + '</div>';
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

    var gpuSizing = data.gpu_sizing || {};
    var pTPSG = gpuSizing.prefill_tpsg || 0;
    var dTPSG = gpuSizing.decode_tpsg || 0;
    var hasTpsg = pTPSG > 0 && dTPSG > 0;

    var bullets = '';
    bullets += '<li><strong>GPU cost per request</strong> = ISL &divide; Prefill TPSG + OSL &divide; Decode TPSG (GPU-seconds)</li>';
    if (hasTpsg) {
        bullets += '<li><strong>Prefill TPSG</strong> = ' + Math.round(pTPSG).toLocaleString() + ' tokens/s/GPU (measured in Step 3 calibration)</li>';
        bullets += '<li><strong>Decode TPSG</strong> = ' + Math.round(dTPSG).toLocaleString() + ' tokens/s/GPU (measured in Step 2 calibration)</li>';
    }
    bullets += '<li><strong>Estimated GPUs</strong> = Tested GPUs &times; (new cost / tested cost) &times; (new concurrency / tested concurrency) &times; turns ratio</li>';
    bullets += '<li>Final GPU count rounded up to nearest <strong>Tensor Parallelism</strong> multiple</li>';
    bullets += '<li>Variance adjusts effective sequence length: effective = mean &times; (1 + CV&sup2;), CV = stdev/mean</li>';
    bullets += '<li><strong>Latency SLA</strong>: compares the tested TTFT at your chosen percentile against your target — workload changes may affect actual latency</li>';

    el.innerHTML = '<div style="text-align:left;"><strong>How GPU estimation works</strong>' +
        '<ul style="margin:8px 0 0 16px;padding:0;list-style:disc;line-height:1.7;">' + bullets + '</ul></div>';
}

function runEstimator(suffix) {
    // Find which report tab owns this estimator via the suffix (e.g., '-rt1')
    const tabId = suffix.replace(/^-/, '');
    const data = tabDataCache[tabId];
    if (!data) return;

    const userConc = parseFloat(document.getElementById('est-concurrency' + suffix).value) || 1;
    const userISL = parseFloat(document.getElementById('est-isl' + suffix).value) || 1;
    const userOSL = parseFloat(document.getElementById('est-osl' + suffix).value) || 1;
    const userIslStdev = parseFloat(document.getElementById('est-isl-stdev' + suffix).value) || 0;
    const userOslStdev = parseFloat(document.getElementById('est-osl-stdev' + suffix).value) || 0;
    const turnsToggle = document.getElementById('est-turns-toggle' + suffix);
    const userTurns = (turnsToggle && turnsToggle.checked) ? (parseFloat(document.getElementById('est-turns' + suffix).value) || 1) : 1;

    const wl = data.recommendation ? data.recommendation.workload : {};
    const testedISL = wl.isl || userISL;
    const testedOSL = wl.osl || userOSL;
    const testedUsers = wl.users || userConc;
    const testedTurns = wl.turns || 1;
    const testedIslStdev = wl.isl_stdev || 0;
    const testedOslStdev = wl.osl_stdev || 0;

    const slaToggle = document.getElementById('est-sla-toggle' + suffix);
    const slaEnabled = slaToggle && slaToggle.checked;
    const slaMs = slaEnabled ? (parseFloat(document.getElementById('est-sla-ms' + suffix).value) || 500) : null;
    const slaPctl = slaEnabled ? (document.getElementById('est-sla-pctl' + suffix).value || 'p99') : null;

    const gpuSizing = data.gpu_sizing || {};
    const latencySearch = data.latency_search || null;
    const eppTuning = data.epp_tuning || null;
    const estData = estimateGPUs(data.all_results || [], testedISL, testedOSL, testedUsers, testedTurns, testedIslStdev, testedOslStdev, userConc, userISL, userOSL, userIslStdev, userOslStdev, userTurns, gpuSizing, slaMs, slaPctl, latencySearch, eppTuning);
    renderEstimatorResults(estData.base, estData.epp, suffix, testedISL, testedOSL, testedUsers, testedTurns, testedIslStdev, testedOslStdev, slaMs, slaPctl);
}

function estimateGPUs(allResults, testedISL, testedOSL, testedUsers, testedTurns, testedIslStdev, testedOslStdev, userConcurrency, userISL, userOSL, userIslStdev, userOslStdev, userTurns, gpuSizing, slaMs, slaPctl, latencySearch, eppTuning) {
    // GPU estimation using additive cost model with TPSG from calibration.
    //
    // GPU cost per request = ISL/prefill_TPSG + OSL/decode_TPSG (GPU-seconds)
    // Total GPUs = cost_per_request × concurrency × headroom × turns
    //
    // Variance increases effective sequence length using Pollaczek-Khinchine:
    //   effective_length = mean × (1 + CV²), where CV = stdev/mean

    function effectiveLen(mean, stdev) {
        if (!stdev || !mean) return mean;
        var cv = stdev / mean;
        return mean * (1 + cv * cv);
    }

    const prefillTPSG = gpuSizing && gpuSizing.prefill_tpsg ? gpuSizing.prefill_tpsg : 0;
    const decodeTPSG = gpuSizing && gpuSizing.decode_tpsg ? gpuSizing.decode_tpsg : 0;
    const headroom = gpuSizing && gpuSizing.headroom ? gpuSizing.headroom : 1.3;

    const testedEffISL = effectiveLen(testedISL, testedIslStdev);
    const testedEffOSL = effectiveLen(testedOSL, testedOslStdev);
    const userEffISL = effectiveLen(userISL, userIslStdev);
    const userEffOSL = effectiveLen(userOSL, userOslStdev);

    // Use TPSG-based additive cost model when calibration data is available
    const hasTpsg = prefillTPSG > 0 && decodeTPSG > 0;
    var costScale;
    if (hasTpsg) {
        var testedCost = testedEffISL / prefillTPSG + testedEffOSL / decodeTPSG;
        var userCost = userEffISL / prefillTPSG + userEffOSL / decodeTPSG;
        costScale = userCost / testedCost;
    } else {
        // Fallback: proportional scaling (less accurate)
        costScale = (userEffISL / testedEffISL) * (userEffOSL / testedEffOSL);
    }

    const concScale = userConcurrency / (testedUsers || userConcurrency);
    const turnsScale = (userTurns || 1) / (testedTurns || 1);
    const totalScale = concScale * costScale * turnsScale;

    // Keep best throughput per architecture
    const bestByArch = {};
    allResults.filter(r => r.throughput_p90 >= 1 && r.gpus > 0).forEach(r => {
        const arch = (r.architecture || 'UNKNOWN').toUpperCase();
        if (!bestByArch[arch] || r.throughput_p90 > bestByArch[arch].throughput_p90) {
            bestByArch[arch] = r;
        }
    });

    // Build EPP entries separately
    const eppByArch = {};
    if (eppTuning && eppTuning.by_architecture) {
        ['aggregated', 'pd'].forEach(function(arch) {
            var trials = (eppTuning.by_architecture || {})[arch];
            if (!trials || !trials.length) return;
            var best = trials.reduce(function(a, b) {
                return (a.throughput_mean || a.throughput_p90 || 0) > (b.throughput_mean || b.throughput_p90 || 0) ? a : b;
            });
            var archKey = arch.toUpperCase();
            var baseEntry = bestByArch[archKey];
            if (best && baseEntry) {
                eppByArch[archKey] = {
                    config_name: best.config_name,
                    architecture: archKey,
                    gpus: baseEntry.gpus,
                    tp: baseEntry.tp || baseEntry.prefill_tp || 1,
                    prefill_tp: baseEntry.prefill_tp,
                    throughput_p50: best.throughput_p50, throughput_p90: best.throughput_p90,
                    throughput_p95: best.throughput_p95, throughput_p99: best.throughput_p99,
                    throughput_mean: best.throughput_mean,
                    ttft_p50: best.ttft_p50, ttft_p90: best.ttft_p90,
                    ttft_p95: best.ttft_p95, ttft_p99: best.ttft_p99,
                };
            }
        });
    }

    function buildEntry(r) {
        var rawGpus = r.gpus * totalScale;
        var tp = r.tp || r.prefill_tp || 1;
        var estGpus = Math.max(tp, Math.ceil(rawGpus / tp) * tp);
        var islScale = userEffISL / testedEffISL;
        var concRatio = userConcurrency / (testedUsers || userConcurrency);
        var ttftScale = islScale * Math.sqrt(concRatio);
        var tputScale = (estGpus / r.gpus) / costScale / turnsScale;

        var est_ttft_p50 = r.ttft_p50 != null ? Math.round(r.ttft_p50 * ttftScale * 10) / 10 : null;
        var est_ttft_p90 = r.ttft_p90 != null ? Math.round(r.ttft_p90 * ttftScale * 10) / 10 : null;
        var est_ttft_p95 = r.ttft_p95 != null ? Math.round(r.ttft_p95 * ttftScale * 10) / 10 : null;
        var est_ttft_p99 = r.ttft_p99 != null ? Math.round(r.ttft_p99 * ttftScale * 10) / 10 : null;
        var est_tput_mean = r.throughput_mean != null ? Math.round(r.throughput_mean * tputScale * 100) / 100 : null;
        var est_tput_p90 = r.throughput_p90 != null ? Math.round(r.throughput_p90 * tputScale * 100) / 100 : null;

        var sla_ttft = null, sla_meets = null, sla_gpus_needed = null;
        if (slaMs && slaPctl) {
            var estTtftAtPctl = slaPctl === 'p50' ? est_ttft_p50 : (slaPctl === 'p90' ? est_ttft_p90 : (slaPctl === 'p95' ? est_ttft_p95 : est_ttft_p99));
            sla_ttft = estTtftAtPctl;
            if (sla_ttft != null) {
                sla_meets = sla_ttft <= slaMs;
                if (!sla_meets) {
                    var slaRatio = sla_ttft / slaMs;
                    var rawSlaGpus = estGpus * slaRatio;
                    sla_gpus_needed = Math.max(tp, Math.ceil(rawSlaGpus / tp) * tp);
                }
            }
        }
        return {
            config_name: r.config_name, chart_label: r.config_name,
            architecture: r.architecture, tested_gpus: r.gpus,
            estimated_gpus: estGpus, tp: tp,
            cost_model: hasTpsg ? 'tpsg' : 'proportional',
            prefill_tpsg: prefillTPSG, decode_tpsg: decodeTPSG,
            ttft_p50: est_ttft_p50, ttft_p90: est_ttft_p90, ttft_p95: est_ttft_p95, ttft_p99: est_ttft_p99,
            throughput_mean: est_tput_mean, throughput_p90: est_tput_p90,
            sla_target_ms: slaMs, sla_percentile: slaPctl,
            sla_ttft: sla_ttft, sla_meets: sla_meets, sla_gpus_needed: sla_gpus_needed,
        };
    }

    var baseResults = Object.values(bestByArch).map(buildEntry).sort(function(a, b) { return a.estimated_gpus - b.estimated_gpus; });
    var eppResults = Object.values(eppByArch).map(buildEntry).sort(function(a, b) { return a.estimated_gpus - b.estimated_gpus; });
    return { base: baseResults, epp: eppResults };
}

function renderEstimatorResults(results, eppResults, suffix, testedISL, testedOSL, testedUsers, testedTurns, testedIslStdev, testedOslStdev, slaMs, slaPctl) {
    if (results.length === 0 && eppResults.length === 0) {
        document.getElementById('est-results' + suffix).innerHTML = '<p style="color:#6b7280;padding:16px;">No valid configurations to estimate.</p>';
        return;
    }

    const bestGpus = results.length ? results[0].estimated_gpus : (eppResults.length ? eppResults[0].estimated_gpus : 0);

    var v = function(x, u) { return x != null ? x + ' ' + u : '-'; };
    var sep = 'border-left:3px solid #e2e8f0;';
    var hasSla = slaMs && slaPctl;

    function buildTable(rows, title) {
        var t = '';
        if (title) t += '<div style="font-weight:700;font-size:0.9em;color:#1e293b;margin:16px 0 8px;padding-top:12px;border-top:1px solid #e2e8f0;">' + title + '</div>';
        t += '<table class="estimator-table"><thead>' +
            '<tr><th rowspan="2">Configuration</th><th rowspan="2">Arch</th><th rowspan="2">TP</th>' +
            '<th rowspan="2">Tested<br>GPUs</th><th rowspan="2">Est.<br>GPUs</th>' +
            '<th colspan="4" style="text-align:center;' + sep + '">Est. TTFT (ms)</th>' +
            '<th rowspan="2" style="text-align:center;' + sep + '">Est. Throughput<br>Mean (req/s)</th>' +
            (hasSla ? '<th rowspan="2" style="' + sep + '">SLA<br>' + slaPctl.toUpperCase() + ' &le; ' + slaMs + 'ms</th>' : '') +
            '</tr><tr>' +
            '<th style="' + sep + '">P50</th><th>P90</th><th>P95</th><th>P99</th>' +
            '</tr></thead><tbody>';
        rows.forEach(function(r) {
            var isBest = r.estimated_gpus === bestGpus;
            var slaCell = '';
            if (hasSla) {
                if (r.sla_meets === true) {
                    slaCell = '<td style="' + sep + 'color:#059669;font-weight:700;text-align:center;">PASS<br><span style="font-weight:400;font-size:0.85em;">' + (r.sla_ttft != null ? r.sla_ttft.toFixed(1) + ' ms' : '-') + '</span></td>';
                } else if (r.sla_meets === false) {
                    var gpuHint = r.sla_gpus_needed ? '<br><span style="font-weight:400;font-size:0.82em;">Need ' + r.sla_gpus_needed + ' GPUs</span>' : '';
                    slaCell = '<td style="' + sep + 'color:#d97706;font-weight:700;text-align:center;">' + (r.sla_ttft != null ? r.sla_ttft.toFixed(1) + ' ms' : '-') + gpuHint + '</td>';
                } else {
                    slaCell = '<td style="' + sep + 'color:#64748b;text-align:center;">N/A</td>';
                }
            }
            var tputMean = r.throughput_mean != null ? r.throughput_mean.toFixed(2) : (r.throughput_p90 != null ? r.throughput_p90.toFixed(2) : '-');
            t += '<tr class="' + (isBest ? 'estimator-best' : '') + '">' +
                '<td>' + r.config_name + '</td>' +
                '<td><span class="arch-badge arch-' + r.architecture.toLowerCase() + '">' + r.architecture + '</span></td>' +
                '<td>' + r.tp + '</td><td>' + r.tested_gpus + '</td>' +
                '<td><strong>' + r.estimated_gpus + '</strong></td>' +
                '<td style="' + sep + '">' + v(r.ttft_p50, '') + '</td>' +
                '<td>' + v(r.ttft_p90, '') + '</td><td>' + v(r.ttft_p95, '') + '</td>' +
                '<td>' + v(r.ttft_p99, '') + '</td>' +
                '<td style="' + sep + 'text-align:center;font-weight:600;">' + tputMean + '</td>' +
                slaCell + '</tr>';
        });
        t += '</tbody></table>';
        return t;
    }

    let html = buildTable(results, null);
    if (eppResults.length > 0) {
        html += buildTable(eppResults, 'With EPP-Tuned Routing');
    }
    document.getElementById('est-results' + suffix).innerHTML = html;

    // Grouped bar chart: tested vs estimated GPUs (combined base + EPP)
    var allForChart = results.concat(eppResults.map(function(r) { return Object.assign({}, r, { chart_label: r.chart_label + ' (EPP)' }); }));
    const labels = allForChart.map(r => r.chart_label);
    Plotly.newPlot('est-chart' + suffix, [
        {
            type: 'bar',
            name: 'Tested GPUs',
            y: labels,
            x: allForChart.map(r => r.tested_gpus),
            text: allForChart.map(r => r.tested_gpus + ' GPUs'),
            textposition: 'inside',
            orientation: 'h',
            marker: { color: '#94a3b8' },
            hovertemplate: '%{y}<br>Tested: %{x} GPUs<extra></extra>',
        },
        {
            type: 'bar',
            name: 'Estimated GPUs',
            y: labels,
            x: allForChart.map(r => r.sla_gpus_needed || r.estimated_gpus),
            text: allForChart.map(r => (r.sla_gpus_needed || r.estimated_gpus) + ' GPUs'),
            textposition: 'outside',
            orientation: 'h',
            marker: { color: allForChart.map(r => r.sla_gpus_needed ? '#ef4444' : '#d97706') },
            hovertemplate: '%{y}<br>Estimated: %{x} GPUs<extra></extra>',
        }
    ], {
        title: { text: 'GPU Requirements: Tested vs Estimated', font: { size: 15 } },
        xaxis: { title: 'GPUs' },
        yaxis: { automargin: true },
        barmode: 'group',
        margin: { l: 200, r: 140, t: 50, b: 40 },
        height: Math.max(300, allForChart.length * 80 + 80),
        legend: { orientation: 'v', x: 1.02, y: 1, xanchor: 'left' },
    }, { responsive: true, displayModeBar: false });
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

