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

    // Fetch modular tab sources (web/static/report/) for offline embedding —
    // the same files the live page loads, so there is exactly one
    // implementation of each tab.
    const moduleSources = {};
    await Promise.all([
        '/static/report/core/registry.js',
        '/static/report/core/helpers.js',
        '/static/report/core/context.js',
        '/static/report/tabs/pod-profiles.js',
        '/static/report/tabs/workloads.js',
        '/static/report/tabs/deploy-timing.js',
        '/static/report/tabs/test-settings.js',
        '/static/report/tabs/pareto.js',
        '/static/report/tabs/traffic.js',
        '/static/report/tabs/vllm-metrics.js',
        '/static/report/tabs/estimator.js',
        '/static/report/tabs/lora.js',
        '/static/report/tabs/epp-tuning.js',
    ].map(async (f) => {
        try {
            const r = await fetch(f, { cache: 'reload' });
            if (r.ok) moduleSources[f] = await r.text();
        } catch (e) { /* module missing — tab renders empty offline */ }
    }));

    const html = buildFullReport(runId, data, charts, rec, summary, best, allRes, hasPD, hasVLLM, moduleSources);

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

// Build a modular tab's section with the download context ('' when the
// tab module is not loaded or builds nothing).
function _tabSection(tabId, data, runId) {
    if (typeof ReportTabs === 'undefined' || !ReportTabs.get(tabId)) return '';
    try {
        return ReportTabs.buildTab(ReportTabs.get(tabId), data, ReportCtx.download(runId));
    } catch (e) {
        console.warn('tab build failed: ' + tabId, e);
        return '';
    }
}

function buildFullReport(runId, data, charts, rec, summary, best, allRes, hasPD, hasVLLM, moduleSources) {
    let secRec = buildRecSection(runId, data, rec, summary, best, allRes);
    // Append calibrated load recommendations to the Recommendation tab
    const calRecCards = buildCalRecCards(data);
    if (calRecCards) secRec += calRecCards;
    const cacheRecCards = buildCacheRecCards(data);
    if (cacheRecCards) secRec += cacheRecCards;
    // Stat cards and percentile breakdown go at the very bottom of the rec tab
    secRec += buildRecFooter(rec, summary, best);
    const secTP = buildTPSection(rec, charts);
    const secCfg = buildCfgSection(runId, data, charts, allRes, hasPD);
    const secCmp = buildCmpSection(runId, rec, data);
    const secStep9 = buildStep9Section(data);
    const secCal = buildCalSection(data);
    const secVLLM = _tabSection('vllm-metrics', data, runId);
    const secEpp = _tabSection('epp-tuning', data, runId);
    const secTestCfg = _tabSection('test-settings', data, runId);
    const secSweep = buildConcurrencySweepSection(data);
    const secCacheSweep = buildCacheSweepSection(data, allRes);
    const secTraffic = _tabSection('traffic', data, runId);
    const secDeploy = _tabSection('deploy-timing', data, runId);
    const secPareto = _tabSection('pareto-frontier', data, runId);
    const secLora = _tabSection('lora-serving', data, runId);
    const secEst = _tabSection('estimator', data, runId);
    const secWorkloads = _tabSection('workloads', data, runId);
    // Modular tab: pod-profiles (web/static/report/tabs/) — one implementation
    // shared with the live report; built here with the download context.
    let secProfiles = '';
    if (typeof ReportTabs !== 'undefined' && ReportTabs.get('pod-profiles')) {
        secProfiles = ReportTabs.buildTab(ReportTabs.get('pod-profiles'), data, ReportCtx.download(runId));
    }

    const dlTabs = [];
    if (secRec) dlTabs.push({ id: 'rec', label: '&#9733; Recommendation', html: secRec });
    if (secTP) dlTabs.push({ id: 'tp', label: '&#9881; TP Calibration', html: secTP });
    if (secCfg) dlTabs.push({ id: 'cfg', label: '&#9776; Configurations', html: secCfg });
    if (secCmp) dlTabs.push({ id: 'cmp', label: '&#8596; Comparison', html: secCmp });
    if (secStep9) dlTabs.push({ id: 'step9', label: '&#128269; Latency Search', html: secStep9 });
    if (secCal) dlTabs.push({ id: 'cal', label: '&#9878; Calibrated Load', html: secCal });
    if (secSweep) dlTabs.push({ id: 'sweep', label: '&#128200; Concurrency Sweep', html: secSweep });
    if (secCacheSweep) dlTabs.push({ id: 'cachesweep', label: '&#128203; Cache Sweep', html: secCacheSweep });
    if (secLora) dlTabs.push({ id: 'lora', label: '&#127919; LoRA Serving', html: secLora });
    if (secVLLM) dlTabs.push({ id: 'vllm', label: '&#9889; vLLM Metrics', html: secVLLM });
    if (secEpp) dlTabs.push({ id: 'epp', label: '&#9881; EPP Tuning', html: secEpp });
    if (secPareto) dlTabs.push({ id: 'pareto', label: '&#128200; Pareto Frontier', html: secPareto });
    if (secTraffic) dlTabs.push({ id: 'traffic', label: '&#128230; Traffic', html: secTraffic });
    if (secDeploy) dlTabs.push({ id: 'deploy', label: '&#9202; Deploy Timing', html: secDeploy });
    if (secTestCfg) dlTabs.push({ id: 'settings', label: '&#9881; Test Settings', html: secTestCfg });
    if (secEst) dlTabs.push({ id: 'estimator', label: '&#128200; Estimator', html: secEst });
    if (secProfiles) dlTabs.push({ id: 'profiles', label: '&#128300; Pod Profiles', html: secProfiles });
    if (secWorkloads) dlTabs.push({ id: 'workloads', label: '&#128203; Workloads', html: secWorkloads });

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
    // Modular tab sources + offline bootstrap — runs AFTER the chart script
    // so `ar` and the blob maps are already defined. The registry + tab
    // modules are the same files the live page loads.
    // Emit core before tabs: tabs call ReportTabs.register at load time,
    // so the registry (and helpers/context) must be defined first — same
    // order as the live page's script tags.
    const modFiles = Object.keys(moduleSources || {}).sort((a, b) => {
        const rank = f => f.indexOf('/core/') >= 0 ? 0 : 1;
        return rank(a) - rank(b) || a.localeCompare(b);
    });
    if (modFiles.length) {
        out += '<script>';
        function safeSrc(t) { return t.replace(/<\/script>/g, '<\\/script>'); }
        modFiles.forEach(f => { out += '\n/* ==== ' + f + ' ==== */\n' + safeSrc(moduleSources[f]) + '\n'; });
        out += 'ReportTabs.all().forEach(function(t) { if (t.render) { try { t.render({ all_results: ar }, ReportCtx.download(' + runId + ')); } catch (e) { console.warn("offline render failed: " + t.id, e); } } });';
        out += '<\/script>';
    }
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
.dl-pane{padding-top:16px}
.dl-pane-ready .dl-pane{display:none}
.dl-pane-ready .dl-pane.active{display:block}
.section-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:24px;font-size:0.9em}
.section-hdr{font-weight:700;color:#1e293b;margin-bottom:10px;padding-bottom:4px}
</style></head><body>`;
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function fmtItl(v) { return v != null ? parseFloat(v).toFixed(2) + ' ms' : '-'; }
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
        const gColors = { ttft: '#3b82f6', throughput: '#f59e0b', balanced: '#10b981', aggregated_only: '#64748b', pd_only: '#8b5cf6', ep_only: '#0ea5e9', dpep_only: '#14b8a6' };
        const gIcons = { ttft: '&#9201;', throughput: '&#9889;', balanced: '&#9878;', aggregated_only: '&#9634;', pd_only: '&#8644;', ep_only: '&#9881;', dpep_only: '&#9635;' };
        const gc = gColors[rec.goal] || '#10b981';
        s += `<div style="border:3px solid ${gc};border-left:8px solid ${gc};border-radius:10px;margin:20px 0;overflow:hidden;">`;
        s += `<div style="background:${gc};color:white;padding:14px 20px;font-size:1.3em;font-weight:800;">Deployment Recommendation <span style="font-size:0.7em;font-weight:400;opacity:0.85;">&mdash; ${rec.goal_info.name}</span></div>`;
        s += `<div style="background:${gc}dd;color:white;padding:8px 20px;font-size:0.92em;">`;
        const rc2 = data.run_config || {};
        const wCpt = rec.workload.chars_per_token || rc2.chars_per_token || 4.5;
        const wIslC = rec.workload.isl_original_chars || rc2.isl_original_chars || Math.round(rec.workload.isl * wCpt);
        const wOslC = rec.workload.osl_original_chars || rc2.osl_original_chars || Math.round(rec.workload.osl * wCpt);
        const wIslStdC = rec.workload.isl_stdev ? (rec.workload.isl_stdev_original_chars || (rc2.isl_stdev_original_chars) || Math.round(rec.workload.isl_stdev * wCpt)) : null;
        s += `Model: <strong>${rec.model}</strong> &nbsp;|&nbsp; Prompt: <strong>${rec.workload.isl.toLocaleString()}</strong>`;
        if (rec.workload.isl_stdev) s += ` + &sigma;=${rec.workload.isl_stdev.toLocaleString()}`;
        s += ` <span style="opacity:0.8;">(${wIslC.toLocaleString()} chars)</span>`;
        s += ` | Output: <strong>${rec.workload.osl.toLocaleString()}</strong>`;
        if (rec.workload.osl_stdev) s += ` + &sigma;=${rec.workload.osl_stdev.toLocaleString()}`;
        s += ` <span style="opacity:0.8;">(${wOslC.toLocaleString()} chars)</span>`;
        var _rc2 = data.run_config || {};
        var _dlTurns = rec.workload.turns > 1 ? rec.workload.turns : (_rc2.turns > 1 ? _rc2.turns : null);
        if (_dlTurns) s += ` | Turns: <strong>${_dlTurns}</strong>`;
        if (_rc2.first_prompt_tokens) s += ` | 1st Prompt: <strong>${_rc2.first_prompt_tokens.toLocaleString()}</strong>` + (_rc2.first_prompt_tokens_stdev ? ` &sigma;=${_rc2.first_prompt_tokens_stdev.toLocaleString()}` : '');
        if (_rc2.prefix_tokens) s += ` | Prefix: <strong>${_rc2.prefix_tokens.toLocaleString()}</strong>&times;${_rc2.prefix_count || 1}`;
        if (_dlTurns && _rc2.turn_delay) s += ` | Delay: <strong>${_rc2.turn_delay}s</strong>` + (_rc2.turn_delay_stdev ? ` &sigma;=${_rc2.turn_delay_stdev}s` : '');
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
            { key: 'lowest_itl', label: 'Lowest ITL', desc: 'Smoothest token streaming', color: '#0891b2', icon: '&#9881;' },
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
            s += '<div style="display:flex;gap:12px;font-size:0.85em;color:#475569;margin-bottom:4px;flex-wrap:wrap;">';
            s += `<span>Throughput: <strong>${typeof tput === 'number' ? tput.toFixed(2) + ' req/s' : tput}</strong></span>`;
            s += `<span>GPUs: <strong>${gpus}</strong></span>`;
            const userConc = rec.workload ? rec.workload.users : null;
            if (userConc) s += `<span style="color:#0ea5e9;font-weight:600;">c=${userConc}</span>`;
            s += '</div>';
            // vLLM tok/s line
            const _dlResEntry = allRes.find(r => (r.test_id || r.config_name) === (cfg.test_id || cfg.config_name)) || {};
            const _dlMj = _dlResEntry.metrics_json ? (typeof _dlResEntry.metrics_json === 'string' ? JSON.parse(_dlResEntry.metrics_json) : _dlResEntry.metrics_json) : {};
            const _dlPm = _dlMj.prometheus_metrics || {};
            const _dlVllmTps = ((_dlPm.vllm_prompt_tokens_rate || {}).avg || 0) + ((_dlPm.vllm_generation_tokens_rate || {}).avg || 0);
            const _dlOutputTps = _dlMj.output_tps_mean || _dlResEntry.output_tps_mean || null;
            if (_dlVllmTps > 0) {
                s += `<div style="font-size:0.8em;color:#475569;margin-bottom:4px;">`;
                s += `<span>vLLM: <strong style="color:#3b82f6;">${Math.round(_dlVllmTps).toLocaleString()} tok/s</strong> <span style="font-size:0.85em;color:#94a3b8;">(server total)</span></span>`;
                s += '</div>';
            }
            // Percentile table — P50 from raw metrics (not in best_by_percentile)
            const _p50d = { ttft_p50: _dlMj.ttft_p50, e2e_p50: _dlMj.e2e_latency_p50 != null ? _dlMj.e2e_latency_p50 * 1000 : null, itl_p50: _dlMj.itl_p50 };
            s += '<table style="width:100%;font-size:0.8em;border-collapse:collapse;margin-top:6px;">';
            s += '<tr style="color:#94a3b8;font-weight:600;"><td></td><td>TTFT</td><td>E2E</td><td>ITL</td></tr>';
            const t50 = _p50d.ttft || _p50d.ttft_p50;
            const t90 = _p90d.ttft || _p90d.ttft_p90;
            const t95 = _p95d.ttft || _p95d.ttft_p95;
            const t99 = _p99d.ttft || _p99d.ttft_p99;
            const fmtE2e = v => v != null ? (v >= 1000 ? (v/1000).toFixed(1) + ' s' : Math.round(v) + ' ms') : '-';
            if (t50 != null) s += `<tr><td style="font-weight:600;color:#94a3b8;">P50</td><td style="color:#64748b;">${Math.round(t50).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(_p50d.e2e_p50)}</td><td style="color:#64748b;">${fmtItl(_p50d.itl || _p50d.itl_p50)}</td></tr>`;
            s += `<tr><td style="font-weight:600;color:#475569;">P90</td><td style="font-weight:700;color:#1e293b;">${t90 != null ? Math.round(t90).toLocaleString() + ' ms' : '-'}</td><td>${fmtE2e(_p90d.e2e_p90)}</td><td>${fmtItl(_p90d.itl || _p90d.itl_p90)}</td></tr>`;
            if (t95) s += `<tr><td style="font-weight:600;color:#475569;">P95</td><td style="color:#64748b;">${Math.round(t95).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(_p95d.e2e_p95)}</td><td>${fmtItl(_p95d.itl || _p95d.itl_p95)}</td></tr>`;
            if (t99) s += `<tr><td style="font-weight:600;color:#475569;">P99</td><td style="color:#64748b;">${Math.round(t99).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(_p99d.e2e_p99)}</td><td>${fmtItl(_p99d.itl || _p99d.itl_p99)}</td></tr>`;
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

        s += '</div>';
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
                    // recommend a VARIANT — the as-run baseline is the reference, not the tuned result
                    const _cands = trials.filter(t => !t.is_baseline);
                    const _pool = _cands.length ? _cands : trials;
                    const b = _pool.length ? _pool.reduce((a, c) => ((a[`ttft_${p}`] || Infinity) < (c[`ttft_${p}`] || Infinity)) ? a : c) : null;
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

    return s;
}

function buildRecFooter(rec, summary, best) {
    let s = '';
    // Summary stat cards
    s += '<div class="stats">';
    s += dlStatCard(summary.successful_tests, 'Successful Tests');
    if (summary.discarded_tests > 0) {
        s += dlStatCard(summary.discarded_tests, 'Discarded Tests (not viable)');
    }
    if (best.lowest_latency) {
        s += dlStatCard(best.lowest_latency.ttft_p90.toFixed(1) + ' ms', 'Best TTFT P90' + (best.lowest_latency.name ? '<br><span style="font-size:0.7em;font-weight:400;color:#94a3b8;">' + best.lowest_latency.name + '</span>' : ''));
        if (best.lowest_latency.ttft_p95) s += dlStatCard(best.lowest_latency.ttft_p95.toFixed(1) + ' ms', 'Best TTFT P95' + (best.lowest_latency.name ? '<br><span style="font-size:0.7em;font-weight:400;color:#94a3b8;">' + best.lowest_latency.name + '</span>' : ''));
        if (best.lowest_latency.ttft_p99) s += dlStatCard(best.lowest_latency.ttft_p99.toFixed(1) + ' ms', 'Best TTFT P99' + (best.lowest_latency.name ? '<br><span style="font-size:0.7em;font-weight:400;color:#94a3b8;">' + best.lowest_latency.name + '</span>' : ''));
    }
    if (best.lowest_itl) {
        s += dlStatCard(best.lowest_itl.itl_p90.toFixed(2) + ' ms', `Best ITL P90 (${best.lowest_itl.name})`);
    }
    if (best.highest_throughput) {
        const htVal = best.highest_throughput.throughput_mean || best.highest_throughput.throughput_p90;
        s += dlStatCard(htVal.toFixed(2) + ' req/s', `Best Throughput Mean (${best.highest_throughput.name})`);
    }
    if (best.most_efficient) s += dlStatCard(best.most_efficient.efficiency.toFixed(3), 'Best Efficiency (req/s/GPU)');
    s += '</div>';

    // Percentile Breakdown: PD/EP vs Aggregated (skip if primary is Aggregated)
    if (rec && rec.recommendations) {
        const primaryKey = (rec.goal === 'ttft' || rec.goal === 'pd_only' || rec.goal === 'full_coverage') ? 'response_time' : 'throughput';
        const primaryRec = rec.recommendations[primaryKey];
        const aggBase = rec.aggregated_baseline;
        const primaryArch = primaryRec && primaryRec.architecture ? primaryRec.architecture : 'PD';
        const hasPrimary = primaryRec && primaryRec.config && primaryRec.config.percentiles;
        const hasAgg = aggBase && aggBase.percentiles && (!primaryRec || aggBase.config_name !== (primaryRec.config || {}).config_name);

        if (hasPrimary && primaryArch.toUpperCase() !== 'AGGREGATED') {
            const p = primaryRec.config.percentiles;
            const a = hasAgg ? aggBase.percentiles : null;
            s += `<div class="chart-box"><h3>Percentile Breakdown: ${primaryArch} vs Aggregated</h3>`;
            s += `<p style="color:#64748b;font-size:0.9em;margin:0 0 8px;">Full percentile distribution (P50 through P99) for TTFT, ITL, and Throughput — comparing the best ${primaryArch} configuration against the Aggregated baseline. Green-highlighted P90 values indicate the winner.</p>`;
            s += '<table><tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';
            const metricDefs = [
                { name: 'TTFT (ms)', key: 'ttft', lowerBetter: true },
                { name: 'ITL (ms)', key: 'itl', lowerBetter: true },
                { name: 'Throughput (req/s)', key: 'throughput', lowerBetter: false },
            ];
            const entries = [{label: primaryArch, pctl: p}];
            if (a) entries.push({label: 'Aggregated', pctl: a});
            entries.forEach(({label, pctl}, idx) => {
                metricDefs.forEach((m, mi) => {
                    const d = pctl[m.key] || {};
                    const bs = mi === 0 && idx > 0 ? 'border-top:2px solid #cbd5e1;' : '';
                    let p90Style = '';
                    if (a) {
                        const other = idx === 0 ? (a[m.key] || {}) : (p[m.key] || {});
                        const wins = m.lowerBetter ? (d.p90 != null && other.p90 != null && d.p90 < other.p90) : (d.p90 != null && other.p90 != null && d.p90 > other.p90);
                        if (wins) p90Style = 'color:#10b981;font-weight:700;';
                    }
                    s += '<tr>';
                    if (mi === 0) s += `<td rowspan="3" style="vertical-align:middle;font-weight:700;${bs}">${label}</td>`;
                    s += `<td style="color:#64748b;${bs}">${m.name}</td>`;
                    s += `<td style="${bs}">${d.p50 ?? '-'}</td>`;
                    s += `<td style="${p90Style}${bs}">${d.p90 ?? '-'}</td>`;
                    s += `<td style="${bs}">${d.p95 ?? '-'}</td>`;
                    s += `<td style="${bs}">${d.p99 ?? '-'}</td>`;
                    s += '</tr>';
                });
            });
            s += '</table></div>';
        }
    }
    return s;
}

// ── TP Calibration Tab ──────────────────────────────────────────────────────
function buildTPSection(rec, charts) {
    if (!rec) return '';
    const hasDecodeTP = rec.decode_tp_all && rec.decode_tp_all.length;
    const hasPrefillTP = rec.prefill_tp_all && rec.prefill_tp_all.length;
    if (!hasDecodeTP && !hasPrefillTP) return '';
    let s = '';
    s += '<div class="grid2"><div class="chart-box"><h3>Decode TP Sweep</h3><div id="tp-dec" style="height:850px"></div></div>';
    s += '<div class="chart-box"><h3>Prefill TP Sweep</h3><div id="tp-pre" style="height:850px"></div></div></div>';
    s += '<div class="chart-box"><h3>TP Calibration (Pareto)</h3><div id="p1" style="height:850px"></div></div>';

    // Calibration parameters table (after all charts)
    const calRows = [];
    if (hasDecodeTP) {
        rec.decode_tp_all.forEach(d => {
            calRows.push({ role: 'Decode', tp: d.tp, isl: d.cal_isl, osl: d.cal_osl, conc: d.cal_concurrency, reqs: d.cal_max_requests, tspg: d.tpsg != null ? d.tpsg : null, ttft: null, itl: d.itl_p90 != null ? d.itl_p90 : null });
        });
    }
    if (hasPrefillTP) {
        rec.prefill_tp_all.forEach(d => {
            calRows.push({ role: 'Prefill', tp: d.tp, isl: d.cal_isl, osl: d.cal_osl, conc: d.cal_concurrency, reqs: d.cal_max_requests, tspg: d.tpsg != null ? d.tpsg : null, ttft: d.ttft_p90 != null ? d.ttft_p90 : null, itl: null });
        });
    }
    if (calRows.length) {
        s += '<div style="margin:16px 0;padding:16px 20px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;">';
        s += '<div style="font-weight:700;font-size:0.95em;color:#1e293b;margin-bottom:8px;">Calibration Test Parameters</div>';
        s += '<p style="color:#64748b;font-size:0.82em;margin:0 0 10px;">Each TP value is tested with isolated workloads: <strong>ISL=1</strong> for decode (measures pure token generation) and <strong>OSL=1</strong> for prefill (measures pure prompt processing). Concurrency is estimated from KV cache capacity to avoid OOM.</p>';
        s += '<table style="width:100%;font-size:0.85em;text-align:center;"><tr><th>Role</th><th>TP</th><th>ISL</th><th>OSL</th><th>Concurrency</th><th>Max Requests</th><th>TSPG (tok/s/GPU)</th><th>ITL P90</th><th>TTFT P90</th></tr>';
        calRows.forEach(r => {
            const cls = r.role === 'Decode' ? ' style="color:#6366f1;text-align:center;"' : ' style="color:#0d9488;text-align:center;"';
            const tspgStr = r.tspg != null ? r.tspg.toLocaleString() : '-';
            const ttftStr = r.ttft != null ? r.ttft.toLocaleString() + ' ms' : '-';
            const itlStr = r.itl != null ? r.itl.toFixed(2) + ' ms' : '-';
            s += `<tr><td${cls}><strong>${r.role}</strong></td><td>${r.tp}</td><td>${r.isl != null ? r.isl.toLocaleString() : '-'}</td><td>${r.osl != null ? r.osl.toLocaleString() : '-'}</td><td>${r.conc || '-'}</td><td>${r.reqs || '-'}</td><td>${tspgStr}</td><td>${itlStr}</td><td>${ttftStr}</td></tr>`;
        });
        s += '</table></div>';
    }

    return s;
}

// ── Configurations Tab ──────────────────────────────────────────────────────
function buildCfgSection(runId, data, charts, allRes, hasPD) {
    if (!allRes || !allRes.length) return '';
    let s = '';

    // Configuration Selection — Top 5 per Architecture (from best_by_percentile)
    const rec = data.recommendation;
    if (rec && rec.best_by_percentile && rec.best_by_percentile.p90) {
        const _bbpSel = rec.best_by_percentile.p90;
        const _recLabel = {};
        if (rec.recommendations) {
            const _rtCfg = rec.recommendations.response_time && rec.recommendations.response_time.config;
            const _tpCfg = rec.recommendations.throughput    && rec.recommendations.throughput.config;
            if (_rtCfg) _recLabel[_rtCfg.config_name] = '&#9733; Primary TTFT recommendation';
            if (_tpCfg) _recLabel[_tpCfg.config_name] = '&#9733; Primary throughput recommendation';
        }
        const _fmtMs = v => v == null ? '—' : (v >= 1000 ? (v/1000).toFixed(1)+'s' : Math.round(v)+'ms');
        const _fmtTput = v => v == null ? '—' : v.toFixed(2)+' req/s';
        const _archDefs = [
            { key: 'pd',         label: 'PD',         color: '#0ea5e9', bg: '#f0f9ff' },
            { key: 'ep',         label: 'EP',         color: '#10b981', bg: '#f0fdf4' },
            { key: 'aggregated', label: 'Aggregated', color: '#6366f1', bg: '#eef2ff' }
        ];
        const _catDefs = [
            { key: 'balanced',       label: 'Best balanced'   },
            { key: 'lowest_ttft',    label: 'Best TTFT'       },
            { key: 'highest_tput',   label: 'Best throughput' },
            { key: 'most_efficient', label: 'Most efficient'  },
            { key: 'lowest_itl',     label: 'Best ITL'        }
        ];
        if (_archDefs.some(a => !!_bbpSel[a.key])) {
            s += '<div class="chart-box"><h3>Configuration Selection — Top 5 per Architecture</h3>';
            s += '<p style="color:#64748b;font-size:0.82em;margin:0 0 10px;">The optimizer selects up to 5 configurations per architecture for the concurrency sweep — one per category. &#9733; = final primary recommendation.</p>';
            _archDefs.forEach(arch => {
                const ad = _bbpSel[arch.key];
                if (!ad) return;
                s += '<div style="margin-bottom:18px;">';
                s += `<div style="font-weight:700;font-size:0.85em;color:${arch.color};text-transform:uppercase;letter-spacing:0.06em;border-bottom:2px solid ${arch.color};padding-bottom:4px;margin-bottom:6px;">${arch.label} Architecture</div>`;
                s += '<table style="width:100%;font-size:0.83em;table-layout:fixed;"><colgroup><col style="width:28%"><col style="width:22%"><col style="width:13%"><col style="width:11%"><col style="width:15%"><col style="width:11%"></colgroup>';
                s += '<tr><th>Category</th><th>Config</th><th>TTFT P90</th><th>ITL P90</th><th>Throughput</th><th>GPUs</th></tr>';
                const _seen = {}, _deduped = [];
                _catDefs.forEach(cat => {
                    const e = ad[cat.key];
                    if (!e) return;
                    const cn = e.config_name;
                    if (_seen[cn]) { _seen[cn].cats.push(cat.label); }
                    else { _seen[cn] = { entry: e, cats: [cat.label] }; _deduped.push(cn); }
                });
                _deduped.forEach(cn => {
                    const e = _seen[cn].entry;
                    const recLbl = _recLabel[cn];
                    const isRec = !!recLbl;
                    const catBadges = _seen[cn].cats.map(lbl =>
                        `<span style="display:inline-block;background:${arch.bg};color:${arch.color};font-size:0.78em;font-weight:600;padding:1px 7px;border-radius:8px;margin:1px 2px 1px 0;">${lbl}</span>`).join('');
                    const recBadge = isRec ? ` <span style="display:inline-block;background:#fef3c7;color:#b45309;font-size:0.78em;font-weight:700;padding:1px 7px;border-radius:8px;">${recLbl}</span>` : '';
                    s += `<tr${isRec ? ' style="background:#fffbeb;"' : ''}><td>${catBadges}${recBadge}</td>` +
                        `<td style="font-weight:${isRec ? '700' : '400'};">${cn}</td>` +
                        `<td>${_fmtMs(e.ttft)}</td><td>${_fmtMs(e.itl)}</td>` +
                        `<td>${_fmtTput(e.throughput_mean || e.throughput)}</td>` +
                        `<td>${e.gpus != null ? e.gpus : '—'}</td></tr>`;
                });
                s += '</table></div>';
            });
            s += '</div>';
        }
    }

    s += '<div class="grid2"><div class="chart-box"><h3>Throughput vs Latency</h3><div id="p2" style="height:850px"></div></div>';
    s += '<div class="chart-box"><h3>GPU Efficiency</h3><div id="p3" style="height:850px"></div></div></div>';
    if (charts.per_user_efficiency && charts.per_user_efficiency.configs.length) {
        s += '<div class="chart-box"><h3>Per-User Token Throughput (tokens/s per user)</h3>';
        s += '<p style="color:#64748b;font-size:0.9em;margin:0 0 8px;">Shows how many output tokens each concurrent user receives per second. <strong>Higher = faster streaming for each user.</strong></p>';
        s += '<div id="dl-cfg-per-user" style="height:850px"></div></div>';
    }
    s += '<div class="chart-box"><h3>Architecture Comparison</h3><div id="p4" style="height:850px"></div></div>';

    // Cache hit rate chart
    const cacheData = allRes.filter(r => r.cache_hit_pct != null);
    if (cacheData.length) {
        s += '<div class="chart-box"><h3>Prefix Cache Hit Rate per Configuration</h3>';
        s += '<p style="color:#64748b;font-size:0.9em;margin:0 0 8px;">Actual prefix cache hit percentage measured by vLLM during each test.</p>';
        s += '<div id="dl-cfg-cache-hit" style="height:850px"></div></div>';
    }

    // Aggregated configurations chart (all percentiles in one chart)
    if (allRes.filter(r => r.quality !== 'discard' && r.architecture === 'AGGREGATED').length > 1) {
        s += '<div class="chart-box"><h3>Aggregated Configurations — TTFT &amp; Throughput (P90 / P95 / P99)</h3>';
        s += '<p style="color:#64748b;font-size:0.9em;margin:0 0 8px;">All aggregated configurations with <strong style="color:#3b82f6">TTFT</strong> (bars, lower is better) and <strong style="color:#f59e0b">Throughput Mean</strong> (line, right axis, higher is better) across percentiles.</p>';
        s += '<div id="dl-cfg-agg-ttft" style="height:850px"></div></div>';
    }

    // Per-percentile PD charts
    if (hasPD) {
        ['P90', 'P95', 'P99'].forEach(p => {
            s += `<div class="chart-box"><h3>PD Configurations &mdash; TTFT &amp; Throughput (${p})</h3><div id="pd-ttft-${p.toLowerCase()}" style="height:850px"></div></div>`;
        });
    }

    // Pareto optimal table
    if (charts.pareto.pareto_table.length) {
        const mlbl = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
        const _mtByTest = {};
        allRes.forEach(r => { if (r.test_id && r.manifest_types) _mtByTest[r.test_id] = r.manifest_types; });
        s += '<div class="chart-box"><h3>Pareto Optimal Configurations</h3><table><tr><th>Config</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>GPUs</th><th>Efficiency</th><th>Manifests</th></tr>';
        charts.pareto.pareto_table.forEach((p, idx) => {
            const bt = idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
            let mLinks = '-';
            const mtypes = p.test_id ? _mtByTest[p.test_id] : null;
            if (mtypes && mtypes.length) {
                mLinks = mtypes.filter(t => !t.includes('service')).map(t =>
                    `<a href="#" onclick="dlManifest('${p.test_id}','${t}');return false;" title="Download ${t}.yaml" style="color:#0ea5e9;text-decoration:none;font-size:11px;padding:2px 6px;background:#f0f9ff;border-radius:4px;border:1px solid #bae6fd;display:inline-block;margin:1px;cursor:pointer;">${mlbl[t] || t} &#8595;</a>`).join(' ');
            }
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
                const fmtV = v => v == null ? '-' : mi === 1 ? parseFloat(v).toFixed(2) : (mi === 2 ? parseFloat(v).toFixed(2) : Math.round(v).toLocaleString());
                s += `<td style="color:#64748b;${bs}">${m.name}</td>`;
                s += `<td style="${bs}">${fmtV(m.p50)}</td><td style="${bs}">${fmtV(m.p90)}</td><td style="${bs}">${fmtV(m.p95)}</td><td style="${bs}">${fmtV(m.p99)}</td>`;
                if (mi === 0) s += `<td rowspan="3" style="vertical-align:middle;${bs}">${p.gpus}</td><td rowspan="3" style="vertical-align:middle;${bs}">${p.efficiency}</td><td rowspan="3" style="vertical-align:middle;${bs}">${mLinks}</td>`;
                s += '</tr>';
            });
        });
        s += '</table></div>';
    }

    // Token Throughput table (exclude calibration/sweep tests)
    const tokenRes = allRes.filter(r => {
        if (r.quality === 'discard') return false;
        const tid = r.test_id || '';
        if (tid.indexOf('step2-') === 0 || tid.indexOf('step3-') === 0 ||
            tid.indexOf('step11-') === 0 || tid.indexOf('step12-') === 0 || tid.indexOf('step13-') === 0) return false;
        const m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
        const pm = m.prometheus_metrics || {};
        return r.output_tps_mean || m.output_tps_mean || pm.vllm_generation_tokens_rate;
    });
    if (tokenRes.length) {
        const ttid = 'dl-token-tput';
        s += `<div class="chart-box"><h3>Token Throughput — vLLM (computed) vs guidellm (all tokens)</h3>`;
        s += `<p style="color:#64748b;font-size:0.9em;margin:0 0 8px;line-height:1.5;">` +
            `<strong>Blue bar (vLLM):</strong> Tokens that required actual GPU compute — prompt tokens that were <em>not</em> served from prefix cache, plus generated output tokens. Measured server-side via Prometheus (<code>vllm:prompt_tokens</code> + <code>vllm:generation_tokens</code>). Cached/transferred tokens are excluded.<br>` +
            `<strong>Gold bar (guidellm):</strong> All tokens in every request — the full prompt length (including tokens served from cache) plus output tokens. Measured client-side by guidellm.<br>` +
            `<strong>The gap between the bars reflects prefix cache efficiency.</strong> With a high cache hit rate, most prompt tokens are served from cache without GPU compute, so the vLLM bar is much smaller than the guidellm bar.</p>`;
        s += `<div id="dl-token-tput-chart" style="height:850px;margin-bottom:16px;"></div>`;
        s += `</div>`;


        s += `<div class="chart-box"><h3>Throughput &amp; Utilization Details</h3>`;
        s += `<table id="${ttid}"><tr>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',0,'str')">Configuration &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',1,'num')">Prompt tok/s &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">avg (peak)</span></th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',2,'num')">Generation tok/s &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">avg (peak)</span></th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',3,'num')">Total tok/s &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',4,'num')">vLLM req/s &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">server-side</span></th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',5,'num')">guidellm req/s &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">client-side</span></th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',6,'num')">Avg Running &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',7,'num')">Avg Waiting &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',8,'num')">Cache Hit &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${ttid}',9,'num')">KV Cache &#x21C5;</th>`;
        s += '</tr>';
        tokenRes.forEach(r => {
            const m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
            const pm = m.prometheus_metrics || {};
            const promptRate = pm.vllm_prompt_tokens_rate || {};
            const genRate = pm.vllm_generation_tokens_rate || {};
            const promptAvg = promptRate.avg != null ? Math.round(promptRate.avg) : null;
            const promptMax = promptRate.max != null ? Math.round(promptRate.max) : null;
            const genAvg = genRate.avg != null ? Math.round(genRate.avg) : null;
            const genMax = genRate.max != null ? Math.round(genRate.max) : null;
            const totalAvg = (promptAvg != null && genAvg != null) ? promptAvg + genAvg : null;
            const vllmReqRate = (pm.vllm_e2e_count_rate || {}).avg;
            const vllmReqStr = vllmReqRate != null ? vllmReqRate.toFixed(2) : 'N/A';
            const guidellmReq = r.throughput_mean != null ? r.throughput_mean.toFixed(2) : 'N/A';
            const avgRunning = (pm.vllm_requests_running || {}).avg;
            const avgWaiting = (pm.vllm_requests_waiting || {}).avg;
            const cacheHit = r.cache_hit_pct != null ? r.cache_hit_pct + '%' : 'N/A';
            const kvUsage = m.kv_cache_usage != null ? (m.kv_cache_usage * 100).toFixed(1) + '%' : 'N/A';
            const na = 'N/A';
            const fmtRate = (avg, max) => avg != null ? `${avg.toLocaleString()} (${max != null ? max.toLocaleString() : '?'})` : na;
            s += `<tr><td>${r.config_name}</td>`;
            s += `<td data-val="${promptAvg ?? ''}">${fmtRate(promptAvg, promptMax)}</td>`;
            s += `<td data-val="${genAvg ?? ''}">${fmtRate(genAvg, genMax)}</td>`;
            s += `<td data-val="${totalAvg ?? ''}">${totalAvg != null ? totalAvg.toLocaleString() : na}</td>`;
            s += `<td data-val="${vllmReqRate ?? ''}">${vllmReqStr}</td>`;
            s += `<td data-val="${r.throughput_mean ?? ''}">${guidellmReq}</td>`;
            s += `<td data-val="${avgRunning ?? ''}">${avgRunning != null ? avgRunning.toFixed(1) : na}</td>`;
            s += `<td data-val="${avgWaiting ?? ''}">${avgWaiting != null ? avgWaiting.toFixed(1) : na}</td>`;
            s += `<td data-val="${r.cache_hit_pct ?? ''}">${cacheHit}</td>`;
            s += `<td data-val="${m.kv_cache_usage ?? ''}">${kvUsage}</td>`;
            s += '</tr>';
        });
        s += '</table></div>';
    }

    // Engine metrics chart
    s += '<div class="chart-box"><h3>vLLM Engine Metrics</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">KV cache usage, running/waiting requests, prefill/decode time across tested configurations.</p>';
    s += '<div id="dl-cfg-engine" style="height:850px"></div></div>';

    // All results table (match UI coreResults filter)
    const coreRes = allRes.filter(r => {
        if (r.quality === 'discard') return false;
        const tid = r.test_id || '';
        return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
               tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
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
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',3,'num')">E2E P90 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',4,'num')">TTFT P95 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',5,'num')">TTFT P99 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',6,'num')">Tput Mean &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',7,'num')">ITL P90 &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',8,'num')">GPUs &#x21C5;</th>`;
        s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',9,'num')">Efficiency &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th>`;
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
            const e2eP90 = r.e2e_latency_p90 != null ? (r.e2e_latency_p90 >= 1000 ? (r.e2e_latency_p90/1000).toFixed(1) + 's' : Math.round(r.e2e_latency_p90) + 'ms') : na;
            s += `<tr${cls}><td>${r.config_name}${eppBadge}</td><td>${r.architecture}</td><td data-val="${r.ttft_p90}">${r.ttft_p90}</td><td data-val="${r.e2e_latency_p90 ?? ''}">${e2eP90}</td><td data-val="${r.ttft_p95 ?? ''}">${r.ttft_p95 ?? na}</td><td data-val="${r.ttft_p99 ?? ''}">${r.ttft_p99 ?? na}</td><td data-val="${tputMean}">${tputMean}</td><td data-val="${r.itl_p90 ?? ''}">${r.itl_p90 != null ? parseFloat(r.itl_p90).toFixed(2) : na}</td><td data-val="${r.gpus}">${r.gpus}</td><td data-val="${r.efficiency}">${r.efficiency}</td><td>${manifestLinks}</td></tr>`;
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
    s += '<div class="chart-box"><h3>Architecture Comparison</h3><div id="dl-chart-arch" style="height:850px"></div></div>';
    s += '<div class="chart-box"><h3>Percentile Comparison: Winner vs Aggregated</h3><div id="dl-chart-pctile" style="height:850px"></div></div>';
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
        s += `<tr><td><strong>Throughput Mean</strong></td><td>${cmp.pd.throughput_mean || cmp.pd.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_mean || cmp.aggregated.throughput_p90} req/s</td><td style="color:${tpC};font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
        if (cmp.pd.ttft_p99 && cmp.aggregated.ttft_p99) {
            const p99Color = cmp.ttft_p99_winner === 'PD' ? '#10b981' : '#f59e0b';
            s += `<tr style="border-top:2px solid #e2e8f0;"><td><strong>TTFT P99 (tail)</strong></td><td>${cmp.pd.ttft_p99} ms</td><td>${cmp.aggregated.ttft_p99} ms</td><td style="color:${p99Color};font-weight:700;">${cmp.ttft_p99_winner} (${cmp.ttft_p99_diff_pct}% better)</td></tr>`;
        }
        s += '</table>';

        // % change table: all PD configs vs baseline
        if (rec.aggregated_baseline && data.all_results && data.all_results.length > 1) {
            const bl = rec.aggregated_baseline;
            const configs = data.all_results.filter(r => {
                if (r.quality === 'discard') return false;
                if (r.architecture !== 'PD' || !r.ttft_p90 || !r.throughput_p90) return false;
                const tid = r.test_id || '';
                return tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
            });
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
        s += `<tr><td><strong>Throughput Mean</strong></td><td>${cmp.ep.throughput_mean || cmp.ep.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_mean || cmp.aggregated.throughput_p90} req/s</td><td style="color:${tpC};font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
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
        } else {
            s += '<div style="margin:8px 0 12px;padding:10px 14px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;color:#991b1b;font-size:0.9em;">&#9888; No concurrency level met the SLA target for ' + arch.toUpperCase() + '.</div>';
        }

        // Per-percentile chart divs
        ['p90', 'p95', 'p99'].forEach(pctl => {
            s += `<div id="dl-step9-${pctl}-${ai}" style="height:850px;background:#fff;border-radius:8px;border:1px solid #e2e8f0;margin-top:12px;"></div>`;
        });

        // Trial table
        const sorted = [...trials].sort((a, b) => a.concurrency - b.concurrency);
        s += '<div style="margin-top:12px;overflow-x:auto;"><table style="font-size:0.85em;"><tr><th>Concurrency</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Throughput P90</th><th>SLA</th><th>Manifests</th></tr>';
        sorted.forEach(t => {
            const sla = t.meets_sla ? '<span style="color:#059669;">Yes</span>' : '<span style="color:#dc2626;">No</span>';
            let mLinks = '-';
            if (t.test_id && t.manifest_types && t.manifest_types.length) {
                mLinks = t.manifest_types.filter(mt => !mt.includes('service')).map(mt =>
                    `<a href="#" onclick="dlManifest('${t.test_id}','${mt}');return false;" title="Download ${mt}.yaml" style="color:#0ea5e9;text-decoration:none;font-size:12px;padding:2px 6px;background:#f0f9ff;border-radius:4px;border:1px solid #bae6fd;margin:1px;display:inline-block;cursor:pointer;">${mt} &#8595;</a>`).join(' ');
            }
            s += `<tr><td style="font-weight:700;">${t.concurrency}</td><td>${dlFmt(t.ttft_p50)}</td><td>${dlFmt(t.ttft_p90)}</td><td>${dlFmt(t.ttft_p95)}</td><td>${dlFmt(t.ttft_p99)}</td><td>${dlFmt(t.throughput_p90, 2)}</td><td>${sla}</td><td>${mLinks}</td></tr>`;
        });
        s += '</table></div></div>';
    });

    // All trials table — every trial with search phase info
    if (!ls.trials.some(t => t.meets_sla)) {
        s += '<div style="margin:12px 20px;padding:12px 16px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;color:#991b1b;font-size:0.92em;">&#9888; No concurrency level met the SLA target in this search.</div>';
    }
    s += '<div style="padding:12px 20px 4px;font-weight:700;font-size:0.95em;color:#1e293b;border-top:1px solid #e2e8f0;margin-top:8px;">All Trials</div>';
    s += '<div style="padding:0 20px 16px;overflow-x:auto;"><table style="font-size:0.85em;">';
    const s9TblTputKey = 'throughput_' + targetPct;
    s += `<tr><th>Arch</th><th>#</th><th>Phase</th><th>Concurrency</th><th>TTFT ${targetPct.toUpperCase()}</th><th>Throughput ${targetPct.toUpperCase()}</th><th>Meets SLA</th><th>Manifests</th></tr>`;
    ls.trials.forEach(t => {
        const latVal = t[metricKey] != null ? t[metricKey].toFixed(1) + ' ms' : '-';
        const s9TputRaw = t[s9TblTputKey] != null ? t[s9TblTputKey] : t.throughput_p90;
        const tputVal = s9TputRaw != null ? s9TputRaw.toFixed(2) + ' req/s' : '-';
        const slaStyle = t.meets_sla ? 'color:#059669; font-weight:700;' : 'color:#dc2626; font-weight:700;';
        let mLinks = '-';
        if (t.test_id && t.manifest_types && t.manifest_types.length) {
            mLinks = t.manifest_types.filter(mt => !mt.includes('service')).map(mt =>
                `<a href="#" onclick="dlManifest('${t.test_id}','${mt}');return false;" title="Download ${mt}.yaml" style="color:#0ea5e9;text-decoration:none;font-size:12px;padding:2px 6px;background:#f0f9ff;border-radius:4px;border:1px solid #bae6fd;margin:1px;display:inline-block;cursor:pointer;">${mt} &#8595;</a>`).join(' ');
        }
        s += `<tr><td>${t.architecture}</td><td>${t.trial_number}</td><td>${t.search_phase}</td>`;
        s += `<td style="font-weight:700;">${t.concurrency}</td>`;
        s += `<td>${latVal}</td><td>${tputVal}</td>`;
        s += `<td style="${slaStyle}">${t.meets_sla ? 'Yes' : 'No'}</td>`;
        s += `<td>${mLinks}</td></tr>`;
    });
    s += '</table></div>';

    s += '</div>';
    return s;
}

// ── Calibrated Load Recommendation Cards (reused in Recommendation tab and Calibrated tab)
function buildCalRecCards(data) {
    const calBest = data.summary && data.summary.calibrated_best;
    if (!calBest) return '';
    let s = '';
    {
        const bp = data.recommendation ? data.recommendation.best_by_percentile || {} : {};
        const calTypes = [
            { key: 'balanced', label: 'Best Balanced', desc: 'Best TTFT-to-throughput ratio at calibrated load', color: '#059669', icon: '&#9878;' },
            { key: 'lowest_ttft', label: 'Lowest TTFT', desc: 'Fastest first token at calibrated load', color: '#3b82f6', icon: '&#9201;' },
            { key: 'lowest_itl', label: 'Lowest ITL', desc: 'Smoothest streaming at calibrated load', color: '#0891b2', icon: '&#9881;' },
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
                deploy = `${cfg.prefill_pods}P×TP${cfg.prefill_tp || cfg.tp || '?'} + ${cfg.decode_pods}D×TP${cfg.decode_tp || cfg.tp || '?'}`;
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
            // vLLM tok/s
            const calVllmTps = cfg.vllm_tps || cfg.prompt_tps_avg || null;
            if (calVllmTps != null) {
                s += `<div style="font-size:0.8em;color:#475569;margin-bottom:4px;"><span>vLLM: <strong style="color:#3b82f6;">${Math.round(calVllmTps).toLocaleString()} tok/s</strong> <span style="font-size:0.85em;color:#94a3b8;">(server total)</span></span></div>`;
            }
            s += '<table style="width:100%;font-size:0.8em;border-collapse:collapse;margin-top:6px;">';
            s += '<tr style="color:#94a3b8;font-weight:600;"><td></td><td>TTFT</td><td>E2E</td><td>ITL</td></tr>';
            if (cfg.ttft_p50) s += `<tr><td style="font-weight:600;color:#475569;">P50</td><td style="color:#64748b;">${Math.round(cfg.ttft_p50).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(cfg.e2e_p50)}</td><td>${fmtItl(cfg.itl_p50)}</td></tr>`;
            s += `<tr><td style="font-weight:600;color:#475569;">P90</td><td style="font-weight:700;color:#1e293b;">${cfg.ttft_p90 != null ? Math.round(cfg.ttft_p90).toLocaleString() + ' ms' : '-'}</td><td>${fmtE2e(cfg.e2e_p90)}</td><td>${fmtItl(cfg.itl_p90)}</td></tr>`;
            if (cfg.ttft_p95) s += `<tr><td style="font-weight:600;color:#475569;">P95</td><td style="color:#64748b;">${Math.round(cfg.ttft_p95).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(cfg.e2e_p95)}</td><td>${fmtItl(cfg.itl_p95)}</td></tr>`;
            if (cfg.ttft_p99) s += `<tr><td style="font-weight:600;color:#475569;">P99</td><td style="color:#64748b;">${Math.round(cfg.ttft_p99).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(cfg.e2e_p99)}</td><td>${fmtItl(cfg.itl_p99)}</td></tr>`;
            s += '</table>';
            s += '</div></div>';
        });

        s += '</div></div>';
    }
    return s;
}

// ── Cache Sweep Recommendation Cards ────────────────────────────────────────
function buildCacheRecCards(data) {
    const cacheBest = data.summary && data.summary.cache_sweep_best;
    if (!cacheBest) return '';
    let s = '';
    const cacheTypes = [
        { key: 'balanced', label: 'Best Balanced', desc: 'Best TTFT-to-throughput ratio with cache', color: '#059669', icon: '&#9878;' },
        { key: 'lowest_ttft', label: 'Lowest TTFT', desc: 'Fastest first token with cache', color: '#3b82f6', icon: '&#9201;' },
        { key: 'lowest_itl', label: 'Lowest ITL', desc: 'Smoothest streaming with cache', color: '#0891b2', icon: '&#9881;' },
        { key: 'highest_tput', label: 'Highest Throughput', desc: 'Maximum req/s with cache', color: '#f59e0b', icon: '&#9889;' },
        { key: 'most_efficient', label: 'Most Efficient', desc: 'Best throughput per GPU with cache', color: '#8b5cf6', icon: '&#128176;' },
    ];
    const archColors = { pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed' };
    const cacheSeen = new Set();

    s += '<div style="border:2px solid #8b5cf6;border-left:6px solid #8b5cf6;border-radius:10px;margin:20px 0;overflow:hidden;">';
    s += '<div style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);padding:14px 20px;font-size:1.1em;font-weight:800;color:white;">Deployment Recommendation &mdash; Cache Hit Sweep</div>';
    s += '<div style="padding:12px 20px 4px;color:#475569;font-size:0.9em;">Performance with varying prefix cache hit rates — showing how cache efficiency affects throughput and latency at calibrated load.</div>';
    s += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;padding:16px 20px;">';

    cacheTypes.forEach(sel => {
        const cfg = cacheBest[sel.key];
        if (!cfg) return;
        const cacheId = cfg.test_id || cfg.config_name || '';
        let dupNote = '';
        if (cacheSeen.has(cacheId)) {
            const others = cacheTypes.filter(s2 => s2.key !== sel.key && cacheBest[s2.key] && (cacheBest[s2.key].test_id || cacheBest[s2.key].config_name) === cacheId).map(s2 => s2.label);
            if (others.length) dupNote = others.join(', ');
            else return;
        }
        cacheSeen.add(cacheId);

        const archKey = cfg.architecture || 'aggregated';
        const aColor = archColors[archKey] || '#64748b';
        let deploy;
        if (cfg.prefill_pods && cfg.decode_pods) {
            deploy = `${cfg.prefill_pods}P×TP${cfg.prefill_tp || cfg.tp || '?'} + ${cfg.decode_pods}D×TP${cfg.decode_tp || cfg.tp || '?'}`;
        } else {
            deploy = cfg.name || cfg.config_name || '';
        }
        const tput = cfg.throughput_mean || cfg.throughput_p90 || '-';
        const concStr = cfg.concurrency ? `c=${cfg.concurrency}` : '';
        const cacheStr = cfg.cache_hit_pct != null ? cfg.cache_hit_pct + '% cache hit' : '';
        const fmtE2e = v => v != null ? (v >= 1000 ? (v/1000).toFixed(1) + ' s' : Math.round(v) + ' ms') : '-';

        s += '<div style="background:white;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);border:2px solid #cbd5e1;">';
        s += `<div style="background:linear-gradient(135deg,${sel.color},${sel.color}cc);padding:10px 14px;color:white;">`;
        s += `<div style="font-weight:700;font-size:0.85em;">${sel.icon} ${sel.label}</div>`;
        s += `<div style="display:flex;gap:4px;margin-top:2px;"><div style="font-size:0.65em;background:${aColor};padding:2px 6px;border-radius:10px;font-weight:600;">${archKey.toUpperCase()}</div>`;
        if (cacheStr) s += `<div style="font-size:0.65em;background:rgba(255,255,255,0.2);padding:2px 6px;border-radius:10px;font-weight:600;">${cacheStr}</div>`;
        if (dupNote) s += `<div style="font-size:0.65em;background:rgba(255,255,255,0.2);padding:2px 6px;border-radius:10px;">+ ${dupNote}</div>`;
        s += '</div>';
        if (sel.desc) s += `<div style="font-size:0.7em;opacity:0.9;margin-top:1px;">${sel.desc}</div>`;
        s += '</div>';
        s += '<div style="padding:12px 14px;">';
        s += `<div style="font-size:1.1em;font-weight:800;color:#0f172a;margin-bottom:8px;">${deploy}</div>`;
        s += '<div style="display:flex;gap:12px;font-size:0.85em;color:#475569;margin-bottom:4px;">';
        s += `<span>Throughput: <strong>${typeof tput === 'number' ? tput.toFixed(2) + ' req/s' : tput}</strong></span>`;
        s += `<span>GPUs: <strong>${cfg.gpus || '?'}</strong></span>`;
        if (concStr) s += `<span style="color:#7c3aed;font-weight:600;">${concStr}</span>`;
        s += '</div>';
        if (cfg.vllm_tps != null) {
            s += `<div style="font-size:0.8em;color:#475569;margin-bottom:4px;"><span>vLLM: <strong style="color:#7c3aed;">${Math.round(cfg.vllm_tps).toLocaleString()} tok/s</strong> <span style="font-size:0.85em;color:#94a3b8;">(server total)</span></span></div>`;
        }
        s += '<table style="width:100%;font-size:0.8em;border-collapse:collapse;margin-top:6px;">';
        s += '<tr style="color:#94a3b8;font-weight:600;"><td></td><td>TTFT</td><td>E2E</td><td>ITL</td></tr>';
        if (cfg.ttft_p50) s += `<tr><td style="font-weight:600;color:#475569;">P50</td><td style="color:#64748b;">${Math.round(cfg.ttft_p50).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(cfg.e2e_p50)}</td><td>${fmtItl(cfg.itl_p50)}</td></tr>`;
        s += `<tr><td style="font-weight:600;color:#475569;">P90</td><td style="font-weight:700;color:#1e293b;">${cfg.ttft_p90 != null ? Math.round(cfg.ttft_p90).toLocaleString() + ' ms' : '-'}</td><td>${fmtE2e(cfg.e2e_p90)}</td><td>${fmtItl(cfg.itl_p90)}</td></tr>`;
        if (cfg.ttft_p95) s += `<tr><td style="font-weight:600;color:#475569;">P95</td><td style="color:#64748b;">${Math.round(cfg.ttft_p95).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(cfg.e2e_p95)}</td><td>${fmtItl(cfg.itl_p95)}</td></tr>`;
        if (cfg.ttft_p99) s += `<tr><td style="font-weight:600;color:#475569;">P99</td><td style="color:#64748b;">${Math.round(cfg.ttft_p99).toLocaleString()} ms</td><td style="color:#64748b;">${fmtE2e(cfg.e2e_p99)}</td><td>${fmtItl(cfg.itl_p99)}</td></tr>`;
        s += '</table>';
        s += '</div></div>';
    });

    s += '</div></div>';
    return s;
}

// ── Calibrated Load Tab (Step 11) ───────────────────────────────────────────
function buildCalSection(data) {
    const cal = data.calibrated_qps;
    const calBest = data.summary && data.summary.calibrated_best;
    if (!cal && !calBest) return '';
    let s = '';

    // GPU sizing analysis
    if (cal && cal.gpu_sizing) {
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
    if (cal) {
        if (cal.pd) entries.push({ label: 'PD', entry: cal.pd });
        if (cal.aggregated) entries.push({ label: 'Aggregated', entry: cal.aggregated });
        if (cal.ep) entries.push({ label: 'EP', entry: cal.ep });
        if (cal.epp_pd) entries.push({ label: 'PD (EPP Tuned)', entry: cal.epp_pd });
        if (cal.epp_agg) entries.push({ label: 'Aggregated (EPP Tuned)', entry: cal.epp_agg });
    }
    const dlRequestedRps = cal && cal.requested_rps != null ? cal.requested_rps : null;
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
                const fmtV = v => v == null ? '-' : mi === 1 ? parseFloat(v).toFixed(2) : (mi === 2 ? parseFloat(v).toFixed(2) : Math.round(v).toLocaleString());
                s += `<td style="color:#64748b;${bs}">${m.name}</td><td style="${bs}">${fmtV(m.p50)}</td><td style="${hl(entry[m.k], m.b)}${bs}">${fmtV(m.p90)}</td><td style="${bs}">${fmtV(m.p95)}</td><td style="${bs}">${fmtV(m.p99)}</td></tr>`;
            });
        });
        s += '</table></div>';
    }

    // Fallback banner when there's no gpu_sizing but capacity info exists
    if (cal && !cal.gpu_sizing && cal.total_gpus_available && cal.requested_rps) {
        s += `<div style="padding:10px 20px;background:#ecfdf5;border:1px solid #6ee7b7;border-radius:8px;margin-bottom:16px;font-size:0.9em;color:#065f46;">&#128202; Cluster can sustain <strong>${cal.requested_rps} req/s</strong> with <strong>${cal.total_gpus_available} GPUs</strong>.</div>`;
    }

    // Load Analysis per architecture (Little's law, from measured Step 7 data)
    if (cal && cal.calibration_analysis) {
        const ca = cal.calibration_analysis;
        s += '<div class="chart-box"><h3>Load Analysis (from measured Step 7 data)</h3>';
        s += '<table><tr><th>Architecture</th><th>Throughput</th><th>Response Time</th><th>Service Time</th><th>Queue Time</th><th>Utilization</th><th>Calibrated Load</th></tr>';
        ['pd', 'aggregated'].forEach(arch => {
            const a = ca[arch];
            if (!a) return;
            const label = arch === 'pd' ? 'PD' : 'Aggregated';
            const queuePct = a.queue_pct || 0;
            const queueColor = queuePct > 80 ? '#dc2626' : (queuePct > 50 ? '#d97706' : '#059669');
            s += `<tr>`;
            s += `<td style="font-weight:700;">${label}</td>`;
            s += `<td>${a.throughput_mean} req/s <span style="color:#94a3b8;">@ c=${a.concurrency_tested}</span></td>`;
            s += `<td>${(a.response_time_s * 1000).toFixed(0)}ms</td>`;
            s += `<td>${a.service_time_ms.toFixed(0)}ms</td>`;
            s += `<td style="color:${queueColor};font-weight:600;">${a.queue_time_ms.toFixed(0)}ms (${queuePct}%)</td>`;
            s += `<td>${a.utilization_pct}%</td>`;
            s += `<td style="font-weight:700;color:#059669;">${a.calibrated_concurrency} users</td>`;
            s += `</tr>`;
        });
        s += '</table>';
        s += '<p style="padding:4px 0 0;font-size:0.82em;color:#64748b;">At the tested concurrency, most time is spent in queue — requests wait for GPU availability rather than being processed. The calibrated load reduces concurrency until queue time is reasonable (~2× service time).</p></div>';
    }

    // Overload impact — grouped by architecture family (PD + EPP-tuned, Aggregated + EPP-tuned)
    if (cal) {
        const origConcurrency = cal.concurrency != null ? `${cal.concurrency} concurrent` : '-';
        const calConcurrency = cal.requested_rps != null ? `${Math.round(cal.requested_rps)} concurrent` : '-';
        const overloadGroups = [];
        if (cal.pd && cal.overloaded_pd) {
            const group = { title: 'PD', rows: [{ label: 'PD', cal: cal.pd, over: cal.overloaded_pd }] };
            if (cal.epp_pd) group.rows.push({ label: 'PD (EPP Tuned)', cal: cal.epp_pd, over: cal.overloaded_pd });
            overloadGroups.push(group);
        }
        if (cal.aggregated && cal.overloaded_agg) {
            const group = { title: 'Aggregated', rows: [{ label: 'Aggregated', cal: cal.aggregated, over: cal.overloaded_agg }] };
            if (cal.epp_agg) group.rows.push({ label: 'Aggregated (EPP Tuned)', cal: cal.epp_agg, over: cal.overloaded_agg });
            overloadGroups.push(group);
        }
        overloadGroups.forEach(group => {
            s += `<div class="chart-box"><h3>Overload Impact: ${group.title}</h3>`;
            s += '<table><tr><th>Configuration</th><th>Load</th><th>TTFT P90</th><th>Throughput Mean</th></tr>';
            group.rows.forEach(row => {
                const calTputMean = row.cal.throughput_mean || row.cal.throughput_p50 || '-';
                const overTputMean = row.over.throughput_mean || row.over.throughput_p90 || '-';
                s += `<tr><td><strong>${row.label} (calibrated)</strong></td><td>${calConcurrency}</td><td style="color:#059669;font-weight:700;">${row.cal.ttft_p90} ms</td><td style="color:#059669;font-weight:700;">${calTputMean} req/s</td></tr>`;
                s += `<tr><td><strong>${row.label} (overloaded)</strong></td><td>${origConcurrency}</td><td style="color:#94a3b8;">${row.over.ttft_p90} ms</td><td style="color:#94a3b8;">${overTputMean} req/s</td></tr>`;
            });
            s += '</table></div>';
        });
    }

    return s;
}

// ── vLLM Metrics Tab ────────────────────────────────────────────────────────
function buildVLLMSection(charts, hasVLLM) {
    if (!hasVLLM) return '';
    let s = '';
    s += '<div class="grid2"><div class="chart-box"><h3>TTFT Percentiles</h3><div id="v1" style="height:850px"></div></div><div class="chart-box"><h3>ITL Percentiles</h3><div id="v2" style="height:850px"></div></div></div>';
    s += '<div class="grid2"><div class="chart-box"><h3>E2E Latency</h3><div id="v3" style="height:850px"></div></div><div class="chart-box"><h3>Token Throughput</h3><div id="v4" style="height:850px"></div></div></div>';
    s += '<div class="grid2"><div class="chart-box"><h3>Request Queue & KV Cache</h3><div id="v5" style="height:850px"></div></div><div class="chart-box"><h3>Time Breakdown & Preemptions</h3><div id="v6" style="height:850px"></div></div></div>';
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
        // Best VARIANT — the as-run baseline is the reference row, not a candidate
        const _variants = trials.filter(t => !t.is_baseline);
        const bestTrial = (_variants.length ? _variants : trials).reduce((a, b) => (a.ttft_p90 || Infinity) < (b.ttft_p90 || Infinity) ? a : b);

        s += `<div class="chart-box" style="border-left:4px solid #7c3aed;">`;
        s += `<h3 style="color:#7c3aed;">Step 9: EPP Tuning &mdash; ${archLabel}</h3>`;
        s += '<p style="font-size:0.9em;color:#475569;">Same deployment, different EPP scoring weights. Each test swapped only the gateway configmap (~10s) to isolate the impact of request routing.</p>';

        // Strategy glossary
        s += '<div style="padding:8px 0 12px;font-size:0.82em;color:#64748b;">' +
            '<table style="width:100%;font-size:1em;">' +
            '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">opt-baseline <span style="font-weight:400;color:#94a3b8;">(screen)</span></td><td style="padding:4px 0;">llm-d optimized-baseline guide: prefix-cache-affinity-filter gates candidates to sticky endpoints, a single load scorer (token-load for prefill-bound, active-request for decode-bound) ranks the survivors.</td></tr>' +
            '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">weighted-max <span style="font-weight:400;color:#94a3b8;">(screen)</span></td><td style="padding:4px 0;">Weighted multi-scorer stack with smart-derived weights and the deterministic max-score picker.</td></tr>' +
            '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">weighted-rand <span style="font-weight:400;color:#94a3b8;">(screen)</span></td><td style="padding:4px 0;">Same weighted stack with the probabilistic weighted-random picker &mdash; spreads load instead of always picking the argmax.</td></tr>' +
            '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">preset <span style="font-weight:400;color:#94a3b8;">(screen)</span></td><td style="padding:4px 0;">The user-selected preset structure &mdash; control arm for the screening phase.</td></tr>' +
            '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">abl-* <span style="font-weight:400;color:#94a3b8;">(ablate)</span></td><td style="padding:4px 0;">One-at-a-time plugin add/remove on the screening winner. Removals come first (abl-filter-off, abl-session-off &mdash; does the winner\'s defining structure actually pay?); then additions (session-affinity, no-hit-lru, kv-cache, LoRA-affinity A/B).</td></tr>' +
            '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">w-* <span style="font-weight:400;color:#94a3b8;">(weight)</span></td><td style="padding:4px 0;">Coordinate-descent moves: &plusmn;1 on the top scorer weights, or the affinity threshold / load weight for filter compositions.</td></tr>' +
            '<tr><td style="padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;font-weight:600;color:#1e293b;">final-* <span style="font-weight:400;color:#94a3b8;">(final)</span></td><td style="padding:4px 0;">Full-duration validation of the best variant &mdash; the apples-to-apples comparison against the baseline.</td></tr>' +
            '</table></div>';

        // Summary cards
        s += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;">';
        s += `<div style="background:#f0fdf4;border-radius:10px;padding:16px;text-align:center;border:1px solid #bbf7d0;"><div style="font-size:1.5em;font-weight:800;color:#059669;">${bestTrial.name}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Best Strategy</div></div>`;
        s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:1.5em;font-weight:800;color:#1e293b;">${bestTrial.ttft_p90 || 'N/A'} <span style="font-size:0.5em;color:#64748b;">ms</span></div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">TTFT P90</div></div>`;
        s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:1.5em;font-weight:800;color:#1e293b;">${(bestTrial.structure && bestTrial.structure.tokens_per_second) ? Math.round(bestTrial.structure.tokens_per_second) : (bestTrial.throughput_p90 || 'N/A')}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Tokens/s</div></div>`;
        s += `<div style="background:#f8fafc;border-radius:10px;padding:16px;text-align:center;border:1px solid #e2e8f0;"><div style="font-size:1.5em;font-weight:800;color:#1e293b;">${trials.length}</div><div style="color:#64748b;font-size:0.82em;margin-top:4px;">Strategies Tested</div></div>`;
        s += '</div>';

        // Chart divs for P90/P95/P99 with per-percentile textual summary under each
        const baselineTrial = trials.find(t => t.is_baseline);
        ['p90', 'p95', 'p99'].forEach(pctl => {
            s += `<div id="dl-epp-${arch}-${pctl}" style="height:850px;background:#fff;border-radius:8px;border:1px solid #e2e8f0;margin-top:8px;"></div>`;
            s += `<div style="padding:4px 2px 12px;font-size:0.85em;line-height:1.7;color:#1e293b;">`;
            trials.forEach(t => {
                const ttftKey = `ttft_${pctl}`;
                const ttft = t[ttftKey] != null ? t[ttftKey].toFixed(0) + 'ms' : 'N/A';
                const tput = t.throughput_mean != null ? t.throughput_mean.toFixed(1) : (t.throughput_p90 != null ? t.throughput_p90.toFixed(1) : 'N/A');
                let ttftDelta = '', tputDelta = '';
                if (baselineTrial && !t.is_baseline && baselineTrial[ttftKey] && t[ttftKey]) {
                    const ttftPct = ((t[ttftKey] - baselineTrial[ttftKey]) / baselineTrial[ttftKey] * 100);
                    if (ttftPct < -0.5) ttftDelta = ` <span style="color:#059669;">(${Math.abs(ttftPct).toFixed(1)}% faster)</span>`;
                    else if (ttftPct > 0.5) ttftDelta = ` <span style="color:#dc2626;">(+${ttftPct.toFixed(1)}% slower)</span>`;
                    else ttftDelta = ` <span style="color:#6b7280;">(0%)</span>`;
                }
                if (baselineTrial && !t.is_baseline) {
                    const bt = baselineTrial.structure && baselineTrial.structure.tokens_per_second;
                    const tt = t.structure && t.structure.tokens_per_second;
                    if (bt && tt) {
                        const tokPct = ((tt - bt) / bt * 100);
                        if (tokPct > 0.5) tputDelta = ` <span style="color:#059669;">(+${tokPct.toFixed(1)}%)</span>`;
                        else if (tokPct < -0.5) tputDelta = ` <span style="color:#dc2626;">(${tokPct.toFixed(1)}%)</span>`;
                        else tputDelta = ` <span style="color:#6b7280;">(&plusmn;0%)</span>`;
                    } else if (baselineTrial.throughput_mean && t.throughput_mean) {
                        const tputPct = ((t.throughput_mean - baselineTrial.throughput_mean) / baselineTrial.throughput_mean * 100);
                        if (tputPct > 0.5) tputDelta = ` <span style="color:#059669;">(+${tputPct.toFixed(1)}% req/s)</span>`;
                        else if (tputPct < -0.5) tputDelta = ` <span style="color:#dc2626;">(${tputPct.toFixed(1)}% req/s)</span>`;
                        else tputDelta = ` <span style="color:#6b7280;">(0%)</span>`;
                    }
                }
                const icon = t.is_baseline ? '&#128202;' : '&#128295;';
                const w = t.weights || {};
                const st = t.structure || {};
                let wStr;
                if (st.affinity_filter) {
                    wStr = 'affinity-filter + ' + (w.token_load ? `token-load&times;${w.token_load}` : (w.active_request ? `active-req&times;${w.active_request}` : 'load'));
                } else {
                    wStr = `${w.prefix_cache ?? '?'}:${!w.kv_cache ? 'off' : w.kv_cache}:${w.queue ?? '?'}:${w.active_request ?? 0}` + (w.token_load ? ` +tl${w.token_load}` : '') + (w.lora_affinity ? ` +lora${w.lora_affinity}` : (w.lora_affinity === 0 && t.name && t.name.indexOf('off') >= 0 ? ' +lora-off' : ''));
                }
                if (st.picker === 'weighted-random-picker') wStr += ' [wr]';
                const durTag = t.duration ? ` <span style="color:#94a3b8;">(${t.duration}s)</span>` : '';
                const deltaTag = (t.delta_vs_baseline && !t.is_baseline) ? ` <span style="color:#7c3aed;font-size:0.85em;">[${t.delta_vs_baseline}]</span>` : '';
                s += `<div>${icon} <strong>${t.name}</strong> <span style="color:#94a3b8;font-size:0.85em;">[${t.phase || 'screen'}]</span> (${wStr})${durTag}${deltaTag}: TTFT ${pctl.toUpperCase()}=${ttft}${ttftDelta}, ${(t.structure && t.structure.tokens_per_second) ? Math.round(t.structure.tokens_per_second) + ' tok/s' : tput + ' req/s'}${tputDelta}</div>`;
            });
            s += '</div>';
        });

        s += `<div id="dl-epp-${arch}-wl-e2e" style="height:600px;background:#fff;border-radius:8px;border:1px solid #e2e8f0;margin-top:8px;"></div>`;
        s += '<div style="padding:4px 2px 12px;font-size:0.85em;line-height:1.7;color:#1e293b;">E2E P90 per workload (guidellm) &mdash; each shape&rsquo;s own request duration, per strategy. W1 (3K-token outputs) sits ~15&times; above W3.</div>';
        s += '<div style="padding:4px 2px 12px;font-size:0.85em;line-height:1.7;color:#1e293b;">E2E request latency (first token + all decoded tokens) per strategy. Long-output workloads (W1: 3K tokens) dominate this metric &mdash; differences between strategies reflect routing&rsquo;s effect on total request time, including queueing and decode slowdowns under load.</div>';

        // Results table
        const na = 'N/A';
        s += '<table style="margin-top:12px;"><tr><th>Strategy</th><th>Phase</th><th>Routing Composition</th><th>&Delta; vs Baseline</th><th>Tok/s</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>ITL P90</th><th>EPP Config</th></tr>';
        trials.forEach(e => {
            const isBest = e === bestTrial && !e.is_baseline;
            const cls = isBest ? ' class="pareto"' : (e.is_baseline ? ' style="color:#64748b;font-style:italic;"' : '');
            const w = e.weights || {};
            const st = e.structure || {};
            let wStr;
            if (st.affinity_filter) {
                wStr = 'affinity-filter + ' + (w.token_load ? `token-load&times;${w.token_load}` : (w.active_request ? `active-req&times;${w.active_request}` : 'load'));
            } else {
                wStr = `${w.prefix_cache ?? '?'}:${!w.kv_cache ? 'off' : w.kv_cache}:${w.queue ?? '?'}:${w.active_request ?? 0}` + (w.token_load ? ` +tl${w.token_load}` : '') + (w.lora_affinity ? ` +lora${w.lora_affinity}` : '');
            }
            let pickerBadge = '';
            if (st.picker === 'weighted-random-picker') pickerBadge = ' <span style="background:#0891b2;color:white;font-size:0.7em;padding:1px 5px;border-radius:3px;" title="weighted-random-picker: picks a pod randomly, weighted by score">w-rand pick</span>';
            else if (st.picker === 'max-score-picker') pickerBadge = ' <span style="background:#475569;color:white;font-size:0.7em;padding:1px 5px;border-radius:3px;" title="max-score-picker: always picks the highest-scored pod">max pick</span>';
            const phaseColors = { screen: '#7c3aed', ablate: '#d97706', weight: '#0891b2', final: '#059669' };
            const phaseColor = phaseColors[e.phase] || '#64748b';
            const phaseBadge = e.phase ? `<span style="background:${phaseColor};color:white;font-size:0.7em;padding:1px 6px;border-radius:3px;">${e.phase}</span>` : '&mdash;';
            let ml = '-';
            if (e.manifest_types && e.manifest_types.length > 0 && e.test_id) {
                ml = e.manifest_types.map(t => `<a href="#" onclick="dlManifest('${e.test_id}','${t}');return false;" title="Download ${t}.yaml" style="color:#7c3aed;text-decoration:none;font-size:11px;padding:2px 6px;background:#f5f3ff;border-radius:4px;border:1px solid #c4b5fd;display:inline-block;margin:1px;cursor:pointer;">${t} &#8595;</a>`).join(' ');
            }
            const deltaStr = (e.delta_vs_baseline && !e.is_baseline) ? `<span style="color:#7c3aed;font-size:0.85em;">[${e.delta_vs_baseline}]</span>` : '';
            const tokVal = (st.tokens_per_second) ? Math.round(st.tokens_per_second) : na;
            s += `<tr${cls}><td>${e.is_baseline ? '&#128202; ' : ''}<strong>${e.name}</strong></td><td>${phaseBadge}</td><td>${wStr}${pickerBadge}</td><td>${deltaStr}</td><td>${tokVal}</td>`;
            s += `<td>${e.ttft_p50 ?? na}</td><td>${e.ttft_p90 ?? na}</td><td>${e.ttft_p95 ?? na}</td><td>${e.ttft_p99 ?? na}</td>`;
            s += `<td>${e.itl_p90 != null ? parseFloat(e.itl_p90).toFixed(2) : na}</td><td>${ml}</td></tr>`;
        });
        s += '</table>';
        // Legend (best variant is the highlighted row — no star marker)
        s += `<div style="padding:10px 16px;margin-top:8px;font-size:0.78em;line-height:1.8;color:#64748b;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;">
            <div style="font-weight:600;color:#475569;margin-bottom:2px;">Legend</div>
            <div><b>Icons:</b> &#128202; your config as-run (reference row) &nbsp;&middot;&nbsp; highlighted row = best variant (lowest objective TTFT)</div>
            <div><b>Phases:</b> screen = short ranking run &nbsp;&middot;&nbsp; ablate = plugin added/removed from the winner &nbsp;&middot;&nbsp; weight = scorer weight moved &nbsp;&middot;&nbsp; final = full-duration validation of the winner</div>
            <div><b>Composition:</b> "affinity-filter + scorer&times;W" &mdash; the prefix-cache-affinity-filter gates requests to cache-warm pods, then the scorer ranks survivors with weight W (omitted W = 1.0; "load" = token-load-scorer)</div>
            <div><b>Picker</b> (final stage, chooses the pod): <b>max pick</b> = max-score-picker, always takes the highest-scored pod &nbsp;&middot;&nbsp; <b>w-rand pick</b> = weighted-random-picker, random pick weighted by score</div>
            <div><b>Weighted stack:</b> "a:b:c:d" = prefix-cache : kv-cache : queue : active-request scorer weights ("off" = disabled)</div>
            <div><b>Tok/s:</b> total tokens (prompt+output) &divide; benchmark elapsed time &mdash; window-exact and comparable across all rows</div>
        </div></div>`;
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
            const skipObj = (typeof skip === 'string') ? { arch: skip, reason: 'not_tested' } : skip;
            const archLabel = ((skipObj && skipObj.arch) || '').toUpperCase();
            const info = reasonLabels[(skipObj && skipObj.reason) || ''] || reasonLabels['not_tested'];
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
        s += `<div class="chart-box"><h3>TTFT ${pctl.toUpperCase()} vs Concurrency</h3><div id="dl-sweep-ttft-${pctl}" style="height:850px"></div></div>`;
    });
    s += '<div class="chart-box"><h3>Throughput per GPU vs Concurrency</h3><div id="dl-sweep-tput-gpu" style="height:850px"></div></div>';

    s += '<div class="chart-box"><h3>vLLM Engine Metrics vs Concurrency</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">KV cache usage, running/waiting requests, prefill/decode GPU time across concurrency levels.</p>';
    s += '<div id="dl-sweep-engine" style="height:850px"></div></div>';

    s += '<div class="chart-box"><h3>Token Throughput &mdash; GPU Computed vs Server Total vs Client</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;"><strong>GPU computed:</strong> Prompt tokens actually computed (excludes cached) + generation. <strong>Server total:</strong> All tokens processed by vLLM (incl. cached prefixes). <strong>Client:</strong> Tokens measured by the load tester.</p>';
    s += '<div id="dl-sweep-token-tput" style="height:850px"></div></div>';

    const hasCache = configKeys.some(k => sweep[k].some(p => p.cache_hit_pct != null));
    if (hasCache) {
        s += '<div class="chart-box"><h3>Cache Hit % vs Concurrency</h3><div id="dl-sweep-cache-hit" style="height:850px"></div></div>';
    }

    // Sweep Pareto charts
    s += '<div class="chart-box"><h3>Pareto &mdash; Throughput vs Interactivity</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">Trade-off between per-user token speed (X) and GPU throughput efficiency (Y) across concurrency levels. Top-right is ideal. Each point is annotated with config name, interactivity, and throughput/GPU.</p>';
    s += '<div id="dl-sweep-pareto-inter" style="height:850px"></div></div>';
    s += '<div class="chart-box"><h3>Pareto &mdash; Throughput vs TTFT</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">Trade-off between time to first token (X, lower is better) and GPU throughput efficiency (Y). Top-left is ideal.</p>';
    s += '<div id="dl-sweep-pareto-ttft" style="height:850px"></div></div>';

    // vLLM Token Throughput vs Concurrency (server total vs output only)
    if (configKeys.some(k => sweep[k].some(p => p.vllm_tps))) {
        s += '<div class="chart-box"><h3>vLLM Token Throughput vs Concurrency</h3>';
        s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;"><strong>Solid line:</strong> Server total (prompt + generation from Prometheus). <strong>Dashed line:</strong> Output tokens only (generation). The gap reflects prefix cache efficiency — cached prompt tokens inflate the server total.</p>';
        s += '<div id="dl-sweep-vllm-tps" style="height:850px"></div></div>';
    }

    // Architecture comparison charts
    s += '<div class="chart-box"><h3>Best Envelope — PD vs Aggregated</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">Pareto frontier of each architecture\'s best throughput/GPU at each concurrency level. The higher curve is the better architecture.</p>';
    s += '<div id="dl-sweep-envelope" style="height:850px"></div></div>';
    s += '<div class="chart-box"><h3>Architecture Comparison — Overlaid</h3>';
    s += '<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">Combined view: frontier lines for each architecture plus markers showing which won at each concurrency level.</p>';
    s += '<div id="dl-sweep-overlaid" style="height:850px"></div></div>';

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
            var _rawLbl = p.config_label || cfgKey;
            var _aTag = cfgKey.startsWith('ep') ? ' (EP)' : cfgKey.startsWith('pd') ? ' (PD)' : ' (AG)';
            const label = _rawLbl.indexOf('(') >= 0 ? _rawLbl : _rawLbl + _aTag;
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
function buildCacheSweepSection(data, allRes) {
    if (!data.cache_sweep || !Object.keys(data.cache_sweep).length) return '';
    const sweep = data.cache_sweep;
    const configKeys = Object.keys(sweep);
    let s = '';

    const archColors = {pd: '#2563eb', aggregated: '#059669', ep: '#7c3aed'};
    const archLabels = {pd: 'PD', aggregated: 'Aggregated', ep: 'EP'};
    function csArchKey(k) { return k.startsWith('pd') ? 'pd' : k.startsWith('ep') ? 'ep' : 'aggregated'; }

    // Per-config sections (matching UI style)
    configKeys.forEach(cfgKey => {
        const ak = csArchKey(cfgKey);
        const color = archColors[ak] || '#888';
        const label = archLabels[ak] || cfgKey;
        const points = [...sweep[cfgKey]].sort((a, b) => (a.hit_pct ?? 0) - (b.hit_pct ?? 0));
        const configLabel = (points[0] && points[0].config_label) || cfgKey;
        const conc = points[0] && points[0].concurrency ? ' (c=' + points[0].concurrency + ')' : '';

        s += `<div style="border-radius:10px;overflow:hidden;border:2px solid ${color};border-left:6px solid ${color};margin-bottom:20px;">`;
        s += `<div style="background:linear-gradient(135deg,${color},${color}99);padding:12px 20px;color:white;font-weight:700;font-size:1.2em;">${label} — ${configLabel}${conc} — Cache Hit Sweep</div>`;
        s += `<div style="padding:8px 20px;font-size:0.85em;color:#64748b;">TTFT percentiles &amp; throughput vs cache hit ratio.</div>`;

        // Charts per config
        const chartBase = `dl-cache-${cfgKey.replace(/[^a-z0-9]/g, '-')}`;
        s += `<div class="grid2">`;
        s += `<div class="chart-box"><h3>TTFT P90/P95/P99 vs Cache Hit %</h3><div id="${chartBase}-ttft" style="height:850px"></div></div>`;
        s += `<div class="chart-box"><h3>Throughput vs Cache Hit %</h3><div id="${chartBase}-tput" style="height:850px"></div></div>`;
        s += `</div>`;
        s += `<div class="chart-box"><h3>Actual Hit Rate vs Configured</h3><div id="${chartBase}-hit" style="height:850px"></div></div>`;

        // Data table per config
        const tid = `dl-cache-tbl-${cfgKey.replace(/[^a-z0-9]/g, '-')}`;
        s += `<div class="chart-box"><table id="${tid}"><tr>`;
        const _allResLk = {};
        (allRes || []).forEach(r => { if (r.test_id) _allResLk[r.test_id] = r; });
        let ci = 0;
        function _cth(label) { s += `<th style="cursor:pointer;" onclick="sortReportTable('${tid}',${ci++},'num')">${label} &#x21C5;</th>`; }
        _cth('Cache Hit %'); _cth('Actual Hit %'); _cth('Concurrency');
        _cth('TTFT P50'); _cth('TTFT P90'); _cth('TTFT P95'); _cth('TTFT P99');
        _cth('Throughput'); _cth('vLLM tok/s'); _cth('ITL P90');
        _cth('E2E P50'); _cth('E2E P90');
        _cth('KV Cache %'); _cth('Running'); _cth('Run Max'); _cth('Load Spread'); _cth('Waiting');
        _cth('Prefill t'); _cth('Decode t');
        s += '</tr>';
        const fmtE2e = v => v != null ? (v >= 1 ? v.toFixed(1) + 's' : Math.round(v * 1000) + 'ms') : '-';
        points.forEach(p => {
            const _r = _allResLk[p.test_id] || {};
            const _mj = _r.metrics_json ? (typeof _r.metrics_json === 'string' ? JSON.parse(_r.metrics_json) : _r.metrics_json) : {};
            const _pm = (_mj.prometheus_metrics || {});
            const _kvA = ((_pm.vllm_kv_cache_pct || {}).avg || 0) * 100;
            const _run = (_pm.vllm_requests_running || {}).avg || 0;
            const _runMax = (_pm.vllm_requests_running || {}).max || 0;
            const _runMin = (_pm.vllm_requests_running || {}).min || 0;
            const _routingCV = (_pm.vllm_routing_cv || {}).avg || null;
            const _wait = (_pm.vllm_requests_waiting || {}).avg || 0;
            const _preT = (_pm.vllm_prefill_time_rate || {}).avg || 0;
            const _decT = (_pm.vllm_decode_time_rate || {}).avg || 0;
            const _e2eP50 = (_pm.vllm_e2e_p50 || {}).avg || (_mj.e2e_latency_p50 != null ? _mj.e2e_latency_p50 : null);
            const _e2eP90 = (_pm.vllm_e2e_p90 || {}).avg || (_mj.e2e_latency_p90 != null ? _mj.e2e_latency_p90 : null);
            s += '<tr>';
            s += `<td data-val="${p.hit_pct ?? ''}">${p.hit_pct != null ? p.hit_pct + '%' : '-'}</td>`;
            s += `<td data-val="${p.actual_hit_rate ?? ''}">${p.actual_hit_rate != null ? p.actual_hit_rate.toFixed(1) + '%' : '-'}</td>`;
            s += `<td data-val="${p.concurrency ?? ''}">${p.concurrency ?? '-'}</td>`;
            s += `<td data-val="${p.ttft_p50 ?? ''}">${dlFmt(p.ttft_p50)}</td>`;
            s += `<td data-val="${p.ttft_p90 ?? ''}">${dlFmt(p.ttft_p90)}</td>`;
            s += `<td data-val="${p.ttft_p95 ?? ''}">${dlFmt(p.ttft_p95)}</td>`;
            s += `<td data-val="${p.ttft_p99 ?? ''}">${dlFmt(p.ttft_p99)}</td>`;
            s += `<td data-val="${p.throughput_mean ?? ''}">${dlFmt(p.throughput_mean, 2)}</td>`;
            s += `<td data-val="${p.vllm_tps ?? ''}">${p.vllm_tps ? p.vllm_tps.toLocaleString() : dlFmt(p.output_tps_mean, 0)}</td>`;
            s += `<td data-val="${p.itl_p90 ?? ''}">${dlFmt(p.itl_p90)}</td>`;
            s += `<td data-val="${_e2eP50 ?? ''}">${fmtE2e(_e2eP50)}</td>`;
            s += `<td data-val="${_e2eP90 ?? ''}">${fmtE2e(_e2eP90)}</td>`;
            s += `<td data-val="${_kvA.toFixed(1)}">${_kvA.toFixed(1)}%</td>`;
            s += `<td data-val="${_run.toFixed(1)}">${_run.toFixed(1)}</td>`;
            s += `<td data-val="${_runMax.toFixed(0)}">${_runMax.toFixed(0)}</td>`;
            s += `<td data-val="${(_runMax - _run).toFixed(1)}">${(_runMax - _run).toFixed(1)}</td>`;
            s += `<td data-val="${_wait.toFixed(1)}">${_wait.toFixed(1)}</td>`;
            s += `<td data-val="${_preT.toFixed(3)}">${_preT.toFixed(3)}</td>`;
            s += `<td data-val="${_decT.toFixed(2)}">${_decT.toFixed(2)}</td>`;
            s += '</tr>';
        });
        s += '</table></div>';

        // Engine metrics chart
        s += `<div class="chart-box"><h3>vLLM Engine Metrics</h3>`;
        s += `<p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">KV cache usage, running/waiting requests, prefill/decode GPU time across cache hit levels.</p>`;
        s += `<div id="${chartBase}-engine" style="height:850px"></div></div>`;

        s += '</div>';
    });
    return s;
}

// ── Traffic Tab ────────────────────────────────────────────────────────────
function buildTrafficSection(data, allRes) {
    const hasTraffic = allRes.some(r => r.request_total != null || r.request_errored != null);
    if (!hasTraffic) return '';

    const trafficPrefixes = ['step11-sweep', 'step13', 'step11-epp', 'step9', 'step7', 'step6', 'step3', 'step2'];
    const trafficLabelMap = {'tp-cal':'TP Calibration','step6':'Aggregated','step7':'PD/EP','step9':'EPP Tuning','step11-epp':'EPP Tuning','step11-sweep':'Concurrency Sweep','step13':'Cache Sweep','other':'Other'};
    const groups = {};
    allRes.forEach(r => {
        const tid = r.test_id || r.config_name || '';
        if (!tid) return;
        let gk = 'other';
        for (const p of trafficPrefixes) {
            if (tid.startsWith(p)) { gk = p; break; }
        }
        if (!groups[gk]) groups[gk] = [];
        groups[gk].push(r);
    });
    if (groups['step2'] || groups['step3']) {
        groups['tp-cal'] = (groups['step2'] || []).concat(groups['step3'] || []);
        delete groups['step2'];
        delete groups['step3'];
    }
    if (groups['step11-epp']) {
        groups['step9'] = (groups['step9'] || []).concat(groups['step11-epp']);
        delete groups['step11-epp'];
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
            if (hasPodNet) s += '<div class="chart-box"><h3>Pod Network Throughput</h3><p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">Management (eth0) network traffic per configuration.</p><div id="v7" style="height:850px"></div></div>';
            if (hasNIXL) s += '<div class="chart-box"><h3>NIXL KV Transfer Throughput</h3><p style="color:#64748b;font-size:0.85em;margin:0 0 4px;">RDMA traffic for KV cache transfer between prefill and decode pods.</p><div id="v8" style="height:850px"></div></div>';
            s += '</div>';
        }
    }

    groupKeys.forEach(gk => {
        const label = trafficLabelMap[gk] || gk;
        const results = groups[gk];
        const hasErrors = results.some(r => (r.request_errored || 0) > 0 || (r.nixl_errors || 0) > 0);
        if (gk === 'step11-sweep') {
            // Per-config charts with c=N labels and error badges (like live)
            const byConfig = {};
            results.forEach(r => {
                const cfgKey = (r.test_id || '').replace('step11-sweep-', '').replace(/-c\d+$/, '');
                const lbl = r.config_name || cfgKey;
                if (!byConfig[cfgKey]) byConfig[cfgKey] = { label: lbl, results: [] };
                byConfig[cfgKey].results.push(r);
            });
            Object.keys(byConfig).sort().forEach(cfgKey => {
                const cfg = byConfig[cfgKey];
                const cfgErr = cfg.results.some(r => (r.request_errored || 0) > 0 || (r.nixl_errors || 0) > 0);
                const cid = 'dl-traffic-sweep-' + cfgKey.replace(/[^a-z0-9]/g, '-');
                s += `<div class="chart-box"${cfgErr ? ' style="border-left:4px solid #dc2626;"' : ''}><h3>Sweep: ${cfg.label}${cfgErr ? ' <span style="color:#dc2626;font-size:0.8em;">&#9888; errors</span>' : ''}</h3><div id="${cid}" style="height:850px"></div></div>`;
            });
        } else {
            s += `<div class="chart-box"${hasErrors ? ' style="border-left:4px solid #dc2626;"' : ''}><h3>Traffic: ${label}${hasErrors ? ' <span style="color:#dc2626;font-size:0.8em;">&#9888; errors detected</span>' : ''}</h3><div id="dl-traffic-${gk}" style="height:850px"></div></div>`;
        }
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

function buildLoraSection(data, charts) {
    let s = '';
    const vllmCharts = charts && charts.vllm ? charts.vllm : {};

    // 1) LoRA Adapter Sweep (per sweep series)
    const lsweep = data.lora_sweep || {};
    Object.keys(lsweep).forEach(skey => {
        const pts = lsweep[skey];
        if (!pts || !pts.length) return;
        const lbl = pts[0].config_label || skey;
        const affByTest = {};
        (vllmCharts.lora_pod_details || []).forEach(pd => {
            if (pd && pd.test_id) affByTest[pd.test_id] = pd;
        });
        const _baseOnly = ((data.run_config || {}).lora_benchmark_mode === 'base_only');
        const rows = pts.map(p => {
            const swap = (p.lru_swapping && !_baseOnly) ? ' <span style="color:#ef4444;">&#9888; LRU</span>' : '';
            const aff = _dlLoraAffCells(affByTest[p.test_id]);
            const evict = p.adapter_evictions;
            const evictCell = (evict === undefined || evict === null) ? '—'
                : (evict > 0 ? '<span style="color:#ef4444;font-weight:600;">' + evict + '</span>' : '0');
            const resident = (p.gpu_resident || p.gpu_resident === 0)
                ? (p.total_slots ? p.gpu_resident + '×' + (p.total_slots / (p.gpu_resident || 1) === Math.round(p.total_slots / (p.gpu_resident || 1)) ? (p.total_slots / (p.gpu_resident || 1)) : '?') : String(p.gpu_resident))
                : '—';
            return '<tr>' +
                '<td style="text-align:center;font-weight:700;">' + p.adapter_count + '</td>' +
                '<td style="text-align:center;">' + resident + swap + '</td>' +
                '<td style="text-align:center;">' + (p.ttft_p90 || 0).toFixed(0) + ' ms</td>' +
                '<td style="text-align:center;">' + (p.throughput_mean || 0).toFixed(3) + ' req/s</td>' +
                '<td style="text-align:center;">' + (p.itl_p90 || 0).toFixed(1) + ' ms</td>' +
                '<td style="text-align:center;">' + _dlCacheCell(p.cache_hit_pct) + '</td>' +
                '<td style="text-align:center;">' + (p.output_tps_mean || 0).toFixed(0) + ' tok/s</td>' +
                '<td style="text-align:center;">' + evictCell + '</td>' +
                '<td style="text-align:center;color:#059669;">' + aff.single + '</td>' +
                '<td style="text-align:center;color:#ef4444;">' + aff.multi + '</td>' +
                '<td style="text-align:center;color:' + aff.color + ';font-weight:600;">' + aff.verdict + '</td>' +
                '</tr>';
        }).join('');
        const sweepChartId = 'dl-lora-sweep-chart-' + skey.replace(/[^a-z0-9]/gi, '-');
        const affChartId = 'dl-lora-sweep-aff-chart-' + skey.replace(/[^a-z0-9]/gi, '-');
        const affPts = [];
        pts.forEach(p => {
            const pd = affByTest[p.test_id];
            if (pd && (pd.total_adapters || 0) > 0) {
                affPts.push({
                    adapter_count: p.adapter_count,
                    pct: Math.round(100 * (pd.concentrated || 0) / pd.total_adapters),
                });
            }
        });
        const affChartHtml = affPts.length >= 2
            ? '<div id="' + affChartId + '" style="width:100%;height:340px;margin-top:18px;"></div>'
            : '';
        const evictChartId = 'dl-lora-sweep-evict-chart-' + skey.replace(/[^a-z0-9]/gi, '-');
        const evictPts = pts.filter(p => p.adapter_evictions !== undefined && p.adapter_evictions !== null);
        const evictChartHtml = evictPts.length >= 2
            ? '<div class="chart-box" style="border-left:6px solid #b91c1c;margin-top:18px;"><h3>Adapter Evictions — ' + lbl + '</h3>' +
                '<p style="color:#64748b;font-size:0.85em;line-height:1.6;">How often a serving pod had to <strong>drop a GPU-resident adapter to make room for another one</strong> (LRU eviction) during each sweep level. ' +
                'Adapter weights occupy residency slots (<code>max_loras</code> per pod); loading a new adapter once all slots are taken evicts the least-recently-used one. ' +
                'vLLM exposes no eviction counter, so this is <strong>estimated by replaying vLLM\'s LRU policy</strong> over each pod\'s in-flight adapter sequence (a lower bound). ' +
                '<strong>Gray bar (loads)</strong> — adapter activations from disk. <strong>Red bar (evictions)</strong> — LRU removals under capacity pressure. ' +
                '<strong>Dashed line</strong> — pool residency capacity (pods × slots per pod).</p>' +
                '<div id="' + evictChartId + '" style="width:100%;height:340px;margin-top:10px;"></div></div>'
            : '';
        s += '<div class="chart-box" style="border-left:6px solid #4f46e5;"><h3>LoRA Adapter Sweep — ' + lbl + '</h3>' +
            '<p style="color:#64748b;font-size:0.9em;line-height:1.6;">Multi-tenant scaling curve: the best configuration re-tested at increasing <strong>adapter counts</strong>, ' +
            'with benchmark requests round-robined across the base model and all adapters. ' +
            '<strong>Dotted green line</strong> — throughput (tokens/s, left axis). ' +
            '<strong>&Delta;% labels</strong> — throughput change vs the 0-adapter baseline. ' +
            '<strong>Red dashed vertical line + shaded region</strong> — the LRU overflow boundary: right of it the adapter working set exceeds total pool residency, so vLLM must swap adapters in and out.</p>' +
            '<div id="' + sweepChartId + '" style="width:100%;height:520px;margin-top:10px;"></div>' +
            affChartHtml +
            '<table id="dl-lora-sweep-table-' + skey + '" style="width:100%;font-size:0.9em;margin-top:10px;">' +
            '<tr><th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',0,\'num\')">Adapters &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',1,\'num\')">Resident Slots &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',2,\'num\')">TTFT p90 &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',3,\'num\')">Throughput &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',4,\'num\')">ITL p90 &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',5,\'num\')">Cache-Hit &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',6,\'num\')">Output &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',7,\'num\')">Evictions &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',8,\'num\')">Single-Pod &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',9,\'num\')">Multi-Pod &#x21C5;</th>' +
            '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-sweep-table-' + skey + '\',10,\'str\')">Routing Behavior &#x21C5;</th></tr>' +
            rows + '</table>' +
            '<p style="font-size:0.85em;color:#64748b;margin-top:8px;line-height:1.7;">' +
            '<strong>Resident slots</strong> — adapter slots per pod × serving pods. ' +
            '<strong>&#9888; LRU</strong> — the adapter working set exceeds total pool residency, so vLLM must evict a least-recently-used adapter to load each new one. ' +
            '<strong>Cache-hit</strong> — prefix-cache hit rate: vLLM keys cache blocks by (prefix, adapter), so spreading requests across adapters multiplies the key space and collapses the hit rate.</p></div>' + evictChartHtml;
    });

    // 2) LoRA routing & residency signals
    if (vllmCharts.lora && vllmCharts.configs && vllmCharts.configs.length) {
        const loraSig = vllmCharts.lora;
        const hasLoraData = (loraSig.ttft_p99_pod_spread_ms || []).some(v => v > 0) ||
            (loraSig.adapter_churn || []).some(v => v > 0) ||
            (loraSig.adapters_per_pod || []).some(v => v > 0);
        if (hasLoraData) {
            s += '<div class="chart-box" style="border-left:6px solid #7c3aed;"><h3>LoRA Routing &amp; Residency</h3>' +
                '<p style="color:#64748b;font-size:0.9em;line-height:1.6;">Per-configuration LoRA serving signals from vLLM Prometheus metrics. ' +
                '<strong>TTFT P99 spread across pods</strong> — the hot-pod vs cold-pod latency delta; misrouted requests pay a CPU&rarr;GPU adapter swap-in and show up as a large spread. ' +
                '<strong>Adapter churn</strong> — residency set changes from LRU evictions; high churn with few adapters means request scattering. ' +
                '<strong>Adapters per pod</strong> — how many distinct adapters each pod served (concentrated = adapter-aware routing; uniform spread = random load balancing). ' +
                '<strong>Waiting queue CV</strong> — queue-depth imbalance across pods.</p>' +
                '<div id="dl-lora-routing-chart" style="width:100%;height:640px;margin-top:10px;"></div></div>';

            // 3) Adapter affinity summary (non-sweep tests)
            const affDetails = (vllmCharts.lora_pod_details || []).filter(pd => {
                return pd && pd.pods && Object.keys(pd.pods).length && (pd.total_adapters || 0) > 0
                    && !(pd.test_id || '').startsWith('step11-epp-')
                    && !(pd.test_id || '').startsWith('step14-lora-');
            });
            if (affDetails.length) {
                const affRows = affDetails.map(pd => {
                    const total = pd.total_adapters || 0;
                    const conc = pd.concentrated || 0;
                    const scat = pd.scattered || 0;
                    const concPct = total ? Math.round(100 * conc / total) : 0;
                    const scatPct = total ? Math.round(100 * scat / total) : 0;
                    const share = pd.top_pod_share;
                    let verdict, verdictColor;
                    if (share === null || share === undefined) {
                        verdict = concPct >= 80 ? 'adapter-aware' : (concPct >= 50 ? 'partial' : 'random');
                        verdictColor = concPct >= 80 ? '#059669' : (concPct >= 50 ? '#f59e0b' : '#ef4444');
                    } else {
                        verdict = share >= 0.9 ? 'adapter-aware' : (share >= 0.5 ? 'partial' : 'random');
                        verdictColor = share >= 0.9 ? '#059669' : (share >= 0.5 ? '#f59e0b' : '#ef4444');
                    }
                    const shareCell = (share === null || share === undefined) ? '—' : Math.round(100 * share) + '%';
                    const shareColor = (share === null || share === undefined) ? '#64748b'
                        : (share >= 0.9 ? '#059669' : (share >= 0.5 ? '#f59e0b' : '#ef4444'));
                    const seen = (pd.adapters_seen && pd.adapters_seen !== total) ? total + '/' + pd.adapters_seen : String(total);
                    return '<tr>' +
                        '<td style="font-weight:600;">' + pd.config + '</td>' +
                        '<td style="text-align:center;">' + seen + '</td>' +
                        '<td style="text-align:center;color:#059669;font-weight:600;">' + conc + ' (' + concPct + '%)</td>' +
                        '<td style="text-align:center;color:#ef4444;">' + scat + ' (' + scatPct + '%)</td>' +
                        '<td style="text-align:center;color:' + shareColor + ';font-weight:600;">' + shareCell + '</td>' +
                        '<td style="text-align:center;color:' + verdictColor + ';font-weight:600;">' + verdict + '</td>' +
                        '</tr>';
                }).join('');
                s += '<div class="chart-box" style="border-left:6px solid #059669;"><h3>Adapter Affinity Summary</h3>' +
                    '<p style="color:#64748b;font-size:0.9em;line-height:1.6;">How consistently each configuration routes an adapter\'s requests to a single pod (non-sweep tests). ' +
                    'Counts include only adapters with <strong>&ge;2 requests</strong>. ' +
                    '<strong>Single-pod</strong> — the adapter was served entirely by one pod (adapter-aware routing). ' +
                    '<strong>Multi-pod</strong> — the adapter\'s requests were scattered across pods (random load balancing). ' +
                    '<strong>Top-pod share</strong> — request-weighted fraction of each adapter\'s requests served by its main pod (100% = fully sticky).</p>' +
                    '<div id="dl-lora-affinity-summary-chart" style="width:100%;height:360px;margin-top:10px;"></div>' +
                    '<table id="dl-lora-aff-table" style="width:100%;font-size:0.9em;margin-top:10px;">' +
                    '<tr><th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-aff-table\',0,\'str\')">Configuration &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-aff-table\',1,\'num\')">Adapters &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-aff-table\',2,\'num\')">Single-Pod &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-aff-table\',3,\'num\')">Multi-Pod &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-aff-table\',4,\'num\')">Top-Pod Share &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'dl-lora-aff-table\',5,\'str\')">Routing Behavior &#x21C5;</th></tr>' +
                    affRows + '</table></div>';
            } else if ((vllmCharts.lora_pod_details || []).some(pd => pd && pd.pods)) {
                const _bmMode = (data.run_config || {}).lora_benchmark_mode || 'round_robin';
                s += '<div class="chart-box"><h3>Adapter Affinity Summary</h3>' +
                    '<p style="color:#64748b;font-size:0.95em;line-height:1.6;">No adapter affinity to show' +
                    (_bmMode === 'base_only'
                        ? ' — this run\'s Benchmark Mode is <strong>base model only</strong>: every request targets the base model, so adapter routing is never exercised. Run the same configuration with Benchmark Mode = round-robin for the adapter-targeted side of the comparison.'
                        : ' — no adapter traffic was observed in the collected metrics.') +
                    '</p></div>';
            }

            // 4) Per-pod breakdowns
            (vllmCharts.lora_pod_details || []).forEach(pd => {
                if (!pd || !pd.pods) return;
                const podNames = Object.keys(pd.pods);
                if (!podNames.length) return;
                const podRows = podNames.map(pod => {
                    const p = pd.pods[pod];
                    const distinct = (p.running_adapters_distinct || []).length;
                    const last = p.running_adapters_last || [];
                    const lastStr = last.length
                        ? last.length + ' (' + last.slice(0, 5).join(', ') + (last.length > 5 ? ', …' : '') + ')'
                        : '—';
                    const wait = (p.waiting_adapters_last || []).length;
                    const evict = p.estimated_adapter_evictions;
                    const evictCell = (evict === undefined || evict === null) ? '—'
                        : (evict > 0 ? '<span style="color:#ef4444;font-weight:600;">' + evict + '</span>' : '0');
                    const loadCell = (p.estimated_adapter_loads === undefined || p.estimated_adapter_loads === null)
                        ? '—' : p.estimated_adapter_loads;
                    return '<tr>' +
                        '<td style="font-family:monospace;font-size:0.85em;">' + pod + '</td>' +
                        '<td style="text-align:center;">' + (p.max_lora != null ? p.max_lora : '—') + '</td>' +
                        '<td style="text-align:center;font-weight:600;">' + distinct + '</td>' +
                        '<td style="text-align:center;">' + (p.residency_set_changes || 0) + '</td>' +
                        '<td style="text-align:center;">' + loadCell + '</td>' +
                        '<td style="text-align:center;">' + evictCell + '</td>' +
                        '<td style="font-size:0.82em;color:#64748b;">' + lastStr + '</td>' +
                        '<td style="text-align:center;">' + (wait || '—') + '</td>' +
                        '</tr>';
                }).join('');
                const affTotal = pd.total_adapters || 0;
                const affPct = affTotal ? Math.round(100 * (pd.concentrated || 0) / affTotal) : 0;
                const affLine = affTotal
                    ? '<div style="margin:8px 0 4px;font-size:0.88em;"><strong>Adapter affinity:</strong> ' +
                      '<span style="color:#059669;font-weight:600;">' + (pd.concentrated || 0) + '</span> of ' + affTotal +
                      ' adapters (' + affPct + '%) served by a single pod — ' +
                      (affPct >= 80 ? 'concentrated (adapter-aware routing)' :
                       affPct >= 50 ? 'partially concentrated' :
                       'scattered across pods (random load balancing)') +
                      '; <span style="color:#ef4444;">' + (pd.scattered || 0) + '</span> served by multiple pods.</div>'
                    : '';
                const bdId = 'dl-lora-pod-bd-' + pd.config.replace(/[^a-z0-9]/gi, '-') + '-n' + (pd.total_adapters || 0);
                s += '<div style="margin-top:16px;border:1.5px solid #e2e8f0;border-radius:8px;overflow:hidden;background:#fff;">' +
                    '<div style="background:#f8fafc;padding:8px 14px;font-weight:600;color:#334155;font-size:0.95em;border-bottom:1px solid #e2e8f0;">' +
                    'Pod Breakdown — ' + pd.config + ((pd.total_adapters || 0) > 0 ? ' · n=' + pd.total_adapters : '') + '</div>' +
                    affLine +
                    '<table id="' + bdId + '" style="width:100%;font-size:0.88em;">' +
                    '<tr><th style="cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',0,\'str\')">Pod &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',1,\'num\')">GPU-Resident Cap &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',2,\'num\')">Distinct Adapters Served &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',3,\'num\')">Residency Changes &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',4,\'num\')">Loads &#x21C5;</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',5,\'num\')">Evictions &#x21C5;</th>' +
                    '<th>Last Running Set</th>' +
                    '<th style="cursor:pointer;" onclick="sortReportTable(\'' + bdId + '\',7,\'num\')">Waiting &#x21C5;</th></tr>' +
                    podRows + '</table></div>';
            });
        }
    }
    return s;
}

// ── Estimator Tab ───────────────────────────────────────────────────────────
function buildEstimatorSection(data) {
    const wl = data.recommendation ? data.recommendation.workload : {};
    let wlDesc = 'Tested with <strong>' + (wl.users || '?') + ' users</strong>, ISL <strong>' + (wl.isl || '?') + '</strong>';
    if (wl.isl_stdev) wlDesc += '&plusmn;' + wl.isl_stdev;
    wlDesc += ', OSL <strong>' + (wl.osl || '?') + '</strong>';
    if (wl.osl_stdev) wlDesc += '&plusmn;' + wl.osl_stdev;
    if (wl.turns > 1) wlDesc += ', <strong>' + wl.turns + ' turns</strong>';
    wlDesc += '.';
    let s = '<div class="chart-box" style="border:2px solid #d97706;">';
    s += '<h3>GPU Estimator</h3>';
    s += '<p style="color:#1e293b;font-size:0.95em;">Estimate how many GPUs/replicas are needed to meet your SLA targets using the measured performance data. ' + wlDesc + '</p>';
    s += '<div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-end;margin:12px 0;">';
    s += '<div><div style="font-weight:700;font-size:0.85em;color:#64748b;margin-bottom:6px;">Throughput SLA</div>' +
        '<label style="display:block;font-size:0.85em;color:#475569;">Target Throughput ' +
        '<input type="number" id="dl-est-tput-target" value="100" min="1" step="1" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;width:110px;"> req/s</label></div>';
    s += '<div><div style="font-weight:700;font-size:0.85em;color:#64748b;margin-bottom:6px;">TTFT SLA</div>' +
        '<label style="display:block;font-size:0.85em;color:#475569;">Max TTFT ' +
        '<input type="number" id="dl-est-ttft-target" value="500" min="1" step="1" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;width:100px;"> ms</label>' +
        '<label style="display:block;font-size:0.85em;color:#475569;margin-top:6px;">Percentile ' +
        '<select id="dl-est-ttft-pctl" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;">' +
        '<option value="p90">P90</option><option value="p95">P95</option><option value="p99" selected>P99</option></select></label></div>';
    s += '<div><div style="font-weight:700;font-size:0.85em;color:#64748b;margin-bottom:6px;">ITL SLA</div>' +
        '<label style="display:block;font-size:0.85em;color:#475569;">Max ITL ' +
        '<input type="number" id="dl-est-itl-target" value="20" min="0.1" step="0.1" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;width:100px;"> ms</label>' +
        '<label style="display:block;font-size:0.85em;color:#475569;margin-top:6px;">Percentile ' +
        '<select id="dl-est-itl-pctl" style="padding:8px 10px;border:2px solid #e2e8f0;border-radius:8px;font-size:0.95em;background:#f8fafc;">' +
        '<option value="p90" selected>P90</option><option value="p95">P95</option><option value="p99">P99</option></select></label></div>';
    s += '<button onclick="dlRunEstimator()" style="padding:10px 28px;font-size:0.95em;border-radius:8px;background:linear-gradient(135deg,#d97706,#b45309);border:none;color:white;cursor:pointer;font-weight:600;">Estimate</button>';
    s += '</div>';
    s += '<div id="dl-est-results"></div>';
    s += '<div id="dl-est-chart" style="width:100%;margin-top:16px;"></div>';
    s += '</div>';
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
        // Pre-extract token metrics before stripping metrics_json
        const mj = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
        const pm = mj.prometheus_metrics || {};
        copy._prompt_tps_avg = (pm.vllm_prompt_tokens_rate || {}).avg || null;
        copy._prompt_tps_max = (pm.vllm_prompt_tokens_rate || {}).max || null;
        copy._gen_tps_avg = (pm.vllm_generation_tokens_rate || {}).avg || null;
        copy._gen_tps_max = (pm.vllm_generation_tokens_rate || {}).max || null;
        copy._per_gpu_tps = mj.per_gpu_throughput_tokens_sec || null;
        copy._vllm_req_rate = (pm.vllm_e2e_count_rate || {}).avg || null;
        copy._avg_running = (pm.vllm_requests_running || {}).avg || null;
        copy._avg_waiting = (pm.vllm_requests_waiting || {}).avg || null;
        copy._kv_cache_usage = mj.kv_cache_usage || null;
        copy._prompt_tokens_mean = mj.prompt_tokens_mean || null;
        copy._output_tokens_mean = mj.output_tokens_mean || null;
        copy._throughput_mean_mj = mj.throughput_mean || null;
        copy._cache_hits_avg = (pm.vllm_prefix_cache_hits_rate || {}).avg || null;
        copy._cache_queries_avg = (pm.vllm_prefix_cache_queries_rate || {}).avg || null;
        copy._kv_cache_avg = (pm.vllm_kv_cache_pct || {}).avg || null;
        copy._kv_cache_max = (pm.vllm_kv_cache_pct || {}).max || null;
        copy._prefill_time_avg = (pm.vllm_prefill_time_rate || {}).avg || null;
        copy._decode_time_avg = (pm.vllm_decode_time_rate || {}).avg || null;
        copy._preemptions_avg = (pm.vllm_preemptions_rate || {}).avg || null;
        copy._avg_running_max = (pm.vllm_requests_running || {}).max || null;
        copy._avg_running_min = (pm.vllm_requests_running || {}).min || null;
        copy._routing_cv = (pm.vllm_routing_cv || {}).avg || null;
        copy._model_load_time_s = mj.model_load_time_s || null;
        delete copy.metrics_json;
        delete copy.manifests;
        delete copy.test_config;
        delete copy.profiles_blob;
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
    // Embedded raw pprof profiles for offline download (base64)
    const profileMap = {};
    allRes.forEach(r => {
        if (r.profiles_blob) {
            try {
                const blobs = typeof r.profiles_blob === 'string' ? JSON.parse(r.profiles_blob) : r.profiles_blob;
                if (blobs && Object.keys(blobs).length) profileMap[r.test_id || r.config_name] = blobs;
            } catch (e) { /* skip malformed */ }
        }
    });
    s += 'var _profiles=' + safeJson(profileMap) + ';';
    s += 'function dlProfile(testId,key){var p=_profiles[testId];if(!p||!p[key]){alert("Profile not available for "+testId+" / "+key);return;}var bin=atob(p[key]);var bytes=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++){bytes[i]=bin.charCodeAt(i);}var blob=new Blob([bytes],{type:"application/octet-stream"});var url=URL.createObjectURL(blob);var a=document.createElement("a");a.href=url;a.download=testId+"-"+key;a.click();URL.revokeObjectURL(url);}';
    s += 'var lo={margin:{t:30,b:40,l:50,r:20},height:850,font:{family:"sans-serif"}};';
    s += 'var co={responsive:true};';
    s += 'function _tL(n,a){if(!n||n.indexOf("(")>=0)return n||"";a=(a||"").toUpperCase();if(a==="PD")return n+" (PD)";if(a==="EP")return n+" (EP)";return n+" (AG)"}';
    s += 'function fmtSI(v,d){if(v==null)return"-";d=d!=null?d:1;if(Math.abs(v)>=1e6)return(v/1e6).toFixed(d)+"M";if(Math.abs(v)>=1e3)return(v/1e3).toFixed(d)+"K";return v.toFixed(d)}';
    s += 'function arrAnn(xs,ys,o){o=o||{};var c=o.color||"#333",d=o.decimals!=null?o.decimals:1,sp=o.suffix||"",spr=o.spread||30;var offs=[{ax:0,ay:-spr},{ax:spr*0.9,ay:spr*0.7},{ax:-spr*0.8,ay:-spr*1.2},{ax:spr*1.1,ay:-spr*0.5},{ax:0,ay:spr*1.1},{ax:-spr,ay:spr*0.8},{ax:spr*1.3,ay:-spr*1.3},{ax:-spr*1.2,ay:spr*1.3}];return ys.map(function(v,i){if(v==null)return null;var p=offs[i%offs.length];return{x:xs[i],y:v,xref:"x",yref:o.yref||"y",text:fmtSI(v,d)+sp,showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#94a3b8",ax:p.ax,ay:p.ay,font:{size:13,color:c},borderpad:2}}).filter(Boolean)}';
    s += 'var vl={...lo,margin:{...lo.margin,b:100},barmode:"group",showlegend:true,legend:{x:0,y:1.15,orientation:"h"}};';
    s += 'var pc={p50:"#60a5fa",p90:"#3b82f6",p95:"#f59e0b",p99:"#ef4444"};';

    // Panes are all visible initially for Plotly rendering; hidden after charts are done

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
    s += '  var dTraces=[{x:tpLabels,y:tpsgVals,name:"Tokens/s/GPU",type:"bar",marker:{color:barColors},text:tpsgVals.map(function(v){return fmtSI(v)}),textposition:"outside",textfont:{size:14,color:"#1e293b"},cliponaxis:false,constraintext:"none",hovertemplate:"<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>"}];';
    s += '  if(itlVals.some(function(v){return v>0})){dTraces.push({x:tpLabels,y:itlVals,name:"ITL P90 (ms)",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#ef4444",width:3},marker:{size:10,symbol:"circle",color:"#ef4444",line:{width:2,color:"white"}},hovertemplate:"<b>%{x}</b><br>ITL P90: %{y:.2f} ms<extra></extra>"});}';
    s += '  Plotly.newPlot("tp-dec",dTraces,{...lo,barmode:"group",margin:{t:30,b:40,l:50,r:60},showlegend:true,legend:{x:0,y:1.15,orientation:"h"},yaxis:{title:"Tokens/s/GPU",side:"left",tickformat:".2s"},yaxis2:{title:"ITL P90 (ms)",side:"right",overlaying:"y",titlefont:{color:"#ef4444"},tickfont:{color:"#ef4444"}}},co);';
    s += '}';

    // Prefill TP Sweep (dual-axis: TPSG bars + TTFT P90 line)
    s += 'if(recData.prefill_tp_all&&recData.prefill_tp_all.length&&document.getElementById("tp-pre")){';
    s += '  var ptp=recData.prefill_tp_all;';
    s += '  var ptpLabels=ptp.map(function(d){return"TP="+d.tp});';
    s += '  var ptpsgVals=ptp.map(function(d){return d.tpsg});';
    s += '  var pBestTpsg=Math.max.apply(null,ptpsgVals);';
    s += '  var pBarColors=ptpsgVals.map(function(v){return v===pBestTpsg?"#10b981":"#f59e0b"});';
    s += '  var ttftVals=ptp.map(function(d){return d.ttft_p90!=null?d.ttft_p90:0});';
    s += '  var pTraces=[{x:ptpLabels,y:ptpsgVals,name:"Tokens/s/GPU",type:"bar",marker:{color:pBarColors},text:ptpsgVals.map(function(v){return fmtSI(v)}),textposition:"outside",textfont:{size:14,color:"#1e293b"},cliponaxis:false,constraintext:"none",hovertemplate:"<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>"}];';
    s += '  if(ttftVals.some(function(v){return v>0})){pTraces.push({x:ptpLabels,y:ttftVals,name:"TTFT P90 (ms)",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#3b82f6",width:3},marker:{size:10,symbol:"circle",color:"#3b82f6",line:{width:2,color:"white"}},hovertemplate:"<b>%{x}</b><br>TTFT P90: %{y:.1f} ms<extra></extra>"});}';
    s += '  Plotly.newPlot("tp-pre",pTraces,{...lo,barmode:"group",margin:{t:30,b:40,l:50,r:60},showlegend:true,legend:{x:0,y:1.15,orientation:"h"},yaxis:{title:"Tokens/s/GPU",side:"left",tickformat:".2s"},yaxis2:{title:"TTFT P90 (ms)",side:"right",overlaying:"y",titlefont:{color:"#3b82f6"},tickfont:{color:"#3b82f6"}}},co);';
    s += '}';

    // Pareto + scatter + efficiency + architecture charts
    // TP Calibration (Pareto) — TPSG bars + TTFT P90 line (log, y2), matching live
    s += '(function(){if(!cd.pareto||!cd.pareto.traces||!cd.pareto.traces.length)return;';
    s += '  var tpsgLookup={};';
    s += '  (recData.decode_tp_all||[]).forEach(function(d){tpsgLookup["decode-"+d.tp]=d.tpsg;});';
    s += '  (recData.prefill_tp_all||[]).forEach(function(d){tpsgLookup["prefill-"+d.tp]=d.tpsg;});';
    s += '  var traces=[];var roleColors={decode:"#3b82f6",prefill:"#f59e0b"};';
    s += '  cd.pareto.traces.forEach(function(t){';
    s += '    if(t.yaxis==="y2")return;';
    s += '    var role=t.name.toLowerCase().indexOf("decode")>=0?"decode":"prefill";var color=roleColors[role];';
    s += '    var tpsgVals=t.x.map(function(tp){return tpsgLookup[role+"-"+tp]||0;});';
    s += '    traces.push({x:t.x,y:tpsgVals,name:role.charAt(0).toUpperCase()+role.slice(1)+" TPSG",type:"bar",marker:{color:color},text:tpsgVals.map(function(v){return v>0?Math.round(v).toLocaleString():""}),textposition:"outside",textfont:{size:13,color:color},cliponaxis:false,constraintext:"none",hovertemplate:"<b>TP=%{x}</b><br>TPSG: %{y:,.0f} tok/s/GPU<extra></extra>"});';
    s += '    traces.push({x:t.x,y:t.y,name:role.charAt(0).toUpperCase()+role.slice(1)+" TTFT",yaxis:"y2",mode:"markers+lines+text",text:t.text,customdata:t.y.map(function(v){return v>=1000?(v/1000).toFixed(1)+"s":Math.round(v)+"ms";}),texttemplate:"%{customdata}",textposition:"top center",textfont:{size:13,color:"#1e293b"},marker:{size:10,color:color,symbol:"circle",line:{width:2,color:"white"}},line:{width:2,dash:"dash",color:color},hovertemplate:"<b>%{text}</b><extra></extra>"});';
    s += '  });';
    s += '  var paretoXvals=[];cd.pareto.traces.filter(function(t){return t.yaxis!=="y2";}).forEach(function(t){t.x.forEach(function(v){if(paretoXvals.indexOf(v)<0)paretoXvals.push(v);});});paretoXvals.sort(function(a,b){return a-b});';
    s += '  Plotly.newPlot("p1",traces,{...lo,margin:{...lo.margin,r:60,t:30},barmode:"group",xaxis:{title:"Total GPUs (= TP)",tickvals:paretoXvals},yaxis:{title:"TPSG (tok/s/GPU) — higher is better",side:"left",tickformat:",.0f"},yaxis2:{title:"TTFT P90 (ms) — lower is better",side:"right",overlaying:"y",type:"log"},showlegend:true},co);';
    s += '})();';
    s += 'if(cd.scatter.traces.length){Plotly.newPlot("p2",cd.scatter.traces.map(function(t){return{x:t.x,y:t.y,text:t.text,name:t.name,mode:"markers",marker:{size:t.sizes,color:t.color,opacity:0.7,line:{width:1,color:"white"}},hovertemplate:"<b>%{text}</b><extra></extra>"}}),{...lo,xaxis:{title:"TTFT P90 (ms)"},yaxis:{title:"Throughput P90 (req/s)"},showlegend:true},co);}';
    s += 'if(cd.efficiency.configs.length){Plotly.newPlot("p3",[{x:cd.efficiency.configs,y:cd.efficiency.values,type:"bar",marker:{color:cd.efficiency.colors},text:cd.efficiency.values.map(function(v){return v!=null?v.toFixed(3):""}),textposition:"outside",textfont:{size:14,color:"#333"},cliponaxis:false,constraintext:"none"}],{...lo,margin:{...lo.margin,b:120},xaxis:{tickangle:-45},yaxis:{title:"req/s/GPU"}},co);}';
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
    s += '  var chd=ar.filter(function(r){var tid=r.test_id||r.config_name||"";return tid.indexOf("step2-")!==0&&tid.indexOf("step3-")!==0&&tid.indexOf("step11-")!==0&&tid.indexOf("step12-")!==0&&tid.indexOf("step13-")!==0&&r.cache_hit_pct!=null&&r.cache_hit_pct>0});';
    s += '  if(chd.length){';
    s += '    var chTraces=[];';
    s += '    var archCols={AGGREGATED:"#1f77b4",PD:"#ff7f0e",EP:"#2ca02c"};';
    s += '    ["AGGREGATED","PD","EP"].forEach(function(arch){';
    s += '      var filtered=chd.filter(function(r){return r.architecture===arch});';
    s += '      if(!filtered.length)return;';
    s += '      chTraces.push({x:filtered.map(function(r){return _tL(r.config_name,r.architecture)}),y:filtered.map(function(r){return r.cache_hit_pct}),text:filtered.map(function(r){return r.cache_hit_pct.toFixed(1)+"%"}),textposition:"outside",textfont:{size:13},name:arch,type:"bar",marker:{color:archCols[arch]}});';
    s += '    });';
    s += '    Plotly.newPlot("dl-cfg-cache-hit",chTraces,{...lo,height:850,barmode:"group",xaxis:{tickangle:-35},yaxis:{title:"Cache Hit %",range:[0,100]},showlegend:true,legend:{x:0,y:1,bgcolor:"rgba(255,255,255,0.9)"},margin:{t:20,b:120,l:60,r:20}},co);';
    s += '  }';
    s += '}';

    // Token throughput chart — 3 series like live: GPU computed / server total / client
    s += 'if(document.getElementById("dl-token-tput-chart")){';
    s += '  var tkRes=ar.filter(function(r){if(r.quality==="discard")return false;var tid=r.test_id||"";return tid.indexOf("step2-")!==0&&tid.indexOf("step3-")!==0&&tid.indexOf("step11-")!==0&&tid.indexOf("step12-")!==0&&tid.indexOf("step13-")!==0&&(r._gen_tps_avg||r.output_tps_mean);});';
    s += '  if(tkRes.length){';
    s += '    var tkLabels=tkRes.map(function(r){return _tL(r.config_name,r.architecture)});';
    s += '    var tkComputed=[],tkVllmTotal=[],tkGuidellmTotal=[];';
    s += '    tkRes.forEach(function(r){';
    s += '      var pAvg=r._prompt_tps_avg||0,gAvg=r._gen_tps_avg||0;';
    s += '      tkVllmTotal.push(Math.round(pAvg+gAvg));';
    s += '      var cacheHits=r._cache_hits_avg||0,cacheQueries=r._cache_queries_avg||0;';
    s += '      var cacheHitRate=cacheQueries>0?cacheHits/cacheQueries:0;';
    s += '      tkComputed.push(Math.round(pAvg*(1-cacheHitRate))+Math.round(gAvg));';
    s += '      var tput=r.throughput_mean||r._throughput_mean_mj||0;';
    s += '      tkGuidellmTotal.push(Math.round((r._prompt_tokens_mean||0)*tput+(r._output_tokens_mean||0)*tput));';
    s += '    });';
    s += '    Plotly.newPlot("dl-token-tput-chart",[';
    s += '      {x:tkLabels,y:tkComputed,name:"GPU computed",type:"bar",marker:{color:"#3b82f6"},text:tkComputed.map(function(v){return v.toLocaleString()}),textposition:"outside",textfont:{size:13,color:"#1e293b"}},';
    s += '      {x:tkLabels,y:tkVllmTotal,name:"Server total (incl. cached)",type:"bar",marker:{color:"#93c5fd"},text:tkVllmTotal.map(function(v){return v.toLocaleString()}),textposition:"outside",textfont:{size:13,color:"#1e293b"}},';
    s += '      {x:tkLabels,y:tkGuidellmTotal,name:"Client (guidellm)",type:"bar",marker:{color:"#f59e0b"},text:tkGuidellmTotal.map(function(v){return v.toLocaleString()}),textposition:"outside",textfont:{size:13,color:"#1e293b"}}';
    s += '    ],{...lo,height:850,barmode:"group",margin:{t:30,b:160,l:60,r:20},xaxis:{tickangle:-45},yaxis:{title:"Total Tokens/s (Prompt + Generation)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
    s += '  }';
    s += '}';

    // Per-User Token Throughput chart
    s += 'if(document.getElementById("dl-cfg-per-user")){';
    s += '  var pu=cd.per_user_efficiency;';
    s += '  if(pu&&pu.configs&&pu.configs.length){';
    s += '    Plotly.newPlot("dl-cfg-per-user",[{x:pu.configs,y:pu.values,type:"bar",marker:{color:pu.colors},text:pu.values.map(function(v){return v!=null?v.toFixed(1):""}),textposition:"outside",textfont:{size:14,color:"#333"},cliponaxis:false,constraintext:"none",hovertemplate:"<b>%{x}</b><br>%{y:.1f} tokens/s/user<extra></extra>"}],{...lo,margin:{t:30,b:120,l:60,r:20},xaxis:{tickangle:-45},yaxis:{title:"Output tokens/s per concurrent user - higher is better"}},co);';
    s += '  }';
    s += '}';

    // Aggregated configurations — TTFT bars + throughput line (+ ITL y3)
    s += '(function(){var el=document.getElementById("dl-cfg-agg-ttft");if(!el)return;';
    s += '  var agg=ar.filter(function(r){if(r.quality==="discard")return false;return r.architecture==="AGGREGATED"&&r.ttft_p90;});';
    s += '  if(agg.length<2)return;';
    s += '  var aggSorted=agg.slice().sort(function(a,b){return(a.tp||1)-(b.tp||1)});';
    s += '  var aggLabels=aggSorted.map(function(r){return(r.replicas||Math.floor(r.gpus/(r.tp||1)))+"×TP"+(r.tp||"?")+((r.pp&&r.pp>1)?" PPx"+r.pp:"")});';
    s += '  var aggColors={p90:"#3b82f6",p95:"#dc2626",p99:"#7c3aed"};';
    s += '  var traces=[];';
    s += '  ["p90","p95","p99"].forEach(function(p){traces.push({x:aggLabels,y:aggSorted.map(function(r){return r["ttft_"+p]}),name:"TTFT "+p.toUpperCase(),type:"bar",marker:{color:aggColors[p],opacity:0.85},hovertemplate:"%{x}<br>TTFT "+p.toUpperCase()+": %{y:.1f} ms<extra></extra>"});});';
    s += '  traces.push({x:aggLabels,y:aggSorted.map(function(r){return r.throughput_mean||r.throughput_p90}),name:"Throughput Mean",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3,shape:"spline"},marker:{color:"#f59e0b",size:10,symbol:"diamond",line:{width:2,color:"white"}},hovertemplate:"Throughput Mean: %{y:.2f} req/s<extra></extra>"});';
    s += '  var itlDashes={p90:"solid",p95:"dash",p99:"dot"};var itlSymbols={p90:"square",p95:"square-open",p99:"square-open-dot"};';
    s += '  var hasAggItl=aggSorted.some(function(r){return r.itl_p90!=null;});';
    s += '  if(hasAggItl){["p90","p95","p99"].forEach(function(p){traces.push({x:aggLabels,y:aggSorted.map(function(r){return r["itl_"+p]}),name:"ITL "+p.toUpperCase(),type:"scatter",mode:"lines+markers",yaxis:"y3",line:{color:"#ef4444",width:2,dash:itlDashes[p]},marker:{color:"#ef4444",size:7,symbol:itlSymbols[p]},hovertemplate:"ITL "+p.toUpperCase()+": %{y:.2f} ms<extra></extra>",connectgaps:true});});}';
    s += '  Plotly.newPlot(el,traces,{...lo,height:850,barmode:"group",margin:{t:30,b:80,l:60,r:hasAggItl?110:60},xaxis:{title:"Aggregated Configuration",domain:hasAggItl?[0,0.96]:[0,1]},yaxis:{title:"TTFT (ms) — lower is better",side:"left",tickformat:".2s"},yaxis2:{title:"Throughput Mean (req/s)",side:"right",overlaying:"y",anchor:hasAggItl?"free":"x",position:hasAggItl?0.97:1,titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.18,orientation:"h"}},co);';
    s += '})();';


    // Per-percentile PD charts
    s += 'var pd=ar.filter(function(r){if(r.quality==="discard")return false;if(r.architecture!=="PD")return false;var tid=r.test_id||"";return tid.indexOf("step2-")!==0&&tid.indexOf("step3-")!==0&&tid.indexOf("step11-")!==0&&tid.indexOf("step12-")!==0&&tid.indexOf("step13-")!==0});';
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
    s += '    var eppAnns=[];pd.forEach(function(r,i){if(r.test_id&&r.test_id.indexOf("step11-epp-")===0){eppAnns.push({x:lbls[i],y:ttft[i],yref:"y",text:"<b>EPP TUNED</b>",showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#7c3aed",ax:55,ay:0,font:{size:12,color:"white"},bgcolor:"#7c3aed",borderpad:3,bordercolor:"#7c3aed",borderwidth:1});eppAnns.push({x:lbls[i],y:tput[i],yref:"y2",text:"<b>EPP TUNED</b>",showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#7c3aed",ax:55,ay:0,font:{size:12,color:"white"},bgcolor:"#7c3aed",borderpad:3,bordercolor:"#7c3aed",borderwidth:1})}});';
    s += '    var traces=[{x:lbls,y:ttft,name:"TTFT "+pctl.k.toUpperCase(),type:"scatter",mode:"lines+markers",line:{color:pctl.c,width:3,shape:"spline"},marker:{color:clrs,size:szs,symbol:"circle",line:{width:2,color:"white"}},fill:"tozeroy",fillcolor:pctl.c+"14"},';
    s += '      {x:lbls,y:tput,name:"Throughput Mean",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3,shape:"spline"},marker:{color:"#f59e0b",size:10,symbol:"diamond",line:{width:2,color:"white"}}}];';
    // Aggregated baseline
    s += '    var aggr=ar.filter(function(r){if(r.architecture!=="Aggregated")return false;var tid=r.test_id||"";return tid.indexOf("step11-")!==0&&tid.indexOf("step12-")!==0&&tid.indexOf("step13-")!==0});';
    s += '    if(aggr.length){var ab=aggr[0];var abl=ab["ttft_"+pctl.k];var abt=ab.throughput_mean||ab.throughput_p90;';
    s += '      if(abl!=null){traces.push({x:["Agg Baseline"],y:[abl],name:"Aggregated",type:"scatter",mode:"markers+text",marker:{color:"#94a3b8",size:16,symbol:"star",line:{width:2,color:"white"}},text:[abl.toFixed(0)+"ms"],textposition:"top center",textfont:{size:13,color:"#64748b"},showlegend:true});}';
    s += '      if(abt!=null){traces.push({x:["Agg Baseline"],y:[abt],name:"Agg Throughput",type:"scatter",mode:"markers",yaxis:"y2",marker:{color:"#d4d4d8",size:12,symbol:"star",line:{width:2,color:"white"}},showlegend:false});}}';
    s += '    Plotly.newPlot(el,traces,{...lo,height:850,margin:{t:30,b:80,l:60,r:60},xaxis:{title:"Prefill : Decode Pod Ratio"},yaxis:{title:"TTFT "+pctl.k.toUpperCase()+" (ms)",titlefont:{color:pctl.c},tickfont:{color:pctl.c}},yaxis2:{title:"Throughput Mean (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.18,orientation:"h"},annotations:ttftAnn.concat(eppAnns)},co);';
    s += '  });';
    s += '}';

    // Configuration engine metrics chart
    s += '(function(){var el=document.getElementById("dl-cfg-engine");if(!el)return;';
    s += '  var cfgRes=ar.filter(function(r){if(r.quality==="discard")return false;var tid=r.test_id||"";return tid.indexOf("step2-")!==0&&tid.indexOf("step3-")!==0&&tid.indexOf("step11-")!==0&&tid.indexOf("step12-")!==0&&tid.indexOf("step13-")!==0});';
    s += '  if(!cfgRes.length)return;';
    s += '  var labels=[],kvA=[],kvM=[],rAvg=[],rMax=[],rSpread=[],wAvg=[],decT=[],preT=[],preempt=[];';
    s += '  cfgRes.forEach(function(r){labels.push(_tL(r.config_name,r.architecture));';
    s += '    var ra=r._avg_running||0;var rm=r._avg_running_max||0;';
    s += '    kvA.push((r._kv_cache_avg||0)*100);kvM.push((r._kv_cache_max||0)*100);';
    s += '    rAvg.push(ra);rMax.push(rm);rSpread.push(rm-ra);';
    s += '    wAvg.push(r._avg_waiting||0);decT.push(r._decode_time_avg||0);preT.push(r._prefill_time_avg||0);';
    s += '    preempt.push(r._preemptions_avg||0);});';
    s += '  var traces=[';
    s += '    {x:labels,y:kvA,name:"KV Cache Avg %",type:"bar",marker:{color:"#3b82f6"}},';
    s += '    {x:labels,y:kvM,name:"KV Cache Max %",type:"bar",marker:{color:"#93c5fd"}},';
    s += '    {x:labels,y:rAvg,name:"Avg Running Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#059669",width:3},marker:{size:8}},';
    s += '    {x:labels,y:rMax,name:"Max Running Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#059669",width:2,dash:"dash"},marker:{size:6,symbol:"triangle-up"}},';
    s += '    {x:labels,y:rSpread,name:"Load Spread",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#dc2626",width:2,dash:"dashdot"},marker:{size:6,symbol:"diamond"}},';
    s += '    {x:labels,y:wAvg,name:"Avg Waiting Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#ef4444",width:2,dash:"dot"},marker:{size:6}},';
    s += '    {x:labels,y:decT,name:"Decode req-s/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:2},marker:{size:6}},';
    s += '    {x:labels,y:preT,name:"Prefill req-s/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#8b5cf6",width:2},marker:{size:6}}];';
    s += '  if(preempt.some(function(v){return v>0})){traces.push({x:labels,y:preempt,name:"Preemptions/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#dc2626",width:3},marker:{size:8,symbol:"x"}});}';
    s += '  Plotly.newPlot(el,traces,{barmode:"group",xaxis:{tickangle:-35},yaxis:{title:"KV Cache %",rangemode:"tozero",gridcolor:"#e2e8f0"},yaxis2:{title:"Count / Rate",overlaying:"y",side:"right",rangemode:"tozero"},legend:{x:0,y:1.15,orientation:"h"},margin:{t:40,b:140,l:60,r:60},plot_bgcolor:"#f8fafc",paper_bgcolor:"#fff",hovermode:"x unified",height:850},co);';
    s += '})();';

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
        s += '    var traces=[{x:cx,y:lats,name:"TTFT "+pctl.k.toUpperCase(),type:"scatter",mode:"lines+markers+text",line:{color:pctl.c,width:3},marker:{color:mc,size:12,symbol:"circle",line:{width:2,color:"white"}},text:latText,textposition:"top center",textfont:{size:13,color:pctl.c}},';
        s += '      {x:cx,y:tps,name:"Throughput "+pctl.k.toUpperCase(),type:"scatter",mode:"lines+markers+text",yaxis:"y2",line:{color:"#f59e0b",width:2,dash:"dot"},marker:{color:"#f59e0b",size:8,symbol:"square"},text:tpText,textposition:"bottom center",textfont:{size:12,color:"#f59e0b"}}];';
        s += '    if(bestIdx>=0){traces.push({x:[cx[bestIdx]],y:[lats[bestIdx]],name:"Optimal",type:"scatter",mode:"markers",marker:{color:"#10b981",size:22,symbol:"circle",line:{width:3,color:"white"}},showlegend:true});}';
        s += '    Plotly.newPlot(el,traces,{...lo,height:850,margin:{t:40,b:70,l:60,r:60},title:{text:cl+" — "+pctl.k.toUpperCase()+" Concurrency vs Latency",font:{size:14}},xaxis:{title:"Concurrent Users"},yaxis:{title:"TTFT "+pctl.k.toUpperCase()+" (ms)",side:"left",titlefont:{color:pctl.c}},yaxis2:{title:"Throughput (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.15,orientation:"h"},shapes:[{type:"line",x0:cx[0],x1:cx[cx.length-1],y0:tgtMs,y1:tgtMs,yref:"y",line:{color:"#ef4444",width:pctl.k===tgtPct?2:1.5,dash:"dash"}}],annotations:[{x:cx[cx.length-1],y:tgtMs,yref:"y",text:"SLA: "+tgtMs+"ms",showarrow:false,font:{color:"#ef4444",size:11},xanchor:"right",yanchor:"bottom",yshift:5,bgcolor:"rgba(255,255,255,0.85)"}]},co);';
        s += '  });';
        s += '});}';
    }

    // vLLM charts — architecture-grouped lines, deduped by config name
    s += 'if(cd.vllm&&cd.vllm.configs.length){var v=cd.vllm;';
    s += 'var aC={aggregated:"#059669",pd:"#2563eb",ep:"#7c3aed"};';
    s += 'var aL={aggregated:"Aggregated",pd:"PD",ep:"EP"};';
    s += 'function gA(l){if(l.indexOf("(AG)")>=0||l.indexOf("(aggregated)")>=0)return"aggregated";if(l.indexOf("(EP)")>=0||l.indexOf("(ep)")>=0)return"ep";return"pd";}';
    s += 'function ddCfg(){var b={};v.configs.forEach(function(c,i){if(!b[c])b[c]=i});var r={};Object.keys(b).forEach(function(c){var a=gA(c);if(!r[a])r[a]=[];r[a].push({c:c,i:b[c]})});return r;}';
    s += 'function archPctl(elId,data,unit){var ba=ddCfg();var t=[];["aggregated","pd","ep"].forEach(function(a){var p=ba[a];if(!p||!p.length)return;';
    s += '  var best={};p.forEach(function(x){if(!best[x.c]||(data.p90[x.i]||1e9)<(data.p90[best[x.c].i]||1e9))best[x.c]=x});';
    s += '  var pts=Object.values(best);pts.sort(function(x,y){return(data.p90[x.i]||0)-(data.p90[y.i]||0)});';
    s += '  var c=aC[a];t.push({x:pts.map(function(x){return x.c}),y:pts.map(function(x){return data.p90[x.i]}),name:aL[a]+" P90",mode:"lines+markers",line:{color:c,width:3},marker:{size:10,color:c}});';
    s += '  t.push({x:pts.map(function(x){return x.c}),y:pts.map(function(x){return data.p99[x.i]}),name:aL[a]+" P99",mode:"lines+markers",line:{color:c,width:2,dash:"dot"},marker:{size:8,color:c,symbol:"diamond"}});';
    s += '});Plotly.newPlot(elId,t,{...vl,xaxis:{tickangle:-35},yaxis:{title:unit}},co);}';
    s += 'archPctl("v1",v.ttft,"TTFT (ms)");';
    s += 'archPctl("v2",v.itl,"ITL (ms)");';
    s += 'archPctl("v3",v.e2e,"E2E (s)");';
    // Token throughput — arch grouped
    s += 'var ba4=ddCfg();var t4=[];["aggregated","pd","ep"].forEach(function(a){var p=ba4[a];if(!p)return;p.sort(function(x,y){return(v.token_rates.generation[x.i]||0)-(v.token_rates.generation[y.i]||0)});var c=aC[a];';
    s += '  t4.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return v.token_rates.prompt[x.i]}),name:aL[a]+" Prompt",mode:"lines+markers",line:{color:c,width:2,dash:"dot"},marker:{size:8,color:c}});';
    s += '  t4.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return v.token_rates.generation[x.i]}),name:aL[a]+" Gen",mode:"lines+markers",line:{color:c,width:3},marker:{size:10,color:c}});';
    s += '});Plotly.newPlot("v4",t4,{...vl,xaxis:{tickangle:-35},yaxis:{title:"Tokens/s"}},co);';
    // Queue & KV — arch grouped
    s += 'var ba5=ddCfg();var t5=[];["aggregated","pd","ep"].forEach(function(a){var p=ba5[a];if(!p)return;var c=aC[a];';
    s += '  t5.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return v.request_state.running[x.i]}),name:aL[a]+" Running",mode:"lines+markers",line:{color:c,width:3},marker:{size:10,color:c}});';
    s += '  t5.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return v.request_state.waiting[x.i]}),name:aL[a]+" Waiting",mode:"lines+markers",line:{color:c,width:2,dash:"dot"},marker:{size:8,color:c,symbol:"diamond"}});';
    s += '});var allIdx5=[];Object.values(ba5).forEach(function(p){p.forEach(function(x){allIdx5.push(x)})});';
    s += 't5.push({x:allIdx5.map(function(x){return x.c}),y:allIdx5.map(function(x){return v.request_state.kv_cache[x.i]}),name:"KV Cache %",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:2},marker:{size:10,color:"#f59e0b",symbol:"diamond"}});';
    s += 'Plotly.newPlot("v5",t5,{...vl,margin:{...vl.margin,r:60},xaxis:{tickangle:-35},yaxis:{title:"Count"},yaxis2:{title:"KV Cache %",side:"right",overlaying:"y",range:[0,105]}},co);';
    // Processing time — arch grouped
    s += 'var ba6=ddCfg();var t6=[];["aggregated","pd","ep"].forEach(function(a){var p=ba6[a];if(!p)return;var c=aC[a];';
    s += '  t6.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return v.time_breakdown.prefill[x.i]}),name:aL[a]+" Prefill",mode:"lines+markers",line:{color:c,width:3},marker:{size:10,color:c}});';
    s += '  t6.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return v.time_breakdown.decode[x.i]}),name:aL[a]+" Decode",mode:"lines+markers",line:{color:c,width:2,dash:"dot"},marker:{size:8,color:c}});';
    s += '});var allIdx6=[];Object.values(ba6).forEach(function(p){p.forEach(function(x){allIdx6.push(x)})});';
    s += 't6.push({x:allIdx6.map(function(x){return x.c}),y:allIdx6.map(function(x){return v.time_breakdown.preemptions[x.i]}),name:"Preemptions/s",mode:"markers",yaxis:"y2",marker:{size:10,color:"#ef4444",symbol:"triangle-up"}});';
    s += 'Plotly.newPlot("v6",t6,{...vl,margin:{...vl.margin,r:60},xaxis:{tickangle:-35},yaxis:{title:"Time Rate (s/s)"},yaxis2:{title:"Preemptions/s",side:"right",overlaying:"y"}},co);';
    // Pod network — arch grouped
    s += 'if(v.network&&v.network.pod_tx&&v.network.pod_tx.some(function(x){return x>0})&&document.getElementById("v7")){';
    s += '  var ba7=ddCfg();var t7=[];["aggregated","pd","ep"].forEach(function(a){var p=ba7[a];if(!p)return;var c=aC[a];';
    s += '  t7.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return v.network.pod_tx[x.i]}),name:aL[a]+" TX",mode:"lines+markers",line:{color:c,width:3},marker:{size:10,color:c}});';
    s += '  t7.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return v.network.pod_rx[x.i]}),name:aL[a]+" RX",mode:"lines+markers",line:{color:c,width:2,dash:"dot"},marker:{size:8,color:c}});';
    s += '  });Plotly.newPlot("v7",t7,{...vl,xaxis:{tickangle:-35},yaxis:{title:"MB/s"}},co);}';
    // NIXL — arch grouped
    s += 'if(document.getElementById("v8")){';
    s += '  var nixlData=v.network&&v.network.nixl_tx?v.network.nixl_tx:[];';
    s += '  var ibData=v.network&&v.network.ib_rx?v.network.ib_rx:[];';
    s += '  if(nixlData.some(function(x){return x>0})||ibData.some(function(x){return x>0})){';
    s += '    var ba8=ddCfg();var t8=[];["aggregated","pd","ep"].forEach(function(a){var p=ba8[a];if(!p)return;var c=aC[a];';
    s += '    if(nixlData.some(function(x){return x>0})){t8.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return nixlData[x.i]}),name:aL[a]+" NIXL TX",mode:"lines+markers+text",text:p.map(function(x){var vv=nixlData[x.i];return vv>0?vv.toFixed(2):""}),textposition:"top center",textfont:{size:13,color:c},line:{color:c,width:3},marker:{size:10,color:c}});}';
    s += '    if(ibData.some(function(x){return x>0})){t8.push({x:p.map(function(x){return x.c}),y:p.map(function(x){return ibData[x.i]}),name:aL[a]+" IB RX",mode:"lines+markers",line:{color:c,width:2,dash:"dot"},marker:{size:8,color:c}});}';
    s += '    });if(t8.length){Plotly.newPlot("v8",t8,{...vl,xaxis:{tickangle:-35},yaxis:{title:"Throughput (GB/s)"}},co);}}';
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
        s += '    var traces=[{x:xLabels,y:lats,name:"TTFT "+pctl.l,type:"scatter",mode:"lines+markers+text",line:{color:pctl.c,width:3,shape:"spline"},marker:{color:mc,size:12,symbol:"circle",line:{width:2,color:"white"}},text:latText,textposition:"top center",textfont:{size:14,color:pctl.c},fill:"tozeroy",fillcolor:pctl.c+"14"},';
        s += '      {x:xLabels,y:tps,name:"Throughput "+pctl.l,type:"scatter",mode:"lines+markers+text",yaxis:"y2",line:{color:"#f59e0b",width:3,shape:"spline"},marker:{color:"#f59e0b",size:10,symbol:"diamond",line:{width:2,color:"white"}},text:tpText,textposition:"bottom center",textfont:{size:13,color:"#f59e0b"}}];';
        s += '    if(bestIdx>=0){traces.push({x:[xLabels[bestIdx]],y:[lats[bestIdx]],name:"Best EPP",type:"scatter",mode:"markers",marker:{color:"#10b981",size:22,symbol:"circle",line:{width:3,color:"white"}},showlegend:true});}';
        // Baseline
        s += '    var bl=(eppD.baselines||{})[arch];';
        s += '    if(bl){var blT=bl["ttft_"+pctl.k];var blP=bl["throughput_"+pctl.k]||bl.throughput_p90;';
        s += '      if(blT!=null){traces.push({x:["Baseline"],y:[blT],name:"Baseline ("+bl.config_name+")",type:"scatter",mode:"markers+text",marker:{color:"#94a3b8",size:18,symbol:"star",line:{width:2,color:"white"}},text:[blT.toFixed(0)+"ms"],textposition:"top center",textfont:{size:14,color:"#64748b"},showlegend:true});}';
        s += '      if(blP!=null){traces.push({x:["Baseline"],y:[blP],name:"Baseline Tput",type:"scatter",mode:"markers",yaxis:"y2",marker:{color:"#d4d4d8",size:14,symbol:"star",line:{width:2,color:"white"}},showlegend:false});}}';
        s += '    var shapes=[];var annotations=[];';
        s += '    if(tgtMs){shapes.push({type:"line",x0:-0.5,x1:xLabels.length-0.5,y0:tgtMs,y1:tgtMs,yref:"y",line:{color:"#ef4444",width:2,dash:"dash"}});annotations.push({x:xLabels.length-1,y:tgtMs,yref:"y",text:"SLA: "+tgtMs+"ms",showarrow:false,font:{color:"#ef4444",size:11},xanchor:"right",yanchor:"bottom",yshift:5,bgcolor:"rgba(255,255,255,0.85)"});}';
        s += '    Plotly.newPlot(el,traces,{...lo,height:850,margin:{t:30,b:80,l:60,r:60},xaxis:{title:"EPP Strategy"},yaxis:{title:"TTFT "+pctl.l+" (ms)",side:"left",titlefont:{color:pctl.c},tickfont:{color:pctl.c}},yaxis2:{title:"Throughput "+pctl.l+" (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.18,orientation:"h"},shapes:shapes,annotations:annotations},co);';
        s += '  });';
        // E2E per workload (guidellm)
        s += '  var wlE2eEl=document.getElementById("dl-epp-"+arch+"-wl-e2e");if(wlE2eEl){';
        s += '    var wn=[];trials.forEach(function(t){(t.workloads||[]).forEach(function(w){if(wn.indexOf(w.name)<0)wn.push(w.name)})});';
        s += '    var wc=["#3b82f6","#dc2626","#059669","#f59e0b","#7c3aed"];';
        s += '    var wtr=wn.map(function(wl,i){var vs=trials.map(function(t){var w=(t.workloads||[]).filter(function(x){return x.name===wl})[0];return w&&w.e2e_latency_p90!=null?w.e2e_latency_p90:null});';
        s += '      return {x:trials.map(function(t){return t.name}),y:vs,name:wl,type:"scatter",mode:"lines+markers+text",text:vs.map(function(v){return v!=null?v.toFixed(1)+"s":""}),textposition:"top center",textfont:{size:11,color:wc[i%wc.length]},line:{color:wc[i%wc.length],width:3},marker:{color:wc[i%wc.length],size:9},hovertemplate:"<b>%{x}</b><br>"+wl+" E2E P90: %{y:.1f}s<extra></extra>",connectgaps:true};';
        s += '    }).filter(function(tr){return tr.y.some(function(v){return v!=null})});';
        s += '    if(wtr.length){Plotly.newPlot(wlE2eEl,wtr,{...lo,height:600,margin:{t:30,b:90,l:60,r:40},xaxis:{title:"EPP Strategy",tickangle:-25},yaxis:{title:"E2E P90 (s)",tickformat:",.1f"},showlegend:true,legend:{x:0,y:1.12,orientation:"h"}},co);}';
        s += '  }';
        s += '});';
    }

    // Concurrency Sweep charts
    if (data.concurrency_sweep && Object.keys(data.concurrency_sweep).length) {
        s += 'var csData=' + JSON.stringify(data.concurrency_sweep) + ';';
        s += 'var csColors=["#3b82f6","#10b981","#f59e0b","#ef4444","#8b5cf6","#0ea5e9","#ec4899","#14b8a6"];';
        s += '[{k:"p90"},{k:"p95"},{k:"p99"}].forEach(function(pctl){';
        s += '  var el=document.getElementById("dl-sweep-ttft-"+pctl.k);if(!el)return;';
        s += '  var traces=[];var ci=0;var calShapes=[];';
        s += '  Object.keys(csData).forEach(function(cfgKey){';
        s += '    var pts=[].concat(csData[cfgKey]).sort(function(a,b){return a.concurrency-b.concurrency});';
        s += '    var cx=pts.map(function(p){return p.concurrency});';
        s += '    var lats=pts.map(function(p){return p["ttft_"+pctl.k]});';
        s += '    var color=csColors[ci%csColors.length];ci++;';
        s += '    var label=pts[0]&&pts[0].config_label?pts[0].config_label:cfgKey;var _at=cfgKey.indexOf("pd")===0?" (PD)":(cfgKey.indexOf("ep")===0?" (EP)":" (AG)");if(label.indexOf("(")<0)label+=_at;';
        s += '    traces.push({x:cx,y:lats,name:label,type:"scatter",mode:"lines+markers+text",';
        s += '      text:lats.map(function(v){return v?Math.round(v).toLocaleString():""}),';
        s += '      textposition:"top center",textfont:{size:13,color:color},';
        s += '      line:{color:color,width:3},marker:{color:color,size:8},';
        s += '      hovertemplate:"<b>"+label+"</b><br>%{x} users<br>TTFT "+pctl.k.toUpperCase()+": %{y:.0f}ms<extra></extra>"});';
        s += '    pts.forEach(function(p){if(p.is_calibrated){';
        s += '      calShapes.push({type:"line",x0:p.concurrency,x1:p.concurrency,y0:0,y1:1,yref:"paper",line:{color:"#059669",width:1.5,dash:"dash"}});';
        s += '    }});';
        s += '  });';
        s += '  if(calShapes.length){traces.push({x:[calShapes[0].x0],y:[null],mode:"lines",name:"Calibrated",line:{color:"#059669",width:1.5,dash:"dash"},showlegend:true});}';
        s += '  var allX=[];traces.forEach(function(t){if(t.x)t.x.forEach(function(v){if(v!=null&&allX.indexOf(v)===-1)allX.push(v)})});allX.sort(function(a,b){return a-b});';
        s += '  Plotly.newPlot(el,traces,{xaxis:{title:"Concurrent Users",gridcolor:"#e2e8f0",tickmode:"array",tickvals:allX},';
        s += '    yaxis:{title:"TTFT "+pctl.k.toUpperCase()+" (ms)",gridcolor:"#e2e8f0"},';
        s += '    plot_bgcolor:"#f8fafc",paper_bgcolor:"#fff",margin:{t:20,b:60,l:70,r:20},';
        s += '    legend:{x:0,y:1,bgcolor:"rgba(255,255,255,0.9)"},shapes:calShapes,hovermode:"closest"},co);';
        s += '});';
        s += '(function(){var el=document.getElementById("dl-sweep-tput-gpu");if(!el)return;';
        s += '  var traces=[];var ci=0;var calShapes=[];';
        s += '  Object.keys(csData).forEach(function(cfgKey){';
        s += '    var pts=[].concat(csData[cfgKey]).sort(function(a,b){return a.concurrency-b.concurrency});';
        s += '    var cx=pts.map(function(p){return p.concurrency});';
        s += '    var tpg=pts.map(function(p){return p.throughput_per_gpu});';
        s += '    var color=csColors[ci%csColors.length];ci++;';
        s += '    var label=pts[0]&&pts[0].config_label?pts[0].config_label:cfgKey;var _at=cfgKey.indexOf("pd")===0?" (PD)":(cfgKey.indexOf("ep")===0?" (EP)":" (AG)");if(label.indexOf("(")<0)label+=_at;';
        s += '    traces.push({x:cx,y:tpg,name:label,type:"scatter",mode:"lines+markers+text",';
        s += '      text:tpg.map(function(v){return v?Math.round(v).toLocaleString():""}),';
        s += '      textposition:"top center",textfont:{size:13,color:color},';
        s += '      line:{color:color,width:3},marker:{color:color,size:8},';
        s += '      hovertemplate:"<b>"+label+"</b><br>%{x} users<br>%{y:.0f} tok/s/gpu<extra></extra>"});';
        s += '    pts.forEach(function(p){if(p.is_calibrated){';
        s += '      calShapes.push({type:"line",x0:p.concurrency,x1:p.concurrency,y0:0,y1:1,yref:"paper",line:{color:"#059669",width:1.5,dash:"dash"}});';
        s += '    }});';
        s += '  });';
        s += '  if(calShapes.length){traces.push({x:[calShapes[0].x0],y:[null],mode:"lines",name:"Calibrated",line:{color:"#059669",width:1.5,dash:"dash"},showlegend:true});}';
        s += '  var allX=[];traces.forEach(function(t){if(t.x)t.x.forEach(function(v){if(v!=null&&allX.indexOf(v)===-1)allX.push(v)})});allX.sort(function(a,b){return a-b});';
        s += '  Plotly.newPlot(el,traces,{xaxis:{title:"Concurrent Users",gridcolor:"#e2e8f0",tickmode:"array",tickvals:allX},';
        s += '    yaxis:{title:"Token Throughput per GPU (tok/s/gpu)",gridcolor:"#e2e8f0"},';
        s += '    plot_bgcolor:"#f8fafc",paper_bgcolor:"#fff",margin:{t:20,b:60,l:70,r:20},';
        s += '    legend:{x:0,y:1,bgcolor:"rgba(255,255,255,0.9)"},shapes:calShapes,hovermode:"closest"},co);';
        s += '})();';
        s += '(function(){var el=document.getElementById("dl-sweep-token-tput");if(!el)return;';
        s += '  var sweepTk=ar.filter(function(r){if(r.quality==="discard")return false;var tid=r.test_id||"";if(tid.indexOf("step11-")!==0&&tid.indexOf("step12-")!==0)return false;return r._gen_tps_avg||r.output_tps_mean;});';
        s += '  if(!sweepTk.length)return;';
        s += '  sweepTk.sort(function(a,b){var aa=a.architecture||"";var ba=b.architecture||"";if(aa!==ba)return aa.localeCompare(ba);return(a.concurrency||0)-(b.concurrency||0)});';
        s += '  var labels=sweepTk.map(function(r){return _tL(r.config_name,r.architecture)+" c="+(r.concurrency||"?")});';
        s += '  var comp=[],srv=[],cli=[];';
        s += '  sweepTk.forEach(function(r){';
        s += '    var pA=r._prompt_tps_avg||0;var gA=r._gen_tps_avg||0;';
        s += '    srv.push(Math.round(pA+gA));';
        s += '    var cH=r._cache_hits_avg||0;var cQ=r._cache_queries_avg||0;';
        s += '    var cR=cQ>0?cH/cQ:0;comp.push(Math.round(pA*(1-cR))+Math.round(gA));';
        s += '    var tp=r._throughput_mean_mj||r.throughput_mean||0;cli.push(Math.round((r._prompt_tokens_mean||0)*tp+(r._output_tokens_mean||0)*tp));';
        s += '  });';
        s += '  Plotly.newPlot(el,[';
        s += '    {x:labels,y:comp,name:"GPU computed",type:"bar",marker:{color:"#3b82f6"},text:comp.map(function(v){return v.toLocaleString()}),textposition:"outside",textfont:{size:12,color:"#1e293b"}},';
        s += '    {x:labels,y:srv,name:"Server total (incl. cached)",type:"bar",marker:{color:"#93c5fd"},text:srv.map(function(v){return v.toLocaleString()}),textposition:"outside",textfont:{size:12,color:"#1e293b"}},';
        s += '    {x:labels,y:cli,name:"Client (guidellm)",type:"bar",marker:{color:"#f59e0b"},text:cli.map(function(v){return v.toLocaleString()}),textposition:"outside",textfont:{size:12,color:"#1e293b"}}';
        s += '  ],{...lo,height:850,margin:{t:60,b:160,l:60,r:20},barmode:"group",xaxis:{tickangle:-45},yaxis:{title:"Total Tokens/s (Prompt + Generation)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);';
        s += '})();';
        // Sweep engine metrics chart
        s += '(function(){var el=document.getElementById("dl-sweep-engine");if(!el||!csData)return;';
        s += '  var labels=[],tColors=[],eKvA=[],eKvM=[],eRun=[],eRunMax=[],eRunSpread=[],eWait=[],eDec=[],ePre=[],ePreempt=[];';
        s += '  var arLk={};ar.forEach(function(r){arLk[r.test_id]=r});';
        s += '  Object.keys(csData).forEach(function(cfgKey){';
        s += '    var isPd=cfgKey.indexOf("pd")===0;var isEp=cfgKey.indexOf("ep")===0;';
        s += '    var aTag=isEp?" (EP)":isPd?" (PD)":" (AG)";';
        s += '    var color=csColors[Object.keys(csData).indexOf(cfgKey)%csColors.length];';
        s += '    var pts=[].concat(csData[cfgKey]).sort(function(a,b){return a.concurrency-b.concurrency});';
        s += '    pts.forEach(function(p){';
        s += '      var lbl=(p.config_label||cfgKey);if(lbl.indexOf("(")<0)lbl+=aTag;';
        s += '      labels.push(lbl+" c="+p.concurrency);tColors.push(color);';
        s += '      var r=arLk[p.test_id]||{};';
        s += '      eKvA.push((r._kv_cache_avg||0)*100);eKvM.push((r._kv_cache_max||0)*100);';
        s += '      eRun.push(r._avg_running||0);eRunMax.push(r._avg_running_max||0);eRunSpread.push((r._avg_running_max||0)-(r._avg_running||0));eWait.push(r._avg_waiting||0);';
        s += '      eDec.push(r._decode_time_avg||0);ePre.push(r._prefill_time_avg||0);';
        s += '      ePreempt.push(r._preemptions_avg||0);});});';
        s += '  if(!labels.length)return;';
        s += '  var xIdx=labels.map(function(_,i){return i});';
        s += '  var traces=[';
        s += '    {x:xIdx,y:eKvA,name:"KV Cache Avg %",type:"bar",marker:{color:"#3b82f6"}},';
        s += '    {x:xIdx,y:eKvM,name:"KV Cache Max %",type:"bar",marker:{color:"#93c5fd"}},';
        s += '    {x:xIdx,y:eRun,name:"Avg Running Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#059669",width:3},marker:{size:8}},';
        s += '    {x:xIdx,y:eRunMax,name:"Max Running Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#059669",width:2,dash:"dash"},marker:{size:6,symbol:"triangle-up"}},';
        s += '    {x:xIdx,y:eRunSpread,name:"Load Spread",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#dc2626",width:2,dash:"dashdot"},marker:{size:6,symbol:"diamond"}},';
        s += '    {x:xIdx,y:eWait,name:"Avg Waiting Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#ef4444",width:2,dash:"dot"},marker:{size:6}},';
        s += '    {x:xIdx,y:eDec,name:"Decode req-s/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:2},marker:{size:6}},';
        s += '    {x:xIdx,y:ePre,name:"Prefill req-s/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#8b5cf6",width:2},marker:{size:6}}];';
        s += '  if(ePreempt.some(function(v){return v>0})){traces.push({x:xIdx,y:ePreempt,name:"Preemptions/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#dc2626",width:3},marker:{size:8,symbol:"x"}});}';
        s += '  var anns=labels.map(function(lbl,i){return{x:i,y:-0.02,xref:"x",yref:"paper",text:lbl,showarrow:false,font:{size:13,color:tColors[i]||"#333"},textangle:-45,xanchor:"right",yanchor:"top"}});';
        s += '  Plotly.newPlot(el,traces,{barmode:"group",xaxis:{showticklabels:false},yaxis:{title:"KV Cache %",rangemode:"tozero",gridcolor:"#e2e8f0"},yaxis2:{title:"Count / Rate",overlaying:"y",side:"right",rangemode:"tozero"},legend:{x:0,y:1.15,orientation:"h"},margin:{t:40,b:180,l:60,r:60},plot_bgcolor:"#f8fafc",paper_bgcolor:"#fff",hovermode:"x unified",height:850,annotations:anns},co);';
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

    // Sweep Pareto: Throughput vs Interactivity (1:1 with UI charts.js)
    s += '(function(){var el=document.getElementById("dl-sweep-pareto-inter");if(!el||!csData)return;';
    s += '  var allPts=[];';
    s += '  Object.keys(csData).forEach(function(cfgKey){';
    s += '    var isPd=cfgKey.indexOf("pd")===0;var isEp=cfgKey.indexOf("ep")===0;';
    s += '    var arch=(isPd||isEp)?"PD":"AGGREGATED";';
    s += '    var aTag=isEp?" (EP)":isPd?" (PD)":" (AG)";';
    s += '    csData[cfgKey].forEach(function(p){';
    s += '      var tputGpu=p.throughput_per_gpu||0;var inter=(p.vllm_tps||p.output_tps_mean||0)/(p.concurrency||1);';
    s += '      var lbl=p.config_label||cfgKey;if(lbl.indexOf("(")<0)lbl+=aTag;';
    s += '      if(tputGpu>0&&inter>0){allPts.push({x:inter,y:tputGpu,nx:inter,ny:tputGpu,label:lbl,conc:p.concurrency,arch:arch,cfgKey:cfgKey});}';
    s += '    });});';
    s += '  if(allPts.length<2)return;';
    s += '  function frontierLine(points){';
    s += '    if(!points.length)return[];';
    s += '    var sorted=points.slice().sort(function(a,b){return b.nx-a.nx});';
    s += '    var frontier=[];var maxY=-Infinity;';
    s += '    for(var i=0;i<sorted.length;i++){if(sorted[i].ny>maxY){frontier.push(sorted[i]);maxY=sorted[i].ny;}}';
    s += '    frontier.sort(function(a,b){return a.nx-b.nx});';
    s += '    return frontier;}';
    s += '  var cfgColors=["#dc2626","#2563eb","#059669","#d97706","#7c3aed","#db2777","#0891b2","#65a30d","#ea580c","#6d28d9"];';
    s += '  var cfgGroups={};allPts.forEach(function(p){if(!cfgGroups[p.cfgKey])cfgGroups[p.cfgKey]=[];cfgGroups[p.cfgKey].push(p);});';
    s += '  var traces=[];var annotations=[];var ci=0;';
    s += '  Object.keys(cfgGroups).sort().forEach(function(key){';
    s += '    var pts=cfgGroups[key];var color=cfgColors[ci%cfgColors.length];ci++;';
    s += '    traces.push({x:pts.map(function(p){return p.nx}),y:pts.map(function(p){return p.ny}),';
    s += '      text:pts.map(function(p){return p.label+"<br>"+p.x.toFixed(1)+" tok/s/user<br>"+p.y.toFixed(0)+" tok/s/GPU<br>c="+p.conc;}),';
    s += '      name:key,mode:"markers",marker:{color:color,size:14},hovertemplate:"<b>%{text}</b><extra></extra>"});';
    s += '  });';
    s += '  var pdAll=allPts.filter(function(p){return p.arch==="PD"});';
    s += '  var aggAll=allPts.filter(function(p){return p.arch==="AGGREGATED"});';
    s += '  var aggPareto=frontierLine(aggAll),pdPareto=frontierLine(pdAll);';
    s += '  if(aggPareto.length>1){traces.push({x:aggPareto.map(function(p){return p.nx}),y:aggPareto.map(function(p){return p.ny}),';
    s += '    name:"Aggregated — Frontier",mode:"lines",line:{color:"#dc2626",width:3,dash:"dot"},hoverinfo:"skip"});}';
    s += '  if(pdPareto.length>1){traces.push({x:pdPareto.map(function(p){return p.nx}),y:pdPareto.map(function(p){return p.ny}),';
    s += '    name:"Disaggregation — Frontier",mode:"lines",line:{color:"#2563eb",width:3,dash:"dot"},hoverinfo:"skip"});}';
    s += '  var positions=[[80,-15],[-80,-15],[70,-45],[-70,-45],[90,-30],[-90,-30],[60,-60],[-60,-60]];';
    s += '  function addAnnot(pts,color){pts.forEach(function(p,i){';
    s += '    var pos=positions[i%positions.length];';
    s += '    annotations.push({x:p.nx,y:p.ny,text:p.cfgKey+"<br>c="+p.conc,showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:color,';
    s += '      ax:pos[0],ay:pos[1],font:{size:13,color:color},bgcolor:"rgba(255,255,255,0)",borderpad:2});});}';
    s += '  addAnnot(aggPareto,"#dc2626");addAnnot(pdPareto,"#2563eb");';
    s += '  Plotly.newPlot(el,traces,{...lo,height:850,';
    s += '    xaxis:{title:"Interactivity (tok/s/user)",gridcolor:"#d1d5db",rangemode:"tozero"},';
    s += '    yaxis:{title:"Token Throughput per GPU (tok/s/GPU)",gridcolor:"#d1d5db",rangemode:"tozero"},';
    s += '    plot_bgcolor:"white",paper_bgcolor:"white",';
    s += '    showlegend:true,legend:{x:1.02,y:1,xanchor:"left",bgcolor:"rgba(255,255,255,0.95)",bordercolor:"#e2e8f0",borderwidth:1},';
    s += '    margin:{t:40,b:70,l:70,r:220},annotations:annotations},co);';
    s += '})();';

    // Sweep Pareto: Throughput vs TTFT (matching UI style)
    s += '(function(){var el=document.getElementById("dl-sweep-pareto-ttft");if(!el||!csData)return;';
    s += '  var aggPts=[],pdPts=[];';
    s += '  Object.keys(csData).forEach(function(cfgKey){';
    s += '    var isPd=cfgKey.indexOf("pd")===0;var isEp=cfgKey.indexOf("ep")===0;';
    s += '    var aTag=isEp?" (EP)":isPd?" (PD)":" (AG)";';
    s += '    csData[cfgKey].forEach(function(p){';
    s += '      var tputGpu=p.throughput_per_gpu||0;var ttft=p.ttft_p90||0;';
    s += '      var lbl=p.config_label||cfgKey;if(lbl.indexOf("(")<0)lbl+=aTag;';
    s += '      if(tputGpu>0&&ttft>0){var pt={x:ttft,y:tputGpu,label:lbl,conc:p.concurrency};';
    s += '        if(isPd){pdPts.push(pt);}else{aggPts.push(pt);}}';
    s += '    });});';
    s += '  if(aggPts.length+pdPts.length<2)return;';
    s += '  function ttftFrontier(points){';
    s += '    if(!points.length)return[];';
    s += '    var sorted=points.slice().sort(function(a,b){return a.x-b.x});';
    s += '    var frontier=[];var maxY=-Infinity;';
    s += '    for(var i=0;i<sorted.length;i++){if(sorted[i].y>maxY){frontier.push(sorted[i]);maxY=sorted[i].y;}}';
    s += '    return frontier;}';
    s += '  var aggPareto=ttftFrontier(aggPts),pdPareto=ttftFrontier(pdPts);';
    s += '  var allTtftPts=aggPts.concat(pdPts);';
    s += '  var allX=allTtftPts.map(function(p){return p.x}).sort(function(a,b){return a-b});';
    s += '  var p95Idx=Math.min(Math.floor(allX.length*0.95),allX.length-1);';
    s += '  var xMax=allX[p95Idx]*1.2;';
    s += '  var frontierMaxX=0;';
    s += '  aggPareto.concat(pdPareto).forEach(function(p){if(p.x>frontierMaxX)frontierMaxX=p.x;});';
    s += '  if(frontierMaxX>xMax)xMax=frontierMaxX*1.1;';
    s += '  var traces=[];';
    s += '  if(aggPts.length){var visAgg=aggPts.filter(function(p){return p.x<=xMax});';
    s += '    traces.push({x:visAgg.map(function(p){return p.x}),y:visAgg.map(function(p){return p.y}),';
    s += '    text:visAgg.map(function(p){return p.label+"<br>TTFT P90: "+p.x.toFixed(0)+"ms<br>"+p.y.toFixed(0)+" tok/s/GPU";}),';
    s += '    name:"Aggregated",mode:"markers",marker:{color:"#fca5a5",size:14,opacity:0.5},';
    s += '    hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '  if(pdPts.length){var visPd=pdPts.filter(function(p){return p.x<=xMax});';
    s += '    traces.push({x:visPd.map(function(p){return p.x}),y:visPd.map(function(p){return p.y}),';
    s += '    text:visPd.map(function(p){return p.label+"<br>TTFT P90: "+p.x.toFixed(0)+"ms<br>"+p.y.toFixed(0)+" tok/s/GPU";}),';
    s += '    name:"Disaggregation",mode:"markers",marker:{color:"#93c5fd",size:14,opacity:0.5},';
    s += '    hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '  if(aggPareto.length){traces.push({x:aggPareto.map(function(p){return p.x}),y:aggPareto.map(function(p){return p.y}),';
    s += '    text:aggPareto.map(function(p){return p.label+"<br>"+p.x.toFixed(0)+"ms / "+p.y.toFixed(0)+" tok/s/GPU";}),';
    s += '    name:"Aggregated — Frontier",mode:aggPareto.length>1?"lines+markers+text":"markers+text",';
    s += '    line:{color:"#dc2626",width:3},marker:{color:"#dc2626",size:8},';
    s += '    textposition:"top right",textfont:{size:13,color:"#dc2626"},hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '  if(pdPareto.length){traces.push({x:pdPareto.map(function(p){return p.x}),y:pdPareto.map(function(p){return p.y}),';
    s += '    text:pdPareto.map(function(p){return p.label+"<br>"+p.x.toFixed(0)+"ms / "+p.y.toFixed(0)+" tok/s/GPU";}),';
    s += '    name:"Disaggregation — Frontier",mode:pdPareto.length>1?"lines+markers+text":"markers+text",';
    s += '    line:{color:"#2563eb",width:3},marker:{color:"#2563eb",size:8},';
    s += '    textposition:"top left",textfont:{size:13,color:"#2563eb"},hovertemplate:"<b>%{text}</b><extra></extra>"});}';
    s += '  Plotly.newPlot(el,traces,{...lo,height:850,';
    s += '    xaxis:{title:"TTFT P90 (ms)",gridcolor:"#d1d5db",range:[0,xMax]},';
    s += '    yaxis:{title:"Token Throughput per GPU (tok/s/GPU)",gridcolor:"#d1d5db",rangemode:"tozero"},';
    s += '    plot_bgcolor:"white",paper_bgcolor:"white",';
    s += '    showlegend:true,legend:{x:1,y:1,xanchor:"right",bgcolor:"rgba(255,255,255,0.95)",bordercolor:"#e2e8f0",borderwidth:1},';
    s += '    margin:{t:40,b:70,l:70,r:160}},co);';
     s += '})();';

    // vLLM Token Throughput vs Concurrency (server total vs output only)
    s += '(function(){var el=document.getElementById("dl-sweep-vllm-tps");if(!el||!csData)return;';
    s += '  var hasVllm=false;Object.keys(csData).forEach(function(k){csData[k].forEach(function(p){if(p.vllm_tps)hasVllm=true;});});';
    s += '  if(!hasVllm)return;';
    s += '  var ci=0;var traces=[];';
    s += '  Object.keys(csData).forEach(function(cfgKey){';
    s += '    var pts=[].concat(csData[cfgKey]).sort(function(a,b){return a.concurrency-b.concurrency});';
    s += '    var color=csColors[ci%csColors.length];ci++;';
    s += '    var label=pts[0]&&pts[0].config_label?pts[0].config_label:cfgKey;var _at=cfgKey.indexOf("pd")===0?" (PD)":(cfgKey.indexOf("ep")===0?" (EP)":" (AG)");if(label.indexOf("(")<0)label+=_at;';
    s += '    traces.push({x:pts.map(function(p){return p.concurrency}),y:pts.map(function(p){return p.vllm_tps||0}),name:label+" (server total)",mode:"lines+markers",line:{color:color,width:3},marker:{size:8,color:color},hovertemplate:"<b>"+label+"</b><br>%{x} users<br>Server: %{y:,.0f} tok/s<extra></extra>"});';
    s += '    traces.push({x:pts.map(function(p){return p.concurrency}),y:pts.map(function(p){return p.output_tps_mean||0}),name:label+" (output only)",mode:"lines+markers",line:{color:color,width:2,dash:"dot"},marker:{size:6,color:color},hovertemplate:"<b>"+label+"</b><br>%{x} users<br>Output: %{y:,.0f} tok/s<extra></extra>",showlegend:false});';
    s += '  });';
    s += '  Plotly.newPlot(el,traces,{xaxis:{title:"Concurrent Users",gridcolor:"#e2e8f0"},yaxis:{title:"Total Tokens/s",gridcolor:"#e2e8f0"},plot_bgcolor:"#f8fafc",paper_bgcolor:"#fff",margin:{t:20,b:60,l:70,r:20},legend:{x:0,y:1,bgcolor:"rgba(255,255,255,0.9)"},hovermode:"closest"},co);';
    s += '})();';

    // Best Envelope + Overlaid architecture comparison (needs ≥2 arch config keys)
    s += '(function(){var envEl=document.getElementById("dl-sweep-envelope");var olEl=document.getElementById("dl-sweep-overlaid");';
    s += '  if(!envEl&&!olEl)return;';
    s += '  var archKeys=Object.keys(csData||{});if(archKeys.length<2)return;';
    s += '  var envColorList=["#3b82f6","#10b981","#f59e0b","#ef4444","#8b5cf6","#0ea5e9","#ec4899","#14b8a6"];var envColorMap={};var envCIdx=0;';
    s += '  function getEnvColor(label){if(!envColorMap[label]){envColorMap[label]=envColorList[envCIdx%envColorList.length];envCIdx++;}return envColorMap[label];}';
    s += '  function sweepArchLabel(key){if(key.indexOf("pd")===0)return "PD";if(key.indexOf("agg")===0)return "AGGREGATED";if(key.indexOf("ep")===0)return "EP";return key.toUpperCase();}';
    s += '  var bestByConc={},bestOverall={};';
    s += '  archKeys.forEach(function(arch){var pts=csData[arch];if(!pts)return;';
    s += '    var archLabel=sweepArchLabel(arch);var isDisagg=arch.indexOf("pd")===0||arch.indexOf("ep")===0;';
    s += '    pts.forEach(function(p){var c=p.concurrency;if(!bestByConc[c])bestByConc[c]={};';
    s += '      var cat=isDisagg?"disagg":"agg";';
    s += '      if(!bestByConc[c][cat]||p.throughput_per_gpu>bestByConc[c][cat].tputGpu){';
    s += '        var _lbl=p.config_label||archLabel;var _at2=arch.indexOf("pd")===0?" (PD)":(arch.indexOf("ep")===0?" (EP)":" (AG)");if(_lbl.indexOf("(")<0)_lbl+=_at2;';
    s += '        bestByConc[c][cat]={tputGpu:p.throughput_per_gpu,ttft:p.ttft_p90,label:_lbl,conc:c};}';
    s += '      var tputGpu=p.throughput_per_gpu||0;var label=p.config_label||archLabel;';
    s += '      if(!bestOverall[c]||tputGpu>bestOverall[c].tputGpu){bestOverall[c]={tputGpu:tputGpu,label:label,conc:c};}';
    s += '    });});';
    s += '  if(envEl){var concs=Object.keys(bestOverall).map(Number).sort(function(a,b){return a-b;});';
    s += '    var xs=[],ys=[],colors=[],texts=[];concs.forEach(function(c){var b=bestOverall[c];xs.push(c);ys.push(b.tputGpu);colors.push(getEnvColor(b.label));texts.push(b.label+"<br>c="+c+"<br>"+b.tputGpu.toFixed(0)+" tok/s/GPU");});';
    s += '    var anns=[];concs.forEach(function(c,i){var b=bestOverall[c];var angles=[[-70,-20],[70,-20],[-50,-40],[50,-40],[-80,-10],[80,-10],[-60,-50],[60,-50]];var a=angles[i%angles.length];';
    s += '      anns.push({x:c,y:b.tputGpu,text:b.label,showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:getEnvColor(b.label),ax:a[0],ay:a[1],font:{size:13,color:getEnvColor(b.label)},bgcolor:"rgba(255,255,255,0)",borderpad:2});});';
    s += '    Plotly.newPlot(envEl,[{x:xs,y:ys,text:texts,name:"Best Config",mode:"lines+markers",line:{color:"#475569",width:2},marker:{color:colors,size:14,line:{color:"#fff",width:2}},hovertemplate:"<b>%{text}</b><extra></extra>"}],{...lo,height:850,xaxis:{title:"Concurrent Users",gridcolor:"#d1d5db",tickmode:"array",tickvals:concs},yaxis:{title:"Best Throughput per GPU (tok/s/GPU)",gridcolor:"#d1d5db"},showlegend:false,margin:{t:40,b:70,l:70,r:40},plot_bgcolor:"white",paper_bgcolor:"white",annotations:anns},co);}';
    s += '  if(olEl){var olConcs=Object.keys(bestOverall).map(Number).sort(function(a,b){return a-b;});';
    s += '    var olXs=[],olYs=[],olColors=[];olConcs.forEach(function(c){var b=bestOverall[c];olXs.push(c);olYs.push(b.tputGpu);olColors.push(getEnvColor(b.label));});';
    s += '    var olTraces=[{x:olXs,y:olYs,text:olConcs.map(function(c){return bestOverall[c].label}),name:"Best Config",mode:"lines+markers",line:{color:"#475569",width:2},marker:{color:olColors,size:14,line:{color:"#fff",width:2}},hovertemplate:"<b>%{text}</b><br>c=%{x}<br>%{y:.0f} tok/s/GPU<extra></extra>"}];';
    s += '    ["agg","disagg"].forEach(function(cat){var eColor=cat==="agg"?"#dc2626":"#2563eb";var eName=cat==="agg"?"Aggregated — Best":"PD/EP — Best";var exs=[],eys=[];';
    s += '      Object.keys(bestByConc).sort(function(a,b){return a-b}).forEach(function(c){if(bestByConc[c][cat]){exs.push(parseInt(c));eys.push(bestByConc[c][cat].tputGpu);}});';
    s += '      if(exs.length>1){olTraces.push({x:exs,y:eys,name:eName,mode:"lines",line:{color:eColor,width:2,dash:"dot"},showlegend:true});}});';
    s += '    var olAnns=[];olConcs.forEach(function(c,i){var b=bestOverall[c];var side=i%2===0?1:-1;olAnns.push({x:c,y:b.tputGpu,text:b.label,showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:getEnvColor(b.label),ax:60*side,ay:-25-(i%3)*15,font:{size:13,color:getEnvColor(b.label)},bgcolor:"rgba(255,255,255,0)",borderpad:2});});';
    s += '    Plotly.newPlot(olEl,olTraces,{...lo,height:850,xaxis:{title:"Concurrent Users",gridcolor:"#d1d5db",tickmode:"array",tickvals:olConcs},yaxis:{title:"Throughput per GPU (tok/s/GPU)",gridcolor:"#d1d5db"},showlegend:true,legend:{x:0.02,y:0.98,bgcolor:"rgba(255,255,255,0.9)",bordercolor:"#e2e8f0",borderwidth:1},plot_bgcolor:"white",paper_bgcolor:"white",annotations:olAnns},co);}';
    s += '})();';

    // Cache Sweep charts — per config
    if (data.cache_sweep && Object.keys(data.cache_sweep).length) {
        s += 'var cacheSweepData=' + JSON.stringify(data.cache_sweep) + ';';
        s += 'Object.keys(cacheSweepData).forEach(function(cfgKey){';
        s += '  var pts=[].concat(cacheSweepData[cfgKey]).sort(function(a,b){return(a.hit_pct||0)-(b.hit_pct||0)});';
        s += '  if(!pts.length)return;';
        s += '  var base="dl-cache-"+cfgKey.replace(/[^a-z0-9]/g,"-");';
        s += '  var cx=pts.map(function(p){return p.hit_pct+"%"});';
        // TTFT P90/P95/P99 combined chart
        s += '  var ttftEl=document.getElementById(base+"-ttft");';
        s += '  if(ttftEl){Plotly.newPlot(ttftEl,[';
        s += '    {x:cx,y:pts.map(function(p){return p.ttft_p90}),name:"P90",type:"scatter",mode:"lines+markers",line:{color:"#3b82f6",width:3},marker:{size:8}},';
        s += '    {x:cx,y:pts.map(function(p){return p.ttft_p95}),name:"P95",type:"scatter",mode:"lines+markers",line:{color:"#f59e0b",width:2},marker:{size:8}},';
        s += '    {x:cx,y:pts.map(function(p){return p.ttft_p99}),name:"P99",type:"scatter",mode:"lines+markers",line:{color:"#ef4444",width:2},marker:{size:8}}';
        s += '  ],{...lo,height:850,xaxis:{title:"Cache Hit %"},yaxis:{title:"TTFT (ms)"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);}';
        // Throughput chart
        s += '  var tputEl=document.getElementById(base+"-tput");';
        s += '  if(tputEl){Plotly.newPlot(tputEl,[';
        s += '    {x:cx,y:pts.map(function(p){return p.throughput_mean}),name:"Throughput",type:"scatter",mode:"lines+markers",line:{color:"#10b981",width:3},marker:{size:10}}';
        s += '  ],{...lo,height:850,xaxis:{title:"Cache Hit %"},yaxis:{title:"Throughput (req/s)"},showlegend:false},co);}';
        // Actual hit rate chart
        s += '  var hitEl=document.getElementById(base+"-hit");';
        s += '  var hitPts=pts.filter(function(p){return p.actual_hit_rate!=null});';
        s += '  if(hitEl&&hitPts.length){Plotly.newPlot(hitEl,[';
        s += '    {x:hitPts.map(function(p){return p.hit_pct+"%"}),y:hitPts.map(function(p){return p.actual_hit_rate}),name:"Actual",type:"scatter",mode:"lines+markers",line:{color:"#8b5cf6",width:3},marker:{size:10}},';
        s += '    {x:["0%","100%"],y:[0,100],name:"Ideal",type:"scatter",mode:"lines",line:{color:"#94a3b8",width:1,dash:"dash"}}';
        s += '  ],{...lo,height:850,xaxis:{title:"Configured Cache Hit %"},yaxis:{title:"Actual Hit Rate %",range:[-5,105]},showlegend:true,legend:{x:0,y:1.15,orientation:"h"}},co);}';
        // Engine metrics chart
        s += '  var engEl=document.getElementById(base+"-engine");';
        s += '  if(engEl){';
        s += '    var arLk={};ar.forEach(function(r){arLk[r.test_id]=r});';
        s += '    var eKvA=[],eKvM=[],eRun=[],eRunMax=[],eRunSpread=[],eWait=[],eDec=[],ePre=[],ePreempt=[];';
        s += '    pts.forEach(function(p){var r=arLk[p.test_id]||{};';
        s += '      eKvA.push((r._kv_cache_avg||0)*100);eKvM.push((r._kv_cache_max||0)*100);';
        s += '      eRun.push(r._avg_running||0);eRunMax.push(r._avg_running_max||0);eRunSpread.push((r._avg_running_max||0)-(r._avg_running||0));eWait.push(r._avg_waiting||0);';
        s += '      eDec.push(r._decode_time_avg||0);ePre.push(r._prefill_time_avg||0);';
        s += '      ePreempt.push(r._preemptions_avg||0);});';
        s += '    var eTraces=[';
        s += '      {x:cx,y:eKvA,name:"KV Cache Avg %",type:"bar",marker:{color:"#3b82f6"}},';
        s += '      {x:cx,y:eKvM,name:"KV Cache Max %",type:"bar",marker:{color:"#93c5fd"}},';
        s += '      {x:cx,y:eRun,name:"Avg Running Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#059669",width:3},marker:{size:8}},';
        s += '      {x:cx,y:eRunMax,name:"Max Running Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#059669",width:2,dash:"dash"},marker:{size:6,symbol:"triangle-up"}},';
        s += '      {x:cx,y:eRunSpread,name:"Load Spread",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#dc2626",width:2,dash:"dashdot"},marker:{size:6,symbol:"diamond"}},';
        s += '      {x:cx,y:eWait,name:"Avg Waiting Reqs",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#ef4444",width:2,dash:"dot"},marker:{size:6}},';
        s += '      {x:cx,y:eDec,name:"Decode req-s/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:2},marker:{size:6}},';
        s += '      {x:cx,y:ePre,name:"Prefill req-s/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#8b5cf6",width:2},marker:{size:6}}];';
        s += '    if(ePreempt.some(function(v){return v>0})){eTraces.push({x:cx,y:ePreempt,name:"Preemptions/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#dc2626",width:3},marker:{size:8,symbol:"x"}});}';
        s += '    Plotly.newPlot(engEl,eTraces,{barmode:"group",xaxis:{title:"Cache Hit %"},yaxis:{title:"KV Cache %",rangemode:"tozero",gridcolor:"#e2e8f0"},yaxis2:{title:"Count / Rate",overlaying:"y",side:"right",rangemode:"tozero"},legend:{x:0,y:1.15,orientation:"h"},margin:{t:40,b:60,l:60,r:60},plot_bgcolor:"#f8fafc",paper_bgcolor:"#fff",hovermode:"x unified"},co);}';
        s += '});';
    }

    // Traffic charts
    {
        const hasTraffic = allRes.some(r => r.request_total != null || r.request_errored != null);
        if (hasTraffic) {
            const trafficGroupMap = {};
            const trafficPrefixes = ['step11-sweep', 'step13', 'step11-epp', 'step9', 'step7', 'step6', 'step3', 'step2'];
            allRes.forEach(r => {
                const rid = r.test_id || r.config_name || '';
                if (!rid) return;
                let gk = 'other';
                for (const p of trafficPrefixes) {
                    if (rid.startsWith(p)) { gk = p; break; }
                }
                if (!trafficGroupMap[gk]) trafficGroupMap[gk] = [];
                trafficGroupMap[gk].push({
                    label: (r.config_name || r.test_id || '?') + (r.concurrency != null && rid.startsWith('step11-sweep') ? ' (c=' + r.concurrency + ')' : ''),
                    cfgKey: rid.replace('step11-sweep-', '').replace(/-c\d+$/, ''),
                    conc: r.concurrency != null ? r.concurrency : null,
                    request_total: r.request_total || 0,
                    request_errored: r.request_errored || 0,
                    nixl_errors: r.nixl_errors || 0,
                    nixl_degraded: r.nixl_degraded || 0
                });
            });
            if (trafficGroupMap['step2'] || trafficGroupMap['step3']) {
                trafficGroupMap['tp-cal'] = (trafficGroupMap['step2'] || []).concat(trafficGroupMap['step3'] || []);
                delete trafficGroupMap['step2'];
                delete trafficGroupMap['step3'];
            }
            if (trafficGroupMap['step11-epp']) {
                trafficGroupMap['step9'] = (trafficGroupMap['step9'] || []).concat(trafficGroupMap['step11-epp']);
                delete trafficGroupMap['step11-epp'];
            }
            const trafficLabelMap = {'tp-cal':'TP Calibration','step6':'Aggregated','step7':'PD/EP','step9':'EPP Tuning','step11-sweep':'Concurrency Sweep','step13':'Cache Sweep','other':'Other'};
            s += 'var trafGrps=' + JSON.stringify(trafficGroupMap) + ';';
            s += 'var trafLabels=' + JSON.stringify(trafficLabelMap) + ';';
            // Group chart renderer (incl. NIXL-degraded markers)
            s += '["tp-cal","step6","step7","step9","step13","other"].forEach(function(gk){';
            s += '  if(!trafGrps[gk])return;';
            s += '  var el=document.getElementById("dl-traffic-"+gk);if(!el)return;';
            s += '  var pts=trafGrps[gk];';
            s += '  var labels=pts.map(function(r){return r.label});';
            s += '  var traces=[{x:labels,y:pts.map(function(r){return r.request_total}),name:"Requests",type:"bar",marker:{color:"#3b82f6"}},';
            s += '    {x:labels,y:pts.map(function(r){return r.request_errored}),name:"Errors",type:"bar",marker:{color:"#ef4444"}}];';
            s += '  var hasNixl=pts.some(function(r){return r.nixl_errors>0});';
            s += '  if(hasNixl){traces.push({x:labels,y:pts.map(function(r){return r.nixl_errors}),name:"NIXL Retries",type:"bar",marker:{color:"#f59e0b"}});}';
            s += '  var nixlDegAnns=[];pts.forEach(function(r,i){if(r.nixl_degraded>0){nixlDegAnns.push({x:labels[i],y:r.request_total,yref:"y",text:"<b>NIXL DEGRADED</b>",showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#b45309",ax:0,ay:-30,font:{size:11,color:"white"},bgcolor:"#b45309",borderpad:3,bordercolor:"#b45309",borderwidth:1});}});';
            s += '  var label=trafLabels[gk]||gk;';
            s += '  Plotly.newPlot(el,traces,{...lo,height:850,barmode:"group",margin:{...lo.margin,b:100},xaxis:{tickangle:-35},yaxis:{title:"Count"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"},title:{text:"Traffic: "+label},annotations:nixlDegAnns},co);';
            s += '});';
            // Per-config sweep charts (key = test_id-derived cfgKey, matching the builder)
            s += '(function(){if(!trafGrps["step11-sweep"])return;';
            s += '  var byCfg={};trafGrps["step11-sweep"].forEach(function(r){if(!byCfg[r.cfgKey])byCfg[r.cfgKey]=[];byCfg[r.cfgKey].push(r);});';
            s += '  Object.keys(byCfg).forEach(function(cfgKey){';
            s += '    var cid="dl-traffic-sweep-"+cfgKey.replace(/[^a-z0-9]/g,"-");var el=document.getElementById(cid);if(!el)return;';
            s += '    var pts=byCfg[cfgKey].sort(function(a,b){return(a.conc||0)-(b.conc||0);});';
            s += '    var labels=pts.map(function(r){return r.label});';
            s += '    var traces=[{x:labels,y:pts.map(function(r){return r.request_total}),name:"Requests",type:"bar",marker:{color:"#3b82f6"}},';
            s += '      {x:labels,y:pts.map(function(r){return r.request_errored}),name:"Errors",type:"bar",marker:{color:"#ef4444"}}];';
            s += '    var hasNixl=pts.some(function(r){return r.nixl_errors>0});';
            s += '    if(hasNixl){traces.push({x:labels,y:pts.map(function(r){return r.nixl_errors}),name:"NIXL Retries",type:"bar",marker:{color:"#f59e0b"}});}';
            s += '    var nixlDegAnns=[];pts.forEach(function(r,i){if(r.nixl_degraded>0){nixlDegAnns.push({x:labels[i],y:r.request_total,yref:"y",text:"<b>NIXL DEGRADED</b>",showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#b45309",ax:0,ay:-30,font:{size:11,color:"white"},bgcolor:"#b45309",borderpad:3,bordercolor:"#b45309",borderwidth:1});}});';
            s += '    Plotly.newPlot(el,traces,{...lo,height:850,barmode:"group",margin:{...lo.margin,b:100},xaxis:{tickangle:-35},yaxis:{title:"Count"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"},annotations:nixlDegAnns},co);';
            s += '  });';
            s += '})();';
        }
    }

    // LoRA Serving charts
    s += 'var lsw=' + safeJson(data.lora_sweep || {}) + ';';
    s += '(function(){';
    // Adapter sweep chart: throughput + TTFT + ITL dual axis, LRU shading
    s += '  Object.keys(lsw).forEach(function(skey){var pts=lsw[skey];if(!pts||!pts.length)return;';
    s += '    var chartId="dl-lora-sweep-chart-"+skey.replace(/[^a-z0-9]/gi,"-");var el=document.getElementById(chartId);if(!el)return;';
    s += '    var x=pts.map(function(p){return p.adapter_count});';
    s += '    var basePt=null;for(var bi=0;bi<pts.length;bi++){if(pts[bi].adapter_count===0&&(pts[bi].output_tps_mean||0)>0){basePt=pts[bi];break;}}';
    s += '    var deltaAnns=[];if(basePt){var baseTps=basePt.output_tps_mean;pts.forEach(function(p){if(p.adapter_count===0||!(p.output_tps_mean>0))return;var delta=(p.output_tps_mean-baseTps)/baseTps*100;var txt=(delta>0?"+":"")+delta.toFixed(0)+"%";var col=delta>0.5?"#059669":(delta<-0.5?"#dc2626":"#64748b");deltaAnns.push({x:p.adapter_count,y:p.output_tps_mean,xref:"x",yref:"y",text:txt,showarrow:true,arrowhead:2,arrowsize:0.9,arrowwidth:1.5,arrowcolor:col,ax:0,ay:-32,font:{color:col,size:11},bgcolor:"rgba(255,255,255,0.88)",bordercolor:col,borderwidth:1,borderpad:2});});}';
    s += '    var traces=[{x:x,y:pts.map(function(p){return p.output_tps_mean||0}),name:"Throughput",type:"scatter",mode:"lines+markers+text",line:{color:"#10b981",width:3,dash:"dot"},marker:{size:8,symbol:"diamond"},text:pts.map(function(p){return p.output_tps_mean?Math.round(p.output_tps_mean).toLocaleString():""}),textposition:"top center",textfont:{color:"#059669",size:10},hovertemplate:"Adapters: %{x}<br>Throughput: %{y:,.0f} tok/s<extra></extra>"},';
    s += '    {x:x,y:pts.map(function(p){return p.ttft_p90||0}),name:"TTFT P90",type:"scatter",mode:"lines+markers+text",yaxis:"y2",line:{color:"#3b82f6",width:3},marker:{size:8},text:pts.map(function(p){return p.ttft_p90?Math.round(p.ttft_p90)+"ms":""}),textposition:"bottom center",textfont:{color:"#3b82f6",size:9},hovertemplate:"Adapters: %{x}<br>TTFT P90: %{y:.0f} ms<extra></extra>"},';
    s += '    {x:x,y:pts.map(function(p){return p.itl_p90||0}),name:"ITL P90",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3},marker:{size:8},hovertemplate:"Adapters: %{x}<br>ITL P90: %{y:.1f} ms<extra></extra>"}];';
    s += '    var shapes=[];var firstLru=-1;for(var li=0;li<pts.length;li++){if(pts[li].lru_swapping){firstLru=li;break;}}';
    s += '    if(firstLru>0){var boundary=(pts[firstLru-1].adapter_count+pts[firstLru].adapter_count)/2;shapes.push({type:"line",x0:boundary,x1:boundary,yref:"paper",y0:0,y1:1,line:{color:"#ef4444",width:2,dash:"dash"}});shapes.push({type:"rect",x0:boundary,x1:x[x.length-1]*1.03,yref:"paper",y0:0,y1:1,fillcolor:"rgba(239,68,68,0.08)",line:{width:0}});}';
    s += '    var resident=pts[0].gpu_resident;var anns=resident?[{x:resident,y:1,yref:"paper",xanchor:"left",yanchor:"top",text:"GPU-resident: "+resident,showarrow:false,font:{color:"#64748b",size:11},bgcolor:"rgba(255,255,255,0.8)"}]:[];';
    s += '    Plotly.newPlot(el,traces,{margin:{l:70,r:70,t:30,b:45},xaxis:{title:"Adapter Count",gridcolor:"#f1f5f9",tickmode:"array",tickvals:x},yaxis:{title:"Tokens per second",gridcolor:"#f1f5f9"},yaxis2:{title:"Latency (ms)",overlaying:"y",side:"right"},shapes:shapes,annotations:anns.concat(deltaAnns),legend:{orientation:"h",y:1.12},hovermode:"x unified"},{displayModeBar:false,responsive:true});';
    // Sweep affinity chart
    s += '    var affChartId="dl-lora-sweep-aff-chart-"+skey.replace(/[^a-z0-9]/gi,"-");var affEl=document.getElementById(affChartId);';
    s += '    if(affEl){var affByTest={};((cd.vllm&&cd.vllm.lora_pod_details)||[]).forEach(function(pd){if(pd&&pd.test_id)affByTest[pd.test_id]=pd;});';
    s += '      var affPts=[];pts.forEach(function(p){var pd=affByTest[p.test_id];if(pd&&(pd.total_adapters||0)>0){affPts.push({adapter_count:p.adapter_count,pct:Math.round(100*(pd.concentrated||0)/pd.total_adapters)});}});';
    s += '      if(affPts.length>=2){var aAnns=affPts.map(function(p){var col=p.pct>=80?"#059669":(p.pct>=50?"#d97706":"#dc2626");return{x:p.adapter_count,y:p.pct,xref:"x",yref:"y",text:p.pct+"%",showarrow:true,arrowhead:2,arrowsize:0.8,arrowwidth:1.2,arrowcolor:col,ax:0,ay:-22,font:{color:col,size:10},bgcolor:"rgba(255,255,255,0.85)",bordercolor:col,borderwidth:1,borderpad:1};});';
    s += '        Plotly.newPlot(affEl,[{x:affPts.map(function(p){return p.adapter_count}),y:affPts.map(function(p){return p.pct}),name:"Single-pod",type:"scatter",mode:"lines+markers",line:{color:"#059669",width:3},marker:{color:"#059669",size:9},hovertemplate:"Adapters: %{x}<br>Single-pod: %{y}%<extra></extra>"}],{margin:{l:60,r:20,t:20,b:55},xaxis:{title:"Adapters",gridcolor:"#f1f5f9"},yaxis:{title:"% adapters served by a single pod",range:[0,108],gridcolor:"#f1f5f9"},showlegend:false,hovermode:"closest",annotations:aAnns},{displayModeBar:false,responsive:true});}}';
    // Evictions chart
    s += '    var evictChartId="dl-lora-sweep-evict-chart-"+skey.replace(/[^a-z0-9]/gi,"-");var evEl=document.getElementById(evictChartId);';
    s += '    if(evEl){var evictPts=pts.filter(function(p){return p.adapter_evictions!==undefined&&p.adapter_evictions!==null;});if(evictPts.length>=2){';
    s += '      var ex=evictPts.map(function(p){return p.adapter_count});var loads=evictPts.map(function(p){return p.adapter_loads!==undefined&&p.adapter_loads!==null?p.adapter_loads:null});var evictions=evictPts.map(function(p){return p.adapter_evictions});var capacity=null;evictPts.forEach(function(p){if(p.total_slots)capacity=p.total_slots;});';
    s += '      var eShapes=[];var eAnns=[];if(capacity){eShapes.push({type:"line",x0:capacity,x1:capacity,y0:0,y1:1,yref:"paper",line:{color:"#b91c1c",width:2,dash:"dash"}});eAnns.push({x:capacity,y:1,yref:"paper",xanchor:"left",yanchor:"top",text:"pool capacity: "+capacity+" slots",showarrow:false,font:{color:"#b91c1c",size:11},bgcolor:"rgba(255,255,255,0.85)"});}';
    s += '      Plotly.newPlot(evEl,[{x:ex,y:loads,name:"Adapter loads",type:"bar",marker:{color:"rgba(100,116,139,0.55)"},hovertemplate:"%{x} adapters<br>Loads: %{y}<extra></extra>"},{x:ex,y:evictions,name:"Evictions",type:"bar",marker:{color:"rgba(220,38,38,0.85)"},hovertemplate:"%{x} adapters<br>Evictions: %{y}<extra></extra>"}],{barmode:"group",margin:{l:60,r:20,t:30,b:50},xaxis:{title:"Adapter count",gridcolor:"#f1f5f9"},yaxis:{title:"Adapter loads / evictions",gridcolor:"#f1f5f9"},shapes:eShapes,annotations:eAnns,legend:{orientation:"h",y:1.08},hovermode:"x unified"},{displayModeBar:false,responsive:true});}}';
    s += '  });';
    // LoRA routing & residency 4-subplot chart
    s += '  var loraSig=cd.vllm&&cd.vllm.lora;if(loraSig&&cd.vllm.configs&&cd.vllm.configs.length){var rEl=document.getElementById("dl-lora-routing-chart");';
    s += '    var hasData=(loraSig.ttft_p99_pod_spread_ms||[]).some(function(v){return v>0;})||(loraSig.adapter_churn||[]).some(function(v){return v>0;})||(loraSig.adapters_per_pod||[]).some(function(v){return v>0;});';
    s += '    if(rEl&&hasData){var cfgLabels=cd.vllm.configs;var dom=[{x:[0,0.47],y:[0.58,1]},{x:[0.53,1],y:[0.58,1]},{x:[0,0.47],y:[0.06,0.48]},{x:[0.53,1],y:[0.06,0.48]}];';
    s += '      function sub(series,color,name,idx,yaxisNum){return{x:cfgLabels,y:series,name:name,type:"bar",marker:{color:color},xaxis:"x"+(idx>1?idx:""),yaxis:"y"+yaxisNum,hovertemplate:"%{x}<br>"+name+": %{y}<extra></extra>"};}';
    s += '      var layout={margin:{l:55,r:20,t:30,b:90},showlegend:false,hovermode:"closest",xaxis:{domain:dom[0].x,anchor:"y",tickangle:-35,automargin:true},yaxis:{domain:dom[0].y,anchor:"x",title:{text:"TTFT P99 spread (ms)"}},xaxis2:{domain:dom[1].x,anchor:"y2",tickangle:-35,automargin:true},yaxis2:{domain:dom[1].y,anchor:"x2",title:{text:"Residency changes"}},xaxis3:{domain:dom[2].x,anchor:"y3",tickangle:-35,automargin:true},yaxis3:{domain:dom[2].y,anchor:"x3",title:{text:"Adapters per pod"}},xaxis4:{domain:dom[3].x,anchor:"y4",tickangle:-35,automargin:true},yaxis4:{domain:dom[3].y,anchor:"x4",title:{text:"Waiting CV"}},annotations:[{xref:"paper",yref:"paper",x:0.23,y:1.03,showarrow:false,text:"Routing cost — hot vs cold pod TTFT delta",font:{size:12,color:"#3b82f6"}},{xref:"paper",yref:"paper",x:0.77,y:1.03,showarrow:false,text:"LRU churn — residency set changes",font:{size:12,color:"#ef4444"}},{xref:"paper",yref:"paper",x:0.23,y:0.51,showarrow:false,text:"Adapter concentration per pod",font:{size:12,color:"#7c3aed"}},{xref:"paper",yref:"paper",x:0.77,y:0.51,showarrow:false,text:"Queue imbalance (0 = perfect)",font:{size:12,color:"#f59e0b"}}]};';
    s += '      var cfgCount=cfgLabels.length;if(cfgCount>8){layout.xaxis.tickmode="array";layout.xaxis.tickvals=cfgLabels.filter(function(_,i){return i%Math.ceil(cfgCount/8)===0;});}';
    s += '      Plotly.newPlot(rEl,[sub(loraSig.ttft_p99_pod_spread_ms||[],"#3b82f6","TTFT spread",1,1),sub(loraSig.adapter_churn||[],"#ef4444","Churn",2,2),sub(loraSig.adapters_per_pod||[],"#7c3aed","Adapters/pod",3,3),sub(loraSig.waiting_cv||[],"#f59e0b","Queue CV",4,4)],layout,{displayModeBar:false,responsive:true});}}';
    // Affinity summary chart
    s += '  var affSumEl=document.getElementById("dl-lora-affinity-summary-chart");';
    s += '  if(affSumEl){var affDetails=((cd.vllm&&cd.vllm.lora_pod_details)||[]).filter(function(pd){return pd&&pd.pods&&Object.keys(pd.pods).length&&(pd.total_adapters||0)>0&&!(pd.test_id||"").startsWith("step11-epp-")&&!(pd.test_id||"").startsWith("step14-lora-");});';
    s += '    if(affDetails.length){var labelCount={};affDetails.forEach(function(pd){labelCount[pd.config]=(labelCount[pd.config]||0)+1;});var used={};var cfgs=affDetails.map(function(pd){var lbl=pd.config;if(labelCount[pd.config]>1){lbl=pd.config+" ·n"+(pd.total_adapters||0);}used[lbl]=(used[lbl]||0)+1;if(used[lbl]>1){lbl=lbl+" #"+used[lbl];}return lbl;});';
    s += '      var y=affDetails.map(function(pd){return pd.total_adapters?Math.round(100*(pd.concentrated||0)/pd.total_adapters):0;});';
    s += '      var annotations=affDetails.map(function(pd,i){var col=y[i]>=80?"#059669":(y[i]>=50?"#d97706":"#dc2626");return{x:cfgs[i],y:y[i],xref:"x",yref:"y",text:y[i]+"%",showarrow:true,arrowhead:2,arrowsize:0.8,arrowwidth:1.2,arrowcolor:col,ax:0,ay:-22,font:{color:col,size:10},bgcolor:"rgba(255,255,255,0.85)",bordercolor:col,borderwidth:1,borderpad:1};});';
    s += '      Plotly.newPlot(affSumEl,[{x:cfgs,y:y,name:"Single-pod",type:"scatter",mode:"lines+markers",line:{color:"#059669",width:3},marker:{color:"#059669",size:9},hovertemplate:"%{x}<br>Single-pod: %{y}%<extra></extra>"}],{margin:{l:60,r:20,t:20,b:100},xaxis:{tickangle:-35,automargin:true},yaxis:{title:"% adapters served by a single pod",range:[0,108],gridcolor:"#f1f5f9"},showlegend:false,hovermode:"closest",annotations:annotations},{displayModeBar:false,responsive:true});}}';
    s += '})();';

    // Estimator — runs client-side against embedded results
    s += 'function dlRunEstimator(){';
    s += '  var targetTput=parseFloat(document.getElementById("dl-est-tput-target").value)||100;';
    s += '  var ttftTarget=parseFloat(document.getElementById("dl-est-ttft-target").value)||500;';
    s += '  var ttftPctl=document.getElementById("dl-est-ttft-pctl").value||"p99";';
    s += '  var itlTarget=parseFloat(document.getElementById("dl-est-itl-target").value)||20;';
    s += '  var itlPctl=document.getElementById("dl-est-itl-pctl").value||"p90";';
    s += '  var allResults=ar.filter(function(r){return r.quality!=="discard"&&r.gpus>0&&(r.throughput_mean>0||r.throughput_p90>0);});';
    s += '  var bestByConfig={};allResults.forEach(function(r){var tid=r.test_id||"";if(tid.indexOf("step2-")===0||tid.indexOf("step3-")===0)return;var key=r.config_name||tid;var tput=r.throughput_mean||r.throughput_p90||0;if(!bestByConfig[key]||tput>(bestByConfig[key].throughput_mean||bestByConfig[key].throughput_p90||0)){bestByConfig[key]=r;}});';
    s += '  var results=[];Object.keys(bestByConfig).forEach(function(key){var r=bestByConfig[key];var tput=r.throughput_mean||r.throughput_p90||0;if(tput<=0)return;var replicas=Math.ceil(targetTput/tput);var totalGpus=replicas*r.gpus;';
    s += '    var ttftVal=ttftPctl==="p90"?r.ttft_p90:(ttftPctl==="p95"?r.ttft_p95:r.ttft_p99);var ttftMeets=ttftVal!=null?ttftVal<=ttftTarget:null;';
    s += '    var itlVal=itlPctl==="p90"?r.itl_p90:(itlPctl==="p95"?r.itl_p95:r.itl_p99);var itlMeets=itlVal!=null&&itlVal>0?itlVal<=itlTarget:null;';
    s += '    results.push({config_name:key,architecture:(r.architecture||"UNKNOWN").toUpperCase(),gpus_per_replica:r.gpus,replicas:replicas,total_gpus:totalGpus,measured_tput:tput,target_tput:targetTput,ttft_val:ttftVal,ttft_meets:ttftMeets,ttft_target:ttftTarget,ttft_pctl:ttftPctl,itl_val:itlVal,itl_meets:itlMeets,itl_target:itlTarget,itl_pctl:itlPctl});});';
    s += '  results.sort(function(a,b){return a.total_gpus-b.total_gpus;});';
    s += '  var box=document.getElementById("dl-est-results");var chartEl=document.getElementById("dl-est-chart");';
    s += '  if(!results.length){box.innerHTML="<p style=\\"color:#6b7280;padding:16px;\\">No valid configurations to estimate.</p>";return;}';
    s += '  var bestGpus=results[0].total_gpus;var sep="border-left:3px solid #e2e8f0;";var fmtMs=function(v){return v!=null?Math.round(v).toLocaleString():"-";};';
    s += '  var t="<table><thead><tr><th>Configuration</th><th>Arch</th><th>GPUs/replica</th><th>Measured req/s</th><th style=\\""+sep+"\\">Replicas</th><th><strong>Total GPUs</strong></th><th style=\\""+sep+"\\">TTFT "+results[0].ttft_pctl.toUpperCase()+"</th><th>ITL "+results[0].itl_pctl.toUpperCase()+"</th><th style=\\""+sep+"\\">SLA Status</th></tr></thead><tbody>";';
    s += '  results.forEach(function(r){var isBest=r.total_gpus===bestGpus&&r.ttft_meets!==false&&r.itl_meets!==false;';
    s += '    var ttftColor=r.ttft_meets===true?"#059669":(r.ttft_meets===false?"#ef4444":"#64748b");var itlColor=r.itl_meets===true?"#059669":(r.itl_meets===false?"#ef4444":"#64748b");';
    s += '    var slaStatus="";if(r.ttft_meets===false&&r.itl_meets===false){slaStatus="<span style=\\"color:#ef4444;font-weight:700;\\">TTFT + ITL FAIL</span>";}else if(r.ttft_meets===false){slaStatus="<span style=\\"color:#ef4444;font-weight:700;\\">TTFT FAIL</span>";}else if(r.itl_meets===false){slaStatus="<span style=\\"color:#ef4444;font-weight:700;\\">ITL FAIL</span>";}else{slaStatus="<span style=\\"color:#059669;font-weight:700;\\">PASS</span>";}';
    s += '    t+="<tr style=\\""+(isBest?"background:#f0fdf4;font-weight:600;":"")+(r.ttft_meets===false||r.itl_meets===false?"opacity:0.6;":"")+"\\"><td>"+r.config_name+"</td><td>"+r.architecture+"</td><td>"+r.gpus_per_replica+"</td><td>"+r.measured_tput.toFixed(2)+"</td><td>"+r.replicas+"</td><td><strong>"+r.total_gpus.toLocaleString()+"</strong></td><td style=\\"color:"+ttftColor+";font-weight:600;\\">"+fmtMs(r.ttft_val)+" ms</td><td style=\\"color:"+itlColor+";font-weight:600;\\">"+(r.itl_val!=null?r.itl_val.toFixed(2):"-")+" ms</td><td>"+slaStatus+"</td></tr>";});';
    s += '  t+="</tbody></table>";box.innerHTML=t;';
    s += '  var labels=results.map(function(r){return r.config_name});var barColors=results.map(function(r){var passes=r.ttft_meets!==false&&r.itl_meets!==false;if(r.total_gpus===bestGpus&&passes)return "#059669";if(!passes)return "#ef4444";return "#d97706";});';
    s += '  Plotly.newPlot(chartEl,[{type:"bar",orientation:"h",y:labels,x:results.map(function(r){return r.total_gpus}),text:results.map(function(r){var t2=r.total_gpus.toLocaleString()+" GPUs ("+r.replicas+" replicas)";if(r.ttft_meets===false)t2+=" [TTFT FAIL]";if(r.itl_meets===false)t2+=" [ITL FAIL]";return t2;}),textposition:"outside",marker:{color:barColors},hovertemplate:"<b>%{y}</b><br>%{x} GPUs<br>%{text}<extra></extra>"}],{title:{text:"GPUs needed for "+results[0].target_tput+" req/s",font:{size:15}},xaxis:{title:"Total GPUs"},yaxis:{automargin:true},margin:{l:200,r:180,t:50,b:40},height:Math.max(600,results.length*60+80)},{responsive:true,displayModeBar:false});';
    s += '}';



    s += '<\/script>';
    // Separate script so it runs even if chart rendering throws an error
    s += '<script>setTimeout(function(){document.body.classList.add("dl-pane-ready");},500);<\/script>';
    return s;
}
