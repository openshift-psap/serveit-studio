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
                pane.querySelectorAll('.chart-plot').forEach(plot => {
                    if (plot.data) Plotly.Plots.resize(plot);
                });
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
        const goalMap = { ttft: 'TTFT', throughput: 'Throughput', balanced: 'Balanced' };
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

    // Best TTFT
    html += '<tr><td><strong>Best TTFT P90</strong></td>';
    const ttftVals = runs.map(r => {
        const b = (r.data.summary.best_configs || {}).lowest_latency;
        return b ? b.ttft_p90 : null;
    });
    const bestTtft = Math.min(...ttftVals.filter(v => v != null));
    runs.forEach((r, i) => {
        const v = ttftVals[i];
        const style = v === bestTtft ? 'color:#059669; font-weight:700;' : '';
        html += '<td style="' + style + '">' + (v != null ? v.toFixed(1) + ' ms' : '-') + '</td>';
    });
    html += '</tr>';

    // Best Throughput
    html += '<tr><td><strong>Best Throughput P90</strong></td>';
    const tputVals = runs.map(r => {
        const b = (r.data.summary.best_configs || {}).highest_throughput;
        return b ? b.throughput_p90 : null;
    });
    const bestTput = Math.max(...tputVals.filter(v => v != null));
    runs.forEach((r, i) => {
        const v = tputVals[i];
        const style = v === bestTput ? 'color:#059669; font-weight:700;' : '';
        html += '<td style="' + style + '">' + (v != null ? v.toFixed(2) + ' req/s' : '-') + '</td>';
    });
    html += '</tr>';

    // Best Efficiency
    html += '<tr><td><strong>Best Efficiency</strong></td>';
    const effVals = runs.map(r => {
        const b = (r.data.summary.best_configs || {}).most_efficient;
        return b ? b.efficiency : null;
    });
    const bestEff = Math.max(...effVals.filter(v => v != null));
    runs.forEach((r, i) => {
        const v = effVals[i];
        const style = v === bestEff ? 'color:#059669; font-weight:700;' : '';
        html += '<td style="' + style + '">' + (v != null ? v.toFixed(3) + ' req/s/GPU' : '-') + '</td>';
    });
    html += '</tr>';

    html += '</table></div></div>';

    // Comparison charts
    const cmpSfx = '-' + tabId;
    html += '<div class="charts-grid-2col">';
    html += '<div class="chart-card"><div class="chart-card-header">Best TTFT P90 by Run</div><div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em;">Lower is better. Shows the best TTFT P90 achieved in each run.</div><div class="chart-card-body"><div id="cmp-ttft' + cmpSfx + '" class="chart-plot"></div></div></div>';
    html += '<div class="chart-card"><div class="chart-card-header">Best Throughput P90 by Run</div><div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em;">Higher is better. Shows the best throughput P90 achieved in each run.</div><div class="chart-card-body"><div id="cmp-tput' + cmpSfx + '" class="chart-plot"></div></div></div>';
    html += '</div>';

    html += '<div class="charts-grid-2col">';
    html += '<div class="chart-card"><div class="chart-card-header">GPU Efficiency by Run</div><div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em;">Higher is better. Best efficiency (req/s per GPU) from each run.</div><div class="chart-card-body"><div id="cmp-eff' + cmpSfx + '" class="chart-plot"></div></div></div>';
    html += '<div class="chart-card"><div class="chart-card-header">All Configs: Throughput vs Latency</div><div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em;">Every tested configuration from all runs overlaid. Top-left is ideal (low latency, high throughput).</div><div class="chart-card-body"><div id="cmp-scatter' + cmpSfx + '" class="chart-plot"></div></div></div>';
    html += '</div>';

    panel.innerHTML = html;

    // Render comparison Plotly charts
    const plotlyLayout = { margin: { t: 10, b: 60, l: 50, r: 20 }, height: 430, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent', font: { family: 'Inter, sans-serif' } };
    const plotlyConfig = { responsive: true, displayModeBar: true };

    const runLabels = runs.map(r => 'Run #' + r.runId);
    const barColors = runs.map(r => r.color);

    // TTFT bar chart
    Plotly.newPlot('cmp-ttft' + cmpSfx, [{
        x: runLabels, y: ttftVals, type: 'bar',
        marker: { color: barColors },
        text: ttftVals.map(v => fmtSI(v) + ' ms'), textposition: 'outside',
        textfont: { size: 11, color: '#333' }, cliponaxis: false, constraintext: 'none',
        hovertemplate: '<b>%{x}</b><br>%{y:.1f} ms<extra></extra>'
    }], { ...plotlyLayout, yaxis: { title: 'TTFT P90 (ms) - lower is better', tickformat: '.2s' } }, plotlyConfig);

    // Throughput bar chart
    Plotly.newPlot('cmp-tput' + cmpSfx, [{
        x: runLabels, y: tputVals, type: 'bar',
        marker: { color: barColors },
        text: tputVals.map(v => v.toFixed(2) + ' req/s'), textposition: 'outside',
        textfont: { size: 11, color: '#333' }, cliponaxis: false, constraintext: 'none',
        hovertemplate: '<b>%{x}</b><br>%{y:.2f} req/s<extra></extra>'
    }], { ...plotlyLayout, yaxis: { title: 'Throughput P90 (req/s) - higher is better' } }, plotlyConfig);

    // Efficiency bar chart
    Plotly.newPlot('cmp-eff' + cmpSfx, [{
        x: runLabels, y: effVals, type: 'bar',
        marker: { color: barColors },
        text: effVals.map(v => v.toFixed(3)), textposition: 'outside',
        textfont: { size: 11, color: '#333' }, cliponaxis: false, constraintext: 'none',
        hovertemplate: '<b>%{x}</b><br>%{y:.3f} req/s/GPU<extra></extra>'
    }], { ...plotlyLayout, yaxis: { title: 'Efficiency (req/s/GPU) - higher is better' } }, plotlyConfig);

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
