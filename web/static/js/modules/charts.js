// charts.js — Plotly chart rendering for all report visualizations

// Sort EPP table by column index (num = numeric, str = string)
window._sortEppTable = function(th, colIdx, type) {
    const table = th.closest('table');
    const tbody = table;
    const rows = [...table.querySelectorAll('tr')].slice(1); // skip header
    if (!rows.length) return;
    // toggle direction (store on the table element)
    if (table._sortCol === colIdx) table._sortDir = -table._sortDir;
    else { table._sortCol = colIdx; table._sortDir = 1; }
    const dir = table._sortDir;
    rows.sort((a, b) => {
        const ca = a.children[colIdx], cb = b.children[colIdx];
        let va, vb;
        if (type === 'num') {
            va = parseFloat(ca.getAttribute('sort-num') || ca.textContent.replace(/[^0-9.\-]/g, '')) || 0;
            vb = parseFloat(cb.getAttribute('sort-num') || cb.textContent.replace(/[^0-9.\-]/g, '')) || 0;
        } else {
            va = ca.textContent.toLowerCase();
            vb = cb.textContent.toLowerCase();
        }
        if (va < vb) return -1 * dir;
        if (va > vb) return 1 * dir;
        return 0;
    });
    // re-append sorted rows
    rows.forEach(r => table.appendChild(r));
    // visual indicator
    table.querySelectorAll('th').forEach(h => h.style.color = '');
    th.style.color = dir === 1 ? '#059669' : '#dc2626';
};

function renderCharts(data, runId) {
    var content = document.getElementById('charts-content');
    try { _renderChartsImpl(data, runId, content); } catch(e) {
        console.error('renderCharts error:', e);
        if (content) content.innerHTML = '<div class="charts-loading" style="color:red;">Chart render error: ' + e.message + '<br><pre style="white-space:pre-wrap;font-size:11px;">' + e.stack + '</pre></div>';
    }
}
function _renderChartsImpl(data, runId, content) {

    const summary = data.summary;
    const chartQueue = [];
    const charts = data.charts;
    const rec = data.recommendation;

    // Download link handled by tab management
    const dlLink = document.getElementById('chart-download-link');
    if (dlLink) {
        dlLink.style.display = 'inline';
        dlLink.href = '#';
        dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(runId, data); };
    }

    let html = '';
    

    // Lookup from test_id/config_name to all_results entry (used across multiple sections)
    const _allResLookup = {};
    (data.all_results || []).forEach(function(r) { _allResLookup[r.test_id || r.config_name] = r; });

    // Shared recommendation card builder
    function _buildRecCard(opts) {
        // opts: label, icon, desc, color, archKey, deploy, tput, gpus, conc,
        //       ttft_p90, ttft_p95, ttft_p99, itl_p90, itl_p95, itl_p99,
        //       recId, testId, manifests, runId, extraBadges (array of {text, bg, color}),
        //       extraInfo
        var archColors = { pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed' };
        var aColor = archColors[opts.archKey] || '#64748b';
        var archLabel = (opts.archKey || 'aggregated').toUpperCase();

        var h = '';
        h += '<div style="background:white;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);border:2px solid #cbd5e1;">';

        // Header
        h += '<div style="background:linear-gradient(135deg,' + opts.color + ',' + opts.color + 'cc);padding:10px 14px;color:white;">';
        h += '<div style="display:flex;align-items:center;justify-content:space-between;">';
        h += '<div>';
        h += '<div style="font-weight:700;font-size:0.85em;">' + (opts.icon || '') + ' ' + opts.label + '</div>';
        h += '<div style="display:flex;gap:4px;margin-top:2px;">';
        h += '<div style="font-size:0.65em;background:' + aColor + ';padding:2px 6px;border-radius:10px;font-weight:600;">' + archLabel + '</div>';
        if (opts.extraBadges) {
            opts.extraBadges.forEach(function(b) {
                h += '<div style="font-size:0.65em;background:' + (b.bg || 'rgba(255,255,255,0.2)') + ';color:' + (b.color || 'white') + ';padding:2px 6px;border-radius:10px;font-weight:600;">' + b.text + '</div>';
            });
        }
        h += '</div>';
        h += '</div></div>';
        if (opts.desc) h += '<div style="font-size:0.7em;opacity:0.9;margin-top:1px;">' + opts.desc + '</div>';
        h += '</div>';

        // Body
        h += '<div style="padding:12px 14px;">';
        h += '<div style="font-size:1.1em;font-weight:800;color:#0f172a;margin-bottom:8px;">' + (opts.deploy || '') + '</div>';

        // Metrics line
        h += '<div style="display:flex;gap:12px;font-size:0.85em;color:#475569;margin-bottom:4px;flex-wrap:wrap;">';
        if (opts.tput != null) h += '<span>Throughput: <strong>' + (typeof opts.tput === 'number' ? opts.tput.toFixed(2) + ' req/s' : opts.tput) + '</strong></span>';
        if (opts.gpus) h += '<span>GPUs: <strong>' + opts.gpus + '</strong></span>';
        if (opts.conc) h += '<span style="color:#0ea5e9;font-weight:600;">' + opts.conc + '</span>';
        h += '</div>';
        if (opts.vllm_tps != null) {
            h += '<div style="font-size:0.8em;color:#475569;margin-bottom:4px;">';
            h += '<span>vLLM: <strong style="color:#3b82f6;">' + Math.round(opts.vllm_tps).toLocaleString() + ' tok/s</strong> <span style="font-size:0.85em;color:#94a3b8;">(server total)</span></span>';
            h += '</div>';
        }

        // Extra info line (for EPP weights etc)
        if (opts.extraInfo) h += '<div style="font-size:0.8em;color:#7c3aed;margin-top:2px;">' + opts.extraInfo + '</div>';

        // Percentile table
        var _fmtE2e = function(v) { return v != null ? (v >= 1000 ? (v/1000).toFixed(1) + ' s' : Math.round(v) + ' ms') : '-'; };
        h += '<table style="width:100%;font-size:0.8em;border-collapse:collapse;margin-top:6px;">';
        h += '<tr style="color:#94a3b8;font-weight:600;"><td></td><td>TTFT</td><td>E2E</td><td>ITL</td></tr>';
        if (opts.ttft_p50 != null) h += '<tr><td style="font-weight:600;color:#94a3b8;">P50</td><td style="color:#64748b;">' + Math.round(opts.ttft_p50).toLocaleString() + ' ms</td><td style="color:#64748b;">' + _fmtE2e(opts.e2e_p50) + '</td><td style="color:#64748b;">' + (opts.itl_p50 ? parseFloat(opts.itl_p50).toFixed(2) + ' ms' : '-') + '</td></tr>';
        h += '<tr><td style="font-weight:600;color:#475569;">P90</td><td style="font-weight:700;color:#1e293b;">' + (opts.ttft_p90 != null ? Math.round(opts.ttft_p90).toLocaleString() + ' ms' : '-') + '</td><td>' + _fmtE2e(opts.e2e_p90) + '</td><td>' + (opts.itl_p90 ? parseFloat(opts.itl_p90).toFixed(2) + ' ms' : '-') + '</td></tr>';
        if (opts.ttft_p95) h += '<tr><td style="font-weight:600;color:#475569;">P95</td><td style="color:#64748b;">' + Math.round(opts.ttft_p95).toLocaleString() + ' ms</td><td style="color:#64748b;">' + _fmtE2e(opts.e2e_p95) + '</td><td>' + (opts.itl_p95 ? parseFloat(opts.itl_p95).toFixed(2) + ' ms' : '-') + '</td></tr>';
        if (opts.ttft_p99) h += '<tr><td style="font-weight:600;color:#475569;">P99</td><td style="color:#64748b;">' + Math.round(opts.ttft_p99).toLocaleString() + ' ms</td><td style="color:#64748b;">' + _fmtE2e(opts.e2e_p99) + '</td><td>' + (opts.itl_p99 ? parseFloat(opts.itl_p99).toFixed(2) + ' ms' : '-') + '</td></tr>';
        h += '</table>';

        // Action buttons
        if (opts.recId || (opts.manifests && opts.manifests.length)) {
            h += '<div style="margin-top:10px;padding-top:8px;border-top:1px solid #f1f5f9;">';
            if (opts.recId) {
                h += '<button onclick="applyReportConfig(\'' + opts.recId + '\')" style="width:100%;background:' + opts.color + '0a;border:1.5px solid ' + opts.color + '40;border-radius:6px;padding:5px 8px;font-size:11px;font-weight:600;color:' + opts.color + ';cursor:pointer;margin-bottom:4px;" onmouseover="this.style.background=\'' + opts.color + '15\'" onmouseout="this.style.background=\'' + opts.color + '0a\'">&#128260; Reuse</button>';
                h += '<button onclick="showSingleTestModal(\'' + opts.recId + '\')" style="width:100%;background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:6px;padding:5px 8px;font-size:11px;font-weight:600;color:#475569;cursor:pointer;margin-bottom:4px;" onmouseover="this.style.background=\'#f1f5f9\'" onmouseout="this.style.background=\'#f8fafc\'">&#129514; Test</button>';
            }
            if (opts.manifests && opts.manifests.length && opts.runId && opts.testId) {
                var _ml = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
                h += '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px;">';
                opts.manifests.forEach(function(t) {
                    if (t.includes('service')) return;
                    h += '<a href="/api/run/' + opts.runId + '/config/' + opts.testId + '/manifest/' + t + '" target="_blank" style="color:#0ea5e9;font-size:10px;padding:2px 6px;background:#f0f9ff;border-radius:4px;border:1px solid #bae6fd;font-weight:500;text-decoration:none;">' + (_ml[t] || t) + ' &#8595;</a>';
                });
                h += '</div>';
            }
            h += '</div>';
        }

        h += '</div></div>';
        return h;
    }

    function _tagLabel(name, arch) {
        if (!name || name.indexOf('(') >= 0) return name || '';
        var a = (arch || '').toUpperCase();
        if (a === 'PD') return name + ' (PD)';
        if (a === 'EP') return name + ' (EP)';
        return name + ' (AG)';
    }

    // Filter out calibration, sweep tests, and discarded tests from configuration charts
    var coreResults = (data.all_results || []).filter(function(r) {
        if (r.quality === 'discard') return false;
        var tid = r.test_id || r.config_name || '';
        return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
               tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
    });

    // Build a lookup from test_id -> manifest_types for download links
    const manifestLookup = {};
    const testIdLookup = {};
    (data.all_results || []).forEach(r => {
        const tid = r.test_id || r.config_name;
        if (r.manifest_types && r.manifest_types.length) manifestLookup[tid] = r.manifest_types;
        testIdLookup[r.config_name] = tid;
    });

    // ============================================================
    // GOAL BANNER — what was this run optimizing for
    // ============================================================
    if (rec && rec.goal_info) {
        const gColors = { ttft: '#3b82f6', throughput: '#f59e0b', full_coverage: '#10b981', balanced: '#10b981', aggregated_only: '#64748b', pd_only: '#8b5cf6', ep_only: '#0ea5e9' };
        const gIcons = { ttft: '&#9201;', throughput: '&#9889;', full_coverage: '&#9878;', balanced: '&#9878;', aggregated_only: '&#9634;', pd_only: '&#8644;', ep_only: '&#9881;' };
        const gc = gColors[rec.goal] || '#10b981';
        html += `<div class="chart-card" style="border: 3px solid ${gc}; border-left: 8px solid ${gc};">`;
        html += `<div class="chart-card-header" style="background: ${gc}; color: white; font-size: 1.3em;">`;
        html += `Deployment Recommendation <span style="font-size:0.7em;font-weight:400;opacity:0.85;">&mdash; ${rec.goal_info.name}</span></div>`;
        html += `<div style="background:${gc}dd; color:white; padding:8px 20px; font-size:0.92em; display:flex; flex-wrap:wrap; gap:6px 20px;">`;
        html += `<span>Model: <strong>${rec.model}</strong></span>`;
        var bCpt = rec.workload.chars_per_token || 4.5;
        var bIslC = rec.workload.isl_original_chars || Math.round(rec.workload.isl * bCpt);
        var bOslC = rec.workload.osl_original_chars || Math.round(rec.workload.osl * bCpt);
        var bIslStdC = rec.workload.isl_stdev ? (rec.workload.isl_stdev_original_chars || Math.round(rec.workload.isl_stdev * bCpt)) : null;
        let wlLabel = `Prompt: <strong>${rec.workload.isl.toLocaleString()}</strong>`;
        if (rec.workload.isl_stdev) wlLabel += ` + σ=${rec.workload.isl_stdev.toLocaleString()}`;
        wlLabel += ` <span style="opacity:0.8;">(${bIslC.toLocaleString()} chars)</span>`;
        wlLabel += ` | Output: <strong>${rec.workload.osl.toLocaleString()}</strong>`;
        if (rec.workload.osl_stdev) wlLabel += ` + σ=${rec.workload.osl_stdev.toLocaleString()}`;
        wlLabel += ` <span style="opacity:0.8;">(${bOslC.toLocaleString()} chars)</span>`;
        var _rc = data.run_config || {};
        var _turns = rec.workload.turns > 1 ? rec.workload.turns : (_rc.turns > 1 ? _rc.turns : null);
        if (_turns) wlLabel += ` | Turns: <strong>${_turns}</strong>`;
        if (_rc.first_prompt_tokens) wlLabel += ` | 1st Prompt: <strong>${_rc.first_prompt_tokens.toLocaleString()}</strong>` + (_rc.first_prompt_tokens_stdev ? ` σ=${_rc.first_prompt_tokens_stdev.toLocaleString()}` : '');
        if (_rc.prefix_tokens) wlLabel += ` | Prefix: <strong>${_rc.prefix_tokens.toLocaleString()}</strong>×${_rc.prefix_count || 1}`;
        if (_turns && _rc.turn_delay) wlLabel += ` | Delay: <strong>${_rc.turn_delay}s</strong>` + (_rc.turn_delay_stdev ? ` σ=${_rc.turn_delay_stdev}s` : '');
        html += `<span>${wlLabel}</span>`;
        html += `<span>Users: <strong>${rec.workload.users}</strong></span>`;
        html += `<span>Tests: <strong>${rec.total_tests}</strong></span>`;
        if (rec.total_duration) html += `<span>Duration: <strong>${rec.total_duration}</strong></span>`;
        html += '</div>';
        html += '<div class="chart-card-body" style="padding: 20px;">';
        html += `<p style="color:#334155; margin:0; font-size:0.95em; line-height:1.6;">${rec.goal_info.description}</p>`;
        html += '</div></div>';
    }

    // ============================================================
    // Store recommendation configs for single test re-run
    window._recConfigs = {};


    // --- Charts with descriptions ---
    const chartDesc = {
        pareto: 'Steps 2 &amp; 3 tested each TP value with a single <strong style="color:#3b82f6">Decode</strong> pod and a single <strong style="color:#f59e0b">Prefill</strong> pod to calibrate tensor parallelism. Points lower and to the left are better (faster response with fewer GPUs). The optimal TP for each role was selected from these results.',
        scatter: 'Each bubble is a tested configuration. The <strong>bubble size</strong> represents GPU count. The ideal configuration is in the <strong>top-left corner</strong> (low latency + high throughput). Hover over any bubble to see the exact configuration details.',
        efficiency: 'Shows how many requests each configuration can serve per GPU. <strong>Higher bars = better value for money.</strong> A configuration with high efficiency means you get more throughput from each GPU you pay for.',
        arch: 'Side-by-side comparison of <strong>Aggregated</strong> (single pool of GPUs) vs <strong>PD</strong> (dedicated prefill and decode GPUs) architectures. Lower TTFT is better for responsiveness. Higher throughput means more users served.'
    };

    // TP Pareto chart → TP Calibration subtab (only if TP data exists)

    if (!coreResults.length) secCfg = '';


    // ============================================================
    // STEP 10: Calibrated Load Validation (separate card)
    // Handles PD, EP, or both depending on goal
    // ============================================================



    // ============================================================



    // === Build Estimator section ===
    const wl = data.recommendation ? data.recommendation.workload : {};
    const estSuffix = _chartSuffix;
    var wlDesc = 'Tested with <strong>' + (wl.users || '?') + ' users</strong>, ISL <strong>' + (wl.isl || '?') + '</strong>';
    if (wl.isl_stdev) wlDesc += '&plusmn;' + wl.isl_stdev;
    wlDesc += ', OSL <strong>' + (wl.osl || '?') + '</strong>';
    if (wl.osl_stdev) wlDesc += '&plusmn;' + wl.osl_stdev;
    if (wl.turns > 1) wlDesc += ', <strong>' + wl.turns + ' turns</strong>';
    wlDesc += '.';
    // === Build subtab structure ===
    const subtabDefs = [];


    const sectionMap = {};

    // ── Modular tabs (web/static/report/tabs/) ── injected by position ──
    if (typeof ReportTabs !== 'undefined') {
        const _liveCtx = (typeof ReportCtx !== 'undefined') ? ReportCtx.live(runId) : null;
        if (_liveCtx) {
            ReportTabs.all().forEach(function(tab) {
                const tabHtml = ReportTabs.buildTab(tab, data, _liveCtx, chartQueue);
                if (!tabHtml) return;
                const def = { id: tab.id, label: tab.label, icon: tab.icon };
                const pos = tab.position || 'end';
                let at = -1;
                if (pos.indexOf('before:') === 0) at = subtabDefs.findIndex(d => d.id === pos.slice(7));
                else if (pos.indexOf('after:') === 0) { at = subtabDefs.findIndex(d => d.id === pos.slice(6)); if (at >= 0) at++; }
                if (at >= 0) subtabDefs.splice(at, 0, def); else subtabDefs.push(def);
                sectionMap[tab.id] = tabHtml;
            });
        }
    }

    if (subtabDefs.length > 1) {
        html += '<div class="report-subtabs-container">';
        html += '<div class="report-subtab-bar">';
        subtabDefs.forEach((st, i) => {
            html += `<div class="report-subtab${i === 0 ? ' active' : ''}" data-subtab="${st.id}${_chartSuffix}">${st.icon} ${st.label}</div>`;
        });
        html += '</div>';
        subtabDefs.forEach((st) => {
            html += `<div class="report-subtab-pane" data-subtab-pane="${st.id}${_chartSuffix}">${sectionMap[st.id]}</div>`;
        });
        html += '</div>';
    } else {
        for (const sec of Object.values(sectionMap)) html += sec;
    }

    // Global report-table stylesheet — normalizes every table in the report
    // (width, padding, alignment, borders, zebra striping) regardless of the
    // inline styles they were built with
    html = '<style>' +
        '#charts-content table { width: 100% !important; border-collapse: collapse !important; margin: 10px 0 0 0 !important; font-size: 0.9em !important; background: #fff !important; }' +
        '#charts-content th { background: #eef2ff !important; color: #334155 !important; padding: 7px 10px !important; text-align: left !important; border-top: 1px solid #c7d2fe !important; border-bottom: 2px solid #c7d2fe !important; white-space: nowrap !important; font-weight: 600 !important; }' +
        '#charts-content td { padding: 6px 10px !important; text-align: left !important; border-bottom: 1px solid #e2e8f0 !important; vertical-align: top !important; }' +
        '#charts-content tr:nth-child(even) td { background: #f8fafc !important; }' +
        '#charts-content tr:hover td { background: #eef2ff !important; }' +
        '#charts-content td[style*="monospace"] { white-space: nowrap; }' +
        '#charts-content .pod-bd-table { table-layout: fixed !important; }' +
        '#charts-content .pod-bd-table th { white-space: normal !important; }' +
        '#charts-content .pod-bd-table th:nth-child(1), #charts-content .pod-bd-table td:nth-child(1) { width: 13%; }' +
        '#charts-content .pod-bd-table th:nth-child(2), #charts-content .pod-bd-table td:nth-child(2) { width: 7%; }' +
        '#charts-content .pod-bd-table th:nth-child(3), #charts-content .pod-bd-table td:nth-child(3) { width: 9%; }' +
        '#charts-content .pod-bd-table th:nth-child(4), #charts-content .pod-bd-table td:nth-child(4) { width: 9%; }' +
        '#charts-content .pod-bd-table th:nth-child(5), #charts-content .pod-bd-table td:nth-child(5) { width: 6%; }' +
        '#charts-content .pod-bd-table th:nth-child(6), #charts-content .pod-bd-table td:nth-child(6) { width: 6%; }' +
        '#charts-content .pod-bd-table th:nth-child(7), #charts-content .pod-bd-table td:nth-child(7) { width: 41%; }' +
        '#charts-content .pod-bd-table th:nth-child(8), #charts-content .pod-bd-table td:nth-child(8) { width: 9%; }' +
        '</style>' + html;

    content.innerHTML = html;

    // --- Plotly chart config (must be before EPP chart rendering) ---
    const plotlyLayout = { margin: { t: 10, b: 40, l: 50, r: 20 }, height: 850, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent', font: { family: 'Inter, sans-serif' } };
    // Safe Plotly wrapper — skips if the DOM element doesn't exist (hidden tab)
    function safePlot(id, data, layout, config) { var el = document.getElementById(id); if (el) Plotly.newPlot(id, data, layout, config); }
    const plotlyConfig = { responsive: true, displayModeBar: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'], toImageButtonOptions: { format: 'png', height: 780, width: 1200, scale: 2 } };

    // Render EPP tuning charts now (before subtabs-ready hides non-active panes)
    if (window._eppChartRenders && window._eppChartRenders.length) {
        window._eppChartRenders.forEach(fn => fn());
        window._eppChartRenders = [];
    }

    // Render parallel-workload charts
    if (window._wlChartRenders && window._wlChartRenders.length) {
        window._wlChartRenders.forEach(fn => { try { fn(); } catch (e) {} });
        window._wlChartRenders = [];
    }

    // Render estimator methodology explanation
    updateEstimatorScaling(_chartSuffix);

    // Pareto frontier — TPSG bars (left) + TTFT line (right), no ITL
    if (charts.pareto.traces.length) {
        var tpsgLookup = {};
        if (rec) {
            (rec.decode_tp_all || []).forEach(function(d) { tpsgLookup['decode-' + d.tp] = d.tpsg; });
            (rec.prefill_tp_all || []).forEach(function(d) { tpsgLookup['prefill-' + d.tp] = d.tpsg; });
        }
        var traces = [];
        var roleColors = { decode: '#3b82f6', prefill: '#f59e0b' };
        charts.pareto.traces.forEach(function(t) {
            if (t.yaxis === 'y2') return;
            var role = t.name.toLowerCase().indexOf('decode') >= 0 ? 'decode' : 'prefill';
            var color = roleColors[role];
            var tpsgVals = t.x.map(function(tp) { return tpsgLookup[role + '-' + tp] || 0; });
            traces.push({
                x: t.x, y: tpsgVals, name: role.charAt(0).toUpperCase() + role.slice(1) + ' TPSG',
                type: 'bar',
                marker: { color: color },
                text: tpsgVals.map(function(v) { return v > 0 ? Math.round(v).toLocaleString() : ''; }),
                textposition: 'outside', textfont: { size: 13, color: color },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>TP=%{x}</b><br>TPSG: %{y:,.0f} tok/s/GPU<extra></extra>'
            });
            traces.push({
                x: t.x, y: t.y, name: role.charAt(0).toUpperCase() + role.slice(1) + ' TTFT', yaxis: 'y2',
                mode: 'markers+lines+text', text: t.text,
                customdata: t.y.map(function(v) { return v >= 1000 ? (v/1000).toFixed(1) + 's' : Math.round(v) + 'ms'; }),
                texttemplate: '%{customdata}',
                textposition: 'top center',
                textfont: { size: 13, color: '#1e293b' },
                marker: { size: 10, color: color, symbol: 'circle', line: { width: 2, color: 'white' } },
                line: { width: 2, dash: 'dash', color: color },
                hovertemplate: '<b>%{text}</b><extra></extra>'
            });
        });
        const paretoXvals = [...new Set(charts.pareto.traces.filter(t => t.yaxis !== 'y2').flatMap(t => t.x))].sort((a, b) => a - b);
        safePlot(cid('chart-pareto'), traces, {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60, t: 30 },
            barmode: 'group',
            xaxis: { title: 'Total GPUs (= TP)', tickvals: paretoXvals },
            yaxis: { title: 'TPSG (tok/s/GPU) — higher is better', side: 'left', tickformat: ',.0f' },
            yaxis2: { title: 'TTFT P90 (ms) — lower is better', side: 'right', overlaying: 'y', type: 'log' },
            showlegend: true
        }, plotlyConfig);
    }

    // Scatter
    if (charts.scatter.traces.length) {
        var filteredScatter = charts.scatter.traces.map(function(t) {
            var keep = [];
            var tids = t.test_ids || [];
            tids.forEach(function(tid, i) {
                if (tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0) keep.push(i);
            });
            return { x: keep.map(i => t.x[i]), y: keep.map(i => t.y[i]), text: keep.map(i => t.text[i]), name: t.name, sizes: keep.map(i => (t.sizes || [])[i]), color: t.color };
        }).filter(t => t.x.length);
        var allSizes = filteredScatter.flatMap(t => t.sizes || []);
        var maxSize = Math.max.apply(null, allSizes) || 1;
        const traces = filteredScatter.map(t => ({
            x: t.x, y: t.y, text: t.text, name: t.name,
            mode: 'markers',
            marker: { size: (t.sizes || []).map(s => 8 + (s / maxSize) * 22), color: t.color, opacity: 0.7, line: { width: 1, color: 'white' } },
            hovertemplate: '<b>%{text}</b><extra></extra>'
        }));
        safePlot(cid('chart-scatter'), traces, { ...plotlyLayout, xaxis: { title: 'TTFT P90 (ms) - lower is better' }, yaxis: { title: 'Throughput Mean (req/s) - higher is better' }, showlegend: true }, plotlyConfig);
    }

    // Efficiency bar
    if (charts.efficiency.configs.length) {
        var effConfigs = charts.efficiency.configs;
        var effValues = charts.efficiency.values;
        var effColors = charts.efficiency.colors;
        if (effConfigs.length) {
            safePlot(cid('chart-efficiency'), [{
                x: effConfigs, y: effValues,
                type: 'bar', marker: { color: effColors },
                text: effValues.map(v => v != null ? v.toFixed(3) : ''),
                textposition: 'outside', textfont: { size: 14, color: '#333' },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>%{x}</b><br>%{y:.3f} req/s/GPU<extra></extra>'
            }], { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 120 }, xaxis: { tickangle: -45 }, yaxis: { title: 'Mean req/s per GPU - higher is better' } }, plotlyConfig);
        }
    }

    // Per-user efficiency bar
    if (charts.per_user_efficiency && charts.per_user_efficiency.configs.length) {
        var puConfigs = charts.per_user_efficiency.configs;
        var puValues = charts.per_user_efficiency.values;
        var puColors = charts.per_user_efficiency.colors;
        safePlot(cid('chart-per-user-efficiency'), [{
            x: puConfigs, y: puValues,
            type: 'bar', marker: { color: puColors },
            text: puValues.map(v => v != null ? v.toFixed(1) : ''),
            textposition: 'outside', textfont: { size: 14, color: '#333' },
            cliponaxis: false, constraintext: 'none',
            hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/user<extra></extra>'
        }], { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 120 }, xaxis: { tickangle: -45 }, yaxis: { title: 'Output tokens/s per concurrent user - higher is better' } }, plotlyConfig);
    }

    // Token Throughput charts (Configurations tab)
    if (document.getElementById(cid('chart-token-tput'))) {
        var tkRes = (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || '';
            if (tid.indexOf('step2-') === 0 || tid.indexOf('step3-') === 0 ||
                tid.indexOf('step11-') === 0 || tid.indexOf('step12-') === 0 || tid.indexOf('step13-') === 0) return false;
            var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
            var pm = m.prometheus_metrics || {};
            return (pm.vllm_generation_tokens_rate || {}).avg || r.output_tps_mean;
        });
        if (tkRes.length) {
            var tkLabels = tkRes.map(function(r) { return _tagLabel(r.config_name, r.architecture); });
            var tkComputed = [], tkServer = [], tkGuidellmTotal = [];
            tkRes.forEach(function(r) {
                var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                var pm = m.prometheus_metrics || {};
                var pAvg = (pm.vllm_prompt_tokens_rate || {}).avg || 0;
                var gAvg = (pm.vllm_generation_tokens_rate || {}).avg || 0;
                var serverTotal = Math.round(pAvg + gAvg);
                tkServer.push(serverTotal);
                // Compute truly computed tok/s: subtract cached prompt tokens
                var cacheHits = (pm.vllm_prefix_cache_hits_rate || {}).avg || 0;
                var cacheQueries = (pm.vllm_prefix_cache_queries_rate || {}).avg || 0;
                var cacheHitRate = cacheQueries > 0 ? cacheHits / cacheQueries : 0;
                var computedPrompt = Math.round(pAvg * (1 - cacheHitRate));
                tkComputed.push(computedPrompt + Math.round(gAvg));
                // guidellm total = prompt_tokens_mean * throughput + output_tokens_mean * throughput
                var tput = m.throughput_mean || r.throughput_mean || 0;
                var pTps = (m.prompt_tokens_mean || 0) * tput;
                var gTps = (m.output_tokens_mean || 0) * tput;
                tkGuidellmTotal.push(Math.round(pTps + gTps));
            });
            safePlot(cid('chart-token-tput'), [
                { x: tkLabels, y: tkComputed, name: 'GPU computed', type: 'bar', marker: { color: '#3b82f6' }, text: tkComputed.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } },
                { x: tkLabels, y: tkServer, name: 'Server total (incl. cached)', type: 'bar', marker: { color: '#93c5fd' }, text: tkServer.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } },
                { x: tkLabels, y: tkGuidellmTotal, name: 'Client (guidellm)', type: 'bar', marker: { color: '#f59e0b' }, text: tkGuidellmTotal.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } }
            ], { ...plotlyLayout, height: 850, margin: { t: 60, b: 140, l: 60, r: 20 }, barmode: 'group', xaxis: { tickangle: -35 }, yaxis: { title: 'Total Tokens/s (Prompt + Generation)' }, showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' } }, plotlyConfig);
        }
    }

    // --- Concurrency Sweep Pareto charts (rendered into Concurrency Sweep tab) ---
    if (data.concurrency_sweep && document.getElementById(cid('chart-pareto-sweep-interactivity'))) {
        // Build set of discarded test_ids to filter from sweep charts
        var discardedIds = {};
        (data.all_results || []).forEach(function(r) {
            if (r.quality === 'discard') discardedIds[r.test_id || r.config_name] = true;
        });
        // Filter sweep data — remove points whose test_id matches a discarded test
        var csw = {};
        Object.keys(data.concurrency_sweep).forEach(function(arch) {
            var pts = data.concurrency_sweep[arch];
            if (!pts) return;
            csw[arch] = pts.filter(function(p) {
                var tid = p.test_id || '';
                return !discardedIds[tid];
            });
        });
        function sweepArchLabel(key) {
            if (key.startsWith('pd')) return 'PD';
            if (key.startsWith('agg')) return 'AGGREGATED';
            if (key.startsWith('ep')) return 'EP';
            return key.toUpperCase();
        }

        function swFrontierLine(points) {
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

        var swAllPts = [];
        Object.keys(csw).forEach(function(arch) {
            var pts = csw[arch];
            if (!pts || !pts.length) return;
            var archLabel = sweepArchLabel(arch);
            var archTag = arch.startsWith('pd') ? ' (PD)' : arch.startsWith('ep') ? ' (EP)' : ' (AG)';
            pts.forEach(function(p) {
                var interPerUser = (p.vllm_tps || p.output_tps_mean || 0) / (p.concurrency || 1);
                var tputGpu = p.throughput_per_gpu || 0;
                var cfgLabel = p.config_label || archLabel;
                if (cfgLabel.indexOf('(') < 0) cfgLabel += archTag;
                if (interPerUser > 0 && tputGpu > 0) {
                    swAllPts.push({
                        x: interPerUser, y: tputGpu, nx: interPerUser, ny: tputGpu,
                        label: cfgLabel + ' c=' + p.concurrency,
                        arch: archLabel, conc: p.concurrency
                    });
                }
            });
        });

        var swParetoConfig = {
            responsive: true, displayModeBar: true,
            modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
            toImageButtonOptions: { format: 'png', height: 1200, width: 1600, scale: 2 }
        };

        if (swAllPts.length > 1) {
            var swAgg = swAllPts.filter(function(p) { return p.arch === 'AGGREGATED'; });
            var swPd = swAllPts.filter(function(p) { return p.arch === 'PD' || p.arch === 'EP'; });
            var swAggPareto = swFrontierLine(swAgg);
            var swPdPareto = swFrontierLine(swPd);

            // Group points by config for individual legend entries
            var swCfgGroups = {};
            swAllPts.forEach(function(p) {
                var key = p.label.replace(/ c=\d+$/, '');
                if (!swCfgGroups[key]) swCfgGroups[key] = { pts: [], arch: p.arch };
                swCfgGroups[key].pts.push(p);
            });
            var swCfgColors = [
                '#dc2626', '#f59e0b', '#ea580c', '#db2777',
                '#2563eb', '#059669', '#7c3aed', '#0891b2', '#65a30d', '#6d28d9'
            ];
            var swTraces = [];
            var swColorIdx = 0;
            Object.keys(swCfgGroups).sort().forEach(function(key) {
                var g = swCfgGroups[key];
                var color = swCfgColors[swColorIdx % swCfgColors.length];
                swColorIdx++;
                swTraces.push({
                    x: g.pts.map(function(p) { return p.nx; }), y: g.pts.map(function(p) { return p.ny; }),
                    customdata: g.pts.map(function(p) { return p.label + '<br>' + p.x.toFixed(1) + ' tok/s/user<br>' + p.y.toFixed(0) + ' tok/s/GPU'; }),
                    name: key, mode: 'markers',
                    marker: { color: color, size: 12 },
                    hovertemplate: '<b>%{customdata}</b><extra></extra>'
                });
            });
            // Pareto frontier lines (aggregated vs PD)
            if (swAggPareto.length > 1) {
                swTraces.push({
                    x: swAggPareto.map(function(p) { return p.nx; }), y: swAggPareto.map(function(p) { return p.ny; }),
                    name: 'Aggregated — Frontier', mode: 'lines',
                    line: { color: '#dc2626', width: 3, dash: 'dot' }, showlegend: true
                });
            }
            if (swPdPareto.length > 1) {
                swTraces.push({
                    x: swPdPareto.map(function(p) { return p.nx; }), y: swPdPareto.map(function(p) { return p.ny; }),
                    name: 'Disaggregation — Frontier', mode: 'lines',
                    line: { color: '#2563eb', width: 3, dash: 'dot' }, showlegend: true
                });
            }

            var swAnnotations = [];
            var swIdx = 0;
            var swPositions = [
                [80, -15], [-80, -15], [70, -45], [-70, -45],
                [90, -30], [-90, -30], [60, -60], [-60, -60],
                [100, -10], [-100, -10], [50, -50], [-50, -50]
            ];
            function addSwLabels(points, color) {
                points.forEach(function(p) {
                    var cfgName = p.label.replace(/ c=\d+$/, '');
                    var pos = swPositions[swIdx % swPositions.length];
                    swIdx++;
                    swAnnotations.push({
                        x: p.nx, y: p.ny,
                        text: cfgName + '<br>' + p.x.toFixed(1) + ' tok/s/user<br>' + p.y.toFixed(0) + ' tok/s/GPU',
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: color,
                        ax: pos[0], ay: pos[1],
                        font: { size: 13, color: color },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2,
                    });
                });
            }
            addSwLabels(swAggPareto, '#dc2626');
            addSwLabels(swPdPareto, '#2563eb');

            safePlot(cid('chart-pareto-sweep-interactivity'), swTraces, {
                ...plotlyLayout, height: 850,
                xaxis: { title: 'Interactivity (tok/s/user)', gridcolor: '#d1d5db' },
                yaxis: { title: 'Token Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db' },
                showlegend: true,
                legend: { x: 1.02, y: 1, xanchor: 'left', bgcolor: 'rgba(255,255,255,0.95)', bordercolor: '#e2e8f0', borderwidth: 1 },
                margin: { t: 40, b: 70, l: 70, r: 160 },
                plot_bgcolor: 'white', paper_bgcolor: 'white',
                annotations: swAnnotations,
            }, swParetoConfig);
        }

        // Shared color function for envelope charts
        var _envColorList = ['#dc2626', '#2563eb', '#059669', '#f59e0b', '#7c3aed', '#db2777', '#0891b2', '#65a30d', '#ea580c', '#6d28d9'];
        var _envColorMap = {};
        var _envCIdx = 0;
        function getEnvColor(label) {
            if (!_envColorMap[label]) { _envColorMap[label] = _envColorList[_envCIdx % _envColorList.length]; _envCIdx++; }
            return _envColorMap[label];
        }

        // --- Best TTFT vs Concurrency (envelope style, dual axis with throughput) ---
        if (document.getElementById(cid('chart-pareto-sweep-ttft'))) {
            var bestTtftByConc = {};
            Object.keys(csw).forEach(function(arch) {
                var pts = csw[arch];
                if (!pts) return;
                pts.forEach(function(p) {
                    var c = p.concurrency;
                    var ttft = p.ttft_p90 || 0;
                    var tput = p.throughput_mean || 0;
                    var label = p.config_label || sweepArchLabel(arch);
                    var _at = arch.startsWith('pd') ? ' (PD)' : arch.startsWith('ep') ? ' (EP)' : ' (AG)';
                    if (label.indexOf('(') < 0) label += _at;
                    if (ttft > 0 && (!bestTtftByConc[c] || ttft < bestTtftByConc[c].ttft)) {
                        bestTtftByConc[c] = { ttft: ttft, tput: tput, label: label };
                    }
                });
            });
            var ttftConcs = Object.keys(bestTtftByConc).map(Number).sort(function(a,b){return a-b;});
            if (ttftConcs.length > 1) {
                var ttftXs = [], ttftYs = [], tputYs = [], ttftColors = [], ttftTexts = [];
                ttftConcs.forEach(function(c) {
                    var b = bestTtftByConc[c];
                    ttftXs.push(c); ttftYs.push(b.ttft); tputYs.push(b.tput);
                    ttftColors.push(getEnvColor(b.label));
                    ttftTexts.push(b.label + '<br>TTFT: ' + b.ttft.toFixed(0) + 'ms<br>Tput: ' + b.tput.toFixed(3) + ' req/s');
                });
                var ttftTraces = [
                    { x: ttftXs, y: ttftYs, text: ttftTexts, name: 'Best TTFT P90', mode: 'lines+markers',
                      line: { color: '#3b82f6', width: 3 }, marker: { color: ttftColors, size: 14, line: { color: '#fff', width: 2 } },
                      yaxis: 'y', hovertemplate: '<b>%{text}</b><extra></extra>' },
                    { x: ttftXs, y: tputYs, name: 'Throughput (req/s)', mode: 'lines+markers',
                      line: { color: '#f59e0b', width: 2, dash: 'dot' }, marker: { color: '#f59e0b', size: 6 },
                      yaxis: 'y2', hovertemplate: '<b>c=%{x}</b><br>%{y:.3f} req/s<extra></extra>' }
                ];
                var ttftAnnotations = [];
                ttftConcs.forEach(function(c, i) {
                    var b = bestTtftByConc[c];
                    var angles = [[-70,-20],[70,-20],[-50,-40],[50,-40],[-80,-10],[80,-10],[-60,-50],[60,-50]];
                    var a = angles[i % angles.length];
                    ttftAnnotations.push({
                        x: c, y: b.ttft, text: b.label,
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: getEnvColor(b.label),
                        ax: a[0], ay: a[1], font: { size: 13, color: getEnvColor(b.label) },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2,
                    });
                });
                safePlot(cid('chart-pareto-sweep-ttft'), ttftTraces, {
                    ...plotlyLayout, height: 850,
                    xaxis: { title: 'Concurrent Users', gridcolor: '#d1d5db', tickmode: 'array', tickvals: ttftConcs },
                    yaxis: { title: 'TTFT P90 (ms)', gridcolor: '#d1d5db', titlefont: { color: '#3b82f6' } },
                    yaxis2: { title: 'Throughput (req/s)', overlaying: 'y', side: 'right', gridcolor: 'transparent', titlefont: { color: '#f59e0b' } },
                    showlegend: true,
                    legend: { x: 0.02, y: 0.98, bgcolor: 'rgba(255,255,255,0.9)', bordercolor: '#e2e8f0', borderwidth: 1 },
                    plot_bgcolor: 'white', paper_bgcolor: 'white',
                    annotations: ttftAnnotations,
                }, swParetoConfig);
            }
        }

        // --- Architecture Comparison Charts (PD vs Aggregated at same concurrency) ---
        var archKeys = Object.keys(csw);
        var hasMultiArch = archKeys.length >= 2;
        if (hasMultiArch) {
            // Build best result per concurrency per arch
            var bestByConc = {};
            archKeys.forEach(function(arch) {
                var pts = csw[arch];
                if (!pts) return;
                var archLabel = sweepArchLabel(arch);
                var isDisagg = arch.startsWith('pd') || arch.startsWith('ep');
                pts.forEach(function(p) {
                    var c = p.concurrency;
                    if (!bestByConc[c]) bestByConc[c] = {};
                    var cat = isDisagg ? 'disagg' : 'agg';
                    if (!bestByConc[c][cat] || p.throughput_per_gpu > bestByConc[c][cat].throughput_per_gpu) {
                        var _lbl = p.config_label || archLabel;
                        var _at2 = arch.startsWith('pd') ? ' (PD)' : arch.startsWith('ep') ? ' (EP)' : ' (AG)';
                        if (_lbl.indexOf('(') < 0) _lbl += _at2;
                        bestByConc[c][cat] = { tputGpu: p.throughput_per_gpu, ttft: p.ttft_p90, label: _lbl, arch: archLabel, conc: c };
                    }
                });
            });

            // Build best-overall per concurrency (across ALL configs)
            var bestOverall = {};
            archKeys.forEach(function(arch) {
                var pts = csw[arch];
                if (!pts) return;
                pts.forEach(function(p) {
                    var c = p.concurrency;
                    var tputGpu = p.throughput_per_gpu || 0;
                    var label = p.config_label || sweepArchLabel(arch);
                    if (!bestOverall[c] || tputGpu > bestOverall[c].tputGpu) {
                        bestOverall[c] = { tputGpu: tputGpu, label: label, arch: sweepArchLabel(arch), conc: c };
                    }
                });
            });

            // Chart 1: Best config per concurrency
            if (document.getElementById(cid('chart-sweep-envelope'))) {
                var concs = Object.keys(bestOverall).map(Number).sort(function(a,b){return a-b;});
                var xs = [], ys = [], colors = [], texts = [];
                concs.forEach(function(c) {
                    var b = bestOverall[c];
                    xs.push(c); ys.push(b.tputGpu);
                    colors.push(getEnvColor(b.label));
                    texts.push(b.label + '<br>c=' + c + '<br>' + b.tputGpu.toFixed(0) + ' tok/s/GPU');
                });
                var envTraces = [{
                    x: xs, y: ys, text: texts,
                    name: 'Best Config', mode: 'lines+markers',
                    line: { color: '#475569', width: 2 },
                    marker: { color: colors, size: 14, line: { color: '#fff', width: 2 } },
                    hovertemplate: '<b>%{text}</b><extra></extra>'
                }];
                var envAnnotations = [];
                concs.forEach(function(c, i) {
                    var b = bestOverall[c];
                    var angles = [[-70,-20],[70,-20],[-50,-40],[50,-40],[-80,-10],[80,-10],[-60,-50],[60,-50]];
                    var a = angles[i % angles.length];
                    envAnnotations.push({
                        x: c, y: b.tputGpu, text: b.label,
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: getEnvColor(b.label),
                        ax: a[0], ay: a[1],
                        font: { size: 13, color: getEnvColor(b.label) },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2,
                    });
                });
                var envXvals = concs.slice();
                safePlot(cid('chart-sweep-envelope'), envTraces, {
                    ...plotlyLayout, height: 850,
                    xaxis: { title: 'Concurrent Users', gridcolor: '#d1d5db', tickmode: 'array', tickvals: envXvals },
                    yaxis: { title: 'Best Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db' },
                    showlegend: false,
                    margin: { t: 40, b: 70, l: 70, r: 40 },
                    plot_bgcolor: 'white', paper_bgcolor: 'white',
                    annotations: envAnnotations,
                }, swParetoConfig);
            }

            // Chart 3: Overlaid — same best-config line + winner comparison
            if (document.getElementById(cid('chart-sweep-overlaid'))) {
                var olConcs = Object.keys(bestOverall).map(Number).sort(function(a,b){return a-b;});
                var olXs = [], olYs = [], olColors = [], olTexts = [];
                olConcs.forEach(function(c) {
                    var b = bestOverall[c];
                    olXs.push(c); olYs.push(b.tputGpu);
                    olColors.push(getEnvColor(b.label));
                    olTexts.push(b.label);
                });
                var olTraces = [{
                    x: olXs, y: olYs, text: olTexts,
                    name: 'Best Config', mode: 'lines+markers',
                    line: { color: '#475569', width: 2 },
                    marker: { color: olColors, size: 14, line: { color: '#fff', width: 2 } },
                    hovertemplate: '<b>%{text}</b><br>c=%{x}<br>%{y:.0f} tok/s/GPU<extra></extra>'
                }];
                // Envelope lines for PD vs Agg comparison
                ['agg', 'disagg'].forEach(function(cat) {
                    var eColor = cat === 'agg' ? '#dc2626' : '#2563eb';
                    var eName = cat === 'agg' ? 'Aggregated — Best' : 'PD/EP — Best';
                    var exs = [], eys = [];
                    Object.keys(bestByConc).sort(function(a,b){return a-b;}).forEach(function(c) {
                        if (bestByConc[c][cat]) { exs.push(parseInt(c)); eys.push(bestByConc[c][cat].tputGpu); }
                    });
                    if (exs.length > 1) {
                        olTraces.push({ x: exs, y: eys, name: eName, mode: 'lines',
                            line: { color: eColor, width: 2, dash: 'dot' }, showlegend: true });
                    }
                });
                var olAnnotations = [];
                olConcs.forEach(function(c, i) {
                    var b = bestOverall[c];
                    var side = i % 2 === 0 ? 1 : -1;
                    olAnnotations.push({
                        x: c, y: b.tputGpu, text: b.label,
                        showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: getEnvColor(b.label),
                        ax: 60 * side, ay: -25 - (i % 3) * 15,
                        font: { size: 13, color: getEnvColor(b.label) },
                        bgcolor: 'rgba(255,255,255,0)', borderpad: 2,
                    });
                });
                var olXvals = olConcs.slice();
                safePlot(cid('chart-sweep-overlaid'), olTraces, {
                    ...plotlyLayout, height: 850,
                    xaxis: { title: 'Concurrent Users', gridcolor: '#d1d5db', tickmode: 'array', tickvals: olXvals },
                    yaxis: { title: 'Throughput per GPU (tok/s/GPU)', gridcolor: '#d1d5db' },
                    showlegend: true,
                    legend: { x: 0.02, y: 0.98, bgcolor: 'rgba(255,255,255,0.9)', bordercolor: '#e2e8f0', borderwidth: 1 },
                    plot_bgcolor: 'white', paper_bgcolor: 'white',
                    annotations: olAnnotations,
                }, swParetoConfig);
            }
        }
    }

    // Architecture comparison — use subplots side by side instead of overlaying
    if (charts.architecture.architectures.length && document.getElementById(cid('chart-arch'))) {
        const arch = charts.architecture;
        const archLabels = arch.architectures;
        const ttftTrace = {
            x: archLabels, y: arch.avg_ttft, type: 'bar',
            marker: { color: '#3b82f6' },
            text: arch.avg_ttft.map(v => fmtSI(v) + ' ms'), textposition: 'auto',
            name: 'Avg TTFT P90', xaxis: 'x', yaxis: 'y'
        };
        const bestTtftTrace = {
            x: archLabels, y: arch.best_ttft, type: 'bar',
            marker: { color: '#93c5fd' },
            text: arch.best_ttft.map(v => fmtSI(v) + ' ms'), textposition: 'auto',
            name: 'Best TTFT P90', xaxis: 'x', yaxis: 'y'
        };
        const tputTrace = {
            x: archLabels, y: arch.avg_throughput, type: 'bar',
            marker: { color: '#f59e0b' },
            text: arch.avg_throughput.map(v => v.toFixed(2) + ' req/s'), textposition: 'auto',
            name: 'Avg Throughput P90', xaxis: 'x2', yaxis: 'y2'
        };
        safePlot(cid('chart-arch'), [ttftTrace, bestTtftTrace, tputTrace], {
            ...plotlyLayout,
            margin: { t: 30, b: 50, l: 60, r: 60 },
            barmode: 'group',
            showlegend: true, legend: { x: 0, y: 1.18, orientation: 'h' },
            xaxis: { domain: [0, 0.45], title: '' },
            yaxis: { title: 'TTFT (ms) - lower is better', titlefont: { color: '#3b82f6' } },
            xaxis2: { domain: [0.55, 1], title: '', anchor: 'y2' },
            yaxis2: { title: 'Throughput Mean (req/s) - higher is better', anchor: 'x2', titlefont: { color: '#f59e0b' } },
        }, plotlyConfig);
    }

    // Percentile comparison bar chart (Winner vs Aggregated)
    if (rec && rec.recommendations && document.getElementById(cid('chart-percentile-bars'))) {
        const primaryKey = rec.goal === 'ttft' ? 'response_time' : 'throughput';
        const primaryRec = rec.recommendations[primaryKey];
        const aggBase = rec.aggregated_baseline;
        if (primaryRec && primaryRec.config.percentiles && aggBase && aggBase.percentiles) {
            const pp = primaryRec.config.percentiles;
            const ap = aggBase.percentiles;
            const primaryArch = primaryRec.architecture || 'PD';
            const pctls = ['p50', 'p90', 'p95', 'p99'];
            const ttftTraces = [
                { x: pctls, y: pctls.map(p => pp.ttft[p]), name: primaryArch + ' TTFT', type: 'bar', marker: { color: '#3b82f6' } },
                { x: pctls, y: pctls.map(p => ap.ttft[p]), name: 'Aggregated TTFT', type: 'bar', marker: { color: '#94a3b8' } },
            ];
            const tputTraces = [
                { x: pctls, y: pctls.map(p => pp.throughput[p]), name: primaryArch + ' Throughput', type: 'bar', marker: { color: '#10b981' }, xaxis: 'x2', yaxis: 'y2' },
                { x: pctls, y: pctls.map(p => ap.throughput[p]), name: 'Aggregated Throughput', type: 'bar', marker: { color: '#d1d5db' }, xaxis: 'x2', yaxis: 'y2' },
            ];
            safePlot(cid('chart-percentile-bars'), [...ttftTraces, ...tputTraces], {
                ...plotlyLayout,
                margin: { t: 30, b: 50, l: 60, r: 60 },
                barmode: 'group',
                showlegend: true, legend: { x: 0, y: 1.18, orientation: 'h' },
                xaxis: { domain: [0, 0.45], title: 'Percentile' },
                yaxis: { title: 'TTFT (ms) — lower is better', titlefont: { color: '#3b82f6' } },
                xaxis2: { domain: [0.55, 1], title: 'Percentile', anchor: 'y2' },
                yaxis2: { title: 'Throughput (req/s) — higher is better', anchor: 'x2', titlefont: { color: '#10b981' } },
            }, plotlyConfig);
        }
    }

    // TP Calibration charts (Step 2 decode, Step 3 prefill)
    // Render empty chart when no data so the card isn't blank


    // PD configurations TTFT charts (one per percentile)
    [{key:'p90',field:'ttft_p90',itlField:'itl_p90',color:'#3b82f6',chartId:'chart-pd-ttft-p90'},
     {key:'p95',field:'ttft_p95',itlField:'itl_p95',color:'#dc2626',chartId:'chart-pd-ttft-p95'},
     {key:'p99',field:'ttft_p99',itlField:'itl_p99',color:'#7c3aed',chartId:'chart-pd-ttft-p99'}
    ].forEach(function(pctl) {
        var archResults = coreResults.filter(r => r.architecture === 'PD');
        if (!archResults.length) return;
        var sorted = archResults.slice().sort((a, b) => a.prefill_pods - b.prefill_pods);
        var labels = sorted.map(r => _tagLabel(r.config_name, r.architecture));
        var ttftVals = sorted.map(r => r[pctl.field]);
        var tputVals = sorted.map(r => r.throughput_mean || r.throughput_p90);
        var itlVals = sorted.map(r => r[pctl.itlField] != null ? r[pctl.itlField] : null);
        var hasItl = itlVals.some(v => v != null);
        var validTtft = ttftVals.filter(v => v != null);
        if (!validTtft.length) return;
        var bestTtftIdx = ttftVals.indexOf(Math.min.apply(null, validTtft));
        var bestTputIdx = tputVals.indexOf(Math.max.apply(null, tputVals));
        var pLabel = pctl.key.toUpperCase();

        var el = document.getElementById(cid(pctl.chartId));
        if (!el) return;

        var traces = [
            { x: labels, y: ttftVals, type: 'scatter', mode: 'lines+markers', name: 'TTFT ' + pLabel,
              line: { color: pctl.color, width: 3, shape: 'spline' },
              marker: { color: pctl.color, size: 12, symbol: 'circle', line: { width: 2, color: 'white' } },
              fill: 'tozeroy', fillcolor: pctl.color + '14' },
            { x: [labels[bestTtftIdx]], y: [validTtft[0]], type: 'scatter', mode: 'markers', name: 'Best TTFT',
              marker: { color: '#10b981', size: 22, symbol: 'circle', line: { width: 3, color: 'white' } }, showlegend: true },
            { x: labels, y: tputVals, type: 'scatter', mode: 'lines+markers', name: 'Throughput Mean', yaxis: 'y2',
              line: { color: '#f59e0b', width: 3, shape: 'spline' },
              marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } } },
            { x: [labels[bestTputIdx]], y: [tputVals[bestTputIdx]], type: 'scatter', mode: 'markers', name: 'Best Throughput', yaxis: 'y2',
              marker: { color: '#e11d48', size: 22, symbol: 'diamond', line: { width: 3, color: 'white' } }, showlegend: true },
        ];

        var layout = {
            height: 850,
            margin: { t: 30, b: 180, l: 60, r: 60 },
            xaxis: { title: { text: 'Configuration', standoff: 20 }, tickangle: -40, anchor: hasItl ? 'y3' : 'y' },
            yaxis: { title: 'TTFT ' + pLabel + ' (ms)', side: 'left', titlefont: { color: pctl.color }, tickfont: { color: pctl.color }, tickformat: '.2s', domain: hasItl ? [0.28, 1] : [0, 1] },
            yaxis2: { title: 'Throughput Mean (req/s)', side: 'right', overlaying: 'y', titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
            showlegend: true,
            legend: { x: 0, y: 1.12, orientation: 'h' },
        };

        if (hasItl) {
            var validItl = itlVals.filter(v => v != null);
            var bestItlIdx = itlVals.indexOf(Math.min.apply(null, validItl));
            traces.push(
                { x: labels, y: itlVals, type: 'scatter', mode: 'lines+markers', name: 'ITL ' + pLabel, yaxis: 'y3',
                  line: { color: '#ef4444', width: 2, shape: 'spline' },
                  marker: { color: '#ef4444', size: 8, symbol: 'square', line: { width: 1, color: 'white' } }, connectgaps: true },
                { x: [labels[bestItlIdx]], y: [itlVals[bestItlIdx]], type: 'scatter', mode: 'markers', name: 'Best ITL', yaxis: 'y3',
                  marker: { color: '#10b981', size: 16, symbol: 'square', line: { width: 2, color: 'white' } }, showlegend: true }
            );
            layout.yaxis3 = { title: 'ITL ' + pLabel + ' (ms)', side: 'left', titlefont: { color: '#ef4444', size: 11 }, tickfont: { color: '#ef4444', size: 10 }, domain: [0, 0.22] };
        }

        Plotly.newPlot(el, traces, layout, { ...plotlyConfig, toImageButtonOptions: { format: 'png', width: 1400, height: 850, scale: 2 } });
    });

    // ============================================================
    // Aggregated configurations chart (all percentiles in one grouped bar chart)
    // ============================================================
    const aggResults = coreResults.filter(r => r.architecture === 'AGGREGATED' && r.ttft_p90);
    if (aggResults.length > 1 && document.getElementById(cid('chart-agg-ttft-all'))) {
        const aggSorted = [...aggResults].sort((a, b) => (a.tp || 1) - (b.tp || 1));
        const aggLabels = aggSorted.map(r => `${r.replicas || Math.floor(r.gpus / (r.tp || 1))}×TP${r.tp || '?'}${(r.pp && r.pp > 1) ? ' PPx' + r.pp : ''}`);
        const aggColors = { p90: '#3b82f6', p95: '#dc2626', p99: '#7c3aed' };
        const traces = [];
        ['p90', 'p95', 'p99'].forEach(p => {
            traces.push({
                x: aggLabels,
                y: aggSorted.map(r => r['ttft_' + p]),
                name: `TTFT ${p.toUpperCase()}`,
                type: 'bar',
                marker: { color: aggColors[p], opacity: 0.85 },
                hovertemplate: `%{x}<br>TTFT ${p.toUpperCase()}: %{y:.1f} ms<extra></extra>`,
            });
        });
        const aggTputVals = aggSorted.map(r => r.throughput_mean || r.throughput_p90);
        traces.push({
            x: aggLabels, y: aggTputVals,
            name: 'Throughput Mean',
            type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
            line: { color: '#f59e0b', width: 3, shape: 'spline' },
            marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } },
            hovertemplate: 'Throughput Mean: %{y:.2f} req/s<extra></extra>',
        });
        const itlDashes = { p90: 'solid', p95: 'dash', p99: 'dot' };
        const itlSymbols = { p90: 'square', p95: 'square-open', p99: 'square-open-dot' };
        const hasAggItl = aggSorted.some(r => r.itl_p90 != null);
        if (hasAggItl) {
            ['p90', 'p95', 'p99'].forEach(p => {
                traces.push({
                    x: aggLabels,
                    y: aggSorted.map(r => r['itl_' + p]),
                    name: `ITL ${p.toUpperCase()}`,
                    type: 'scatter', mode: 'lines+markers', yaxis: 'y3',
                    line: { color: '#ef4444', width: 2, dash: itlDashes[p] },
                    marker: { color: '#ef4444', size: 7, symbol: itlSymbols[p] },
                    hovertemplate: `ITL ${p.toUpperCase()}: %{y:.2f} ms<extra></extra>`,
                    connectgaps: true,
                });
            });
        }
        safePlot(cid('chart-agg-ttft-all'), traces, {
            ...plotlyLayout,
            height: 850,
            barmode: 'group',
            margin: { t: 30, b: 80, l: 60, r: hasAggItl ? 110 : 60 },
            xaxis: { title: 'Aggregated Configuration', domain: hasAggItl ? [0, 0.96] : [0, 1] },
            yaxis: { title: 'TTFT (ms) — lower is better', side: 'left', tickformat: '.2s' },
            yaxis2: { title: 'Throughput Mean (req/s)', side: 'right', overlaying: 'y', anchor: hasAggItl ? 'free' : 'x', position: hasAggItl ? 0.97 : 1, titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
            ...(hasAggItl ? { yaxis3: { title: 'ITL (ms)', side: 'right', overlaying: 'y', anchor: 'free', position: 1, titlefont: { color: '#ef4444', size: 11 }, tickfont: { color: '#ef4444', size: 10 } } } : {}),
            showlegend: true,
            legend: { x: 0, y: 1.18, orientation: 'h' },
        }, plotlyConfig);
    }

    // ============================================================
    // STEP 9: Latency Search — Plotly rendering
    // ============================================================
    if (data.latency_search && data.latency_search.by_architecture) {
        const byArch = data.latency_search.by_architecture;
        const archKeys = Object.keys(byArch);
        const s9ArchConfigs = data.latency_search.arch_configs || {};
        const firstTrial = data.latency_search.trials[0];
        const targetMs = firstTrial.target_ms;
        const targetPct = firstTrial.target_percentile || 'p90';
        const metricKey = 'ttft_' + targetPct;

        archKeys.forEach((arch, ai) => {
            const trials = byArch[arch];
            const cfgLabel = s9ArchConfigs[arch] || arch.toUpperCase();

            // Skip architectures with no valid data
            if (!trials.some(t => t.ttft_p90 != null)) return;

            // Sort by concurrency for clean x-axis
            const sorted = [...trials].filter(t => t.ttft_p90 != null).sort((a, b) => a.concurrency - b.concurrency);
            const xLabels = sorted.map(t => `c=${t.concurrency}`);

            const pctlCharts = [
                { key: 'p90', ttftField: 'ttft_p90', tputField: 'throughput_mean', color: '#3b82f6', divId: `step9-chart-p90-${ai}` },
                { key: 'p95', ttftField: 'ttft_p95', tputField: 'throughput_mean', color: '#dc2626', divId: `step9-chart-p95-${ai}` },
                { key: 'p99', ttftField: 'ttft_p99', tputField: 'throughput_mean', color: '#7c3aed', divId: `step9-chart-p99-${ai}` },
            ];

            pctlCharts.forEach(pctl => {
                const el = document.getElementById(cid(pctl.divId));
                if (!el) return;
                const pLabel = pctl.key.toUpperCase();
                const latencies = sorted.map(t => t[pctl.ttftField]);
                const throughputs = sorted.map(t => t[pctl.tputField] || t.throughput_mean || t.throughput_p90);

                const hoverTexts = sorted.map((t, i) =>
                    `<b>${cfgLabel} c=${t.concurrency}</b><br>` +
                    `TTFT ${pLabel}: <b>${latencies[i] != null ? latencies[i].toFixed(1) : '-'} ms</b><br>` +
                    `Throughput Mean: ${throughputs[i] != null ? throughputs[i].toFixed(2) : '-'} req/s<br>` +
                    `SLA (${targetPct.toUpperCase()}): ${t.meets_sla ? '<span style="color:#10b981">PASS</span>' : '<span style="color:#ef4444">FAIL</span>'}`
                );

                // SLA markers based on target percentile (only color the target pctl chart)
                const isTargetPctl = pctl.key === targetPct;
                const markerColors = isTargetPctl
                    ? sorted.map(t => t.meets_sla ? '#10b981' : '#ef4444')
                    : sorted.map(() => pctl.color);

                // Find best passing (highest throughput meeting SLA) — only for target percentile
                let bestIdx = -1;
                if (isTargetPctl) {
                    let bestTput = -1;
                    sorted.forEach((t, i) => {
                        if (t.meets_sla && throughputs[i] != null && throughputs[i] > bestTput) {
                            bestTput = throughputs[i];
                            bestIdx = i;
                        }
                    });
                } else {
                    // For non-target percentiles, mark the best TTFT
                    let bestTtft = Infinity;
                    latencies.forEach((v, i) => { if (v != null && v < bestTtft) { bestTtft = v; bestIdx = i; } });
                }

                const traces = [
                    {
                        x: xLabels, y: latencies, name: `TTFT ${pLabel}`,
                        type: 'scatter', mode: 'lines+markers',
                        line: { color: pctl.color, width: 3, shape: 'spline' },
                        marker: { color: markerColors, size: 12, symbol: 'circle', line: { width: 2, color: 'white' } },
                        hovertext: hoverTexts, hoverinfo: 'text',
                        fill: 'tozeroy', fillcolor: pctl.color + '14',
                    },
                    {
                        x: xLabels, y: throughputs, name: `Throughput Mean`,
                        type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                        line: { color: '#f59e0b', width: 3, shape: 'spline' },
                        marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } },
                        hovertemplate: `c=%{x}: %{y:.2f} req/s<extra></extra>`,
                    },
                ];

                if (bestIdx >= 0) {
                    traces.push({
                        x: [xLabels[bestIdx]], y: [latencies[bestIdx]],
                        name: isTargetPctl ? 'Best (meets SLA)' : `Best TTFT ${pLabel}`,
                        type: 'scatter', mode: 'markers',
                        marker: { color: '#10b981', size: 22, symbol: 'circle', line: { width: 3, color: 'white' } },
                        hovertext: [hoverTexts[bestIdx]], hoverinfo: 'text', showlegend: true,
                    });
                    traces.push({
                        x: [xLabels[bestIdx]], y: [throughputs[bestIdx]],
                        name: isTargetPctl ? 'Best Throughput (meets SLA)' : `Best Throughput ${pLabel}`,
                        type: 'scatter', mode: 'markers', yaxis: 'y2',
                        marker: { color: '#e11d48', size: 22, symbol: 'diamond', line: { width: 3, color: 'white' } },
                        hovertext: [hoverTexts[bestIdx]], hoverinfo: 'text', showlegend: true,
                    });
                }

                // SLA line (shown on all percentile charts for reference)
                const shapes = [];
                const chartAnnotations = [];
                const slaLabel = isTargetPctl ? `SLA Target (${targetPct.toUpperCase()}): ${targetMs} ms` : `SLA Target (${targetPct.toUpperCase()}): ${targetMs} ms`;
                shapes.push({
                    type: 'line', x0: -0.5, x1: xLabels.length - 0.5,
                    y0: targetMs, y1: targetMs, yref: 'y',
                    line: { color: '#ef4444', width: isTargetPctl ? 2 : 1.5, dash: 'dash' },
                });
                chartAnnotations.push({
                    x: xLabels.length - 1, y: targetMs, yref: 'y',
                    text: slaLabel, showarrow: false,
                    font: { color: '#ef4444', size: 11, weight: 700 },
                    xanchor: 'right', yanchor: 'bottom', yshift: 5,
                    bgcolor: 'rgba(255,255,255,0.85)',
                });

                Plotly.newPlot(el, traces, {
                    ...plotlyLayout, height: 850,
                    margin: { t: 30, b: 80, l: 60, r: 60 },
                    xaxis: { title: 'Concurrent Users' },
                    yaxis: { title: `TTFT ${pLabel} (ms) — lower is better`, side: 'left', titlefont: { color: pctl.color }, tickfont: { color: pctl.color }, tickformat: '.2s' },
                    yaxis2: { title: `Throughput Mean (req/s) — higher is better`, side: 'right', overlaying: 'y', titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
                    showlegend: true, legend: { x: 0, y: 1.18, orientation: 'h' },
                    shapes, annotations: chartAnnotations,
                }, plotlyConfig);
            });
        });
    }

    // ============================================================
    // vLLM ENGINE METRICS — Plotly rendering
    // ============================================================
   if (charts.vllm && charts.vllm.configs.length) {
        const vllm = charts.vllm;
        const vllmLayout = { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 100 }, showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' } };
        const pColors = { p50: '#60a5fa', p90: '#3b82f6', p95: '#f59e0b', p99: '#ef4444' };
        const pctiles = ['P50', 'P90', 'P95', 'P99'];

        // Sort configs by architecture group then P90 value
        function sortedIndices(p90arr, cfgs) {
            var indices = cfgs.map(function(_, i) { return i; });
            var archOrder = { 'aggregated': 0, 'pd': 1, 'ep': 2 };
            function getArch(label) {
                if (label.indexOf('(AG)') >= 0 || label.indexOf('(aggregated)') >= 0) return 'aggregated';
                if (label.indexOf('(EP)') >= 0 || label.indexOf('(ep)') >= 0) return 'ep';
                return 'pd';
            }
            indices.sort(function(a, b) {
                var aa = archOrder[getArch(cfgs[a])] || 1, ba = archOrder[getArch(cfgs[b])] || 1;
                if (aa !== ba) return aa - ba;
                return (p90arr[a] || 0) - (p90arr[b] || 0);
            });
            return indices;
        }
        function reorder(arr, idx) { return idx.map(function(i) { return arr[i]; }); }
        // Group configs by architecture, sort by P90 within each
        var archColors = { aggregated: '#059669', pd: '#2563eb', ep: '#7c3aed' };
        var archLabels = { aggregated: 'Aggregated', pd: 'PD', ep: 'EP' };
        function getArch(label) {
            if (label.indexOf('(aggregated)') >= 0) return 'aggregated';
            if (label.indexOf('(ep)') >= 0) return 'ep';
            return 'pd';
        }
        function makeArchLines(data, unit) {
            var byArch = {};
            var bestByCfg = {};
            vllm.configs.forEach(function(cfg, i) {
                var existing = bestByCfg[cfg];
                if (!existing || (data.p90[i] || 1e9) < (existing.p90 || 1e9)) {
                    bestByCfg[cfg] = { cfg: cfg, p50: data.p50[i], p90: data.p90[i], p95: data.p95[i], p99: data.p99[i] };
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
                pts.sort(function(x, y) { return (x.p90 || 0) - (y.p90 || 0); });
                var color = archColors[a];
                var label = archLabels[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return p.p90; }),
                    name: label + ' P90', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>P90: %{y:,.0f} ' + unit + '<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return p.p99; }),
                    name: label + ' P99', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color, symbol: 'diamond' },
                    hovertemplate: '<b>%{x}</b><br>P99: %{y:,.0f} ' + unit + '<extra></extra>' });
            });
            return traces;
        }

        // Chart 1: TTFT — lines per architecture, sorted by P90
        safePlot(cid('chart-vllm-ttft'), makeArchLines(vllm.ttft, 'ms'),
            { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'TTFT (ms) — lower is better' } }, plotlyConfig);

        // Chart 2: ITL — lines per architecture, sorted by P90
        safePlot(cid('chart-vllm-itl'), makeArchLines(vllm.itl, 'ms'),
            { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'ITL (ms) — lower is better' } }, plotlyConfig);

        // Chart 3: E2E Latency — lines per architecture, sorted by P90
        safePlot(cid('chart-vllm-e2e'), makeArchLines(vllm.e2e, 's'),
            { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'E2E Latency (s) — lower is better' } }, plotlyConfig);

        // Dedup helper for non-percentile metrics: best by lowest P90-equivalent (or first seen)
        function dedupByConfig(arrays) {
            var best = {};
            vllm.configs.forEach(function(cfg, i) {
                if (!best[cfg]) { best[cfg] = i; }
            });
            var cfgs = Object.keys(best);
            var byArch = {};
            cfgs.forEach(function(cfg) {
                var a = getArch(cfg);
                if (!byArch[a]) byArch[a] = [];
                byArch[a].push({ cfg: cfg, idx: best[cfg] });
            });
            return { cfgs: cfgs, byArch: byArch };
        }

        // Chart 4: Token Throughput — lines per architecture
        (function() {
            var dd = dedupByConfig();
            var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a];
                if (!pts || !pts.length) return;
                pts.sort(function(x, y) { return (vllm.token_rates.generation[x.idx] || 0) - (vllm.token_rates.generation[y.idx] || 0); });
                var color = archColors[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.token_rates.prompt[p.idx]; }),
                    name: archLabels[a] + ' Prompt', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                    hovertemplate: '<b>%{x}</b><br>Prompt: %{y:,.0f} tok/s<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.token_rates.generation[p.idx]; }),
                    name: archLabels[a] + ' Generation', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Generation: %{y:,.0f} tok/s<extra></extra>' });
            });
            safePlot(cid('chart-vllm-tokens'), traces, { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Tokens/second — higher is better' } }, plotlyConfig);
        })();

        // Chart 5: Request Queue & KV Cache — lines per architecture
        (function() {
            var dd = dedupByConfig();
            var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a];
                if (!pts || !pts.length) return;
                var color = archColors[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.request_state.running[p.idx]; }),
                    name: archLabels[a] + ' Running', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Running: %{y:.1f}<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.request_state.waiting[p.idx]; }),
                    name: archLabels[a] + ' Waiting', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color, symbol: 'diamond' },
                    hovertemplate: '<b>%{x}</b><br>Waiting: %{y:.1f}<extra></extra>' });
            });
            traces.push({ x: dd.cfgs, y: dd.cfgs.map(function(c) { return vllm.request_state.kv_cache[dedupByConfig().byArch[getArch(c)].find(function(p){return p.cfg===c}).idx]; }),
                name: 'KV Cache %', mode: 'lines+markers', yaxis: 'y2', line: { color: '#f59e0b', width: 2 }, marker: { size: 10, color: '#f59e0b', symbol: 'diamond' },
                hovertemplate: '<b>%{x}</b><br>KV Cache: %{y:.1f}%<extra></extra>' });
            safePlot(cid('chart-vllm-queue'), traces, {
                ...vllmLayout, margin: { ...vllmLayout.margin, r: 60 }, xaxis: { tickangle: -35 },
                yaxis: { title: 'Request Count (avg)', side: 'left' },
                yaxis2: { title: 'KV Cache Usage (%)', side: 'right', overlaying: 'y', range: [0, 105], titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
            }, plotlyConfig);
        })();

        // Chart 6: Processing Time — lines per architecture
        (function() {
            var dd = dedupByConfig();
            var traces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a];
                if (!pts || !pts.length) return;
                var color = archColors[a];
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.time_breakdown.prefill[p.idx]; }),
                    name: archLabels[a] + ' Prefill', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>Prefill: %{y:.2f}<extra></extra>' });
                traces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.time_breakdown.decode[p.idx]; }),
                    name: archLabels[a] + ' Decode', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                    hovertemplate: '<b>%{x}</b><br>Decode: %{y:.2f}<extra></extra>' });
            });
            var allPts = [];
            Object.keys(dd.byArch).forEach(function(a) { dd.byArch[a].forEach(function(p) { allPts.push(p); }); });
            traces.push({ x: allPts.map(function(p) { return p.cfg; }), y: allPts.map(function(p) { return vllm.time_breakdown.preemptions[p.idx]; }),
                name: 'Preemptions/s', mode: 'markers', yaxis: 'y2', marker: { size: 10, color: '#ef4444', symbol: 'triangle-up' },
                hovertemplate: '<b>%{x}</b><br>Preemptions: %{y:.1f}/s<extra></extra>' });
            safePlot(cid('chart-vllm-time'), traces, {
                ...vllmLayout, margin: { ...vllmLayout.margin, r: 60 }, xaxis: { tickangle: -35 },
                yaxis: { title: 'Time Rate (s/s)', side: 'left' },
                yaxis2: { title: 'Preemptions/s', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
            }, plotlyConfig);
        })();

        // Chart 7: Pod Network Throughput — lines per architecture
        if (vllm.network && vllm.network.pod_tx.some(v => v > 0)) {
            var dd = dedupByConfig();
            var netTraces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd.byArch[a];
                if (!pts || !pts.length) return;
                var color = archColors[a];
                netTraces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.network.pod_tx[p.idx]; }),
                    name: archLabels[a] + ' TX', mode: 'lines+markers', line: { color: color, width: 3 }, marker: { size: 10, color: color },
                    hovertemplate: '<b>%{x}</b><br>TX: %{y:.2f} MB/s<extra></extra>' });
                netTraces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return vllm.network.pod_rx[p.idx]; }),
                    name: archLabels[a] + ' RX', mode: 'lines+markers', line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                    hovertemplate: '<b>%{x}</b><br>RX: %{y:.2f} MB/s<extra></extra>' });
            });
            safePlot(cid('chart-net-pod'), netTraces, { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Throughput (MB/s)' } }, plotlyConfig);
        }

        // Chart 8: NIXL KV Transfer Throughput — lines per architecture
        var nixlData = vllm.network.nixl_tx || [];
        var ibData = vllm.network.ib_rx || [];
        if (nixlData.some(v => v > 0) || ibData.some(v => v > 0)) {
            var dd8 = dedupByConfig();
            var nixlTraces = [];
            ['aggregated', 'pd', 'ep'].forEach(function(a) {
                var pts = dd8.byArch[a];
                if (!pts || !pts.length) return;
                var color = archColors[a];
                if (nixlData.some(v => v > 0)) {
                    nixlTraces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return nixlData[p.idx]; }),
                        name: archLabels[a] + ' NIXL TX', mode: 'lines+markers+text',
                        text: pts.map(function(p) { var v = nixlData[p.idx]; return v > 0 ? v.toFixed(2) : ''; }),
                        textposition: 'top center', textfont: { size: 13, color: color },
                        line: { color: color, width: 3 }, marker: { size: 10, color: color },
                        hovertemplate: '<b>%{x}</b><br>NIXL TX: %{y:.2f} GB/s<extra></extra>' });
                }
                if (ibData.some(v => v > 0)) {
                    nixlTraces.push({ x: pts.map(function(p) { return p.cfg; }), y: pts.map(function(p) { return ibData[p.idx]; }),
                        name: archLabels[a] + ' IB RX', mode: 'lines+markers',
                        line: { color: color, width: 2, dash: 'dot' }, marker: { size: 8, color: color },
                        hovertemplate: '<b>%{x}</b><br>IB RX: %{y:.2f} GB/s<extra></extra>' });
                }
            });
            safePlot(cid('chart-net-ib'), nixlTraces,
                { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Throughput (GB/s)' } }, plotlyConfig);
        }
    }

    // Execute deferred chart renders (concurrency sweep, cache sweep)
    chartQueue.forEach(function(item) {
        if (typeof item === 'function') {
            try { item(); } catch(e) { console.warn('Deferred chart render failed:', e); }
        } else if (item && item.id && item.traces) {
            var el = document.getElementById(item.id);
            if (el) Plotly.newPlot(el, item.traces, item.layout || {}, { responsive: true });
        }
    });

    // Initialize subtab switching (activates first pane, hides others)
    initReportSubtabs(content);

    // Resize all charts in the active pane after tabs are set up
    setTimeout(function() {
        content.querySelectorAll('.js-plotly-plot').forEach(function(plot) {
            if (plot.offsetParent !== null) Plotly.Plots.resize(plot);
        });
    }, 100);
}

