// report-download.js — Self-contained HTML report generator for ServeIt Studio
// Extracted from app.js to keep the download report maintainable separately.

async function downloadHTMLReport(runId, data) {
    const charts = data.charts;
    const rec = data.recommendation || {};
    const summary = data.summary;
    const best = summary.best_configs || {};
    const allRes = data.all_results || [];
    const pdResults = allRes.filter(r => r.architecture === 'PD');
    const hasVLLM = charts.vllm && charts.vllm.configs.length;
    const hasPD = pdResults.length > 0;

    // Fetch manifests if not already embedded in data
    const needsManifests = allRes.some(r => r.manifest_types && r.manifest_types.length > 0 && (!r.manifests || !Object.keys(r.manifests).length));
    if (needsManifests) {
        try {
            const resp = await fetch(`/api/runs/${runId}/charts`);
            if (resp.ok) {
                const fresh = await resp.json();
                if (fresh.all_results) {
                    fresh.all_results.forEach(fr => {
                        if (fr.manifests && Object.keys(fr.manifests).length) {
                            const match = allRes.find(r => r.test_id === fr.test_id);
                            if (match) match.manifests = fr.manifests;
                        }
                    });
                }
            }
        } catch(e) { /* offline — manifests won't be available */ }
    }

    const html = buildFullReport(runId, data, charts, rec, summary, best, allRes, hasPD, hasVLLM);

    const blob = new Blob([html], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const rc = data.run_config || {};
    const model = (rc.model_name || '').split('/').pop() || 'model';
    const goal = (rc.objective || 'ttft').toLowerCase();
    const date = new Date().toISOString().slice(0, 10);
    a.download = `serveit-${model}-${goal}-run${runId}-${date}.html`;
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
    const secSweep = buildConcurrencySweepSection(data);
    const secCacheSweep = buildCacheSweepSection(data);
    const secTraffic = buildTrafficSection(data, allRes);
    const secDeploy = buildDeployTimingSection(data, allRes);
    const secPareto = buildParetoFrontierSection(data, allRes);

    const dlTabs = [];
    if (secRec) dlTabs.push({ id: 'rec', label: '&#9733; Recommendation', html: secRec });
    if (secTP) dlTabs.push({ id: 'tp', label: '&#9881; TP Calibration', html: secTP });
    if (secCfg) dlTabs.push({ id: 'cfg', label: '&#9776; Configurations', html: secCfg });
    if (secCmp) dlTabs.push({ id: 'cmp', label: '&#8596; Comparison', html: secCmp });
    if (secStep9) dlTabs.push({ id: 'step9', label: '&#128269; Latency Search', html: secStep9 });
    if (secCal) dlTabs.push({ id: 'cal', label: '&#9878; Calibrated Load', html: secCal });
    if (secSweep) dlTabs.push({ id: 'sweep', label: '&#128200; Concurrency Sweep', html: secSweep });
    if (secCacheSweep) dlTabs.push({ id: 'cachesweep', label: '&#128203; Cache Sweep', html: secCacheSweep });
    if (secVLLM) dlTabs.push({ id: 'vllm', label: '&#9889; vLLM Metrics', html: secVLLM });
    if (secEpp) dlTabs.push({ id: 'epp', label: '&#9881; EPP Tuning', html: secEpp });
    if (secPareto) dlTabs.push({ id: 'pareto', label: '&#128200; Pareto Frontier', html: secPareto });
    if (secTraffic) dlTabs.push({ id: 'traffic', label: '&#128230; Traffic', html: secTraffic });
    if (secDeploy) dlTabs.push({ id: 'deploy', label: '&#9202; Deploy Timing', html: secDeploy });
    if (secTestCfg) dlTabs.push({ id: 'settings', label: '&#9881; Test Settings', html: secTestCfg });

    let out = buildHead(runId);
    out += `<h1>ServeIt Studio Optimization Report &mdash; Run #${runId}</h1>`;
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
    return `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>ServeIt Studio Report - Run ${runId}</title>
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
        s += `<div style="background:${gc};color:white;padding:14px 20px;font-size:1.3em;font-weight:800;">Deployment Recommendation <span style="font-size:0.7em;font-weight:400;opacity:0.85;">&mdash; ${rec.goal_info.name}</span></div>`;
        s += `<div style="background:${gc}dd;color:white;padding:8px 20px;font-size:0.92em;">`;
        const rc2 = data.run_config || {};
        const wCpt = rec.workload.chars_per_token || rc2.chars_per_token || 4.5;
        const wIslC = rec.workload.isl_original_chars || rc2.isl_original_chars || Math.round(rec.workload.isl * wCpt);
        const wOslC = rec.workload.osl_original_chars || rc2.osl_original_chars || Math.round(rec.workload.osl * wCpt);
        const wIslStdC = rec.workload.isl_stdev ? (rec.workload.isl_stdev_original_chars || (rc2.isl_stdev_original_chars) || Math.round(rec.workload.isl_stdev * wCpt)) : null;
        s += `Model: <strong>${rec.model}</strong> &nbsp;|&nbsp; Prompt: <strong>${wIslC.toLocaleString()} chars</strong>`;
        if (wIslStdC) s += ` (&sigma;=${wIslStdC.toLocaleString()})`;
        s += ` | Output: <strong>${wOslC.toLocaleString()} chars</strong>`;
        if (rec.workload.osl_stdev) s += ` (&sigma;=${Math.round(rec.workload.osl_stdev * wCpt).toLocaleString()})`;
        if (rec.workload.turns && rec.workload.turns > 1) s += ` | Turns: <strong>${rec.workload.turns}</strong>`;
        s += ` &nbsp;|&nbsp; Users: <strong>${rec.workload.users}</strong> &nbsp;|&nbsp; Tests: <strong>${rec.total_tests}</strong>`;
        if (rec.total_duration) s += ` &nbsp;|&nbsp; Duration: <strong>${rec.total_duration}</strong>`;
        s += '</div>';
        s += `<div style="padding:20px;"><p style="color:#334155;margin:0;font-size:0.95em;line-height:1.6;">${rec.goal_info.description}</p></div></div>`;
    }

    // Recommendation cards — same design as live UI
    if (rec.best_by_percentile && Object.keys(rec.best_by_percentile).length) {
        const bp = rec.best_by_percentile;
        const selTypes = [
            { key: 'balanced', label: 'Best Balanced', desc: 'Best TTFT-to-throughput ratio — the sweet spot', color: '#059669', icon: '&#9878;' },
            { key: 'lowest_ttft', label: 'Lowest TTFT', desc: 'Fastest time to first token', color: '#3b82f6', icon: '&#9201;' },
            { key: 'highest_tput', label: 'Highest Throughput', desc: 'Maximum requests per second', color: '#f59e0b', icon: '&#9889;' },
            { key: 'most_efficient', label: 'Most Efficient', desc: 'Best throughput per GPU — cost optimized', color: '#8b5cf6', icon: '&#128176;' },
        ];
        const archColors = { pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed' };

        // Find global best per category across all architectures
        const globalBest = {};
        selTypes.forEach(sel => {
            let best = null;
            ['pd', 'aggregated', 'ep'].forEach(archKey => {
                const p90Data = (bp.p90 || {})[archKey];
                if (!p90Data) return;
                const cfg = p90Data[sel.key] || (sel.key === 'balanced' ? p90Data : null);
                if (!cfg) return;
                const ttft = cfg.ttft_p90 || cfg.ttft || 1e9;
                const tput = cfg.throughput_mean || cfg.throughput || cfg.throughput_p90 || 0;
                const gpus = cfg.gpus || cfg.total_gpus || 1;
                let score;
                if (sel.key === 'lowest_ttft') score = -ttft;
                else if (sel.key === 'highest_tput') score = tput;
                else if (sel.key === 'most_efficient') score = tput / gpus;
                else score = -ttft / Math.max(tput, 0.001);
                if (!best || score > best.score) best = { cfg, score, archKey };
            });
            if (best) globalBest[sel.key] = best;
        });

        s += '<div style="border:2px solid #10b981;border-left:6px solid #10b981;border-radius:10px;margin:20px 0;overflow:hidden;">';
        s += '<div style="background:linear-gradient(135deg,#059669,#10b981);padding:14px 20px;font-size:1.1em;font-weight:800;color:white;">Deployment Recommendation &mdash; User-Defined Workload</div>';
        s += '<div style="padding:12px 20px 4px;color:#475569;font-size:0.9em;">Best configurations found at the user-configured concurrency. These results reflect peak-load performance at the workload settings you defined.</div>';
        s += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;padding:20px;">';

        selTypes.forEach(sel => {
            const entry = globalBest[sel.key];
            if (!entry) return;
            const cfg = entry.cfg;
            const archKey = entry.archKey;
            const aColor = archColors[archKey] || '#64748b';
            const archLabel = archKey.toUpperCase();

            let deploy;
            if (cfg.prefill_pods && cfg.decode_pods) {
                deploy = cfg.prefill_tp === cfg.decode_tp
                    ? `${cfg.prefill_pods}P+${cfg.decode_pods}D TP=${cfg.prefill_tp || cfg.tp || '?'}`
                    : `${cfg.prefill_pods}P+${cfg.decode_pods}D PTP=${cfg.prefill_tp || '?'} DTP=${cfg.decode_tp || '?'}`;
            } else {
                deploy = cfg.config_name || '';
            }

            const tput = cfg.throughput_mean || cfg.throughput || cfg.throughput_p90 || '-';
            const gpus = cfg.gpus || cfg.total_gpus || '?';

            const _p90d = ((bp.p90 || {})[archKey] || {})[sel.key] || (bp.p90 || {})[archKey] || {};
            const _p95d = ((bp.p95 || {})[archKey] || {})[sel.key] || (bp.p95 || {})[archKey] || {};
            const _p99d = ((bp.p99 || {})[archKey] || {})[sel.key] || (bp.p99 || {})[archKey] || {};

            // Card
            s += '<div style="background:white;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);border:2px solid #cbd5e1;">';
            // Header
            s += `<div style="background:linear-gradient(135deg,${sel.color},${sel.color}cc);padding:10px 14px;color:white;">`;
            s += `<div style="font-weight:700;font-size:0.85em;">${sel.icon} ${sel.label}</div>`;
            s += `<div style="display:flex;gap:4px;margin-top:2px;"><div style="font-size:0.65em;background:${aColor};padding:2px 6px;border-radius:10px;font-weight:600;">${archLabel}</div></div>`;
            if (sel.desc) s += `<div style="font-size:0.7em;opacity:0.9;margin-top:1px;">${sel.desc}</div>`;
            s += '</div>';
            // Body
            s += '<div style="padding:12px 14px;">';
            s += `<div style="font-size:1.1em;font-weight:800;color:#0f172a;margin-bottom:8px;">${deploy}</div>`;
            s += '<div style="display:flex;gap:12px;font-size:0.85em;color:#475569;margin-bottom:4px;">';
            s += `<span>Throughput: <strong>${typeof tput === 'number' ? tput.toFixed(2) + ' req/s' : tput}</strong></span>`;
            s += `<span>GPUs: <strong>${gpus}</strong></span>`;
            s += '</div>';
            // Percentile table
            s += '<table style="width:100%;font-size:0.8em;border-collapse:collapse;margin-top:6px;">';
            s += '<tr style="color:#94a3b8;font-weight:600;"><td></td><td>TTFT</td><td>E2E</td><td>ITL</td></tr>';
            const t90 = _p90d.ttft || _p90d.ttft_p90;
            const t95 = _p95d.ttft || _p95d.ttft_p95;
            const t99 = _p99d.ttft || _p99d.ttft_p99;
            const fmtE2e = v => v != null ? (v >= 1000 ? (v/1000).toFixed(1) + ' s' : Math.round(v) + ' ms') : '-';
            s += `<tr><td style="font-weight:600;color:#475569;">P90</td><td style="font-weight:700;color:#1e293b;">${t90 != null ? Math.round(t90).toLocaleString() + ' ms' : '-'}</td><td>${fmtE2e(_p90d.e2e_p90)}</td><td>${_p90d.itl || _p90d.itl_p90 ? (_p90d.itl || _p90d.itl_p90) + ' ms' : '-'}</td></tr>`;
            if (t95) s += `<tr><td style="font-weight:600;color:#475569;">P95</td><td style="color:#64748b;">${Math.round(t95).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(_p95d.e2e_p95)}</td><td>${_p95d.itl || _p95d.itl_p95 ? (_p95d.itl || _p95d.itl_p95) + ' ms' : '-'}</td></tr>`;
            if (t99) s += `<tr><td style="font-weight:600;color:#475569;">P99</td><td style="color:#64748b;">${Math.round(t99).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(_p99d.e2e_p99)}</td><td>${_p99d.itl || _p99d.itl_p99 ? (_p99d.itl || _p99d.itl_p99) + ' ms' : '-'}</td></tr>`;
            s += '</table>';
            // Manifest links
            const mTypes = cfg.manifest_types || [];
            if (mTypes.length) {
                const ml = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
                s += '<div style="margin-top:10px;padding-top:8px;border-top:1px solid #f1f5f9;display:flex;flex-wrap:wrap;gap:4px;">';
                mTypes.filter(t => !t.includes('service')).forEach(t => {
                    const tid = cfg.test_id || cfg.config_name || '';
                    s += `<a href="#" onclick="dlManifest('${tid}','${t}');return false;" style="color:#0ea5e9;font-size:10px;padding:2px 6px;background:#f0f9ff;border-radius:4px;border:1px solid #bae6fd;font-weight:500;text-decoration:none;cursor:pointer;">${ml[t] || t} &#8595;</a>`;
                });
                s += '</div>';
            }
            s += '</div></div>';
        });

        s += '</div>';

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
        // Constraint notes filtered out — not useful in downloaded report

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
                        const eppTputMean = b.throughput_mean || b.throughput_p90;
                        s += `<div style="font-size:0.9em;color:#475569;">TTFT ${pLabel}: <strong>${b[`ttft_${p}`]} ms</strong> | Throughput Mean: <strong>${eppTputMean} req/s</strong>${concStr}</div>`;
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

    // Cache hit rate chart
    const cacheData = allRes.filter(r => r.cache_hit_pct != null);
    if (cacheData.length) {
        s += '<div class="chart-box"><h3>Prefix Cache Hit Rate per Configuration</h3>';
        s += '<p style="color:#64748b;font-size:0.9em;margin:0 0 8px;">Actual prefix cache hit percentage measured by vLLM during each test.</p>';
        s += '<div id="dl-cfg-cache-hit" style="height:400px"></div></div>';
    }

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

    // All results table (exclude step2/step3 calibration tests)
    const coreRes = allRes.filter(r => {
        const tid = r.test_id || '';
        return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0;
    });
    if (coreRes.length) {
        const pn = new Set(charts.pareto.pareto_table.map(p => p.config_name));
        const tid = 'dl-all-configs';
        s += `<div class="chart-box"><h3>All Successful Configurations</h3>`;
        s += `<p style="color:#64748b;font-size:0.9em;margin:0 0 8px;">Complete results from every test that ran successfully. <strong style="color:#059669;">Green highlighted rows</strong> are Pareto optimal.</p>`;
        s += `<table id="${tid}"><tr>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',0,'str')">Configuration &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',1,'str')">Architecture &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',2,'num')">TTFT P90 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',3,'num')">TTFT P95 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',4,'num')">TTFT P99 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',5,'num')">Tput Mean &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',6,'num')">ITL P90 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',7,'num')">GPUs &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',8,'num')">Efficiency &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th>`;
        s += `<th>Manifests</th>`;
        s += '</tr>';
        const mlabels = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
        coreRes.forEach(r => {
            const cls = pn.has(r.config_name) ? ' class="pareto"' : '';
            const na = 'N/A';
            const eppBadge = (r.test_id && r.test_id.startsWith('step11-epp-')) ? ' <span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
            let manifestLinks = '-';
            if (r.manifest_types && r.manifest_types.length > 0) {
                manifestLinks = r.manifest_types.filter(t => !t.includes('service')).map(t => {
                    return `<a href="#" onclick="dlManifest('${r.test_id}','${t}');return false;" title="Download ${t}.yaml" style="color:#0ea5e9;text-decoration:none;font-size:11px;padding:2px 6px;background:#f0f9ff;border-radius:4px;border:1px solid #bae6fd;display:inline-block;margin:1px;cursor:pointer;">${mlabels[t] || t} &#8595;</a>`;
                }).join(' ');
            }
            const tputMean = r.throughput_mean ?? r.throughput_p90 ?? na;
            s += `<tr${cls}><td>${r.config_name}${eppBadge}</td><td>${r.architecture}</td><td data-val="${r.ttft_p90}">${r.ttft_p90}</td><td data-val="${r.ttft_p95 ?? ''}">${r.ttft_p95 ?? na}</td><td data-val="${r.ttft_p99 ?? ''}">${r.ttft_p99 ?? na}</td><td data-val="${tputMean}">${tputMean}</td><td data-val="${r.itl_p90 ?? ''}">${r.itl_p90 ?? na}</td><td data-val="${r.gpus}">${r.gpus}</td><td data-val="${r.efficiency}">${r.efficiency}</td><td>${manifestLinks}</td></tr>`;
        });
        s += '</table></div>';
    }

    return s;
}

// ── Comparison Tab (Step 8) ─────────────────────────────────────────────────
function buildCmpSection(runId, rec, data) {
    if (!rec || (!rec.pd_vs_agg && !rec.ep_vs_agg)) return '';
    let s = '';

    // Architecture comparison charts
    s += '<div class="grid2">';
    s += '<div class="chart-box"><h3>Architecture Comparison</h3><div id="dl-chart-arch" style="height:430px"></div></div>';
    s += '<div class="chart-box"><h3>Percentile Comparison: Winner vs Aggregated</h3><div id="dl-chart-pctile" style="height:430px"></div></div>';
    s += '</div>';

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

    // Calibrated Recommendation cards (same design as Deployment Recommendation)
    const calBest = data.summary && data.summary.calibrated_best;
    if (calBest) {
        const bp = data.recommendation ? data.recommendation.best_by_percentile || {} : {};
        const calTypes = [
            { key: 'balanced', label: 'Best Balanced', desc: 'Best TTFT-to-throughput ratio at calibrated load', color: '#0ea5e9', icon: '&#9878;' },
            { key: 'lowest_ttft', label: 'Lowest TTFT', desc: 'Fastest first token at calibrated load', color: '#3b82f6', icon: '&#9201;' },
            { key: 'highest_tput', label: 'Highest Throughput', desc: 'Maximum req/s at calibrated load', color: '#f59e0b', icon: '&#9889;' },
            { key: 'most_efficient', label: 'Most Efficient', desc: 'Best throughput per GPU at calibrated load', color: '#8b5cf6', icon: '&#128176;' },
        ];
        const archColors = { pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed' };
        const calSeen = new Set();

        s += '<div style="border:2px solid #0ea5e9;border-left:6px solid #0ea5e9;border-radius:10px;margin:0 0 20px;overflow:hidden;">';
        s += '<div style="background:linear-gradient(135deg,#0ea5e9,#06b6d4);padding:14px 20px;font-size:1.1em;font-weight:800;color:white;">Deployment Recommendation &mdash; Calibrated Load</div>';
        s += '<div style="padding:12px 20px 4px;color:#475569;font-size:0.9em;">Performance at sustainable production load — calibrated concurrency where queue wait is reasonable. These results reflect realistic production conditions.</div>';
        s += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;padding:16px 20px;">';

        calTypes.forEach(sel => {
            const cfg = calBest[sel.key];
            if (!cfg) return;
            const calId = cfg.test_id || cfg.config_name || '';
            let dupNote = '';
            if (calSeen.has(calId)) {
                const others = calTypes.filter(s2 => s2.key !== sel.key && calBest[s2.key] && (calBest[s2.key].test_id || calBest[s2.key].config_name) === calId).map(s2 => s2.label);
                if (others.length) dupNote = others.join(', ');
                else return;
            }
            calSeen.add(calId);

            const archKey = cfg.architecture || 'aggregated';
            const aColor = archColors[archKey] || '#64748b';
            let deploy;
            if (cfg.prefill_pods && cfg.decode_pods) {
                deploy = cfg.prefill_tp === cfg.decode_tp
                    ? `${cfg.prefill_pods}P+${cfg.decode_pods}D TP=${cfg.prefill_tp || cfg.tp || '?'}`
                    : `${cfg.prefill_pods}P+${cfg.decode_pods}D PTP=${cfg.prefill_tp || '?'} DTP=${cfg.decode_tp || '?'}`;
            } else {
                deploy = cfg.name || cfg.config_name || '';
            }
            const tput = cfg.throughput_mean || cfg.throughput_p90 || '-';
            const concStr = cfg.concurrency ? `c=${cfg.concurrency}` : '';
            const fmtE2e = v => v != null ? (v >= 1000 ? (v/1000).toFixed(1) + ' s' : Math.round(v) + ' ms') : '-';

            s += '<div style="background:white;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);border:2px solid #cbd5e1;">';
            s += `<div style="background:linear-gradient(135deg,${sel.color},${sel.color}cc);padding:10px 14px;color:white;">`;
            s += `<div style="font-weight:700;font-size:0.85em;">${sel.icon} ${sel.label}</div>`;
            s += `<div style="display:flex;gap:4px;margin-top:2px;"><div style="font-size:0.65em;background:${aColor};padding:2px 6px;border-radius:10px;font-weight:600;">${archKey.toUpperCase()}</div>`;
            if (dupNote) s += `<div style="font-size:0.65em;background:rgba(255,255,255,0.2);padding:2px 6px;border-radius:10px;">+ ${dupNote}</div>`;
            s += '</div>';
            if (sel.desc) s += `<div style="font-size:0.7em;opacity:0.9;margin-top:1px;">${sel.desc}</div>`;
            s += '</div>';
            s += '<div style="padding:12px 14px;">';
            s += `<div style="font-size:1.1em;font-weight:800;color:#0f172a;margin-bottom:8px;">${deploy}</div>`;
            s += '<div style="display:flex;gap:12px;font-size:0.85em;color:#475569;margin-bottom:4px;">';
            s += `<span>Throughput: <strong>${typeof tput === 'number' ? tput.toFixed(2) + ' req/s' : tput}</strong></span>`;
            s += `<span>GPUs: <strong>${cfg.gpus || '?'}</strong></span>`;
            if (concStr) s += `<span style="color:#0ea5e9;font-weight:600;">${concStr}</span>`;
            s += '</div>';
            s += '<table style="width:100%;font-size:0.8em;border-collapse:collapse;margin-top:6px;">';
            s += '<tr style="color:#94a3b8;font-weight:600;"><td></td><td>TTFT</td><td>E2E</td><td>ITL</td></tr>';
            s += `<tr><td style="font-weight:600;color:#475569;">P90</td><td style="font-weight:700;color:#1e293b;">${cfg.ttft_p90 != null ? Math.round(cfg.ttft_p90).toLocaleString() + ' ms' : '-'}</td><td>${fmtE2e(cfg.e2e_p90)}</td><td>${cfg.itl_p90 ? cfg.itl_p90 + ' ms' : '-'}</td></tr>`;
            if (cfg.ttft_p95) s += `<tr><td style="font-weight:600;color:#475569;">P95</td><td style="color:#64748b;">${Math.round(cfg.ttft_p95).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(cfg.e2e_p95)}</td><td>${cfg.itl_p95 ? cfg.itl_p95 + ' ms' : '-'}</td></tr>`;
            if (cfg.ttft_p99) s += `<tr><td style="font-weight:600;color:#475569;">P99</td><td style="color:#64748b;">${Math.round(cfg.ttft_p99).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(cfg.e2e_p99)}</td><td>${cfg.itl_p99 ? cfg.itl_p99 + ' ms' : '-'}</td></tr>`;
            s += '</table>';
            s += '</div></div>';
        });

        s += '</div></div>';
    }

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
    if (cal.epp_pd) entries.push({ label: 'PD (EPP Tuned)', entry: cal.epp_pd });
    if (cal.epp_agg) entries.push({ label: 'Aggregated (EPP Tuned)', entry: cal.epp_agg });

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
    // Network charts moved to Traffic tab
    return s;
}

// ── EPP Tuning Tab (Step 9) ────────────────────────────────────────────────
function buildEppTuningSection(runId, data) {
    if (!data.epp_tuning) return '';
    const eppData = data.epp_tuning;
    const archKeys = Object.keys(eppData.by_architecture || {});
    const hasTrials = archKeys.some(k => (eppData.by_architecture[k] || []).length > 0);
    const hasSkipped = eppData.skipped_architectures && eppData.skipped_architectures.length > 0;
    if (!hasTrials && !hasSkipped) return '';

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

    // Skipped architectures
    if (eppData.skipped_architectures && eppData.skipped_architectures.length) {
        const reasonLabels = {
            'weights_match': { title: 'EPP tuning skipped — preset is optimal', desc: 'Smart weight derivation analyzed the measured Prometheus metrics (cache hit rate, KV pressure, queue depth) and concluded the current EPP preset weights are already optimal. No test needed.' },
            'single_pod': { title: 'EPP tuning skipped — single pod', desc: 'This architecture uses a single inference pod. EPP routing optimization requires multiple pods to balance requests across.' },
            'few_pods': { title: 'EPP tuning skipped — too few pods', desc: 'This architecture has 3 or fewer pods. Smart weight derivation adds noise with so few routing targets — using the user preset as-is.' },
            'no_metrics': { title: 'EPP tuning skipped — no metrics', desc: 'No Prometheus metrics were available from the Step 6/7 tests for this architecture. Smart weight derivation requires measured data.' },
            'not_tested': { title: 'EPP tuning not run', desc: 'This architecture was not tested during EPP tuning.' },
        };
        eppData.skipped_architectures.forEach(skip => {
            const archLabel = (skip.arch || '').toUpperCase();
            const info = reasonLabels[skip.reason] || reasonLabels['not_tested'];
            s += `<div class="chart-box" style="border-left:4px solid #7c3aed;">`;
            s += `<h3 style="color:#7c3aed;">Step 9: EPP Tuning &mdash; ${archLabel}</h3>`;
            s += `<div style="background:#f0fdf4;border-radius:10px;padding:16px;border:1px solid #bbf7d0;">`;
            s += `<div style="font-weight:700;color:#059669;">&#10004; ${info.title}</div>`;
            s += `<div style="color:#475569;font-size:0.9em;margin-top:4px;">${info.desc}</div>`;
            s += '</div></div>';
        });
    }

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
    const cpt = rc.chars_per_token || 4.5;
    let islChars, oslChars, islStdevC, oslStdevC;
    if (rc.length_unit === 'characters' && rc.isl_original_chars) {
        islChars = rc.isl_original_chars;
        oslChars = rc.osl_original_chars;
        islStdevC = rc.isl_stdev_original_chars;
        oslStdevC = rc.osl_stdev_original_chars;
    } else {
        islChars = Math.round(rc.isl * cpt);
        oslChars = Math.round(rc.osl * cpt);
        islStdevC = rc.isl_stdev ? Math.round(rc.isl_stdev * cpt) : null;
        oslStdevC = rc.osl_stdev ? Math.round(rc.osl_stdev * cpt) : null;
    }
    s += `<div><span style="color:#64748b;">Prompt Length:</span> ${islChars.toLocaleString()} characters${islStdevC ? ' (&sigma;=' + islStdevC.toLocaleString() + ')' : ''}</div>`;
    s += `<div><span style="color:#64748b;">Prompt Output:</span> ${oslChars.toLocaleString()} characters${oslStdevC ? ' (&sigma;=' + oslStdevC.toLocaleString() + ')' : ''}</div>`;
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
    s += `<div><span style="color:#64748b;">TP Pair Breadth:</span> Top-${rc.tp_pair_top_n || 4}</div>`;
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
    if (rc.scheduler_image) s += `<div><span style="color:#64748b;">Scheduler Image:</span> <span style="word-break:break-all;font-size:0.9em;">${rc.scheduler_image}</span></div>`;
    s += `<div><span style="color:#64748b;">Namespace:</span> ${rc.namespace || na}</div>`;
    s += `<div><span style="color:#64748b;">PVC:</span> ${rc.pvc_name || na}</div>`;
    s += `<div><span style="color:#64748b;">Network Type:</span> ${rc.network_type || na}</div>`;
    s += `<div><span style="color:#64748b;">NCCL IB HCA:</span> ${rc.nccl_ib_hca || na}</div>`;
    if (rc.rdma_nics_per_node) s += `<div><span style="color:#64748b;">RDMA NICs/Node:</span> ${rc.rdma_nics_per_node}</div>`;
    s += '</div>';

    // Component Versions
    if (data.infra_versions && Object.keys(data.infra_versions).length > 0) {
        const iv = data.infra_versions;
        const vLabels = { openshift: 'OpenShift', k8s: 'Kubernetes', gpu_operator: 'GPU Operator', gpu_driver: 'GPU Driver', cuda_runtime: 'CUDA Runtime', network_operator: 'Network Operator', mofed: 'MOFED/DOCA', istio: 'Istio', service_mesh: 'Service Mesh', epp: 'EPP Scheduler', nfd: 'NFD', lws: 'LWS' };
        s += '<div class="section-hdr" style="border-bottom:2px solid #059669;">Component Versions</div><div style="line-height:2.2;margin-bottom:20px;">';
        Object.keys(iv).forEach(k => {
            if (iv[k]) s += `<div><span style="color:#64748b;">${vLabels[k] || k}:</span> ${iv[k]}</div>`;
        });
        s += '</div>';
    }

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

    // Tuned Settings vs Upstream Defaults (full version matching live report)
    const allResults = data.all_results || [];
    const archConfigs = {};
    const archOrder = [];
    for (const r of allResults) {
        const arch = (r.architecture || '').toUpperCase();
        if (archConfigs[arch]) continue;
        let tc = r.test_config;
        if (!tc) continue;
        if (typeof tc === 'string') try { tc = JSON.parse(tc); } catch(e) { continue; }
        if (tc && typeof tc === 'object') { archConfigs[arch] = tc; archOrder.push(arch); }
    }

    if (archOrder.length > 0) {
        s += '<div class="chart-box"><h3>Tuned Settings vs Upstream Defaults</h3>';
        s += '<p style="font-size:0.9em;color:#475569;"><span style="color:#059669;font-weight:600;">Green</span> = auto-tuned by ServeIt Studio. <span style="color:#94a3b8;">Gray</span> = upstream default.</p>';

        const hasEp = archOrder.includes('EP');
        const naS = '<span style="color:#cbd5e1;">N/A</span>';

        const _eppPresets = {
            balanced: {prefix_cache_weight:3, kv_cache_weight:2, queue_weight:2, active_request_weight:2, decode_prefix_cache_weight:1, decode_active_request_weight:3},
            cache_optimized: {prefix_cache_weight:5, kv_cache_weight:1, queue_weight:2, active_request_weight:1, decode_prefix_cache_weight:1, decode_active_request_weight:3},
            queue_balanced: {prefix_cache_weight:1, kv_cache_weight:1, queue_weight:3, active_request_weight:3, decode_prefix_cache_weight:1, decode_active_request_weight:3},
            latency_aware: {prefix_cache_weight:3, kv_cache_weight:2, queue_weight:2, active_request_weight:2, decode_prefix_cache_weight:1, decode_active_request_weight:3},
        };
        function _eppW(tc) {
            if (!tc.epp_config) return null;
            const preset = tc.epp_config.preset || rc.epp_preset || 'balanced';
            return _eppPresets[preset] || _eppPresets.balanced;
        }

        function getVal(tc, key) { return tc[key] != null ? String(tc[key]) : null; }
        function pdVal(tc, pKey, dKey) {
            const p = tc[pKey], d = tc[dKey];
            if (p != null && d != null && p !== d) return `P=${p} D=${d}`;
            return p != null ? String(p) : (d != null ? String(d) : null);
        }
        function boolVal(tc, key) { return tc[key] === true ? 'On' : tc[key] === false ? 'Off' : null; }

        const sections = [
            { title: 'Memory & Batching', params: [
                { label: 'gpu-memory-utilization', def: '0.90', get: tc => pdVal(tc, 'prefill_gpu_memory_utilization', 'decode_gpu_memory_utilization') || getVal(tc, 'gpu_memory_utilization') },
                { label: 'max-model-len', def: 'auto', get: tc => getVal(tc, 'max_model_len') },
                { label: 'max-num-seqs', def: '256', get: tc => pdVal(tc, 'prefill_max_num_seqs', 'decode_max_num_seqs') || getVal(tc, 'max_num_seqs') },
                { label: 'max-num-batched-tokens', def: 'auto', get: tc => getVal(tc, 'max_num_batched_tokens') },
                { label: 'block-size', def: '16', get: tc => getVal(tc, 'block_size') },
                { label: 'kv-cache-memory-bytes', def: 'auto', get: tc => getVal(tc, 'kv_cache_memory_bytes'), pd_only: true },
            ]},
            { title: 'Precision & Compute', params: [
                { label: 'dtype', def: 'auto', get: tc => getVal(tc, 'dtype') },
                { label: 'kv-cache-dtype', def: 'auto', get: tc => getVal(tc, 'kv_cache_dtype') },
                { label: 'pipeline-parallel-size', def: '1', get: tc => getVal(tc, 'pipeline_parallel_size') },
            ]},
            { title: 'Feature Flags', params: [
                { label: 'enable-prefix-caching', def: 'Off', get: tc => boolVal(tc, 'enable_prefix_caching') },
                { label: 'enable-expert-parallel', def: 'Off', get: tc => boolVal(tc, 'enable_expert_parallel') },
                { label: 'enable-dbo', def: 'Off', get: tc => boolVal(tc, 'enable_dbo') },
                { label: 'enable-eplb', def: 'Off', get: tc => boolVal(tc, 'enable_eplb') },
                { label: 'trust-remote-code', def: 'Off', get: tc => boolVal(tc, 'trust_remote_code') },
            ]},
            { title: 'MoE / Expert Parallel', params: [
                { label: 'moe-dp-chunk-size', def: '256', get: tc => getVal(tc, 'moe_dp_chunk_size'), ep_only: true },
                { label: 'all2all-backend', def: 'auto', get: tc => getVal(tc, 'all2all_backend') || (tc.decode_all2all_backend ? `HT / ${tc.decode_all2all_backend}` : null), ep_only: true },
                { label: 'moe-backend', def: 'auto', get: tc => getVal(tc, 'moe_backend'), ep_only: true },
                { label: 'use-deep-gemm', def: 'auto', get: tc => tc.use_deep_gemm === true ? 'On' : tc.use_deep_gemm === false ? 'Off' : null },
                { label: 'dbo-prefill-threshold', def: '32', get: tc => getVal(tc, 'dbo_prefill_token_threshold') },
                { label: 'dbo-decode-threshold', def: '32', get: tc => getVal(tc, 'dbo_decode_token_threshold') },
                { label: 'num-redundant-experts', def: '32', get: tc => getVal(tc, 'num_redundant_experts'), ep_only: true },
                { label: 'NVSHMEM_SYMMETRIC_SIZE', def: '16G', get: tc => getVal(tc, 'nvshmem_symmetric_size'), ep_only: true },
            ]},
            { title: 'EPP Prefill Routing Weights', params: [
                { label: 'prefix-cache-weight', def: '3', get: tc => { const w = _eppW(tc); return w ? String(w.prefix_cache_weight) : null; } },
                { label: 'kv-cache-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.kv_cache_weight) : null; } },
                { label: 'queue-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.queue_weight) : null; } },
                { label: 'active-request-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.active_request_weight) : null; } },
            ]},
            { title: 'EPP Decode Routing Weights', params: [
                { label: 'decode-prefix-cache-weight', def: '3', get: tc => { const w = _eppW(tc); return w ? String(w.decode_prefix_cache_weight) : null; } },
                { label: 'decode-active-request-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.decode_active_request_weight) : null; } },
            ]},
            { title: 'EPP Auto-Calculated', params: [
                { label: 'maxPrefixBlocksToMatch', def: 'auto', get: tc => tc.epp_config ? String(tc.epp_config.max_prefix_blocks || tc.epp_config.maxPrefixBlocksToMatch || '-') : null },
                { label: 'lruCapacityPerServer', def: 'auto', get: tc => tc.epp_config ? String(tc.epp_config.lru_capacity || tc.epp_config.lruCapacityPerServer || '-') : null },
                { label: 'nonCachedTokens', def: '16', get: tc => tc.epp_config ? String(tc.epp_config.non_cached_tokens || tc.epp_config.nonCachedTokens || '-') : null },
            ]},
        ];

        s += '<table style="table-layout:fixed;width:100%;"><tr><th style="text-align:center;width:220px;">Parameter</th><th style="text-align:center;width:100px;">Default</th>';
        archOrder.forEach(arch => {
            const color = arch === 'AGGREGATED' ? '#6366f1' : arch === 'PD' ? '#0ea5e9' : '#10b981';
            s += `<th style="color:${color};text-align:center;">${arch}</th>`;
        });
        s += '</tr>';

        sections.forEach(section => {
            s += `<tr><td colspan="${2 + archOrder.length}" style="background:#f1f5f9;font-weight:700;color:#475569;padding:8px 10px;font-size:1em;text-align:center;">${section.title}</td></tr>`;
            section.params.forEach(param => {
                if (param.ep_only && !hasEp) return;
                s += `<tr><td style="color:#334155;text-align:center;"><code style="font-size:0.9em;">${param.label}</code></td>`;
                s += `<td style="color:#94a3b8;text-align:center;">${param.def}</td>`;
                archOrder.forEach(arch => {
                    const tc = archConfigs[arch];
                    if (param.ep_only && arch !== 'EP') { s += `<td style="text-align:center;">${naS}</td>`; return; }
                    if (param.pd_only && arch === 'AGGREGATED') { s += `<td style="text-align:center;">${naS}</td>`; return; }
                    const val = param.get(tc);
                    const display = val || param.def;
                    const changed = val && val !== param.def && val !== 'null';
                    const style = changed ? 'font-weight:700;color:#059669;' : '';
                    s += `<td style="text-align:center;${style}">${display}</td>`;
                });
                s += '</tr>';
            });
        });

        s += '</table></div>';
    }

    return s;
}

// ── Concurrency Sweep Tab ──────────────────────────────────────────────────
function buildConcurrencySweepSection(data) {
    if (!data.concurrency_sweep || !Object.keys(data.concurrency_sweep).length) return '';
    const sweep = data.concurrency_sweep;
    const configKeys = Object.keys(sweep);
    let s = '';

    s += '<div style="border-radius:10px;overflow:hidden;border:2px solid #0ea5e9;border-left:6px solid #0ea5e9;margin-bottom:20px;">';
    s += '<div style="background:linear-gradient(135deg,#0ea5e9,#38bdf8);padding:12px 20px;color:white;font-weight:700;">Concurrency Sweep</div>';
    s += '<div style="padding:12px 20px;font-size:0.95em;">Sweep concurrency levels per configuration to measure latency and throughput scaling.</div>';

    let totalPoints = 0;
    let calibratedPoints = 0;
    configKeys.forEach(k => {
        totalPoints += sweep[k].length;
        calibratedPoints += sweep[k].filter(p => p.is_calibrated).length;
    });
    s += '<div class="stats">';
    s += dlStatCard(configKeys.length, 'Configurations');
    s += dlStatCard(totalPoints, 'Data Points');
    if (calibratedPoints > 0) s += dlStatCard(calibratedPoints, 'Calibrated Points');
    s += '</div>';

    ['p90', 'p95', 'p99'].forEach(pctl => {
        s += `<div class="chart-box"><h3>TTFT ${pctl.toUpperCase()} vs Concurrency</h3><div id="dl-sweep-ttft-${pctl}" style="height:430px"></div></div>`;
    });
    s += '<div class="chart-box"><h3>Throughput per GPU vs Concurrency</h3><div id="dl-sweep-tput-gpu" style="height:430px"></div></div>';

    const hasCache = configKeys.some(k => sweep[k].some(p => p.cache_hit_pct != null));
    if (hasCache) {
        s += '<div class="chart-box"><h3>Cache Hit % vs Concurrency</h3><div id="dl-sweep-cache-hit" style="height:430px"></div></div>';
    }

    // Sweep Pareto charts
    s += '<div class="chart-box"><h3>Pareto &mdash; Throughput vs Interactivity</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">Each architecture\'s best throughput/GPU at each concurrency level. Higher curve = better architecture.</p>';
    s += '<div id="dl-sweep-pareto-inter" style="height:600px"></div></div>';
    s += '<div class="chart-box"><h3>Pareto &mdash; Throughput vs TTFT</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">Trade-off between latency and throughput per GPU across concurrency levels.</p>';
    s += '<div id="dl-sweep-pareto-ttft" style="height:600px"></div></div>';

    const tid = 'dl-sweep-table';
    s += `<div class="chart-box"><h3>All Sweep Data Points</h3>`;
    s += `<table id="${tid}"><tr>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',0,'str')">Config &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',1,'num')">Concurrency &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',2,'num')">TTFT P90 &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',3,'num')">TTFT P95 &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',4,'num')">TTFT P99 &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',5,'num')">Tput Mean &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',6,'num')">Tput/GPU &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',7,'num')">ITL P90 &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',8,'num')">GPUs &#x21C5;</th>`;
    s += '<th>Calibrated</th>';
    s += '</tr>';
    configKeys.forEach(cfgKey => {
        const points = [...sweep[cfgKey]].sort((a, b) => a.concurrency - b.concurrency);
        points.forEach(p => {
            const cls = p.is_calibrated ? ' class="pareto"' : '';
            const label = p.config_label || cfgKey;
            s += `<tr${cls}>`;
            s += `<td>${label}</td>`;
            s += `<td data-val="${p.concurrency}">${p.concurrency}</td>`;
            s += `<td data-val="${p.ttft_p90 ?? ''}">${dlFmt(p.ttft_p90)}</td>`;
            s += `<td data-val="${p.ttft_p95 ?? ''}">${dlFmt(p.ttft_p95)}</td>`;
            s += `<td data-val="${p.ttft_p99 ?? ''}">${dlFmt(p.ttft_p99)}</td>`;
            s += `<td data-val="${p.throughput_mean ?? ''}">${dlFmt(p.throughput_mean, 2)}</td>`;
            s += `<td data-val="${p.throughput_per_gpu ?? ''}">${dlFmt(p.throughput_per_gpu, 3)}</td>`;
            s += `<td data-val="${p.itl_p90 ?? ''}">${dlFmt(p.itl_p90)}</td>`;
            s += `<td data-val="${p.gpus ?? ''}">${p.gpus ?? '-'}</td>`;
            s += `<td>${p.is_calibrated ? '<span style="color:#059669;">&#10003;</span>' : ''}</td>`;
            s += '</tr>';
        });
    });
    s += '</table></div>';

    s += '</div>';
    return s;
}

// ── Cache Sweep Tab ────────────────────────────────────────────────────────
function buildCacheSweepSection(data) {
    if (!data.cache_sweep || !Object.keys(data.cache_sweep).length) return '';
    const sweep = data.cache_sweep;
    const configKeys = Object.keys(sweep);
    let s = '';

    s += '<div style="border-radius:10px;overflow:hidden;border:2px solid #8b5cf6;border-left:6px solid #8b5cf6;margin-bottom:20px;">';
    s += '<div style="background:linear-gradient(135deg,#8b5cf6,#a78bfa);padding:12px 20px;color:white;font-weight:700;">Cache Sweep</div>';
    s += '<div style="padding:12px 20px;font-size:0.95em;">Sweep prefix cache hit rates to measure the impact of caching on latency and throughput.</div>';

    let totalPoints = 0;
    configKeys.forEach(k => { totalPoints += sweep[k].length; });
    s += '<div class="stats">';
    s += dlStatCard(configKeys.length, 'Configurations');
    s += dlStatCard(totalPoints, 'Data Points');
    s += '</div>';

    ['p90', 'p95', 'p99'].forEach(pctl => {
        s += `<div class="chart-box"><h3>TTFT ${pctl.toUpperCase()} vs Cache Hit %</h3><div id="dl-cache-ttft-${pctl}" style="height:430px"></div></div>`;
    });
    s += '<div class="chart-box"><h3>Throughput Mean vs Cache Hit %</h3><div id="dl-cache-tput" style="height:430px"></div></div>';
    s += '<div class="chart-box"><h3>Actual Hit Rate vs Configured Hit %</h3><div id="dl-cache-actual-hit" style="height:430px"></div></div>';

    const tid = 'dl-cache-sweep-table';
    s += `<div class="chart-box"><h3>All Cache Sweep Data</h3>`;
    s += `<table id="${tid}"><tr>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',0,'str')">Config &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',1,'num')">Hit % &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',2,'num')">Actual Hit Rate &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',3,'num')">TTFT P90 &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',4,'num')">TTFT P95 &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',5,'num')">TTFT P99 &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',6,'num')">Tput Mean &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',7,'num')">Output TPS &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',8,'num')">ITL P90 &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',9,'num')">Concurrency &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',10,'num')">GPUs &#x21C5;</th>`;
    s += '</tr>';
    configKeys.forEach(cfgKey => {
        const points = [...sweep[cfgKey]].sort((a, b) => (a.hit_pct ?? 0) - (b.hit_pct ?? 0));
        points.forEach(p => {
            s += '<tr>';
            s += `<td>${cfgKey}</td>`;
            s += `<td data-val="${p.hit_pct ?? ''}">${p.hit_pct != null ? p.hit_pct + '%' : '-'}</td>`;
            s += `<td data-val="${p.actual_hit_rate ?? ''}">${dlFmt(p.actual_hit_rate, 2)}</td>`;
            s += `<td data-val="${p.ttft_p90 ?? ''}">${dlFmt(p.ttft_p90)}</td>`;
            s += `<td data-val="${p.ttft_p95 ?? ''}">${dlFmt(p.ttft_p95)}</td>`;
            s += `<td data-val="${p.ttft_p99 ?? ''}">${dlFmt(p.ttft_p99)}</td>`;
            s += `<td data-val="${p.throughput_mean ?? ''}">${dlFmt(p.throughput_mean, 2)}</td>`;
            s += `<td data-val="${p.output_tps_mean ?? ''}">${dlFmt(p.output_tps_mean, 1)}</td>`;
            s += `<td data-val="${p.itl_p90 ?? ''}">${dlFmt(p.itl_p90)}</td>`;
            s += `<td data-val="${p.concurrency ?? ''}">${p.concurrency ?? '-'}</td>`;
            s += `<td data-val="${p.gpus ?? ''}">${p.gpus ?? '-'}</td>`;
            s += '</tr>';
        });
    });
    s += '</table></div>';

    s += '</div>';
    return s;
}

// ── Traffic Tab ────────────────────────────────────────────────────────────
function buildTrafficSection(data, allRes) {
    const hasTraffic = allRes.some(r => r.request_total != null || r.request_errored != null);
    if (!hasTraffic) return '';

    const trafficPrefixes = ['step11-sweep', 'step13', 'step9', 'step7', 'step6', 'step3', 'step2'];
    const trafficLabelMap = {'tp-cal':'TP Calibration','step6':'Aggregated','step7':'PD/EP','step9':'EPP Tuning','step11-sweep':'Concurrency Sweep','step13':'Cache Sweep','other':'Other'};
    const groups = {};
    allRes.forEach(r => {
        if (!r.test_id) return;
        let gk = 'other';
        for (const p of trafficPrefixes) {
            if (r.test_id.startsWith(p)) { gk = p; break; }
        }
        if (!groups[gk]) groups[gk] = [];
        groups[gk].push(r);
    });
    if (groups['step2'] || groups['step3']) {
        groups['tp-cal'] = (groups['step2'] || []).concat(groups['step3'] || []);
        delete groups['step2'];
        delete groups['step3'];
    }

    const groupOrder = ['tp-cal', 'step6', 'step7', 'step9', 'step11-sweep', 'step13', 'other'];
    const groupKeys = groupOrder.filter(k => groups[k] && groups[k].length);
    if (!groupKeys.length) return '';

    let s = '';
    s += '<div style="border-radius:10px;overflow:hidden;border:2px solid #64748b;border-left:6px solid #64748b;margin-bottom:20px;">';
    s += '<div style="background:linear-gradient(135deg,#475569,#64748b);padding:12px 20px;color:white;font-weight:700;">Traffic Overview</div>';
    s += '<div style="padding:12px 20px;font-size:0.95em;">Request counts, errors, and NIXL issues per test phase.</div>';

    let totalRequests = 0, totalErrors = 0, totalNixl = 0;
    allRes.forEach(r => {
        totalRequests += r.request_total || 0;
        totalErrors += r.request_errored || 0;
        totalNixl += r.nixl_errors || 0;
    });
    s += '<div class="stats">';
    s += dlStatCard(totalRequests.toLocaleString(), 'Total Requests');
    s += dlStatCard(totalErrors.toLocaleString(), 'HTTP Request Errors');
    if (totalNixl > 0) s += dlStatCard(totalNixl.toLocaleString(), 'NIXL Transfer Retries');
    const errorRate = totalRequests > 0 ? (totalErrors / totalRequests * 100).toFixed(2) : '0';
    s += dlStatCard(errorRate + '%', 'Error Rate');
    s += '</div>';

    // Network throughput charts (moved from vLLM tab)
    if (data.charts && data.charts.vllm && data.charts.vllm.network) {
        const net = data.charts.vllm.network;
        const hasPodNet = net.pod_tx && net.pod_tx.some(v => v > 0);
        const hasNIXL = (net.nixl_tx && net.nixl_tx.some(v => v > 0)) || (net.ib_rx && net.ib_rx.some(v => v > 0));
        if (hasPodNet || hasNIXL) {
            s += '<div class="grid2">';
            if (hasPodNet) s += '<div class="chart-box"><h3>Pod Network Throughput</h3><p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">Management (eth0) network traffic per configuration.</p><div id="v7" style="height:430px"></div></div>';
            if (hasNIXL) s += '<div class="chart-box"><h3>NIXL KV Transfer Throughput</h3><p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">RDMA traffic for KV cache transfer between prefill and decode pods.</p><div id="v8" style="height:430px"></div></div>';
            s += '</div>';
        }
    }

    groupKeys.forEach(gk => {
        const label = trafficLabelMap[gk] || gk;
        s += `<div class="chart-box"><h3>Traffic: ${label}</h3><div id="dl-traffic-${gk}" style="height:400px"></div></div>`;
    });

    const tid = 'dl-traffic-table';
    s += `<div class="chart-box"><h3>Per-Test Traffic</h3>`;
    s += `<table id="${tid}"><tr>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',0,'str')">Test ID &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',1,'str')">Config &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',2,'num')">Requests &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',3,'num')">HTTP Errors &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',4,'num')">NIXL Retries &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',5,'num')">NIXL Degraded &#x21C5;</th>`;
    s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',6,'num')">Quality &#x21C5;</th>`;
    s += '</tr>';
    allRes.forEach(r => {
        if (r.request_total == null && r.request_errored == null) return;
        const errStyle = (r.request_errored || 0) > 0 ? 'color:#dc2626;font-weight:700;' : '';
        const nixlStyle = (r.nixl_errors || 0) > 0 ? 'color:#dc2626;font-weight:700;' : '';
        s += '<tr>';
        s += `<td>${r.test_id || '-'}</td>`;
        s += `<td>${r.config_name || '-'}</td>`;
        s += `<td data-val="${r.request_total ?? 0}">${r.request_total ?? '-'}</td>`;
        s += `<td data-val="${r.request_errored ?? 0}" style="${errStyle}">${r.request_errored ?? 0}</td>`;
        s += `<td data-val="${r.nixl_errors ?? 0}" style="${nixlStyle}">${r.nixl_errors ?? 0}</td>`;
        s += `<td data-val="${r.nixl_degraded ?? 0}">${r.nixl_degraded ?? 0}</td>`;
        s += `<td data-val="${r.quality ?? ''}">${r.quality ?? '-'}</td>`;
        s += '</tr>';
    });
    s += '</table></div>';

    s += '</div>';
    return s;
}

// ── Deploy Timing Tab ──────────────────────────────────────────────────────
function buildDeployTimingSection(data, allRes) {
    const timings = [];
    allRes.forEach(r => {
        const dt = r.deploy_timing || (r.test_config && r.test_config.deploy_timing);
        if (dt && (dt.pod_creation_s != null || dt.model_load_s != null)) {
            timings.push({
                config: r.config_name || r.test_id || 'Unknown',
                pod_creation_s: dt.pod_creation_s || 0,
                model_load_s: dt.model_load_s || 0,
                total_s: (dt.pod_creation_s || 0) + (dt.model_load_s || 0)
            });
        }
    });
    if (!timings.length) return '';

    let s = '';
    s += '<div style="border-radius:10px;overflow:hidden;border:2px solid #f59e0b;border-left:6px solid #f59e0b;margin-bottom:20px;">';
    s += '<div style="background:linear-gradient(135deg,#f59e0b,#fbbf24);padding:12px 20px;color:white;font-weight:700;">Deploy Timing</div>';
    s += '<div style="padding:12px 20px;font-size:0.95em;">Time taken for pod creation and model loading per configuration.</div>';

    const avgTotal = timings.reduce((a, t) => a + t.total_s, 0) / timings.length;
    const minTotal = Math.min(...timings.map(t => t.total_s));
    const maxTotal = Math.max(...timings.map(t => t.total_s));
    s += '<div class="stats">';
    s += dlStatCard(timings.length, 'Deployments');
    s += dlStatCard(dlFmt(avgTotal, 0) + 's', 'Avg Deploy Time');
    s += dlStatCard(dlFmt(minTotal, 0) + 's', 'Fastest Deploy');
    s += dlStatCard(dlFmt(maxTotal, 0) + 's', 'Slowest Deploy');
    s += '</div>';

    s += '<div class="chart-box"><h3>Deploy Time per Configuration</h3><div id="dl-deploy-timing" style="height:430px"></div></div>';

    s += '<div class="chart-box"><h3>Deploy Timing Details</h3>';
    s += '<table><tr><th>Configuration</th><th>Pod Creation (s)</th><th>Model Load (s)</th><th>Total (s)</th></tr>';
    [...timings].sort((a, b) => a.total_s - b.total_s).forEach(t => {
        s += `<tr><td>${t.config}</td><td>${dlFmt(t.pod_creation_s, 1)}</td><td>${dlFmt(t.model_load_s, 1)}</td><td><strong>${dlFmt(t.total_s, 1)}</strong></td></tr>`;
    });
    s += '</table></div>';

    s += '</div>';
    return s;
}

// ── Pareto Frontier Tab ────────────────────────────────────────────────────
function buildParetoFrontierSection(data, allRes) {
    const valid = (allRes || []).filter(r => {
        const tid = r.test_id || r.config_name || '';
        return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
               r.ttft_p90 > 0 && (r.throughput_mean > 0 || r.throughput_p90 > 0);
    });
    if (valid.length < 2) return '';
    let s = '';
    s += '<div class="chart-box"><h3>Pareto &mdash; Throughput vs TTFT</h3>';
    s += '<p style="color:#64748b;font-size:0.9em;margin:0 0 4px;">Higher throughput per GPU (Y) and lower TTFT (X) is better. Frontier lines show the best achievable trade-offs.</p>';
    s += '<div id="dl-pareto-ttft" style="height:700px"></div></div>';
    s += '<div class="chart-box"><h3>Pareto &mdash; Throughput vs Interactivity</h3>';
    s += '<p style="color:#64748b;font-size:0.9em;margin:0 0 4px;">Higher interactivity (tok/s/user, X) and higher throughput/GPU (Y) is better.</p>';
    s += '<div id="dl-pareto-interactivity" style="height:700px"></div></div>';
    return s;
}

// ── Chart Rendering Script ──────────────────────────────────────────────────
function buildChartScript(data, charts, allRes) {
    let s = '<script>';
    s += `function switchDlTab(id){document.querySelectorAll('.dl-tab').forEach(function(t){t.classList.remove('active')});document.querySelectorAll('.dl-pane').forEach(function(p){p.classList.remove('active')});var tab=document.querySelector('.dl-tab[onclick*=\"'+id+'\"]');if(tab)tab.classList.add('active');var pane=document.getElementById('dl-pane-'+id);if(pane){pane.classList.add('active');pane.querySelectorAll('[class*="js-plotly"]').forEach(function(p){Plotly.Plots.resize(p)});}}`;
    s += 'function sortReportTable(tableId,colIdx,type){var table=document.getElementById(tableId);if(!table)return;var rows=Array.from(table.querySelectorAll("tr")).slice(1);var baselineRows=rows.filter(function(r){return r.classList.contains("baseline-row")});var dataRows=rows.filter(function(r){return !r.classList.contains("baseline-row")});var dir=table.getAttribute("data-sort-col")===String(colIdx)&&table.getAttribute("data-sort-dir")==="asc"?"desc":"asc";table.setAttribute("data-sort-col",colIdx);table.setAttribute("data-sort-dir",dir);dataRows.sort(function(a,b){var aCell=a.cells[colIdx],bCell=b.cells[colIdx];var aVal,bVal;if(type==="num"){aVal=parseFloat(aCell.getAttribute("data-val")||aCell.textContent.replace(/[^0-9.\\-]/g,""))||0;bVal=parseFloat(bCell.getAttribute("data-val")||bCell.textContent.replace(/[^0-9.\\-]/g,""))||0}else{aVal=aCell.textContent.trim().toLowerCase();bVal=bCell.textContent.trim().toLowerCase()}if(aVal<bVal)return dir==="asc"?-1:1;if(aVal>bVal)return dir==="asc"?1:-1;return 0});var tbody=table.querySelector("tbody")||table;dataRows.forEach(function(r){tbody.appendChild(r)});baselineRows.forEach(function(r){tbody.appendChild(r)})}';
    // Escape strings for safe embedding in <script> tags
    function safeJson(obj) {
        return JSON.stringify(obj).replace(/<\//g, '<\\/').replace(/<!--/g, '<\\!--');
    }
    // Strip large fields from allRes to keep HTML size manageable
    const arLite = allRes.map(r => {
        const copy = Object.assign({}, r);
        delete copy.metrics_json;
        delete copy.manifests;
        delete copy.test_config;
        return copy;
    });
    s += 'var cd=' + safeJson(charts) + ';';
    s += 'var ar=' + safeJson(arLite) + ';';
    // Build embedded manifest lookup for offline YAML download
    const manifestMap = {};
    allRes.forEach(r => {
        if (r.manifests && typeof r.manifests === 'object' && Object.keys(r.manifests).length) {
            manifestMap[r.test_id] = r.manifests;
        }
    });
    s += 'var _manifests=' + safeJson(manifestMap) + ';';
    s += 'function dlManifest(testId,type){var m=_manifests[testId];if(!m||!m[type]){alert("Manifest not available for "+testId+" / "+type);return;}var blob=new Blob([m[type]],{type:"text/yaml"});var url=URL.createObjectURL(blob);var a=document.createElement("a");a.href=url;a.download=testId+"-"+type+".yaml";a.click();URL.revokeObjectURL(url);}';
    s += 'var lo={margin:{t:30,b:40,l:50,r:20},height:430,font:{family:"sans-serif"}};';
    s += 'var co={responsive:true};';
    s += 'function fmtSI(v,d){if(v==null)return"-";d=d!=null?d:1;if(Math.abs(v)>=1e6)return(v/1e6).toFixed(d)+"M";if(Math.abs(v)>=1e3)return(v/1e3).toFixed(d)+"K";return v.toFixed(d)}';
    s += 'function arrAnn(xs,ys,o){o=o||{};var c=o.color||"#333",d=o.decimals!=null?o.decimals:1,sp=o.suffix||"",spr=o.spread||30;var offs=[{ax:0,ay:-spr},{ax:spr*0.9,ay:spr*0.7},{ax:-spr*0.8,ay:-spr*1.2},{ax:spr*1.1,ay:-spr*0.5},{ax:0,ay:spr*1.1},{ax:-spr,ay:spr*0.8},{ax:spr*1.3,ay:-spr*1.3},{ax:-spr*1.2,ay:spr*1.3}];return ys.map(function(v,i){if(v==null)return null;var p=offs[i%offs.length];return{x:xs[i],y:v,xref:"x",yref:o.yref||"y",text:fmtSI(v,d)+sp,showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#94a3b8",ax:p.ax,ay:p.ay,font:{size:10,color:c},borderpad:2}}).filter(Boolean)}';
    s += 'var vl={...lo,margin:{...lo.margin,b:100},barmode:"group",showlegend:true,legend:{x:0,y:1.15,orientation:"h"}};';
    s += 'var pc={p50:"#60a5fa",p90:"#3b82f6",p95:"#f59e0b",p99:"#ef4444"};';

    // Show all panes for initial rendering
    s += 'document.querySelectorAll(".dl-pane").forEach(function(p){p.style.display="block"});';

    // TP calibration charts — use rec.decode_tp_all / rec.prefill_tp_all
    s += 'var recData=' + JSON.stringify(data.recommendation || {}) + ';';

    // Decode TP Sweep (dual-axis: TPSG bars + ITL P90 line)
    s += 'if(recData.decode_tp_all&&recData.decode_tp_all.length&&document.getElementById("tp-dec")){';
    s += '  var dtp=recData.decode_tp_all;';
    s += '  var tpLabels=dtp.map(function(d){return"TP="+d.tp});';
    s += '  var tpsgVals=dtp.map(function(d){return d.tpsg});';
    s += '  var bestTpsg=Math.max.apply(null,tpsgVals);';
    s += '  var barColors=tpsgVals.map(function(v){return v===bestTpsg?"#10b981":"#6366f1"});';
    s += '  var itlVals=dtp.map(function(d){return d.itl_p90!=null?d.itl_p90:0});';
    s += '  var dTraces=[{x:tpLabels,y:tpsgVals,name:"Tokens/s/GPU",type:"bar",marker:{color:barColors},text:tpsgVals.map(function(v){return fmtSI(v)}),textposition:"outside",textfont:{size:11,color:"#1e293b"},cliponaxis:false,constraintext:"none",hovertemplate:"<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>"}];';
    s += '  if(itlVals.some(function(v){return v>0})){dTraces.push({x:tpLabels,y:itlVals,name:"ITL P90 (ms)",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#ef4444",width:3},marker:{size:10,symbol:"circle",color:"#ef4444",line:{width:2,color:"white"}},hovertemplate:"<b>%{x}</b><br>ITL P90: %{y:.2f} ms<extra></extra>"});}';
    s += '  Plotly.newPlot("tp-dec",dTraces,{...lo,barmode:"group",showlegend:true,legend:{x:0,y:1.15,orientation:"h"},yaxis:{title:"Tokens/s/GPU",side:"left",tickformat:".2s"},yaxis2:{title:"ITL P90 (ms)",side:"right",overlaying:"y",titlefont:{color:"#ef4444"},tickfont:{color:"#ef4444"}}},co);';
    s += '}';

    // Prefill TP Sweep (dual-axis: TPSG bars + TTFT P90 line)
    s += 'if(recData.prefill_tp_all&&recData.prefill_tp_all.length&&document.getElementById("tp-pre")){';
    s += '  var ptp=recData.prefill_tp_all;';
    s += '  var ptpLabels=ptp.map(function(d){return"TP="+d.tp});';
    s += '  var ptpsgVals=ptp.map(function(d){return d.tpsg});';
    s += '  var pBestTpsg=Math.max.apply(null,ptpsgVals);';
    s += '  var pBarColors=ptpsgVals.map(function(v){return v===pBestTpsg?"#10b981":"#f59e0b"});';
    s += '  var ttftVals=ptp.map(function(d){return d.ttft_p90!=null?d.ttft_p90:0});';
    s += '  var pTraces=[{x:ptpLabels,y:ptpsgVals,name:"Tokens/s/GPU",type:"bar",marker:{color:pBarColors},text:ptpsgVals.map(function(v){return fmtSI(v)}),textposition:"outside",textfont:{size:11,color:"#1e293b"},cliponaxis:false,constraintext:"none",hovertemplate:"<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>"}];';
    s += '  if(ttftVals.some(function(v){return v>0})){pTraces.push({x:ptpLabels,y:ttftVals,name:"TTFT P90 (ms)",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#3b82f6",width:3},marker:{size:10,symbol:"circle",color:"#3b82f6",line:{width:2,color:"white"}},hovertemplate:"<b>%{x}</b><br>TTFT P90: %{y:.1f} ms<extra></extra>"});}';
    s += '  Plotly.newPlot("tp-pre",pTraces,{...lo,barmode:"group",showlegend:true,legend:{x:0,y:1.15,orientation:"h"},yaxis:{title:"Tokens/s/GPU",side:"left",tickformat:".2s"},yaxis2:{title:"TTFT P90 (ms)",side:"right",overlaying:"y",titlefont:{color:"#3b82f6"},tickfont:{color:"#3b82f6"}}},co);';
    s += '}';

    // Pareto + scatter + efficiency + architecture charts
    s += 'if(cd.pareto&&cd.pareto.traces&&cd.pareto.traces.length){var pxv=[...new Set(cd.pareto.traces.flatMap(function(t){return t.x}))].sort(function(a,b){return a-b});Plotly.newPlot("p1",cd.pareto.traces.map(function(t){return{x:t.x,y:t.y,text:t.text,name:t.name,mode:"markers+lines",marker:{size:14,color:t.color,symbol:"diamond",line:{width:2,color:"white"}},line:{width:2,dash:"dot"},hovertemplate:"<b>%{text}</b><extra></extra>"}}),{...lo,xaxis:{title:"GPUs",tickvals:pxv},yaxis:{title:"TTFT P90 (ms)"},showlegend:true},co);}';
    s += 'if(cd.scatter.traces.length){Plotly.newPlot("p2",cd.scatter.traces.map(function(t){return{x:t.x,y:t.y,text:t.text,name:t.name,mode:"markers",marker:{size:t.sizes,color:t.color,opacity:0.7,line:{width:1,color:"white"}},hovertemplate:"<b>%{text}</b><extra></extra>"}}),{...lo,xaxis:{title:"TTFT P90 (ms)"},yaxis:{title:"Throughput P90 (req/s)"},showlegend:true},co);}';
    s += 'if(cd.efficiency.configs.length){Plotly.newPlot("p3",[{x:cd.efficiency.configs,y:cd.efficiency.values,type:"bar",marker:{color:cd.efficiency.colors},text:cd.efficiency.values.map(function(v){return v!=null?v.toFixed(3):""}),textposition:"outside",textfont:{size:11,color:"#333"},cliponaxis:false,constraintext:"none"}],{...lo,margin:{...lo.margin,b:120},xaxis:{tickangle:-45},yaxis:{title:"req/s/GPU"}},co);}';
    s += 'if(cd.architecture.architectures.length){var a=cd.architecture;Plotly.newPlot("p4",[{x:a.architectures,y:a.avg_ttft,type:"bar",marker:{color:"#3b82f6"},text:a.avg_ttft.map(function(v){return fmtSI(v)+" ms"}),textposition:"auto",name:"Avg TTFT P90",xaxis:"x",yaxis:"y"},{x:a.architectures,y:a.best_ttft,type:"bar",marker:{color:"#93c5fd"},text:a.best_ttft.map(function(v){return fmtSI(v)+" ms"}),textposition:"auto",name:"Best TTFT P90",xaxis:"x",yaxis:"y"},{x:a.architectures,y:a.avg_throughput,type:"bar",marker:{color:"#f59e0b"},text:a.avg_throughput.map(function(v){return v.toFixed(2)+" req/s"}),textposition:"auto",name:"Avg Throughput P90",xaxis:"x2",yaxis:"y2"}],{...lo,margin:{t:30,b:50,l:60,r:60},barmode:"group",showlegend:true,legend:{x:0,y:1.18,orientation:"h"},xaxis:{domain:[0,0.45]},yaxis:{title:"TTFT (ms)",titlefont:{color:"#3b82f6"},tickformat:".2s"},xaxis2:{domain:[0.55,1],anchor:"y2"},yaxis2:{title:"Throughput (req/s)",anchor:"x2",titlefont:{color:"#f59e0b"}}},co);}';

    // Comparison tab: Architecture Comparison (reuse same data as p4 but in separate div)
    s += 'if(cd.architecture&&cd.architecture.architectures.length&&document.getElementById("dl-chart-arch")){var a2=cd.architecture;Plotly.newPlot("dl-chart-arch",[{x:a2.architectures,y:a2.avg_ttft,type:"bar",marker:{color:"#3b82f6"},text:a2.avg_ttft.map(function(v){return fmtSI(v)+" ms"}),textposition:"auto",name:"Avg TTFT P90",xaxis:"x",yaxis:"y"},{x:a2.architectures,y:a2.best_ttft,type:"bar",marker:{color:"#93c5fd"},text:a2.best_ttft.map(function(v){return fmtSI(v)+" ms"}),textposition:"auto",name:"Best TTFT P90",xaxis:"x",yaxis:"y"},{x:a2.architectures,y:a2.avg_throughput,type:"bar",marker:{color:"#f59e0b"},text:a2.avg_throughput.map(function(v){return v.toFixed(2)+" req/s"}),textposition:"auto",name:"Avg Throughput P90",xaxis:"x2",yaxis:"y2"}],{...lo,margin:{t:30,b:50,l:60,r:60},barmode:"group",showlegend:true,legend:{x:0,y:1.18,orientation:"h"},xaxis:{domain:[0,0.45]},yaxis:{title:"TTFT (ms)",titlefont:{color:"#3b82f6"},tickformat:".2s"},xaxis2:{domain:[0.55,1],anchor:"y2"},yaxis2:{title:"Throughput (req/s)",anchor:"x2",titlefont:{color:"#f59e0b"}}},co);}';

    // Comparison tab: Percentile Comparison (Winner vs Aggregated)
    s += '(function(){';
    s += '  var el=document.getElementById("dl-chart-pctile");if(!el)return;';
    s += '  if(!recData||!recData.recommendations||!recData.aggregated_baseline)return;';
    s += '  var primaryKey=recData.goal==="ttft"?"response_time":"throughput";';
    s += '  var pr=recData.recommendations[primaryKey];';
    s += '  var ab=recData.aggregated_baseline;';
    s += '  if(!pr||!pr.config||!pr.config.percentiles||!ab||!ab.percentiles)return;';
    s += '  var pp=pr.config.percentiles;var ap=ab.percentiles;';
    s += '  var pArch=pr.architecture||"PD";';
    s += '  var pctls=["p50","p90","p95","p99"];';
    s += '  var ttftT=[{x:pctls,y:pctls.map(function(p){return pp.ttft[p]}),name:pArch+" TTFT",type:"bar",marker:{color:"#3b82f6"}},{x:pctls,y:pctls.map(function(p){return ap.ttft[p]}),name:"Aggregated TTFT",type:"bar",marker:{color:"#94a3b8"}}];';
    s += '  var tputT=[{x:pctls,y:pctls.map(function(p){return pp.throughput[p]}),name:pArch+" Throughput",type:"bar",marker:{color:"#10b981"},xaxis:"x2",yaxis:"y2"},{x:pctls,y:pctls.map(function(p){return ap.throughput[p]}),name:"Aggregated Throughput",type:"bar",marker:{color:"#d1d5db"},xaxis:"x2",yaxis:"y2"}];';
    s += '  Plotly.newPlot(el,ttftT.concat(tputT),{...lo,margin:{t:30,b:50,l:60,r:60},barmode:"group",showlegend:true,legend:{x:0,y:1.18,orientation:"h"},xaxis:{domain:[0,0.45],title:"Percentile"},yaxis:{title:"TTFT (ms)",titlefont:{color:"#3b82f6"}},xaxis2:{domain:[0.55,1],title:"Percentile",anchor:"y2"},yaxis2:{title:"Throughput (req/s)",anchor:"x2",titlefont:{color:"#10b981"}}},co);';
    s += '})();';

    // Cache hit rate chart (exclude step2/step3 calibration runs)
    s += 'if(document.getElementById("dl-cfg-cache-hit")){';
    s += '  var chd=ar.filter(function(r){var tid=r.test_id||r.config_name||"";return tid.indexOf("step2-")!==0&&tid.indexOf("step3-")!==0&&r.cache_hit_pct!=null&&r.cache_hit_pct>0});';
    s += '  if(chd.length){';
    s += '    var chTraces=[];';
    s += '    var archCols={AGGREGATED:"#1f77b4",PD:"#ff7f0e",EP:"#2ca02c"};';
    s += '    ["AGGREGATED","PD","EP"].forEach(function(arch){';
    s += '      var filtered=chd.filter(function(r){return r.architecture===arch});';
    s += '      if(!filtered.length)return;';
    s += '      chTraces.push({x:filtered.map(function(r){return r.config_name}),y:filtered.map(function(r){return r.cache_hit_pct}),text:filtered.map(function(r){return r.cache_hit_pct.toFixed(1)+"%"}),textposition:"outside",textfont:{size:10},name:arch,type:"bar",marker:{color:archCols[arch]}});';
    s += '    });';
    s += '    Plotly.newPlot("dl-cfg-cache-hit",chTraces,{...lo,height:400,barmode:"group",xaxis:{tickangle:-35},yaxis:{title:"Cache Hit %",range:[0,100]},showlegend:true,legend:{x:0,y:1,bgcolor:"rgba(255,255,255,0.9)"},margin:{t:20,b:120,l:60,r:20}},co);';
    s += '  }';
    s += '}';

    // Per-percentile PD charts
    s += 'var pd=ar.filter(function(r){return r.architecture==="PD"});';
    s += 'if(pd.length){';
    s += '  pd.sort(function(a,b){return a.prefill_pods-b.prefill_pods});';
    s += '  var lbls=pd.map(function(r){return r.prefill_pods+"P : "+r.decode_pods+"D"});';
    s += '  var pctls=[{k:"p90",c:"#3b82f6"},{k:"p95",c:"#f59e0b"},{k:"p99",c:"#ef4444"}];';
    s += '  pctls.forEach(function(pctl){';
    s += '    var el=document.getElementById("pd-ttft-"+pctl.k);if(!el)return;';
    s += '    var ttft=pd.map(function(r){return r["ttft_"+pctl.k]});';
    s += '    var tput=pd.map(function(r){return r.throughput_mean||r.throughput_p90});';
    s += '    var best=Math.min.apply(null,ttft.filter(function(v){return v!=null}));';
    s += '    var clrs=ttft.map(function(v){return v===best?"#10b981":pctl.c});';
    s += '    var szs=ttft.map(function(v){return v===best?22:14});';
    s += '    var ttftAnn=arrAnn(lbls,ttft,{color:"#1e40af",decimals:0,suffix:"ms",spread:35});';
    s += '    var eppAnns=[];pd.forEach(function(r,i){if(r.test_id&&r.test_id.indexOf("step11-epp-")===0){eppAnns.push({x:lbls[i],y:ttft[i],yref:"y",text:"<b>EPP TUNED</b>",showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#7c3aed",ax:55,ay:0,font:{size:9,color:"white"},bgcolor:"#7c3aed",borderpad:3,bordercolor:"#7c3aed",borderwidth:1});eppAnns.push({x:lbls[i],y:tput[i],yref:"y2",text:"<b>EPP TUNED</b>",showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#7c3aed",ax:55,ay:0,font:{size:9,color:"white"},bgcolor:"#7c3aed",borderpad:3,bordercolor:"#7c3aed",borderwidth:1})}});';
    s += '    var traces=[{x:lbls,y:ttft,name:"TTFT "+pctl.k.toUpperCase(),type:"scatter",mode:"lines+markers",line:{color:pctl.c,width:3,shape:"spline"},marker:{color:clrs,size:szs,symbol:"circle",line:{width:2,color:"white"}},fill:"tozeroy",fillcolor:pctl.c+"14"},';
    s += '      {x:lbls,y:tput,name:"Throughput Mean",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3,shape:"spline"},marker:{color:"#f59e0b",size:10,symbol:"diamond",line:{width:2,color:"white"}}}];';
    // Aggregated baseline
    s += '    var aggr=ar.filter(function(r){return r.architecture==="Aggregated"});';
    s += '    if(aggr.length){var ab=aggr[0];var abl=ab["ttft_"+pctl.k];var abt=ab.throughput_mean||ab.throughput_p90;';
    s += '      if(abl!=null){traces.push({x:["Agg Baseline"],y:[abl],name:"Aggregated",type:"scatter",mode:"markers+text",marker:{color:"#94a3b8",size:16,symbol:"star",line:{width:2,color:"white"}},text:[abl.toFixed(0)+"ms"],textposition:"top center",textfont:{size:10,color:"#64748b"},showlegend:true});}';
    s += '      if(abt!=null){traces.push({x:["Agg Baseline"],y:[abt],name:"Agg Throughput",type:"scatter",mode:"markers",yaxis:"y2",marker:{color:"#d4d4d8",size:12,symbol:"star",line:{width:2,color:"white"}},showlegend:false});}}';
    s += '    Plotly.newPlot(el,traces,{...lo,height:500,margin:{t:30,b:80,l:60,r:60},xaxis:{title:"Prefill : Decode Pod Ratio"},yaxis:{title:"TTFT "+pctl.k.toUpperCase()+" (ms)",titlefont:{color:pctl.c},tickfont:{color:pctl.c}},yaxis2:{title:"Throughput Mean (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.18,orientation:"h"},annotations:ttftAnn.concat(eppAnns)},co);';
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
        s += '    var tps=st.map(function(t){return t.throughput_mean||t.throughput_p90});';
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
    s += 'Plotly.newPlot("v1",[{x:v.configs,y:v.ttft.p50,name:"P50",mode:"lines+markers",line:{color:pc.p50,width:2},marker:{size:8}},{x:v.configs,y:v.ttft.p90,name:"P90",mode:"lines+markers",line:{color:pc.p90,width:3},marker:{size:10}},{x:v.configs,y:v.ttft.p95,name:"P95",mode:"lines+markers",line:{color:pc.p95,width:2},marker:{size:8}},{x:v.configs,y:v.ttft.p99,name:"P99",mode:"lines+markers",line:{color:pc.p99,width:2},marker:{size:8}}],{...vl,xaxis:{tickangle:-35},yaxis:{title:"TTFT (ms)"}},co);';
    s += 'Plotly.newPlot("v2",[{x:v.configs,y:v.itl.p50,name:"P50",mode:"lines+markers",line:{color:pc.p50,width:2},marker:{size:8}},{x:v.configs,y:v.itl.p90,name:"P90",mode:"lines+markers",line:{color:pc.p90,width:3},marker:{size:10}},{x:v.configs,y:v.itl.p95,name:"P95",mode:"lines+markers",line:{color:pc.p95,width:2},marker:{size:8}},{x:v.configs,y:v.itl.p99,name:"P99",mode:"lines+markers",line:{color:pc.p99,width:2},marker:{size:8}}],{...vl,xaxis:{tickangle:-35},yaxis:{title:"ITL (ms)"}},co);';
    s += 'Plotly.newPlot("v3",[{x:v.configs,y:v.e2e.p50,name:"P50",mode:"lines+markers",line:{color:pc.p50,width:2},marker:{size:8}},{x:v.configs,y:v.e2e.p90,name:"P90",mode:"lines+markers",line:{color:pc.p90,width:3},marker:{size:10}},{x:v.configs,y:v.e2e.p95,name:"P95",mode:"lines+markers",line:{color:pc.p95,width:2},marker:{size:8}},{x:v.configs,y:v.e2e.p99,name:"P99",mode:"lines+markers",line:{color:pc.p99,width:2},marker:{size:8}}],{...vl,xaxis:{tickangle:-35},yaxis:{title:"E2E (seconds)"}},co);';
    s += 'Plotly.newPlot("v4",[{x:v.configs,y:v.token_rates.prompt,name:"Prompt Tokens/s",mode:"lines+markers",line:{color:"#6366f1",width:3},marker:{size:10}},{x:v.configs,y:v.token_rates.generation,name:"Generation Tokens/s",mode:"lines+markers",line:{color:"#10b981",width:3},marker:{size:10}}],{...vl,xaxis:{tickangle:-35},yaxis:{title:"Tokens/s"}},co);';
    s += 'Plotly.newPlot("v5",[{x:v.configs,y:v.request_state.running,name:"Running",type:"bar",marker:{color:"#3b82f6"}},{x:v.configs,y:v.request_state.waiting,name:"Waiting",type:"bar",marker:{color:"#ef4444"}},{x:v.configs,y:v.request_state.kv_cache,name:"KV Cache %",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3},marker:{size:10,symbol:"diamond",color:"#f59e0b"}}],{...vl,margin:{...vl.margin,r:60},xaxis:{tickangle:-35},yaxis:{title:"Count"},yaxis2:{title:"KV Cache %",side:"right",overlaying:"y",range:[0,105]}},co);';
    s += 'Plotly.newPlot("v6",[{x:v.configs,y:v.time_breakdown.prefill,name:"Prefill",type:"bar",marker:{color:"#6366f1"}},{x:v.configs,y:v.time_breakdown.decode,name:"Decode",type:"bar",marker:{color:"#3b82f6"}},{x:v.configs,y:v.time_breakdown.queue,name:"Queue",type:"bar",marker:{color:"#94a3b8"}},{x:v.configs,y:v.time_breakdown.preemptions,name:"Preemptions/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#ef4444",width:3},marker:{size:10,symbol:"triangle-up",color:"#ef4444"}}],{...vl,barmode:"stack",margin:{...vl.margin,r:60},xaxis:{tickangle:-35},yaxis:{title:"Time Rate (s/s)"},yaxis2:{title:"Preemptions/s",side:"right",overlaying:"y"}},co);';
    // Pod network (eth0 management traffic)
    s += 'if(v.network&&v.network.pod_tx&&v.network.pod_tx.some(function(x){return x>0})&&document.getElementById("v7")){Plotly.newPlot("v7",[{x:v.configs,y:v.network.pod_tx,name:"TX (MB/s)",mode:"lines+markers",line:{color:"#3b82f6",width:3},marker:{size:8}},{x:v.configs,y:v.network.pod_rx,name:"RX (MB/s)",mode:"lines+markers",line:{color:"#10b981",width:3},marker:{size:8}}],{...vl,xaxis:{tickangle:-35},yaxis:{title:"MB/s"}},co);}';
    // NIXL KV Transfer (RDMA traffic)
    s += 'if(document.getElementById("v8")){';
    s += '  var nixlTraces=[];';
    s += '  var nixlData=v.network&&v.network.nixl_tx?v.network.nixl_tx:[];';
    s += '  var ibData=v.network&&v.network.ib_rx?v.network.ib_rx:[];';
    s += '  if(nixlData.some(function(x){return x>0})){nixlTraces.push({x:v.configs,y:nixlData,name:"NIXL TX (GB/s)",mode:"lines+markers+text",text:nixlData.map(function(x){return x>0?x.toFixed(2):""}),textposition:"top center",textfont:{size:10,color:"#8b5cf6"},line:{color:"#8b5cf6",width:3},marker:{size:8},hovertemplate:"<b>%{x}</b><br>NIXL TX: %{y:.2f} GB/s<extra></extra>"});}';
    s += '  if(ibData.some(function(x){return x>0})){nixlTraces.push({x:v.configs,y:ibData,name:"IB RX (GB/s)",mode:"lines+markers",line:{color:"#f59e0b",width:2,dash:"dash"},marker:{size:8},hovertemplate:"<b>%{x}</b><br>IB RX: %{y:.2f} GB/s<extra></extra>"});}';
    s += '  if(nixlTraces.length){Plotly.newPlot("v8",nixlTraces,{...vl,xaxis:{tickangle:-35},yaxis:{title:"Throughput (GB/s)"}},co);}';
    s += '}';
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
        s += '    var tps=trials.map(function(t){return t.throughput_mean||t.throughput_p90});';
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

    // Concurrency Sweep charts
    if (data.concurrency_sweep && Object.keys(data.concurrency_sweep).length) {
        s += 'var csData=' + JSON.stringify(data.concurrency_sweep) + ';';
        s += 'var csColors=["#3b82f6","#10b981","#f59e0b","#ef4444","#8b5cf6","#0ea5e9","#ec4899","#14b8a6"];';
        s += '[{k:"p90"},{k:"p95"},{k:"p99"}].forEach(function(pctl){';
        s += '  var el=document.getElementById("dl-sweep-ttft-"+pctl.k);if(!el)return;';
        s += '  var traces=[];var ci=0;';
        s += '  Object.keys(csData).forEach(function(cfgKey){';
        s += '    var pts=[].concat(csData[cfgKey]).sort(function(a,b){return a.concurrency-b.concurrency});';
        s += '    var cx=pts.map(function(p){return p.concurrency});';
        s += '    var lats=pts.map(function(p){return p["ttft_"+pctl.k]});';
        s += '    var color=csColors[ci%csColors.length];ci++;';
        s += '    var label=pts[0]&&pts[0].config_label?pts[0].config_label:cfgKey;';
        s += '    traces.push({x:cx,y:lats,name:label,type:"scatter",mode:"lines+markers",line:{color:color,width:2},marker:{color:color,size:8}});';
        s += '    var calPts=pts.filter(function(p){return p.is_calibrated});';
        s += '    if(calPts.length){traces.push({x:calPts.map(function(p){return p.concurrency}),y:calPts.map(function(p){return p["ttft_"+pctl.k]}),name:label+" (calibrated)",type:"scatter",mode:"markers",marker:{color:color,size:16,symbol:"star",line:{width:2,color:"white"}},showlegend:false});}';
        s += '  });';
        s += '  Plotly.newPlot(el,traces,{...lo,xaxis:{title:"Concurrent Users"},yaxis:{title:"TTFT "+pctl.k.toUpperCase()+" (ms)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
        s += '});';
        s += '(function(){var el=document.getElementById("dl-sweep-tput-gpu");if(!el)return;';
        s += '  var traces=[];var ci=0;';
        s += '  Object.keys(csData).forEach(function(cfgKey){';
        s += '    var pts=[].concat(csData[cfgKey]).sort(function(a,b){return a.concurrency-b.concurrency});';
        s += '    var cx=pts.map(function(p){return p.concurrency});';
        s += '    var tpg=pts.map(function(p){return p.throughput_per_gpu});';
        s += '    var color=csColors[ci%csColors.length];ci++;';
        s += '    var label=pts[0]&&pts[0].config_label?pts[0].config_label:cfgKey;';
        s += '    traces.push({x:cx,y:tpg,name:label,type:"scatter",mode:"lines+markers",line:{color:color,width:2},marker:{color:color,size:8}});';
        s += '  });';
        s += '  Plotly.newPlot(el,traces,{...lo,xaxis:{title:"Concurrent Users"},yaxis:{title:"Throughput per GPU (req/s/GPU)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
        s += '})();';
        s += '(function(){var el=document.getElementById("dl-sweep-cache-hit");if(!el)return;';
        s += '  var traces=[];var ci=0;';
        s += '  Object.keys(csData).forEach(function(cfgKey){';
        s += '    var pts=[].concat(csData[cfgKey]).filter(function(p){return p.cache_hit_pct!=null}).sort(function(a,b){return a.concurrency-b.concurrency});';
        s += '    if(!pts.length)return;';
        s += '    var cx=pts.map(function(p){return p.concurrency});';
        s += '    var ch=pts.map(function(p){return p.cache_hit_pct});';
        s += '    var color=csColors[ci%csColors.length];ci++;';
        s += '    var label=pts[0].config_label||cfgKey;';
        s += '    traces.push({x:cx,y:ch,name:label,type:"scatter",mode:"lines+markers",line:{color:color,width:2},marker:{color:color,size:8}});';
        s += '  });';
        s += '  if(traces.length)Plotly.newPlot(el,traces,{...lo,xaxis:{title:"Concurrent Users"},yaxis:{title:"Cache Hit %",range:[0,105]},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
        s += '})();';
    }

    // Sweep Pareto: Throughput vs Interactivity
    s += '(function(){var el=document.getElementById("dl-sweep-pareto-inter");if(!el||!csData)return;';
    s += '  var allPts=[];';
    s += '  Object.keys(csData).forEach(function(cfgKey){';
    s += '    csData[cfgKey].forEach(function(p){';
    s += '      var tputGpu=p.throughput_per_gpu||0;var inter=p.interactivity||0;';
    s += '      if(tputGpu>0&&inter>0){allPts.push({x:inter,y:tputGpu,label:p.config_label||cfgKey,conc:p.concurrency,arch:cfgKey});}';
    s += '    });';
    s += '  });';
    s += '  if(allPts.length<2)return;';
    s += '  var byArch={};allPts.forEach(function(p){if(!byArch[p.arch])byArch[p.arch]=[];byArch[p.arch].push(p);});';
    s += '  var traces=[];var ci=0;';
    s += '  Object.keys(byArch).forEach(function(arch){';
    s += '    var pts=byArch[arch].sort(function(a,b){return a.x-b.x});';
    s += '    var color=csColors[ci%csColors.length];ci++;';
    s += '    traces.push({x:pts.map(function(p){return p.x}),y:pts.map(function(p){return p.y}),text:pts.map(function(p){return p.label+"<br>c="+p.conc+"<br>"+p.y.toFixed(0)+" tok/s/GPU"}),name:pts[0].label,mode:"lines+markers",line:{color:color,width:2},marker:{color:color,size:10},hovertemplate:"<b>%{text}</b><extra></extra>"});';
    s += '  });';
    s += '  Plotly.newPlot(el,traces,{...lo,height:600,xaxis:{title:"Interactivity (tok/s/user)"},yaxis:{title:"Throughput per GPU (tok/s/GPU)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
    s += '})();';

    // Sweep Pareto: Throughput vs TTFT
    s += '(function(){var el=document.getElementById("dl-sweep-pareto-ttft");if(!el||!csData)return;';
    s += '  var allPts=[];';
    s += '  Object.keys(csData).forEach(function(cfgKey){';
    s += '    csData[cfgKey].forEach(function(p){';
    s += '      var tputGpu=p.throughput_per_gpu||0;var ttft=p.ttft_p90||0;';
    s += '      if(tputGpu>0&&ttft>0){allPts.push({x:ttft,y:tputGpu,label:p.config_label||cfgKey,conc:p.concurrency,arch:cfgKey});}';
    s += '    });';
    s += '  });';
    s += '  if(allPts.length<2)return;';
    s += '  var byArch={};allPts.forEach(function(p){if(!byArch[p.arch])byArch[p.arch]=[];byArch[p.arch].push(p);});';
    s += '  var traces=[];var ci=0;';
    s += '  Object.keys(byArch).forEach(function(arch){';
    s += '    var pts=byArch[arch].sort(function(a,b){return a.x-b.x});';
    s += '    var color=csColors[ci%csColors.length];ci++;';
    s += '    traces.push({x:pts.map(function(p){return p.x}),y:pts.map(function(p){return p.y}),text:pts.map(function(p){return p.label+"<br>c="+p.conc+"<br>TTFT="+p.x.toFixed(0)+"ms"}),name:pts[0].label,mode:"lines+markers",line:{color:color,width:2},marker:{color:color,size:10},hovertemplate:"<b>%{text}</b><extra></extra>"});';
    s += '  });';
    s += '  Plotly.newPlot(el,traces,{...lo,height:600,xaxis:{title:"TTFT P90 (ms)"},yaxis:{title:"Throughput per GPU (tok/s/GPU)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
    s += '})();';

    // Cache Sweep charts
    if (data.cache_sweep && Object.keys(data.cache_sweep).length) {
        s += 'var cacheSweepData=' + JSON.stringify(data.cache_sweep) + ';';
        s += 'var cacheColors=["#8b5cf6","#10b981","#3b82f6","#ef4444","#f59e0b","#0ea5e9","#ec4899","#14b8a6"];';
        s += '[{k:"p90"},{k:"p95"},{k:"p99"}].forEach(function(pctl){';
        s += '  var el=document.getElementById("dl-cache-ttft-"+pctl.k);if(!el)return;';
        s += '  var traces=[];var ci=0;';
        s += '  Object.keys(cacheSweepData).forEach(function(cfgKey){';
        s += '    var pts=[].concat(cacheSweepData[cfgKey]).sort(function(a,b){return(a.hit_pct||0)-(b.hit_pct||0)});';
        s += '    var cx=pts.map(function(p){return p.hit_pct});';
        s += '    var lats=pts.map(function(p){return p["ttft_"+pctl.k]});';
        s += '    var color=cacheColors[ci%cacheColors.length];ci++;';
        s += '    traces.push({x:cx,y:lats,name:cfgKey,type:"scatter",mode:"lines+markers",line:{color:color,width:2},marker:{color:color,size:8}});';
        s += '  });';
        s += '  Plotly.newPlot(el,traces,{...lo,xaxis:{title:"Configured Cache Hit %"},yaxis:{title:"TTFT "+pctl.k.toUpperCase()+" (ms)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
        s += '});';
        s += '(function(){var el=document.getElementById("dl-cache-tput");if(!el)return;';
        s += '  var traces=[];var ci=0;';
        s += '  Object.keys(cacheSweepData).forEach(function(cfgKey){';
        s += '    var pts=[].concat(cacheSweepData[cfgKey]).sort(function(a,b){return(a.hit_pct||0)-(b.hit_pct||0)});';
        s += '    var cx=pts.map(function(p){return p.hit_pct});';
        s += '    var tps=pts.map(function(p){return p.throughput_mean});';
        s += '    var color=cacheColors[ci%cacheColors.length];ci++;';
        s += '    traces.push({x:cx,y:tps,name:cfgKey,type:"scatter",mode:"lines+markers",line:{color:color,width:2},marker:{color:color,size:8}});';
        s += '  });';
        s += '  Plotly.newPlot(el,traces,{...lo,xaxis:{title:"Configured Cache Hit %"},yaxis:{title:"Throughput Mean (req/s)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
        s += '})();';
        s += '(function(){var el=document.getElementById("dl-cache-actual-hit");if(!el)return;';
        s += '  var traces=[];var ci=0;';
        s += '  Object.keys(cacheSweepData).forEach(function(cfgKey){';
        s += '    var pts=[].concat(cacheSweepData[cfgKey]).filter(function(p){return p.actual_hit_rate!=null}).sort(function(a,b){return(a.hit_pct||0)-(b.hit_pct||0)});';
        s += '    if(!pts.length)return;';
        s += '    var cx=pts.map(function(p){return p.hit_pct});';
        s += '    var ahr=pts.map(function(p){return p.actual_hit_rate});';
        s += '    var color=cacheColors[ci%cacheColors.length];ci++;';
        s += '    traces.push({x:cx,y:ahr,name:cfgKey,type:"scatter",mode:"lines+markers",line:{color:color,width:2},marker:{color:color,size:8}});';
        s += '  });';
        s += '  traces.push({x:[0,100],y:[0,100],name:"Ideal (y=x)",type:"scatter",mode:"lines",line:{color:"#94a3b8",width:1,dash:"dash"},showlegend:true});';
        s += '  if(traces.length>1)Plotly.newPlot(el,traces,{...lo,xaxis:{title:"Configured Cache Hit %",range:[-5,105]},yaxis:{title:"Actual Hit Rate",range:[-5,105]},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
        s += '})();';
    }

    // Traffic charts
    {
        const hasTraffic = allRes.some(r => r.request_total != null || r.request_errored != null);
        if (hasTraffic) {
            const trafficGroupMap = {};
            const trafficPrefixes = ['step11-sweep', 'step13', 'step9', 'step7', 'step6', 'step3', 'step2'];
            allRes.forEach(r => {
                if (!r.test_id) return;
                let gk = 'other';
                for (const p of trafficPrefixes) {
                    if (r.test_id.startsWith(p)) { gk = p; break; }
                }
                if (!trafficGroupMap[gk]) trafficGroupMap[gk] = [];
                trafficGroupMap[gk].push({
                    label: r.config_name || r.test_id || '?',
                    request_total: r.request_total || 0,
                    request_errored: r.request_errored || 0,
                    nixl_errors: r.nixl_errors || 0
                });
            });
            if (trafficGroupMap['step2'] || trafficGroupMap['step3']) {
                trafficGroupMap['tp-cal'] = (trafficGroupMap['step2'] || []).concat(trafficGroupMap['step3'] || []);
                delete trafficGroupMap['step2'];
                delete trafficGroupMap['step3'];
            }
            const trafficLabelMap = {'tp-cal':'TP Calibration','step6':'Aggregated','step7':'PD/EP','step9':'EPP Tuning','step11-sweep':'Concurrency Sweep','step13':'Cache Sweep','other':'Other'};
            s += 'var trafGrps=' + JSON.stringify(trafficGroupMap) + ';';
            s += 'var trafLabels=' + JSON.stringify(trafficLabelMap) + ';';
            s += '["tp-cal","step6","step7","step9","step11-sweep","step13","other"].forEach(function(gk){';
            s += '  if(!trafGrps[gk])return;';
            s += '  var el=document.getElementById("dl-traffic-"+gk);if(!el)return;';
            s += '  var pts=trafGrps[gk];';
            s += '  var labels=pts.map(function(r){return r.label});';
            s += '  var traces=[{x:labels,y:pts.map(function(r){return r.request_total}),name:"Requests",type:"scatter",mode:"lines+markers",line:{color:"#3b82f6",width:2},marker:{color:"#3b82f6",size:8}},';
            s += '    {x:labels,y:pts.map(function(r){return r.request_errored}),name:"Errors",type:"scatter",mode:"lines+markers",line:{color:"#ef4444",width:2},marker:{color:"#ef4444",size:8}}];';
            s += '  var hasNixl=pts.some(function(r){return r.nixl_errors>0});';
            s += '  if(hasNixl){traces.push({x:labels,y:pts.map(function(r){return r.nixl_errors}),name:"NIXL Retries",type:"scatter",mode:"lines+markers",line:{color:"#f59e0b",width:2},marker:{color:"#f59e0b",size:8}});}';
            s += '  var label=trafLabels[gk]||gk;';
            s += '  Plotly.newPlot(el,traces,{...lo,height:400,margin:{...lo.margin,b:100},xaxis:{tickangle:-35},yaxis:{title:"Count"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"},title:{text:"Traffic: "+label}},co);';
            s += '});';
        }
    }

    // Deploy Timing chart
    s += '(function(){var el=document.getElementById("dl-deploy-timing");if(!el)return;';
    s += '  var timings=[];ar.forEach(function(r){var dt=r.deploy_timing;if(dt&&(dt.pod_creation_s!=null||dt.model_load_s!=null)){timings.push({config:r.config_name||r.test_id||"Unknown",pod:dt.pod_creation_s||0,model:dt.model_load_s||0});}});';
    s += '  if(!timings.length)return;';
    s += '  timings.sort(function(a,b){return(a.pod+a.model)-(b.pod+b.model)});';
    s += '  var labels=timings.map(function(t){return t.config});';
    s += '  Plotly.newPlot(el,[{x:labels,y:timings.map(function(t){return t.pod}),name:"Pod Creation",type:"bar",marker:{color:"#3b82f6"}},{x:labels,y:timings.map(function(t){return t.model}),name:"Model Load",type:"bar",marker:{color:"#f59e0b"}}],{...vl,barmode:"stack",title:{text:"Deploy Time per Configuration"},xaxis:{tickangle:-45},yaxis:{title:"Time (seconds)"}},co);';
    s += '})();';

    // Pareto Frontier charts
    s += '(function(){';
    s += '  var runOsl=' + JSON.stringify((data.recommendation && data.recommendation.workload) ? data.recommendation.workload.osl : 100) + ';';
    s += '  var paretoResults=ar.filter(function(r){var tid=r.test_id||r.config_name||"";return tid.indexOf("step2-")!==0&&tid.indexOf("step3-")!==0&&r.ttft_p90>0&&(r.throughput_mean>0||r.throughput_p90>0)});';
    s += '  if(paretoResults.length<2)return;';

    // Throughput vs TTFT
    s += '  var ttftEl=document.getElementById("dl-pareto-ttft");';
    s += '  if(ttftEl){';
    s += '    var pts=paretoResults.map(function(r){var tput=r.throughput_mean||r.throughput_p90||0;var tpsGpu=tput*runOsl/(r.gpus||1);return{x:r.ttft_p90,y:tpsGpu,label:r.config_name,arch:r.architecture}}).filter(function(p){return p.x>0&&p.y>0});';
    s += '    var pdPts=pts.filter(function(p){return p.arch==="PD"||p.arch==="EP"});';
    s += '    var aggPts=pts.filter(function(p){return p.arch==="AGGREGATED"});';
    s += '    function ttftFrontier(points){if(!points.length)return[];var s=points.slice().sort(function(a,b){return a.x-b.x});var f=[];var maxY=-Infinity;for(var i=0;i<s.length;i++){if(s[i].y>maxY){f.push(s[i]);maxY=s[i].y}}f.sort(function(a,b){return a.x-b.x});return f}';
    s += '    var aggF=ttftFrontier(aggPts);var pdF=ttftFrontier(pdPts);';
    // Auto-zoom: P95 of X
    s += '    var allX=pts.map(function(p){return p.x}).sort(function(a,b){return a-b});';
    s += '    var p95Idx=Math.min(Math.floor(allX.length*0.95),allX.length-1);';
    s += '    var xMax=allX[p95Idx]*1.2;';
    s += '    var fMaxX=0;aggF.concat(pdF).forEach(function(p){if(p.x>fMaxX)fMaxX=p.x});';
    s += '    if(fMaxX>xMax)xMax=fMaxX*1.1;';
    s += '    var traces=[];';
    s += '    if(aggPts.length){var va=aggPts.filter(function(p){return p.x<=xMax});traces.push({x:va.map(function(p){return p.x}),y:va.map(function(p){return p.y}),text:va.map(function(p){return p.label+"<br>TTFT: "+p.x.toFixed(0)+"ms<br>"+p.y.toFixed(0)+" tok/s/GPU"}),name:"Aggregated",mode:"markers",marker:{color:"#fca5a5",size:14,opacity:0.5},hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '    if(pdPts.length){var vp=pdPts.filter(function(p){return p.x<=xMax});traces.push({x:vp.map(function(p){return p.x}),y:vp.map(function(p){return p.y}),text:vp.map(function(p){return p.label+"<br>TTFT: "+p.x.toFixed(0)+"ms<br>"+p.y.toFixed(0)+" tok/s/GPU"}),name:"Disaggregation",mode:"markers",marker:{color:"#93c5fd",size:14,opacity:0.5},hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '    if(aggF.length){traces.push({x:aggF.map(function(p){return p.x}),y:aggF.map(function(p){return p.y}),text:aggF.map(function(p){return p.label}),name:"Aggregated Frontier",mode:aggF.length>1?"lines+markers+text":"markers+text",line:{color:"#dc2626",width:3},marker:{color:"#dc2626",size:8},textposition:"top right",textfont:{size:10,color:"#dc2626"},hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '    if(pdF.length){traces.push({x:pdF.map(function(p){return p.x}),y:pdF.map(function(p){return p.y}),text:pdF.map(function(p){return p.label}),name:"Disaggregation Frontier",mode:pdF.length>1?"lines+markers+text":"markers+text",line:{color:"#2563eb",width:3},marker:{color:"#2563eb",size:8},textposition:"top left",textfont:{size:10,color:"#2563eb"},hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '    Plotly.newPlot(ttftEl,traces,{...lo,height:700,xaxis:{title:"TTFT P90 (ms)",gridcolor:"#d1d5db",range:[0,xMax]},yaxis:{title:"Token Throughput per GPU (tok/s/GPU)",gridcolor:"#d1d5db",rangemode:"tozero"},showlegend:true,legend:{x:1.02,y:1,xanchor:"left",bgcolor:"rgba(255,255,255,0.95)"},margin:{t:40,b:70,l:70,r:200},plot_bgcolor:"white",paper_bgcolor:"white"},co);';
    s += '  }';

    // Throughput vs Interactivity
    s += '  var intEl=document.getElementById("dl-pareto-interactivity");';
    s += '  if(intEl){';
    s += '    var iPts=paretoResults.map(function(r){var tput=r.throughput_mean||r.throughput_p90||0;var conc=r.concurrency||100;var totalTps=r.output_tps_mean||(tput*runOsl);var inter=totalTps/conc;var tpsGpu=totalTps/(r.gpus||1);return{x:inter,y:tpsGpu,label:r.config_name,arch:r.architecture,conc:conc}}).filter(function(p){return p.x>0&&p.y>0});';
    s += '    var iPd=iPts.filter(function(p){return p.arch==="PD"||p.arch==="EP"});';
    s += '    var iAgg=iPts.filter(function(p){return p.arch==="AGGREGATED"});';
    s += '    function intFrontier(points){if(!points.length)return[];var s=points.slice().sort(function(a,b){return b.x-a.x});var f=[];var maxY=-Infinity;for(var i=0;i<s.length;i++){if(s[i].y>maxY){f.push(s[i]);maxY=s[i].y}}f.sort(function(a,b){return a.x-b.x});return f}';
    s += '    var iAggF=intFrontier(iAgg);var iPdF=intFrontier(iPd);';
    s += '    var iTraces=[];';
    s += '    if(iAgg.length){iTraces.push({x:iAgg.map(function(p){return p.x}),y:iAgg.map(function(p){return p.y}),text:iAgg.map(function(p){return p.label+"<br>"+p.x.toFixed(1)+" tok/s/user<br>"+p.y.toFixed(0)+" tok/s/GPU<br>c="+p.conc}),name:"Aggregated",mode:"markers",marker:{color:"#fca5a5",size:14,opacity:0.5},hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '    if(iPd.length){iTraces.push({x:iPd.map(function(p){return p.x}),y:iPd.map(function(p){return p.y}),text:iPd.map(function(p){return p.label+"<br>"+p.x.toFixed(1)+" tok/s/user<br>"+p.y.toFixed(0)+" tok/s/GPU<br>c="+p.conc}),name:"Disaggregation",mode:"markers",marker:{color:"#93c5fd",size:14,opacity:0.5},hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '    if(iAggF.length>1){iTraces.push({x:iAggF.map(function(p){return p.x}),y:iAggF.map(function(p){return p.y}),name:"Aggregated Frontier",mode:"lines",line:{color:"#dc2626",width:3,dash:"dot"},hoverinfo:"skip"});}';
    s += '    if(iPdF.length>1){iTraces.push({x:iPdF.map(function(p){return p.x}),y:iPdF.map(function(p){return p.y}),name:"Disaggregation Frontier",mode:"lines",line:{color:"#2563eb",width:3,dash:"dot"},hoverinfo:"skip"});}';
    s += '    Plotly.newPlot(intEl,iTraces,{...lo,height:700,xaxis:{title:"Interactivity (tok/s/user)",gridcolor:"#d1d5db",rangemode:"tozero"},yaxis:{title:"Token Throughput per GPU (tok/s/GPU)",gridcolor:"#d1d5db",rangemode:"tozero"},showlegend:true,legend:{x:1.02,y:1,xanchor:"left",bgcolor:"rgba(255,255,255,0.95)"},margin:{t:40,b:70,l:70,r:200},plot_bgcolor:"white",paper_bgcolor:"white"},co);';
    s += '  }';
    s += '})();';

    // Hide non-active panes after rendering
    s += 'setTimeout(function(){document.querySelectorAll(".dl-pane").forEach(function(p){p.style.display="";});},100);';

    s += '<\/script>';
    return s;
}
