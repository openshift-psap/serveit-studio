// tabs/deploy-timing.js — Deploy Timing report tab. One implementation for
// live + offline reports.
(function() {

    function timingData(data) {
        var out = [];
        (data.all_results || []).forEach(function(r) {
            var mj = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
            var loadTime = mj.model_load_time_s;
            // Include ALL results — None means "pods reused" (0s load time), not "skip"
            var load = (loadTime != null && loadTime > 0) ? loadTime : 0;
            out.push({
                name: r.config_name, load_s: load, arch: r.architecture,
                pods: (r.prefill_pods || 0) + (r.decode_pods || 0) || r.replicas || 0,
                reused: !(loadTime != null && loadTime > 0)
            });
        });
        return out;
    }

    function build(data, ctx) {
        var timings = timingData(data);
        if (!timings.length) return '';

        var dtTblId = ctx.chartId('deploy-timing-tbl');
        var s = '';
        s += '<div class="chart-card" style="border-left:6px solid #475569;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#475569,#64748b); color:white;">Deploy Timing</div>' +
            '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Pod startup and <strong>model loading times</strong> for each tested configuration. ' +
            'Larger TP values typically load faster (fewer replicas), while smaller TP with more replicas takes longer as each pod loads the model independently.' +
            '</div></div>';

        s += '<div class="chart-card" style="margin-top:24px; border-left:6px solid #d97706;">';
        s += '<div class="chart-card-header" style="background:linear-gradient(135deg,#d97706,#b45309); color:white; font-size:1.2em;">Deployment Timing</div>';
        s += '<div style="padding:8px 20px; color:#1e293b; font-size:0.95em;">Time from pod deployment to model fully loaded and serving. Includes scheduling, image pull, model weight loading, and CUDA graph capture.</div>';
        s += '<div id="' + ctx.chartId('deploy-timing') + '" style="width:100%;height:850px;"></div>';

        // Data table
        s += '<div style="padding:12px 20px;"><table class="results-table" id="' + dtTblId + '" style="font-size:0.85em;">';
        s += '<tr>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + dtTblId + '\',0,\'str\')">Configuration &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + dtTblId + '\',1,\'str\')">Architecture &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + dtTblId + '\',2,\'num\')">Pods &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + dtTblId + '\',3,\'num\')">Model Load Time &#x21C5;</th>';
        s += '</tr>';
        timings.slice().sort(function(a, b) { return a.load_s - b.load_s; }).forEach(function(d) {
            var mins = Math.floor(d.load_s / 60);
            var secs = d.load_s % 60;
            var timeStr = d.reused ? 'reused' : (mins > 0 ? mins + 'm ' + secs + 's' : secs + 's');
            var reusedStyle = d.reused ? ' style="color:#94a3b8;"' : '';
            s += '<tr><td>' + d.name + '</td><td>' + d.arch + '</td><td>' + d.pods + '</td><td' + reusedStyle + '>' + timeStr + '</td></tr>';
        });
        s += '</table></div>';
        s += '</div>';
        return s;
    }

    function render(data, ctx) {
        var timings = timingData(data);
        if (!timings.length) return;
        var el = document.getElementById(ctx.chartId('deploy-timing'));
        if (!el || typeof Plotly === 'undefined') return;
        var archColorsDT = { AGGREGATED: '#6366f1', PD: '#10b981', EP: '#f59e0b' };
        var sorted = timings.slice().sort(function(a, b) { return b.load_s - a.load_s; });
        Plotly.newPlot(el, [{
            x: sorted.map(function(d) { return d.name; }),
            y: sorted.map(function(d) { return d.load_s; }),
            type: 'bar',
            marker: { color: sorted.map(function(d) { return archColorsDT[d.arch] || '#94a3b8'; }) },
            text: sorted.map(function(d) { return d.load_s + 's'; }),
            textposition: 'outside',
            textfont: { size: 13, color: '#334155' },
            cliponaxis: false, constraintext: 'none',
            hovertemplate: '<b>%{x}</b><br>Load time: %{y}s<extra></extra>'
        }], {
            xaxis: { tickangle: -35, automargin: true, tickfont: { size: 11 } },
            yaxis: { title: 'Model Load Time (seconds)' },
            plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
            margin: { t: 20, b: 120, l: 70, r: 20 }
        }, { responsive: true });
    }

    ReportTabs.register({
        id: 'deploy-timing',
        label: 'Deploy Timing',
        icon: '&#9202;',
        position: 'after:traffic',
        build: build,
        render: render
    });
})();
