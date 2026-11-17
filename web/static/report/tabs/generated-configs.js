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

        // load + render after DOM insertion (fetch from the API)
        setTimeout(function() { loadConfigs(runId); }, 50);
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

        var tblId = 'gen-configs-tbl';
        var s = '<table class="results-table" id="' + tblId + '" style="font-size:0.82em;width:100%">';
        var headers = ['Configuration', 'Architecture', 'TP', 'PP', 'Pods', 'GPUs',
                       'max_model_len', 'max_num_seqs', 'gpu_mem_util', 'block_size', 'Download'];
        s += '<tr>';
        headers.forEach(function(h, i) {
            s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tblId + '\',' + i + ',\'num\')">' + h + ' &#x21C5;</th>';
        });
        s += '</tr>';

        configs.forEach(function(c) {
            var archLabel = c.architecture === 'PD'
                ? 'PD (' + c.prefill_pods + 'P/' + c.decode_pods + 'D)'
                : c.architecture;
            var pods = c.architecture === 'PD'
                ? (c.prefill_pods + c.decode_pods)
                : c.replicas;
            s += '<tr>' +
                '<td style="font-weight:600;color:#0e7490;">' + c.name + '</td>' +
                '<td>' + archLabel + '</td>' +
                '<td style="text-align:center;">' + (c.tp || '—') + '</td>' +
                '<td style="text-align:center;">' + (c.pp > 1 ? c.pp : '—') + '</td>' +
                '<td style="text-align:center;">' + pods + '</td>' +
                '<td style="text-align:center;">' + (c.gpus || '—') + '</td>' +
                '<td>' + (c.max_model_len || '—') + '</td>' +
                '<td>' + (c.max_num_seqs || '—') + '</td>' +
                '<td>' + (c.gpu_memory_utilization != null ? c.gpu_memory_utilization : 'auto') + '</td>' +
                '<td>' + (c.block_size || '—') + '</td>' +
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
