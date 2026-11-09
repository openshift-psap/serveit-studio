// report-download.js — Thin offline report generator
//
// The offline report uses the EXACT same tab modules as the live report:
//   1. Embed core modules (registry, helpers, context) — tabs self-register
//   2. Embed tab module sources
//   3. Embed the data (stripped of large non-renderable fields)
//   4. Bootstrap: iterate ReportTabs.all(), build content, create the
//      subtab navigation, render Plotly charts — same as charts.js
//
// No duplicated chart-building logic. One source of truth.

async function downloadHTMLReport(runId, data) {
    // ── Fetch module sources ─────────────────────────────────────────
    const MODULES = [
        '/static/report/core/registry.js',
        '/static/report/core/helpers.js',
        '/static/report/core/context.js',
        '/static/report/tabs/recommendation.js',
        '/static/report/tabs/configurations.js',
        '/static/report/tabs/tp-calibration.js',
        '/static/report/tabs/comparison.js',
        '/static/report/tabs/latency-search.js',
        '/static/report/tabs/calibrated-load.js',
        '/static/report/tabs/cache-sweep.js',
        '/static/report/tabs/concurrency-sweep.js',
        '/static/report/tabs/lora.js',
        '/static/report/tabs/epp-tuning.js',
        '/static/report/tabs/traffic.js',
        '/static/report/tabs/pareto.js',
        '/static/report/tabs/deploy-timing.js',
        '/static/report/tabs/test-settings.js',
        '/static/report/tabs/estimator.js',
        '/static/report/tabs/vllm-metrics.js',
        '/static/report/tabs/workloads.js',
        '/static/report/tabs/pod-profiles.js',
        '/static/report/tabs/inference-profiles.js',
        '/static/report/tabs/long-run.js',
        '/static/report/tabs/resources.js',
    ];
    const moduleSources = {};
    await Promise.all(MODULES.map(async (f) => {
        try {
            const r = await fetch(f, { cache: 'reload' });
            if (r.ok) moduleSources[f] = await r.text();
        } catch (e) { /* tab missing — renders empty offline */ }
    }));

    // ── Strip large non-renderable data ──────────────────────────────
    // profiles_blob: raw base64 pprof (~1.5MB for 50 cycles) — only
    //   usable via live API download routes, not from a static file
    // prometheus_metrics / workloads: large sub-objects the tabs don't
    //   render (the old charts used them; the modular tabs read
    //   pprof, resource_series, resource_snapshot, and epp_heap instead)
    const slimData = JSON.parse(JSON.stringify(data));
    (slimData.all_results || []).forEach(r => {
        delete r.profiles_blob;
        delete r.manifests;
        if (r.metrics_json) {
            try {
                const mj = typeof r.metrics_json === 'string'
                    ? JSON.parse(r.metrics_json) : r.metrics_json;
                const pm = mj.prometheus_metrics || {};
                // Null-safe extraction — 0 is a valid value for these metrics,
                // so `|| null` would wrongly discard it (e.g. avg waiting = 0)
                const num = (v) => (v === undefined ? null : v);
                const pmAvg = (k) => num((pm[k] || {}).avg);
                const pmMax = (k) => num((pm[k] || {}).max);
                // Pre-extract the flat fields the configurations tab charts
                // need (they read _prompt_tps_avg etc. — small, ~10 numbers)
                r._prompt_tps_avg = pmAvg('vllm_prompt_tokens_rate');
                r._prompt_tps_max = pmMax('vllm_prompt_tokens_rate');
                r._gen_tps_avg = pmAvg('vllm_generation_tokens_rate');
                r._gen_tps_max = pmMax('vllm_generation_tokens_rate');
                r._per_gpu_tps = mj.per_gpu_throughput_tokens_sec != null ? mj.per_gpu_throughput_tokens_sec : null;
                r._vllm_req_rate = pmAvg('vllm_e2e_count_rate');
                r._avg_running = pmAvg('vllm_requests_running');
                r._avg_waiting = pmAvg('vllm_requests_waiting');
                r._kv_cache_usage = mj.kv_cache_usage != null ? mj.kv_cache_usage : null;
                r._prompt_tokens_mean = mj.prompt_tokens_mean != null ? mj.prompt_tokens_mean : null;
                r._output_tokens_mean = mj.output_tokens_mean != null ? mj.output_tokens_mean : null;
                r._throughput_mean_mj = mj.throughput_mean != null ? mj.throughput_mean : null;
                r._cache_hits_avg = pmAvg('vllm_prefix_cache_hits_rate');
                r._cache_queries_avg = pmAvg('vllm_prefix_cache_queries_rate');
                r._kv_cache_avg = pmAvg('vllm_kv_cache_pct');
                r._kv_cache_max = pmMax('vllm_kv_cache_pct');
                r._decode_time_rate = pmAvg('vllm_decode_time_rate');
                r._prefill_time_rate = pmAvg('vllm_prefill_time_rate');
                r._preemptions_rate = pmAvg('vllm_preemptions_rate');
                r._model_load_time_s = mj.model_load_time_s != null ? mj.model_load_time_s : null;
                // Strip the large sub-objects the tabs don't render
                const slim = Object.assign({}, mj);
                delete slim.prometheus_metrics;
                delete slim.workloads;
                delete slim.guidellm_command;
                r.metrics_json = JSON.stringify(slim);
            } catch (e) { delete r.metrics_json; }
        }
    });

    // ── Build the HTML ────────────────────────────────────────────────
    const safeJson = (obj) => JSON.stringify(obj)
        .replace(/<\//g, '<\\/')
        .replace(/<!--/g, '<\\!--');
    const safeSrc = (src) => src.replace(/<\/script>/g, '<\\/script>');

    const rc = data.run_config || {};
    const model = (rc.model_name || '').split('/').pop() || 'model';
    const goal = (rc.objective || 'report').toLowerCase();
    const date = new Date().toISOString().slice(0, 10);

    let scripts = '';
    // Core modules first (tabs depend on ReportHelpers/ReportCtx at load)
    ['core/registry.js', 'core/helpers.js', 'core/context.js'].forEach(f => {
        if (moduleSources['/static/report/' + f])
            scripts += '\n/* ==== ' + f + ' ==== */\n' + safeSrc(moduleSources['/static/report/' + f]) + '\n';
    });
    // Tab modules (self-register via ReportTabs.register)
    MODULES.filter(f => f.startsWith('/static/report/tabs/')).forEach(f => {
        if (moduleSources[f])
            scripts += '\n/* ==== ' + f.split('/').pop() + ' ==== */\n' + safeSrc(moduleSources[f]) + '\n';
    });

    const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>ServeIt Studio Report — Run #${runId}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
${REPORT_CSS}
</style>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8fafc;margin:0;padding:20px;">
<div style="width:100%;">
<div style="padding:12px 20px;background:linear-gradient(135deg,#b45309,#f59e0b);color:#fff;border-radius:12px 12px 0 0;display:flex;justify-content:space-between;align-items:center;">
<div><div style="font-size:1.3em;font-weight:700;">ServeIt Studio Report — Run #${runId}</div>
<div style="font-size:0.9em;opacity:0.9;margin-top:4px;">${model} · ${goal} · ${date}</div></div>
<div style="font-size:0.8em;opacity:0.7;">Offline report — generated from the same tab modules as the live view</div>
</div>
<div id="charts-content" style="background:#fff;border-radius:0 0 12px 12px;padding:20px;border:1px solid #e2e8f0;border-top:none;">
<div class="charts-loading">Building report…</div>
</div>
</div>

<script>
var _REPORT_DATA = ${safeJson(slimData)};
var _RUN_ID = ${runId};
${scripts}
</script>

<script>
// ── Bootstrap: same rendering approach as the live report's charts.js ──
(function() {
    var data = _REPORT_DATA;
    var runId = _RUN_ID;
    var content = document.getElementById('charts-content');

    if (typeof ReportTabs === 'undefined') {
        content.innerHTML = '<div class="charts-loading" style="color:#ef4444;">Error: ReportTabs not loaded — tab modules failed to embed.</div>';
        return;
    }

    // Use the download context (dlProfile/dlManifest work from embedded data)
    var ctx = (typeof ReportCtx !== 'undefined') ? ReportCtx.download(runId) : null;
    if (!ctx) {
        content.innerHTML = '<div class="charts-loading" style="color:#ef4444;">Error: ReportCtx not loaded.</div>';
        return;
    }

    // Build all tabs (same iteration as _renderChartsImpl in charts.js)
    var subtabDefs = [];
    var sectionMap = {};
    ReportTabs.all().forEach(function(tab) {
        try {
            var tabHtml = ReportTabs.buildTab(tab, data, ctx, []);
            if (!tabHtml) return;
            var def = { id: tab.id, label: tab.label, icon: tab.icon };
            var pos = tab.position || 'end';
            var at = -1;
            if (pos.indexOf('before:') === 0) at = subtabDefs.findIndex(function(d) { return d.id === pos.slice(7); });
            else if (pos.indexOf('after:') === 0) { at = subtabDefs.findIndex(function(d) { return d.id === pos.slice(6); }); if (at >= 0) at++; }
            if (at >= 0) subtabDefs.splice(at, 0, def); else subtabDefs.push(def);
            sectionMap[tab.id] = tabHtml;
        } catch (e) {
            console.warn('offline tab build failed: ' + tab.id, e);
        }
    });

    if (!subtabDefs.length) {
        content.innerHTML = '<div class="charts-loading">No report tabs registered.</div>';
        return;
    }

    // Create the subtab navigation + panes (same HTML as the live report)
    var html = '<style>' +
        '#charts-content table{width:100%!important;border-collapse:collapse!important;margin:10px 0 0 0!important;font-size:0.9em!important;background:#fff!important}' +
        '#charts-content th{background:#eef2ff!important;color:#334155!important;padding:7px 10px!important;text-align:left!important;border-top:1px solid #c7d2fe!important;border-bottom:2px solid #c7d2fe!important;white-space:nowrap!important;font-weight:600!important}' +
        '#charts-content td{padding:6px 10px!important;text-align:left!important;border-bottom:1px solid #e2e8f0!important;vertical-align:top!important}' +
        '#charts-content tr:nth-child(even) td{background:#f8fafc!important}' +
        '#charts-content tr:hover td{background:#eef2ff!important}' +
        '</style>';

    html += '<div class="report-subtabs-container subtabs-ready">';
    html += '<div class="report-subtab-bar">';
    subtabDefs.forEach(function(st, i) {
        html += '<div class="report-subtab' + (i === 0 ? ' active' : '') + '" data-subtab="' + st.id + '">' + st.icon + ' ' + st.label + '</div>';
    });
    html += '</div>';
    subtabDefs.forEach(function(st) {
        html += '<div class="report-subtab-pane' + (st.id === subtabDefs[0].id ? ' active' : '') + '" data-subtab-pane="' + st.id + '">' + sectionMap[st.id] + '</div>';
    });
    html += '</div>';

    content.innerHTML = html;

    // Tab switching (same behavior as the live report)
    document.querySelectorAll('.report-subtab').forEach(function(el) {
        el.addEventListener('click', function() {
            document.querySelectorAll('.report-subtab').forEach(function(s) { s.classList.remove('active'); });
            document.querySelectorAll('.report-subtab-pane').forEach(function(p) { p.classList.remove('active'); });
            el.classList.add('active');
            var pane = document.querySelector('[data-subtab-pane="' + el.getAttribute('data-subtab') + '"]');
            if (pane) {
                pane.classList.add('active');
                pane.querySelectorAll('[class*="js-plotly"]').forEach(function(p) { if (window.Plotly) Plotly.Plots.resize(p); });
            }
        });
    });

    // Render Plotly charts — call each tab's render()
    ReportTabs.all().forEach(function(tab) {
        if (tab.render) {
            try { tab.render(data, ctx); } catch (e) {
                console.warn('offline render failed: ' + tab.id, e);
            }
        }
    });

    // Resize all Plotly charts to fill their containers — charts in
    // hidden panes render at 0 width, so resize when tabs are shown
    // and when the window resizes
    function resizeAllCharts() {
        document.querySelectorAll('[class*="js-plotly"]').forEach(function(p) {
            if (window.Plotly) Plotly.Plots.resize(p);
        });
    }
    window.addEventListener('resize', resizeAllCharts);
    // 'load' fires after ALL resources (incl. Plotly CDN) are loaded —
    // setTimeout can race the CDN script and resize before Plotly exists
    window.addEventListener('load', resizeAllCharts);
    // Backup resizes in case 'load' already fired (cached CDN)
    setTimeout(resizeAllCharts, 200);
    setTimeout(resizeAllCharts, 1000);
    // Also resize when any subtab becomes active
    document.querySelectorAll('.report-subtab').forEach(function(el) {
        el.addEventListener('click', function() { setTimeout(resizeAllCharts, 50); });
    });

    // Also handle DOMContentLoaded for fast browsers where 'load' fires
    // before Plotly finishes initializing
    if (document.readyState === 'complete') {
        resizeAllCharts();
    } else {
        document.addEventListener('DOMContentLoaded', function() { setTimeout(resizeAllCharts, 50); });
    }

    // Sortable tables
    window.sortReportTable = function(tableId, colIdx, type) {
        var table = document.getElementById(tableId);
        if (!table) return;
        var rows = Array.from(table.querySelectorAll('tr')).slice(1);
        var dir = table.getAttribute('data-sort-col') === String(colIdx) && table.getAttribute('data-sort-dir') === 'asc' ? 'desc' : 'asc';
        table.setAttribute('data-sort-col', colIdx);
        table.setAttribute('data-sort-dir', dir);
        rows.sort(function(a, b) {
            var aCell = a.cells[colIdx], bCell = b.cells[colIdx];
            var aVal, bVal;
            if (type === 'num') {
                aVal = parseFloat(aCell.getAttribute('data-val') || aCell.textContent.replace(/[^0-9.\\-]/g, '')) || 0;
                bVal = parseFloat(bCell.getAttribute('data-val') || bCell.textContent.replace(/[^0-9.\\-]/g, '')) || 0;
            } else {
                aVal = aCell.textContent.trim().toLowerCase();
                bVal = bCell.textContent.trim().toLowerCase();
            }
            if (aVal < bVal) return dir === 'asc' ? -1 : 1;
            if (aVal > bVal) return dir === 'asc' ? 1 : -1;
            return 0;
        });
        var tbody = table.querySelector('tbody') || table;
        rows.forEach(function(r) { tbody.appendChild(r); });
    };
})();
</script>
</body>
</html>`;

    // ── Download ──────────────────────────────────────────────────────
    const blob = new Blob([html], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `serveit-${model}-${goal}-run${runId}-${date}.html`;
    a.click();
    URL.revokeObjectURL(url);
}

// Report CSS — extracted from style.css (the classes the tab modules use)
const REPORT_CSS = `
.report-subtabs-container { margin-top: 0; }
.report-subtabs-container .report-subtab-pane { display: none; }
.report-subtabs-container.subtabs-ready .report-subtab-pane.active { display: block; }
.report-subtab-bar { display: flex; flex-wrap: wrap; gap: 0; border-bottom: 2px solid #e2e8f0; margin-bottom: 16px; padding: 0 4px; background: #fff; }
.report-subtab { padding: 10px 16px; font-size: 0.88em; font-weight: 600; color: #64748b; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all .15s; white-space: nowrap; }
.report-subtab:hover { color: #b45309; background: #fffbeb; }
.report-subtab.active { color: #b45309; border-bottom-color: #b45309; background: #fffbeb; }
.chart-card { background: #fff; border-radius: 10px; margin: 14px 0; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); border: 1px solid #e2e8f0; }
.chart-card-header { padding: 10px 16px; font-weight: 700; font-size: 0.95em; color: #fff; background: linear-gradient(135deg,#334155,#475569); }
.chart-card-body { padding: 12px; }
.results-table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
.results-table th { background: #eef2ff; color: #334155; padding: 7px 10px; text-align: left; border-top: 1px solid #c7d2fe; border-bottom: 2px solid #c7d2fe; white-space: nowrap; font-weight: 600; }
.results-table td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
.results-table tr:nth-child(even) td { background: #f8fafc; }
.results-table tr:hover td { background: #eef2ff; }
.charts-loading { text-align: center; padding: 60px 20px; color: #94a3b8; font-size: 1.1em; }
.js-plotly-plot { width: 100% !important; }
.report-subtab-pane.active .js-plotly-plot { width: 100% !important; }
.stat-card { display: inline-flex; flex-direction: column; align-items: center; justify-content: center; min-width: 130px; padding: 14px 18px; background: white; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08); border: 1px solid #e2e8f0; text-align: center; margin: 4px; }
.stat-card .val { font-size: 1.35em; font-weight: 800; color: #1e293b; }
.stat-card .lbl { font-size: 0.72em; font-weight: 600; color: #64748b; margin-top: 4px; text-transform: uppercase; letter-spacing: .03em; }
.stat-row { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; padding: 12px; }
.charts-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
.charts-grid-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 1000px) { .charts-grid-2col { grid-template-columns: 1fr; } }
.results-table .pareto-row { background: #f0fdf4; font-weight: 600; }
.results-table .pareto-row:hover { background: #dcfce7; }
#charts-content .pod-bd-table { table-layout: fixed !important; }
#charts-content .pod-bd-table th { white-space: normal !important; }
#charts-content .pod-bd-table th:nth-child(1), #charts-content .pod-bd-table td:nth-child(1) { width: 13%; }
#charts-content .pod-bd-table th:nth-child(2), #charts-content .pod-bd-table td:nth-child(2) { width: 7%; }
#charts-content .pod-bd-table th:nth-child(3), #charts-content .pod-bd-table td:nth-child(3) { width: 9%; }
#charts-content .pod-bd-table th:nth-child(4), #charts-content .pod-bd-table td:nth-child(4) { width: 9%; }
#charts-content .pod-bd-table th:nth-child(5), #charts-content .pod-bd-table td:nth-child(5) { width: 6%; }
#charts-content .pod-bd-table th:nth-child(6), #charts-content .pod-bd-table td:nth-child(6) { width: 6%; }
#charts-content .pod-bd-table th:nth-child(7), #charts-content .pod-bd-table td:nth-child(7) { width: 41%; }
#charts-content .pod-bd-table th:nth-child(8), #charts-content .pod-bd-table td:nth-child(8) { width: 9%; }
/* Estimator (GPU Estimator tab) */
.estimator-form { display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-end; padding: 16px 0; justify-content: center; flex-direction: column; align-items: center; }
.estimator-row { display: flex; flex-wrap: nowrap; gap: 16px; justify-content: center; align-items: flex-start; width: 100%; }
.estimator-group { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; padding: 14px 18px; background: #f8fafc; border-radius: 12px; border: 1px solid #e2e8f0; }
.estimator-group-label { width: 100%; font-size: 0.75em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; color: #64748b; margin-bottom: 2px; }
.estimator-field { display: flex; flex-direction: column; gap: 6px; }
.estimator-field > span { font-size: 0.8em; font-weight: 700; color: #475569; text-transform: uppercase; letter-spacing: 0.5px; }
.estimator-input-wrap { display: flex; align-items: center; background: #f8fafc; border: 2px solid #e2e8f0; border-radius: 10px; padding: 0 4px; transition: border-color 0.2s, box-shadow 0.2s; }
.estimator-input-wrap:focus-within { border-color: #d97706; box-shadow: 0 0 0 3px rgba(217,119,6,0.15); }
.estimator-icon { font-size: 1.1em; padding: 0 8px; opacity: 0.6; }
.estimator-input-wrap input { border: none; background: transparent; padding: 10px 4px; font-size: 1em; width: 90px; outline: none; font-weight: 600; color: #1e293b; }
.estimator-unit { font-size: 0.78em; color: #94a3b8; padding: 0 8px; font-weight: 500; }
.estimator-field select { padding: 10px 12px; border: 2px solid #e2e8f0; border-radius: 10px; font-size: 0.95em; width: auto; min-width: 160px; background: #f8fafc; }
.estimator-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 0.92em; }
.estimator-table th { background: #f1f5f9; padding: 10px 14px; text-align: left; font-weight: 600; color: #334155; border-bottom: 2px solid #e2e8f0; }
.estimator-table td { padding: 9px 14px; border-bottom: 1px solid #f1f5f9; }
.estimator-best td:nth-child(5) { color: #16a34a; font-weight: 700; }
.action-button { color: #fff; border: none; cursor: pointer; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; position: relative; overflow: hidden; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); }
`;
