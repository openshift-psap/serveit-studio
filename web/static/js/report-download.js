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
                // Pre-extract the flat fields the configurations tab charts
                // need (they read _prompt_tps_avg etc. — small, ~10 numbers)
                r._prompt_tps_avg = (pm.vllm_prompt_tokens_rate || {}).avg || null;
                r._prompt_tps_max = (pm.vllm_prompt_tokens_rate || {}).max || null;
                r._gen_tps_avg = (pm.vllm_generation_tokens_rate || {}).avg || null;
                r._gen_tps_max = (pm.vllm_generation_tokens_rate || {}).max || null;
                r._per_gpu_tps = mj.per_gpu_throughput_tokens_sec || null;
                r._vllm_req_rate = (pm.vllm_e2e_count_rate || {}).avg || null;
                r._avg_running = (pm.vllm_requests_running || {}).avg || null;
                r._avg_waiting = (pm.vllm_requests_waiting || {}).avg || null;
                r._kv_cache_usage = mj.kv_cache_usage || null;
                r._prompt_tokens_mean = mj.prompt_tokens_mean || null;
                r._output_tokens_mean = mj.output_tokens_mean || null;
                r._throughput_mean_mj = mj.throughput_mean || null;
                r._cache_hits_avg = (pm.vllm_prefix_cache_hits_rate || {}).avg || null;
                r._cache_queries_avg = (pm.vllm_prefix_cache_queries_rate || {}).avg || null;
                r._kv_cache_avg = (pm.vllm_kv_cache_pct || {}).avg || null;
                r._kv_cache_max = (pm.vllm_kv_cache_pct || {}).max || null;
                r._decode_time_rate = (pm.vllm_decode_time_rate || {}).avg || null;
                r._prefill_time_rate = (pm.vllm_prefill_time_rate || {}).avg || null;
                r._preemptions_rate = (pm.vllm_preemptions_rate || {}).avg || null;
                r._model_load_time_s = mj.model_load_time_s || null;
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
`;
