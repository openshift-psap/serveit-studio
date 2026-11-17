// tabs/generated-configs.js — Config Generator output
// Lists every generated configuration with tuned params, inline YAML
// preview, and per-config / download-all buttons.
(function() {
    var H = window.ReportHelpers;

    function build(data, ctx) {
        // only render for config_gen runs
        var goal = ((data.run_config || {}).goal || '').toLowerCase();
        if (goal !== 'config_gen') return '';

        var runId = ctx.runId;
        var s = '';
        s += '<div class="chart-card" style="margin-top:16px; border-left:6px solid #0891b2;">' +
            '<div class="chart-card-header" style="background:linear-gradient(135deg,#0891b2,#22d3ee);">&#9881;&#65039; Generated Configurations</div>' +
            '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            'Every deployable configuration for the scanned system and workload — rendered with the same auto-tuned ' +
            'parameters and EPP templates a real run would deploy. Download individually or as a bundle.</div>' +
            '<div style="padding:8px 20px 16px;">' +
            '<button id="gen-configs-download-all" style="padding:8px 18px;background:#0891b2;color:white;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:0.9em;">&#11015;&#65039; Download All (zip)</button>' +
            '<span id="gen-configs-status" style="margin-left:12px;color:#64748b;font-size:0.85em;">Loading…</span>' +
            '</div></div>';

        s += '<div id="gen-configs-table" style="margin:0 20px 20px;">' +
            '<div style="padding:20px;color:#64748b;text-align:center;">Loading configurations…</div></div>';

        // prefer configs embedded in the report data (works offline);
        // fall back to the API for live views
        var embedded = data.generated_configs;
        if (Array.isArray(embedded)) {
            setTimeout(function() { renderTable(embedded, runId); }, 0);
        } else {
            setTimeout(function() { loadConfigs(runId); }, 50);
        }
        return s;
    }

    function loadConfigs(runId) {
        fetch('/api/runs/' + runId + '/generated_configs')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.success) throw new Error(data.error || 'load failed');
                renderTable(data.configs || [], runId);
            })
            .catch(function(err) {
                var el = document.getElementById('gen-configs-table');
                if (el) el.innerHTML = '<div style="padding:20px;color:#e53e3e;text-align:center;">' +
                    'Failed to load configurations: ' + err.message + '</div>';
            });
    }

    function renderTable(configs, runId) {
        var status = document.getElementById('gen-configs-status');
        if (status) status.textContent = configs.length + ' configuration(s)';

        var el = document.getElementById('gen-configs-table');
        if (!el) return;
        if (!configs.length) {
            el.innerHTML = '<div style="padding:20px;color:#64748b;text-align:center;">No configurations found.</div>';
            return;
        }

        var btnAll = document.getElementById('gen-configs-download-all');
        if (btnAll) {
            btnAll.onclick = function() {
                window.open('/api/runs/' + runId + '/configs', '_blank');
            };
        }

        // Normalise a config entry — older summaries lack PD pod/tp
        // fields, so derive them from the name (pd-13p3d-ptp1-dtp1)
        function norm(c) {
            var o = {
                name: c.name, arch: (c.architecture || '').toLowerCase(),
                tp: c.tp, pp: c.pp, gpus: c.gpus,
                max_model_len: c.max_model_len,
                prefill_tp: c.prefill_tp, decode_tp: c.decode_tp,
                prefill_pods: c.prefill_pods, decode_pods: c.decode_pods,
                replicas: c.replicas,
                max_num_seqs: c.max_num_seqs,
                prefill_max_num_seqs: c.prefill_max_num_seqs,
                decode_max_num_seqs: c.decode_max_num_seqs,
                gpu_memory_utilization: c.gpu_memory_utilization,
                prefill_gpu_memory_utilization: c.prefill_gpu_memory_utilization,
                decode_gpu_memory_utilization: c.decode_gpu_memory_utilization
            };
            var m = /^(pd|ep)-(\d+)p(\d+)d-ptp(\d+)-dtp(\d+)/.exec(o.name || '');
            if (m) {
                o.arch = m[1];
                o.prefill_pods = o.prefill_pods != null ? o.prefill_pods : parseInt(m[2], 10);
                o.decode_pods = o.decode_pods != null ? o.decode_pods : parseInt(m[3], 10);
                o.prefill_tp = o.prefill_tp != null ? o.prefill_tp : parseInt(m[4], 10);
                o.decode_tp = o.decode_tp != null ? o.decode_tp : parseInt(m[5], 10);
                o.gpus = o.prefill_pods * o.prefill_tp + o.decode_pods * o.decode_tp;
            } else if (o.gpus == null) {
                o.gpus = (o.tp || 1) * (o.replicas || 1);
            }
            return o;
        }

        function splitPair(c, base) {
            var pf = c['prefill_' + base], dc = c['decode_' + base];
            if (pf != null && dc != null) {
                return (pf === dc) ? pf : (pf + ' / ' + dc);
            }
            var v = c[base];
            return (v != null && v !== '') ? v : 'auto';
        }

        var rows = configs.map(norm);
        var tblId = 'gen-configs-tbl';
        var s = '<table class="results-table" id="' + tblId + '" style="font-size:0.82em;width:100%">';
        var headers = ['Configuration', 'Architecture', 'TP', 'PP', 'Pods',
                       'GPUs', 'gpu_mem_util', 'max_num_seqs', 'max_model_len', 'Download'];
        s += '<tr>';
        headers.forEach(function(h, i) {
            s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',' + i + ',\'num\')">' + h + ' &#x21C5;</th>';
        });
        s += '</tr>';

        rows.forEach(function(c) {
            var isPD = (c.arch === 'pd' || c.arch === 'ep');
            var tp = isPD
                ? (c.prefill_tp === c.decode_tp
                    ? 'TP' + c.prefill_tp
                    : 'TP' + c.prefill_tp + ' / TP' + c.decode_tp)
                : 'TP' + (c.tp || 1);
            var pods = isPD
                ? c.prefill_pods + 'P + ' + c.decode_pods + 'D'
                : (c.replicas || 1) + '&#215;';
            s += '<tr>' +
                '<td style="font-weight:600;color:#0e7490;">' + c.name + '</td>' +
                '<td>' + (c.arch === 'ep' ? 'EP' : (c.arch === 'pd' ? 'PD' : 'AGGREGATED')) + '</td>' +
                '<td style="text-align:center;">' + tp + '</td>' +
                '<td style="text-align:center;">' + (c.pp > 1 ? c.pp : '&#8212;') + '</td>' +
                '<td style="text-align:center;">' + pods + '</td>' +
                '<td style="text-align:center;">' + (c.gpus != null ? c.gpus : '&#8212;') + '</td>' +
                '<td style="text-align:center;">' + splitPair(c, 'gpu_memory_utilization') + '</td>' +
                '<td style="text-align:center;">' + splitPair(c, 'max_num_seqs') + '</td>' +
                '<td>' + (c.max_model_len || '&#8212;') + '</td>' +
                '<td><button onclick="window.open(\'/api/runs/' + runId + '/configs/' + c.name + '\', \'_blank\')" ' +
                'style="padding:3px 10px;background:#f0f9ff;color:#0284c7;border:1px solid #bae6fd;border-radius:4px;cursor:pointer;font-size:0.85em;">&#11015; zip</button></td>' +
                '</tr>';
        });
        s += '</table>';
        el.innerHTML = s;
    }

    function render(data, ctx) {
        // table renders itself via loadConfigs after DOM insertion
    }

    ReportTabs.register({
        id: 'generated-configs',
        label: 'Generated Configs',
        icon: '&#9881;&#65039;',
        position: 'after:test-settings',
        build: build,
        render: render
    });
})();
