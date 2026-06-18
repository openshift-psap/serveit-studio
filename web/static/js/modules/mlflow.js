/* MLflow export dialog */

function openMlflowDialog() {
    document.getElementById('mlflow-overlay').style.display = 'flex';
    // Don't reload if export is in progress
    if (window._mlflowExporting) return;
    fetch('/api/mlflow/config')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success && data.config) {
                document.getElementById('mlflow-uri').value = data.config.tracking_uri || '';
                document.getElementById('mlflow-user').value = data.config.username || '';
                document.getElementById('mlflow-experiment').value = data.config.experiment_name || '';
                if (data.config.insecure_tls !== undefined) document.getElementById('mlflow-insecure-tls').checked = data.config.insecure_tls;
            }
        });
    loadAllRuns();
}

function closeMlflowDialog() {
    document.getElementById('mlflow-overlay').style.display = 'none';
}

function saveMlflowConfig() {
    var data = {
        tracking_uri: document.getElementById('mlflow-uri').value,
        username: document.getElementById('mlflow-user').value || null,
        password: document.getElementById('mlflow-pass').value || null,
        experiment_name: document.getElementById('mlflow-experiment').value || null,
        insecure_tls: document.getElementById('mlflow-insecure-tls').checked,
    };
    fetch('/api/mlflow/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) {
                document.getElementById('mlflow-status').innerHTML = '<span style="color:#16a34a;">Config saved</span>';
                setTimeout(function() { document.getElementById('mlflow-status').textContent = ''; }, 2000);
            }
        });
}

function loadAllRuns() {
    var list = document.getElementById('mlflow-runs-list');
    list.innerHTML = '<div style="color:#94a3b8;font-size:0.85em;padding:20px;text-align:center;">Loading...</div>';
    fetch('/api/mlflow/runs')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.success || data.runs.length === 0) {
                list.innerHTML = '<div style="color:#94a3b8;font-size:0.85em;padding:20px;text-align:center;">No runs found</div>';
                return;
            }
            list.innerHTML = '';
            data.runs.forEach(function(run) {
                var runDiv = document.createElement('div');
                runDiv.style.cssText = 'margin-bottom:8px;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;';
                var modelShort = run.model ? run.model.split('/').pop() : '';
                var header = document.createElement('div');
                header.style.cssText = 'display:flex;align-items:center;gap:8px;padding:10px 12px;background:#f0f9ff;cursor:pointer;';
                header.innerHTML =
                    '<input type="checkbox" class="mlflow-run-cb" data-run-id="' + run.id + '" checked style="width:16px;height:16px;" onclick="event.stopPropagation();" onchange="toggleRunTests(' + run.id + ',this.checked)">' +
                    '<span style="font-weight:700;color:#0c4a6e;flex:1;">' + run.run_name + '</span>' +
                    '<span style="font-size:0.8em;color:#475569;">' + modelShort + '</span>' +
                    '<span style="font-size:0.75em;padding:2px 8px;border-radius:10px;background:' + (run.status === 'completed' ? '#dcfce7;color:#166534' : '#fef3c7;color:#92400e') + ';">' + run.status + '</span>' +
                    '<span class="mlflow-expand" style="font-size:0.8em;color:#94a3b8;">&#9660;</span>';
                header.onclick = function() { toggleRunExpand(run.id); };
                runDiv.appendChild(header);

                var testsDiv = document.createElement('div');
                testsDiv.id = 'mlflow-run-tests-' + run.id;
                testsDiv.style.cssText = 'display:none;padding:4px 8px 8px;background:white;';
                testsDiv.innerHTML = '<div style="color:#94a3b8;font-size:0.82em;padding:8px;text-align:center;">Loading tests...</div>';
                runDiv.appendChild(testsDiv);
                list.appendChild(runDiv);
            });
        });
}

function toggleRunExpand(runId) {
    var div = document.getElementById('mlflow-run-tests-' + runId);
    if (!div) return;
    if (div.style.display === 'none') {
        div.style.display = 'block';
        if (div.querySelector('[data-loaded]')) return;
        fetch('/api/mlflow/tests/' + runId)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                div.setAttribute('data-loaded', '1');
                if (!data.success || data.tests.length === 0) {
                    div.innerHTML = '<div style="color:#94a3b8;font-size:0.82em;padding:8px;text-align:center;">No tests</div>';
                    return;
                }
                div.innerHTML = '';
                data.tests.forEach(function(t) {
                    var completed = t.status === 'completed' || t.status === 'passed';
                    var row = document.createElement('div');
                    row.style.cssText = 'display:flex;align-items:center;gap:6px;padding:4px 8px;font-size:0.8em;border-bottom:1px solid #f1f5f9;' + (completed ? '' : 'opacity:0.4;');
                    row.innerHTML =
                        '<input type="checkbox" class="mlflow-test-cb" data-run-id="' + runId + '" value="' + t.config_name + '" ' + (completed ? 'checked' : 'disabled') + ' style="width:14px;height:14px;">' +
                        '<span style="font-weight:600;min-width:170px;color:#1e293b;">' + t.config_name + '</span>' +
                        '<span style="color:#64748b;min-width:55px;">' + (t.architecture || '') + '</span>' +
                        '<span style="color:#0ea5e9;min-width:40px;">TP' + t.tensor_parallelism + '</span>' +
                        (t.ttft_p90 ? '<span style="color:#8b5cf6;min-width:85px;">TTFT ' + Math.round(t.ttft_p90) + 'ms</span>' : '<span style="min-width:85px;"></span>') +
                        (t.throughput_p90 ? '<span style="color:#f59e0b;min-width:70px;">' + t.throughput_p90.toFixed(1) + ' req/s</span>' : '<span style="min-width:70px;"></span>') +
                        '<span style="margin-left:auto;color:' + (completed ? '#16a34a' : '#ef4444') + ';">' + t.status + '</span>';
                    div.appendChild(row);
                });
            });
    } else {
        div.style.display = 'none';
    }
}

function toggleRunTests(runId, checked) {
    document.querySelectorAll('.mlflow-test-cb[data-run-id="' + runId + '"]:not(:disabled)').forEach(function(cb) { cb.checked = checked; });
}

function toggleMlflowAll(checked) {
    document.querySelectorAll('.mlflow-run-cb').forEach(function(cb) { cb.checked = checked; toggleRunTests(cb.dataset.runId, checked); });
}

function exportAllMlflow() {
    var btn = document.getElementById('mlflow-export-btn');
    var status = document.getElementById('mlflow-status');

    // Collect selected runs and their tests
    var selectedRuns = [];
    document.querySelectorAll('.mlflow-run-cb:checked').forEach(function(cb) {
        var runId = parseInt(cb.dataset.runId);
        var testIds = [];
        document.querySelectorAll('.mlflow-test-cb[data-run-id="' + runId + '"]:checked').forEach(function(tcb) {
            testIds.push(tcb.value);
        });
        selectedRuns.push({run_id: runId, test_ids: testIds.length > 0 ? testIds : null});
    });

    if (selectedRuns.length === 0) { status.textContent = 'No runs selected'; return; }

    if (!document.getElementById('mlflow-spinner-style')) {
        var style = document.createElement('style');
        style.id = 'mlflow-spinner-style';
        style.textContent = '.mlflow-spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,0.3);border-top-color:white;border-radius:50%;animation:mlflow-spin 0.6s linear infinite;vertical-align:middle;margin-right:6px;}@keyframes mlflow-spin{to{transform:rotate(360deg)}}';
        document.head.appendChild(style);
    }

    window._mlflowAbort = false;
    window._mlflowExporting = true;
    var remaining = selectedRuns.length;
    btn.style.display = 'none';

    var stopBtn = document.getElementById('mlflow-stop-btn');
    if (!stopBtn) {
        stopBtn = document.createElement('button');
        stopBtn.id = 'mlflow-stop-btn';
        stopBtn.style.cssText = 'width:100%;padding:10px;background:#ef4444;color:white;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:0.95em;';
        btn.parentNode.insertBefore(stopBtn, btn.nextSibling);
    }
    stopBtn.style.display = 'block';
    stopBtn.innerHTML = '<span class="mlflow-spinner"></span> Exporting ' + remaining + ' run(s)... Click to stop';
    stopBtn.onclick = function() {
        window._mlflowAbort = true;
        stopBtn.textContent = 'Stopping...';
        stopBtn.disabled = true;
    };

    var totalExported = 0;
    var errors = [];
    var queue = selectedRuns.slice();

    function processNext() {
        if (window._mlflowAbort || queue.length === 0) {
            window._mlflowExporting = false;
            stopBtn.style.display = 'none';
            btn.style.display = 'block';
            btn.disabled = false;
            btn.textContent = 'Export Selected to MLflow';
            if (window._mlflowAbort) {
                status.innerHTML = '<span style="color:#f59e0b;">Stopped. Exported ' + totalExported + ' tests from ' + (selectedRuns.length - remaining) + ' run(s)</span>';
            } else if (errors.length === 0) {
                status.innerHTML = '<span style="color:#16a34a;">Exported ' + totalExported + ' tests from ' + selectedRuns.length + ' run(s)</span>';
            } else {
                status.innerHTML = '<span style="color:#16a34a;">Exported ' + totalExported + ' tests</span><br><span style="color:#ef4444;">' + errors.length + ' error(s): ' + errors[0] + '</span>';
            }
            return;
        }

        var sr = queue.shift();
        remaining--;
        stopBtn.innerHTML = '<span class="mlflow-spinner"></span> Exporting ' + (remaining + 1) + ' run(s) remaining... Click to stop';

        fetch('/api/mlflow/export', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                run_id: sr.run_id,
                test_ids: sr.test_ids,
                experiment_name: document.getElementById('mlflow-experiment').value || null,
            })
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) totalExported += data.total;
            else errors.push(data.error);
            status.textContent = totalExported + ' tests exported so far...';
            processNext();
        })
        .catch(function(err) {
            errors.push(String(err));
            processNext();
        });
    }

    processNext();
}
