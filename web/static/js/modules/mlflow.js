/* MLflow export dialog */

function openMlflowDialog() {
    document.getElementById('mlflow-overlay').style.display = 'flex';
    // Load saved config
    fetch('/api/mlflow/config')
        .then(r => r.json())
        .then(data => {
            if (data.success && data.config) {
                document.getElementById('mlflow-uri').value = data.config.tracking_uri || '';
                document.getElementById('mlflow-user').value = data.config.username || '';
                document.getElementById('mlflow-experiment').value = data.config.experiment_name || '';
            }
        });
    // Load runs
    fetch('/api/mlflow/runs')
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                var sel = document.getElementById('mlflow-run-select');
                sel.innerHTML = '<option value="">-- Select a run --</option>';
                data.runs.forEach(function(run) {
                    var opt = document.createElement('option');
                    opt.value = run.id;
                    opt.textContent = run.run_name + ' (' + run.model + ') - ' + run.status;
                    sel.appendChild(opt);
                });
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
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                document.getElementById('mlflow-status').textContent = 'Config saved';
                setTimeout(function() { document.getElementById('mlflow-status').textContent = ''; }, 2000);
            }
        });
}

function loadMlflowTests(runId) {
    var section = document.getElementById('mlflow-tests-section');
    var btn = document.getElementById('mlflow-export-btn');
    if (!runId) {
        section.style.display = 'none';
        btn.disabled = true;
        return;
    }
    fetch('/api/mlflow/tests/' + runId)
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                var list = document.getElementById('mlflow-tests-list');
                list.innerHTML = '';
                data.tests.forEach(function(t) {
                    var completed = t.status === 'completed' || t.status === 'passed';
                    var label = document.createElement('label');
                    label.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 8px;font-size:0.85em;' + (completed ? '' : 'opacity:0.4;');
                    label.innerHTML = '<input type="checkbox" class="mlflow-test-cb" value="' + t.config_name + '" ' +
                        (completed ? 'checked' : 'disabled') + '>' +
                        '<span style="font-weight:600;">' + t.config_name + '</span>' +
                        '<span style="color:#64748b;">' + (t.architecture || '') + ' TP' + t.tensor_parallelism + '</span>' +
                        (t.ttft_p90 ? '<span style="color:#0ea5e9;">TTFT=' + Math.round(t.ttft_p90) + 'ms</span>' : '') +
                        '<span style="color:' + (completed ? '#16a34a' : '#ef4444') + ';">' + t.status + '</span>';
                    list.appendChild(label);
                });
                section.style.display = 'block';
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
    if (!runId) return;
    var testIds = [];
    document.querySelectorAll('.mlflow-test-cb:checked').forEach(function(cb) { testIds.push(cb.value); });
    if (testIds.length === 0) {
        document.getElementById('mlflow-status').textContent = 'No tests selected';
        return;
    }
    var btn = document.getElementById('mlflow-export-btn');
    var status = document.getElementById('mlflow-status');
    btn.disabled = true;
    status.textContent = 'Exporting ' + testIds.length + ' tests...';

    fetch('/api/mlflow/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            run_id: parseInt(runId),
            test_ids: testIds,
            experiment_name: document.getElementById('mlflow-experiment').value || null,
        })
    })
    .then(r => r.json())
    .then(data => {
        btn.disabled = false;
        if (data.success) {
            status.innerHTML = '<span style="color:#16a34a;">Exported ' + data.total + ' tests to MLflow</span>';
        } else {
            status.innerHTML = '<span style="color:#ef4444;">Error: ' + data.error + '</span>';
        }
    })
    .catch(err => {
        btn.disabled = false;
        status.innerHTML = '<span style="color:#ef4444;">Export failed: ' + err + '</span>';
    });
}
