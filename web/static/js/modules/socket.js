// socket.js — Socket.IO init, session guard, utility helpers

const tabId = Math.random().toString(36).slice(2, 10);
const socket = io({ query: { tab_id: tabId } });
let initialConnectDone = false;
let _takeoverTimeout = null;

// --- Session guard ---
socket.on('session_locked', function(data) {
    document.getElementById('session-lock-user').textContent = data.username;
    document.getElementById('session-lock-time').textContent = data.connected_at;
    document.getElementById('session-lock-modal').classList.add('active');
});

socket.on('session_kicked', function(data) {
    document.getElementById('session-kicked-by').textContent = data.taken_by;
    document.getElementById('session-kicked-modal').classList.add('active');
});

socket.on('session_granted', function() {
    document.getElementById('session-lock-modal').classList.remove('active');
    // Clear takeover timeout since we got a response
    if (_takeoverTimeout) { clearTimeout(_takeoverTimeout); _takeoverTimeout = null; }
    // Reset button state
    var btn = document.querySelector('#session-lock-modal .modal-footer button:last-child');
    if (btn) { btn.disabled = false; btn.textContent = 'Take Over'; }
    loadConfig();
});

// Heartbeat — prove we're alive every 5 seconds
setInterval(function() { socket.emit('heartbeat'); }, 5000);

function toggleTestConfig(rowId) {
    var row = document.getElementById(rowId);
    if (row) row.style.display = row.style.display === 'none' ? '' : 'none';
}

function sortReportTable(tableId, colIdx, type) {
    var table = document.getElementById(tableId);
    if (!table) return;
    var rows = Array.from(table.querySelectorAll('tr')).slice(1);
    var baselineRows = rows.filter(function(r) { return r.classList.contains('baseline-row'); });
    var dataRows = rows.filter(function(r) { return !r.classList.contains('baseline-row'); });
    var dir = table.getAttribute('data-sort-col') === String(colIdx) && table.getAttribute('data-sort-dir') === 'asc' ? 'desc' : 'asc';
    table.setAttribute('data-sort-col', colIdx);
    table.setAttribute('data-sort-dir', dir);
    dataRows.sort(function(a, b) {
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
    dataRows.forEach(function(r) { tbody.appendChild(r); });
    baselineRows.forEach(function(r) { tbody.appendChild(r); });
}

function takeOverSession() {
    var btn = document.querySelector('#session-lock-modal .modal-footer button:last-child');
    btn.disabled = true;
    btn.innerHTML = '<span style="display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,0.3);border-top-color:white;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:8px;"></span>Taking over...';
    socket.emit('take_over');
    // If no response in 5 seconds, reset button and show error
    _takeoverTimeout = setTimeout(function() {
        btn.disabled = false;
        btn.textContent = 'Take Over';
        // If modal is still showing, the takeover failed
        if (document.getElementById('session-lock-modal').classList.contains('active')) {
            btn.textContent = 'Retry Take Over';
        }
    }, 5000);
}

// Re-load config on reconnect (e.g. after server restart)
// The server's connect handler will either grant access or send session_locked
socket.on('connect', () => {
    if (initialConnectDone) {
        console.log('Reconnected to server...');
        // Server will replay state if we're still active, or send session_locked if not
    }
    initialConnectDone = true;
});

let currentStep = 1;
let config = {
    goal: null,
    model: null,
    isl: 3000,
    osl: 100,
    isl_stdev: null,
    osl_stdev: null,
    turns: 1,
    users: 100,
    rate_type: 'concurrent',
    duration: 300,
    stop_mode: 'duration',
    max_requests: null,
    hf_token: null,
    max_gpus: null,
    use_achievable_qps: false,
    latency_constraint_enabled: false,
    latency_constraint_ms: 500,
    latency_constraint_percentile: 'p90',
    tp_pair_top_n: 2,
    workload_mode: 'synthetic',
    dataset_source: null,
    dataset_column: null,
    dataset_max_output: 256,
    prefix_cache_hit_pct: 0,
    prefix_cache_mode: 'identical',
    prefix_cache_groups: 5,
    pd_search_mode: 'smart',
    run_description: '',
    epp_preset: 'balanced',
    epp_benchmark: false,
    epp_config: null,
    advanced_vllm: null
};

// Track configuration used for last test plan generation
let lastTestPlanConfig = null;

function getTestPlanConfigSignature() {
    // Create a signature of config values that affect test plan generation
    return {
        model: config.model,
        goal: config.goal,
        max_gpus: config.max_gpus || config.cluster_resources?.total_gpus,
        gpu_vram_gb: config.cluster_resources?.gpu_memory_per_gpu_mb / 1024 || 80,
        isl: config.isl,
        osl: config.osl,
        num_users: config.users || 100,
        use_existing_pvc: config.use_existing_pvc || false,
        existing_pvc_name: config.existing_pvc_name || null
    };
}

function configSignaturesMatch(sig1, sig2) {
    if (!sig1 || !sig2) return false;
    return JSON.stringify(sig1) === JSON.stringify(sig2);
}

// Helper function to log to console
