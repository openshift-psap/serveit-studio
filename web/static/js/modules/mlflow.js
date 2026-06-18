/* MLflow export dialog */

function openMlflowDialog() {
    document.getElementById('mlflow-overlay').style.display = 'flex';
    fetch('/api/mlflow/config')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success && data.config) {
                document.getElementById('mlflow-uri').value = data.config.tracking_uri || '';
                document.getElementById('mlflow-user').value = data.config.username || '';
                document.getElementById('mlflow-experiment').value = data.config.experiment_name || '';
            }
        });
    fetch('/api/mlflow/runs')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                var sel = document.getElementById('mlflow-run-select');
                sel.innerHTML = '<option value="">-- Select a run --</option>';
                data.runs.forEach(function(run) {
                    var opt = document.createElement('option');
                    opt.value = run.id;
                    opt.textContent = run.run_name + ' (' + run.model.split('/').pop() + ') - ' + run.status;
                    sel.appendChild(opt);
                });
                if (data.runs.length === 1) {
                    sel.value = data.runs[0].id;
                    loadMlflowTests(data.runs[0].id);
                }
            }
        });
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

function loadMlflowTests(runId) {
    var list = document.getElementById('mlflow-tests-list');
    var btn = document.getElementById('mlflow-export-btn');
    if (!runId) {
        list.innerHTML = '<div style="color:#94a3b8;font-size:0.85em;padding:20px;text-align:center;">Select a run to see tests</div>';
        btn.disabled = true;
        return;
    }
    list.innerHTML = '<div style="color:#94a3b8;font-size:0.85em;padding:20px;text-align:center;">Loading...</div>';
    fetch('/api/mlflow/tests/' + runId)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                list.innerHTML = '';
                if (data.tests.length === 0) {
                    list.innerHTML = '<div style="color:#94a3b8;font-size:0.85em;padding:20px;text-align:center;">No tests in this run</div>';
                    btn.disabled = true;
                    return;
                }
                data.tests.forEach(function(t) {
                    var completed = t.status === 'completed' || t.status === 'passed';
                    var div = document.createElement('div');
                    div.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 10px;margin-bottom:2px;border-radius:4px;font-size:0.83em;' + (completed ? 'background:white;' : 'background:#f1f5f9;opacity:0.5;');
                    div.innerHTML =
                        '<input type="checkbox" class="mlflow-test-cb" value="' + t.config_name + '" ' + (completed ? 'checked' : 'disabled') + ' style="width:15px;height:15px;">' +
                        '<span style="font-weight:600;min-width:180px;">' + t.config_name + '</span>' +
                        '<span style="color:#64748b;min-width:60px;">' + (t.architecture || '') + '</span>' +
                        '<span style="color:#0ea5e9;min-width:50px;">TP' + t.tensor_parallelism + '</span>' +
                        (t.ttft_p90 ? '<span style="color:#8b5cf6;min-width:90px;">TTFT ' + Math.round(t.ttft_p90) + 'ms</span>' : '<span style="min-width:90px;"></span>') +
                        (t.throughput_p90 ? '<span style="color:#f59e0b;">' + t.throughput_p90.toFixed(1) + ' req/s</span>' : '') +
                        '<span style="margin-left:auto;font-size:0.9em;color:' + (completed ? '#16a34a' : '#ef4444') + ';">' + t.status + '</span>';
                    list.appendChild(div);
                });
                btn.disabled = false;
                document.getElementById('mlflow-select-all').checked = true;
            }
        });
}

function toggleMlflowAll(checked) {
    document.querySelectorAll('.mlflow-test-cb:not(:disabled)').forEach(function(cb) { cb.checked = checked; });
}

function exportToMlflow() {
    var runId = document.getElementById('mlflow-run-select').value;
    if (!runId) { document.getElementById('mlflow-status').textContent = 'Select a run first'; return; }
    var testIds = [];
    document.querySelectorAll('.mlflow-test-cb:checked').forEach(function(cb) { testIds.push(cb.value); });
    if (testIds.length === 0) { document.getElementById('mlflow-status').textContent = 'No tests selected'; return; }

    var btn = document.getElementById('mlflow-export-btn');
    var status = document.getElementById('mlflow-status');
    btn.disabled = true;
    btn.textContent = 'Exporting...';
    status.textContent = 'Exporting ' + testIds.length + ' tests to MLflow...';

    fetch('/api/mlflow/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            run_id: parseInt(runId),
            test_ids: testIds,
            experiment_name: document.getElementById('mlflow-experiment').value || null,
        })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        btn.disabled = false;
        btn.textContent = 'Export to MLflow';
        if (data.success) {
            status.innerHTML = '<span style="color:#16a34a;">Exported ' + data.total + ' tests successfully</span>';
            if (data.errors && data.errors.length > 0) {
                status.innerHTML += '<br><span style="color:#f59e0b;">' + data.errors.length + ' tests had errors</span>';
            }
        } else {
            status.innerHTML = '<span style="color:#ef4444;">' + data.error + '</span>';
        }
    })
    .catch(function(err) {
        btn.disabled = false;
        btn.textContent = 'Export to MLflow';
        status.innerHTML = '<span style="color:#ef4444;">Export failed: ' + err + '</span>';
    });
}
