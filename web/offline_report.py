"""Server-side builder for the self-contained offline report HTML.

The browser path (static/js/report-download.js) fetches the same module
sources, slims the data, and assembles the page. This mirrors that logic
so /api/runs/<id>/report works without a browser round-trip.

Keeping the module list, CSS, and bootstrap in ONE place (report-download.js)
would be nicer, but the tab modules must be inlined for a standalone file —
read them from disk here and escape any </script> in their source.
"""
import json
import os
import re


def _read(path):
    with open(path) as f:
        return f.read()


def _safe_src(src):
    # Embedded raw in <script>...</script>, the first </script> (from a
    # template literal or string in the module) would close the tag early.
    return src.replace("</script>", "<\\/script>")


def _safe_json(obj):
    return (json.dumps(obj)
            .replace("</", "<\\/")
            .replace("<!--", "<\\!--"))


def _modules_from_js(js_src):
    m = re.search(r"const MODULES = \[(.*?)\];", js_src, re.DOTALL)
    if not m:
        return []
    return re.findall(r"'([^']+)'", m.group(1))


def _css_from_js(js_src):
    m = re.search(r"const REPORT_CSS = `(.*?)`;", js_src, re.DOTALL)
    return m.group(1) if m else ""


def _slim_data(data):
    """Drop sub-objects that tabs never render but bloat the file."""
    slim = json.loads(json.dumps(data))
    for r in slim.get("all_results") or []:
        r.pop("profiles_blob", None)
        r.pop("manifests", None)
        mj_raw = r.get("metrics_json")
        if not mj_raw:
            continue
        try:
            mj = json.loads(mj_raw) if isinstance(mj_raw, str) else mj_raw
            pm = mj.get("prometheus_metrics") or {}

            def avg(k):
                return (pm.get(k) or {}).get("avg")

            def mx(k):
                return (pm.get(k) or {}).get("max")

            r["_prompt_tps_avg"] = avg("vllm_prompt_tokens_rate")
            r["_prompt_tps_max"] = mx("vllm_prompt_tokens_rate")
            r["_gen_tps_avg"] = avg("vllm_generation_tokens_rate")
            r["_gen_tps_max"] = mx("vllm_generation_tokens_rate")
            r["_per_gpu_tps"] = mj.get("per_gpu_throughput_tokens_sec")
            r["_vllm_req_rate"] = avg("vllm_e2e_count_rate")
            r["_avg_running"] = avg("vllm_requests_running")
            r["_avg_waiting"] = avg("vllm_requests_waiting")
            r["_kv_cache_usage"] = mj.get("kv_cache_usage")
            r["_prompt_tokens_mean"] = mj.get("prompt_tokens_mean")
            r["_output_tokens_mean"] = mj.get("output_tokens_mean")
            r["_throughput_mean_mj"] = mj.get("throughput_mean")
            r["_cache_hits_avg"] = avg("vllm_prefix_cache_hits_rate")
            r["_cache_queries_avg"] = avg("vllm_prefix_cache_queries_rate")
            r["_kv_cache_avg"] = avg("vllm_kv_cache_pct")
            r["_kv_cache_max"] = mx("vllm_kv_cache_pct")
            r["_decode_time_rate"] = avg("vllm_decode_time_rate")
            r["_prefill_time_rate"] = avg("vllm_prefill_time_rate")
            r["_preemptions_rate"] = avg("vllm_preemptions_rate")
            r["_model_load_time_s"] = mj.get("model_load_time_s")
            for k in ("prometheus_metrics", "workloads", "guidellm_command"):
                mj.pop(k, None)
            r["metrics_json"] = json.dumps(mj)
        except (ValueError, TypeError, AttributeError):
            r.pop("metrics_json", None)
    return slim


_BOOTSTRAP = r"""
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

    var ctx = (typeof ReportCtx !== 'undefined') ? ReportCtx.download(runId) : null;
    if (!ctx) {
        content.innerHTML = '<div class="charts-loading" style="color:#ef4444;">Error: ReportCtx not loaded.</div>';
        return;
    }

    var subtabDefs = [];
    var sectionMap = {};
    var _goal = ((data.run_config || {}).goal || '').toLowerCase();
    var _cfgGenTabs = _goal === 'config_gen';
    ReportTabs.all().forEach(function(tab) {
        if (_cfgGenTabs && !['test-settings', 'generated-configs'].includes(tab.id)) return;
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

    ReportTabs.all().forEach(function(tab) {
        if (tab.render) {
            try { tab.render(data, ctx); } catch (e) {
                console.warn('offline render failed: ' + tab.id, e);
            }
        }
    });

    function resizeAllCharts() {
        document.querySelectorAll('[class*="js-plotly"]').forEach(function(p) {
            if (window.Plotly) Plotly.Plots.resize(p);
        });
    }
    window.addEventListener('resize', resizeAllCharts);
    window.addEventListener('load', resizeAllCharts);
    setTimeout(resizeAllCharts, 200);
    setTimeout(resizeAllCharts, 1000);
    document.querySelectorAll('.report-subtab').forEach(function(el) {
        el.addEventListener('click', function() { setTimeout(resizeAllCharts, 50); });
    });
    if (document.readyState === 'complete') {
        resizeAllCharts();
    } else {
        document.addEventListener('DOMContentLoaded', function() { setTimeout(resizeAllCharts, 50); });
    }

    window.sortReportTable = function(tableId, colIdx, type) {
        var table = document.getElementById(tableId);
        if (!table) return;
        var rows = Array.from(table.querySelectorAll('tr')).slice(1);
        var dir = table.getAttribute('data-sort-col') === String(colIdx) && table.getAttribute('data-sort-dir') === 'asc' ? 'desc' : 'asc';
        table.setAttribute('data-sort-col', String(colIdx));
        table.setAttribute('data-sort-dir', dir);
        rows.sort(function(a, b) {
            var aCell = a.cells[colIdx], bCell = b.cells[colIdx];
            var aVal, bVal;
            if (type === 'num') {
                aVal = parseFloat(aCell.getAttribute('data-val') || aCell.textContent.replace(/[^0-9.\-]/g, '')) || 0;
                bVal = parseFloat(bCell.getAttribute('data-val') || bCell.textContent.replace(/[^0-9.\-]/g, '')) || 0;
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
"""


def build_offline_report_html(run_id, data, app_root):
    js_src = _read(os.path.join(app_root, "web", "static", "js",
                                "report-download.js"))
    modules = _modules_from_js(js_src)
    css = _css_from_js(js_src)
    static_root = os.path.join(app_root, "web")

    scripts = ""
    for mod in modules:
        fp = os.path.join(static_root, mod.lstrip("/"))
        if not os.path.isfile(fp):
            continue
        label = mod.rsplit("/", 1)[-1]
        scripts += "\n/* ==== %s ==== */\n%s\n" % (label, _safe_src(_read(fp)))

    slim = _slim_data(data)
    rc = data.get("run_config") or {}
    model = (rc.get("model_name") or "model").split("/")[-1] or "model"
    goal = (rc.get("objective") or rc.get("goal") or "report")
    goal = str(goal).lower()
    import datetime
    date = datetime.date.today().isoformat()

    title = "ServeIt Studio Report — Run #%s" % run_id
    header = (
        '<div style="padding:12px 20px;background:linear-gradient(135deg,#b45309,#f59e0b);'
        'color:#fff;border-radius:12px 12px 0 0;display:flex;justify-content:space-between;'
        'align-items:center;">'
        '<div><div style="font-size:1.3em;font-weight:700;">%s</div>'
        '<div style="font-size:0.9em;opacity:0.9;margin-top:4px;">%s · %s · %s</div></div>'
        '<div style="font-size:0.8em;opacity:0.7;">Offline report — generated from the same '
        'tab modules as the live view</div></div>' % (title, model, goal, date)
    )

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
        '<title>%s</title>\n'
        '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>\n'
        '<style>\n%s\n</style>\n</head>\n'
        '<body style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
        'background:#f8fafc;margin:0;padding:20px;">\n'
        '<div style="width:100%%;">\n%s\n'
        '<div id="charts-content" style="background:#fff;border-radius:0 0 12px 12px;'
        'padding:20px;border:1px solid #e2e8f0;border-top:none;">\n'
        '<div class="charts-loading">Building report…</div>\n</div>\n</div>\n\n'
        '<script>\nvar _REPORT_DATA = %s;\nvar _RUN_ID = %d;\n%s\n</script>\n%s\n'
        '</body>\n</html>'
        % (title, css, header, _safe_json(slim), run_id, scripts, _BOOTSTRAP)
    )
