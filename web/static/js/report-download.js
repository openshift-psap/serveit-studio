// report-download.js — Self-contained HTML report generator for Inftune Studio
// Extracted from app.js to keep the download report maintainable separately.

// Load html2pdf.js once on first use
var _html2pdfReady = null;
function _ensureHtml2Pdf() {
    if (_html2pdfReady) return _html2pdfReady;
    _html2pdfReady = new Promise((resolve, reject) => {
        if (window.html2pdf) { resolve(); return; }
        var s = document.createElement('script');
        s.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.2/html2pdf.bundle.min.js';
        s.onload = resolve;
        s.onerror = () => { _html2pdfReady = null; reject(new Error('Failed to load html2pdf')); };
        document.head.appendChild(s);
    });
    return _html2pdfReady;
}

function downloadPDFReport(runId, data) {
    const btn = document.getElementById('chart-download-pdf-link');
    const origText = btn.textContent;
    btn.textContent = 'Generating PDF...';
    btn.style.pointerEvents = 'none';

    function reset() { btn.textContent = origText; btn.style.pointerEvents = ''; }

    _ensureHtml2Pdf().then(() => {
        // Capture existing rendered charts as static images first
        var chartImages = {};
        var chartPromises = [];
        document.querySelectorAll('.report-tab-panel.active .js-plotly-plot').forEach(el => {
            if (el.id) {
                chartPromises.push(
                    Plotly.toImage(el, { format: 'png', width: 1000, height: 400 })
                        .then(img => { chartImages[el.id] = img; })
                        .catch(() => {})
                );
            }
        });

        Promise.all(chartPromises).then(() => {
            const charts = data.charts;
            const rec = data.recommendation || {};
            const summary = data.summary;
            const best = summary.best_configs || {};
            const allRes = data.all_results || [];
            const hasPD = allRes.some(r => r.architecture === 'PD');

            // Build sections (tables + text only — no chart placeholders)
            const secRec = buildRecSection(runId, data, rec, summary, best, allRes);
            const secCfg = buildCfgSection(runId, data, charts, allRes, hasPD);
            const secCmp = buildCmpSection(runId, rec, data);
            const secStep9 = buildStep9Section(data);
            const secCal = buildCalSection(data);
            const secEpp = buildEppTuningSection(runId, data);
            const secTestCfg = buildTestSettingsSection(data);

            var bodyHtml = '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:#1e293b;padding:20px;">';
            bodyHtml += `<h1 style="border-bottom:3px solid #10b981;padding-bottom:10px;">Inftune Studio Report — Run #${runId}</h1>`;
            bodyHtml += `<p style="color:#64748b;">Generated: ${new Date().toLocaleString()}</p>`;

            // Inject captured chart images
            if (Object.keys(chartImages).length) {
                bodyHtml += '<div style="margin:20px 0;">';
                for (var cid in chartImages) {
                    bodyHtml += `<div style="background:white;border-radius:8px;border:1px solid #e2e8f0;margin:12px 0;padding:12px;page-break-inside:avoid;"><img src="${chartImages[cid]}" style="width:100%;max-width:1000px;"></div>`;
                }
                bodyHtml += '</div>';
            }

            [secRec, secCfg, secCmp, secStep9, secCal, secEpp, secTestCfg].forEach(sec => {
                if (sec) bodyHtml += sec;
            });
            bodyHtml += '</div>';

            // Use html2pdf string mode
            html2pdf().set({
                margin: [8, 8, 8, 8],
                filename: `inftune-report-run-${runId}.pdf`,
                image: { type: 'jpeg', quality: 0.92 },
                html2canvas: { scale: 1.5, useCORS: true },
                jsPDF: { unit: 'mm', format: 'a4', orientation: 'landscape' },
                pagebreak: { mode: ['css', 'legacy'], avoid: ['tr', 'img'] }
            }).from(bodyHtml, 'string').save().then(() => {
                reset();
            }).catch(err => {
                console.error('PDF generation failed:', err);
                reset();
            });
        });
    }).catch(() => {
        reset();
        downloadHTMLReport(runId, data);
        alert('PDF library unavailable — downloaded HTML report instead. Open it and use Print > Save as PDF.');
    });
}

function downloadHTMLReport(runId, data) {
    const charts = data.charts;
    const rec = data.recommendation || {};
    const summary = data.summary;
    const best = summary.best_configs || {};
    const allRes = data.all_results || [];
    const pdResults = allRes.filter(r => r.architecture === 'PD');
    const hasVLLM = charts.vllm && charts.vllm.configs.length;
    const hasPD = pdResults.length > 0;

    const html = buildFullReport(runId, data, charts, rec, summary, best, allRes, hasPD, hasVLLM);

    const blob = new Blob([html], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `inftune-report-run-${runId}.html`;
    a.click();
    URL.revokeObjectURL(url);
}

function buildFullReport(runId, data, charts, rec, summary, best, allRes, hasPD, hasVLLM) {
    const secRec = buildRecSection(runId, data, rec, summary, best, allRes);
    const secTP = buildTPSection(rec, charts);
    const secCfg = buildCfgSection(runId, data, charts, allRes, hasPD);
    const secCmp = buildCmpSection(runId, rec, data);
    const secStep9 = buildStep9Section(data);
    const secCal = buildCalSection(data);
    const secVLLM = buildVLLMSection(charts, hasVLLM);
    const secEpp = buildEppTuningSection(runId, data);
    const secTestCfg = buildTestSettingsSection(data);

    const dlTabs = [];
    if (secRec) dlTabs.push({ id: 'rec', label: '&#9733; Recommendation', html: secRec });
    if (secTP) dlTabs.push({ id: 'tp', label: '&#9881; TP Calibration', html: secTP });
    if (secCfg) dlTabs.push({ id: 'cfg', label: '&#9776; Configurations', html: secCfg });
    if (secCmp) dlTabs.push({ id: 'cmp', label: '&#8596; Comparison', html: secCmp });
    if (secStep9) dlTabs.push({ id: 'step9', label: '&#128269; Latency Search', html: secStep9 });
    if (secCal) dlTabs.push({ id: 'cal', label: '&#9878; Calibrated Load', html: secCal });
    if (secVLLM) dlTabs.push({ id: 'vllm', label: '&#9889; vLLM Metrics', html: secVLLM });
    if (secEpp) dlTabs.push({ id: 'epp', label: '&#9881; EPP Tuning', html: secEpp });
    if (secTestCfg) dlTabs.push({ id: 'settings', label: '&#9881; Test Settings', html: secTestCfg });

    let out = buildHead(runId);
    out += `<h1>Inftune Studio Optimization Report &mdash; Run #${runId}</h1>`;
    out += `<p style="color:#64748b;">Generated: ${new Date().toLocaleString()}</p>`;

    if (dlTabs.length > 1) {
        out += '<div class="dl-tab-bar">';
        dlTabs.forEach((t, i) => { out += `<div class="dl-tab${i === 0 ? ' active' : ''}" onclick="switchDlTab('${t.id}')">${t.label}</div>`; });
        out += '</div>';
    }
    dlTabs.forEach((t, i) => {
        out += `<div id="dl-pane-${t.id}" class="dl-pane${i === 0 ? ' active' : ''}">${t.html}</div>`;
    });

    out += buildChartScript(data, charts, allRes);
    out += '</body></html>';
    return out;
}

// ── Head & Styles ────────────────────────────────────────────────────────────
function buildHead(runId) {
    return `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Inftune Studio Report - Run ${runId}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"><\/script>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;width:95%;margin:0 auto;padding:20px;background:#f8fafc;color:#1e293b}
h1{color:#1e293b;border-bottom:3px solid #10b981;padding-bottom:10px}
h2{margin-top:30px;border-bottom:2px solid #3b82f6;padding-bottom:8px;color:#1e293b}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin:20px 0}
.stat-card{background:white;padding:20px;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.08);text-align:center}
.stat-card .val{font-size:2em;font-weight:800;color:#1e293b}
.stat-card .lbl{color:#64748b;font-size:0.85em}
.chart-box{background:white;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.08);margin:20px 0;padding:16px}
.chart-box h3{margin:0 0 10px;color:#1e293b}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
table{width:100%;border-collapse:collapse}
th{background:#1e293b;color:white;padding:10px;text-align:left}
th[onclick]{cursor:pointer}
th[onclick]:hover{background:#334155}
td{padding:8px 10px;border-bottom:1px solid #f1f5f9}
.pareto{background:#f0fdf4;font-weight:600}
.dl-tab-bar{display:flex;gap:4px;padding:12px 0 0;border-bottom:2px solid #e5e7eb;margin:20px 0 0;flex-wrap:wrap;position:sticky;top:0;background:#f8fafc;z-index:10}
.dl-tab{padding:8px 16px;font-size:13px;font-weight:600;color:#6b7280;cursor:pointer;border-radius:8px 8px 0 0;border-bottom:2px solid transparent;margin-bottom:-2px;user-select:none}
.dl-tab:hover{color:#374151;background:#f3f4f6}
.dl-tab.active{color:#1e293b;border-bottom-color:#3b82f6;background:#eff6ff}
.dl-pane{display:none;padding-top:16px}
.dl-pane.active{display:block}
@media print{body{width:100%;padding:10px}h1{font-size:1.2em}.dl-tab-bar{display:none!important}.dl-pane{display:block!important;page-break-before:auto}.chart-box,.stat-card{break-inside:avoid}table{font-size:0.85em}}
.section-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:24px;font-size:0.9em}
.section-hdr{font-weight:700;color:#1e293b;margin-bottom:10px;padding-bottom:4px}
</style></head><body>`;
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function dlStatCard(val, lbl) {
    return `<div class="stat-card"><div class="val">${val}</div><div class="lbl">${lbl}</div></div>`;
}
function dlFmt(v, d) { return v != null ? v.toFixed(d != null ? d : 1) : '-'; }
function dlPctChange(val, base, lowerBetter) {
    if (!base || !val) return { pct: '-', color: '#64748b', arrow: '' };
    const pct = ((val - base) / base * 100).toFixed(1);
    const better = lowerBetter ? parseFloat(pct) < 0 : parseFloat(pct) > 0;
    return { pct, color: better ? '#059669' : '#dc2626', arrow: better ? (lowerBetter ? '&#9660;' : '&#9650;') : (lowerBetter ? '&#9650;' : '&#9660;') };
}

// ── Recommendation Tab ──────────────────────────────────────────────────────
function buildRecSection(runId, data, rec, summary, best, allRes) {
    let s = '';

    // Goal banner
    if (rec.goal_info) {
        const gColors = { ttft: '#3b82f6', throughput: '#f59e0b', balanced: '#10b981', aggregated_only: '#64748b', pd_only: '#8b5cf6', ep_only: '#0ea5e9' };
        const gIcons = { ttft: '&#9201;', throughput: '&#9889;', balanced: '&#9878;', aggregated_only: '&#9634;', pd_only: '&#8644;', ep_only: '&#9881;' };
        const gc = gColors[rec.goal] || '#10b981';
        s += `<div style="border:3px solid ${gc};border-left:8px solid ${gc};border-radius:10px;margin:20px 0;overflow:hidden;">`;
        s += `<div style="background:${gc};color:white;padding:14px 20px;font-size:1.3em;font-weight:800;">${gIcons[rec.goal] || ''} ${rec.goal_info.name}</div>`;
        s += `<div style="background:${gc}dd;color:white;padding:8px 20px;font-size:0.92em;">`;
        s += `Model: <strong>${rec.model}</strong> &nbsp;|&nbsp; ISL: <strong>${rec.workload.isl}</strong>`;
        if (rec.workload.isl_stdev) s += ` (&sigma;=${rec.workload.isl_stdev})`;
        s += ` | OSL: <strong>${rec.workload.osl}</strong>`;
        if (rec.workload.osl_stdev) s += ` (&sigma;=${rec.workload.osl_stdev})`;
        if (rec.workload.turns && rec.workload.turns > 1) s += ` | Turns: <strong>${rec.workload.turns}</strong>`;
        s += ` &nbsp;|&nbsp; Users: <strong>${rec.workload.users}</strong> &nbsp;|&nbsp; Tests: <strong>${rec.total_tests}</strong>`;
        if (rec.total_duration) s += ` &nbsp;|&nbsp; Duration: <strong>${rec.total_duration}</strong>`;
        s += '</div>';
        s += `<div style="padding:20px;"><p style="color:#334155;margin:0;font-size:0.95em;line-height:1.6;">${rec.goal_info.description}</p></div></div>`;
    }

    // Per-percentile recommendation cards
    if (rec.recommendations && Object.keys(rec.recommendations).length) {
        const goalIcons = { response_time: '&#9201;', throughput: '&#9889;' };
        const goalColors = { response_time: '#3b82f6', throughput: '#f59e0b' };
        const goalExplain = {
            response_time: 'Best for chatbots, real-time assistants, and interactive applications where users are waiting for a reply.',
            throughput: 'Best for batch processing, API services, and high-volume workloads where you need to handle the most requests per second.',
        };
        const bp = rec.best_by_percentile || {};
        const pctls = ['p90', 'p95', 'p99'];
        const goalOrder = ['response_time', 'throughput'];

        s += '<div style="border:2px solid #10b981;border-left:6px solid #10b981;border-radius:10px;margin:20px 0;overflow:hidden;">';
        s += '<div style="background:linear-gradient(135deg,#ecfdf5,#d1fae5);padding:14px 20px;font-size:1.2em;font-weight:800;color:#1e293b;">Deployment Recommendation</div>';
        s += '<div style="padding:24px;">';

        pctls.forEach((p, pi) => {
            s += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:12px;">';
            for (const key of goalOrder) {
                const r = rec.recommendations[key];
                if (!r) { s += '<div></div>'; continue; }
                const c = r.config;
                const isPrimary = (rec.goal === 'ttft' && key === 'response_time') || (rec.goal === 'throughput' && key === 'throughput');
                const archKey = (r.architecture || '').toLowerCase() === 'pd' ? 'pd' : 'aggregated';

                let cardConfig, cardDeploy, cardArch;
                if (pi === 0) {
                    cardConfig = c; cardDeploy = r.deploy; cardArch = r.architecture;
                } else {
                    const bpData = (bp[p] || {})[archKey];
                    if (!bpData) { s += '<div></div>'; continue; }
                    cardConfig = bpData; cardDeploy = bpData.config_name; cardArch = archKey.toUpperCase();
                }

                const border = (pi === 0 && isPrimary) ? `3px solid ${goalColors[key]}` : `2px solid ${goalColors[key]}40`;
                const badge = (pi === 0 && isPrimary) ? `<span style="background:${goalColors[key]};color:white;font-size:0.7em;padding:2px 8px;border-radius:4px;margin-left:8px;">PRIMARY</span>` : '';
                const archBadge = cardArch ? `<span style="background:#64748b;color:white;font-size:0.65em;padding:2px 6px;border-radius:3px;margin-left:6px;">${cardArch}</span>` : '';
                const pLabel = p.toUpperCase();

                s += `<div style="background:white;border:${border};border-radius:10px;padding:16px;">`;
                s += `<div style="font-weight:800;color:${goalColors[key]};font-size:0.85em;text-transform:uppercase;margin-bottom:8px;">${goalIcons[key] || ''} ${r.goal} &mdash; ${pLabel}${badge}${archBadge}</div>`;
                s += `<div style="font-size:1.4em;font-weight:800;color:#1e293b;margin-bottom:4px;">${cardDeploy}</div>`;

                const ttftVal = pi === 0 ? c.ttft_p90 : cardConfig.ttft;
                const tputVal = pi === 0 ? c.throughput_p90 : cardConfig.throughput;
                const tputMean = pi === 0 ? c.throughput_mean : cardConfig.throughput_mean;
                const gpus = pi === 0 ? c.gpus : cardConfig.gpus;
                const conc = pi === 0 ? c.concurrency : cardConfig.concurrency;
                const ratio = (pi === 0 && c.ratio && c.decode_pods > 0) ? `P:D ratio ${c.ratio} | ` : '';
                const userConc = rec.workload ? rec.workload.users : null;
                const concStr = conc ? ` | c=${conc}${userConc && userConc !== conc ? ' (from ' + userConc + ')' : ''}` : '';
                const meanStr = tputMean ? ` | Throughput Mean: <strong>${tputMean} req/s</strong>` : '';

                s += `<div style="font-size:0.9em;color:#475569;">${ratio}TTFT ${pLabel}: <strong>${ttftVal} ms</strong> | Throughput ${pLabel}: <strong>${tputVal} req/s</strong>${meanStr} | ${gpus} GPUs${concStr}</div>`;
                if (pi === 0) s += `<div style="font-size:0.82em;color:#64748b;margin-top:8px;line-height:1.5;">${goalExplain[key] || ''}</div>`;
                s += '</div>';
            }
            s += '</div>';
        });

        // Optimal TP + test counts
        if (rec.optimal_decode_tp || rec.optimal_prefill_tp || rec.pd_tests_count || rec.ep_tests_count) {
            s += '<div style="background:#f8fafc;border-radius:8px;padding:14px 18px;display:flex;gap:32px;flex-wrap:wrap;font-size:0.9em;margin-top:12px;">';
            if (rec.optimal_decode_tp) s += `<div><strong>Optimal Decode TP:</strong> ${rec.optimal_decode_tp.tp} <span style="color:#64748b">(${rec.optimal_decode_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.optimal_prefill_tp) s += `<div><strong>Optimal Prefill TP:</strong> ${rec.optimal_prefill_tp.tp} <span style="color:#64748b">(${rec.optimal_prefill_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.pd_tests_count) s += `<div><strong>PD Splits Tested:</strong> ${rec.pd_tests_count}</div>`;
            if (rec.ep_tests_count) s += `<div><strong>EP Configs Tested:</strong> ${rec.ep_tests_count}</div>`;
            s += '</div>';
        }

        // Constraint notes
        if (rec.constraint_notes && rec.constraint_notes.length) {
            s += '<div style="background:#fffbeb;border:2px solid #f59e0b;border-left:6px solid #f59e0b;border-radius:8px;padding:14px 18px;margin-top:12px;">';
            s += '<div style="font-weight:700;color:#92400e;margin-bottom:8px;font-size:0.95em;">&#9888; Configuration Constraints</div>';
            rec.constraint_notes.forEach(n => { s += `<p style="color:#78350f;margin:0 0 8px;font-size:0.88em;line-height:1.6;">${n}</p>`; });
            s += '</div>';
        }

        s += '</div></div>';
    }

    // EPP-Optimized Recommendation (from Step 11)
    if (data.epp_tuning && data.epp_tuning.by_architecture) {
        const eppArch = data.epp_tuning.by_architecture;
        if (Object.values(eppArch).some(t => t && t.length > 0)) {
            s += '<div style="margin-top:24px;border:2px solid #7c3aed;border-left:6px solid #7c3aed;border-radius:10px;overflow:hidden;">';
            s += '<div style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);color:white;padding:14px 20px;font-size:1.1em;font-weight:800;">EPP-Optimized Recommendation (Step 9)</div>';
            s += '<div style="padding:12px 20px 4px;color:#475569;font-size:0.9em;">Same deployment with tuned EPP scoring weights. The gateway routes requests more efficiently, improving latency without changing the inference pods.</div>';
            s += '<div style="padding:16px 20px;">';

            ['p90', 'p95', 'p99'].forEach(p => {
                const pLabel = p.toUpperCase();
                s += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:12px;">';

                [{ key: 'aggregated', goalLabel: '&#9201; TTFT', archLabel: 'AGGREGATED' },
                 { key: 'pd', goalLabel: '&#9889; THROUGHPUT', archLabel: 'PD' }].forEach(({ key, goalLabel, archLabel }) => {
                    const trials = eppArch[key] || [];
                    const b = trials.length ? trials.reduce((a, c) => ((a[`ttft_${p}`] || Infinity) < (c[`ttft_${p}`] || Infinity)) ? a : c) : null;
                    if (b && b[`ttft_${p}`]) {
                        const w = b.weights || {};
                        s += `<div style="background:white;border:2px solid #7c3aed40;border-radius:10px;padding:16px;">`;
                        s += `<div style="font-weight:800;color:#7c3aed;font-size:0.85em;text-transform:uppercase;margin-bottom:8px;">${goalLabel} ${pLabel} <span style="background:#7c3aed;color:white;font-size:0.7em;padding:2px 8px;border-radius:4px;margin-left:6px;">EPP TUNED</span> <span style="background:#64748b;color:white;font-size:0.65em;padding:2px 6px;border-radius:3px;margin-left:4px;">${archLabel}</span></div>`;
                        s += `<div style="font-size:1.3em;font-weight:800;color:#1e293b;margin-bottom:4px;">${b.config_name}</div>`;
                        const concStr = b.concurrency ? ` | c=${b.concurrency}` : '';
                        const eppMeanStr = b.throughput_mean ? ` | Mean: <strong>${b.throughput_mean} req/s</strong>` : '';
                        s += `<div style="font-size:0.9em;color:#475569;">TTFT ${pLabel}: <strong>${b[`ttft_${p}`]} ms</strong> | Throughput: <strong>${b[`throughput_${p}`] || b.throughput_p90} req/s</strong>${eppMeanStr}${concStr}</div>`;
                        s += `<div style="font-size:0.8em;color:#7c3aed;margin-top:4px;">EPP: ${b.name} (${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'})</div>`;
                        s += '</div>';
                    } else {
                        s += '<div></div>';
                    }
                });

                s += '</div>';
            });

            s += '</div></div>';
        }
    }

    // Summary stat cards
    s += '<div class="stats">';
    s += dlStatCard(summary.successful_tests, `Successful Tests (${summary.total_tests} total)`);
    if (best.lowest_latency) {
        s += dlStatCard(best.lowest_latency.ttft_p90.toFixed(1) + ' ms', 'Best TTFT P90');
        if (best.lowest_latency.ttft_p95) s += dlStatCard(best.lowest_latency.ttft_p95.toFixed(1) + ' ms', 'Best TTFT P95');
        if (best.lowest_latency.ttft_p99) s += dlStatCard(best.lowest_latency.ttft_p99.toFixed(1) + ' ms', 'Best TTFT P99');
    }
    if (best.highest_throughput) {
        s += dlStatCard(best.highest_throughput.throughput_p90.toFixed(2) + ' req/s', 'Best Throughput P90');
        if (best.highest_throughput.throughput_p95) s += dlStatCard(best.highest_throughput.throughput_p95.toFixed(2) + ' req/s', 'Best Throughput P95');
        if (best.highest_throughput.throughput_p99) s += dlStatCard(best.highest_throughput.throughput_p99.toFixed(2) + ' req/s', 'Best Throughput P99');
    }
    if (best.most_efficient) s += dlStatCard(best.most_efficient.efficiency.toFixed(3), 'Best Efficiency (req/s/GPU)');
    s += '</div>';

    return s;
}

// ── TP Calibration Tab ──────────────────────────────────────────────────────
function buildTPSection(rec, charts) {
    let s = '';
    s += '<div class="grid2"><div class="chart-box"><h3>Decode TP Sweep</h3><div id="tp-dec" style="height:430px"></div></div>';
    s += '<div class="chart-box"><h3>Prefill TP Sweep</h3><div id="tp-pre" style="height:430px"></div></div></div>';
    s += '<div class="chart-box"><h3>TP Calibration (Pareto)</h3><div id="p1" style="height:430px"></div></div>';
    return s;
}

// ── Configurations Tab ──────────────────────────────────────────────────────
function buildCfgSection(runId, data, charts, allRes, hasPD) {
    let s = '';
    s += '<div class="grid2"><div class="chart-box"><h3>Throughput vs Latency</h3><div id="p2" style="height:430px"></div></div>';
    s += '<div class="chart-box"><h3>GPU Efficiency</h3><div id="p3" style="height:430px"></div></div></div>';
    s += '<div class="chart-box"><h3>Architecture Comparison</h3><div id="p4" style="height:430px"></div></div>';

    // Per-percentile PD charts
    if (hasPD) {
        ['P90', 'P95', 'P99'].forEach(p => {
            s += `<div class="chart-box"><h3>PD Configurations &mdash; TTFT &amp; Throughput (${p})</h3><div id="pd-ttft-${p.toLowerCase()}" style="height:500px"></div></div>`;
        });
    }

    // Pareto optimal table
    if (charts.pareto.pareto_table.length) {
        s += '<div class="chart-box"><h3>Pareto Optimal Configurations</h3><table><tr><th>Config</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>GPUs</th><th>Efficiency</th></tr>';
        charts.pareto.pareto_table.forEach((p, idx) => {
            const bt = idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
            [{name:'TTFT (ms)',p50:p.ttft_p50,p90:p.ttft_p90,p95:p.ttft_p95,p99:p.ttft_p99},
             {name:'ITL (ms)',p50:p.itl_p50,p90:p.itl_p90,p95:p.itl_p95,p99:p.itl_p99},
             {name:'Throughput (req/s)',p50:p.throughput_p50,p90:p.throughput_p90,p95:p.throughput_p95,p99:p.throughput_p99}
            ].forEach((m, mi) => {
                const bs = mi === 0 && idx > 0 ? bt : '';
                s += '<tr class="pareto">';
                if (mi === 0) {
                    const pEpp = (p.test_id && p.test_id.startsWith('step11-epp-')) ? '<br><span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
                    s += `<td rowspan="3" style="vertical-align:middle;font-weight:700;${bs}">${p.config_name}<br><span style="font-weight:400;font-size:0.85em;color:#64748b;">${p.architecture}</span>${pEpp}</td>`;
                }
                s += `<td style="color:#64748b;${bs}">${m.name}</td>`;
                s += `<td style="${bs}">${m.p50 ?? '-'}</td><td style="${bs}">${m.p90 ?? '-'}</td><td style="${bs}">${m.p95 ?? '-'}</td><td style="${bs}">${m.p99 ?? '-'}</td>`;
                if (mi === 0) s += `<td rowspan="3" style="vertical-align:middle;${bs}">${p.gpus}</td><td rowspan="3" style="vertical-align:middle;${bs}">${p.efficiency}</td>`;
                s += '</tr>';
            });
        });
        s += '</table></div>';
    }

    // All results table
    if (allRes.length) {
        const pn = new Set(charts.pareto.pareto_table.map(p => p.config_name));
        const tid = 'dl-all-configs';
        s += `<div class="chart-box"><h3>All Results (sorted by TTFT)</h3><table id="${tid}"><tr>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',0,'str')">Config &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',1,'str')">Arch &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',2,'num')">TTFT P90 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',3,'num')">TTFT P95 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',4,'num')">TTFT P99 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',5,'num')">Tput P90 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',6,'num')">Tput P95 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',7,'num')">Tput P99 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',8,'num')">ITL P90 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',9,'num')">GPUs &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',10,'num')">Efficiency &#x21C5;</th>`;
        s += '</tr>';
        allRes.forEach(r => {
            const cls = pn.has(r.config_name) ? ' class="pareto"' : '';
            const na = 'N/A';
            const eppBadge = (r.test_id && r.test_id.startsWith('step11-epp-')) ? ' <span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
            s += `<tr${cls}><td>${r.config_name}${eppBadge}</td><td>${r.architecture}</td><td data-val="${r.ttft_p90}">${r.ttft_p90}</td><td data-val="${r.ttft_p95 ?? ''}">${r.ttft_p95 ?? na}</td><td data-val="${r.ttft_p99 ?? ''}">${r.ttft_p99 ?? na}</td><td data-val="${r.throughput_p90}">${r.throughput_p90}</td><td data-val="${r.throughput_p95 ?? ''}">${r.throughput_p95 ?? na}</td><td data-val="${r.throughput_p99 ?? ''}">${r.throughput_p99 ?? na}</td><td data-val="${r.itl_p90 ?? ''}">${r.itl_p90 ?? na}</td><td data-val="${r.gpus}">${r.gpus}</td><td data-val="${r.efficiency}">${r.efficiency}</td></tr>`;
        });
        s += '</table></div>';
    }

    return s;
}

// ── Comparison Tab (Step 8) ─────────────────────────────────────────────────
function buildCmpSection(runId, rec, data) {
    if (!rec || (!rec.pd_vs_agg && !rec.ep_vs_agg)) return '';
    let s = '';

    // PD vs Aggregated
    if (rec.pd_vs_agg) {
        const cmp = rec.pd_vs_agg;
        const ttC = cmp.ttft_winner === 'PD' ? '#10b981' : '#f59e0b';
        const tpC = cmp.throughput_winner === 'PD' ? '#10b981' : '#f59e0b';
        s += '<div style="margin-top:16px;border-radius:10px;overflow:hidden;border:2px solid #6366f1;border-left:6px solid #6366f1;">';
        s += '<div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:12px 20px;color:white;font-weight:700;">PD vs Aggregated Comparison</div>';
        s += '<table><tr><th>Metric</th><th>PD (best)</th><th>Aggregated</th><th>Winner</th></tr>';
        s += `<tr><td><strong>TTFT P90</strong></td><td>${cmp.pd.ttft_p90} ms</td><td>${cmp.aggregated.ttft_p90} ms</td><td style="color:${ttC};font-weight:700;">${cmp.ttft_winner} (${cmp.ttft_diff_pct}% better)</td></tr>`;
        s += `<tr><td><strong>Throughput P90</strong></td><td>${cmp.pd.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_p90} req/s</td><td style="color:${tpC};font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
        s += '</table>';

        // % change table: all PD configs vs baseline
        if (rec.aggregated_baseline && data.all_results && data.all_results.length > 1) {
            const bl = rec.aggregated_baseline;
            const configs = data.all_results.filter(r => r.architecture === 'PD' && r.ttft_p90 && r.throughput_p90);
            if (configs.length && bl.ttft_p90 && bl.throughput_p90) {
                s += `<div style="padding:16px 20px 4px;"><div style="font-weight:700;font-size:0.95em;color:#1e293b;margin-bottom:4px;">All PD Configurations vs Aggregated Baseline</div>`;
                s += `<div style="color:#1e293b;font-size:0.92em;margin-bottom:12px;">% change relative to ${bl.config_name}. TTFT: negative (green) = better. Throughput: positive (green) = better.</div>`;
                s += '<table id="dl-pd-vs-agg"><tr>';
                s += `<th style="cursor:pointer;" onclick="sortReportTable('dl-pd-vs-agg',0,'str')">Configuration &#x21C5;</th>`;
                s += `<th style="cursor:pointer;" onclick="sortReportTable('dl-pd-vs-agg',1,'num')">TTFT P90 &#x21C5;</th>`;
                s += `<th style="cursor:pointer;" onclick="sortReportTable('dl-pd-vs-agg',2,'num')">TTFT vs Agg &#x21C5;</th>`;
                s += `<th style="cursor:pointer;" onclick="sortReportTable('dl-pd-vs-agg',3,'num')">Throughput P90 &#x21C5;</th>`;
                s += `<th style="cursor:pointer;" onclick="sortReportTable('dl-pd-vs-agg',4,'num')">Tput vs Agg &#x21C5;</th>`;
                s += '</tr>';
                [...configs].sort((a, b) => a.ttft_p90 - b.ttft_p90).forEach(cfg => {
                    const t = dlPctChange(cfg.ttft_p90, bl.ttft_p90, true);
                    const p = dlPctChange(cfg.throughput_p90, bl.throughput_p90, false);
                    const cmpEpp = (cfg.test_id && cfg.test_id.startsWith('step11-epp-')) ? ' <span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
                    s += `<tr><td><strong>${cfg.config_name}</strong>${cmpEpp}</td><td data-val="${cfg.ttft_p90}">${cfg.ttft_p90} ms</td><td data-val="${t.pct}" style="color:${t.color};font-weight:700;">${t.arrow} ${t.pct}%</td><td data-val="${cfg.throughput_p90}">${cfg.throughput_p90} req/s</td><td data-val="${p.pct}" style="color:${p.color};font-weight:700;">${p.arrow} ${p.pct}%</td></tr>`;
                });
                s += `<tr style="background:#f1f5f9;"><td><strong>${bl.config_name}</strong> <span style="background:#1f77b4;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">BASELINE</span></td><td>${bl.ttft_p90} ms</td><td style="color:#64748b;">-</td><td>${bl.throughput_p90} req/s</td><td style="color:#64748b;">-</td></tr>`;
                s += '</table></div>';
            }
        }
        s += '</div>';
    }

    // EP vs Aggregated
    if (rec.ep_vs_agg) {
        const cmp = rec.ep_vs_agg;
        const ttC = cmp.ttft_winner === 'EP' ? '#10b981' : '#f59e0b';
        const tpC = cmp.throughput_winner === 'EP' ? '#10b981' : '#f59e0b';
        s += '<div style="margin-top:16px;border-radius:10px;overflow:hidden;border:2px solid #6366f1;border-left:6px solid #6366f1;">';
        s += '<div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:12px 20px;color:white;font-weight:700;">EP vs Aggregated Comparison</div>';
        s += '<table><tr><th>Metric</th><th>EP (best)</th><th>Aggregated</th><th>Winner</th></tr>';
        s += `<tr><td><strong>TTFT P90</strong></td><td>${cmp.ep.ttft_p90} ms</td><td>${cmp.aggregated.ttft_p90} ms</td><td style="color:${ttC};font-weight:700;">${cmp.ttft_winner} (${cmp.ttft_diff_pct}% better)</td></tr>`;
        s += `<tr><td><strong>Throughput P90</strong></td><td>${cmp.ep.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_p90} req/s</td><td style="color:${tpC};font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
        s += '</table>';

        if (rec.aggregated_baseline && rec.ep_all_configs && rec.ep_all_configs.length) {
            const bl = rec.aggregated_baseline;
            if (bl.ttft_p90 && bl.throughput_p90) {
                s += '<div style="padding:16px 20px 4px;"><div style="font-weight:700;font-size:0.95em;color:#1e293b;margin-bottom:4px;">All EP Configurations vs Aggregated Baseline</div>';
                s += '<table><tr><th>Configuration</th><th>TTFT P90</th><th>TTFT vs Agg</th><th>Throughput P90</th><th>Tput vs Agg</th></tr>';
                [...rec.ep_all_configs].sort((a, b) => (b.throughput_p90 || 0) - (a.throughput_p90 || 0)).forEach(cfg => {
                    if (!cfg.ttft_p90 || !cfg.throughput_p90) return;
                    const t = dlPctChange(cfg.ttft_p90, bl.ttft_p90, true);
                    const p = dlPctChange(cfg.throughput_p90, bl.throughput_p90, false);
                    const label = `EP TP${cfg.tp} x ${cfg.replicas} replicas`;
                    s += `<tr><td><strong>${label}</strong></td><td>${cfg.ttft_p90} ms</td><td style="color:${t.color};font-weight:700;">${t.arrow} ${t.pct}%</td><td>${cfg.throughput_p90} req/s</td><td style="color:${p.color};font-weight:700;">${p.arrow} ${p.pct}%</td></tr>`;
                });
                s += `<tr style="background:#f1f5f9;"><td><strong>${bl.config_name}</strong> <span style="background:#1f77b4;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">BASELINE</span></td><td>${bl.ttft_p90} ms</td><td style="color:#64748b;">-</td><td>${bl.throughput_p90} req/s</td><td style="color:#64748b;">-</td></tr>`;
                s += '</table></div>';
            }
        }
        s += '</div>';
    }

    return s;
}

// ── Latency Search Tab (Step 10) ─────────────────────────────────────────────
function buildStep9Section(data) {
    if (!data.latency_search || !data.latency_search.trials || !data.latency_search.trials.length) return '';
    const ls = data.latency_search;
    const byArch = ls.by_architecture || {};
    const archKeys = Object.keys(byArch);
    const firstTrial = ls.trials[0];
    const targetMs = firstTrial.target_ms;
    const targetPct = firstTrial.target_percentile || 'p90';
    const metricKey = 'ttft_' + targetPct;
    let s = '';

    s += '<div style="border-radius:10px;overflow:hidden;border:2px solid #8b5cf6;border-left:6px solid #8b5cf6;margin-bottom:20px;">';
    s += '<div style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);padding:12px 20px;color:white;font-weight:700;">Step 10: Latency-Bounded Throughput Search</div>';
    s += `<div style="padding:12px 20px;font-size:0.95em;">Binary search over concurrency to find max throughput keeping TTFT ${targetPct.toUpperCase()} under <strong>${targetMs} ms</strong>.</div>`;

    const archConfigs = ls.arch_configs || {};
    archKeys.forEach((arch, ai) => {
        const trials = byArch[arch];
        const passing = trials.filter(t => t.meets_sla);
        const bestPassing = passing.length ? passing.reduce((a, b) => a.concurrency > b.concurrency ? a : b) : null;
        const cfgLabel = archConfigs[arch] || arch.toUpperCase();

        s += `<div style="padding:12px 20px;margin-top:4px;">`;
        s += `<div style="font-weight:700;font-size:1.05em;color:#1e293b;margin-bottom:10px;border-bottom:1px solid #e2e8f0;padding-bottom:6px;">${arch.toUpperCase()}: ${cfgLabel}</div>`;

        if (bestPassing) {
            const latVal = bestPassing[metricKey] != null ? bestPassing[metricKey].toFixed(1) : '-';
            const tputKey = 'throughput_' + targetPct;
            const tputVal = bestPassing[tputKey] != null ? bestPassing[tputKey].toFixed(2) : (bestPassing.throughput_p90 != null ? bestPassing.throughput_p90.toFixed(2) : '-');
            s += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;">';
            s += `<div style="background:#f0fdf4;border-radius:10px;padding:16px;text-align:center;border:1px solid #bbf7d0;"><div style="font-size:2em;font-weight:800;color:#059669;">${bestPassing.concurrency}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Optimal Concurrency</div></div>`;
            s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:2em;font-weight:800;color:#1e293b;">${latVal} <span style="font-size:0.5em;color:#64748b;">ms</span></div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">TTFT ${targetPct.toUpperCase()} at Optimal</div></div>`;
            s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:2em;font-weight:800;color:#1e293b;">${tputVal}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Throughput ${targetPct.toUpperCase()} (req/s)</div></div>`;
            s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:2em;font-weight:800;color:#1e293b;">${trials.length}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Tests Run</div></div>`;
            s += '</div>';
        }

        // Per-percentile chart divs
        ['p90', 'p95', 'p99'].forEach(pctl => {
            s += `<div id="dl-step9-${pctl}-${ai}" style="height:450px;background:#fff;border-radius:8px;border:1px solid #e2e8f0;margin-top:12px;"></div>`;
        });

        // Trial table
        const sorted = [...trials].sort((a, b) => a.concurrency - b.concurrency);
        s += '<div style="margin-top:12px;overflow-x:auto;"><table style="font-size:0.85em;"><tr><th>Concurrency</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Throughput P90</th><th>SLA</th></tr>';
        sorted.forEach(t => {
            const sla = t.meets_sla ? '<span style="color:#059669;">Yes</span>' : '<span style="color:#dc2626;">No</span>';
            s += `<tr><td style="font-weight:700;">${t.concurrency}</td><td>${dlFmt(t.ttft_p50)}</td><td>${dlFmt(t.ttft_p90)}</td><td>${dlFmt(t.ttft_p95)}</td><td>${dlFmt(t.ttft_p99)}</td><td>${dlFmt(t.throughput_p90, 2)}</td><td>${sla}</td></tr>`;
        });
        s += '</table></div></div>';
    });

    s += '</div>';
    return s;
}

// ── Calibrated Load Tab (Step 11) ───────────────────────────────────────────
function buildCalSection(data) {
    if (!data.calibrated_qps) return '';
    const cal = data.calibrated_qps;
    let s = '';

    // GPU sizing analysis
    if (cal.gpu_sizing) {
        const g = cal.gpu_sizing;
        s += '<div style="padding:12px 20px;background:#ecfdf5;border:1px solid #6ee7b7;border-radius:8px;margin-bottom:16px;font-size:0.9em;color:#065f46;">';
        s += '<div style="font-weight:700;margin-bottom:8px;">Cluster Capacity Analysis</div>';
        s += '<table style="width:auto;margin:0;font-size:0.95em;border:none;">';
        s += '<tr style="background:none;"><td style="border:none;padding:2px 16px 2px 0;color:#047857;"><strong>GPU Cost per Request</strong></td><td style="border:none;"></td></tr>';
        s += `<tr style="background:none;"><td style="border:none;padding:1px 16px 1px 12px;">Prefill</td><td style="border:none;">${g.isl} ISL / ${g.prefill_tpsg} TPSG = <strong>${g.prefill_cost} GPU-sec</strong></td></tr>`;
        s += `<tr style="background:none;"><td style="border:none;padding:1px 16px 1px 12px;">Decode</td><td style="border:none;">${g.osl} OSL / ${g.decode_tpsg} TPSG = <strong>${g.decode_cost} GPU-sec</strong></td></tr>`;
        s += `<tr style="background:none;"><td style="border:none;padding:1px 16px 1px 12px;">Total</td><td style="border:none;"><strong>${g.total_cost} GPU-sec/request</strong></td></tr>`;
        s += '<tr style="background:none;"><td style="border:none;padding:6px 16px 2px 0;color:#047857;"><strong>Sustainable Throughput</strong></td><td style="border:none;"></td></tr>';
        s += `<tr style="background:none;"><td style="border:none;padding:1px 16px 1px 12px;">Cluster capacity</td><td style="border:none;">${g.total_gpus} GPUs / ${g.total_cost} GPU-sec / ${g.headroom}x headroom = <strong>${g.sustainable_throughput_rps || g.sustainable_qps} req/s</strong> (${g.sustainable_concurrency || '?'} concurrent users)</td></tr>`;
        s += `<tr style="background:none;"><td style="border:none;padding:1px 16px 1px 12px;">Concurrency tested</td><td style="border:none;"><strong>${g.concurrency} simultaneous requests</strong></td></tr>`;
        s += `<tr style="background:none;"><td style="border:none;padding:1px 16px 1px 12px;">Ideal P/D ratio</td><td style="border:none;"><strong>${g.ideal_prefill_pct}% prefill</strong></td></tr>`;
        s += '</table></div>';
    }

    // Percentile breakdown table
    const entries = [];
    if (cal.pd) entries.push({ label: 'PD', entry: cal.pd });
    if (cal.aggregated) entries.push({ label: 'Aggregated', entry: cal.aggregated });
    if (cal.ep) entries.push({ label: 'EP', entry: cal.ep });

    const dlRequestedRps = cal.requested_rps != null ? cal.requested_rps : null;
    const rpsLabel = dlRequestedRps != null ? ` at ${Math.round(dlRequestedRps)} concurrent` : '';

    if (entries.length) {
        function findBest(metric, lower) { const v = entries.map(e => e.entry[metric]).filter(x => x != null); return !v.length ? null : lower ? Math.min(...v) : Math.max(...v); }
        const bT = findBest('ttft_p90', true), bP = findBest('throughput_p90', false), bI = findBest('itl_p90', true);
        const hl = (v, b) => v != null && v === b ? 'color:#059669;font-weight:700;' : '';

        s += `<div class="chart-box"><h3>Percentile Breakdown${rpsLabel}</h3>`;
        s += '<table><tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';
        entries.forEach(({ label, entry }, idx) => {
            [{name:'TTFT (ms)',p50:entry.ttft_p50,p90:entry.ttft_p90,p95:entry.ttft_p95,p99:entry.ttft_p99,b:bT,k:'ttft_p90'},
             {name:'ITL (ms)',p50:entry.itl_p50,p90:entry.itl_p90,p95:entry.itl_p95,p99:entry.itl_p99,b:bI,k:'itl_p90'},
             {name:'Throughput (req/s)',p50:entry.throughput_p50,p90:entry.throughput_p90,p95:entry.throughput_p95,p99:entry.throughput_p99,b:bP,k:'throughput_p90'}
            ].forEach((m, mi) => {
                const bs = mi === 0 && idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
                s += '<tr>';
                if (mi === 0) s += `<td rowspan="3" style="vertical-align:middle;font-weight:700;${bs}">${label}</td>`;
                s += `<td style="color:#64748b;${bs}">${m.name}</td><td style="${bs}">${m.p50 ?? '-'}</td><td style="${hl(entry[m.k], m.b)}${bs}">${m.p90 ?? '-'}</td><td style="${bs}">${m.p95 ?? '-'}</td><td style="${bs}">${m.p99 ?? '-'}</td></tr>`;
            });
        });
        s += '</table></div>';
    }

    // Overload impact
    const primary = cal.pd || cal.ep;
    const primaryLabel = cal.pd ? 'PD' : 'EP';
    const dlOverload = cal.overloaded_pd || cal.overloaded_ep;
    if (dlOverload && primary) {
        s += `<div class="chart-box"><h3>Overload Impact: ${primaryLabel} at Calibrated vs Overloaded Load</h3>`;
        s += '<table><tr><th>Configuration</th><th>Load</th><th>TTFT P90</th><th>Throughput P90</th></tr>';
        s += `<tr><td><strong>${primaryLabel} (calibrated)</strong></td><td>${dlRequestedRps != null ? Math.round(dlRequestedRps) + ' concurrent' : '-'}</td><td style="color:#059669;font-weight:700;">${primary.ttft_p90} ms</td><td style="color:#059669;font-weight:700;">${primary.throughput_p90} req/s</td></tr>`;
        s += `<tr><td><strong>${primaryLabel} (overloaded)</strong></td><td>${cal.concurrency != null ? cal.concurrency + ' concurrent' : '-'}</td><td style="color:#94a3b8;">${dlOverload.ttft_p90} ms</td><td style="color:#94a3b8;">${dlOverload.throughput_p90} req/s</td></tr>`;
        s += '</table></div>';
    }

    return s;
}

// ── vLLM Metrics Tab ────────────────────────────────────────────────────────
function buildVLLMSection(charts, hasVLLM) {
    if (!hasVLLM) return '';
    let s = '';
    s += '<div class="grid2"><div class="chart-box"><h3>TTFT Percentiles</h3><div id="v1" style="height:430px"></div></div><div class="chart-box"><h3>ITL Percentiles</h3><div id="v2" style="height:430px"></div></div></div>';
    s += '<div class="grid2"><div class="chart-box"><h3>E2E Latency</h3><div id="v3" style="height:430px"></div></div><div class="chart-box"><h3>Token Throughput</h3><div id="v4" style="height:430px"></div></div></div>';
    s += '<div class="grid2"><div class="chart-box"><h3>Request Queue & KV Cache</h3><div id="v5" style="height:430px"></div></div><div class="chart-box"><h3>Time Breakdown & Preemptions</h3><div id="v6" style="height:430px"></div></div></div>';
    if (charts.vllm.network && charts.vllm.network.pod_tx.some(v => v > 0)) {
        s += '<div class="grid2"><div class="chart-box"><h3>Pod Network</h3><div id="v7" style="height:430px"></div></div>';
        if (charts.vllm.network.ib_rx.some(v => v > 0)) s += '<div class="chart-box"><h3>InfiniBand RDMA</h3><div id="v8" style="height:430px"></div></div>';
        s += '</div>';
    }
    return s;
}

// ── EPP Tuning Tab (Step 9) ────────────────────────────────────────────────
function buildEppTuningSection(runId, data) {
    if (!data.epp_tuning || !data.epp_tuning.by_architecture) return '';
    const eppData = data.epp_tuning;
    const archKeys = Object.keys(eppData.by_architecture);
    if (!archKeys.some(k => (eppData.by_architecture[k] || []).length > 0)) return '';

    let s = '';
    archKeys.forEach((arch, archIdx) => {
        const trials = eppData.by_architecture[arch];
        if (!trials || !trials.length) return;
        const archLabel = arch.toUpperCase();
        const bestTrial = trials.reduce((a, b) => (a.ttft_p90 || Infinity) < (b.ttft_p90 || Infinity) ? a : b);

        s += `<div class="chart-box" style="border-left:4px solid #7c3aed;">`;
        s += `<h3 style="color:#7c3aed;">Step 9: EPP Tuning &mdash; ${archLabel}</h3>`;
        s += '<p style="font-size:0.9em;color:#475569;">Same deployment, different EPP scoring weights. Each test swapped only the gateway configmap (~10s) to isolate the impact of request routing.</p>';

        // Summary cards
        s += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;">';
        s += `<div style="background:#f0fdf4;border-radius:10px;padding:16px;text-align:center;border:1px solid #bbf7d0;"><div style="font-size:1.5em;font-weight:800;color:#059669;">${bestTrial.name}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Best Strategy</div></div>`;
        s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:1.5em;font-weight:800;color:#1e293b;">${bestTrial.ttft_p90 || 'N/A'} <span style="font-size:0.5em;color:#64748b;">ms</span></div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">TTFT P90</div></div>`;
        s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:1.5em;font-weight:800;color:#1e293b;">${bestTrial.throughput_p90 || 'N/A'}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Throughput P90 (req/s)</div></div>`;
        s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:1.5em;font-weight:800;color:#1e293b;">${trials.length}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Strategies Tested</div></div>`;
        s += '</div>';

        // Chart divs for P90/P95/P99
        ['p90', 'p95', 'p99'].forEach(pctl => {
            s += `<div id="dl-epp-${arch}-${pctl}" style="height:400px;background:#fff;border-radius:8px;border:1px solid #e2e8f0;margin-top:8px;"></div>`;
        });

        // Results table
        s += '<table style="margin-top:12px;"><tr><th>Strategy</th><th>Weights (C:K:Q)</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Tput P90</th><th>ITL P90</th></tr>';
        trials.forEach(e => {
            const isBest = e === bestTrial;
            const cls = isBest ? ' class="pareto"' : '';
            const w = e.weights || {};
            const wStr = `${w.prefix_cache || '?'}:${w.kv_cache || '?'}:${w.queue || '?'}`;
            const na = 'N/A';
            s += `<tr${cls}><td><strong>${e.name}</strong>${isBest ? ' *' : ''}</td><td>${wStr}</td>`;
            s += `<td>${e.ttft_p50 ?? na}</td><td>${e.ttft_p90 ?? na}</td><td>${e.ttft_p95 ?? na}</td><td>${e.ttft_p99 ?? na}</td>`;
            s += `<td>${e.throughput_p90 ?? na}</td><td>${e.itl_p90 ?? na}</td></tr>`;
        });
        s += '</table></div>';
    });

    return s;
}

// ── Test Settings Tab ───────────────────────────────────────────────────────
function buildTestSettingsSection(data) {
    if (!data.run_config) return '';
    const rc = data.run_config;
    const na = 'N/A';
    const adv = rc.advanced_vllm || {};
    const advVal = (key, fallback) => { const s = adv[key]; return s && s.mode === 'custom' && s.value != null ? s.value : (fallback != null ? fallback : 'auto'); };
    const advToggle = (key, fallback) => { const s = adv[key]; return s ? (s.mode === 'on' ? 'On' : s.mode === 'off' ? 'Off' : fallback) : fallback; };

    let s = '<div class="chart-box"><h3>User Defined Test Settings</h3>';
    s += '<p style="font-size:0.9em;color:#475569;">All settings configured for this optimization run. These apply to every test &mdash; only the architecture, TP values, and pod counts vary between tests.</p>';
    s += '<div class="section-grid">';

    // Left: Workload + Search Strategy
    s += '<div>';
    s += '<div class="section-hdr" style="border-bottom:2px solid #10b981;">Workload</div><div style="line-height:2.2;margin-bottom:20px;">';
    s += `<div><span style="color:#64748b;">Model:</span> <strong>${rc.model_name || na}</strong></div>`;
    s += `<div><span style="color:#64748b;">ISL:</span> ${rc.isl}${rc.isl_stdev ? ' (&sigma;=' + rc.isl_stdev + ')' : ''}</div>`;
    s += `<div><span style="color:#64748b;">OSL:</span> ${rc.osl}${rc.osl_stdev ? ' (&sigma;=' + rc.osl_stdev + ')' : ''}</div>`;
    s += `<div><span style="color:#64748b;">Concurrent Users:</span> ${rc.qps != null ? Math.round(rc.qps) : na}</div>`;
    s += `<div><span style="color:#64748b;">Rate Type:</span> ${rc.rate_type || 'concurrent'}</div>`;
    s += `<div><span style="color:#64748b;">Test Duration:</span> ${rc.test_duration || 300}s</div>`;
    s += `<div><span style="color:#64748b;">Stop Mode:</span> ${rc.stop_mode || 'duration'}</div>`;
    if (rc.max_requests) s += `<div><span style="color:#64748b;">Max Requests:</span> ${rc.max_requests}</div>`;
    if (rc.turns > 1) s += `<div><span style="color:#64748b;">Turns:</span> ${rc.turns}</div>`;
    s += `<div><span style="color:#64748b;">Workload Mode:</span> ${rc.workload_mode || 'synthetic'}</div>`;
    if (rc.dataset_source) s += `<div><span style="color:#64748b;">Dataset:</span> <span style="word-break:break-all;">${rc.dataset_source}</span></div>`;
    if (rc.dataset_column) s += `<div><span style="color:#64748b;">Dataset Column:</span> ${rc.dataset_column}</div>`;
    if (rc.prefix_cache_hit_pct > 0) s += `<div><span style="color:#64748b;">Prefix Cache Hit:</span> ${rc.prefix_cache_hit_pct}%</div>`;
    s += '</div>';

    s += '<div class="section-hdr" style="border-bottom:2px solid #6366f1;">Search Strategy</div><div style="line-height:2.2;">';
    s += `<div><span style="color:#64748b;">Optimization Goal:</span> <strong>${(rc.objective || 'ttft').toUpperCase()}</strong></div>`;
    s += `<div><span style="color:#64748b;">Total GPUs:</span> ${rc.total_gpus || na}</div>`;
    s += `<div><span style="color:#64748b;">TP Options:</span> ${(rc.tp_options || []).join(', ') || na}</div>`;
    s += `<div><span style="color:#64748b;">TP Pair Breadth:</span> Top-${rc.tp_pair_top_n || 2}</div>`;
    s += `<div><span style="color:#64748b;">P/D Ratio Search:</span> ${rc.pd_search_mode === 'exhaustive' ? 'Exhaustive' : 'Smart'}</div>`;
    s += `<div><span style="color:#64748b;">Use Achievable Concurrency:</span> ${rc.use_achievable_qps ? 'Yes' : 'No'}</div>`;
    s += `<div><span style="color:#64748b;">Headroom:</span> ${rc.headroom || 1.3}x</div>`;
    if (rc.latency_constraint_enabled) {
        s += `<div><span style="color:#64748b;">Latency SLA:</span> ${rc.latency_constraint_ms}ms @ ${rc.latency_constraint_percentile}</div>`;
    } else {
        s += `<div><span style="color:#64748b;">Latency SLA:</span> Disabled</div>`;
    }
    s += '</div></div>';

    // Right: Infrastructure + Advanced + EPP
    s += '<div>';
    s += '<div class="section-hdr" style="border-bottom:2px solid #f59e0b;">Infrastructure</div><div style="line-height:2.2;margin-bottom:20px;">';
    s += `<div><span style="color:#64748b;">Image:</span> <span style="word-break:break-all;font-size:0.9em;">${rc.image || na}</span></div>`;
    s += `<div><span style="color:#64748b;">Namespace:</span> ${rc.namespace || na}</div>`;
    s += `<div><span style="color:#64748b;">PVC:</span> ${rc.pvc_name || na}</div>`;
    s += `<div><span style="color:#64748b;">Network Type:</span> ${rc.network_type || na}</div>`;
    s += `<div><span style="color:#64748b;">NCCL IB HCA:</span> ${rc.nccl_ib_hca || na}</div>`;
    if (rc.rdma_nics_per_node) s += `<div><span style="color:#64748b;">RDMA NICs/Node:</span> ${rc.rdma_nics_per_node}</div>`;
    s += '</div>';

    s += '<div class="section-hdr" style="border-bottom:2px solid #8b5cf6;">Advanced vLLM Settings</div><div style="line-height:2.2;">';
    s += `<div><span style="color:#64748b;">Max Model Len:</span> ${advVal('max_model_len', rc.max_model_len)}</div>`;
    s += `<div><span style="color:#64748b;">GPU Memory Utilization:</span> ${advVal('gpu_memory_utilization', rc.gpu_memory_utilization)}</div>`;
    s += `<div><span style="color:#64748b;">Block Size:</span> ${advVal('block_size', 'auto')}</div>`;
    s += `<div><span style="color:#64748b;">Max Num Seqs:</span> ${advVal('max_num_seqs', 'auto')}</div>`;
    s += `<div><span style="color:#64748b;">Max Batched Tokens:</span> ${advVal('max_num_batched_tokens', 'auto')}</div>`;
    s += `<div><span style="color:#64748b;">Dtype:</span> ${advVal('dtype', 'auto')}</div>`;
    s += `<div><span style="color:#64748b;">KV Cache Dtype:</span> ${advVal('kv_cache_dtype', 'auto')}</div>`;
    s += `<div><span style="color:#64748b;">Pipeline Parallel:</span> ${advVal('pipeline_parallel_size', 'auto')}</div>`;
    s += `<div><span style="color:#64748b;">Tool Call Parser:</span> ${advVal('tool_call_parser', 'auto')}</div>`;
    s += `<div><span style="color:#64748b;">Prefix Caching:</span> ${advToggle('enable_prefix_caching', 'On (auto)')}</div>`;
    s += `<div><span style="color:#64748b;">Custom All-Reduce:</span> ${advToggle('disable_custom_all_reduce', 'Enabled (auto)')}</div>`;
    s += `<div><span style="color:#64748b;">Trust Remote Code:</span> ${advToggle('trust_remote_code', 'On (auto)')}</div>`;
    s += `<div><span style="color:#64748b;">Disable Log Requests:</span> ${advToggle('disable_log_requests', 'On (auto)')}</div>`;
    s += `<div><span style="color:#64748b;">Auto Tool Choice:</span> ${advToggle('enable_auto_tool_choice', 'Off (auto)')}</div>`;
    s += `<div><span style="color:#64748b;">vLLM Debug Logs:</span> ${advToggle('vllm_debug_logs', 'Off (auto)')}</div>`;
    s += `<div><span style="color:#64748b;">NCCL Debug Logs:</span> ${advToggle('nccl_debug_logs', 'Off (auto)')}</div>`;
    s += '</div>';

    const eppLabels = { balanced: 'Balanced', cache_optimized: 'Cache Optimized', queue_balanced: 'Queue Balanced', latency_aware: 'Latency Aware', custom: 'Custom' };
    s += '<div class="section-hdr" style="border-bottom:2px solid #7c3aed;">EPP Configuration</div><div style="line-height:2.2;">';
    s += `<div><span style="color:#64748b;">Scoring Preset:</span> <strong>${eppLabels[rc.epp_preset] || rc.epp_preset || 'Balanced'}</strong></div>`;
    s += `<div><span style="color:#64748b;">EPP Tuning (Step 9):</span> ${rc.epp_benchmark ? 'Enabled' : 'Disabled'}</div>`;
    if (rc.epp_config) {
        const ec = rc.epp_config;
        if (ec.maxPrefixBlocksToMatch) s += `<div><span style="color:#64748b;">Max Prefix Blocks:</span> ${ec.maxPrefixBlocksToMatch}</div>`;
        if (ec.lruCapacityPerServer) s += `<div><span style="color:#64748b;">LRU Capacity/Server:</span> ${ec.lruCapacityPerServer}</div>`;
        if (ec.nonCachedTokens) s += `<div><span style="color:#64748b;">Non-Cached Tokens:</span> ${ec.nonCachedTokens}</div>`;
    }
    s += '</div></div>';

    s += '</div></div>';
    return s;
}

// ── Chart Rendering Script ──────────────────────────────────────────────────
function buildChartScript(data, charts, allRes) {
    let s = '<script>';
    s += `function switchDlTab(id){document.querySelectorAll('.dl-tab').forEach(function(t){t.classList.remove('active')});document.querySelectorAll('.dl-pane').forEach(function(p){p.classList.remove('active')});var tab=document.querySelector('.dl-tab[onclick*=\"'+id+'\"]');if(tab)tab.classList.add('active');var pane=document.getElementById('dl-pane-'+id);if(pane){pane.classList.add('active');pane.querySelectorAll('[class*="js-plotly"]').forEach(function(p){Plotly.Plots.resize(p)});}}`;
    s += 'function sortReportTable(tableId,colIdx,type){var table=document.getElementById(tableId);if(!table)return;var rows=Array.from(table.querySelectorAll("tr")).slice(1);var baselineRows=rows.filter(function(r){return r.classList.contains("baseline-row")});var dataRows=rows.filter(function(r){return !r.classList.contains("baseline-row")});var dir=table.getAttribute("data-sort-col")===String(colIdx)&&table.getAttribute("data-sort-dir")==="asc"?"desc":"asc";table.setAttribute("data-sort-col",colIdx);table.setAttribute("data-sort-dir",dir);dataRows.sort(function(a,b){var aCell=a.cells[colIdx],bCell=b.cells[colIdx];var aVal,bVal;if(type==="num"){aVal=parseFloat(aCell.getAttribute("data-val")||aCell.textContent.replace(/[^0-9.\\-]/g,""))||0;bVal=parseFloat(bCell.getAttribute("data-val")||bCell.textContent.replace(/[^0-9.\\-]/g,""))||0}else{aVal=aCell.textContent.trim().toLowerCase();bVal=bCell.textContent.trim().toLowerCase()}if(aVal<bVal)return dir==="asc"?-1:1;if(aVal>bVal)return dir==="asc"?1:-1;return 0});var tbody=table.querySelector("tbody")||table;dataRows.forEach(function(r){tbody.appendChild(r)});baselineRows.forEach(function(r){tbody.appendChild(r)})}';
    s += 'var cd=' + JSON.stringify(charts) + ';';
    s += 'var ar=' + JSON.stringify(allRes) + ';';
    s += 'var lo={margin:{t:30,b:40,l:50,r:20},height:430,font:{family:"sans-serif"}};';
    s += 'var co={responsive:true};';
    s += 'function fmtSI(v,d){if(v==null)return"-";d=d!=null?d:1;if(Math.abs(v)>=1e6)return(v/1e6).toFixed(d)+"M";if(Math.abs(v)>=1e3)return(v/1e3).toFixed(d)+"K";return v.toFixed(d)}';
    s += 'function arrAnn(xs,ys,o){o=o||{};var c=o.color||"#333",d=o.decimals!=null?o.decimals:1,sp=o.suffix||"",spr=o.spread||30;var offs=[{ax:0,ay:-spr},{ax:spr*0.9,ay:spr*0.7},{ax:-spr*0.8,ay:-spr*1.2},{ax:spr*1.1,ay:-spr*0.5},{ax:0,ay:spr*1.1},{ax:-spr,ay:spr*0.8},{ax:spr*1.3,ay:-spr*1.3},{ax:-spr*1.2,ay:spr*1.3}];return ys.map(function(v,i){if(v==null)return null;var p=offs[i%offs.length];return{x:xs[i],y:v,xref:"x",yref:o.yref||"y",text:fmtSI(v,d)+sp,showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#94a3b8",ax:p.ax,ay:p.ay,font:{size:10,color:c},borderpad:2}}).filter(Boolean)}';
    s += 'var vl={...lo,margin:{...lo.margin,b:100},barmode:"group",showlegend:true,legend:{x:0,y:1.15,orientation:"h"}};';
    s += 'var pc={p50:"#60a5fa",p90:"#3b82f6",p95:"#f59e0b",p99:"#ef4444"};';

    // Show all panes for initial rendering
    s += 'document.querySelectorAll(".dl-pane").forEach(function(p){p.style.display="block"});';

    // TP calibration charts
    s += 'if(cd.pareto&&cd.pareto.traces){cd.pareto.traces.forEach(function(t){';
    s += '  var tgt=t.name==="Decode"?"tp-dec":"tp-pre";';
    s += '  if(document.getElementById(tgt)){';
    s += '    var tps=t.x.map(function(_,i){return"TP"+t.x[i]});';
    s += '    Plotly.newPlot(tgt,[{x:tps,y:t.y,type:"bar",marker:{color:t.color},hovertext:t.text,hoverinfo:"text",text:t.y.map(function(v){return fmtSI(v)}),textposition:"outside",textfont:{size:11,color:"#1e293b"},cliponaxis:false,constraintext:"none"}],{...lo,title:{text:t.name+" TP Sweep"},yaxis:{title:"TTFT P90 (ms)",tickformat:".2s"}},co);';
    s += '}});}';

    // Pareto + scatter + efficiency + architecture charts
    s += 'if(cd.pareto.traces.length){var pxv=[...new Set(cd.pareto.traces.flatMap(function(t){return t.x}))].sort(function(a,b){return a-b});Plotly.newPlot("p1",cd.pareto.traces.map(function(t){return{x:t.x,y:t.y,text:t.text,name:t.name,mode:"markers+lines",marker:{size:14,color:t.color,symbol:"diamond",line:{width:2,color:"white"}},line:{width:2,dash:"dot"},hovertemplate:"<b>%{text}</b><extra></extra>"}}),{...lo,xaxis:{title:"GPUs",tickvals:pxv},yaxis:{title:"TTFT P90 (ms)"},showlegend:true},co);}';
    s += 'if(cd.scatter.traces.length){Plotly.newPlot("p2",cd.scatter.traces.map(function(t){return{x:t.x,y:t.y,text:t.text,name:t.name,mode:"markers",marker:{size:t.sizes,color:t.color,opacity:0.7,line:{width:1,color:"white"}},hovertemplate:"<b>%{text}</b><extra></extra>"}}),{...lo,xaxis:{title:"TTFT P90 (ms)"},yaxis:{title:"Throughput P90 (req/s)"},showlegend:true},co);}';
    s += 'if(cd.efficiency.configs.length){Plotly.newPlot("p3",[{x:cd.efficiency.configs,y:cd.efficiency.values,type:"bar",marker:{color:cd.efficiency.colors},text:cd.efficiency.values.map(function(v){return v!=null?v.toFixed(3):""}),textposition:"outside",textfont:{size:11,color:"#333"},cliponaxis:false,constraintext:"none"}],{...lo,margin:{...lo.margin,b:120},xaxis:{tickangle:-45},yaxis:{title:"req/s/GPU"}},co);}';
    s += 'if(cd.architecture.architectures.length){var a=cd.architecture;Plotly.newPlot("p4",[{x:a.architectures,y:a.avg_ttft,type:"bar",marker:{color:"#3b82f6"},text:a.avg_ttft.map(function(v){return fmtSI(v)+" ms"}),textposition:"auto",name:"Avg TTFT P90",xaxis:"x",yaxis:"y"},{x:a.architectures,y:a.best_ttft,type:"bar",marker:{color:"#93c5fd"},text:a.best_ttft.map(function(v){return fmtSI(v)+" ms"}),textposition:"auto",name:"Best TTFT P90",xaxis:"x",yaxis:"y"},{x:a.architectures,y:a.avg_throughput,type:"bar",marker:{color:"#f59e0b"},text:a.avg_throughput.map(function(v){return v.toFixed(2)+" req/s"}),textposition:"auto",name:"Avg Throughput P90",xaxis:"x2",yaxis:"y2"}],{...lo,margin:{t:30,b:50,l:60,r:60},barmode:"group",showlegend:true,legend:{x:0,y:1.18,orientation:"h"},xaxis:{domain:[0,0.45]},yaxis:{title:"TTFT (ms)",titlefont:{color:"#3b82f6"},tickformat:".2s"},xaxis2:{domain:[0.55,1],anchor:"y2"},yaxis2:{title:"Throughput (req/s)",anchor:"x2",titlefont:{color:"#f59e0b"}}},co);}';

    // Per-percentile PD charts
    s += 'var pd=ar.filter(function(r){return r.architecture==="PD"});';
    s += 'if(pd.length){';
    s += '  pd.sort(function(a,b){return a.prefill_pods-b.prefill_pods});';
    s += '  var lbls=pd.map(function(r){return r.prefill_pods+"P : "+r.decode_pods+"D"});';
    s += '  var pctls=[{k:"p90",c:"#3b82f6"},{k:"p95",c:"#f59e0b"},{k:"p99",c:"#ef4444"}];';
    s += '  pctls.forEach(function(pctl){';
    s += '    var el=document.getElementById("pd-ttft-"+pctl.k);if(!el)return;';
    s += '    var ttft=pd.map(function(r){return r["ttft_"+pctl.k]});';
    s += '    var tput=pd.map(function(r){return r["throughput_"+pctl.k]||r.throughput_p90});';
    s += '    var best=Math.min.apply(null,ttft.filter(function(v){return v!=null}));';
    s += '    var clrs=ttft.map(function(v){return v===best?"#10b981":pctl.c});';
    s += '    var szs=ttft.map(function(v){return v===best?22:14});';
    s += '    var ttftAnn=arrAnn(lbls,ttft,{color:"#1e40af",decimals:0,suffix:"ms",spread:35});';
    s += '    var eppAnns=[];pd.forEach(function(r,i){if(r.test_id&&r.test_id.indexOf("step11-epp-")===0){eppAnns.push({x:lbls[i],y:ttft[i],yref:"y",text:"<b>EPP TUNED</b>",showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#7c3aed",ax:55,ay:0,font:{size:9,color:"white"},bgcolor:"#7c3aed",borderpad:3,bordercolor:"#7c3aed",borderwidth:1});eppAnns.push({x:lbls[i],y:tput[i],yref:"y2",text:"<b>EPP TUNED</b>",showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#7c3aed",ax:55,ay:0,font:{size:9,color:"white"},bgcolor:"#7c3aed",borderpad:3,bordercolor:"#7c3aed",borderwidth:1})}});';
    s += '    var traces=[{x:lbls,y:ttft,name:"TTFT "+pctl.k.toUpperCase(),type:"scatter",mode:"lines+markers",line:{color:pctl.c,width:3,shape:"spline"},marker:{color:clrs,size:szs,symbol:"circle",line:{width:2,color:"white"}},fill:"tozeroy",fillcolor:pctl.c+"14"},';
    s += '      {x:lbls,y:tput,name:"Throughput "+pctl.k.toUpperCase(),type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3,shape:"spline"},marker:{color:"#f59e0b",size:10,symbol:"diamond",line:{width:2,color:"white"}}}];';
    // Aggregated baseline
    s += '    var aggr=ar.filter(function(r){return r.architecture==="Aggregated"});';
    s += '    if(aggr.length){var ab=aggr[0];var abl=ab["ttft_"+pctl.k];var abt=ab["throughput_"+pctl.k]||ab.throughput_p90;';
    s += '      if(abl!=null){traces.push({x:["Agg Baseline"],y:[abl],name:"Aggregated",type:"scatter",mode:"markers+text",marker:{color:"#94a3b8",size:16,symbol:"star",line:{width:2,color:"white"}},text:[abl.toFixed(0)+"ms"],textposition:"top center",textfont:{size:10,color:"#64748b"},showlegend:true});}';
    s += '      if(abt!=null){traces.push({x:["Agg Baseline"],y:[abt],name:"Agg Throughput",type:"scatter",mode:"markers",yaxis:"y2",marker:{color:"#d4d4d8",size:12,symbol:"star",line:{width:2,color:"white"}},showlegend:false});}}';
    s += '    Plotly.newPlot(el,traces,{...lo,height:500,margin:{t:30,b:80,l:60,r:60},xaxis:{title:"Prefill : Decode Pod Ratio"},yaxis:{title:"TTFT "+pctl.k.toUpperCase()+" (ms)",titlefont:{color:pctl.c},tickfont:{color:pctl.c}},yaxis2:{title:"Throughput (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.18,orientation:"h"},annotations:ttftAnn.concat(eppAnns)},co);';
    s += '  });';
    s += '}';

    // Step 9 latency search charts (per-percentile per-architecture)
    if (data.latency_search && data.latency_search.by_architecture) {
        s += 'var lsData=' + JSON.stringify(data.latency_search) + ';';
        s += 'if(lsData&&lsData.by_architecture){var acfg=lsData.arch_configs||{};';
        s += 'var pctls2=[{k:"p90",c:"#3b82f6"},{k:"p95",c:"#f59e0b"},{k:"p99",c:"#ef4444"}];';
        s += 'Object.keys(lsData.by_architecture).forEach(function(arch,ai){';
        s += '  var trials=lsData.by_architecture[arch];';
        s += '  var tgtMs=trials[0].target_ms;var tgtPct=trials[0].target_percentile||"p90";';
        s += '  var cl=acfg[arch]||arch.toUpperCase();';
        s += '  var st=[].concat(trials).sort(function(a,b){return a.concurrency-b.concurrency});';
        s += '  pctls2.forEach(function(pctl){';
        s += '    var el=document.getElementById("dl-step9-"+pctl.k+"-"+ai);if(!el)return;';
        s += '    var cx=st.map(function(t){return t.concurrency});';
        s += '    var lats=st.map(function(t){return t["ttft_"+pctl.k]});';
        s += '    var tpk="throughput_"+pctl.k;var tps=st.map(function(t){return t[tpk]!=null?t[tpk]:t.throughput_p90});';
        s += '    var mc=st.map(function(t){var v=t["ttft_"+pctl.k];return v!=null&&v<=tgtMs?"#10b981":"#ef4444"});';
        s += '    var latText=lats.map(function(v){return v!=null?v.toFixed(0)+"ms":""});';
        s += '    var tpText=tps.map(function(v){return v!=null?v.toFixed(1):""});';
        // Find best meeting SLA
        s += '    var bestIdx=-1;var bestConc=-1;st.forEach(function(t,i){var v=t["ttft_"+pctl.k];if(v!=null&&v<=tgtMs&&t.concurrency>bestConc){bestConc=t.concurrency;bestIdx=i;}});';
        s += '    var traces=[{x:cx,y:lats,name:"TTFT "+pctl.k.toUpperCase(),type:"scatter",mode:"lines+markers+text",line:{color:pctl.c,width:3},marker:{color:mc,size:12,symbol:"circle",line:{width:2,color:"white"}},text:latText,textposition:"top center",textfont:{size:10,color:pctl.c}},';
        s += '      {x:cx,y:tps,name:"Throughput "+pctl.k.toUpperCase(),type:"scatter",mode:"lines+markers+text",yaxis:"y2",line:{color:"#f59e0b",width:2,dash:"dot"},marker:{color:"#f59e0b",size:8,symbol:"square"},text:tpText,textposition:"bottom center",textfont:{size:9,color:"#f59e0b"}}];';
        s += '    if(bestIdx>=0){traces.push({x:[cx[bestIdx]],y:[lats[bestIdx]],name:"Optimal",type:"scatter",mode:"markers",marker:{color:"#10b981",size:22,symbol:"circle",line:{width:3,color:"white"}},showlegend:true});}';
        s += '    Plotly.newPlot(el,traces,{...lo,height:450,margin:{t:40,b:70,l:60,r:60},title:{text:cl+" — "+pctl.k.toUpperCase()+" Concurrency vs Latency",font:{size:14}},xaxis:{title:"Concurrent Users"},yaxis:{title:"TTFT "+pctl.k.toUpperCase()+" (ms)",side:"left",titlefont:{color:pctl.c}},yaxis2:{title:"Throughput (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.15,orientation:"h"},shapes:[{type:"line",x0:cx[0],x1:cx[cx.length-1],y0:tgtMs,y1:tgtMs,yref:"y",line:{color:"#ef4444",width:pctl.k===tgtPct?2:1.5,dash:"dash"}}],annotations:[{x:cx[cx.length-1],y:tgtMs,yref:"y",text:"SLA: "+tgtMs+"ms",showarrow:false,font:{color:"#ef4444",size:11},xanchor:"right",yanchor:"bottom",yshift:5,bgcolor:"rgba(255,255,255,0.85)"}]},co);';
        s += '  });';
        s += '});}';
    }

    // vLLM charts
    s += 'if(cd.vllm&&cd.vllm.configs.length){var v=cd.vllm;';
    s += 'Plotly.newPlot("v1",[{x:v.configs,y:v.ttft.p50,name:"P50",type:"bar",marker:{color:pc.p50}},{x:v.configs,y:v.ttft.p90,name:"P90",type:"bar",marker:{color:pc.p90}},{x:v.configs,y:v.ttft.p95,name:"P95",type:"bar",marker:{color:pc.p95}},{x:v.configs,y:v.ttft.p99,name:"P99",type:"bar",marker:{color:pc.p99}}],{...vl,title:{text:"TTFT Percentiles"},xaxis:{tickangle:-35},yaxis:{title:"TTFT (ms)"}},co);';
    s += 'Plotly.newPlot("v2",[{x:v.configs,y:v.itl.p50,name:"P50",type:"bar",marker:{color:pc.p50}},{x:v.configs,y:v.itl.p90,name:"P90",type:"bar",marker:{color:pc.p90}},{x:v.configs,y:v.itl.p95,name:"P95",type:"bar",marker:{color:pc.p95}},{x:v.configs,y:v.itl.p99,name:"P99",type:"bar",marker:{color:pc.p99}}],{...vl,title:{text:"ITL Percentiles"},xaxis:{tickangle:-35},yaxis:{title:"ITL (ms)"}},co);';
    s += 'Plotly.newPlot("v3",[{x:v.configs,y:v.e2e.p50,name:"P50",type:"bar",marker:{color:pc.p50}},{x:v.configs,y:v.e2e.p90,name:"P90",type:"bar",marker:{color:pc.p90}},{x:v.configs,y:v.e2e.p95,name:"P95",type:"bar",marker:{color:pc.p95}},{x:v.configs,y:v.e2e.p99,name:"P99",type:"bar",marker:{color:pc.p99}}],{...vl,title:{text:"E2E Latency"},xaxis:{tickangle:-35},yaxis:{title:"E2E (seconds)"}},co);';
    s += 'Plotly.newPlot("v4",[{x:v.configs,y:v.token_rates.prompt,name:"Prompt Tokens/s",type:"bar",marker:{color:"#6366f1"}},{x:v.configs,y:v.token_rates.generation,name:"Generation Tokens/s",type:"bar",marker:{color:"#10b981"}}],{...vl,title:{text:"Token Throughput"},xaxis:{tickangle:-35},yaxis:{title:"Tokens/s"}},co);';
    s += 'Plotly.newPlot("v5",[{x:v.configs,y:v.request_state.running,name:"Running",type:"bar",marker:{color:"#3b82f6"}},{x:v.configs,y:v.request_state.waiting,name:"Waiting",type:"bar",marker:{color:"#ef4444"}},{x:v.configs,y:v.request_state.kv_cache,name:"KV Cache %",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3},marker:{size:10,symbol:"diamond",color:"#f59e0b"}}],{...vl,title:{text:"Request Queue & KV Cache"},margin:{...vl.margin,r:60},xaxis:{tickangle:-35},yaxis:{title:"Count"},yaxis2:{title:"KV Cache %",side:"right",overlaying:"y",range:[0,105]}},co);';
    s += 'Plotly.newPlot("v6",[{x:v.configs,y:v.time_breakdown.prefill,name:"Prefill",type:"bar",marker:{color:"#6366f1"}},{x:v.configs,y:v.time_breakdown.decode,name:"Decode",type:"bar",marker:{color:"#3b82f6"}},{x:v.configs,y:v.time_breakdown.queue,name:"Queue",type:"bar",marker:{color:"#94a3b8"}},{x:v.configs,y:v.time_breakdown.preemptions,name:"Preemptions/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#ef4444",width:3},marker:{size:10,symbol:"triangle-up",color:"#ef4444"}}],{...vl,barmode:"stack",title:{text:"Time Breakdown & Preemptions"},margin:{...vl.margin,r:60},xaxis:{tickangle:-35},yaxis:{title:"Time Rate (s/s)"},yaxis2:{title:"Preemptions/s",side:"right",overlaying:"y"}},co);';
    s += 'if(v.network&&v.network.pod_tx.some(function(x){return x>0})){Plotly.newPlot("v7",[{x:v.configs,y:v.network.pod_tx,name:"TX (MB/s)",type:"bar",marker:{color:"#3b82f6"}},{x:v.configs,y:v.network.pod_rx,name:"RX (MB/s)",type:"bar",marker:{color:"#10b981"}}],{...vl,title:{text:"Pod Network Throughput"},xaxis:{tickangle:-35},yaxis:{title:"MB/s"}},co);}';
    s += 'if(v.network&&v.network.ib_rx.some(function(x){return x>0})){Plotly.newPlot("v8",[{x:v.configs,y:v.network.ib_rx,name:"IB RX (GB/s)",type:"bar",marker:{color:"#8b5cf6"},text:v.network.ib_rx.map(function(x){return x>0?x.toFixed(2):""}),textposition:"outside",textfont:{size:11,color:"#1e293b"},cliponaxis:false,constraintext:"none"}],{...vl,title:{text:"InfiniBand RDMA Throughput"},xaxis:{tickangle:-35},yaxis:{title:"GB/s"}},co);}';
    s += '}';

    // EPP Tuning charts (per-architecture, per-percentile)
    if (data.epp_tuning && data.epp_tuning.by_architecture) {
        s += 'var eppD=' + JSON.stringify(data.epp_tuning) + ';';
        s += 'var pctls3=[{k:"p90",l:"P90",c:"#3b82f6"},{k:"p95",l:"P95",c:"#dc2626"},{k:"p99",l:"P99",c:"#7c3aed"}];';
        s += 'Object.keys(eppD.by_architecture).forEach(function(arch){';
        s += '  var trials=eppD.by_architecture[arch];if(!trials||!trials.length)return;';
        s += '  var tgtMs=eppD.target_ms;';
        s += '  pctls3.forEach(function(pctl){';
        s += '    var el=document.getElementById("dl-epp-"+arch+"-"+pctl.k);if(!el)return;';
        s += '    var xLabels=trials.map(function(t){return t.name});';
        s += '    var lats=trials.map(function(t){return t["ttft_"+pctl.k]});';
        s += '    var tps=trials.map(function(t){return t["throughput_"+pctl.k]||t.throughput_p90});';
        s += '    var bestIdx=lats.indexOf(Math.min.apply(null,lats.filter(function(v){return v!=null})));';
        s += '    var mc=lats.map(function(v){if(tgtMs&&v!=null)return v<=tgtMs?"#10b981":"#ef4444";return pctl.c});';
        s += '    var latText=lats.map(function(v){return v!=null?v.toFixed(0)+"ms":""});';
        s += '    var tpText=tps.map(function(v){return v!=null?v.toFixed(1):""});';
        s += '    var traces=[{x:xLabels,y:lats,name:"TTFT "+pctl.l,type:"scatter",mode:"lines+markers+text",line:{color:pctl.c,width:3,shape:"spline"},marker:{color:mc,size:12,symbol:"circle",line:{width:2,color:"white"}},text:latText,textposition:"top center",textfont:{size:11,color:pctl.c},fill:"tozeroy",fillcolor:pctl.c+"14"},';
        s += '      {x:xLabels,y:tps,name:"Throughput "+pctl.l,type:"scatter",mode:"lines+markers+text",yaxis:"y2",line:{color:"#f59e0b",width:3,shape:"spline"},marker:{color:"#f59e0b",size:10,symbol:"diamond",line:{width:2,color:"white"}},text:tpText,textposition:"bottom center",textfont:{size:10,color:"#f59e0b"}}];';
        s += '    if(bestIdx>=0){traces.push({x:[xLabels[bestIdx]],y:[lats[bestIdx]],name:"Best EPP",type:"scatter",mode:"markers",marker:{color:"#10b981",size:22,symbol:"circle",line:{width:3,color:"white"}},showlegend:true});}';
        // Baseline
        s += '    var bl=(eppD.baselines||{})[arch];';
        s += '    if(bl){var blT=bl["ttft_"+pctl.k];var blP=bl["throughput_"+pctl.k]||bl.throughput_p90;';
        s += '      if(blT!=null){traces.push({x:["Baseline"],y:[blT],name:"Baseline ("+bl.config_name+")",type:"scatter",mode:"markers+text",marker:{color:"#94a3b8",size:18,symbol:"star",line:{width:2,color:"white"}},text:[blT.toFixed(0)+"ms"],textposition:"top center",textfont:{size:11,color:"#64748b"},showlegend:true});}';
        s += '      if(blP!=null){traces.push({x:["Baseline"],y:[blP],name:"Baseline Tput",type:"scatter",mode:"markers",yaxis:"y2",marker:{color:"#d4d4d8",size:14,symbol:"star",line:{width:2,color:"white"}},showlegend:false});}}';
        s += '    var shapes=[];var annotations=[];';
        s += '    if(tgtMs){shapes.push({type:"line",x0:-0.5,x1:xLabels.length-0.5,y0:tgtMs,y1:tgtMs,yref:"y",line:{color:"#ef4444",width:2,dash:"dash"}});annotations.push({x:xLabels.length-1,y:tgtMs,yref:"y",text:"SLA: "+tgtMs+"ms",showarrow:false,font:{color:"#ef4444",size:11},xanchor:"right",yanchor:"bottom",yshift:5,bgcolor:"rgba(255,255,255,0.85)"});}';
        s += '    Plotly.newPlot(el,traces,{...lo,height:400,margin:{t:30,b:80,l:60,r:60},xaxis:{title:"EPP Strategy"},yaxis:{title:"TTFT "+pctl.l+" (ms)",side:"left",titlefont:{color:pctl.c},tickfont:{color:pctl.c}},yaxis2:{title:"Throughput "+pctl.l+" (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.18,orientation:"h"},shapes:shapes,annotations:annotations},co);';
        s += '  });';
        s += '});';
    }

    // Hide non-active panes after rendering
    s += 'setTimeout(function(){document.querySelectorAll(".dl-pane").forEach(function(p){p.style.display="";});},100);';

    s += '<\/script>';
    return s;
}
