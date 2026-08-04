// resume.js — Resume Testing overlay, run management

function loadResumeRuns() {
    const content = document.getElementById('resume-table-content');
    content.innerHTML = '<div class="resume-empty">Loading runs...</div>';

    fetch('/api/runs_for_resume')
        .then(r => r.json())
        .then(runs => {
            if (!runs.length) {
                content.innerHTML = '<div class="resume-empty">No optimization runs found in database.</div>';
                return;
            }

            let html = '<table class="resume-table"><thead><tr>';
            html += '<th>ID</th><th>Date</th><th>Description</th><th>Priority</th><th>Model</th>';
            html += '<th>Workload</th><th>GPUs</th><th>Status</th><th>Progress</th><th></th><th></th>';
            html += '</tr></thead><tbody>';

            runs.forEach(run => {
                const date = new Date(run.created_at).toLocaleDateString('en-US', {
                    month: 'short', day: 'numeric', year: 'numeric',
                    hour: '2-digit', minute: '2-digit'
                });

                // Map goal to display name
                let goalLabel = 'Unknown';
                let goalClass = 'ttft';
                const goal = (run.goal || '').toLowerCase();
                if (goal === 'ttft' || goal.includes('response') || goal.includes('latency')) {
                    goalLabel = 'Response Time';
                    goalClass = 'ttft';
                } else if (goal === 'throughput' || goal.includes('throughput')) {
                    goalLabel = 'Throughput';
                    goalClass = 'throughput';
                } else if (goal === 'balanced') {
                    goalLabel = 'Balanced';
                    goalClass = 'throughput';
                } else if (goal === 'aggregated_only') {
                    goalLabel = 'Aggregated Only';
                    goalClass = 'throughput';
                } else if (goal === 'pd_only') {
                    goalLabel = 'PD Only';
                    goalClass = 'ttft';
                } else if (goal === 'ep_only') {
                    goalLabel = 'EP Only';
                    goalClass = 'throughput';
                } else if (goal) {
                    goalLabel = run.goal;
                }

                const statusClass = run.status || 'running';
                const completed = run.completed_tests || 0;
                const lastStep = run.last_step || 0;
                const completedSteps = run.completed_steps || [];

                // Build step progress label
                // Steps: 2=Decode TP, 3=Prefill TP, 7=PD Splits, 8=Validation, 9=Calibration
                const stepNames = {2: 'Decode TP', 3: 'Prefill TP', 7: 'PD/EP Tests', 8: 'Validation', 9: 'Calibration'};
                const allSteps = [2, 3, 7, 8, 9];
                let progressLabel = '';
                if (completed === 0) {
                    progressLabel = 'Not started';
                } else {
                    const doneSteps = allSteps.filter(s => completedSteps.includes(s));
                    const currentStepName = stepNames[lastStep] || `Step ${lastStep}`;
                    progressLabel = `${currentStepName} (${completed} tests)`;
                    if (run.status === 'stopped') progressLabel += ' Stopped';
                    if (run.status === 'interrupted') progressLabel += ' Interrupted';
                }

                // Determine if resumable
                const canResume = run.status !== 'completed' &&
                                  (run.status === 'stopped' || run.status === 'interrupted' ||
                                   run.status === 'failed' || run.status === 'running' ||
                                   completed > 0);

                // Truncate model name for display
                const modelShort = (run.model || '').split('/').pop() || run.model || '-';

                // Workload info
                let workload = '-';
                if (run.isl && run.osl) {
                    workload = `ISL=${run.isl}`;
                    if (run.isl_stdev) workload += `±${run.isl_stdev}`;
                    workload += ` OSL=${run.osl}`;
                    if (run.osl_stdev) workload += `±${run.osl_stdev}`;
                    if (run.turns && run.turns > 1) workload += ` ${run.turns}T`;
                    workload += ` × ${run.num_users || '?'} users`;
                }

                // GPUs
                const gpus = run.max_gpus ? `${run.max_gpus}` : '-';

                html += '<tr>';
                html += `<td style="font-weight: 700; color: #7c3aed;">#${run.id}</td>`;
                html += `<td style="white-space: nowrap;">${date}</td>`;
                const notesEsc = (run.notes || '').replace(/"/g, '&quot;').replace(/</g, '&lt;');
                html += `<td style="font-size:0.85em;color:#475569;min-width:180px;">`;
                html += `<span class="run-notes-text" data-run-id="${run.id}" style="vertical-align:middle;" title="${notesEsc}">${run.notes || '<span style="color:#cbd5e1;">—</span>'}</span>`;
                html += ` <span class="run-notes-edit" data-run-id="${run.id}" style="cursor:pointer;color:#94a3b8;font-size:1.1em;vertical-align:middle;" title="Edit description">&#9998;</span>`;
                html += `</td>`;
                html += `<td><span class="resume-goal-badge ${goalClass}">${goalLabel}</span></td>`;
                html += `<td style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${run.model || ''}">${modelShort}</td>`;
                html += `<td style="white-space: nowrap; font-size: 0.85em; color: #475569;">${workload}</td>`;
                html += `<td style="text-align: center; font-weight: 600;">${gpus}</td>`;
                html += `<td><span class="resume-status ${statusClass}">${run.status || 'unknown'}</span></td>`;
                html += `<td style="white-space: nowrap;" title="${completedSteps.map(s => stepNames[s] || s).join(', ')}">${progressLabel}</td>`;
                html += `<td>`;
                if (canResume) {
                    html += `<button class="resume-btn" data-run-id="${run.id}" data-run-name="${run.run_name || ''}">Resume</button>`;
                } else if (run.status === 'completed') {
                    html += `<span style="color: #059669; font-weight: 600; font-size: 0.85em;">Done</span>`;
                } else {
                    html += `<span style="color: #9ca3af; font-size: 0.85em;">No tests</span>`;
                }
                html += `</td>`;
                html += `<td style="white-space: nowrap;"><button class="recreate-storage-btn" data-run-id="${run.id}" title="Recreate PVCs and download model for run #${run.id}" style="font-size:0.8em;padding:3px 8px;background:#0ea5e9;color:white;border:none;border-radius:4px;cursor:pointer;">💾 Regenerate Storage</button> <button class="restart-run-btn" data-run-id="${run.id}" data-run-name="${run.run_name || ''}" title="Restart run #${run.id} from beginning">🔄</button> <button class="delete-run-btn" data-run-id="${run.id}" title="Delete run #${run.id}">🗑</button></td>`;
                html += '</tr>';
            });

            html += '</tbody></table>';
            content.innerHTML = html;

            // Attach click handlers to resume buttons
            content.querySelectorAll('.resume-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const runId = btn.dataset.runId;
                    const runName = btn.dataset.runName;
                    resumeRun(parseInt(runId), runName);
                });
            });

            // Attach click handlers to restart buttons
            content.querySelectorAll('.restart-run-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const runId = parseInt(btn.dataset.runId);
                    const runName = btn.dataset.runName;
                    document.getElementById('restart-run-id').textContent = runId;
                    const modal = document.getElementById('restart-run-modal');
                    modal.classList.add('active');

                    const confirmBtn = document.getElementById('restart-run-confirm');
                    const cancelBtn = document.getElementById('restart-run-cancel');
                    const cleanup = () => {
                        modal.classList.remove('active');
                        confirmBtn.replaceWith(confirmBtn.cloneNode(true));
                        cancelBtn.replaceWith(cancelBtn.cloneNode(true));
                    };

                    cancelBtn.addEventListener('click', cleanup, { once: true });
                    confirmBtn.addEventListener('click', () => {
                        cleanup();
                        fetch(`/api/restart_run/${runId}`, { method: 'POST' })
                            .then(res => res.json())
                            .then(data => {
                                if (data.success) {
                                    logToConsole(`Cleared ${data.deleted_tests} tests from run #${runId} — restarting from beginning`, 'success');
                                    resumeRun(runId, runName);
                                } else {
                                    logToConsole(`Failed to restart run #${runId}: ${data.error}`, 'error');
                                }
                            })
                            .catch(err => logToConsole(`Failed to restart run #${runId}: ${err.message}`, 'error'));
                    }, { once: true });
                });
            });

            // Attach click handlers to delete buttons
            content.querySelectorAll('.delete-run-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const runId = parseInt(btn.dataset.runId);
                    document.getElementById('delete-run-id').textContent = runId;
                    const modal = document.getElementById('delete-run-modal');
                    modal.classList.add('active');

                    const confirmBtn = document.getElementById('delete-run-confirm');
                    const cancelBtn = document.getElementById('delete-run-cancel');
                    const cleanup = () => {
                        modal.classList.remove('active');
                        confirmBtn.replaceWith(confirmBtn.cloneNode(true));
                        cancelBtn.replaceWith(cancelBtn.cloneNode(true));
                    };

                    cancelBtn.addEventListener('click', cleanup, { once: true });
                    confirmBtn.addEventListener('click', () => {
                        cleanup();
                        fetch(`/api/delete_run/${runId}`, { method: 'DELETE' })
                            .then(res => res.json())
                            .then(data => {
                                if (data.success) {
                                    logToConsole(`Deleted run #${runId} (${data.deleted_tests} tests removed)`, 'success');
                                    loadResumeRuns();
                                } else {
                                    logToConsole(`Failed to delete run #${runId}: ${data.error}`, 'error');
                                }
                            })
                            .catch(err => logToConsole(`Failed to delete run #${runId}: ${err.message}`, 'error'));
                    }, { once: true });
                });
            });
            // Attach click handlers to recreate storage buttons
            content.querySelectorAll('.recreate-storage-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const runId = parseInt(btn.dataset.runId);
                    btn.disabled = true;
                    btn.textContent = '⏳ Working...';
                    logToConsole(`\n💾 Recreating storage for run #${runId}...`, 'info');
                    socket.emit('recreate_storage', { run_id: runId, hf_token: config.hf_token });
                });
            });

            socket.off('recreate_storage_done');
            socket.on('recreate_storage_done', function(data) {
                var btn = content.querySelector('.recreate-storage-btn[data-run-id="' + data.run_id + '"]');
                if (data.need_storage_class) {
                    var classes = data.available_classes || [];
                    var modal = document.getElementById('storage-class-modal');
                    var sel = document.getElementById('storage-class-select-modal');
                    sel.innerHTML = '';
                    classes.forEach(function(sc) {
                        var opt = document.createElement('option');
                        opt.value = sc; opt.textContent = sc;
                        sel.appendChild(opt);
                    });
                    if (!classes.length) {
                        sel.innerHTML = '<option value="">No storage classes found</option>';
                    }
                    modal.classList.add('active');
                    var runId = data.run_id;
                    var confirmBtn = document.getElementById('storage-class-confirm');
                    var cancelBtn = document.getElementById('storage-class-cancel');
                    var cleanup = function() {
                        modal.classList.remove('active');
                        confirmBtn.replaceWith(confirmBtn.cloneNode(true));
                        cancelBtn.replaceWith(cancelBtn.cloneNode(true));
                    };
                    document.getElementById('storage-class-cancel').addEventListener('click', function() {
                        cleanup();
                        if (btn) { btn.disabled = false; btn.textContent = '💾 Regenerate Storage'; }
                        logToConsole('   ⚠️ Storage class required — cancelled', 'warning');
                    }, { once: true });
                    document.getElementById('storage-class-confirm').addEventListener('click', function() {
                        var chosen = sel.value;
                        cleanup();
                        if (chosen) {
                            logToConsole('   Using storage class: ' + chosen, 'info');
                            socket.emit('recreate_storage', { run_id: runId, hf_token: config.hf_token, storage_class: chosen });
                        } else {
                            if (btn) { btn.disabled = false; btn.textContent = '💾 Regenerate Storage'; }
                            logToConsole('   ⚠️ No storage class selected — cancelled', 'warning');
                        }
                    }, { once: true });
                    return;
                }
                if (btn) { btn.disabled = false; btn.textContent = '💾 Regenerate Storage'; }
                if (data.error) {
                    logToConsole('   ❌ ' + data.error, 'error');
                } else {
                    logToConsole('   ✅ Storage ready — you can now resume this run', 'success');
                }
            });

            // Attach click handlers to edit-notes icons
            content.querySelectorAll('.run-notes-edit').forEach(icon => {
                icon.addEventListener('click', () => {
                    const runId = icon.dataset.runId;
                    const textSpan = content.querySelector(`.run-notes-text[data-run-id="${runId}"]`);
                    const current = textSpan.textContent === '—' ? '' : textSpan.textContent;
                    const input = document.createElement('input');
                    input.type = 'text';
                    input.value = current;
                    input.style.cssText = 'width:100%;padding:2px 6px;border:1px solid #7c3aed;border-radius:4px;font-size:0.9em;outline:none;box-sizing:border-box;';
                    input.placeholder = 'Enter description...';
                    textSpan.replaceWith(input);
                    icon.style.display = 'none';
                    input.focus();
                    const save = () => {
                        const val = input.value.trim();
                        fetch(`/api/runs/${runId}/notes`, {
                            method: 'PUT',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({notes: val})
                        }).then(() => {
                            const newSpan = document.createElement('span');
                            newSpan.className = 'run-notes-text';
                            newSpan.dataset.runId = runId;
                            newSpan.style.cssText = 'vertical-align:middle;';
                            newSpan.title = val;
                            newSpan.innerHTML = val || '<span style="color:#cbd5e1;">—</span>';
                            input.replaceWith(newSpan);
                            icon.style.display = '';
                        });
                    };
                    input.addEventListener('blur', save);
                    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') input.blur(); if (e.key === 'Escape') { input.value = current; input.blur(); } });
                });
            });
        })
        .catch(err => {
            content.innerHTML = `<div class="resume-empty">Failed to load runs: ${err.message}</div>`;
        });
}

function resumeRun(runId, runName) {
    // Block if optimization is already running
    if (isOptimizationRunning()) {
        document.getElementById('resume-overlay').classList.remove('active');
        document.getElementById('running-modal').classList.add('active');
        return;
    }

    // Close overlay
    document.getElementById('resume-overlay').classList.remove('active');

    // Navigate to step 6 (Review & Run)
    goToStep(7);

    logToConsole('\n' + '='.repeat(55), 'info');
    logToConsole(`Resuming Run #${runId}: ${runName}`, 'success');
    logToConsole('Skipping previously completed tests', 'info');
    logToConsole('='.repeat(55), 'info');

    // Show stop button (as if optimization is already running)
    document.getElementById('start-optimization').style.display = 'none';
    document.getElementById('stop-optimization').style.display = 'block';

    // Emit resume_optimization — loads config from DB, no test plan needed
    socket.emit('resume_optimization', {
        run_id: runId,
        hf_token: config.hf_token
    });
}

