// ui-helpers.js — Report subtabs, comparison, sidebar toggle

function initReportSubtabs(container) {
    const wrapper = container.querySelector('.report-subtabs-container');
    if (!wrapper) return;
    const bar = wrapper.querySelector('.report-subtab-bar');
    const panes = wrapper.querySelectorAll('.report-subtab-pane');
    const tabs = bar.querySelectorAll('.report-subtab');

    // Activate first pane (all were visible for Plotly rendering)
    panes.forEach((p, i) => {
        if (i === 0) p.classList.add('active');
    });
    wrapper.classList.add('subtabs-ready');

    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const paneId = tab.dataset.subtab;
            tabs.forEach(t => t.classList.remove('active'));
            panes.forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            const pane = wrapper.querySelector(`[data-subtab-pane="${paneId}"]`);
            if (pane) {
                pane.classList.add('active');
                setTimeout(function() {
                    pane.querySelectorAll('.js-plotly-plot').forEach(plot => {
                        Plotly.Plots.resize(plot);
                    });
                }, 50);
                // Render EPP charts on first tab click
                if (paneId.startsWith('epp-tuning') && window._eppChartRenders && window._eppChartRenders.length) {
                    setTimeout(() => {
                        window._eppChartRenders.forEach(fn => fn());
                        window._eppChartRenders = [];
                    }, 50);
                }
            }
        });
    });
}

function chartCard(title, description, plotId) {
    return `<div class="chart-card"><div class="chart-card-header">${title}</div><div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">${description}</div><div class="chart-card-body"><div id="${plotId}${_chartSuffix}" class="chart-plot"></div></div></div>`;
}

function statCard(value, label, detail) {
    return `<div class="chart-stat-card"><div class="stat-value">${value}</div><div class="stat-label">${label}</div>${detail ? `<div class="stat-detail">${detail}</div>` : ''}</div>`;
}

// ===== COMPARISON TAB =====
function generateComparison() {
    try {
    // Remove any existing comparison tab
    reportTabs.filter(t => t.isComparison).forEach(t => closeReportTab(t.id));

    const runTabs = reportTabs.filter(t => !t.isComparison && tabDataCache[t.id]);
    if (runTabs.length < 2) {
        console.warn('Compare: need 2+ runs with data. Tabs:', reportTabs.length, 'with cache:', runTabs.length);
        return;
    }

    const tabId = 'rt' + (++_tabCounter);
    reportTabs.push({ id: tabId, runId: null, label: 'Compare', isComparison: true });

    const placeholder = document.querySelector('#charts-content > .charts-loading');
    if (placeholder) placeholder.remove();

    const panel = document.createElement('div');
    panel.id = 'panel-' + tabId;
    panel.className = 'report-tab-panel';
    document.getElementById('charts-content').appendChild(panel);

    updateTabBar();
    switchReportTab(tabId);

    // Gather data
    const runColors = ['#3b82f6', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16'];
    const runs = runTabs.map((t, i) => ({
        tabId: t.id,
        runId: t.runId,
        color: runColors[i % runColors.length],
        data: tabDataCache[t.id]
    }));

    let html = '';

    // Header
    html += '<div class="chart-card" style="border:3px solid #6366f1; border-left:8px solid #6366f1;">';
    html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#8b5cf6); color:white; font-size:1.3em;">Run Comparison</div>';
    html += '<div class="chart-card-body" style="padding:20px;">';
    html += '<p style="color:#475569; margin:0;">Comparing <strong>' + runs.length + '</strong> optimization runs side by side.</p>';
    html += '</div></div>';

    // Summary table
    html += '<div class="chart-card"><div class="chart-card-header">Summary</div>';
    html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
    html += '<tr><th>Metric</th>';
    runs.forEach(r => { html += '<th style="border-left:3px solid ' + r.color + ';">Run #' + r.runId + '</th>'; });
    html += '</tr>';

    // Model
    html += '<tr><td><strong>Model</strong></td>';
    runs.forEach(r => {
        const rec = r.data.recommendation || {};
        html += '<td>' + (rec.model || '-') + '</td>';
    });
    html += '</tr>';

    // Goal
    html += '<tr><td><strong>Goal</strong></td>';
    runs.forEach(r => {
        const rec = r.data.recommendation || {};
        const goalMap = { ttft: 'TTFT', throughput: 'Throughput', balanced: 'Full Coverage' };
        html += '<td>' + (goalMap[rec.goal] || rec.goal || '-') + '</td>';
    });
    html += '</tr>';

    // Workload
    html += '<tr><td><strong>Workload</strong></td>';
    runs.forEach(r => {
        const w = (r.data.recommendation || {}).workload || {};
        html += '<td>ISL=' + (w.isl || '?') + ' OSL=' + (w.osl || '?') + ' / ' + (w.users || '?') + 'u</td>';
    });
    html += '</tr>';

    // Tests
    html += '<tr><td><strong>Tests</strong></td>';
    runs.forEach(r => { html += '<td>' + (r.data.summary.total_tests || 0) + ' total, ' + (r.data.summary.successful_tests || 0) + ' passed</td>'; });
    html += '</tr>';

    // Helper: render a comparison row with best highlighting
    function cmpRow(label, vals, best, fmt, lowerIsBetter) {
        html += '<tr><td><strong>' + label + '</strong></td>';
        runs.forEach((r, i) => {
            const v = vals[i];
            const style = v != null && v === best ? 'color:#059669; font-weight:700;' : '';
            html += '<td style="' + style + '">' + (v != null ? fmt(v) : '-') + '</td>';
        });
        html += '</tr>';
    }

    // Best TTFT P90/P95/P99
    const ttftVals = runs.map(r => { const b = (r.data.summary.best_configs || {}).lowest_latency; return b ? b.ttft_p90 : null; });
    const ttft95Vals = runs.map(r => { const b = (r.data.summary.best_configs || {}).lowest_latency; return b && b.ttft_p95 ? b.ttft_p95 : null; });
    const ttft99Vals = runs.map(r => { const b = (r.data.summary.best_configs || {}).lowest_latency; return b && b.ttft_p99 ? b.ttft_p99 : null; });
    cmpRow('Best TTFT P90', ttftVals, Math.min(...ttftVals.filter(v => v != null)), v => v.toFixed(1) + ' ms');
    cmpRow('Best TTFT P95', ttft95Vals, Math.min(...ttft95Vals.filter(v => v != null)), v => v.toFixed(1) + ' ms');
    cmpRow('Best TTFT P99', ttft99Vals, Math.min(...ttft99Vals.filter(v => v != null)), v => v.toFixed(1) + ' ms');

    // Best Throughput Mean
    const tputVals = runs.map(r => { const b = (r.data.summary.best_configs || {}).highest_throughput; return b && b.throughput_mean ? b.throughput_mean : (b ? b.throughput_p90 : null); });
    cmpRow('Best Throughput Mean', tputVals, Math.max(...tputVals.filter(v => v != null)), v => v.toFixed(2) + ' req/s');

    // Best Efficiency
    const effVals = runs.map(r => { const b = (r.data.summary.best_configs || {}).most_efficient; return b ? b.efficiency : null; });
    cmpRow('Best Efficiency', effVals, Math.max(...effVals.filter(v => v != null)), v => v.toFixed(3) + ' req/s/GPU');

    // Per-architecture breakdown
    html += '<tr style="border-top:2px solid #e2e8f0;"><td colspan="' + (runs.length + 1) + '" style="font-weight:700; color:#6366f1; padding-top:12px;">Per-Architecture Best</td></tr>';
    ['PD', 'AGGREGATED'].forEach(arch => {
        const archVals90 = runs.map(r => { const ba = ((r.data.summary.best_configs || {}).by_architecture || {})[arch]; return ba ? ba.best_ttft_p90 : null; });
        const archVals99 = runs.map(r => { const ba = ((r.data.summary.best_configs || {}).by_architecture || {})[arch]; return ba ? ba.best_ttft_p99 : null; });
        const archTput = runs.map(r => { const ba = ((r.data.summary.best_configs || {}).by_architecture || {})[arch]; return ba ? ba.best_throughput_mean : null; });
        if (archVals90.some(v => v != null)) {
            cmpRow(arch + ' TTFT P90', archVals90, Math.min(...archVals90.filter(v => v != null)), v => v.toFixed(1) + ' ms');
            cmpRow(arch + ' TTFT P99', archVals99, Math.min(...archVals99.filter(v => v != null)), v => v.toFixed(1) + ' ms');
            cmpRow(arch + ' Throughput Mean', archTput, Math.max(...archTput.filter(v => v != null)), v => v.toFixed(2) + ' req/s');
        }
    });

    // Winner config details
    html += '<tr style="border-top:2px solid #e2e8f0;"><td><strong>Best TTFT Config</strong></td>';
    runs.forEach(r => {
        const b = (r.data.summary.best_configs || {}).lowest_latency;
        html += '<td>' + (b ? b.name + ' <span style="color:#94a3b8;font-size:0.8em;">(' + (b.architecture || '?') + ')</span>' : '-') + '</td>';
    });
    html += '</tr>';
    html += '<tr><td><strong>Best Throughput Config</strong></td>';
    runs.forEach(r => {
        const b = (r.data.summary.best_configs || {}).highest_throughput;
        html += '<td>' + (b ? b.name + ' <span style="color:#94a3b8;font-size:0.8em;">(' + (b.architecture || '?') + ')</span>' : '-') + '</td>';
    });
    html += '</tr>';

    html += '</table></div></div>';

    // Recommended config comparison cards
    html += '<div class="chart-card"><div class="chart-card-header">Recommended Configurations</div>';
    html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
    html += '<tr><th>Recommendation</th>';
    runs.forEach(r => { html += '<th style="border-left:3px solid ' + r.color + ';">Run #' + r.runId + '</th>'; });
    html += '</tr>';
    ['response_time', 'throughput'].forEach(key => {
        const label = key === 'response_time' ? '⏱ Best TTFT' : '⚡ Best Throughput';
        html += '<tr><td><strong>' + label + '</strong></td>';
        runs.forEach(r => {
            const recs = (r.data.recommendation || {}).recommendations || {};
            const rec = recs[key];
            if (rec) {
                const arch = rec.architecture || '?';
                const ttft = rec.config.ttft_p90 ? rec.config.ttft_p90.toFixed(1) + 'ms' : '?';
                const tput = rec.config.throughput_p90 ? rec.config.throughput_p90.toFixed(2) : '?';
                const deploy = rec.deploy || '';
                html += '<td><div style="font-weight:700;color:#1e293b;">' + deploy + '</div>';
                html += '<div style="font-size:0.85em;color:#64748b;">TTFT=' + ttft + ' | Tput=' + tput + ' req/s</div>';
                html += '<div style="font-size:0.75em;color:#94a3b8;margin-top:2px;">' + arch + '</div></td>';
            } else {
                html += '<td>-</td>';
            }
        });
        html += '</tr>';
    });
    html += '</table></div></div>';

    // Comparison charts (full-width)
    const cmpSfx = '-' + tabId;
    html += '<div class="chart-card"><div class="chart-card-header">Best TTFT by Run (P90 / P95 / P99)</div><div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em;">Lower is better. Grouped bars show best TTFT at each percentile per run.</div><div class="chart-card-body"><div id="cmp-ttft' + cmpSfx + '" class="chart-plot"></div></div></div>';
    html += '<div class="chart-card"><div class="chart-card-header">Best Throughput Mean by Run</div><div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em;">Higher is better. Shows the best mean throughput achieved in each run.</div><div class="chart-card-body"><div id="cmp-tput' + cmpSfx + '" class="chart-plot"></div></div></div>';
    html += '<div class="chart-card"><div class="chart-card-header">All Configs: Throughput vs Latency</div><div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em;">Every tested configuration from all runs overlaid. Top-left is ideal (low latency, high throughput).</div><div class="chart-card-body"><div id="cmp-scatter' + cmpSfx + '" class="chart-plot"></div></div></div>';

    panel.innerHTML = html;

    // Render comparison Plotly charts
    const plotlyLayout = { margin: { t: 10, b: 60, l: 50, r: 20 }, height: 430, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent', font: { family: 'Inter, sans-serif' } };
    const plotlyConfig = { responsive: true, displayModeBar: true };

    const runLabels = runs.map(r => 'Run #' + r.runId);
    const barColors = runs.map(r => r.color);

    // TTFT grouped bar chart (P90/P95/P99)
    const ttftTraces = [
        { vals: ttftVals, label: 'P90', color: '#3b82f6' },
        { vals: ttft95Vals, label: 'P95', color: '#dc2626' },
        { vals: ttft99Vals, label: 'P99', color: '#7c3aed' },
    ].map(t => ({
        x: runLabels, y: t.vals, name: 'TTFT ' + t.label, type: 'bar',
        marker: { color: t.color, opacity: 0.85 },
        text: t.vals.map(v => v != null ? fmtSI(v) + ' ms' : ''), textposition: 'outside',
        textfont: { size: 10, color: '#333' }, cliponaxis: false, constraintext: 'none',
        hovertemplate: '<b>%{x}</b><br>TTFT ' + t.label + ': %{y:.1f} ms<extra></extra>'
    }));
    Plotly.newPlot('cmp-ttft' + cmpSfx, ttftTraces, { ...plotlyLayout, barmode: 'group', yaxis: { title: 'TTFT (ms) - lower is better', tickformat: '.2s' }, showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' } }, plotlyConfig);

    // Throughput mean bar chart
    Plotly.newPlot('cmp-tput' + cmpSfx, [{
        x: runLabels, y: tputVals, type: 'bar',
        marker: { color: barColors },
        text: tputVals.map(v => v != null ? v.toFixed(2) + ' req/s' : ''), textposition: 'outside',
        textfont: { size: 11, color: '#333' }, cliponaxis: false, constraintext: 'none',
        hovertemplate: '<b>%{x}</b><br>%{y:.2f} req/s<extra></extra>'
    }], { ...plotlyLayout, yaxis: { title: 'Throughput Mean (req/s) - higher is better' } }, plotlyConfig);

    // Scatter: all configs from all runs
    const scatterTraces = runs.map(r => {
        const results = r.data.all_results || [];
        return {
            x: results.map(c => c.ttft_p90),
            y: results.map(c => c.throughput_p90),
            text: results.map(c => c.config_name + ' (' + c.architecture + ')'),
            name: 'Run #' + r.runId,
            mode: 'markers',
            marker: { size: results.map(c => Math.max(8, Math.min(c.gpus * 2, 30))), color: r.color, opacity: 0.7, line: { width: 1, color: 'white' } },
            hovertemplate: '<b>%{text}</b><br>TTFT: %{x:.1f} ms<br>Throughput: %{y:.2f} req/s<extra>Run #' + r.runId + '</extra>'
        };
    });
    Plotly.newPlot('cmp-scatter' + cmpSfx, scatterTraces, {
        ...plotlyLayout,
        xaxis: { title: 'TTFT P90 (ms) - lower is better' },
        yaxis: { title: 'Throughput P90 (req/s) - higher is better' },
        showlegend: true
    }, plotlyConfig);
    } catch (e) { console.error('Compare error:', e); }
}

// downloadHTMLReport() is defined in report-download.js

// Sidebar toggle for responsive layout
(function() {
    const toggle = document.getElementById('sidebar-toggle');
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (toggle && sidebar && backdrop) {
        toggle.addEventListener('click', () => {
            sidebar.classList.toggle('open');
            backdrop.classList.toggle('active');
        });
        backdrop.addEventListener('click', () => {
            sidebar.classList.remove('open');
            backdrop.classList.remove('active');
        });
    }
})();
