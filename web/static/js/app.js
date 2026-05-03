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
function logToConsole(message, type = 'info') {
    const line = document.createElement('div');
    line.className = `console-line ${type}`;
    line.textContent = message;
    const consoleEl = document.getElementById('console-output');
    consoleEl.appendChild(line);
    consoleEl.scrollTop = consoleEl.scrollHeight;

    // Persist to localStorage
    saveConsoleMessage(message, type);
}

// Save console message to localStorage
function saveConsoleMessage(message, type) {
    const consoleHistory = JSON.parse(localStorage.getItem('inferecipe-console') || '[]');
    consoleHistory.push({ message, type, timestamp: Date.now() });
    localStorage.setItem('inferecipe-console', JSON.stringify(consoleHistory));
}

// Save console log to txt file
function saveConsoleToFile() {
    const consoleHistory = JSON.parse(localStorage.getItem('inferecipe-console') || '[]');

    if (consoleHistory.length === 0) {
        alert('Console is empty, nothing to save.');
        return;
    }

    // Format console history as text
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    let content = `InfeRecipe Console Log\n`;
    content += `Generated: ${new Date().toLocaleString()}\n`;
    content += `Total Messages: ${consoleHistory.length}\n`;
    content += `${'='.repeat(80)}\n\n`;

    consoleHistory.forEach((entry, index) => {
        const date = new Date(entry.timestamp);
        const timeStr = date.toLocaleTimeString();
        const typeLabel = entry.type.toUpperCase().padEnd(8);
        content += `[${timeStr}] [${typeLabel}] ${entry.message}\n`;
    });

    // Create download link
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `inferecipe-console-log-${timestamp}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// Restore console from localStorage
function restoreConsole() {
    const consoleHistory = JSON.parse(localStorage.getItem('inferecipe-console') || '[]');
    const consoleEl = document.getElementById('console-output');

    // Only clear and restore if there's history
    if (consoleHistory.length > 0) {
        // Clear existing content
        consoleEl.innerHTML = '';

        // Restore messages
        consoleHistory.forEach(entry => {
            const line = document.createElement('div');
            line.className = `console-line ${entry.type || 'info'}`;
            line.textContent = entry.message;
            consoleEl.appendChild(line);
        });

        consoleEl.scrollTop = consoleEl.scrollHeight;
    }
}

// Clear console history
function clearConsole() {
    // Clear localStorage
    localStorage.removeItem('inferecipe-console');

    // Clear database via API
    fetch('/api/clear_console', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Clear UI immediately (will be replicated to all clients via socket)
            document.getElementById('console-output').innerHTML = '<div class="console-line">Console cleared.</div>';
        } else {
            console.error('Failed to clear console:', data.error);
        }
    })
    .catch(err => {
        console.error('Error clearing console:', err);
        // Clear UI anyway
        document.getElementById('console-output').innerHTML = '<div class="console-line">Console cleared.</div>';
    });
}

// Save config to server (persisted in database)
function saveConfig() {
    socket.emit('save_config', {
        config: config,
        current_step: currentStep
    });

    // Also keep in localStorage as fallback
    localStorage.setItem('inferecipe-config', JSON.stringify(config));
    localStorage.setItem('inferecipe-step', currentStep.toString());
}

// Update UI from current config state
function updateUIFromConfig() {
    // Restore UI selections
    if (config.goal) {
        document.querySelectorAll('[data-goal]').forEach(card => {
            if (card.dataset.goal === config.goal) {
                card.classList.add('selected');
            }
        });
    }

    if (config.model) {
        const modelCards = document.querySelectorAll('.model-card');
        let found = false;
        modelCards.forEach(card => {
            if (card.dataset.model === config.model) {
                card.classList.add('selected');
                found = true;
            }
        });
        if (!found && document.getElementById('custom-model')) {
            document.getElementById('custom-model').value = config.model;
        }
    }

    if (config.hf_token && document.getElementById('hf-token')) {
        document.getElementById('hf-token').value = config.hf_token;
    }

    if (document.getElementById('isl-input')) {
        document.getElementById('isl-input').value = config.isl;
    }
    if (document.getElementById('osl-input')) {
        document.getElementById('osl-input').value = config.osl;
    }
    if (document.getElementById('users-input')) {
        document.getElementById('users-input').value = config.users;
    }
    if (document.getElementById('duration-input')) {
        document.getElementById('duration-input').value = config.duration;
    }
    if (config.isl_stdev && document.getElementById('isl-stdev-input')) {
        document.getElementById('isl-stdev-input').value = config.isl_stdev;
    }
    if (config.osl_stdev && document.getElementById('osl-stdev-input')) {
        document.getElementById('osl-stdev-input').value = config.osl_stdev;
    }
    if (document.getElementById('multi-turn-enabled')) {
        if (config.turns && config.turns > 1) {
            document.getElementById('multi-turn-enabled').checked = true;
            document.getElementById('turns-input').value = config.turns;
            toggleMultiTurn();
        }
    }
    if (config.max_requests && document.getElementById('max-requests-input')) {
        document.getElementById('max-requests-input').value = config.max_requests;
    }
    if (config.stop_mode && config.stop_mode !== 'duration') {
        setStopMode(config.stop_mode);
    }
    if (config.rate_type && config.rate_type !== 'concurrent') {
        setRateType(config.rate_type);
    }

    if (document.getElementById('use-achievable-qps')) {
        document.getElementById('use-achievable-qps').checked = config.use_achievable_qps || false;
    }

    if (document.getElementById('latency-constraint-enabled')) {
        document.getElementById('latency-constraint-enabled').checked = config.latency_constraint_enabled || false;
        if (config.latency_constraint_enabled) {
            document.getElementById('latency-sla-body').style.display = 'block';
            document.getElementById('latency-sla-arrow').textContent = '▾';
            var li = document.getElementById('latency-sla-inner');
            if (li) { li.style.opacity = '1'; }
        }
    }
    if (config.latency_constraint_ms && document.getElementById('latency-target-input')) {
        document.getElementById('latency-target-input').value = config.latency_constraint_ms;
    }
    if (config.latency_constraint_percentile) {
        setLatencyPercentile(config.latency_constraint_percentile);
    }
    if (config.tp_pair_top_n) {
        setTpPairTopN(config.tp_pair_top_n);
    }

    if (config.pvc_size && document.getElementById('pvc-size-input')) {
        document.getElementById('pvc-size-input').value = config.pvc_size;
    }

    // Restore workload mode
    if (config.workload_mode === 'dataset') {
        document.getElementById('synthetic-workload-panel').style.display = 'none';
        document.getElementById('dataset-workload-panel').style.display = 'block';
        if (config.dataset_source && document.getElementById('dataset-source-input'))
            document.getElementById('dataset-source-input').value = config.dataset_source;
        if (config.dataset_column && document.getElementById('dataset-column-input'))
            document.getElementById('dataset-column-input').value = config.dataset_column;
        if (config.dataset_max_output && document.getElementById('dataset-max-output-input'))
            document.getElementById('dataset-max-output-input').value = config.dataset_max_output;
    }

    // Restore prefix cache slider
    if (config.prefix_cache_hit_pct && document.getElementById('prefix-cache-slider')) {
        document.getElementById('prefix-cache-slider').value = config.prefix_cache_hit_pct;
        document.getElementById('prefix-cache-value').textContent = config.prefix_cache_hit_pct + '%';
    }

    // Restore PD search mode
    if (config.pd_search_mode && document.getElementById('pd-search-smart')) {
        setPdSearchMode(config.pd_search_mode);
    }

    // Restore run description
    if (config.run_description && document.getElementById('run-description-input')) {
        document.getElementById('run-description-input').value = config.run_description;
    }

    // Restore advanced vLLM settings
    restoreAdvVllm();

    // Restore "Use Existing PVC" checkbox and name
    if (config.use_existing_pvc && document.getElementById('use-existing-pvc')) {
        document.getElementById('use-existing-pvc').checked = true;
        document.getElementById('existing-pvc-group').style.display = 'block';
        document.getElementById('storage-class-group').style.display = 'none';
        document.getElementById('pvc-size-group').style.display = 'none';
        document.getElementById('storage-note').style.display = 'none';
        document.getElementById('existing-pvc-note').style.display = 'block';

        // Fetch PVCs to populate dropdown
        if (typeof fetchAvailablePVCs === 'function') {
            fetchAvailablePVCs();
        }
    }

    // Restore cluster resources if available (Step 4)
    if (config.cluster_resources && currentStep >= 4) {
        restoreClusterResources();
    }

    // Restore Configuration Summary (Step 4+)
    if (config.goal && config.model && currentStep >= 4) {
        restoreConfigSummary();
    }

    // Restore Test Plan if it exists
    if (config.test_plan) {
        restoreTestPlan();
    }
}

// Load config from server (with localStorage fallback)
function loadConfig() {
    // First, try to load from server
    socket.emit('load_config');

    // Fallback: also load from localStorage in case server fails
    const saved = localStorage.getItem('inferecipe-config');
    const savedStep = localStorage.getItem('inferecipe-step');

    if (saved) {
        const loadedConfig = JSON.parse(saved);
        config = { ...config, ...loadedConfig };

        if (savedStep) {
            currentStep = parseInt(savedStep);
        }

        // Note: updateUIFromConfig and goToStep will be called
        // when we receive the load_config_result event
    }
}

// Restore Cluster Resources display
function restoreClusterResources() {
    const data = config.cluster_resources;
    if (!data) return;

    // Build network interfaces display
    let nicDisplay = 'None';
    if (data.total_network_interfaces > 0) {
        const nicTypes = [];
        if (data.network_interfaces_by_type) {
            for (const [type, count] of Object.entries(data.network_interfaces_by_type)) {
                nicTypes.push(`${count}× ${type}`);
            }
        }
        const nicVendors = [];
        if (data.network_interfaces_by_vendor) {
            for (const [vendor, count] of Object.entries(data.network_interfaces_by_vendor)) {
                nicVendors.push(`${count}× ${vendor}`);
            }
        }
        nicDisplay = `${data.total_network_interfaces} total`;
        if (nicTypes.length > 0) {
            nicDisplay += `<br>&nbsp;&nbsp;&nbsp;&nbsp;Type: ${nicTypes.join(', ')}`;
        }
        if (nicVendors.length > 0) {
            nicDisplay += `<br>&nbsp;&nbsp;&nbsp;&nbsp;Vendor: ${nicVendors.join(', ')}`;
        }
    }

    // Build GPU display with vendor/model
    let gpuDisplay = `${data.total_gpus}`;
    if (data.gpu_vendor && data.gpu_vendor !== 'unknown') {
        gpuDisplay += ` × ${data.gpu_vendor}`;
        if (data.gpu_model && data.gpu_model !== 'unknown') {
            gpuDisplay += ` ${data.gpu_model}`;
        }
    } else {
        gpuDisplay += ` × ${data.gpu_type}`;
    }
    gpuDisplay += ` (${Math.round(data.gpu_memory_per_gpu_mb / 1024)} GB each)`;

    // Build provider and network information panel (always shown)
    let providerName = 'Unknown';
    let networkName = 'Unknown';

    if (data.provider === 'ibm_cloud') {
        providerName = 'IBM Cloud';
    } else if (data.provider === 'coreweave') {
        providerName = 'CoreWeave';
    } else if (data.provider === 'baremetal') {
        providerName = 'Bare Metal';
    } else if (data.provider === 'aws') {
        providerName = 'AWS';
    } else if (data.provider === 'gcp') {
        providerName = 'GCP';
    } else if (data.provider === 'azure') {
        providerName = 'Azure';
    }

    if (data.network_type === 'dra') {
        networkName = 'DRA (DRANET)';
    } else if (data.network_type === 'nad') {
        networkName = 'NAD (Multus)';
    } else if (data.network_type === 'sriov') {
        networkName = 'SR-IOV';
    }

    // Build notes based on provider + network combination
    let deploymentNotes = '';

    // IBM Cloud with DRANET
    if (data.provider === 'ibm_cloud' && data.dranet_available) {
        deploymentNotes = `
            <li style="list-style-type: none;">✅ No pod-per-node limit - PD can run on single node</li>
            <li style="list-style-type: none;">✅ GPU+NIC PCIe affinity guaranteed</li>
        `;
    }
    // IBM Cloud with NAD only
    else if (data.provider === 'ibm_cloud' && !data.dranet_available) {
        deploymentNotes = `
            <li style="list-style-type: none;">⚠️ With NAD network: Requires minimum 2 nodes for PD/EP workloads</li>
            <li style="list-style-type: none;">⚠️ All nodes must have the same number of GPUs</li>
        `;
    }
    // CoreWeave
    else if (data.provider === 'coreweave') {
        deploymentNotes = `
            <li style="list-style-type: none;">✅ No pod-per-node constraints</li>
            <li style="list-style-type: none;">✅ InfiniBand RDMA via rdma/ib device plugin</li>
            <li style="list-style-type: none;">✅ Vanilla Kubernetes (not OpenShift)</li>
        `;
    }

    let ibmWarning = `
        <div style="padding: 4px 0; text-align: center;">
            <ul style="margin: 5px auto; padding: 0; list-style-position: inside; text-align: center;">
                <li style="list-style-type: none;"><strong>Provider:</strong> ${providerName}</li>
                <li style="list-style-type: none;"><strong>Network:</strong> ${networkName}</li>
                ${deploymentNotes}
            </ul>
        </div>
    `;

    // Parse GPU info
    let gpuModel = '';
    let gpuAmount = data.total_gpus;
    if (data.gpu_vendor && data.gpu_vendor !== 'unknown') {
        gpuModel = data.gpu_vendor;
        if (data.gpu_model && data.gpu_model !== 'unknown') {
            gpuModel += ` ${data.gpu_model}`;
        }
    } else {
        gpuModel = data.gpu_type || '-';
    }
    const gpuMemory = Math.round(data.gpu_memory_per_gpu_mb / 1024);

    // Parse CPU info
    let cpuModel = data.cpu_model && data.cpu_model !== 'unknown' ? data.cpu_model : '-';

    // Parse host info
    let hostModel = data.host_model && data.host_model !== 'unknown' ? data.host_model : '-';

    // Parse NIC info
    let nicType = '-';
    let nicVendor = '-';
    let nicModel = '-';
    if (data.network_interfaces && data.network_interfaces.length > 0) {
        const nicTypes = [...new Set(data.network_interfaces.map(nic => nic.type).filter(t => t && t !== 'unknown'))];
        const nicVendors = [...new Set(data.network_interfaces.map(nic => nic.vendor).filter(v => v && v !== 'unknown'))];
        if (nicTypes.length > 0) nicType = nicTypes.join(', ');
        if (nicVendors.length > 0) nicVendor = nicVendors.join(', ');
    }
    if (data.nic_models && data.nic_models.length > 0) {
        nicModel = data.nic_models.join(', ');
    }

    // Display detected configuration (provider, network, notes)
    const detectedDiv = document.getElementById('detected-config-summary');
    if (detectedDiv) detectedDiv.innerHTML = ibmWarning;

    // Display hardware resources table
    const resourcesDiv = document.getElementById('resources-summary');
    resourcesDiv.innerHTML = `
        <table style="width: 100%; border-collapse: collapse; border-top: 2px solid var(--rh-gray-400); border-bottom: 2px solid var(--rh-gray-400);">
            <thead>
                <tr style="border-bottom: 2px solid var(--rh-gray-400);">
                    <th style="padding: 12px 8px; text-align: center; font-weight: 800; text-transform: uppercase; color: var(--rh-red-primary); font-size: 0.9em;">Type</th>
                    <th style="padding: 12px 8px; text-align: center; font-weight: 800; text-transform: uppercase; color: var(--rh-red-primary); font-size: 0.9em;">Model</th>
                    <th style="padding: 12px 8px; text-align: center; font-weight: 800; text-transform: uppercase; color: var(--rh-red-primary); font-size: 0.9em;">Amount</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">GPU</td>
                    <td style="padding: 10px 8px; text-align: center;">${gpuModel}</td>
                    <td style="padding: 10px 8px; text-align: center;">${gpuAmount} × (${gpuMemory} GB each)</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">GPU Nodes</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.gpu_node_count} (${data.max_gpus_per_node} GPUs per node)</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">Total GPU VRAM</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.total_gpu_memory_gb} GB</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">Host/Instance</td>
                    <td style="padding: 10px 8px; text-align: center;">${hostModel}</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.gpu_node_count} nodes</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">CPU</td>
                    <td style="padding: 10px 8px; text-align: center;">${cpuModel}</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.total_cpu_cores} cores</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">RAM</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.total_memory_gb} GB</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">RDMA/InfiniBand</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.has_rdma ? `Yes (${data.rdma_capable_nodes} nodes)` : 'No'}</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">Network Interfaces</td>
                    <td style="padding: 10px 8px; text-align: center;">${nicModel !== '-' ? nicModel : `${nicType} (${nicVendor})`}</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.total_network_interfaces} total (${Math.round(data.total_network_interfaces / data.gpu_node_count)} per node)</td>
                </tr>
                ${buildNicDetailRows(data)}
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">Max TP</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.max_gpus_per_node}</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">TP Options</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.tp_options.join(', ')}</td>
                </tr>
            </tbody>
        </table>
    `;

    // Populate storage class dropdown
    const storageSelect = document.getElementById('storage-class-select');
    if (storageSelect && data.storage_classes && data.storage_classes.length > 0) {
        storageSelect.innerHTML = '<option value="">-- Select a Storage Class --</option>';
        data.storage_classes.forEach(sc => {
            const option = document.createElement('option');
            option.value = sc.name;
            option.textContent = `${sc.name} (${sc.provisioner})`;
            storageSelect.appendChild(option);
        });
        // Restore selected storage class
        if (config.storage_class) {
            storageSelect.value = config.storage_class;
        }
    } else if (storageSelect && (!data.storage_classes || data.storage_classes.length === 0)) {
        // Stale config has no storage classes — trigger a fresh scan
        logToConsole('🔍 Storage classes missing from saved config, re-scanning...', 'info');
        socket.emit('scan_cluster', {});
    }

    // Populate max GPU dropdown
    const maxGpuSelect = document.getElementById('max-gpu-select');
    if (maxGpuSelect) {
        maxGpuSelect.innerHTML = `<option value="${data.total_gpus}" selected>All (${data.total_gpus} GPUs)</option>`;

        // Add powers of 2 and common GPU counts
        const gpuOptions = [];
        for (let i = 1; i < data.total_gpus; i *= 2) {
            gpuOptions.push(i);
        }
        const commonValues = [4, 8, 16, 32, 64];
        commonValues.forEach(val => {
            if (val < data.total_gpus && !gpuOptions.includes(val)) {
                gpuOptions.push(val);
            }
        });
        gpuOptions.sort((a, b) => a - b);
        gpuOptions.forEach(count => {
            const option = document.createElement('option');
            option.value = count;
            option.textContent = `${count} GPU${count > 1 ? 's' : ''}`;
            maxGpuSelect.appendChild(option);
        });

        // Restore selected max GPUs
        if (config.max_gpus) {
            maxGpuSelect.value = config.max_gpus;
        }

        // Note: Change listener is added in cluster_scan_result handler

        // Display GPU usage information
        const gpuUsageInfo = document.getElementById('gpu-usage-info');
        if (data.gpus_in_use && data.gpus_in_use > 0) {
            gpuUsageInfo.innerHTML = `⚠️ <strong>${data.gpus_in_use} GPU${data.gpus_in_use > 1 ? 's' : ''}</strong> currently in use by other workloads. <strong>${data.gpus_available} GPU${data.gpus_available !== 1 ? 's' : ''}</strong> available.`;
            gpuUsageInfo.style.display = 'block';
        } else {
            gpuUsageInfo.style.display = 'none';
        }
    }

    // Show the sections
    document.getElementById('cluster-resources').style.display = 'block';
    document.getElementById('max-gpu-group').style.display = 'block';

    // Restore node selection checkboxes from saved config
    if (data.nodes_detail && data.nodes_detail.length > 0) {
        let nodeHtml = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;">';
        data.nodes_detail.filter(n => n.gpus > 0).forEach(node => {
            const prevSelected = config.selected_nodes && config.selected_nodes.includes(node.name);
            nodeHtml += '<label style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:#f8fafc;border:2px solid ' + (prevSelected ? '#0ea5e9' : '#e2e8f0') + ';border-radius:8px;cursor:pointer;transition:border-color 0.2s;">' +
                '<input type="checkbox" class="node-select-cb" value="' + node.name + '" data-gpus="' + node.gpus + '"' + (prevSelected ? ' checked' : '') + ' style="width:18px;height:18px;accent-color:#0ea5e9;" onchange="validateNodeSelection();this.closest(\'label\').style.borderColor=this.checked?\'#0ea5e9\':\'#e2e8f0\'">' +
                '<div style="flex:1;min-width:0;">' +
                    '<div style="font-weight:600;color:#1e293b;font-size:0.95em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="' + node.name + '">' + node.name + '</div>' +
                    '<div style="font-size:0.82em;color:#64748b;">' + node.gpus + ' GPUs · ' + (node.gpu_model || 'GPU') + ' · ' + node.memory_gb + 'GB RAM</div>' +
                '</div>' +
            '</label>';
        });
        nodeHtml += '</div>';
        document.getElementById('node-select-list').innerHTML = nodeHtml;
        document.getElementById('node-select-group').style.display = 'block';

        if (config.selected_nodes && config.selected_nodes.length > 0) {
            document.getElementById('enable-node-select').checked = true;
            document.getElementById('node-select-list').style.opacity = '1';
        }
    }

    // Show re-scan button
    document.getElementById('rescan-cluster-btn').style.display = 'inline-block';
}

// Node selection toggle
document.getElementById('enable-node-select').addEventListener('change', function() {
    if (!this.checked) {
        document.getElementById('node-select-warning').style.display = 'none';
        config.selected_nodes = null;
        saveConfig();
    } else {
        validateNodeSelection();
    }
});

// Re-validate node selection when GPU limit changes
document.getElementById('max-gpu-select').addEventListener('change', function() {
    config.max_gpus = parseInt(this.value) || null;
    validateNodeSelection();
});

function validateNodeSelection() {
    const enabled = document.getElementById('enable-node-select').checked;
    const warningEl = document.getElementById('node-select-warning');
    if (!enabled) {
        warningEl.style.display = 'none';
        config.selected_nodes = null;
        return;
    }

    const checked = [...document.querySelectorAll('.node-select-cb:checked')];
    config.selected_nodes = checked.map(cb => cb.value);

    if (checked.length === 0) {
        warningEl.style.display = 'none';
        return;
    }

    const selectedGpus = checked.reduce((sum, cb) => sum + parseInt(cb.dataset.gpus), 0);
    const maxGpus = config.max_gpus || config.cluster_resources?.total_gpus || 0;

    if (selectedGpus < maxGpus) {
        warningEl.innerHTML = '⚠️ Selected nodes have <strong>' + selectedGpus + ' GPUs</strong> total, but GPU limit is <strong>' + maxGpus + '</strong>. Reduce the GPU limit to ≤' + selectedGpus + ' or select more nodes.';
        warningEl.style.display = 'block';
        logToConsole('⚠️ Node selection: ' + checked.length + ' node(s) with ' + selectedGpus + ' GPUs selected, but GPU limit is ' + maxGpus, 'warning');
    } else {
        warningEl.style.display = 'none';
    }
    saveConfig();
}

// Restore Configuration Summary in Step 6
function restoreConfigSummary() {
    const goalNames = {
        'throughput': 'Throughput Priority',
        'ttft': 'Response Time Priority',
        'balanced': 'Balanced Performance'
    };

    const storageClassSelect = document.getElementById('storage-class-select');

    // Update configuration summary spans
    document.getElementById('config-summary-goal').textContent = goalNames[config.goal] || config.goal;
    document.getElementById('config-summary-model').textContent = config.model;
    document.getElementById('config-summary-isl').textContent = config.isl;
    document.getElementById('config-summary-osl').textContent = config.osl;
    document.getElementById('config-summary-users').textContent = config.users;
    document.getElementById('config-summary-duration').textContent = config.stop_mode === 'max_requests' ? `${config.max_requests} requests` : `${config.duration}s duration`;
    document.getElementById('config-summary-qps-mode').textContent = config.use_achievable_qps ? 'Sustainable Load (auto-scaled)' : 'User-defined Concurrent Users';
    document.getElementById('config-summary-gpus').textContent = config.max_gpus || config.cluster_resources?.total_gpus || 'Not set';
    document.getElementById('config-summary-achievable-qps').textContent = config.use_achievable_qps ? 'Enabled' : 'Disabled';
    document.getElementById('config-summary-pvc').textContent = config.existing_pvc_name || 'Not set';
    document.getElementById('config-summary-namespace').textContent = config.namespace || 'Not set';
    const lcEl = document.getElementById('config-summary-latency-constraint');
    if (lcEl) {
        lcEl.textContent = config.latency_constraint_enabled
            ? `${config.latency_constraint_ms}ms @ ${config.latency_constraint_percentile.toUpperCase()}`
            : 'Disabled';
    }
    const tdEl = document.getElementById('config-summary-tp-depth');
    if (tdEl) {
        const labels = {1: '1 (Fast)', 2: '2 (Default)', 3: '3 (Deep)', 4: '4 (Full)'};
        tdEl.textContent = labels[config.tp_pair_top_n] || config.tp_pair_top_n;
    }

    // Update recipe section spans
    document.getElementById('recipe-objective').textContent = goalNames[config.goal] || config.goal;
    document.getElementById('recipe-isl').textContent = config.isl;
    document.getElementById('recipe-isl-stdev').textContent = config.isl_stdev ? ` (σ=${config.isl_stdev})` : '';
    document.getElementById('recipe-osl').textContent = config.osl;
    document.getElementById('recipe-osl-stdev').textContent = config.osl_stdev ? ` (σ=${config.osl_stdev})` : '';
    const turnsEl = document.getElementById('recipe-turns');
    if (turnsEl) turnsEl.textContent = config.turns > 1 ? ` × ${config.turns} turns` : '';
    document.getElementById('recipe-users').textContent = config.users;
    document.getElementById('recipe-duration').textContent = config.stop_mode === 'max_requests' ? `${config.max_requests} requests` : `${config.duration}s duration`;
    document.getElementById('recipe-gpus').textContent = config.max_gpus || config.cluster_resources?.total_gpus || 'Not set';
    document.getElementById('recipe-model').textContent = config.model;
}

// Restore Test Plan UI
function restoreTestPlan() {
    const data = config.test_plan;
    if (!data || !data.can_proceed) return;

}

// Load config and restore console on page load
setTimeout(() => {
    restoreConsole();  // Restore console first
    loadConfig();      // Then load config (without logging extra message)
}, 100);

// Goal selection
document.querySelectorAll('[data-goal]').forEach(card => {
    card.addEventListener('click', function() {
        document.querySelectorAll('[data-goal]').forEach(c => c.classList.remove('selected'));
        this.classList.add('selected');
        config.goal = this.dataset.goal;
        saveConfig();
    });
});

// Load models from API
let allModels = [];
let displayedModels = 0;
const modelsPerPage = 16;

function renderModels(models) {
    const modelList = document.getElementById('model-list');
    models.forEach(model => {
        const card = document.createElement('div');
        card.className = 'model-card';
        card.dataset.model = model.id;
        card.dataset.category = model.category;
        card.innerHTML = `
            <div class="model-name">${model.name}</div>
            <div class="model-desc">${model.description}</div>
        `;
        if (config.model === model.id) {
            card.classList.add('selected');
        }
        card.addEventListener('click', function() {
            document.querySelectorAll('.model-card').forEach(c => c.classList.remove('selected'));
            this.classList.add('selected');
            config.model = this.dataset.model;
            saveConfig();
        });
        modelList.appendChild(card);
    });
    displayedModels += models.length;

    // Show/hide "Load More" button
    const loadMoreBtn = document.getElementById('load-more-models');
    if (displayedModels < allModels.length) {
        loadMoreBtn.style.display = 'inline-block';
    } else {
        loadMoreBtn.style.display = 'none';
    }
}

function loadMoreModels() {
    const nextBatch = allModels.slice(displayedModels, displayedModels + modelsPerPage);
    renderModels(nextBatch);
}

let activeCategory = null;

function buildCategoryFilters() {
    const container = document.getElementById('model-category-filters');
    if (!container || !allModels.length) return;
    const cats = [...new Set(allModels.map(m => m.category))];
    const btnStyle = 'padding:6px 14px; border:2px solid #cbd5e1; border-radius:16px; background:white; color:#475569; font-weight:600; cursor:pointer; font-size:0.82em; font-family:inherit; transition:all 0.2s;';
    const activeStyle = 'padding:6px 14px; border:2px solid var(--rh-red-primary); border-radius:16px; background:var(--rh-red-primary); color:white; font-weight:600; cursor:pointer; font-size:0.82em; font-family:inherit; transition:all 0.2s;';

    container.innerHTML = '';
    const allBtn = document.createElement('button');
    allBtn.textContent = 'All';
    allBtn.style.cssText = activeCategory === null ? activeStyle : btnStyle;
    allBtn.onclick = () => { activeCategory = null; applyModelFilters(); buildCategoryFilters(); };
    container.appendChild(allBtn);

    cats.forEach(cat => {
        const btn = document.createElement('button');
        btn.textContent = cat;
        btn.style.cssText = activeCategory === cat ? activeStyle : btnStyle;
        btn.onclick = () => { activeCategory = cat; applyModelFilters(); buildCategoryFilters(); };
        container.appendChild(btn);
    });
}

function applyModelFilters() {
    const searchTerm = (document.getElementById('model-search').value || '').toLowerCase();
    let filtered = allModels;
    if (activeCategory) {
        filtered = filtered.filter(m => m.category === activeCategory);
    }
    if (searchTerm) {
        filtered = filtered.filter(m =>
            m.name.toLowerCase().includes(searchTerm) ||
            m.id.toLowerCase().includes(searchTerm) ||
            (m.description || '').toLowerCase().includes(searchTerm)
        );
    }
    const modelList = document.getElementById('model-list');
    modelList.innerHTML = '';
    displayedModels = 0;
    renderModels(filtered.slice(0, modelsPerPage));
    if (!searchTerm && !activeCategory && filtered.length > modelsPerPage) {
        document.getElementById('load-more-models').style.display = 'inline-block';
    } else {
        document.getElementById('load-more-models').style.display = 'none';
    }
}

// Fetch models on page load
fetch('/api/models')
    .then(response => response.json())
    .then(models => {
        allModels = models;
        buildCategoryFilters();
        renderModels(models.slice(0, modelsPerPage));
    })
    .catch(error => {
        console.error('Failed to load models:', error);
        document.getElementById('model-list').innerHTML = '<div style="padding: 20px; text-align: center; color: #e53e3e;">Failed to load models. Please refresh the page.</div>';
    });

// Load More button
document.getElementById('load-more-models').addEventListener('click', loadMoreModels);

// Model search
document.getElementById('model-search').addEventListener('input', () => applyModelFilters());

// Custom model
document.getElementById('custom-model').addEventListener('input', (e) => {
    if (e.target.value) {
        document.querySelectorAll('.model-card').forEach(c => c.classList.remove('selected'));
        config.model = e.target.value;
        saveConfig();
    }
});

// HuggingFace token
document.getElementById('hf-token').addEventListener('input', (e) => {
    config.hf_token = e.target.value || null;
    saveConfig();
});

// Number inputs
['isl', 'osl', 'users', 'duration'].forEach(field => {
    document.getElementById(`${field}-input`).addEventListener('change', (e) => {
        config[field] = parseInt(e.target.value);
        saveConfig();
    });
});

// ISL/OSL stdev inputs
document.getElementById('isl-stdev-input').addEventListener('change', (e) => {
    config.isl_stdev = e.target.value ? parseInt(e.target.value) : null;
    saveConfig();
});
document.getElementById('osl-stdev-input').addEventListener('change', (e) => {
    config.osl_stdev = e.target.value ? parseInt(e.target.value) : null;
    saveConfig();
});

// Dataset input listeners
document.getElementById('dataset-source-input').addEventListener('change', (e) => {
    config.dataset_source = e.target.value || null;
    saveConfig();
});
document.getElementById('dataset-column-input').addEventListener('change', (e) => {
    config.dataset_column = e.target.value || null;
    saveConfig();
});
document.getElementById('dataset-max-output-input').addEventListener('change', (e) => {
    config.dataset_max_output = parseInt(e.target.value) || 256;
    saveConfig();
});

// Workload mode toggle
function toggleWorkloadMode() {
    var synPanel = document.getElementById('synthetic-workload-panel');
    var dsPanel = document.getElementById('dataset-workload-panel');
    var isDataset = synPanel.style.display !== 'none';
    var hiding = isDataset ? synPanel : dsPanel;
    var showing = isDataset ? dsPanel : synPanel;

    hiding.classList.remove('flipping-in');
    hiding.classList.add('flipping-out');

    setTimeout(function() {
        hiding.style.display = 'none';
        hiding.classList.remove('flipping-out');
        showing.style.display = 'block';
        showing.classList.add('flipping-in');
        setTimeout(function() { showing.classList.remove('flipping-in'); }, 350);
    }, 350);

    config.workload_mode = isDataset ? 'dataset' : 'synthetic';
    saveConfig();
}

function handleDatasetUpload(input) {
    if (!input.files || !input.files[0]) return;
    var file = input.files[0];
    var statusEl = document.getElementById('dataset-upload-status');
    statusEl.textContent = 'Uploading ' + file.name + '...';
    statusEl.style.display = 'block';

    var formData = new FormData();
    formData.append('file', file);
    fetch('/api/upload-dataset', { method: 'POST', body: formData })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                statusEl.textContent = '✅ ' + file.name + ' uploaded';
                statusEl.style.color = '#059669';
                document.getElementById('dataset-source-input').value = data.path;
            } else {
                statusEl.textContent = '❌ ' + (data.error || 'Upload failed');
                statusEl.style.color = '#dc2626';
            }
        })
        .catch(function(err) {
            statusEl.textContent = '❌ ' + err.message;
            statusEl.style.color = '#dc2626';
        });
}

function toggleMultiTurn() {
    const enabled = document.getElementById('multi-turn-enabled').checked;
    if (enabled) {
        config.turns = Math.max(2, parseInt(document.getElementById('turns-input').value) || 3);
    } else {
        config.turns = 1;
    }
    saveConfig();
}
const turnsInput = document.getElementById('turns-input');
if (turnsInput) {
    turnsInput.addEventListener('change', (e) => {
        config.turns = Math.max(2, parseInt(e.target.value) || 3);
        saveConfig();
    });
}

// Max requests input
document.getElementById('max-requests-input').addEventListener('change', (e) => {
    config.max_requests = parseInt(e.target.value);
    saveConfig();
});

// Stop mode toggle (duration vs max_requests)
function setStopMode(mode) {
    config.stop_mode = mode;
    const durBtn = document.getElementById('stop-mode-duration');
    const reqBtn = document.getElementById('stop-mode-requests');
    const durWrap = document.getElementById('duration-wrapper');
    const reqWrap = document.getElementById('max-requests-wrapper');
    if (mode === 'duration') {
        durBtn.style.background = 'var(--rh-red-primary)';
        durBtn.style.color = 'white';
        durBtn.style.borderColor = 'var(--rh-red-primary)';
        reqBtn.style.background = 'white';
        reqBtn.style.color = '#475569';
        reqBtn.style.borderColor = '#cbd5e1';
        durWrap.style.display = 'block';
        reqWrap.style.display = 'none';
    } else {
        reqBtn.style.background = 'var(--rh-red-primary)';
        reqBtn.style.color = 'white';
        reqBtn.style.borderColor = 'var(--rh-red-primary)';
        durBtn.style.background = 'white';
        durBtn.style.color = '#475569';
        durBtn.style.borderColor = '#cbd5e1';
        durWrap.style.display = 'none';
        reqWrap.style.display = 'block';
    }
    saveConfig();
}

function setRateType(type) {
    config.rate_type = type;
    ['concurrent', 'constant', 'poisson'].forEach(t => {
        const btn = document.getElementById('rate-type-' + t);
        if (!btn) return;
        if (t === type) {
            btn.style.background = 'var(--rh-red-primary)';
            btn.style.color = 'white';
            btn.style.borderColor = 'var(--rh-red-primary)';
        } else {
            btn.style.background = 'white';
            btn.style.color = '#475569';
            btn.style.borderColor = '#cbd5e1';
        }
    });
    const label = document.getElementById('users-label');
    const help = document.getElementById('users-help');
    if (type === 'concurrent') {
        if (label) label.textContent = 'Concurrent Users';
        if (help) help.textContent = 'How many users are sending requests at the same time. Think of it like how many people are in line at once.';
    } else if (type === 'poisson') {
        if (label) label.textContent = 'Requests per Second (avg)';
        if (help) help.textContent = 'Average requests per second with random variation, simulating realistic bursty traffic patterns.';
    } else {
        if (label) label.textContent = 'Requests per Second';
        if (help) help.textContent = 'Sends exactly this many requests per second, regardless of how fast the server responds.';
    }
    saveConfig();
}

// Achievable QPS checkbox
document.getElementById('use-achievable-qps').addEventListener('change', (e) => {
    config.use_achievable_qps = e.target.checked;
    saveConfig();
});

// Latency constraint
function toggleLatencyConstraint() {
    const enabled = document.getElementById('latency-constraint-enabled').checked;
    config.latency_constraint_enabled = enabled;
    saveConfig();
}

function setLatencyPercentile(pctl) {
    config.latency_constraint_percentile = pctl;
    ['p50', 'p90', 'p95', 'p99'].forEach(p => {
        const btn = document.getElementById('pctl-' + p);
        if (p === pctl) {
            btn.style.background = 'var(--rh-red-primary)';
            btn.style.color = 'white';
            btn.style.borderColor = 'var(--rh-red-primary)';
        } else {
            btn.style.background = '#FAFAFA';
            btn.style.color = '#475569';
            btn.style.borderColor = '#cbd5e1';
        }
    });
    saveConfig();
}

// Advanced vLLM settings
var advValueFields = ['max-model-len','gpu-memory-utilization','max-num-seqs','max-num-batched-tokens','dtype','kv-cache-dtype','pipeline-parallel-size','block-size','tool-call-parser'];
var advToggleFields = ['enable-prefix-caching','disable-custom-all-reduce','enable-auto-tool-choice','trust-remote-code','disable-log-requests','vllm-debug-logs','nccl-debug-logs'];

function updateAdvVllm() {
    var adv = {};
    advValueFields.forEach(function(f) {
        var modeEl = document.getElementById('adv-' + f + '-mode');
        var valEl = document.getElementById('adv-' + f + '-val');
        if (!modeEl) return;
        var mode = modeEl.value;
        valEl.disabled = (mode === 'auto');
        if (mode === 'auto') valEl.value = '';
        var key = f.replace(/-/g, '_');
        adv[key] = { mode: mode, value: mode === 'custom' ? (valEl.value || null) : null };
    });
    advToggleFields.forEach(function(f) {
        var modeEl = document.getElementById('adv-' + f + '-mode');
        if (!modeEl) return;
        var key = f.replace(/-/g, '_');
        adv[key] = { mode: modeEl.value };
    });
    config.advanced_vllm = adv;
    saveConfig();
}

function restoreAdvVllm() {
    var adv = config.advanced_vllm;
    if (!adv) return;
    advValueFields.forEach(function(f) {
        var key = f.replace(/-/g, '_');
        var setting = adv[key];
        if (!setting) return;
        var modeEl = document.getElementById('adv-' + f + '-mode');
        var valEl = document.getElementById('adv-' + f + '-val');
        if (modeEl) modeEl.value = setting.mode || 'auto';
        if (valEl) {
            valEl.disabled = (setting.mode !== 'custom');
            if (setting.mode === 'custom' && setting.value != null) valEl.value = setting.value;
        }
    });
    advToggleFields.forEach(function(f) {
        var key = f.replace(/-/g, '_');
        var setting = adv[key];
        if (!setting) return;
        var modeEl = document.getElementById('adv-' + f + '-mode');
        if (modeEl) modeEl.value = setting.mode || 'auto';
    });
}

function resetAdvVllm() {
    advValueFields.forEach(function(f) {
        var modeEl = document.getElementById('adv-' + f + '-mode');
        var valEl = document.getElementById('adv-' + f + '-val');
        if (modeEl) modeEl.value = 'auto';
        if (valEl) { valEl.disabled = true; valEl.value = ''; }
    });
    advToggleFields.forEach(function(f) {
        var modeEl = document.getElementById('adv-' + f + '-mode');
        if (modeEl) modeEl.value = 'auto';
    });
    config.advanced_vllm = null;
    saveConfig();
}

document.getElementById('latency-target-input').addEventListener('change', (e) => {
    config.latency_constraint_ms = parseInt(e.target.value) || 500;
    saveConfig();
});

function setTpPairTopN(n) {
    config.tp_pair_top_n = n;
    [1, 2, 3, 4].forEach(v => {
        const btn = document.getElementById('tp-depth-' + v);
        if (v === n) {
            btn.style.background = 'var(--rh-red-primary)';
            btn.style.color = 'white';
            btn.style.borderColor = 'var(--rh-red-primary)';
        } else {
            btn.style.background = '#FAFAFA';
            btn.style.color = '#475569';
            btn.style.borderColor = '#cbd5e1';
        }
    });
    saveConfig();
}

function setPdSearchMode(mode) {
    config.pd_search_mode = mode;
    ['smart', 'exhaustive'].forEach(m => {
        const btn = document.getElementById('pd-search-' + m);
        if (!btn) return;
        if (m === mode) {
            btn.style.background = 'var(--rh-red-primary)';
            btn.style.color = 'white';
            btn.style.borderColor = 'var(--rh-red-primary)';
        } else {
            btn.style.background = '#FAFAFA';
            btn.style.color = '#475569';
            btn.style.borderColor = '#cbd5e1';
        }
    });
    saveConfig();
}

function setEppPreset(preset) {
    config.epp_preset = preset;
    document.querySelectorAll('[data-epp]').forEach(el => {
        if (el.dataset.epp === preset) {
            el.style.borderColor = 'var(--rh-red-primary)';
            el.style.background = 'linear-gradient(135deg,#fef2f2,#fee2e2)';
        } else {
            el.style.borderColor = '#cbd5e1';
            el.style.background = '#fafafa';
        }
    });
    var editor = document.getElementById('epp-custom-editor');
    if (editor) editor.style.display = preset === 'custom' ? 'block' : 'none';
    saveConfig();
}

function updateEppCustom() {
    config.epp_config = {
        prefix_cache: { enabled: document.getElementById('epp-plugin-prefix-cache').checked, weight: parseFloat(document.getElementById('epp-weight-prefix-cache').value) },
        kv_cache: { enabled: document.getElementById('epp-plugin-kv-cache').checked, weight: parseFloat(document.getElementById('epp-weight-kv-cache').value) },
        queue: { enabled: document.getElementById('epp-plugin-queue').checked, weight: parseFloat(document.getElementById('epp-weight-queue').value) },
        slo: { enabled: document.getElementById('epp-plugin-slo').checked, weight: parseFloat(document.getElementById('epp-weight-slo').value) },
    };
    saveConfig();
}

function resetEppParams() {
    ['epp-max-prefix-blocks', 'epp-lru-capacity', 'epp-non-cached-tokens'].forEach(id => {
        var el = document.getElementById(id);
        if (el) delete el.dataset.userEdited;
    });
    updateEppAutoSuggestion();
    updateEppParams();
}

function updateEppParams() {
    if (!config.epp_config) config.epp_config = {};
    config.epp_config.maxPrefixBlocksToMatch = parseInt(document.getElementById('epp-max-prefix-blocks').value) || 256;
    config.epp_config.lruCapacityPerServer = parseInt(document.getElementById('epp-lru-capacity').value) || 31250;
    config.epp_config.nonCachedTokens = parseInt(document.getElementById('epp-non-cached-tokens').value) || 16;
    saveConfig();
}

function updateEppAutoSuggestion() {
    var suggestion = document.getElementById('epp-auto-suggestion');
    var text = document.getElementById('epp-suggestion-text');
    if (!suggestion || !text) return;
    if (config.prefix_cache_hit_pct > 0 && config.epp_preset !== 'cache_optimized') {
        suggestion.style.display = 'block';
        text.textContent = 'You have prefix cache enabled (' + config.prefix_cache_hit_pct + '%). Consider "Cache Optimized" for better cache-aware routing.';
    } else if (config.latency_constraint_enabled && config.epp_preset !== 'latency_aware') {
        suggestion.style.display = 'block';
        text.textContent = 'You have a latency SLA enabled. Consider "Latency Aware" for SLO-based routing.';
    } else {
        suggestion.style.display = 'none';
    }
    // Auto-calculate parameters from workload settings
    var isl = config.isl || 3000;
    var osl = config.osl || 100;
    var seqLen = isl + osl;
    var blockSize = Math.pow(2, Math.ceil(Math.log2(Math.max(1, Math.sqrt(seqLen)))));
    blockSize = Math.max(8, Math.min(512, blockSize));
    if (['ttft','balanced','pd_only'].includes(config.goal)) blockSize = Math.max(128, blockSize);

    var autoMaxPrefixBlocks = Math.ceil(isl / blockSize);
    var autoNonCachedTokens = Math.min(16, Math.max(1, Math.floor(isl / 100)));

    // Estimate LRU capacity: (GPU_VRAM_MB - model_size_est) / (block_size * 2KB per block)
    var gpuVramMb = 80 * 1024; // default 80GB
    if (config.cluster_resources && config.cluster_resources.gpu_memory_per_gpu_mb) {
        gpuVramMb = config.cluster_resources.gpu_memory_per_gpu_mb;
    }
    var autoLruCapacity = Math.max(1000, Math.floor((gpuVramMb * 0.5) / (blockSize * 2)));

    var el;
    el = document.getElementById('epp-max-prefix-blocks');
    if (el && !el.dataset.userEdited) el.value = autoMaxPrefixBlocks;
    el = document.getElementById('epp-lru-capacity');
    if (el && !el.dataset.userEdited) el.value = autoLruCapacity;
    el = document.getElementById('epp-non-cached-tokens');
    if (el && !el.dataset.userEdited) el.value = autoNonCachedTokens;
}

// Step navigation
document.getElementById('next-step1').addEventListener('click', () => {
    if (!config.goal) {
        logToConsole('❌ Please select an optimization goal', 'error');
        return;
    }
    const goalNames = {
        'throughput': 'Throughput Priority',
        'ttft': 'Response Time Priority',
        'balanced': 'Balanced Performance'
    };
    logToConsole(`\n📋 Step 1 Complete: ${goalNames[config.goal]}`, 'success');
    goToStep(2);
});

document.getElementById('next-step2').addEventListener('click', () => {
    if (!config.model) {
        logToConsole('❌ Please select a model', 'error');
        return;
    }
    logToConsole(`\n📋 Step 2 Complete: Model = ${config.model}`, 'success');
    if (config.hf_token) {
        logToConsole(`   HuggingFace token provided`, 'info');
    }
    goToStep(3);
});

document.getElementById('next-step3').addEventListener('click', () => {
    if (config.workload_mode === 'dataset') {
        if (!config.dataset_source) {
            logToConsole('❌ Please enter a dataset source (HuggingFace ID or file path)', 'error');
            return;
        }
        logToConsole(`\n📋 Step 3 Complete: Dataset = ${config.dataset_source} / Max Output: ${config.dataset_max_output}`, 'success');
    } else {
        logToConsole(`\n📋 Step 3 Complete: Workload = ISL:${config.isl} / OSL:${config.osl}`, 'success');
    }
    goToStep(4);
});

document.getElementById('next-step4').addEventListener('click', () => {
    const qpsMode = config.use_achievable_qps ? 'Sustainable Load (auto-scaled)' : 'User-defined Concurrent Users';
    const stopInfo = config.stop_mode === 'max_requests' ? `${config.max_requests} requests` : `${config.duration}s`;
    logToConsole(`\n📋 Step 4 Complete: Test Config = ${config.users} users, ${stopInfo}, Mode: ${qpsMode}`, 'success');
    goToStep(5);
});

// Step 5: EPP Config
document.getElementById('next-step5').addEventListener('click', () => {
    logToConsole(`\n📋 Step 5 Complete: EPP Config = ${config.epp_preset || 'balanced'}`, 'success');
    goToStep(6);
});

// Save storage class selection
document.getElementById('storage-class-select').addEventListener('change', (e) => {
    config.storage_class = e.target.value;
    saveConfig();
});

// Save PVC size selection
document.getElementById('pvc-size-input').addEventListener('change', (e) => {
    config.pvc_size = parseInt(e.target.value);
    saveConfig();
});

// Handle "Use Existing PVC" checkbox
document.getElementById('use-existing-pvc').addEventListener('change', (e) => {
    const useExisting = e.target.checked;
    config.use_existing_pvc = useExisting;
    saveConfig();

    // Toggle visibility of fields
    document.getElementById('existing-pvc-group').style.display = useExisting ? 'block' : 'none';
    document.getElementById('storage-class-group').style.display = useExisting ? 'none' : 'block';
    document.getElementById('pvc-size-group').style.display = useExisting ? 'none' : 'block';
    document.getElementById('storage-note').style.display = useExisting ? 'none' : 'block';
    document.getElementById('existing-pvc-note').style.display = useExisting ? 'block' : 'none';

    // Fetch available PVCs when checkbox is enabled
    if (useExisting) {
        // Reset flag and fetch (user explicitly toggled checkbox)
        pvcsFetched = false;
        fetchAvailablePVCs();
    }
});

// Save existing PVC name
document.getElementById('existing-pvc-select').addEventListener('change', (e) => {
    config.existing_pvc_name = e.target.value;
    saveConfig();
});

// Track if PVCs have been fetched to avoid duplicate requests
let pvcsFetched = false;

// Fetch available PVCs from cluster
function fetchAvailablePVCs() {
    // Skip if already fetched
    if (pvcsFetched) {
        return;
    }
    pvcsFetched = true;
    socket.emit('list_pvcs', {});
}

document.getElementById('next-step6').addEventListener('click', () => {
    // Validate storage setup
    const useExistingPvc = document.getElementById('use-existing-pvc').checked;
    const maxGpus = config.max_gpus || config.cluster_resources?.total_gpus;

    if (useExistingPvc) {
        // Validate existing PVC is selected
        const existingPvcName = document.getElementById('existing-pvc-select').value;
        if (!existingPvcName) {
            logToConsole('❌ Please select an existing PVC', 'error');
            return;
        }
        config.existing_pvc_name = existingPvcName;
    } else {
        // Validate storage class is selected for new PVC
        const storageClass = document.getElementById('storage-class-select').value;
        if (!storageClass) {
            logToConsole('❌ Please select a storage class', 'error');
            return;
        }
        config.storage_class = storageClass;
    }

    if (!maxGpus) {
        logToConsole('❌ Please scan cluster resources first', 'error');
        return;
    }

    // Validate node selection
    if (config.selected_nodes && config.selected_nodes.length > 0) {
        const checked = [...document.querySelectorAll('.node-select-cb:checked')];
        const selectedGpus = checked.reduce((sum, cb) => sum + parseInt(cb.dataset.gpus), 0);
        if (selectedGpus < maxGpus) {
            logToConsole('❌ Selected nodes only have ' + selectedGpus + ' GPUs but GPU limit is ' + maxGpus + '. Select more nodes or reduce the GPU limit.', 'error');
            return;
        }
    }

    saveConfig();
    logToConsole(`\n📋 Step 5 Complete: Resources configured`, 'success');
    if (config.selected_nodes && config.selected_nodes.length > 0) {
        logToConsole('   Node pinning: ' + config.selected_nodes.join(', '), 'info');
    }

    // Just go to step 6 - test plan will be generated there if needed
    goToStep(7);
});

function generateTestPlan() {
    // Disable start button and show preparing state
    const startButton = document.getElementById('start-optimization');
    startButton.disabled = true;
    startButton.textContent = '🚀 Start Optimization (Preparing...)';
    startButton.style.opacity = '0.6';

    // Build configuration summary
    const goalNames = {
        'throughput': 'Throughput Priority',
        'ttft': 'Response Time Priority',
        'balanced': 'Balanced Performance'
    };

    // Update configuration summary spans
    document.getElementById('config-summary-goal').textContent = goalNames[config.goal] || config.goal;
    document.getElementById('config-summary-model').textContent = config.model;
    document.getElementById('config-summary-isl').textContent = config.isl;
    document.getElementById('config-summary-osl').textContent = config.osl;
    document.getElementById('config-summary-users').textContent = config.users;
    document.getElementById('config-summary-duration').textContent = config.stop_mode === 'max_requests' ? `${config.max_requests} requests` : `${config.duration}s duration`;
    document.getElementById('config-summary-qps-mode').textContent = config.use_achievable_qps ? 'Sustainable Load (auto-scaled)' : 'User-defined Concurrent Users';
    document.getElementById('config-summary-gpus').textContent = config.max_gpus || config.cluster_resources?.total_gpus;
    document.getElementById('config-summary-achievable-qps').textContent = config.use_achievable_qps ? 'Enabled' : 'Disabled';
    document.getElementById('config-summary-pvc').textContent = config.existing_pvc_name || 'Not set';
    document.getElementById('config-summary-namespace').textContent = config.namespace || 'Not set';
    const tdEl2 = document.getElementById('config-summary-tp-depth');
    if (tdEl2) {
        const labels = {1: '1 (Fast)', 2: '2 (Default)', 3: '3 (Deep)', 4: '4 (Full)'};
        tdEl2.textContent = labels[config.tp_pair_top_n] || config.tp_pair_top_n;
    }

    // Update recipe section spans
    document.getElementById('recipe-objective').textContent = goalNames[config.goal] || config.goal;
    document.getElementById('recipe-isl').textContent = config.isl;
    document.getElementById('recipe-isl-stdev').textContent = config.isl_stdev ? ` (σ=${config.isl_stdev})` : '';
    document.getElementById('recipe-osl').textContent = config.osl;
    document.getElementById('recipe-osl-stdev').textContent = config.osl_stdev ? ` (σ=${config.osl_stdev})` : '';
    const turnsEl = document.getElementById('recipe-turns');
    if (turnsEl) turnsEl.textContent = config.turns > 1 ? ` × ${config.turns} turns` : '';
    document.getElementById('recipe-users').textContent = config.users;
    document.getElementById('recipe-duration').textContent = config.stop_mode === 'max_requests' ? `${config.max_requests} requests` : `${config.duration}s duration`;
    document.getElementById('recipe-gpus').textContent = config.max_gpus || config.cluster_resources?.total_gpus;
    document.getElementById('recipe-model').textContent = config.model;

    // Store config signature before generating
    lastTestPlanConfig = getTestPlanConfigSignature();

    // Request test plan from server
    socket.emit('generate_test_plan', {
        model: config.model,
        optimization_goal: config.goal,
        max_gpus: config.max_gpus || config.cluster_resources?.total_gpus,
        gpu_vram_gb: config.cluster_resources?.gpu_memory_per_gpu_mb / 1024 || 80,
        isl: config.isl,
        osl: config.osl,
        num_users: config.users || 100,
        use_existing_pvc: config.use_existing_pvc || false,
        existing_pvc_name: config.existing_pvc_name || null,
        hf_token: config.hf_token || null
    });
}

document.getElementById('start-optimization').addEventListener('click', () => {
    // Toggle buttons immediately - lock start, show stop
    document.getElementById('start-optimization').style.display = 'none';
    document.getElementById('stop-optimization').style.display = 'block';

    // Log start of optimization
    logToConsole('\n' + '='.repeat(55), 'info');
    logToConsole('Starting Optimization Process', 'success');
    logToConsole('='.repeat(55), 'info');

    const useExistingPvc = document.getElementById('use-existing-pvc').checked;
    const existingPvcName = useExistingPvc ? document.getElementById('existing-pvc-select').value : null;
    const storageClass = document.getElementById('storage-class-select').value;
    const pvcSize = parseInt(document.getElementById('pvc-size-input').value);

    if (useExistingPvc) {
        logToConsole('\n📦 Using existing PVC...', 'info');
    } else {
        logToConsole('\n📦 Setting up storage and downloading model...', 'info');
    }

    socket.emit('setup_storage', {
        existing_pvc: existingPvcName,
        storage_class: storageClass,
        pvc_size: pvcSize,
        model: config.model,
        hf_token: config.hf_token,
        isl: config.isl,
        osl: config.osl,
        isl_stdev: config.isl_stdev || null,
        osl_stdev: config.osl_stdev || null,
        turns: config.turns || 1,
        num_users: config.users || 100,
        optimization_goal: config.goal || 'ttft',
        duration: config.duration || 300,
        stop_mode: config.stop_mode || 'duration',
        max_requests: config.max_requests || null,
        max_gpus: config.max_gpus || config.cluster_resources?.total_gpus || 16,
        use_achievable_qps: config.use_achievable_qps || false,
        latency_constraint_enabled: config.latency_constraint_enabled || false,
        latency_constraint_ms: config.latency_constraint_ms || 500,
        latency_constraint_percentile: config.latency_constraint_percentile || 'p90',
        tp_pair_top_n: config.tp_pair_top_n || 2,
        pd_search_mode: config.pd_search_mode || 'smart',
        selected_nodes: config.selected_nodes || [],
        workload_mode: config.workload_mode || 'synthetic',
        dataset_source: config.dataset_source || null,
        dataset_column: config.dataset_column || null,
        dataset_max_output: config.dataset_max_output || 256,
        rate_type: config.rate_type || 'concurrent',
        prefix_cache_hit_pct: config.prefix_cache_hit_pct || 0,
        run_description: config.run_description || '',
        epp_preset: config.epp_preset || 'balanced',
        epp_benchmark: config.epp_benchmark || false,
        epp_config: config.epp_config || null,
        advanced_vllm: config.advanced_vllm || null
    });
});

// Handle stop optimization button
document.getElementById('stop-optimization').addEventListener('click', () => {
    // Show custom modal
    document.getElementById('stop-modal').classList.add('active');
});

// Stop modal - Cancel button
document.getElementById('stop-cancel').addEventListener('click', () => {
    document.getElementById('stop-modal').classList.remove('active');
});

// Stop modal - Confirm button
document.getElementById('stop-confirm').addEventListener('click', () => {
    // Hide modal
    document.getElementById('stop-modal').classList.remove('active');

    // Execute stop
    logToConsole('\n═══════════════════════════════════════', 'warning');
    logToConsole('🛑 Stopping Optimization Process', 'warning');
    logToConsole('═══════════════════════════════════════', 'warning');

    fetch('/api/stop_optimization', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            logToConsole('✅ Optimization stopped', 'success');
        } else {
            logToConsole(`❌ Failed to stop: ${data.error}`, 'error');
        }
    })
    .catch(err => {
        logToConsole(`❌ Error stopping optimization: ${err}`, 'error');
    });
});

// Close modal when clicking outside
document.getElementById('stop-modal').addEventListener('click', (e) => {
    if (e.target.id === 'stop-modal') {
        document.getElementById('stop-modal').classList.remove('active');
    }
});

function buildNicDetailRows(data) {
    if (!data.nodes_detail || data.nodes_detail.length === 0) return '';

    let rows = '';
    for (const node of data.nodes_detail) {
        if (!node.nics || node.nics.length === 0) continue;
        const shortName = node.name.length > 30 ? '...' + node.name.slice(-27) : node.name;
        // Group NICs by name (e.g., rdma/ib) and show count per node
        const nicGroups = {};
        for (const nic of node.nics) {
            const key = nic.name;
            if (!nicGroups[key]) {
                nicGroups[key] = { ...nic, count: 0 };
            }
            nicGroups[key].count += nic.count || 1;
        }
        for (const [name, nic] of Object.entries(nicGroups)) {
            const speed = nic.speed_gbps > 0 ? `${nic.speed_gbps} Gbps` : '';
            const nicInfo = nic.vendor !== 'unknown' ? `${nic.vendor} ${nic.model !== 'unknown' ? nic.model : ''}`.trim() : nic.type;
            const countStr = nic.count > 1 ? `${nic.count}× ` : '';
            rows += `<tr style="background: #f9fafb; font-size: 13px;">
                <td style="padding: 6px 8px; text-align: center; color: #6b7280;">&nbsp;&nbsp;${name}</td>
                <td style="padding: 6px 8px; text-align: center; color: #6b7280;">${countStr}${nicInfo} (${nic.type})</td>
                <td style="padding: 6px 8px; text-align: center; color: #6b7280;">${shortName} - ${speed}</td>
            </tr>`;
        }
    }
    return rows;
}

function rescanCluster() {
    // Hide resources and show scanning status
    document.getElementById('cluster-resources').style.display = 'none';
    document.getElementById('scanning-status').style.display = 'block';
    document.getElementById('rescan-cluster-btn').style.display = 'none';

    // Disable Continue button during scan
    const nextStep5Btn = document.getElementById('next-step6');
    nextStep5Btn.disabled = true;
    nextStep5Btn.textContent = 'Continue to Review → (Scanning...)';
    nextStep5Btn.style.opacity = '0.6';

    logToConsole('🔍 Re-scanning cluster resources...', 'info');
    socket.emit('scan_cluster', {});
}

function isOptimizationRunning() {
    const stopBtn = document.getElementById('stop-optimization');
    return stopBtn && stopBtn.style.display !== 'none';
}

function goToStep(step, skipSave) {
    // Block navigation away from step 6 while optimization is running
    if (step !== 7 && isOptimizationRunning()) {
        document.getElementById('running-modal').classList.add('active');
        return;
    }

    // Hide all sections
    for (let i = 1; i <= 7; i++) {
        const section = document.getElementById(`step${i}-section`);
        const indicator = document.getElementById(`step${i}-indicator`);

        section.style.display = 'none';
        indicator.classList.remove('active');

        if (i < step) {
            indicator.classList.add('completed');
        } else {
            indicator.classList.remove('completed');
        }
    }

    // Show current section
    document.getElementById(`step${step}-section`).style.display = 'block';
    document.getElementById(`step${step}-indicator`).classList.add('active');
    currentStep = step;
    if (!skipSave) saveConfig();

    // Update breadcrumb
    const stepTitles = {1:'Goal', 2:'Model', 3:'Workload', 4:'Test Config', 5:'EPP Config', 6:'Setup', 7:'Review & Run'};
    const bc = document.getElementById('breadcrumb-title');
    if (bc) bc.textContent = `Step ${step}: ${stepTitles[step] || ''}`;

    // Close sidebar on mobile after navigation
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebar) sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('active');

    // Update sublabels
    if (config.goal) {
        document.getElementById('step1-value').textContent = config.goal.toUpperCase();
    }
    if (config.model) {
        const modelName = config.model.split('/').pop().substring(0, 15);
        document.getElementById('step2-value').textContent = modelName;
    }
    if (step > 2) {
        document.getElementById('step3-value').textContent = `${config.isl}/${config.osl}`;
    }
    if (step > 3) {
        document.getElementById('step4-value').textContent = `${config.users} users`;
    }
    if (step > 4) {
        const presetLabels = {balanced:'Balanced', cache_optimized:'Cache Opt.', queue_balanced:'Queue Bal.', latency_aware:'Latency', custom:'Custom'};
        document.getElementById('step5-value').textContent = presetLabels[config.epp_preset] || 'Balanced';
    }
    if (step > 5 && config.cluster_resources) {
        document.getElementById('step6-value').textContent = `${config.cluster_resources.total_gpus} GPUs`;
    }
    if (step === 5) {
        updateEppAutoSuggestion();
        if (config.epp_preset) setEppPreset(config.epp_preset);
    }
    if (step === 6) {
        // Check if cluster resources already scanned
        if (config.cluster_resources && document.getElementById('cluster-resources').style.display !== 'none') {
            // Resources already loaded - enable button immediately
            const nextStep5Btn = document.getElementById('next-step6');
            nextStep5Btn.disabled = false;
            nextStep5Btn.textContent = 'Continue to Review →';
            nextStep5Btn.style.opacity = '1';
        } else {
            // Auto-scan cluster resources when entering step 5
            // Disable next button and show scanning status
            const nextStep5Btn = document.getElementById('next-step6');
            nextStep5Btn.disabled = true;
            nextStep5Btn.textContent = 'Continue to Review → (Scanning...)';
            nextStep5Btn.style.opacity = '0.6';

            // Hide cluster resources div and show scanning status
            document.getElementById('cluster-resources').style.display = 'none';
            document.getElementById('scanning-status').style.display = 'block';

            // Trigger cluster scan
            logToConsole('🔍 Auto-scanning cluster resources...', 'info');
            socket.emit('scan_cluster', {});
        }
    }
    if (step === 7) {
        restoreConfigSummary();
    }
    if (step > 5) {
        document.getElementById('step7-value').textContent = 'Ready';
    }
}

// Make step indicators clickable to go back
for (let i = 1; i <= 7; i++) {
    document.getElementById(`step${i}-indicator`).addEventListener('click', () => {
        if (i <= currentStep && i !== currentStep) {
            if (isOptimizationRunning() && i !== 7) {
                document.getElementById('running-modal').classList.add('active');
                return;
            }
            logToConsole(`← Going back to Step ${i}`, 'info');
            goToStep(i);
        }
    });
}

// Cluster scanning happens automatically when entering step 5
// (scan-cluster button removed - auto-scan on page entry)

// Socket events
socket.on('console_log', function(data) {
    const line = document.createElement('div');
    line.className = `console-line ${data.type || 'info'}`;
    line.textContent = data.message;
    const console = document.getElementById('console-output');
    console.appendChild(line);
    if (!data.replayed) {
        console.scrollTop = console.scrollHeight;
    }

    // Persist to localStorage
    saveConsoleMessage(data.message, data.type || 'info');
});

socket.on('status_update', function(data) {
    if (data.running) {
        document.getElementById('start-optimization').style.display = 'none';
        document.getElementById('stop-optimization').style.display = 'block';
    } else {
        document.getElementById('start-optimization').style.display = 'block';
        document.getElementById('stop-optimization').style.display = 'none';
    }
});

socket.on('clear_console', function() {
    // Clear console UI when server broadcasts clear event
    document.getElementById('console-output').innerHTML = '<div class="console-line">Console cleared.</div>';
    localStorage.removeItem('inferecipe-console');
});

socket.on('config_updated', function(data) {
    // Another client updated config - sync our state without saving back
    config = { ...config, ...data.config };
    currentStep = (data.current_step !== null && data.current_step !== undefined) ? data.current_step : 1;

    updateUIFromConfig();
    goToStep(currentStep, true);
});

socket.on('load_config_result', function(data) {
    if (data.success) {
        console.log('Loaded config from server:', data);
        config = { ...config, ...data.config };
        if (data.namespace) {
            config.namespace = data.namespace;
        }
        // Explicitly check for null/undefined, but allow 0 and other values
        currentStep = (data.current_step !== null && data.current_step !== undefined) ? data.current_step : 1;

        // Update UI to reflect loaded state without saving back
        updateUIFromConfig();
        goToStep(currentStep, true);

        // Sync button state with server optimization_running status
        const startBtn = document.getElementById('start-optimization');
        const stopBtn = document.getElementById('stop-optimization');
        if (data.optimization_running) {
            // Optimization is running - show Stop button
            if (startBtn) startBtn.style.display = 'none';
            if (stopBtn) stopBtn.style.display = 'block';
        } else {
            // Optimization is NOT running - show Start button
            if (startBtn) startBtn.style.display = 'block';
            if (stopBtn) stopBtn.style.display = 'none';
        }
    } else {
        console.error('Failed to load config:', data.error);
        // Still update UI from localStorage fallback
        updateUIFromConfig();
        goToStep(currentStep);
    }
});

socket.on('cluster_scan_result', function(data) {
    logToConsole('✅ Cluster scan complete!', 'success');
    logToConsole(`   Total GPUs: ${data.total_gpus}`, 'info');

    // Show GPU vendor and model if available
    let gpuInfo = data.gpu_type;
    if (data.gpu_vendor && data.gpu_vendor !== 'unknown') {
        gpuInfo = data.gpu_vendor;
        if (data.gpu_model && data.gpu_model !== 'unknown') {
            gpuInfo += ` ${data.gpu_model}`;
        }
    }
    logToConsole(`   GPU Type: ${gpuInfo}`, 'info');
    logToConsole(`   GPU Nodes: ${data.gpu_node_count}`, 'info');
    logToConsole(`   GPU Memory: ${data.total_gpu_memory_gb} GB total`, 'info');
    logToConsole(`   CPU Cores: ${data.total_cpu_cores}`, 'info');
    logToConsole(`   System RAM: ${data.total_memory_gb} GB`, 'info');
    logToConsole(`   RDMA Support: ${data.has_rdma ? 'Yes' : 'No'}`, 'info');

    // Log network interfaces with type and vendor
    if (data.total_network_interfaces > 0) {
        let nicLog = `   Network Interfaces: ${data.total_network_interfaces} total`;
        const nicDetails = [];
        if (data.network_interfaces_by_type && Object.keys(data.network_interfaces_by_type).length > 0) {
            const typesSummary = Object.entries(data.network_interfaces_by_type)
                .map(([type, count]) => `${count}× ${type}`)
                .join(', ');
            nicDetails.push(`Type: ${typesSummary}`);
        }
        if (data.network_interfaces_by_vendor && Object.keys(data.network_interfaces_by_vendor).length > 0) {
            const vendorsSummary = Object.entries(data.network_interfaces_by_vendor)
                .map(([vendor, count]) => `${count}× ${vendor}`)
                .join(', ');
            nicDetails.push(`Vendor: ${vendorsSummary}`);
        }
        if (nicDetails.length > 0) {
            nicLog += ` (${nicDetails.join(', ')})`;
        }
        logToConsole(nicLog, 'info');
    }

    logToConsole(`   Storage Classes: ${data.storage_classes.length}`, 'info');

    // Build network interfaces display
    let nicDisplay = 'None';
    if (data.total_network_interfaces > 0) {
        const nicTypes = [];
        if (data.network_interfaces_by_type) {
            for (const [type, count] of Object.entries(data.network_interfaces_by_type)) {
                nicTypes.push(`${count}× ${type}`);
            }
        }
        const nicVendors = [];
        if (data.network_interfaces_by_vendor) {
            for (const [vendor, count] of Object.entries(data.network_interfaces_by_vendor)) {
                nicVendors.push(`${count}× ${vendor}`);
            }
        }
        nicDisplay = `${data.total_network_interfaces} total`;
        if (nicTypes.length > 0) {
            nicDisplay += `<br>&nbsp;&nbsp;&nbsp;&nbsp;Type: ${nicTypes.join(', ')}`;
        }
        if (nicVendors.length > 0) {
            nicDisplay += `<br>&nbsp;&nbsp;&nbsp;&nbsp;Vendor: ${nicVendors.join(', ')}`;
        }
    }

    // Parse GPU info
    let gpuModel = '';
    let gpuAmount = data.total_gpus;
    if (data.gpu_vendor && data.gpu_vendor !== 'unknown') {
        gpuModel = data.gpu_vendor;
        if (data.gpu_model && data.gpu_model !== 'unknown') {
            gpuModel += ` ${data.gpu_model}`;
        }
    } else {
        gpuModel = data.gpu_type || '-';
    }
    const gpuMemory = Math.round(data.gpu_memory_per_gpu_mb / 1024);

    // Parse CPU info
    let cpuModel = data.cpu_model && data.cpu_model !== 'unknown' ? data.cpu_model : '-';

    // Parse host info
    let hostModel = data.host_model && data.host_model !== 'unknown' ? data.host_model : '-';

    // Parse NIC info
    let nicType = '-';
    let nicVendor = '-';
    let nicModel = '-';
    if (data.network_interfaces && data.network_interfaces.length > 0) {
        const nicTypes = [...new Set(data.network_interfaces.map(nic => nic.type).filter(t => t && t !== 'unknown'))];
        const nicVendors = [...new Set(data.network_interfaces.map(nic => nic.vendor).filter(v => v && v !== 'unknown'))];
        if (nicTypes.length > 0) nicType = nicTypes.join(', ');
        if (nicVendors.length > 0) nicVendor = nicVendors.join(', ');
    }
    if (data.nic_models && data.nic_models.length > 0) {
        nicModel = data.nic_models.join(', ');
    }

    // Build provider and network information panel (always shown)
    let providerName = 'Unknown';
    let networkName = 'Unknown';

    if (data.provider === 'ibm_cloud') {
        providerName = 'IBM Cloud';
    } else if (data.provider === 'coreweave') {
        providerName = 'CoreWeave';
    } else if (data.provider === 'baremetal') {
        providerName = 'Bare Metal';
    } else if (data.provider === 'aws') {
        providerName = 'AWS';
    } else if (data.provider === 'gcp') {
        providerName = 'GCP';
    } else if (data.provider === 'azure') {
        providerName = 'Azure';
    }

    if (data.network_type === 'dra') {
        networkName = 'DRA (DRANET)';
    } else if (data.network_type === 'nad') {
        networkName = 'NAD (Multus)';
    } else if (data.network_type === 'sriov') {
        networkName = 'SR-IOV';
    }

    // Build notes based on provider + network combination
    let deploymentNotes = '';

    // IBM Cloud with DRANET
    if (data.provider === 'ibm_cloud' && data.dranet_available) {
        deploymentNotes = `
            <li style="list-style-type: none;">✅ No pod-per-node limit - PD can run on single node</li>
            <li style="list-style-type: none;">✅ GPU+NIC PCIe affinity guaranteed</li>
        `;
    }
    // IBM Cloud with NAD only
    else if (data.provider === 'ibm_cloud' && !data.dranet_available) {
        deploymentNotes = `
            <li style="list-style-type: none;">⚠️ With NAD network: Requires minimum 2 nodes for PD/EP workloads</li>
            <li style="list-style-type: none;">⚠️ All nodes must have the same number of GPUs</li>
        `;
    }
    // CoreWeave
    else if (data.provider === 'coreweave') {
        deploymentNotes = `
            <li style="list-style-type: none;">✅ No pod-per-node constraints</li>
            <li style="list-style-type: none;">✅ InfiniBand RDMA via rdma/ib device plugin</li>
            <li style="list-style-type: none;">✅ Vanilla Kubernetes (not OpenShift)</li>
        `;
    }

    let ibmWarning = `
        <div style="padding: 4px 0; text-align: center;">
            <ul style="margin: 5px auto; padding: 0; list-style-position: inside; text-align: center;">
                <li style="list-style-type: none;"><strong>Provider:</strong> ${providerName}</li>
                <li style="list-style-type: none;"><strong>Network:</strong> ${networkName}</li>
                ${deploymentNotes}
            </ul>
        </div>
    `;

    // Display detected configuration (provider, network, notes)
    const detectedDiv = document.getElementById('detected-config-summary');
    if (detectedDiv) detectedDiv.innerHTML = ibmWarning;

    // Display hardware resources table
    const resourcesDiv = document.getElementById('resources-summary');
    resourcesDiv.innerHTML = `
        <table style="width: 100%; border-collapse: collapse; border-top: 2px solid var(--rh-gray-400); border-bottom: 2px solid var(--rh-gray-400);">
            <thead>
                <tr style="border-bottom: 2px solid var(--rh-gray-400);">
                    <th style="padding: 12px 8px; text-align: center; font-weight: 800; text-transform: uppercase; color: var(--rh-red-primary); font-size: 0.9em;">Type</th>
                    <th style="padding: 12px 8px; text-align: center; font-weight: 800; text-transform: uppercase; color: var(--rh-red-primary); font-size: 0.9em;">Model</th>
                    <th style="padding: 12px 8px; text-align: center; font-weight: 800; text-transform: uppercase; color: var(--rh-red-primary); font-size: 0.9em;">Amount</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">GPU</td>
                    <td style="padding: 10px 8px; text-align: center;">${gpuModel}</td>
                    <td style="padding: 10px 8px; text-align: center;">${gpuAmount} × (${gpuMemory} GB each)</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">GPU Nodes</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.gpu_node_count} (${data.max_gpus_per_node} GPUs per node)</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">Total GPU VRAM</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.total_gpu_memory_gb} GB</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">Host/Instance</td>
                    <td style="padding: 10px 8px; text-align: center;">${hostModel}</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.gpu_node_count} nodes</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">CPU</td>
                    <td style="padding: 10px 8px; text-align: center;">${cpuModel}</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.total_cpu_cores} cores</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">RAM</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.total_memory_gb} GB</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">RDMA/InfiniBand</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.has_rdma ? `Yes (${data.rdma_capable_nodes} nodes)` : 'No'}</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">Network Interfaces</td>
                    <td style="padding: 10px 8px; text-align: center;">${nicModel !== '-' ? nicModel : `${nicType} (${nicVendor})`}</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.total_network_interfaces} total (${Math.round(data.total_network_interfaces / data.gpu_node_count)} per node)</td>
                </tr>
                ${buildNicDetailRows(data)}
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">Max TP</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.max_gpus_per_node}</td>
                </tr>
                <tr>
                    <td style="padding: 10px 8px; font-weight: 600; text-align: center;">TP Options</td>
                    <td style="padding: 10px 8px; text-align: center;">-</td>
                    <td style="padding: 10px 8px; text-align: center;">${data.tp_options.join(', ')}</td>
                </tr>
            </tbody>
        </table>
    `;

    // Populate storage class dropdown
    const select = document.getElementById('storage-class-select');
    select.innerHTML = '<option value="">-- Select a Storage Class --</option>';
    data.storage_classes.forEach(sc => {
        const option = document.createElement('option');
        option.value = sc.name;
        option.textContent = `${sc.name} (${sc.provisioner})`;
        select.appendChild(option);
    });

    // Populate max GPU dropdown
    const maxGpuSelect = document.getElementById('max-gpu-select');
    maxGpuSelect.innerHTML = `<option value="${data.total_gpus}" selected>All (${data.total_gpus} GPUs)</option>`;

    // Add powers of 2 and common GPU counts
    const gpuOptions = [];
    for (let i = 1; i < data.total_gpus; i *= 2) {
        gpuOptions.push(i);
    }
    const commonValues = [4, 8, 16, 32, 64];
    commonValues.forEach(val => {
        if (val < data.total_gpus && !gpuOptions.includes(val)) {
            gpuOptions.push(val);
        }
    });
    gpuOptions.sort((a, b) => a - b);
    gpuOptions.forEach(count => {
        const option = document.createElement('option');
        option.value = count;
        option.textContent = `${count} GPU${count > 1 ? 's' : ''}`;
        maxGpuSelect.appendChild(option);
    });

    // Initialize from config if available, otherwise default to total_gpus
    if (config.max_gpus && config.max_gpus !== data.total_gpus) {
        maxGpuSelect.value = config.max_gpus;
    } else if (!config.max_gpus) {
        // First time - initialize to total GPUs (dropdown already shows this)
        config.max_gpus = data.total_gpus;
        saveConfig();
    }

    // Add event listener for max GPU selection
    maxGpuSelect.addEventListener('change', (e) => {
        const newMaxGpus = parseInt(e.target.value);
        config.max_gpus = newMaxGpus;
        saveConfig();
        // Update config summary live
        const gpuSummary = document.getElementById('config-summary-gpus');
        if (gpuSummary) gpuSummary.textContent = newMaxGpus;
        logToConsole(`✅ Maximum GPUs set to ${newMaxGpus}`, 'success');
        // Test plan will be regenerated when reaching step 6 (Review & Run)
    });

    // Display GPU usage information
    const gpuUsageInfo = document.getElementById('gpu-usage-info');
    if (data.gpus_in_use && data.gpus_in_use > 0) {
        gpuUsageInfo.innerHTML = `⚠️ <strong>${data.gpus_in_use} GPU${data.gpus_in_use > 1 ? 's' : ''}</strong> currently in use by other workloads. <strong>${data.gpus_available} GPU${data.gpus_available !== 1 ? 's' : ''}</strong> available.`;
        gpuUsageInfo.style.display = 'block';
    } else {
        gpuUsageInfo.style.display = 'none';
    }

    // Show max GPU selection group
    document.getElementById('max-gpu-group').style.display = 'block';

    // Populate node selection checkboxes
    if (data.nodes_detail && data.nodes_detail.length > 0) {
        let nodeHtml = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;">';
        data.nodes_detail.filter(node => node.gpus > 0).forEach(node => {
            const prevSelected = config.selected_nodes && config.selected_nodes.includes(node.name);
            nodeHtml += '<label style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:#f8fafc;border:2px solid ' + (prevSelected ? '#0ea5e9' : '#e2e8f0') + ';border-radius:8px;cursor:pointer;transition:border-color 0.2s;">' +
                '<input type="checkbox" class="node-select-cb" value="' + node.name + '" data-gpus="' + node.gpus + '"' + (prevSelected ? ' checked' : '') + ' style="width:18px;height:18px;accent-color:#0ea5e9;" onchange="validateNodeSelection();this.closest(\'label\').style.borderColor=this.checked?\'#0ea5e9\':\'#e2e8f0\'">' +
                '<div style="flex:1;min-width:0;">' +
                    '<div style="font-weight:600;color:#1e293b;font-size:0.95em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="' + node.name + '">' + node.name + '</div>' +
                    '<div style="font-size:0.82em;color:#64748b;">' + node.gpus + ' GPUs · ' + (node.gpu_model || 'GPU') + ' · ' + node.memory_gb + 'GB RAM</div>' +
                '</div>' +
            '</label>';
        });
        nodeHtml += '</div>';
        document.getElementById('node-select-list').innerHTML = nodeHtml;
        document.getElementById('node-select-group').style.display = 'block';

        if (config.selected_nodes && config.selected_nodes.length > 0) {
            document.getElementById('enable-node-select').checked = true;
            document.getElementById('node-select-list').style.opacity = '1';
            validateNodeSelection();
        }
    }

    // Hide scanning status and show resources section
    document.getElementById('scanning-status').style.display = 'none';
    document.getElementById('cluster-resources').style.display = 'block';

    // Show re-scan button
    document.getElementById('rescan-cluster-btn').style.display = 'inline-block';

    // Enable Continue to Review button
    const nextStep5Btn = document.getElementById('next-step6');
    nextStep5Btn.disabled = false;
    nextStep5Btn.textContent = 'Continue to Review →';
    nextStep5Btn.style.opacity = '1';

    // Store in config
    config.cluster_resources = data;
    saveConfig();
});

socket.on('test_plan_result', function(data) {
    if (data.can_proceed) {
        // Display simplified test plan summary
        const reportHtml = `
            <div style="background: #ffffff; border: 2px solid #e5e7eb; border-radius: 8px; padding: 20px; font-family: system-ui, -apple-system, sans-serif;">
                <div style="font-size: 1.1em; font-weight: bold; margin-bottom: 16px; color: #111827; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px;">
                    🧪 Test Plan Summary
                </div>

                <div style="margin-bottom: 16px;">
                    <div style="font-weight: 600; margin-bottom: 8px; color: #374151;">📊 Overview</div>
                    <div style="font-size: 0.9em; color: #6b7280; margin-bottom: 8px;">
                        • Total tests planned: ${data.tests.length}<br>
                        • Model: ${data.model_name}<br>
                        • GPUs available: ${data.max_gpus_to_use}<br>
                        • Optimization goal: ${data.optimization_goal}
                    </div>
                </div>

                <!-- Validation Status -->
                <div style="background: #fef3c7; border: 1px solid #f59e0b; padding: 12px; border-radius: 6px;">
                    <strong style="color: #92400e;">⏳ Preparing deployment templates...</strong>
                    <div style="margin-top: 4px; font-size: 0.9em; color: #78350f;">
                        Please wait while configurations are being saved.
                    </div>
                </div>

                <div style="margin-top: 12px; font-size: 0.85em; color: #6b7280;">
                    📋 View detailed test plan and VRAM calculations in the console below.
                </div>
            </div>
        `;

        document.getElementById('resource-validation').innerHTML = '';

        // Store test plan in config
        config.test_plan = data;
        saveConfig();

    } else {
        // Show error
        logToConsole('❌ ' + data.error_message, 'error');

        document.getElementById('resource-validation').innerHTML = `
            <div style="background: #fee2e2; border: 1px solid #dc2626; padding: 12px; border-radius: 6px;">
                <pre style="margin: 0; white-space: pre-wrap; font-family: monospace; font-size: 0.9em;">${data.error_message}</pre>
            </div>
        `;
    }
});

// New handler for when test plan is fully ready
socket.on('test_plan_ready', function(data) {
    if (data.ready) {
        // Enable the Start Optimization button
        const startButton = document.getElementById('start-optimization');
        startButton.disabled = false;
        startButton.textContent = '🚀 Start Optimization';
        startButton.style.opacity = '1';

        // Update the validation status in the UI
        const reportHtml = `
            <div style="background: #ffffff; border: 2px solid #e5e7eb; border-radius: 8px; padding: 20px; font-family: system-ui, -apple-system, sans-serif;">
                <div style="font-size: 1.1em; font-weight: bold; margin-bottom: 16px; color: #111827; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px;">
                    🧪 Test Plan Summary
                </div>

                <div style="margin-bottom: 16px;">
                    <div style="font-weight: 600; margin-bottom: 8px; color: #374151;">📊 Overview</div>
                    <div style="font-size: 0.9em; color: #6b7280; margin-bottom: 8px;">
                        • Total tests planned: ${config.test_plan.tests.length}<br>
                        • Model: ${config.test_plan.model_name}<br>
                        • GPUs available: ${config.test_plan.max_gpus_to_use}<br>
                        • Optimization goal: ${config.test_plan.optimization_goal}
                    </div>
                </div>

                <!-- Ready Status -->
                <div style="background: #d1fae5; border: 1px solid #059669; padding: 12px; border-radius: 6px;">
                    <strong style="color: #065f46;">✅ Ready to Start</strong>
                    <div style="margin-top: 4px; font-size: 0.9em; color: #047857;">
                        All ${config.test_plan.tests.length} test configurations have been prepared and saved.
                    </div>
                </div>

                <div style="margin-top: 12px; font-size: 0.85em; color: #6b7280;">
                    📋 View detailed test plan and VRAM calculations in the console below.
                </div>
            </div>
        `;

    }
});

socket.on('pvc_list_result', function(data) {
    const select = document.getElementById('existing-pvc-select');

    if (data.success && data.pvcs && data.pvcs.length > 0) {
        // Server already logged the PVC list, just populate dropdown

        // Populate dropdown
        select.innerHTML = '<option value="">-- Select a PVC --</option>';
        data.pvcs.forEach(pvc => {
            const option = document.createElement('option');
            option.value = pvc.name;

            // Show PVC name, size, and storage class
            let label = pvc.name;
            if (pvc.size) {
                label += ` (${pvc.size})`;
            }
            if (pvc.storage_class) {
                label += ` - ${pvc.storage_class}`;
            }

            option.textContent = label;
            select.appendChild(option);
        });

        // Restore selected PVC if available
        if (config.existing_pvc_name) {
            select.value = config.existing_pvc_name;
        }
    } else {
        logToConsole('⚠️  No PVCs found in namespace', 'warning');
        select.innerHTML = '<option value="">-- No PVCs found --</option>';
    }
});

socket.on('storage_setup_result', function(data) {
    if (data.success) {
        logToConsole('✅ Storage setup complete!', 'success');
        logToConsole(`   PVC: ${data.pvc_name}`, 'info');
        logToConsole(`   Size: ${data.pvc_size} GB`, 'info');
        logToConsole(`   Storage Class: ${data.storage_class}`, 'info');
        logToConsole(`   Model: ${data.model}`, 'info');

        if (data.existing) {
            // Using existing PVC - model already downloaded
            logToConsole('✅ Model already available in PVC', 'success');
            logToConsole('\n🚀 Proceeding to test execution...', 'info');
        } else {
            // New PVC - model download in progress
            logToConsole(`   Download Job: ${data.job_name}`, 'info');
            logToConsole('\n📥 Model download in progress...', 'info');
            logToConsole('   (This may take several minutes depending on model size)', 'info');
        }

        // Store PVC info in config
        config.pvc_name = data.pvc_name;
        config.download_job = data.job_name;
        saveConfig();
    } else {
        logToConsole(`❌ Storage setup failed: ${data.error}`, 'error');
    }
});

document.getElementById('save-console').addEventListener('click', saveConsoleToFile);
document.getElementById('clear-console').addEventListener('click', clearConsole);

// Reset button - clear database and all settings
document.getElementById('reset-indicator').addEventListener('click', () => {
    if (isOptimizationRunning()) {
        document.getElementById('running-modal').classList.add('active');
        return;
    }
    document.getElementById('reset-modal').classList.add('active');
});

// Reset modal - Cancel button
document.getElementById('reset-cancel').addEventListener('click', () => {
    document.getElementById('reset-modal').classList.remove('active');
});

// Reset modal - Confirm button
document.getElementById('reset-confirm').addEventListener('click', () => {
    // Hide modal
    document.getElementById('reset-modal').classList.remove('active');

    // Execute reset
    logToConsole('\n═══════════════════════════════════════', 'info');
    logToConsole('🔄 Resetting Database and Settings', 'warning');
    logToConsole('═══════════════════════════════════════', 'info');

    socket.emit('reset_database', {});
});

// Close modal when clicking outside
document.getElementById('reset-modal').addEventListener('click', (e) => {
    if (e.target.id === 'reset-modal') {
        document.getElementById('reset-modal').classList.remove('active');
    }
});

// Download database button — compress first, then download
document.getElementById('download-indicator').addEventListener('click', () => {
    logToConsole('\n💾 Compressing database for download...', 'info');

    var modal = document.getElementById('compress-modal');
    var bar = document.getElementById('compress-bar');
    var pctEl = document.getElementById('compress-percent');
    var statusEl = document.getElementById('compress-status');
    var sizeEl = document.getElementById('compress-size-info');

    bar.style.width = '0%';
    pctEl.textContent = '0%';
    statusEl.textContent = 'Compressing...';
    sizeEl.textContent = '';
    modal.classList.add('active');

    socket.emit('compress_database');
});

socket.on('compression_progress', function(data) {
    var bar = document.getElementById('compress-bar');
    var pctEl = document.getElementById('compress-percent');
    var statusEl = document.getElementById('compress-status');
    var sizeEl = document.getElementById('compress-size-info');

    bar.style.width = data.percent + '%';
    pctEl.textContent = data.percent + '%';
    if (data.status) statusEl.textContent = data.status;
    if (data.original_size) {
        var mb = (data.original_size / (1024 * 1024)).toFixed(1);
        sizeEl.textContent = 'Original size: ' + mb + ' MB';
    }
});

socket.on('compression_complete', function(data) {
    var bar = document.getElementById('compress-bar');
    var pctEl = document.getElementById('compress-percent');
    var statusEl = document.getElementById('compress-status');
    var sizeEl = document.getElementById('compress-size-info');

    bar.style.width = '100%';
    pctEl.textContent = '100%';
    statusEl.textContent = 'Downloading...';

    var origMb = (data.original_size / (1024 * 1024)).toFixed(1);
    var compMb = (data.compressed_size / (1024 * 1024)).toFixed(1);
    sizeEl.textContent = origMb + ' MB -> ' + compMb + ' MB (' + data.ratio + '% smaller)';
    logToConsole('   Compressed: ' + origMb + ' MB -> ' + compMb + ' MB (' + data.ratio + '% reduction)', 'info');

    fetch('/api/download_database')
        .then(function(response) {
            if (!response.ok) throw new Error('Download failed');
            return response.blob();
        })
        .then(function(blob) {
            var url = window.URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = 'inferecipe-optimizer.db.gz';
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            logToConsole('   Database downloaded successfully', 'success');
            setTimeout(function() {
                document.getElementById('compress-modal').classList.remove('active');
            }, 1000);
        })
        .catch(function(err) {
            logToConsole('   Failed to download: ' + err.message, 'error');
            document.getElementById('compress-modal').classList.remove('active');
        });
});

socket.on('compression_error', function(data) {
    logToConsole('   Compression failed: ' + data.error, 'error');
    document.getElementById('compress-modal').classList.remove('active');
});

// Upload database button
document.getElementById('upload-indicator').addEventListener('click', () => {
    document.getElementById('upload-db-input').click();
});

document.getElementById('upload-db-input').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    e.target.value = '';

    logToConsole(`\n📤 Uploading database: ${file.name} (${(file.size / 1024).toFixed(1)} KB)...`, 'info');

    const formData = new FormData();
    formData.append('database', file);

    fetch('/api/upload_database', {
        method: 'POST',
        body: formData,
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            logToConsole(`✅ Imported ${data.imported_runs} run(s) with ${data.imported_tests} test(s) from "${file.name}"`, 'success');
            if (data.skipped_runs > 0) {
                logToConsole(`   ⚠️  Skipped ${data.skipped_runs} run(s) that already exist`, 'warning');
            }
            logToConsole('   Open "Resume Testing" to see imported runs', 'info');
            loadRunList();
        } else {
            logToConsole(`❌ Upload failed: ${data.error}`, 'error');
        }
    })
    .catch(err => {
        logToConsole(`❌ Upload failed: ${err.message}`, 'error');
    });
});

// Handle reset database response
socket.on('reset_complete', (data) => {
    if (data.success) {
        logToConsole('✅ Database reset complete', 'success');
        logToConsole('🔄 Reloading page...', 'info');

        // Clear localStorage
        localStorage.clear();

        // Reload page after short delay
        setTimeout(() => {
            window.location.reload();
        }, 1000);
    } else {
        logToConsole(`❌ Reset failed: ${data.error}`, 'error');
    }
});

// Handle server-side errors (e.g., validation failures)
socket.on('error', (data) => {
    // Show error message
    if (data.message) {
        logToConsole(`❌ ${data.message}`, 'error');
    }

    // Reset button states if optimization didn't start
    const startBtn = document.getElementById('start-optimization');
    const stopBtn = document.getElementById('stop-optimization');
    if (startBtn && stopBtn) {
        startBtn.style.display = 'block';
        stopBtn.style.display = 'none';
    }
});

// --- Report / Charts Overlay ---
document.getElementById('report-indicator').addEventListener('click', () => {
    const overlay = document.getElementById('charts-overlay');
    overlay.classList.add('active');
    // Load run list
    loadRunList();
});

document.getElementById('charts-close').addEventListener('click', () => {
    document.getElementById('charts-overlay').classList.remove('active');
});

// Close on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.getElementById('charts-overlay').classList.remove('active');
        document.getElementById('resume-overlay').classList.remove('active');
    }
});

// --- Resume Testing Overlay ---
document.getElementById('resume-indicator').addEventListener('click', () => {
    document.getElementById('resume-overlay').classList.add('active');
    loadResumeRuns();
});

document.getElementById('resume-close').addEventListener('click', () => {
    document.getElementById('resume-overlay').classList.remove('active');
});

// Close resume overlay when clicking outside the container
document.getElementById('resume-overlay').addEventListener('click', (e) => {
    if (e.target.id === 'resume-overlay') {
        document.getElementById('resume-overlay').classList.remove('active');
    }
});

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
                }

                // Determine if resumable
                const canResume = (run.status !== 'completed' && completed > 0) ||
                                  (run.status === 'failed') ||
                                  (run.status === 'running');

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
                html += `<td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:0.85em;color:#475569;" title="${run.notes || ''}">${run.notes || '<span style="color:#cbd5e1;">—</span>'}</td>`;
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
                html += `<td style="white-space: nowrap;"><button class="restart-run-btn" data-run-id="${run.id}" data-run-name="${run.run_name || ''}" title="Restart run #${run.id} from beginning">🔄</button> <button class="delete-run-btn" data-run-id="${run.id}" title="Delete run #${run.id}">🗑</button></td>`;
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

// ===== TABBED REPORT MANAGEMENT =====
const reportTabs = [];
let activeTabId = null;
const tabDataCache = {};
let _tabCounter = 0;
let _chartSuffix = '';
function cid(id) { return id + _chartSuffix; }

document.getElementById('chart-add-btn').addEventListener('click', () => {
    const runId = document.getElementById('chart-run-select').value;
    if (runId) addReportTab(runId);
});

document.getElementById('chart-compare-btn').addEventListener('click', generateComparison);

function loadRunList() {
    fetch('/api/runs')
        .then(r => r.json())
        .then(runs => {
            const sel = document.getElementById('chart-run-select');
            sel.innerHTML = '';
            if (!runs.length) {
                sel.innerHTML = '<option value="">No runs found</option>';
                return;
            }
            runs.forEach(run => {
                const opt = document.createElement('option');
                opt.value = run.id;
                const modelShort = (run.model || '').split('/').pop() || '?';
                const goalMap = { ttft: 'TTFT', throughput: 'Throughput', balanced: 'Balanced' };
                const goal = goalMap[(run.goal || '').toLowerCase()] || run.goal || '?';
                let workload = '';
                if (run.isl && run.osl) {
                    workload = `ISL=${run.isl}`;
                    if (run.isl_stdev) workload += `±${run.isl_stdev}`;
                    workload += ` OSL=${run.osl}`;
                    if (run.osl_stdev) workload += `±${run.osl_stdev}`;
                    if (run.turns && run.turns > 1) workload += ` ${run.turns}T`;
                }
                const users = run.num_users ? `${run.num_users}u` : '';
                const gpus = run.max_gpus ? `${run.max_gpus}GPU` : '';
                const date = run.created_at ? new Date(run.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
                const isCompleted = run.status === 'completed';
                const statusLabel = isCompleted ? '\u2705 completed' : '\u274C ' + (run.status || 'unknown');
                const desc = run.notes ? `"${run.notes}"` : '';
                const parts = [`#${run.id}`, desc, goal, modelShort, workload, users, gpus, statusLabel, date].filter(Boolean);
                opt.textContent = parts.join(' | ');
                sel.appendChild(opt);
            });
            // No auto-load — let the user choose
        })
        .catch(err => {
            document.getElementById('charts-content').innerHTML =
                `<div class="charts-loading">Failed to load runs: ${err.message}</div>`;
        });
}

function addReportTab(runId) {
    // If tab for this run already exists, just switch to it
    const existing = reportTabs.find(t => t.runId == runId && !t.isComparison);
    if (existing) { switchReportTab(existing.id); return; }

    const tabId = 'rt' + (++_tabCounter);
    const sel = document.getElementById('chart-run-select');
    const opt = sel.querySelector('option[value="' + runId + '"]');
    const label = opt ? opt.textContent : 'Run #' + runId;

    reportTabs.push({ id: tabId, runId: runId, label: label, isComparison: false });

    // Remove placeholder text if present
    const placeholder = document.querySelector('#charts-content > .charts-loading');
    if (placeholder) placeholder.remove();

    // Create panel
    const panel = document.createElement('div');
    panel.id = 'panel-' + tabId;
    panel.className = 'report-tab-panel';
    panel.innerHTML = '<div class="charts-loading">Loading charts...</div>';
    document.getElementById('charts-content').appendChild(panel);

    updateTabBar();
    switchReportTab(tabId);

    // Fetch data and render
    fetch('/api/runs/' + runId + '/charts')
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                panel.innerHTML = '<div class="charts-loading">' + data.error + '</div>';
                return;
            }
            tabDataCache[tabId] = data;
            renderChartsInPanel(data, runId, tabId);
            // Update download link if this is still the active tab
            if (activeTabId === tabId) {
                const dlLink = document.getElementById('chart-download-link');
                dlLink.style.display = 'inline';
                dlLink.href = '#';
                dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(runId, data); };
            }
        })
        .catch(err => {
            panel.innerHTML = '<div class="charts-loading">Error: ' + err.message + '</div>';
        });
}

function renderChartsInPanel(data, runId, tabId) {
    const panel = document.getElementById('panel-' + tabId);
    const origContent = document.getElementById('charts-content');
    _chartSuffix = '-' + tabId;

    // Temporarily swap IDs so renderCharts writes to the panel
    panel.id = 'charts-content';
    origContent.id = '_charts-content-swap';

    renderCharts(data, runId);

    // Restore IDs
    panel.id = 'panel-' + tabId;
    origContent.id = 'charts-content';
    _chartSuffix = '';
}

function updateTabBar() {
    const bar = document.getElementById('report-tab-bar');
    const compareBtn = document.getElementById('chart-compare-btn');
    const runTabs = reportTabs.filter(t => !t.isComparison);

    if (reportTabs.length === 0) {
        bar.style.display = 'none';
        compareBtn.style.display = 'none';
        return;
    }

    bar.style.display = 'flex';
    compareBtn.style.display = runTabs.length >= 2 ? 'inline-block' : 'none';

    bar.innerHTML = '';
    reportTabs.forEach(tab => {
        const el = document.createElement('div');
        el.className = 'report-tab' + (tab.id === activeTabId ? ' active' : '') + (tab.isComparison ? ' compare-tab' : '');
        const shortLabel = tab.isComparison ? 'Compare' : '#' + tab.runId;
        el.innerHTML = '<span class="tab-label" title="' + (tab.label || '').replace(/"/g, '&quot;') + '">' + shortLabel + '</span><span class="tab-close" data-tab-id="' + tab.id + '">&times;</span>';
        el.addEventListener('click', (e) => {
            if (!e.target.classList.contains('tab-close')) switchReportTab(tab.id);
        });
        el.querySelector('.tab-close').addEventListener('click', (e) => {
            e.stopPropagation();
            closeReportTab(tab.id);
        });
        bar.appendChild(el);
    });

}

function updateEstimatorScaling(suffix) {
    var tabId = suffix.replace(/^-/, '');
    var data = tabDataCache[tabId];
    if (!data) return;
    var el = document.getElementById('est-scaling-info' + suffix);
    if (!el) return;

    var gpuSizing = data.gpu_sizing || {};
    var pTPSG = gpuSizing.prefill_tpsg || 0;
    var dTPSG = gpuSizing.decode_tpsg || 0;
    var hasTpsg = pTPSG > 0 && dTPSG > 0;

    var bullets = '';
    bullets += '<li><strong>GPU cost per request</strong> = ISL &divide; Prefill TPSG + OSL &divide; Decode TPSG (GPU-seconds)</li>';
    if (hasTpsg) {
        bullets += '<li><strong>Prefill TPSG</strong> = ' + Math.round(pTPSG).toLocaleString() + ' tokens/s/GPU (measured in Step 3 calibration)</li>';
        bullets += '<li><strong>Decode TPSG</strong> = ' + Math.round(dTPSG).toLocaleString() + ' tokens/s/GPU (measured in Step 2 calibration)</li>';
    }
    bullets += '<li><strong>Estimated GPUs</strong> = Tested GPUs &times; (new cost / tested cost) &times; (new concurrency / tested concurrency) &times; turns ratio</li>';
    bullets += '<li>Final GPU count rounded up to nearest <strong>Tensor Parallelism</strong> multiple</li>';
    bullets += '<li>Variance adjusts effective sequence length: effective = mean &times; (1 + CV&sup2;), CV = stdev/mean</li>';
    bullets += '<li><strong>Latency SLA</strong>: compares the tested TTFT at your chosen percentile against your target — workload changes may affect actual latency</li>';

    el.innerHTML = '<div style="text-align:left;"><strong>How GPU estimation works</strong>' +
        '<ul style="margin:8px 0 0 16px;padding:0;list-style:disc;line-height:1.7;">' + bullets + '</ul></div>';
}

function runEstimator(suffix) {
    // Find which report tab owns this estimator via the suffix (e.g., '-rt1')
    const tabId = suffix.replace(/^-/, '');
    const data = tabDataCache[tabId];
    if (!data) return;

    const userConc = parseFloat(document.getElementById('est-concurrency' + suffix).value) || 1;
    const userISL = parseFloat(document.getElementById('est-isl' + suffix).value) || 1;
    const userOSL = parseFloat(document.getElementById('est-osl' + suffix).value) || 1;
    const userIslStdev = parseFloat(document.getElementById('est-isl-stdev' + suffix).value) || 0;
    const userOslStdev = parseFloat(document.getElementById('est-osl-stdev' + suffix).value) || 0;
    const turnsToggle = document.getElementById('est-turns-toggle' + suffix);
    const userTurns = (turnsToggle && turnsToggle.checked) ? (parseFloat(document.getElementById('est-turns' + suffix).value) || 1) : 1;

    const wl = data.recommendation ? data.recommendation.workload : {};
    const testedISL = wl.isl || userISL;
    const testedOSL = wl.osl || userOSL;
    const testedUsers = wl.users || userConc;
    const testedTurns = wl.turns || 1;
    const testedIslStdev = wl.isl_stdev || 0;
    const testedOslStdev = wl.osl_stdev || 0;

    const slaToggle = document.getElementById('est-sla-toggle' + suffix);
    const slaEnabled = slaToggle && slaToggle.checked;
    const slaMs = slaEnabled ? (parseFloat(document.getElementById('est-sla-ms' + suffix).value) || 500) : null;
    const slaPctl = slaEnabled ? (document.getElementById('est-sla-pctl' + suffix).value || 'p99') : null;

    const gpuSizing = data.gpu_sizing || {};
    const latencySearch = data.latency_search || null;
    const results = estimateGPUs(data.all_results || [], testedISL, testedOSL, testedUsers, testedTurns, testedIslStdev, testedOslStdev, userConc, userISL, userOSL, userIslStdev, userOslStdev, userTurns, gpuSizing, slaMs, slaPctl, latencySearch);
    renderEstimatorResults(results, suffix, testedISL, testedOSL, testedUsers, testedTurns, testedIslStdev, testedOslStdev, slaMs, slaPctl);
}

function estimateGPUs(allResults, testedISL, testedOSL, testedUsers, testedTurns, testedIslStdev, testedOslStdev, userConcurrency, userISL, userOSL, userIslStdev, userOslStdev, userTurns, gpuSizing, slaMs, slaPctl, latencySearch) {
    // GPU estimation using additive cost model with TPSG from calibration.
    //
    // GPU cost per request = ISL/prefill_TPSG + OSL/decode_TPSG (GPU-seconds)
    // Total GPUs = cost_per_request × concurrency × headroom × turns
    //
    // Variance increases effective sequence length using Pollaczek-Khinchine:
    //   effective_length = mean × (1 + CV²), where CV = stdev/mean

    function effectiveLen(mean, stdev) {
        if (!stdev || !mean) return mean;
        var cv = stdev / mean;
        return mean * (1 + cv * cv);
    }

    const prefillTPSG = gpuSizing && gpuSizing.prefill_tpsg ? gpuSizing.prefill_tpsg : 0;
    const decodeTPSG = gpuSizing && gpuSizing.decode_tpsg ? gpuSizing.decode_tpsg : 0;
    const headroom = gpuSizing && gpuSizing.headroom ? gpuSizing.headroom : 1.3;

    const testedEffISL = effectiveLen(testedISL, testedIslStdev);
    const testedEffOSL = effectiveLen(testedOSL, testedOslStdev);
    const userEffISL = effectiveLen(userISL, userIslStdev);
    const userEffOSL = effectiveLen(userOSL, userOslStdev);

    // Use TPSG-based additive cost model when calibration data is available
    const hasTpsg = prefillTPSG > 0 && decodeTPSG > 0;
    var costScale;
    if (hasTpsg) {
        var testedCost = testedEffISL / prefillTPSG + testedEffOSL / decodeTPSG;
        var userCost = userEffISL / prefillTPSG + userEffOSL / decodeTPSG;
        costScale = userCost / testedCost;
    } else {
        // Fallback: proportional scaling (less accurate)
        costScale = (userEffISL / testedEffISL) * (userEffOSL / testedEffOSL);
    }

    const concScale = userConcurrency / (testedUsers || userConcurrency);
    const turnsScale = (userTurns || 1) / (testedTurns || 1);
    const totalScale = concScale * costScale * turnsScale;

    // Keep best throughput per architecture
    const bestByArch = {};
    allResults.filter(r => r.throughput_p90 >= 1 && r.gpus > 0).forEach(r => {
        const arch = (r.architecture || 'UNKNOWN').toUpperCase();
        if (!bestByArch[arch] || r.throughput_p90 > bestByArch[arch].throughput_p90) {
            bestByArch[arch] = r;
        }
    });

    return Object.values(bestByArch).map(r => {
        const rawGpus = r.gpus * totalScale;
        const tp = r.tp || r.prefill_tp || 1;
        const estGpus = Math.max(tp, Math.ceil(rawGpus / tp) * tp);

        // SLA assessment from Step 7 tested data
        var sla_ttft = null;
        var sla_meets = null;
        if (slaMs && slaPctl) {
            sla_ttft = r['ttft_' + slaPctl];
            if (sla_ttft != null) {
                sla_meets = sla_ttft <= slaMs;
            }
        }

        return {
            config_name: r.config_name,
            chart_label: r.config_name,
            architecture: r.architecture,
            tested_gpus: r.gpus,
            estimated_gpus: estGpus,
            tp: tp,
            cost_model: hasTpsg ? 'tpsg' : 'proportional',
            prefill_tpsg: prefillTPSG,
            decode_tpsg: decodeTPSG,
            ttft_p50: r.ttft_p50, ttft_p90: r.ttft_p90, ttft_p95: r.ttft_p95, ttft_p99: r.ttft_p99,
            throughput_p50: r.throughput_p50, throughput_p90: r.throughput_p90, throughput_p95: r.throughput_p95, throughput_p99: r.throughput_p99,
            sla_target_ms: slaMs,
            sla_percentile: slaPctl,
            sla_ttft: sla_ttft,
            sla_meets: sla_meets,
        };
    }).sort((a, b) => a.estimated_gpus - b.estimated_gpus);
}

function renderEstimatorResults(results, suffix, testedISL, testedOSL, testedUsers, testedTurns, testedIslStdev, testedOslStdev, slaMs, slaPctl) {
    if (results.length === 0) {
        document.getElementById('est-results' + suffix).innerHTML = '<p style="color:#6b7280;padding:16px;">No valid configurations to estimate.</p>';
        return;
    }

    const bestGpus = results[0].estimated_gpus;

    let html = '';
    var v = function(x, u) { return x != null ? x + ' ' + u : '-'; };
    var sep = 'border-left:3px solid #e2e8f0;';

    var hasSla = slaMs && slaPctl;
    html += '<table class="estimator-table"><thead>' +
        '<tr><th rowspan="2">Configuration</th><th rowspan="2">Arch</th><th rowspan="2">TP</th>' +
        '<th rowspan="2">Tested<br>GPUs</th><th rowspan="2">Est.<br>GPUs</th>' +
        '<th colspan="4" style="text-align:center;' + sep + '">TTFT (ms)</th>' +
        '<th colspan="4" style="text-align:center;' + sep + '">Throughput (req/s)</th>' +
        (hasSla ? '<th rowspan="2" style="' + sep + '">SLA<br>' + slaPctl.toUpperCase() + ' &le; ' + slaMs + 'ms</th>' : '') +
        '</tr>' +
        '<tr>' +
        '<th style="' + sep + '">P50</th><th>P90</th><th>P95</th><th>P99</th>' +
        '<th style="' + sep + '">P50</th><th>P90</th><th>P95</th><th>P99</th>' +
        '</tr></thead><tbody>';

    results.forEach(r => {
        const isBest = r.estimated_gpus === bestGpus;
        var slaCell = '';
        if (hasSla) {
            if (r.sla_meets === true) {
                slaCell = '<td style="' + sep + 'color:#059669;font-weight:700;text-align:center;">PASS<br><span style="font-weight:400;font-size:0.85em;">' + (r.sla_ttft != null ? r.sla_ttft.toFixed(1) + ' ms' : '-') + '</span></td>';
            } else if (r.sla_meets === false) {
                slaCell = '<td style="' + sep + 'color:#dc2626;font-weight:700;text-align:center;">FAIL<br><span style="font-weight:400;font-size:0.85em;">' + (r.sla_ttft != null ? r.sla_ttft.toFixed(1) + ' ms' : '-') + '</span></td>';
            } else {
                slaCell = '<td style="' + sep + 'color:#64748b;text-align:center;">N/A</td>';
            }
        }
        html += '<tr class="' + (isBest ? 'estimator-best' : '') + '">' +
            '<td>' + r.config_name + '</td>' +
            '<td><span class="arch-badge arch-' + r.architecture.toLowerCase() + '">' + r.architecture + '</span></td>' +
            '<td>' + r.tp + '</td>' +
            '<td>' + r.tested_gpus + '</td>' +
            '<td><strong>' + r.estimated_gpus + '</strong></td>' +
            '<td style="' + sep + '">' + v(r.ttft_p50, '') + '</td>' +
            '<td>' + v(r.ttft_p90, '') + '</td>' +
            '<td>' + v(r.ttft_p95, '') + '</td>' +
            '<td>' + v(r.ttft_p99, '') + '</td>' +
            '<td style="' + sep + '">' + v(r.throughput_p50, '') + '</td>' +
            '<td>' + v(r.throughput_p90, '') + '</td>' +
            '<td>' + v(r.throughput_p95, '') + '</td>' +
            '<td>' + v(r.throughput_p99, '') + '</td>' +
            slaCell +
            '</tr>';
    });
    html += '</tbody></table>';
    document.getElementById('est-results' + suffix).innerHTML = html;

    // Grouped bar chart: tested vs estimated GPUs
    const labels = results.map(r => r.chart_label);
    Plotly.newPlot('est-chart' + suffix, [
        {
            type: 'bar',
            name: 'Tested GPUs',
            y: labels,
            x: results.map(r => r.tested_gpus),
            text: results.map(r => r.tested_gpus + ' GPUs'),
            textposition: 'inside',
            orientation: 'h',
            marker: { color: '#94a3b8' },
            hovertemplate: '%{y}<br>Tested: %{x} GPUs<extra></extra>',
        },
        {
            type: 'bar',
            name: 'Estimated GPUs',
            y: labels,
            x: results.map(r => r.estimated_gpus),
            text: results.map(r => r.estimated_gpus + ' GPUs'),
            textposition: 'outside',
            orientation: 'h',
            marker: { color: '#d97706' },
            hovertemplate: '%{y}<br>Estimated: %{x} GPUs<extra></extra>',
        }
    ], {
        title: { text: 'GPU Requirements: Tested vs Estimated', font: { size: 15 } },
        xaxis: { title: 'GPUs' },
        yaxis: { automargin: true },
        barmode: 'group',
        margin: { l: 200, r: 140, t: 50, b: 40 },
        height: Math.max(300, results.length * 80 + 80),
        legend: { orientation: 'v', x: 1.02, y: 1, xanchor: 'left' },
    }, { responsive: true, displayModeBar: false });
}

function switchReportTab(tabId) {
    activeTabId = tabId;
    // Toggle panel visibility
    document.querySelectorAll('#charts-content > .report-tab-panel').forEach(p => {
        p.classList.toggle('active', p.id === 'panel-' + tabId);
    });
    updateTabBar();

    // Update download link
    const tab = reportTabs.find(t => t.id === tabId);
    const dlLink = document.getElementById('chart-download-link');
    if (tab && !tab.isComparison && tabDataCache[tabId]) {
        dlLink.style.display = 'inline';
        dlLink.href = '#';
        dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(tab.runId, tabDataCache[tabId]); };
    } else {
        dlLink.style.display = 'none';
    }
}

function closeReportTab(tabId) {
    const idx = reportTabs.findIndex(t => t.id === tabId);
    if (idx === -1) return;
    reportTabs.splice(idx, 1);
    delete tabDataCache[tabId];

    const panel = document.getElementById('panel-' + tabId);
    if (panel) panel.remove();

    if (activeTabId === tabId) {
        if (reportTabs.length > 0) {
            switchReportTab(reportTabs[Math.min(idx, reportTabs.length - 1)].id);
        } else {
            activeTabId = null;
            document.getElementById('chart-download-link').style.display = 'none';
            // Show placeholder
            const content = document.getElementById('charts-content');
            if (!content.querySelector('.charts-loading')) {
                const ph = document.createElement('div');
                ph.className = 'charts-loading';
                ph.textContent = 'Select a run and click + Add to view results';
                content.appendChild(ph);
            }
        }
    }
    updateTabBar();
}

function fmtSI(v, decimals) {
    if (v == null) return '-';
    const d = decimals != null ? decimals : 1;
    if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(d) + 'M';
    if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(d) + 'K';
    return v.toFixed(d);
}

function arrowAnnotations(xs, ys, opts) {
    const color = (opts && opts.color) || '#333';
    const decimals = opts && opts.decimals != null ? opts.decimals : 1;
    const suffix = (opts && opts.suffix) || '';
    const yref = (opts && opts.yref) || 'y';
    const s = (opts && opts.spread) || 30;
    const offsets = [
        { ax: 0,          ay: -s        },
        { ax:  s * 0.9,   ay:  s * 0.7  },
        { ax: -s * 0.8,   ay: -s * 1.2  },
        { ax:  s * 1.1,   ay: -s * 0.5  },
        { ax: 0,          ay:  s * 1.1   },
        { ax: -s,         ay:  s * 0.8   },
        { ax:  s * 1.3,   ay: -s * 1.3   },
        { ax: -s * 1.2,   ay:  s * 1.3   },
    ];
    return ys.map((v, i) => {
        if (v == null) return null;
        const o = offsets[i % offsets.length];
        return {
            x: xs[i], y: v, xref: 'x', yref: yref,
            text: fmtSI(v, decimals) + suffix,
            showarrow: true, arrowhead: 0, arrowwidth: 1, arrowcolor: '#94a3b8',
            ax: o.ax, ay: o.ay,
            font: { size: 10, color: color },
            borderpad: 2,
        };
    }).filter(Boolean);
}

function renderCharts(data, runId) {
    const content = document.getElementById('charts-content');
    const summary = data.summary;
    const charts = data.charts;
    const rec = data.recommendation;

    // Download link handled by tab management
    const dlLink = document.getElementById('chart-download-link');
    dlLink.style.display = 'inline';
    dlLink.href = '#';
    dlLink.onclick = (e) => { e.preventDefault(); downloadHTMLReport(runId, data); };

    let html = '';
    let secRec = '', secTP = '', secCfg = '', secCmp = '', secStep9 = '', secCal = '', secVLLM = '', secTestCfg = '', secEppTuning = '';

    // Build a lookup from test_id -> manifest_types for download links
    const manifestLookup = {};
    const testIdLookup = {};
    (data.all_results || []).forEach(r => {
        const tid = r.test_id || r.config_name;
        if (r.manifest_types && r.manifest_types.length) manifestLookup[tid] = r.manifest_types;
        testIdLookup[r.config_name] = tid;
    });

    // ============================================================
    // GOAL BANNER — what was this run optimizing for
    // ============================================================
    if (rec && rec.goal_info) {
        const gColors = { ttft: '#3b82f6', throughput: '#f59e0b', balanced: '#10b981', aggregated_only: '#64748b', pd_only: '#8b5cf6', ep_only: '#0ea5e9' };
        const gIcons = { ttft: '&#9201;', throughput: '&#9889;', balanced: '&#9878;', aggregated_only: '&#9634;', pd_only: '&#8644;', ep_only: '&#9881;' };
        const gc = gColors[rec.goal] || '#10b981';
        html += `<div class="chart-card" style="border: 3px solid ${gc}; border-left: 8px solid ${gc};">`;
        html += `<div class="chart-card-header" style="background: ${gc}; color: white; font-size: 1.3em;">`;
        html += `${gIcons[rec.goal] || ''} ${rec.goal_info.name}</div>`;
        html += `<div style="background:${gc}dd; color:white; padding:8px 20px; font-size:0.92em; display:flex; flex-wrap:wrap; gap:6px 20px;">`;
        html += `<span>Model: <strong>${rec.model}</strong></span>`;
        let wlLabel = `ISL: <strong>${rec.workload.isl}</strong>`;
        if (rec.workload.isl_stdev) wlLabel += ` (σ=${rec.workload.isl_stdev})`;
        wlLabel += ` | OSL: <strong>${rec.workload.osl}</strong>`;
        if (rec.workload.osl_stdev) wlLabel += ` (σ=${rec.workload.osl_stdev})`;
        if (rec.workload.turns && rec.workload.turns > 1) wlLabel += ` | Turns: <strong>${rec.workload.turns}</strong>`;
        html += `<span>${wlLabel}</span>`;
        html += `<span>Users: <strong>${rec.workload.users}</strong></span>`;
        html += `<span>Tests: <strong>${rec.total_tests}</strong></span>`;
        if (rec.total_duration) html += `<span>Duration: <strong>${rec.total_duration}</strong></span>`;
        html += '</div>';
        html += '<div class="chart-card-body" style="padding: 20px;">';
        html += `<p style="color:#334155; margin:0; font-size:0.95em; line-height:1.6;">${rec.goal_info.description}</p>`;
        html += '</div></div>';
    }

    // ============================================================
    // RECOMMENDATION — the bottom line
    // ============================================================
    if (rec && rec.recommendations && Object.keys(rec.recommendations).length) {
        html += '<div class="chart-card" style="border: 2px solid #10b981; border-left: 6px solid #10b981;">';
        html += '<div class="chart-card-header" style="background: linear-gradient(135deg, #ecfdf5, #d1fae5); font-size: 1.2em;">';
        html += 'Deployment Recommendation</div>';
        html += '<div class="chart-card-body" style="padding: 24px;">';

        // Recommendation cards for each goal
        html += '<div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px;">';
        const goalIcons = { response_time: '&#9201;', throughput: '&#9889;' };
        const goalColors = { response_time: '#3b82f6', throughput: '#f59e0b' };
        const goalExplain = {
            response_time: 'Best for chatbots, real-time assistants, and interactive applications where users are waiting for a reply. This configuration minimizes the delay before the model starts generating its response.',
            throughput: 'Best for batch processing, API services, and high-volume workloads where you need to handle the most requests per second. Users may wait slightly longer per request, but the system serves more users overall.',
        };
        for (const [key, r] of Object.entries(rec.recommendations)) {
            const c = r.config;
            const isPrimary = (rec.goal === 'ttft' && key === 'response_time') ||
                             (rec.goal === 'throughput' && key === 'throughput');
            const border = isPrimary ? `3px solid ${goalColors[key]}` : `2px solid ${goalColors[key]}40`;
            const badge = isPrimary ? `<span style="background:${goalColors[key]}; color:white; font-size:0.7em; padding:2px 8px; border-radius:4px; margin-left:8px;">PRIMARY</span>` : '';
            const archBadge = r.architecture ? `<span style="background:#64748b; color:white; font-size:0.65em; padding:2px 6px; border-radius:3px; margin-left:6px;">${r.architecture}</span>` : '';
            html += `<div style="background:${goalColors[key]}10; border:${border}; border-radius:10px; padding:16px;">`;
            html += `<div style="font-weight:800; color:${goalColors[key]}; font-size:0.85em; text-transform:uppercase; margin-bottom:8px;">${goalIcons[key] || ''} ${r.goal}${badge}${archBadge}</div>`;
            html += `<div style="font-size:1.4em; font-weight:800; color:#1e293b; margin-bottom:4px;">${r.deploy}</div>`;
            const details = c.ratio ? `P:D ratio ${c.ratio} &nbsp;|&nbsp; ` : '';
            // Show both TTFT and Throughput for every recommendation card
            const ttftStr = c.ttft_p90 != null ? `TTFT P90: <strong>${c.ttft_p90} ms</strong>` : '';
            const tputStr = c.throughput_p90 != null ? `Throughput P90: <strong>${c.throughput_p90} req/s</strong>` : '';
            const metricsStr = [ttftStr, tputStr].filter(Boolean).join(' &nbsp;|&nbsp; ');
            html += `<div style="font-size:0.9em; color:#475569;">${details}${metricsStr} &nbsp;|&nbsp; ${c.gpus} GPUs</div>`;
            html += `<div style="font-size:0.82em; color:#64748b; margin-top:8px; line-height:1.5;">${goalExplain[key] || ''}</div>`;
            // Manifest download links for recommended config
            const recTestId = c.test_id || testIdLookup[c.config_name] || c.config_name;
            const recManifests = manifestLookup[recTestId];
            if (recManifests && recManifests.length) {
                html += '<div style="margin-top:10px; padding-top:8px; border-top:1px solid #e2e8f0;">';
                html += '<span style="font-size:0.78em; color:#64748b; margin-right:6px;">Download YAML:</span>';
                recManifests.filter(t => !t.includes('service')).forEach(t => {
                    html += `<a href="/api/run/${runId}/config/${recTestId}/manifest/${t}" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:2px; display:inline-block;">${t}</a>`;
                });
                html += '</div>';
            }
            html += '</div>';
        }
        html += '</div>';

        // Optimal TP values and test counts
        if (rec.optimal_decode_tp || rec.optimal_prefill_tp || rec.pd_tests_count || rec.ep_tests_count) {
            html += '<div style="background:#f8fafc; border-radius:8px; padding:14px 18px; display:flex; gap:32px; flex-wrap:wrap; font-size:0.9em; margin-top:12px;">';
            if (rec.optimal_decode_tp)
                html += `<div><strong>Optimal Decode TP:</strong> ${rec.optimal_decode_tp.tp} <span style="color:#64748b">(${rec.optimal_decode_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.optimal_prefill_tp)
                html += `<div><strong>Optimal Prefill TP:</strong> ${rec.optimal_prefill_tp.tp} <span style="color:#64748b">(${rec.optimal_prefill_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.pd_tests_count)
                html += `<div><strong>PD Splits Tested:</strong> ${rec.pd_tests_count}</div>`;
            if (rec.ep_tests_count)
                html += `<div><strong>EP Configs Tested:</strong> ${rec.ep_tests_count}</div>`;
            html += '</div>';
        }

        // Constraint notes (asymmetric TP, etc.)
        if (rec.constraint_notes && rec.constraint_notes.length) {
            html += '<div style="background:#fffbeb; border:2px solid #f59e0b; border-left:6px solid #f59e0b; border-radius:8px; padding:14px 18px; margin-top:12px;">';
            html += '<div style="font-weight:700; color:#92400e; margin-bottom:8px; font-size:0.95em;">&#9888; Configuration Constraints</div>';
            for (const note of rec.constraint_notes) {
                html += `<p style="color:#78350f; margin:0 0 8px; font-size:0.88em; line-height:1.6;">${note}</p>`;
            }
            html += '</div>';
        }

        html += '</div></div>';

    } // end Deployment Recommendation card

    // Flush recommendation part 1 (goal banner + deployment cards)
    secRec = html; html = '';

    // ============================================================
    // TP CALIBRATION CHARTS — Step 2 (Decode) & Step 3 (Prefill)
    // ============================================================
    if (rec) {
            const hasDecodeTP = rec.decode_tp_all && rec.decode_tp_all.length;
            const hasPrefillTP = rec.prefill_tp_all && rec.prefill_tp_all.length;
            if (hasDecodeTP || hasPrefillTP) {
                html += '<div class="charts-grid-2col">';
                html += chartCard(
                    'Step 2: Decode TP Sweep',
                    hasDecodeTP
                        ? 'Each TP value was tested with a single decode pod to find the optimal tensor parallelism for decode. The <strong>tokens/s/GPU</strong> metric (bars) shows efficiency — higher is better. The <strong style="color:#ef4444">ITL P90</strong> line shows inter-token latency — lower means smoother streaming. The optimal TP maximizes throughput per GPU.'
                        : 'Decode TP calibration chart will appear here once Step 2 completes.',
                    'chart-tp-decode'
                );
                html += chartCard(
                    'Step 3: Prefill TP Sweep',
                    hasPrefillTP
                        ? 'Each TP value was tested with a single prefill pod to find the optimal tensor parallelism for prefill. The <strong>tokens/s/GPU</strong> metric (bars) shows efficiency — higher is better. The <strong style="color:#ef4444">TTFT P90</strong> line shows time-to-first-token — lower means the model starts responding faster.'
                        : 'Prefill TP calibration chart will appear here once Step 3 completes.',
                    'chart-tp-prefill'
                );
                html += '</div>';
            }
        }

    // Flush TP calibration
    secTP = html; html = '';

    // ============================================================
    // PERCENTILE BREAKDOWN — combined primary vs Aggregated table
    // ============================================================
    if (rec && rec.recommendations) {
        const primaryKey = rec.goal === 'ttft' ? 'response_time' : 'throughput';
        const primaryRec = rec.recommendations[primaryKey];
        const aggBase = rec.aggregated_baseline;
        const primaryArch = primaryRec && primaryRec.architecture ? primaryRec.architecture : 'PD';

        // Build a combined table when we have both primary and Aggregated percentiles
        const hasPrimary = primaryRec && primaryRec.config.percentiles;
        const hasAgg = aggBase && aggBase.percentiles && (!primaryRec || aggBase.config_name !== primaryRec.config.config_name);

        if (hasPrimary) {
            const p = primaryRec.config.percentiles;
            const a = hasAgg ? aggBase.percentiles : null;

            html += `<div class="chart-card"><div class="chart-card-header">Percentile Breakdown: ${primaryArch} vs Aggregated</div>`;
            html += `<div style="padding:10px 20px 4px; color:#4b5563; font-size:0.9em;">Full percentile distribution (P50 through P99) for TTFT, ITL, and Throughput — comparing the best ${primaryArch} configuration against the Aggregated baseline using the same GPU budget. Green-highlighted P90 values indicate the winner for each metric.</div>`;
            html += '<div class="chart-card-body" style="padding:0;">';
            html += '<table class="results-table">';

            html += '<tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';

            const betterLower = (v1, v2) => v1 != null && v2 != null && v1 < v2;
            const betterHigher = (v1, v2) => v1 != null && v2 != null && v1 > v2;

            const metricDefs = [
                { name: 'TTFT (ms)', key: 'ttft', lowerBetter: true },
                { name: 'ITL (ms)', key: 'itl', lowerBetter: true },
                { name: 'Throughput (req/s)', key: 'throughput', lowerBetter: false },
            ];

            const entries = [{label: primaryArch, pctl: p}];
            if (a) entries.push({label: 'Aggregated', pctl: a});

            entries.forEach(({label, pctl}, idx) => {
                metricDefs.forEach((m, mi) => {
                    const d = pctl[m.key];
                    const borderStyle = mi === 0 && idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
                    let p90Style = '';
                    if (a) {
                        const other = idx === 0 ? a[m.key] : p[m.key];
                        const wins = m.lowerBetter ? betterLower(d.p90, other.p90) : betterHigher(d.p90, other.p90);
                        if (wins) p90Style = 'color:#10b981; font-weight:700;';
                    }
                    html += '<tr>';
                    if (mi === 0) html += `<td rowspan="3" style="vertical-align:middle; font-weight:700;${borderStyle}">${label}</td>`;
                    html += `<td style="color:#64748b;${borderStyle}">${m.name}</td>`;
                    html += `<td style="${borderStyle}">${d.p50 ?? '-'}</td>`;
                    html += `<td style="${p90Style}${borderStyle}">${d.p90 ?? '-'}</td>`;
                    html += `<td style="${borderStyle}">${d.p95 ?? '-'}</td>`;
                    html += `<td style="${borderStyle}">${d.p99 ?? '-'}</td>`;
                    html += '</tr>';
                });
            });

            html += '</table></div></div>';
        }
    }

    // --- Summary cards ---
    const best = summary.best_configs || {};
    html += '<div class="charts-summary">';
    html += statCard(summary.successful_tests, 'Successful Tests', `${summary.total_tests} total`);
    if (best.lowest_latency) {
        const ll = best.lowest_latency;
        html += statCard(ll.ttft_p90.toFixed(1) + ' ms', 'Best TTFT P90', ll.name);
        if (ll.ttft_p95) html += statCard(ll.ttft_p95.toFixed(1) + ' ms', 'Best TTFT P95', ll.name);
        if (ll.ttft_p99) html += statCard(ll.ttft_p99.toFixed(1) + ' ms', 'Best TTFT P99', ll.name);
    }
    if (best.highest_throughput) {
        const ht = best.highest_throughput;
        html += statCard(ht.throughput_p90.toFixed(2) + ' req/s', 'Best Throughput P90', ht.name);
        if (ht.throughput_p95) html += statCard(ht.throughput_p95.toFixed(2) + ' req/s', 'Best Throughput P95', ht.name);
        if (ht.throughput_p99) html += statCard(ht.throughput_p99.toFixed(2) + ' req/s', 'Best Throughput P99', ht.name);
    }
    if (best.most_efficient)
        html += statCard(best.most_efficient.efficiency.toFixed(3) + ' req/s/GPU', 'Best Efficiency', best.most_efficient.name);
    html += '</div>';

    // Flush recommendation part 2 (percentile breakdown + summary cards)
    secRec += html; html = '';

    // --- Charts with descriptions ---
    const chartDesc = {
        pareto: 'Steps 2 &amp; 3 tested each TP value with a single <strong style="color:#3b82f6">Decode</strong> pod and a single <strong style="color:#f59e0b">Prefill</strong> pod to calibrate tensor parallelism. Points lower and to the left are better (faster response with fewer GPUs). The optimal TP for each role was selected from these results.',
        scatter: 'Each bubble is a tested configuration. The <strong>bubble size</strong> represents GPU count. The ideal configuration is in the <strong>top-left corner</strong> (low latency + high throughput). Hover over any bubble to see the exact configuration details.',
        efficiency: 'Shows how many requests each configuration can serve per GPU. <strong>Higher bars = better value for money.</strong> A configuration with high efficiency means you get more throughput from each GPU you pay for.',
        arch: 'Side-by-side comparison of <strong>Aggregated</strong> (single pool of GPUs) vs <strong>PD</strong> (dedicated prefill and decode GPUs) architectures. Lower TTFT is better for responsiveness. Higher throughput means more users served.'
    };

    // TP Pareto chart → TP Calibration subtab
    secTP += chartCard('TP Calibration: Latency vs GPU Count', chartDesc.pareto, 'chart-pareto');

    // Scatter, efficiency, architecture → Configurations subtab
    secCfg += '<div class="charts-grid-2col">';
    secCfg += chartCard('Throughput vs Latency', chartDesc.scatter, 'chart-scatter');
    secCfg += chartCard('GPU Efficiency (req/s per GPU)', chartDesc.efficiency, 'chart-efficiency');
    secCfg += '</div>';
    secCfg += chartCard('Architecture Comparison', chartDesc.arch, 'chart-arch');

    // --- PD configurations TTFT + Throughput charts (one per percentile) ---
    if (data.all_results.filter(r => r.architecture === 'PD').length) {
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P90)',
            '<strong style="color:#3b82f6">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P90. The <strong style="color:#10b981">green point</strong> marks the best TTFT, the <strong style="color:#e11d48">pink point</strong> marks the best throughput.',
            'chart-pd-ttft-p90'
        );
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P95)',
            '<strong style="color:#3b82f6">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P95. Captures tail latency beyond P90.',
            'chart-pd-ttft-p95'
        );
        html += chartCard(
            'PD Configurations — TTFT & Throughput (P99)',
            '<strong style="color:#3b82f6">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P99. Shows worst-case tail latency.',
            'chart-pd-ttft-p99'
        );
    }

    // --- Pareto table ---
    if (charts.pareto.pareto_table.length) {
        html += '<div class="chart-card"><div class="chart-card-header">Pareto Optimal Configurations</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">These configurations represent the <strong>best possible trade-offs</strong>. Each one is optimal for a different balance of speed, throughput, and GPU cost. No other tested configuration beats any of these on all metrics at once.</div>';
        html += '<div class="chart-card-body" style="padding:0;">';
        html += '<table class="results-table"><tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>GPUs</th><th title="Throughput P90 ÷ Total GPUs (req/s per GPU). Higher = better cost-efficiency.">Efficiency<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th><th>Manifests</th></tr>';
        charts.pareto.pareto_table.forEach((p, idx) => {
            let manifestLinks = '-';
            const pTestId = p.test_id || testIdLookup[p.config_name] || p.config_name;
            const mTypes = manifestLookup[pTestId];
            if (mTypes && mTypes.length) {
                manifestLinks = mTypes.filter(t => !t.includes('service')).map(t => {
                    return `<a href="/api/run/${runId}/config/${pTestId}/manifest/${t}" title="Download ${t}.yaml" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${t}</a>`;
                }).join(' ');
            }
            const borderTop = idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
            const metrics = [
                {name: 'TTFT (ms)', p50: p.ttft_p50, p90: p.ttft_p90, p95: p.ttft_p95, p99: p.ttft_p99},
                {name: 'ITL (ms)', p50: p.itl_p50, p90: p.itl_p90, p95: p.itl_p95, p99: p.itl_p99},
                {name: 'Throughput (req/s)', p50: p.throughput_p50, p90: p.throughput_p90, p95: p.throughput_p95, p99: p.throughput_p99},
            ];
            metrics.forEach((m, mi) => {
                const rowBorder = mi === 0 && idx > 0 ? borderTop : '';
                html += `<tr class="pareto-row">`;
                if (mi === 0) {
                    html += `<td rowspan="3" style="vertical-align:middle; font-weight:700;${rowBorder}">${p.config_name}<br><span style="font-weight:400; font-size:0.85em; color:#64748b;">${p.architecture}</span></td>`;
                }
                html += `<td style="color:#64748b;${rowBorder}">${m.name}</td>`;
                html += `<td style="${rowBorder}">${m.p50 ?? '-'}</td>`;
                html += `<td style="${rowBorder}">${m.p90 ?? '-'}</td>`;
                html += `<td style="${rowBorder}">${m.p95 ?? '-'}</td>`;
                html += `<td style="${rowBorder}">${m.p99 ?? '-'}</td>`;
                if (mi === 0) {
                    html += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${p.gpus}</td>`;
                    html += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${p.efficiency}</td>`;
                    html += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${manifestLinks}</td>`;
                }
                html += '</tr>';
            });
        });
        html += '</table></div></div>';
    }

    // --- All results table ---
    if (data.all_results.length) {
        var allCfgTableId = 'all-configs-table-' + runId;
        html += '<div class="chart-card"><div class="chart-card-header">All Successful Configurations</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Complete results from every test that ran successfully. <strong>Green highlighted rows</strong> are Pareto optimal (the best trade-offs). Click any column header to sort.</div>';
        html += '<div class="chart-card-body" style="padding:0;">';
        html += '<table class="results-table" id="' + allCfgTableId + '"><tr>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',1,\'str\')">Architecture &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',2,\'num\')">TTFT P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',3,\'num\')">TTFT P95 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',4,\'num\')">TTFT P99 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',5,\'num\')">Tput P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',6,\'num\')">Tput P95 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',7,\'num\')">Tput P99 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',8,\'num\')">ITL P90 &#x21C5;</th>';
        html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',9,\'num\')">GPUs &#x21C5;</th>';
        html += '<th style="cursor:pointer;" title="Throughput P90 ÷ Total GPUs (req/s per GPU)" onclick="sortReportTable(\'' + allCfgTableId + '\',10,\'num\')">Efficiency &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th>';
        html += '<th>Manifests</th>';
        html += '</tr>';
        const paretoNames = new Set(charts.pareto.pareto_table.map(p => p.config_name));
        data.all_results.forEach((r, idx) => {
            const cls = paretoNames.has(r.config_name) ? ' class="pareto-row"' : '';
            const rTestId = r.test_id || testIdLookup[r.config_name] || r.config_name;
            let manifestLinks = '-';
            if (r.manifest_types && r.manifest_types.length > 0) {
                manifestLinks = r.manifest_types.filter(t => !t.includes('service')).map(t => {
                    return `<a href="/api/run/${runId}/config/${rTestId}/manifest/${t}" title="Download ${t}.yaml" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${t}</a>`;
                }).join(' ');
            }
            const na = 'N/A';
            html += `<tr${cls}><td>${r.config_name}</td><td>${r.architecture}</td><td data-val="${r.ttft_p90}">${r.ttft_p90}</td><td data-val="${r.ttft_p95 ?? ''}">${r.ttft_p95 ?? na}</td><td data-val="${r.ttft_p99 ?? ''}">${r.ttft_p99 ?? na}</td><td data-val="${r.throughput_p90}">${r.throughput_p90}</td><td data-val="${r.throughput_p95 ?? ''}">${r.throughput_p95 ?? na}</td><td data-val="${r.throughput_p99 ?? ''}">${r.throughput_p99 ?? na}</td><td data-val="${r.itl_p90 ?? ''}">${r.itl_p90 ?? na}</td><td data-val="${r.gpus}">${r.gpus}</td><td data-val="${r.efficiency}">${r.efficiency}</td><td>${manifestLinks}</td></tr>`;
        });
        html += '</table></div></div>';
    }

    // Flush configurations (PD charts + pareto table + all results)
    secCfg += html; html = '';

    // ============================================================
    // USER DEFINED TEST SETTINGS tab — run-level configuration
    // ============================================================
    if (data.run_config) {
        const rc = data.run_config;
        const na = 'N/A';
        const adv = rc.advanced_vllm || {};
        const advVal = (key, fallback) => { const s = adv[key]; return s && s.mode === 'custom' && s.value != null ? s.value : (fallback != null ? fallback : 'auto'); };
        const advToggle = (key, fallback) => { const s = adv[key]; return s ? (s.mode === 'on' ? 'On' : s.mode === 'off' ? 'Off' : fallback) : fallback; };

        html += '<div class="chart-card"><div class="chart-card-header">User Defined Test Settings</div>';
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">All settings configured for this optimization run. These apply to every test — only the architecture, TP values, and pod counts vary between tests.</div>';
        html += '<div class="chart-card-body" style="padding:16px 20px;">';
        html += '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:24px;font-size:0.9em;">';

        // Left column: Workload + Search Strategy
        html += '<div>';
        // Workload
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #10b981;padding-bottom:4px;">Workload</div><div style="line-height:2.2;margin-bottom:20px;">';
        html += `<div><span style="color:#64748b;">Model:</span> <strong>${rc.model_name || na}</strong></div>`;
        html += `<div><span style="color:#64748b;">ISL:</span> ${rc.isl}${rc.isl_stdev ? ' (&sigma;=' + rc.isl_stdev + ')' : ''}</div>`;
        html += `<div><span style="color:#64748b;">OSL:</span> ${rc.osl}${rc.osl_stdev ? ' (&sigma;=' + rc.osl_stdev + ')' : ''}</div>`;
        html += `<div><span style="color:#64748b;">Concurrent Users:</span> ${rc.qps != null ? Math.round(rc.qps) : na}</div>`;
        html += `<div><span style="color:#64748b;">Rate Type:</span> ${rc.rate_type || 'concurrent'}</div>`;
        html += `<div><span style="color:#64748b;">Test Duration:</span> ${rc.test_duration || 300}s</div>`;
        html += `<div><span style="color:#64748b;">Stop Mode:</span> ${rc.stop_mode || 'duration'}</div>`;
        if (rc.max_requests) html += `<div><span style="color:#64748b;">Max Requests:</span> ${rc.max_requests}</div>`;
        if (rc.turns > 1) html += `<div><span style="color:#64748b;">Turns:</span> ${rc.turns}</div>`;
        html += `<div><span style="color:#64748b;">Workload Mode:</span> ${rc.workload_mode || 'synthetic'}</div>`;
        if (rc.dataset_source) html += `<div><span style="color:#64748b;">Dataset:</span> <span style="word-break:break-all;">${rc.dataset_source}</span></div>`;
        if (rc.dataset_column) html += `<div><span style="color:#64748b;">Dataset Column:</span> ${rc.dataset_column}</div>`;
        if (rc.prefix_cache_hit_pct > 0) html += `<div><span style="color:#64748b;">Prefix Cache Hit:</span> ${rc.prefix_cache_hit_pct}%</div>`;
        html += '</div>';
        // Search Strategy
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #6366f1;padding-bottom:4px;">Search Strategy</div><div style="line-height:2.2;">';
        html += `<div><span style="color:#64748b;">Optimization Goal:</span> <strong>${(rc.objective || 'ttft').toUpperCase()}</strong></div>`;
        html += `<div><span style="color:#64748b;">Total GPUs:</span> ${rc.total_gpus || na}</div>`;
        html += `<div><span style="color:#64748b;">TP Options:</span> ${(rc.tp_options || []).join(', ') || na}</div>`;
        html += `<div><span style="color:#64748b;">TP Pair Breadth:</span> Top-${rc.tp_pair_top_n || 2}</div>`;
        html += `<div><span style="color:#64748b;">P/D Ratio Search:</span> ${rc.pd_search_mode === 'exhaustive' ? 'Exhaustive' : 'Smart'}</div>`;
        html += `<div><span style="color:#64748b;">Use Achievable QPS:</span> ${rc.use_achievable_qps ? 'Yes' : 'No'}</div>`;
        html += `<div><span style="color:#64748b;">Headroom:</span> ${rc.headroom || 1.3}x</div>`;
        if (rc.latency_constraint_enabled) {
            html += `<div><span style="color:#64748b;">Latency SLA:</span> ${rc.latency_constraint_ms}ms @ ${rc.latency_constraint_percentile}</div>`;
        } else {
            html += `<div><span style="color:#64748b;">Latency SLA:</span> Disabled</div>`;
        }
        html += '</div>';
        html += '</div>';

        // Right column: Infrastructure + Advanced vLLM Settings
        html += '<div>';
        // Infrastructure
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #f59e0b;padding-bottom:4px;">Infrastructure</div><div style="line-height:2.2;margin-bottom:20px;">';
        html += `<div><span style="color:#64748b;">Image:</span> <span style="word-break:break-all;font-size:0.9em;">${rc.image || na}</span></div>`;
        html += `<div><span style="color:#64748b;">Namespace:</span> ${rc.namespace || na}</div>`;
        html += `<div><span style="color:#64748b;">PVC:</span> ${rc.pvc_name || na}</div>`;
        html += `<div><span style="color:#64748b;">Network Type:</span> ${rc.network_type || na}</div>`;
        html += `<div><span style="color:#64748b;">NCCL IB HCA:</span> ${rc.nccl_ib_hca || na}</div>`;
        if (rc.rdma_nics_per_node) html += `<div><span style="color:#64748b;">RDMA NICs/Node:</span> ${rc.rdma_nics_per_node}</div>`;
        html += '</div>';
        // Advanced vLLM Settings
        html += '<div style="font-weight:700;color:#1e293b;margin-bottom:10px;border-bottom:2px solid #8b5cf6;padding-bottom:4px;">Advanced vLLM Settings</div><div style="line-height:2.2;">';
        html += `<div><span style="color:#64748b;">Max Model Len:</span> ${advVal('max_model_len', rc.max_model_len)}</div>`;
        html += `<div><span style="color:#64748b;">GPU Memory Utilization:</span> ${advVal('gpu_memory_utilization', rc.gpu_memory_utilization)}</div>`;
        html += `<div><span style="color:#64748b;">Block Size:</span> ${advVal('block_size', 'auto')}</div>`;
        html += `<div><span style="color:#64748b;">Max Num Seqs:</span> ${advVal('max_num_seqs', 'auto')}</div>`;
        html += `<div><span style="color:#64748b;">Max Batched Tokens:</span> ${advVal('max_num_batched_tokens', 'auto')}</div>`;
        html += `<div><span style="color:#64748b;">Dtype:</span> ${advVal('dtype', 'auto')}</div>`;
        html += `<div><span style="color:#64748b;">KV Cache Dtype:</span> ${advVal('kv_cache_dtype', 'auto')}</div>`;
        html += `<div><span style="color:#64748b;">Pipeline Parallel:</span> ${advVal('pipeline_parallel_size', 'auto')}</div>`;
        html += `<div><span style="color:#64748b;">Tool Call Parser:</span> ${advVal('tool_call_parser', 'auto')}</div>`;
        html += `<div><span style="color:#64748b;">Prefix Caching:</span> ${advToggle('enable_prefix_caching', 'On (auto)')}</div>`;
        html += `<div><span style="color:#64748b;">Custom All-Reduce:</span> ${advToggle('disable_custom_all_reduce', 'Enabled (auto)')}</div>`;
        html += `<div><span style="color:#64748b;">Trust Remote Code:</span> ${advToggle('trust_remote_code', 'On (auto)')}</div>`;
        html += `<div><span style="color:#64748b;">Disable Log Requests:</span> ${advToggle('disable_log_requests', 'On (auto)')}</div>`;
        html += `<div><span style="color:#64748b;">Auto Tool Choice:</span> ${advToggle('enable_auto_tool_choice', 'Off (auto)')}</div>`;
        html += '</div>';
        html += '</div>';

        html += '</div></div></div>';
        secTestCfg = html; html = '';
    }

    // ============================================================
    // STEP 8: Architecture Comparison (separate card)
    // Renders PD vs Agg, EP vs Agg, or both depending on goal
    // ============================================================
    if (rec && (rec.pd_vs_agg || rec.ep_vs_agg)) {
        // --- PD vs Aggregated ---
        if (rec.pd_vs_agg) {
            const cmp = rec.pd_vs_agg;
            const ttftColor = cmp.ttft_winner === 'PD' ? '#10b981' : '#f59e0b';
            const tputColor = cmp.throughput_winner === 'PD' ? '#10b981' : '#f59e0b';
            html += '<div class="chart-card" style="margin-top:16px; border:2px solid #6366f1; border-left:6px solid #6366f1;"><div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);">Step 8: PD vs Aggregated Comparison</div>';
            html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">The best PD configuration was tested head-to-head against an equivalent Aggregated deployment using the same GPU count and full workload. This validates whether PD disaggregation actually helps for this model.</div>';
            html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            html += '<tr><th>Metric</th><th>PD (best)</th><th>Aggregated</th><th>Winner</th></tr>';
            html += `<tr><td><strong>TTFT P90</strong></td><td>${cmp.pd.ttft_p90} ms</td><td>${cmp.aggregated.ttft_p90} ms</td><td style="color:${ttftColor}; font-weight:700;">${cmp.ttft_winner} (${cmp.ttft_diff_pct}% better)</td></tr>`;
            html += `<tr><td><strong>Throughput P90</strong></td><td>${cmp.pd.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_p90} req/s</td><td style="color:${tputColor}; font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
            html += '</table></div>';

            // --- % Change chart: All PD configs vs Aggregated baseline ---
            if (rec.aggregated_baseline && data.all_results && data.all_results.length > 1) {
                const aggBaseline = rec.aggregated_baseline;
                const baseTtft = aggBaseline.ttft_p90;
                const baseTput = aggBaseline.throughput_p90;
                const configs = data.all_results.filter(r => r.architecture === 'PD' && r.ttft_p90 && r.throughput_p90);
                if (configs.length && baseTtft && baseTput) {
                    html += '<div style="padding:16px 20px 4px;"><div style="font-weight:700; font-size:0.95em; color:#1e293b; margin-bottom:4px;">All PD Configurations vs Aggregated Baseline</div>';
                    html += '<div style="color:#1e293b; font-size:0.92em; margin-bottom:12px;">Percentage change in TTFT and Throughput relative to the Step 8 Aggregated baseline (' + aggBaseline.config_name + '). For TTFT, negative (green) is better. For Throughput, positive (green) is better.</div>';
                    var pdTableId = 'pd-vs-agg-table-' + runId;
                    html += '<div class="chart-card-body" style="padding:0;"><table class="results-table" id="' + pdTableId + '">';
                    html += '<tr>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',1,\'num\')">TTFT P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',2,\'num\')">TTFT vs Agg &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',3,\'num\')">Throughput P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + pdTableId + '\',4,\'num\')">Tput vs Agg &#x21C5;</th>';
                    html += '</tr>';
                    const sorted = [...configs].sort((a, b) => a.ttft_p90 - b.ttft_p90);
                    for (const cfg of sorted) {
                        const ttftPct = ((cfg.ttft_p90 - baseTtft) / baseTtft * 100).toFixed(1);
                        const tputPct = ((cfg.throughput_p90 - baseTput) / baseTput * 100).toFixed(1);
                        const ttftBetter = parseFloat(ttftPct) < 0;
                        const tputBetter = parseFloat(tputPct) > 0;
                        const ttftColor = ttftBetter ? '#059669' : '#dc2626';
                        const tputColor = tputBetter ? '#059669' : '#dc2626';
                        const ttftArrow = ttftBetter ? '&#9660;' : '&#9650;';
                        const tputArrow = tputBetter ? '&#9650;' : '&#9660;';
                        html += `<tr><td><strong>${cfg.config_name}</strong></td>`;
                        html += `<td data-val="${cfg.ttft_p90}">${cfg.ttft_p90} ms</td>`;
                        html += `<td data-val="${ttftPct}" style="color:${ttftColor}; font-weight:700;">${ttftArrow} ${ttftPct}%</td>`;
                        html += `<td data-val="${cfg.throughput_p90}">${cfg.throughput_p90} req/s</td>`;
                        html += `<td data-val="${tputPct}" style="color:${tputColor}; font-weight:700;">${tputArrow} ${tputPct}%</td></tr>`;
                    }
                    html += `<tr class="baseline-row" style="background:#f1f5f9;"><td><strong>${aggBaseline.config_name}</strong> <span style="background:#1f77b4; color:white; font-size:0.65em; padding:1px 5px; border-radius:3px;">BASELINE</span></td>`;
                    html += `<td data-val="${baseTtft}">${baseTtft} ms</td><td data-val="0" style="color:#64748b;">-</td>`;
                    html += `<td data-val="${baseTput}">${baseTput} req/s</td><td data-val="0" style="color:#64748b;">-</td></tr>`;
                    html += '</table></div></div>';
                }
            }
            html += '</div>';
        }

        // --- EP vs Aggregated ---
        if (rec.ep_vs_agg) {
            const cmp = rec.ep_vs_agg;
            const ttftColor = cmp.ttft_winner === 'EP' ? '#10b981' : '#f59e0b';
            const tputColor = cmp.throughput_winner === 'EP' ? '#10b981' : '#f59e0b';
            html += '<div class="chart-card" style="margin-top:16px; border:2px solid #6366f1; border-left:6px solid #6366f1;"><div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);">Step 8: EP vs Aggregated Comparison</div>';
            html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">The best EP (Expert Parallelism) configuration was tested head-to-head against an equivalent Aggregated deployment using the same GPU count and full workload. EP uses EPLB (expert-level prefill load balancing) to distribute work across independent pods.</div>';
            html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            html += '<tr><th>Metric</th><th>EP (best)</th><th>Aggregated</th><th>Winner</th></tr>';
            html += `<tr><td><strong>TTFT P90</strong></td><td>${cmp.ep.ttft_p90} ms</td><td>${cmp.aggregated.ttft_p90} ms</td><td style="color:${ttftColor}; font-weight:700;">${cmp.ttft_winner} (${cmp.ttft_diff_pct}% better)</td></tr>`;
            html += `<tr><td><strong>Throughput P90</strong></td><td>${cmp.ep.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_p90} req/s</td><td style="color:${tputColor}; font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
            html += '</table></div>';

            // --- % Change chart: All EP configs vs Aggregated baseline ---
            if (rec.aggregated_baseline && rec.ep_all_configs && rec.ep_all_configs.length > 0) {
                const aggBaseline = rec.aggregated_baseline;
                const baseTtft = aggBaseline.ttft_p90;
                const baseTput = aggBaseline.throughput_p90;
                if (baseTtft && baseTput) {
                    html += '<div style="padding:16px 20px 4px;"><div style="font-weight:700; font-size:0.95em; color:#1e293b; margin-bottom:4px;">All EP Configurations vs Aggregated Baseline</div>';
                    html += '<div style="color:#1e293b; font-size:0.92em; margin-bottom:12px;">Percentage change in TTFT and Throughput relative to the Step 8 Aggregated baseline. For TTFT, negative (green) is better. For Throughput, positive (green) is better.</div>';
                    var epTableId = 'ep-vs-agg-table-' + runId;
                    html += '<div class="chart-card-body" style="padding:0;"><table class="results-table" id="' + epTableId + '">';
                    html += '<tr>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',1,\'num\')">TTFT P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',2,\'num\')">TTFT vs Agg &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',3,\'num\')">Throughput P90 &#x21C5;</th>';
                    html += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + epTableId + '\',4,\'num\')">Tput vs Agg &#x21C5;</th>';
                    html += '</tr>';
                    const sorted = [...rec.ep_all_configs].sort((a, b) => (b.throughput_p90||0) - (a.throughput_p90||0));
                    for (const cfg of sorted) {
                        if (!cfg.ttft_p90 || !cfg.throughput_p90) continue;
                        const ttftPct = ((cfg.ttft_p90 - baseTtft) / baseTtft * 100).toFixed(1);
                        const tputPct = ((cfg.throughput_p90 - baseTput) / baseTput * 100).toFixed(1);
                        const ttftBetter = parseFloat(ttftPct) < 0;
                        const tputBetter = parseFloat(tputPct) > 0;
                        const ttftColor = ttftBetter ? '#059669' : '#dc2626';
                        const tputColor = tputBetter ? '#059669' : '#dc2626';
                        const ttftArrow = ttftBetter ? '&#9660;' : '&#9650;';
                        const tputArrow = tputBetter ? '&#9650;' : '&#9660;';
                        const label = `EP TP${cfg.tp} x ${cfg.replicas} replicas`;
                        html += `<tr><td><strong>${label}</strong></td>`;
                        html += `<td data-val="${cfg.ttft_p90}">${cfg.ttft_p90} ms</td>`;
                        html += `<td data-val="${ttftPct}" style="color:${ttftColor}; font-weight:700;">${ttftArrow} ${ttftPct}%</td>`;
                        html += `<td data-val="${cfg.throughput_p90}">${cfg.throughput_p90} req/s</td>`;
                        html += `<td data-val="${tputPct}" style="color:${tputColor}; font-weight:700;">${tputArrow} ${tputPct}%</td></tr>`;
                    }
                    html += `<tr class="baseline-row" style="background:#f1f5f9;"><td><strong>${aggBaseline.config_name}</strong> <span style="background:#1f77b4; color:white; font-size:0.65em; padding:1px 5px; border-radius:3px;">BASELINE</span></td>`;
                    html += `<td data-val="${baseTtft}">${baseTtft} ms</td><td data-val="0" style="color:#64748b;">-</td>`;
                    html += `<td data-val="${baseTput}">${baseTput} req/s</td><td data-val="0" style="color:#64748b;">-</td></tr>`;
                    html += '</table></div></div>';
                }
            }
            html += '</div>';
        }
    }

    // Flush comparison (Step 8)
    secCmp = html; html = '';

    // ============================================================
    // STEP 9: Latency-Bounded Throughput Search
    // Binary search over concurrency to find max throughput under SLA
    // ============================================================
    if (data.latency_search && data.latency_search.trials && data.latency_search.trials.length) {
        const ls = data.latency_search;
        const byArch = ls.by_architecture || {};
        const archKeys = Object.keys(byArch);

        // Get SLA target from first trial
        const firstTrial = ls.trials[0];
        const targetMs = firstTrial.target_ms;
        const targetPct = firstTrial.target_percentile || 'p90';
        const metricKey = 'ttft_' + targetPct;

        html += '<div class="chart-card" style="margin-top:16px; border:2px solid #8b5cf6; border-left:6px solid #8b5cf6;">';
        html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">Step 9: Latency-Bounded Throughput Search</div>';
        html += `<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Binary search over concurrency to find the maximum throughput that keeps TTFT ${targetPct.toUpperCase()} under <strong>${targetMs} ms</strong>.</div>`;

        const archConfigs = ls.arch_configs || {};

        // Summary cards per architecture
        archKeys.forEach((arch, ai) => {
            const trials = byArch[arch];
            // Skip architectures with no valid data
            if (!trials.some(t => t.ttft_p90 != null)) return;
            const passing = trials.filter(t => t.meets_sla);
            const bestPassing = passing.length ? passing.reduce((a, b) => a.concurrency > b.concurrency ? a : b) : null;
            const cfgLabel = archConfigs[arch] || arch.toUpperCase();

            html += `<div style="padding:12px 20px; margin-top:4px;">`;
            html += `<div style="font-weight:700; font-size:1.05em; color:#1e293b; margin-bottom:10px; border-bottom:1px solid #e2e8f0; padding-bottom:6px;">${arch.toUpperCase()}: ${cfgLabel}</div>`;
            if (bestPassing) {
                const latVal = bestPassing[metricKey] != null ? bestPassing[metricKey].toFixed(1) : '-';
                const s9TputKey = 'throughput_' + targetPct;
                const tputVal = bestPassing[s9TputKey] != null ? bestPassing[s9TputKey].toFixed(2) : (bestPassing.throughput_p90 != null ? bestPassing.throughput_p90.toFixed(2) : '-');
                html += '<div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin-bottom:16px;">';
                html += `<div style="background:#f0fdf4; border-radius:10px; padding:16px; text-align:center; border:1px solid #bbf7d0;"><div style="font-size:2em; font-weight:800; color:#059669;">${bestPassing.concurrency}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Optimal Concurrency</div></div>`;
                html += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:2em; font-weight:800; color:#1e293b;">${latVal} <span style="font-size:0.5em; color:#64748b;">ms</span></div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">TTFT ${targetPct.toUpperCase()} at Optimal</div></div>`;
                html += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:2em; font-weight:800; color:#1e293b;">${tputVal}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Throughput ${targetPct.toUpperCase()} (req/s)</div></div>`;
                html += `<div style="background:#f8fafc; border-radius:10px; padding:16px; text-align:center; border:1px solid #e2e8f0;"><div style="font-size:2em; font-weight:800; color:#1e293b;">${trials.length}</div><div style="color:#64748b; font-size:0.82em; margin-top:4px;">Tests Run</div></div>`;
                html += '</div>';
            } else {
                html += `<div style="padding:10px 14px; background:#fef2f2; border-radius:8px; color:#991b1b; font-size:0.9em; border:1px solid #fecaca; margin-bottom:12px;">No concurrency level met the SLA target of ${targetMs} ms</div>`;
            }

            // Per-percentile charts (P90, P95, P99)
            html += `<div id="step9-chart-p90-${ai}${_chartSuffix}" style="height:500px; background:#fff; border-radius:8px; border:1px solid #e2e8f0;"></div>`;
            html += `<div id="step9-chart-p95-${ai}${_chartSuffix}" style="height:500px; background:#fff; border-radius:8px; border:1px solid #e2e8f0; margin-top:16px;"></div>`;
            html += `<div id="step9-chart-p99-${ai}${_chartSuffix}" style="height:500px; background:#fff; border-radius:8px; border:1px solid #e2e8f0; margin-top:16px;"></div>`;
            // Legacy div for backward compat (hidden)
            html += `<div id="step9-chart-${ai}${_chartSuffix}" style="display:none;"></div>`;

            // Cost table
            const archTrials = byArch[arch];
            const sortedTrials = [...archTrials].sort((a, b) => a.concurrency - b.concurrency);
            html += '<div style="margin-top:12px; overflow-x:auto;"><table class="results-table" style="font-size:0.85em;">';
            html += '<tr><th>Concurrency</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Throughput P90</th><th>Meets SLA</th><th>Manifests</th></tr>';
            sortedTrials.forEach(t => {
                const slaStyle = t.meets_sla ? 'color:#059669; font-weight:700;' : 'color:#dc2626; font-weight:700;';
                let mLinks = '-';
                if (t.manifest_types && t.manifest_types.length && t.test_id) {
                    mLinks = t.manifest_types.filter(mt => !mt.includes('service')).map(mt =>
                        `<a href="/api/run/${runId}/config/${t.test_id}/manifest/${mt}" title="Download ${mt}.yaml" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${mt}</a>`
                    ).join(' ');
                }
                html += `<tr><td style="font-weight:700;">${t.concurrency}</td>`;
                html += `<td>${t.ttft_p50 != null ? t.ttft_p50.toFixed(1) + ' ms' : '-'}</td>`;
                html += `<td>${t.ttft_p90 != null ? t.ttft_p90.toFixed(1) + ' ms' : '-'}</td>`;
                html += `<td>${t.ttft_p95 != null ? t.ttft_p95.toFixed(1) + ' ms' : '-'}</td>`;
                html += `<td>${t.ttft_p99 != null ? t.ttft_p99.toFixed(1) + ' ms' : '-'}</td>`;
                html += `<td>${t.throughput_p90 != null ? t.throughput_p90.toFixed(2) + ' req/s' : '-'}</td>`;
                html += `<td style="${slaStyle}">${t.meets_sla ? 'Yes' : 'No'}</td>`;
                html += `<td>${mLinks}</td></tr>`;
            });
            html += '</table></div>';

            html += '</div>';
        });

        // Trials table
        html += '<div style="padding:12px 20px 4px; font-weight:700; font-size:0.95em; color:#1e293b; border-top:1px solid #e2e8f0; margin-top:8px;">All Trials</div>';
        html += '<div class="chart-card-body" style="padding:0 20px 16px;"><table class="results-table">';
        const s9TblTputKey = 'throughput_' + targetPct;
        html += `<tr><th>Arch</th><th>#</th><th>Phase</th><th>Concurrency</th><th>TTFT ${targetPct.toUpperCase()}</th><th>Throughput ${targetPct.toUpperCase()}</th><th>Meets SLA</th><th>Manifests</th></tr>`;
        ls.trials.forEach(t => {
            const latVal = t[metricKey] != null ? t[metricKey].toFixed(1) + ' ms' : '-';
            const s9TputRaw = t[s9TblTputKey] != null ? t[s9TblTputKey] : t.throughput_p90;
            const tputVal = s9TputRaw != null ? s9TputRaw.toFixed(2) + ' req/s' : '-';
            const slaStyle = t.meets_sla ? 'color:#059669; font-weight:700;' : 'color:#dc2626; font-weight:700;';
            const slaText = t.meets_sla ? 'Yes' : 'No';
            let mLinks = '-';
            if (t.manifest_types && t.manifest_types.length && t.test_id) {
                mLinks = t.manifest_types.filter(mt => !mt.includes('service')).map(mt =>
                    `<a href="/api/run/${runId}/config/${t.test_id}/manifest/${mt}" title="Download ${mt}.yaml" style="color:#0ea5e9; text-decoration:none; font-size:12px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${mt}</a>`
                ).join(' ');
            }
            html += `<tr><td>${t.architecture}</td><td>${t.trial_number}</td><td>${t.search_phase}</td>`;
            html += `<td style="font-weight:700;">${t.concurrency}</td>`;
            html += `<td>${latVal}</td><td>${tputVal}</td>`;
            html += `<td style="${slaStyle}">${slaText}</td>`;
            html += `<td>${mLinks}</td></tr>`;
        });
        html += '</table></div></div>';
    }

    // Flush Step 9
    secStep9 = html; html = '';

    // ============================================================
    // STEP 10: Calibrated Load Validation (separate card)
    // Handles PD, EP, or both depending on goal
    // ============================================================
    if (data.calibrated_qps) {
        const cal = data.calibrated_qps;
        // Determine primary architecture (PD or EP)
        const primary = cal.pd || cal.ep;
        const primaryKey = cal.pd ? 'pd' : 'ep';
        const primaryLabel = cal.pd ? 'PD' : 'EP';

        html += '<div class="chart-card" style="margin-top:16px; border:2px solid #059669; border-left:6px solid #059669;"><div class="chart-card-header" style="background:linear-gradient(135deg,#059669,#10b981);">Step 10: Calibrated Load Validation</div>';
        // Capacity info with math breakdown
        if (cal.gpu_sizing) {
            const s = cal.gpu_sizing;
            html += '<div style="padding:12px 20px; background:#ecfdf5; border-bottom:1px solid #6ee7b7; font-size:0.9em; color:#065f46;">';
            html += `<div style="font-weight:700; margin-bottom:8px;">📊 Cluster Capacity Analysis</div>`;
            html += '<table style="width:auto; margin:0; font-size:0.95em; border:none;">';
            html += '<tr style="background:none;"><td style="border:none; padding:2px 16px 2px 0; color:#047857;"><strong>GPU Cost per Request</strong></td><td style="border:none; padding:2px 0;"></td></tr>';
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Prefill</td><td style="border:none; padding:1px 0;">${s.isl} ISL ÷ ${s.prefill_tpsg} TPSG = <strong>${s.prefill_cost} GPU-sec</strong></td></tr>`;
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Decode</td><td style="border:none; padding:1px 0;">${s.osl} OSL ÷ ${s.decode_tpsg} TPSG = <strong>${s.decode_cost} GPU-sec</strong></td></tr>`;
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Total</td><td style="border:none; padding:1px 0;"><strong>${s.total_cost} GPU-sec/request</strong></td></tr>`;
            html += '<tr style="background:none;"><td style="border:none; padding:6px 16px 2px 0; color:#047857;"><strong>Sustainable Throughput</strong></td><td style="border:none; padding:6px 0 2px;"></td></tr>';
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Cluster capacity</td><td style="border:none; padding:1px 0;">${s.total_gpus} GPUs ÷ ${s.total_cost} GPU-sec ÷ ${s.headroom}x headroom = <strong>${s.sustainable_throughput_rps || s.sustainable_qps} req/s</strong> (${s.sustainable_concurrency || '?'} concurrent users)</td></tr>`;
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Concurrency tested</td><td style="border:none; padding:1px 0;"><strong>${s.concurrency} simultaneous requests</strong></td></tr>`;
            html += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Ideal P/D ratio</td><td style="border:none; padding:1px 0;"><strong>${s.ideal_prefill_pct}% prefill</strong></td></tr>`;
            html += '</table></div>';
        } else if (cal.total_gpus_available && cal.requested_rps) {
            html += `<div style="padding:10px 20px; background:#ecfdf5; border-bottom:1px solid #6ee7b7; font-size:0.9em; color:#065f46;">📊 Cluster can sustain <strong>${cal.requested_rps} req/s</strong> with <strong>${cal.total_gpus_available} GPUs</strong>.</div>`;
        }
        html += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Steps 7-8 ran at the original user-requested concurrency which exceeded cluster capacity. This step re-tested the best configurations at a sustainable load to show realistic latency and throughput.</div>';

        // --- Table 1: Percentile Breakdown at Calibrated Load ---
        const isBalanced = !!(cal.pd && cal.ep);
        const requestedRps = cal.requested_rps != null ? cal.requested_rps : null;
        const rpsLabel = requestedRps != null ? ` at ${Math.round(requestedRps)} concurrent` : '';

        // Collect entries
        const calEntries = [];
        if (cal.pd) calEntries.push({label: 'PD', entry: cal.pd});
        if (cal.aggregated) calEntries.push({label: 'Aggregated', entry: cal.aggregated});
        if (isBalanced && cal.ep) calEntries.push({label: 'EP', entry: cal.ep});

        const tableTitle = calEntries.length > 1
            ? 'Percentile Breakdown: ' + calEntries.map(e => e.label).join(' vs ') + rpsLabel
            : 'Percentile Breakdown' + rpsLabel;

        html += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b;">${tableTitle}</div>`;
        html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
        html += '<tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';

        // Helper: find best P90 value per metric for highlighting
        function findBest(metric, lowerIsBetter) {
            const vals = calEntries.map(e => e.entry[metric]).filter(v => v != null);
            if (!vals.length) return null;
            return lowerIsBetter ? Math.min(...vals) : Math.max(...vals);
        }
        const bestTtft = findBest('ttft_p90', true);
        const bestTput = findBest('throughput_p90', false);
        const bestItl = findBest('itl_p90', true);
        const hl = (val, best) => val != null && val === best ? 'color:#059669; font-weight:700;' : '';

        const fmt = (v, unit) => v != null ? `${v} ${unit}` : '-';

        calEntries.forEach(({label, entry}, idx) => {
            const metrics = [
                {name: 'TTFT (ms)', p50: entry.ttft_p50, p90: entry.ttft_p90, p95: entry.ttft_p95, p99: entry.ttft_p99, best: bestTtft, p90key: 'ttft_p90', unit: ''},
                {name: 'ITL (ms)', p50: entry.itl_p50, p90: entry.itl_p90, p95: entry.itl_p95, p99: entry.itl_p99, best: bestItl, p90key: 'itl_p90', unit: ''},
                {name: 'Throughput (req/s)', p50: entry.throughput_p50, p90: entry.throughput_p90, p95: entry.throughput_p95, p99: entry.throughput_p99, best: bestTput, p90key: 'throughput_p90', unit: ''},
            ];
            metrics.forEach((m, mi) => {
                const borderStyle = mi === 0 && idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
                const rowspan = mi === 0 ? ` rowspan="3" style="vertical-align:middle; font-weight:700;${borderStyle}"` : '';
                html += '<tr>';
                if (mi === 0) html += `<td${rowspan}>${label}</td>`;
                html += `<td style="color:#64748b;${borderStyle}">${m.name}</td>`;
                html += `<td style="${borderStyle}">${m.p50 != null ? m.p50 : '-'}</td>`;
                html += `<td style="${hl(entry[m.p90key], m.best)}${borderStyle}">${m.p90 != null ? m.p90 : '-'}</td>`;
                html += `<td style="${borderStyle}">${m.p95 != null ? m.p95 : '-'}</td>`;
                html += `<td style="${borderStyle}">${m.p99 != null ? m.p99 : '-'}</td>`;
                html += '</tr>';
            });
        });
        html += '</table></div>';

        // --- Table 2: Overload Impact ---
        const overloadKey = cal.overloaded_pd ? 'overloaded_pd' : (cal.overloaded_ep ? 'overloaded_ep' : null);
        const overloadData = overloadKey ? cal[overloadKey] : null;
        if (overloadData) {
            const origConcurrency = cal.concurrency != null ? `${cal.concurrency} concurrent` : '-';
            const calConcurrency = requestedRps != null ? `${Math.round(requestedRps)} concurrent` : '-';
            html += `<div style="padding:8px 20px 2px; font-weight:700; font-size:0.9em; color:#1e293b; margin-top:8px;">Overload Impact: ${primaryLabel} at Calibrated vs Overloaded Load</div>`;
            html += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
            html += '<tr><th>Configuration</th><th>Load</th><th>TTFT P90</th><th>Throughput P90</th></tr>';
            html += `<tr><td><strong>${primaryLabel} (calibrated)</strong></td><td>${calConcurrency}</td><td style="color:#059669; font-weight:700;">${primary.ttft_p90} ms</td><td style="color:#059669; font-weight:700;">${primary.throughput_p90} req/s</td></tr>`;
            html += `<tr><td><strong>${primaryLabel} (overloaded)</strong></td><td>${origConcurrency}</td><td style="color:#94a3b8;">${overloadData.ttft_p90} ms</td><td style="color:#94a3b8;">${overloadData.throughput_p90} req/s</td></tr>`;
            html += '</table></div>';
        }
        html += '</div>';
    }

    // Flush calibrated load (Step 10)
    secCal = html; html = '';

    // ============================================================
    // vLLM ENGINE METRICS CHARTS
    // ============================================================
    if (charts.vllm && charts.vllm.configs.length) {
        html += '<div class="chart-card" style="margin-top:24px; border-left:6px solid #8b5cf6;">';
        html += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#6366f1); color:white; font-size:1.2em;">vLLM Engine Metrics</div>';
        html += '<div style="padding:8px 20px; color:#1e293b; font-size:0.95em;">Prometheus metrics collected from the vLLM engine during each test. These metrics show engine-level behavior across all configurations — latency distributions, token throughput, request queuing, and processing time breakdown.</div>';
        html += '</div>';

        html += '<div class="charts-grid-2col">';
        html += chartCard(
            'vLLM TTFT Percentiles',
            'Time-to-First-Token as reported by the vLLM engine histogram (averaged over the test window). Lower bars mean faster first-token delivery. Compare P50 (typical) vs P99 (worst-case) across configurations.',
            'chart-vllm-ttft'
        );
        html += chartCard(
            'vLLM ITL Percentiles',
            'Inter-Token Latency from vLLM engine histograms. This is the delay between consecutive generated tokens — it determines how "smooth" streaming feels to the user. Lower is better.',
            'chart-vllm-itl'
        );
        html += '</div>';

        html += '<div class="charts-grid-2col">';
        html += chartCard(
            'vLLM E2E Request Latency',
            'End-to-end request latency from vLLM (includes TTFT + all token generation). Shows the full time a request spends in the engine. Compare tail latency (P99) across configurations to spot saturation.',
            'chart-vllm-e2e'
        );
        html += chartCard(
            'Token Throughput',
            'Average prompt (input) and generation (output) token processing rates across all pods. Higher bars = more tokens processed per second. Generation rate directly impacts how many users can be served concurrently.',
            'chart-vllm-tokens'
        );
        html += '</div>';

        html += '<div class="charts-grid-2col">';
        html += chartCard(
            'Request Queue & KV Cache',
            'Average concurrent requests running and waiting in queue, plus KV cache utilization (%). High waiting counts indicate the engine is saturated. High KV cache usage means the model is near memory capacity.',
            'chart-vllm-queue'
        );
        html += chartCard(
            'Processing Time & Preemptions',
            'How engine time is split between prefill (prompt processing), decode (token generation), and queuing. Preemptions show how often the engine evicts running requests to make room — high preemptions indicate memory pressure.',
            'chart-vllm-time'
        );
        html += '</div>';

        // Network throughput row
        if (charts.vllm.network && charts.vllm.network.pod_tx.some(v => v > 0)) {
            const hasIB = charts.vllm.network.ib_rx.some(v => v > 0);
            html += '<div class="charts-grid-2col">';
            html += chartCard(
                'Pod Network Throughput',
                'Average network transmit (TX) and receive (RX) rates aggregated across all pods in each configuration. Higher TX indicates more data being sent to clients (generated tokens). Higher RX reflects incoming requests and model weight loading.',
                'chart-net-pod'
            );
            if (hasIB) {
                html += chartCard(
                    'InfiniBand RDMA Throughput',
                    'InfiniBand receive throughput across pods. In PD configurations this captures KV cache transfer from prefill to decode pods over RDMA. Higher values indicate more data flowing through the high-speed interconnect.',
                    'chart-net-ib'
                );
            }
            html += '</div>';
        }
    }

    // Flush vLLM metrics
    secVLLM = html; html = '';

    // === Build Estimator section ===
    const wl = data.recommendation ? data.recommendation.workload : {};
    const estSuffix = _chartSuffix;
    let secEst = '<div class="chart-card" style="margin-top:16px; border:2px solid #d97706; border-left:6px solid #d97706;">' +
        '<div class="chart-card-header" style="background:linear-gradient(135deg,#d97706,#b45309);">GPU Estimator</div>' +
        '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">' +
            'Estimate how many GPUs each tested configuration would need for a different workload. ' +
            'Tested with <strong>' + (wl.users || '?') + ' users</strong>, ISL <strong>' + (wl.isl || '?') + '</strong>, OSL <strong>' + (wl.osl || '?') + '</strong>.' +
        '</div>' +
        '<div style="padding:12px 20px 0;">' +
            '<div class="estimator-form">' +
                '<div class="estimator-row">' +
                    '<div class="estimator-group">' +
                        '<div class="estimator-group-label">Workload</div>' +
                        '<label class="estimator-field">' +
                            '<span>Concurrency</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#128101;</span><input type="number" id="est-concurrency' + estSuffix + '" value="' + (wl.users || 100) + '" min="1"><span class="estimator-unit">users</span></div>' +
                        '</label>' +
                        '<label class="estimator-field">' +
                            '<span>ISL</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#128229;</span><input type="number" id="est-isl' + estSuffix + '" value="' + (wl.isl || 1024) + '" min="1"><span class="estimator-unit">tokens</span></div>' +
                        '</label>' +
                        '<label class="estimator-field">' +
                            '<span>OSL</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#128228;</span><input type="number" id="est-osl' + estSuffix + '" value="' + (wl.osl || 256) + '" min="1"><span class="estimator-unit">tokens</span></div>' +
                        '</label>' +
                    '</div>' +
                    '<div class="estimator-group">' +
                        '<div class="estimator-group-label">Variance</div>' +
                        '<label class="estimator-field">' +
                            '<span>ISL StdDev</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#177;</span><input type="number" id="est-isl-stdev' + estSuffix + '" value="' + (wl.isl_stdev || 0) + '" min="0"><span class="estimator-unit">tokens</span></div>' +
                        '</label>' +
                        '<label class="estimator-field">' +
                            '<span>OSL StdDev</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#177;</span><input type="number" id="est-osl-stdev' + estSuffix + '" value="' + (wl.osl_stdev || 0) + '" min="0"><span class="estimator-unit">tokens</span></div>' +
                        '</label>' +
                    '</div>' +
                    '<div class="estimator-group">' +
                        '<div class="estimator-group-label">' +
                            '<label style="cursor:pointer;display:flex;align-items:center;gap:6px;">' +
                                '<input type="checkbox" id="est-turns-toggle' + estSuffix + '"' + ((wl.turns || 1) > 1 ? ' checked' : '') + ' onchange="document.getElementById(\'est-turns' + estSuffix + '\').disabled=!this.checked;"> Multi-turn' +
                            '</label>' +
                        '</div>' +
                        '<label class="estimator-field">' +
                            '<span>Turns</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#128260;</span><input type="number" id="est-turns' + estSuffix + '" value="' + (wl.turns || 1) + '" min="1"' + ((wl.turns || 1) <= 1 ? ' disabled' : '') + '><span class="estimator-unit">per user</span></div>' +
                        '</label>' +
                    '</div>' +
                    '<div class="estimator-group">' +
                        '<div class="estimator-group-label">' +
                            '<label style="cursor:pointer;display:flex;align-items:center;gap:6px;">' +
                                '<input type="checkbox" id="est-sla-toggle' + estSuffix + '" onchange="document.getElementById(\'est-sla-ms' + estSuffix + '\').disabled=!this.checked;document.getElementById(\'est-sla-pctl' + estSuffix + '\').disabled=!this.checked;"> Latency SLA' +
                            '</label>' +
                        '</div>' +
                        '<label class="estimator-field">' +
                            '<span>Target TTFT</span>' +
                            '<div class="estimator-input-wrap"><span class="estimator-icon">&#9201;</span><input type="number" id="est-sla-ms' + estSuffix + '" value="500" min="1" disabled><span class="estimator-unit">ms</span></div>' +
                        '</label>' +
                        '<label class="estimator-field">' +
                            '<span>Percentile</span>' +
                            '<select id="est-sla-pctl' + estSuffix + '" disabled style="padding:10px 12px;border:2px solid #e2e8f0;border-radius:10px;font-size:0.95em;background:#f8fafc;">' +
                                '<option value="p90">P90</option><option value="p95">P95</option><option value="p99" selected>P99</option>' +
                            '</select>' +
                        '</label>' +
                    '</div>' +
                '</div>' +
            '</div>' +
            '<div id="est-scaling-info' + estSuffix + '" style="background:#fffbeb;border-top:1px solid #fde68a;border-bottom:1px solid #fde68a;padding:14px 20px;margin:0 -20px;font-size:0.9em;color:#92400e;width:calc(100% + 40px);"></div>' +
            '<div style="padding:0 20px;"><button class="action-button" onclick="runEstimator(\'' + estSuffix + '\')" style="margin-top:16px;padding:10px 28px;font-size:0.95em;border-radius:8px;background:linear-gradient(135deg,#d97706,#b45309);border:none;color:white;cursor:pointer;font-weight:600;box-shadow:0 2px 8px rgba(217,119,6,0.3);">Estimate</button></div>' +
            '<div id="est-results' + estSuffix + '"></div>' +
            '<div id="est-chart' + estSuffix + '" style="width:100%;height:400px;margin-top:16px;"></div>' +
        '</div></div>';

    // === Build subtab structure ===
    const subtabDefs = [];
    // Build EPP Tuning section from data.epp_tuning
    if (data.epp_tuning && data.epp_tuning.length > 0) {
        let eppHtml = '<div class="chart-card" style="border:2px solid #7c3aed;border-left:6px solid #7c3aed;">';
        eppHtml += '<div class="chart-card-header" style="background:linear-gradient(135deg,#7c3aed,#8b5cf6);">Step 11: EPP Tuning Results</div>';
        eppHtml += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Same deployment, different EPP scoring weights. Each test swapped only the gateway configmap (~10s) to isolate the impact of request routing on performance.</div>';
        eppHtml += '<div class="chart-card-body" style="padding:0;"><table class="results-table">';
        eppHtml += '<tr><th>Strategy</th><th>Weights (Cache : KV : Queue)</th><th>TTFT P50</th><th>TTFT P90</th><th>Throughput P90</th><th>ITL P90</th><th>EPP Config</th></tr>';
        let bestTtft = Math.min(...data.epp_tuning.map(e => e.ttft_p90 || Infinity));
        data.epp_tuning.forEach(e => {
            const isBest = e.ttft_p90 === bestTtft;
            const cls = isBest ? ' class="pareto-row"' : '';
            const w = e.weights || {};
            const weights = `${w.prefix_cache || '?'} : ${w.kv_cache || '?'} : ${w.queue || '?'}`;
            const na = 'N/A';
            let manifestLinks = '-';
            if (e.manifest_types && e.manifest_types.length > 0) {
                const rTestId = e.test_id || e.name;
                manifestLinks = e.manifest_types.map(t => {
                    return `<a href="/api/run/${runId}/config/${rTestId}/manifest/${t}" title="Download ${t}.yaml" style="color:#7c3aed; text-decoration:none; font-size:12px; padding:2px 6px; background:#f5f3ff; border-radius:4px; border:1px solid #c4b5fd; margin:1px; display:inline-block;">${t}</a>`;
                }).join(' ');
            }
            eppHtml += `<tr${cls}><td><strong>${e.name}</strong>${isBest ? ' ⭐' : ''}</td><td>${weights}</td>`;
            eppHtml += `<td>${e.ttft_p50 != null ? e.ttft_p50 : na}</td>`;
            eppHtml += `<td>${e.ttft_p90 != null ? e.ttft_p90 : na}</td>`;
            eppHtml += `<td>${e.throughput_p90 != null ? e.throughput_p90 : na}</td>`;
            eppHtml += `<td>${e.itl_p90 != null ? e.itl_p90 : na}</td>`;
            eppHtml += `<td>${manifestLinks}</td></tr>`;
        });
        eppHtml += '</table></div></div>';
        secEppTuning = eppHtml;
    }

    if (secRec) subtabDefs.push({ id: 'recommendation', label: 'Recommendation', icon: '&#9733;' });
    if (secTP) subtabDefs.push({ id: 'tp-calibration', label: 'TP Calibration', icon: '&#9881;' });
    if (secCfg) subtabDefs.push({ id: 'configurations', label: 'Configurations', icon: '&#9776;' });
    if (secCmp) subtabDefs.push({ id: 'comparison', label: 'Comparison', icon: '&#8596;' });
    if (secStep9) subtabDefs.push({ id: 'latency-search', label: 'Latency Search', icon: '&#128269;' });
    if (secCal) subtabDefs.push({ id: 'calibrated-load', label: 'Calibrated Load', icon: '&#9878;' });
    if (secVLLM) subtabDefs.push({ id: 'vllm-metrics', label: 'vLLM Metrics', icon: '&#9889;' });
    if (secEppTuning) subtabDefs.push({ id: 'epp-tuning', label: 'EPP Tuning', icon: '&#9881;' });
    subtabDefs.push({ id: 'estimator', label: 'Estimator', icon: '&#128200;' });
    if (secTestCfg) subtabDefs.push({ id: 'test-settings', label: 'Test Settings', icon: '&#9881;' });

    const sectionMap = {
        'recommendation': secRec, 'tp-calibration': secTP, 'configurations': secCfg,
        'test-settings': secTestCfg, 'comparison': secCmp, 'latency-search': secStep9,
        'calibrated-load': secCal, 'vllm-metrics': secVLLM, 'epp-tuning': secEppTuning,
        'estimator': secEst
    };

    if (subtabDefs.length > 1) {
        html += '<div class="report-subtabs-container">';
        html += '<div class="report-subtab-bar">';
        subtabDefs.forEach((st, i) => {
            html += `<div class="report-subtab${i === 0 ? ' active' : ''}" data-subtab="${st.id}${_chartSuffix}">${st.icon} ${st.label}</div>`;
        });
        html += '</div>';
        subtabDefs.forEach((st) => {
            html += `<div class="report-subtab-pane" data-subtab-pane="${st.id}${_chartSuffix}">${sectionMap[st.id]}</div>`;
        });
        html += '</div>';
    } else {
        for (const sec of Object.values(sectionMap)) html += sec;
    }

    content.innerHTML = html;

    // Render estimator methodology explanation
    updateEstimatorScaling(_chartSuffix);

    // --- Render Plotly charts ---
    const plotlyLayout = { margin: { t: 10, b: 40, l: 50, r: 20 }, height: 430, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent', font: { family: 'Inter, sans-serif' } };
    const plotlyConfig = { responsive: true, displayModeBar: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'], toImageButtonOptions: { format: 'png', height: 600, width: 1200, scale: 2 } };

    // Pareto frontier
    if (charts.pareto.traces.length) {
        const traces = charts.pareto.traces.map(t => ({
            x: t.x, y: t.y, text: t.text, name: t.name,
            mode: 'markers+lines',
            marker: { size: 14, color: t.color, symbol: 'diamond', line: { width: 2, color: 'white' } },
            line: { width: 2, dash: 'dot' },
            hovertemplate: '<b>%{text}</b><extra></extra>'
        }));
        const paretoXvals = [...new Set(traces.flatMap(t => t.x))].sort((a, b) => a - b);
        Plotly.newPlot(cid('chart-pareto'), traces, { ...plotlyLayout, xaxis: { title: 'Total GPUs', tickvals: paretoXvals }, yaxis: { title: 'TTFT P90 (ms) - lower is better' }, showlegend: true }, plotlyConfig);
    }

    // Scatter
    if (charts.scatter.traces.length) {
        const traces = charts.scatter.traces.map(t => ({
            x: t.x, y: t.y, text: t.text, name: t.name,
            mode: 'markers',
            marker: { size: t.sizes, color: t.color, opacity: 0.7, line: { width: 1, color: 'white' } },
            hovertemplate: '<b>%{text}</b><extra></extra>'
        }));
        Plotly.newPlot(cid('chart-scatter'), traces, { ...plotlyLayout, xaxis: { title: 'TTFT P90 (ms) - lower is better' }, yaxis: { title: 'Throughput P90 (req/s) - higher is better' }, showlegend: true }, plotlyConfig);
    }

    // Efficiency bar
    if (charts.efficiency.configs.length) {
        Plotly.newPlot(cid('chart-efficiency'), [{
            x: charts.efficiency.configs, y: charts.efficiency.values,
            type: 'bar', marker: { color: charts.efficiency.colors },
            text: charts.efficiency.values.map(v => v != null ? v.toFixed(3) : ''),
            textposition: 'outside', textfont: { size: 11, color: '#333' },
            cliponaxis: false, constraintext: 'none',
            hovertemplate: '<b>%{x}</b><br>%{y:.3f} req/s/GPU<extra></extra>'
        }], { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 120 }, xaxis: { tickangle: -45 }, yaxis: { title: 'req/s per GPU - higher is better' } }, plotlyConfig);
    }

    // Architecture comparison — use subplots side by side instead of overlaying
    if (charts.architecture.architectures.length) {
        const arch = charts.architecture;
        const archLabels = arch.architectures;
        const ttftTrace = {
            x: archLabels, y: arch.avg_ttft, type: 'bar',
            marker: { color: '#3b82f6' },
            text: arch.avg_ttft.map(v => fmtSI(v) + ' ms'), textposition: 'auto',
            name: 'Avg TTFT P90', xaxis: 'x', yaxis: 'y'
        };
        const bestTtftTrace = {
            x: archLabels, y: arch.best_ttft, type: 'bar',
            marker: { color: '#93c5fd' },
            text: arch.best_ttft.map(v => fmtSI(v) + ' ms'), textposition: 'auto',
            name: 'Best TTFT P90', xaxis: 'x', yaxis: 'y'
        };
        const tputTrace = {
            x: archLabels, y: arch.avg_throughput, type: 'bar',
            marker: { color: '#f59e0b' },
            text: arch.avg_throughput.map(v => v.toFixed(2) + ' req/s'), textposition: 'auto',
            name: 'Avg Throughput P90', xaxis: 'x2', yaxis: 'y2'
        };
        Plotly.newPlot(cid('chart-arch'), [ttftTrace, bestTtftTrace, tputTrace], {
            ...plotlyLayout,
            margin: { t: 30, b: 50, l: 60, r: 60 },
            barmode: 'group',
            showlegend: true, legend: { x: 0, y: 1.18, orientation: 'h' },
            xaxis: { domain: [0, 0.45], title: '' },
            yaxis: { title: 'TTFT (ms) - lower is better', titlefont: { color: '#3b82f6' } },
            xaxis2: { domain: [0.55, 1], title: '', anchor: 'y2' },
            yaxis2: { title: 'Throughput (req/s) - higher is better', anchor: 'x2', titlefont: { color: '#f59e0b' } },
        }, plotlyConfig);
    }

    // TP Calibration charts (Step 2 decode, Step 3 prefill)
    // Render empty chart when no data so the card isn't blank
    if (rec && document.getElementById(cid('chart-tp-decode')) && !(rec.decode_tp_all && rec.decode_tp_all.length)) {
        Plotly.newPlot(cid('chart-tp-decode'), [], {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            xaxis: { title: 'TP Value' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left' },
            yaxis2: { title: 'ITL P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }
    if (rec && document.getElementById(cid('chart-tp-prefill')) && !(rec.prefill_tp_all && rec.prefill_tp_all.length)) {
        Plotly.newPlot(cid('chart-tp-prefill'), [], {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            xaxis: { title: 'TP Value' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left' },
            yaxis2: { title: 'TTFT P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }
    if (rec && rec.decode_tp_all && rec.decode_tp_all.length && document.getElementById(cid('chart-tp-decode'))) {
        const dtp = rec.decode_tp_all;
        const tpLabels = dtp.map(d => `TP=${d.tp}`);
        const tpsgVals = dtp.map(d => d.tpsg);
        const bestTpsg = Math.max(...tpsgVals);
        const barColors = tpsgVals.map(v => v === bestTpsg ? '#10b981' : '#6366f1');
        const itlVals = dtp.map(d => d.itl_p90 != null ? d.itl_p90 : 0);
        const traces = [
            { x: tpLabels, y: tpsgVals, name: 'Tokens/s/GPU', type: 'bar', marker: { color: barColors },
              text: tpsgVals.map(v => fmtSI(v)), textposition: 'outside',
              textfont: { size: 11, color: '#1e293b' }, cliponaxis: false, constraintext: 'none',
              hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>' },
        ];
        if (itlVals.some(v => v > 0)) {
            traces.push({
                x: tpLabels, y: itlVals, name: 'ITL P90 (ms)', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'circle', color: '#ef4444', line: { width: 2, color: 'white' } },
                hovertemplate: '<b>%{x}</b><br>ITL P90: %{y:.2f} ms<extra></extra>',
            });
        }
        Plotly.newPlot(cid('chart-tp-decode'), traces, {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            barmode: 'group', showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left', tickformat: '.2s' },
            yaxis2: { title: 'ITL P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }

    if (rec && rec.prefill_tp_all && rec.prefill_tp_all.length && document.getElementById(cid('chart-tp-prefill'))) {
        const ptp = rec.prefill_tp_all;
        const tpLabels = ptp.map(d => `TP=${d.tp}`);
        const tpsgVals = ptp.map(d => d.tpsg);
        const bestTpsg = Math.max(...tpsgVals);
        const barColors = tpsgVals.map(v => v === bestTpsg ? '#10b981' : '#6366f1');
        const ttftVals = ptp.map(d => d.ttft_p90 != null ? d.ttft_p90 : 0);
        const traces = [
            { x: tpLabels, y: tpsgVals, name: 'Tokens/s/GPU', type: 'bar', marker: { color: barColors },
              text: tpsgVals.map(v => fmtSI(v)), textposition: 'outside',
              textfont: { size: 11, color: '#1e293b' }, cliponaxis: false, constraintext: 'none',
              hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/GPU<extra></extra>' },
        ];
        if (ttftVals.some(v => v > 0)) {
            traces.push({
                x: tpLabels, y: ttftVals, name: 'TTFT P90 (ms)', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'circle', color: '#ef4444', line: { width: 2, color: 'white' } },
                hovertemplate: '<b>%{x}</b><br>TTFT P90: %{y:.1f} ms<extra></extra>',
            });
        }
        Plotly.newPlot(cid('chart-tp-prefill'), traces, {
            ...plotlyLayout, margin: { ...plotlyLayout.margin, r: 60 },
            barmode: 'group', showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' },
            yaxis: { title: 'Tokens/s/GPU — higher is better', side: 'left', tickformat: '.2s' },
            yaxis2: { title: 'TTFT P90 (ms) — lower is better', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);
    }

    // PD configurations TTFT charts (one per percentile)
    const pdResults = data.all_results.filter(r => r.architecture === 'PD');
    if (pdResults.length) {
        const sorted = [...pdResults].sort((a, b) => a.prefill_pods - b.prefill_pods);
        const labels = sorted.map(r => `${r.prefill_pods}P : ${r.decode_pods}D`);
        const aggBase = rec ? rec.aggregated_baseline : null;

        const ttftPercentiles = [
            { key: 'p90', field: 'ttft_p90', tputField: 'throughput_p90', color: '#3b82f6', chartId: 'chart-pd-ttft-p90' },
            { key: 'p95', field: 'ttft_p95', tputField: 'throughput_p95', color: '#dc2626', chartId: 'chart-pd-ttft-p95' },
            { key: 'p99', field: 'ttft_p99', tputField: 'throughput_p99', color: '#7c3aed', chartId: 'chart-pd-ttft-p99' },
        ];

        ttftPercentiles.forEach(pctl => {
            const ttftVals = sorted.map(r => r[pctl.field]);
            const tputVals = sorted.map(r => r[pctl.tputField]);
            const bestTtft = Math.min(...ttftVals);
            const bestTtftIdx = ttftVals.indexOf(bestTtft);
            const bestTput = Math.max(...tputVals);
            const bestTputIdx = tputVals.indexOf(bestTput);
            const pLabel = pctl.key.toUpperCase();

            const hoverText = sorted.map(r =>
                `<b>${r.prefill_pods} Prefill pods</b> (TP=${r.prefill_tp})<br>` +
                `<b>${r.decode_pods} Decode pods</b> (TP=${r.decode_tp})<br>` +
                `TTFT ${pLabel}: <b>${r[pctl.field].toFixed(1)} ms</b><br>` +
                `Throughput ${pLabel}: ${r[pctl.tputField]} req/s<br>` +
                `Total GPUs: ${r.gpus}`
            );

            const shapes = [];
            const annotations = [];
            const aggTtft = aggBase ? aggBase[pctl.field] : null;
            const aggTput = aggBase ? aggBase[pctl.tputField] : null;
            if (aggTtft) {
                shapes.push({ type: 'line', x0: -0.5, x1: labels.length - 0.5, y0: aggTtft, y1: aggTtft, yref: 'y', line: { color: pctl.color, width: 2, dash: 'dash' } });
                annotations.push({ x: 0, y: aggTtft, yref: 'y', text: `Agg TTFT ${pLabel}: ${fmtSI(aggTtft)} ms`, showarrow: false, font: { color: pctl.color, size: 11, weight: 700 }, xanchor: 'left', yanchor: 'bottom', yshift: 5, bgcolor: 'rgba(255,255,255,0.85)' });
            }
            if (aggTput) {
                shapes.push({ type: 'line', x0: -0.5, x1: labels.length - 0.5, y0: aggTput, y1: aggTput, yref: 'y2', line: { color: '#f59e0b', width: 2, dash: 'dash' } });
                annotations.push({ x: labels.length - 1, y: aggTput, yref: 'y2', text: `Agg Tput ${pLabel}: ${aggTput} req/s`, showarrow: false, font: { color: '#f59e0b', size: 11, weight: 700 }, xanchor: 'right', yanchor: 'bottom', yshift: 5, bgcolor: 'rgba(255,255,255,0.85)' });
            }

            Plotly.newPlot(cid(pctl.chartId), [
                {
                    x: labels, y: ttftVals, name: `TTFT ${pLabel}`,
                    type: 'scatter', mode: 'lines+markers',
                    line: { color: pctl.color, width: 3, shape: 'spline' },
                    marker: { color: pctl.color, size: 12, symbol: 'circle', line: { width: 2, color: 'white' } },
                    hovertext: hoverText, hoverinfo: 'text',
                    fill: 'tozeroy', fillcolor: pctl.color + '14',
                },
                {
                    x: [labels[bestTtftIdx]], y: [bestTtft], name: `Best TTFT`,
                    type: 'scatter', mode: 'markers',
                    marker: { color: '#10b981', size: 22, symbol: 'circle', line: { width: 3, color: 'white' } },
                    hovertext: [hoverText[bestTtftIdx]], hoverinfo: 'text',
                    showlegend: true,
                },
                {
                    x: labels, y: tputVals, name: `Throughput ${pLabel}`,
                    type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                    line: { color: '#f59e0b', width: 3, shape: 'spline' },
                    marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } },
                    hovertemplate: `Throughput ${pLabel}: %{y:.2f} req/s<extra></extra>`,
                },
                {
                    x: [labels[bestTputIdx]], y: [tputVals[bestTputIdx]], name: `Best Throughput`,
                    type: 'scatter', mode: 'markers', yaxis: 'y2',
                    marker: { color: '#e11d48', size: 22, symbol: 'diamond', line: { width: 3, color: 'white' } },
                    hovertext: [hoverText[bestTputIdx]], hoverinfo: 'text',
                    showlegend: true,
                },
            ], {
                ...plotlyLayout,
                height: 500,
                margin: { t: 30, b: 80, l: 60, r: 60 },
                xaxis: { title: 'Prefill : Decode Pod Ratio' },
                yaxis: { title: `TTFT ${pLabel} (ms) — lower is better`, side: 'left', titlefont: { color: pctl.color }, tickfont: { color: pctl.color }, tickformat: '.2s' },
                yaxis2: { title: `Throughput ${pLabel} (req/s) — higher is better`, side: 'right', overlaying: 'y', titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
                showlegend: true,
                legend: { x: 0, y: 1.18, orientation: 'h' },
                shapes: shapes,
                annotations: annotations,
            }, plotlyConfig);
        });
    }

    // ============================================================
    // STEP 9: Latency Search — Plotly rendering
    // ============================================================
    if (data.latency_search && data.latency_search.by_architecture) {
        const byArch = data.latency_search.by_architecture;
        const archKeys = Object.keys(byArch);
        const s9ArchConfigs = data.latency_search.arch_configs || {};
        const firstTrial = data.latency_search.trials[0];
        const targetMs = firstTrial.target_ms;
        const targetPct = firstTrial.target_percentile || 'p90';
        const metricKey = 'ttft_' + targetPct;

        archKeys.forEach((arch, ai) => {
            const trials = byArch[arch];
            const cfgLabel = s9ArchConfigs[arch] || arch.toUpperCase();

            // Skip architectures with no valid data
            if (!trials.some(t => t.ttft_p90 != null)) return;

            // Sort by concurrency for clean x-axis
            const sorted = [...trials].filter(t => t.ttft_p90 != null).sort((a, b) => a.concurrency - b.concurrency);
            const xLabels = sorted.map(t => `c=${t.concurrency}`);

            const pctlCharts = [
                { key: 'p90', ttftField: 'ttft_p90', tputField: 'throughput_p90', color: '#3b82f6', divId: `step9-chart-p90-${ai}` },
                { key: 'p95', ttftField: 'ttft_p95', tputField: 'throughput_p95', color: '#dc2626', divId: `step9-chart-p95-${ai}` },
                { key: 'p99', ttftField: 'ttft_p99', tputField: 'throughput_p99', color: '#7c3aed', divId: `step9-chart-p99-${ai}` },
            ];

            pctlCharts.forEach(pctl => {
                const el = document.getElementById(cid(pctl.divId));
                if (!el) return;
                const pLabel = pctl.key.toUpperCase();
                const latencies = sorted.map(t => t[pctl.ttftField]);
                const throughputs = sorted.map(t => t[pctl.tputField] != null ? t[pctl.tputField] : t.throughput_p90);

                const hoverTexts = sorted.map((t, i) =>
                    `<b>${cfgLabel} c=${t.concurrency}</b><br>` +
                    `TTFT ${pLabel}: <b>${latencies[i] != null ? latencies[i].toFixed(1) : '-'} ms</b><br>` +
                    `Throughput ${pLabel}: ${throughputs[i] != null ? throughputs[i].toFixed(2) : '-'} req/s<br>` +
                    `SLA (${targetPct.toUpperCase()}): ${t.meets_sla ? '<span style="color:#10b981">PASS</span>' : '<span style="color:#ef4444">FAIL</span>'}`
                );

                // SLA markers based on target percentile (only color the target pctl chart)
                const isTargetPctl = pctl.key === targetPct;
                const markerColors = isTargetPctl
                    ? sorted.map(t => t.meets_sla ? '#10b981' : '#ef4444')
                    : sorted.map(() => pctl.color);

                // Find best passing (highest throughput meeting SLA) — only for target percentile
                let bestIdx = -1;
                if (isTargetPctl) {
                    let bestTput = -1;
                    sorted.forEach((t, i) => {
                        if (t.meets_sla && throughputs[i] != null && throughputs[i] > bestTput) {
                            bestTput = throughputs[i];
                            bestIdx = i;
                        }
                    });
                } else {
                    // For non-target percentiles, mark the best TTFT
                    let bestTtft = Infinity;
                    latencies.forEach((v, i) => { if (v != null && v < bestTtft) { bestTtft = v; bestIdx = i; } });
                }

                const traces = [
                    {
                        x: xLabels, y: latencies, name: `TTFT ${pLabel}`,
                        type: 'scatter', mode: 'lines+markers',
                        line: { color: pctl.color, width: 3, shape: 'spline' },
                        marker: { color: markerColors, size: 12, symbol: 'circle', line: { width: 2, color: 'white' } },
                        hovertext: hoverTexts, hoverinfo: 'text',
                        fill: 'tozeroy', fillcolor: pctl.color + '14',
                    },
                    {
                        x: xLabels, y: throughputs, name: `Throughput ${pLabel}`,
                        type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                        line: { color: '#f59e0b', width: 3, shape: 'spline' },
                        marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } },
                        hovertemplate: `c=%{x}: %{y:.2f} req/s<extra></extra>`,
                    },
                ];

                if (bestIdx >= 0) {
                    traces.push({
                        x: [xLabels[bestIdx]], y: [latencies[bestIdx]],
                        name: isTargetPctl ? 'Best (meets SLA)' : `Best TTFT ${pLabel}`,
                        type: 'scatter', mode: 'markers',
                        marker: { color: '#10b981', size: 22, symbol: 'circle', line: { width: 3, color: 'white' } },
                        hovertext: [hoverTexts[bestIdx]], hoverinfo: 'text', showlegend: true,
                    });
                    traces.push({
                        x: [xLabels[bestIdx]], y: [throughputs[bestIdx]],
                        name: isTargetPctl ? 'Best Throughput (meets SLA)' : `Best Throughput ${pLabel}`,
                        type: 'scatter', mode: 'markers', yaxis: 'y2',
                        marker: { color: '#e11d48', size: 22, symbol: 'diamond', line: { width: 3, color: 'white' } },
                        hovertext: [hoverTexts[bestIdx]], hoverinfo: 'text', showlegend: true,
                    });
                }

                // SLA line (shown on all percentile charts for reference)
                const shapes = [];
                const chartAnnotations = [];
                const slaLabel = isTargetPctl ? `SLA Target (${targetPct.toUpperCase()}): ${targetMs} ms` : `SLA Target (${targetPct.toUpperCase()}): ${targetMs} ms`;
                shapes.push({
                    type: 'line', x0: -0.5, x1: xLabels.length - 0.5,
                    y0: targetMs, y1: targetMs, yref: 'y',
                    line: { color: '#ef4444', width: isTargetPctl ? 2 : 1.5, dash: 'dash' },
                });
                chartAnnotations.push({
                    x: xLabels.length - 1, y: targetMs, yref: 'y',
                    text: slaLabel, showarrow: false,
                    font: { color: '#ef4444', size: 11, weight: 700 },
                    xanchor: 'right', yanchor: 'bottom', yshift: 5,
                    bgcolor: 'rgba(255,255,255,0.85)',
                });

                Plotly.newPlot(el, traces, {
                    ...plotlyLayout, height: 500,
                    margin: { t: 30, b: 80, l: 60, r: 60 },
                    xaxis: { title: 'Concurrent Users' },
                    yaxis: { title: `TTFT ${pLabel} (ms) — lower is better`, side: 'left', titlefont: { color: pctl.color }, tickfont: { color: pctl.color }, tickformat: '.2s' },
                    yaxis2: { title: `Throughput ${pLabel} (req/s) — higher is better`, side: 'right', overlaying: 'y', titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
                    showlegend: true, legend: { x: 0, y: 1.18, orientation: 'h' },
                    shapes, annotations: chartAnnotations,
                }, plotlyConfig);
            });
        });
    }

    // ============================================================
    // vLLM ENGINE METRICS — Plotly rendering
    // ============================================================
    if (charts.vllm && charts.vllm.configs.length) {
        const vllm = charts.vllm;
        const vllmLayout = { ...plotlyLayout, margin: { ...plotlyLayout.margin, b: 100 }, barmode: 'group', showlegend: true, legend: { x: 0, y: 1.15, orientation: 'h' } };
        const pColors = { p50: '#60a5fa', p90: '#3b82f6', p95: '#f59e0b', p99: '#ef4444' };

        // Chart 1: TTFT Percentiles (grouped bar)
        Plotly.newPlot(cid('chart-vllm-ttft'), [
            { x: vllm.configs, y: vllm.ttft.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 } },
            { x: vllm.configs, y: vllm.ttft.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 } },
            { x: vllm.configs, y: vllm.ttft.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 } },
            { x: vllm.configs, y: vllm.ttft.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 } },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'TTFT (ms) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 2: ITL Percentiles (grouped bar)
        Plotly.newPlot(cid('chart-vllm-itl'), [
            { x: vllm.configs, y: vllm.itl.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 } },
            { x: vllm.configs, y: vllm.itl.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 } },
            { x: vllm.configs, y: vllm.itl.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 } },
            { x: vllm.configs, y: vllm.itl.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 } },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'ITL (ms) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 3: E2E Latency (grouped bar)
        Plotly.newPlot(cid('chart-vllm-e2e'), [
            { x: vllm.configs, y: vllm.e2e.p50, name: 'P50', type: 'bar', marker: { color: pColors.p50 } },
            { x: vllm.configs, y: vllm.e2e.p90, name: 'P90', type: 'bar', marker: { color: pColors.p90 } },
            { x: vllm.configs, y: vllm.e2e.p95, name: 'P95', type: 'bar', marker: { color: pColors.p95 } },
            { x: vllm.configs, y: vllm.e2e.p99, name: 'P99', type: 'bar', marker: { color: pColors.p99 } },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'E2E Latency (seconds) — lower is better', tickformat: '.2s' } }, plotlyConfig);

        // Chart 4: Token Throughput (grouped bar)
        Plotly.newPlot(cid('chart-vllm-tokens'), [
            { x: vllm.configs, y: vllm.token_rates.prompt, name: 'Prompt Tokens/s', type: 'bar', marker: { color: '#6366f1' },
              hovertemplate: '<b>%{x}</b><br>Prompt: %{y:.0f} tokens/s<extra></extra>' },
            { x: vllm.configs, y: vllm.token_rates.generation, name: 'Generation Tokens/s', type: 'bar', marker: { color: '#10b981' },
              hovertemplate: '<b>%{x}</b><br>Generation: %{y:.0f} tokens/s<extra></extra>' },
        ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Tokens/second — higher is better' } }, plotlyConfig);

        // Chart 5: Request Queue & KV Cache (dual axis)
        Plotly.newPlot(cid('chart-vllm-queue'), [
            { x: vllm.configs, y: vllm.request_state.running, name: 'Avg Running', type: 'bar', marker: { color: '#3b82f6' },
              hovertemplate: '<b>%{x}</b><br>Running: %{y:.1f}<extra></extra>' },
            { x: vllm.configs, y: vllm.request_state.waiting, name: 'Avg Waiting', type: 'bar', marker: { color: '#ef4444' },
              hovertemplate: '<b>%{x}</b><br>Waiting: %{y:.1f}<extra></extra>' },
            { x: vllm.configs, y: vllm.request_state.kv_cache, name: 'KV Cache %', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
              line: { color: '#f59e0b', width: 3 }, marker: { size: 10, symbol: 'diamond', color: '#f59e0b', line: { width: 2, color: 'white' } },
              hovertemplate: '<b>%{x}</b><br>KV Cache: %{y:.1f}%<extra></extra>' },
        ], {
            ...vllmLayout,
            margin: { ...vllmLayout.margin, r: 60 },
            xaxis: { tickangle: -35 },
            yaxis: { title: 'Request Count (avg)', side: 'left' },
            yaxis2: { title: 'KV Cache Usage (%)', side: 'right', overlaying: 'y', range: [0, 105], titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
        }, plotlyConfig);

        // Chart 6: Processing Time Breakdown (stacked bar) + Preemptions line
        Plotly.newPlot(cid('chart-vllm-time'), [
            { x: vllm.configs, y: vllm.time_breakdown.prefill, name: 'Prefill Time', type: 'bar', marker: { color: '#6366f1' },
              hovertemplate: '<b>%{x}</b><br>Prefill: %{y:.2f}<extra></extra>' },
            { x: vllm.configs, y: vllm.time_breakdown.decode, name: 'Decode Time', type: 'bar', marker: { color: '#3b82f6' },
              hovertemplate: '<b>%{x}</b><br>Decode: %{y:.2f}<extra></extra>' },
            { x: vllm.configs, y: vllm.time_breakdown.queue, name: 'Queue Time', type: 'bar', marker: { color: '#94a3b8' },
              hovertemplate: '<b>%{x}</b><br>Queue: %{y:.2f}<extra></extra>' },
            { x: vllm.configs, y: vllm.time_breakdown.preemptions, name: 'Preemptions/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
              line: { color: '#ef4444', width: 3 }, marker: { size: 10, symbol: 'triangle-up', color: '#ef4444', line: { width: 2, color: 'white' } },
              hovertemplate: '<b>%{x}</b><br>Preemptions: %{y:.1f}/s<extra></extra>' },
        ], {
            ...vllmLayout,
            barmode: 'stack',
            margin: { ...vllmLayout.margin, r: 60 },
            xaxis: { tickangle: -35 },
            yaxis: { title: 'Time Rate (s/s)', side: 'left' },
            yaxis2: { title: 'Preemptions/s', side: 'right', overlaying: 'y', titlefont: { color: '#ef4444' }, tickfont: { color: '#ef4444' } },
        }, plotlyConfig);

        // Chart 7: Pod Network Throughput
        if (vllm.network && vllm.network.pod_tx.some(v => v > 0)) {
            Plotly.newPlot(cid('chart-net-pod'), [
                { x: vllm.configs, y: vllm.network.pod_tx, name: 'TX (MB/s)', type: 'bar', marker: { color: '#3b82f6' },
                  hovertemplate: '<b>%{x}</b><br>TX: %{y:.2f} MB/s<extra></extra>' },
                { x: vllm.configs, y: vllm.network.pod_rx, name: 'RX (MB/s)', type: 'bar', marker: { color: '#10b981' },
                  hovertemplate: '<b>%{x}</b><br>RX: %{y:.2f} MB/s<extra></extra>' },
            ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'Throughput (MB/s)' } }, plotlyConfig);
        }

        // Chart 8: InfiniBand RDMA Throughput
        if (vllm.network && vllm.network.ib_rx.some(v => v > 0)) {
            Plotly.newPlot(cid('chart-net-ib'), [
                { x: vllm.configs, y: vllm.network.ib_rx, name: 'IB RX (GB/s)', type: 'bar',
                  marker: { color: vllm.network.ib_rx.map(v => v > 0 ? '#8b5cf6' : '#cbd5e1') },
                  text: vllm.network.ib_rx.map(v => v > 0 ? v.toFixed(2) : ''),
                  textposition: 'outside', textfont: { size: 11, color: '#1e293b' },
                  cliponaxis: false, constraintext: 'none',
                  hovertemplate: '<b>%{x}</b><br>IB RX: %{y:.2f} GB/s<extra></extra>' },
            ], { ...vllmLayout, xaxis: { tickangle: -35 }, yaxis: { title: 'RDMA Throughput (GB/s)' } }, plotlyConfig);
        }
    }

    // Initialize subtab switching (activates first pane, hides others)
    initReportSubtabs(content);
}

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
    // Remove any existing comparison tab
    reportTabs.filter(t => t.isComparison).forEach(t => closeReportTab(t.id));

    const runTabs = reportTabs.filter(t => !t.isComparison && tabDataCache[t.id]);
    if (runTabs.length < 2) return;

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
}

function downloadHTMLReport(runId, data) {
    const charts = data.charts;
    const rec = data.recommendation || {};
    const summary = data.summary;
    const best = summary.best_configs || {};
    const allRes = data.all_results || [];
    const pdResults = allRes.filter(r => r.architecture === 'PD');
    const hasVLLM = charts.vllm && charts.vllm.configs.length;
    const hasPD = pdResults.length > 0;
    const hasStep8 = rec && (rec.pd_vs_agg || rec.ep_vs_agg);
    const hasStep10 = !!data.calibrated_qps;

    let html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>InfeRecipe Report - Run ${runId}</title>`;
    html += '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"><\/script>';
    html += `<style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; width: 95%; margin: 0 auto; padding: 20px; background: #f8fafc; color: #1e293b; }
        h1 { color: #1e293b; border-bottom: 3px solid #10b981; padding-bottom: 10px; }
        h2 { margin-top: 30px; border-bottom: 2px solid #3b82f6; padding-bottom: 8px; color: #1e293b; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: 20px 0; }
        .stat-card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); text-align: center; }
        .stat-card .val { font-size: 2em; font-weight: 800; color: #1e293b; }
        .stat-card .lbl { color: #64748b; font-size: 0.85em; }
        .chart-box { background: white; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin: 20px 0; padding: 16px; }
        .chart-box h3 { margin: 0 0 10px; color: #1e293b; }
        .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        table { width: 100%; border-collapse: collapse; }
        th { background: #1e293b; color: white; padding: 10px; text-align: left; }
        td { padding: 8px 10px; border-bottom: 1px solid #f1f5f9; }
        .pareto { background: #f0fdf4; font-weight: 600; }
        .constraint-box { background: #fffbeb; border: 2px solid #f59e0b; border-left: 6px solid #f59e0b; border-radius: 8px; padding: 16px 20px; margin: 20px 0; }
        .constraint-box h3 { color: #92400e; margin: 0 0 8px; }
        .constraint-box p { color: #78350f; margin: 4px 0; line-height: 1.6; }
        .dl-tab-bar { display:flex; gap:4px; padding:12px 0 0; border-bottom:2px solid #e5e7eb; margin:20px 0 0; flex-wrap:wrap; position:sticky; top:0; background:#f8fafc; z-index:10; }
        .dl-tab { padding:8px 16px; font-size:13px; font-weight:600; color:#6b7280; cursor:pointer; border-radius:8px 8px 0 0; border-bottom:2px solid transparent; margin-bottom:-2px; user-select:none; }
        .dl-tab:hover { color:#374151; background:#f3f4f6; }
        .dl-tab.active { color:#1e293b; border-bottom-color:#3b82f6; background:#eff6ff; }
        .dl-pane { display:none; padding-top:16px; }
        .dl-pane.active { display:block; }
    </style></head><body>`;

    html += `<h1>InfeRecipe Optimization Report - Run #${runId}</h1>`;
    html += `<p>Generated: ${new Date().toLocaleString()}</p>`;

    // === Build each section separately ===
    let secRec = '', secTP = '', secCfg = '', secCmp = '', secStep9 = '', secCal = '', secVLLM = '', secTestCfg = '', secEppTuning = '';

    // --- RECOMMENDATION ---
    if (rec.constraint_notes && rec.constraint_notes.length) {
        secRec += '<div class="constraint-box"><h3>&#9888; Configuration Constraints</h3>';
        rec.constraint_notes.forEach(n => { secRec += `<p>${n}</p>`; });
        secRec += '</div>';
    }
    if (rec.goal_info) {
        const gColors = { ttft: '#3b82f6', throughput: '#f59e0b', balanced: '#10b981', aggregated_only: '#64748b', pd_only: '#8b5cf6', ep_only: '#0ea5e9' };
        const gIcons = { ttft: '&#9201;', throughput: '&#9889;', balanced: '&#9878;', aggregated_only: '&#9634;', pd_only: '&#8644;', ep_only: '&#9881;' };
        const gc = gColors[rec.goal] || '#10b981';
        secRec += `<div style="border:3px solid ${gc}; border-left:8px solid ${gc}; border-radius:10px; margin:20px 0; overflow:hidden;">`;
        secRec += `<div style="background:${gc}; color:white; padding:14px 20px; font-size:1.3em; font-weight:800;">${gIcons[rec.goal] || ''} ${rec.goal_info.name}</div>`;
        secRec += `<div style="background:${gc}dd; color:white; padding:8px 20px; font-size:0.92em;">`;
        let turnsLabel = (rec.workload.turns && rec.workload.turns > 1) ? ` | Turns: <strong>${rec.workload.turns}</strong>` : '';
        secRec += `Model: <strong>${rec.model}</strong> &nbsp;|&nbsp; ISL: <strong>${rec.workload.isl}</strong> | OSL: <strong>${rec.workload.osl}</strong>${turnsLabel} &nbsp;|&nbsp; Users: <strong>${rec.workload.users}</strong> &nbsp;|&nbsp; Tests: <strong>${rec.total_tests}</strong>`;
        if (rec.total_duration) secRec += ` &nbsp;|&nbsp; Duration: <strong>${rec.total_duration}</strong>`;
        secRec += '</div>';
        secRec += `<div style="padding:20px;"><p style="color:#334155; margin:0; font-size:0.95em; line-height:1.6;">${rec.goal_info.description}</p></div></div>`;
    }
    if (rec.recommendations && Object.keys(rec.recommendations).length) {
        secRec += '<div style="border:2px solid #10b981; border-left:6px solid #10b981; border-radius:10px; margin:20px 0; overflow:hidden;">';
        secRec += '<div style="background:linear-gradient(135deg,#ecfdf5,#d1fae5); padding:14px 20px; font-size:1.2em; font-weight:800; color:#1e293b;">Deployment Recommendation</div>';
        secRec += '<div style="padding:24px;">';
        secRec += '<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px; margin-bottom:20px;">';
        const goalIcons = { response_time: '&#9201;', throughput: '&#9889;' };
        const goalColors = { response_time: '#3b82f6', throughput: '#f59e0b' };
        const goalExplain = { response_time: 'Best for chatbots and interactive applications.', throughput: 'Best for batch processing and high-volume workloads.' };
        for (const [key, r] of Object.entries(rec.recommendations)) {
            const c = r.config;
            const isPrimary = (rec.goal === 'ttft' && key === 'response_time') || (rec.goal === 'throughput' && key === 'throughput');
            const border = isPrimary ? `3px solid ${goalColors[key]}` : `2px solid ${goalColors[key]}40`;
            const badge = isPrimary ? `<span style="background:${goalColors[key]}; color:white; font-size:0.7em; padding:2px 8px; border-radius:4px; margin-left:8px;">PRIMARY</span>` : '';
            const archBadge = r.architecture ? `<span style="background:#64748b; color:white; font-size:0.65em; padding:2px 6px; border-radius:3px; margin-left:6px;">${r.architecture}</span>` : '';
            secRec += `<div style="background:${goalColors[key]}10; border:${border}; border-radius:10px; padding:16px;">`;
            secRec += `<div style="font-weight:800; color:${goalColors[key]}; font-size:0.85em; text-transform:uppercase; margin-bottom:8px;">${goalIcons[key] || ''} ${r.goal}${badge}${archBadge}</div>`;
            secRec += `<div style="font-size:1.4em; font-weight:800; color:#1e293b; margin-bottom:4px;">${r.deploy}</div>`;
            const details = c.ratio ? `P:D ratio ${c.ratio} &nbsp;|&nbsp; ` : '';
            const ttftStr = c.ttft_p90 != null ? `TTFT P90: <strong>${c.ttft_p90} ms</strong>` : '';
            const tputStr = c.throughput_p90 != null ? `Throughput P90: <strong>${c.throughput_p90} req/s</strong>` : '';
            secRec += `<div style="font-size:0.9em; color:#475569;">${details}${[ttftStr, tputStr].filter(Boolean).join(' &nbsp;|&nbsp; ')} &nbsp;|&nbsp; ${c.gpus} GPUs</div>`;
            secRec += `<div style="font-size:0.82em; color:#64748b; margin-top:8px; line-height:1.5;">${goalExplain[key] || ''}</div>`;
            secRec += '</div>';
        }
        secRec += '</div>';
        if (rec.optimal_decode_tp || rec.optimal_prefill_tp || rec.pd_tests_count || rec.ep_tests_count) {
            secRec += '<div style="background:#f8fafc; border-radius:8px; padding:14px 18px; display:flex; gap:32px; flex-wrap:wrap; font-size:0.9em; margin-top:12px;">';
            if (rec.optimal_decode_tp) secRec += `<div><strong>Optimal Decode TP:</strong> ${rec.optimal_decode_tp.tp} <span style="color:#64748b">(${rec.optimal_decode_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.optimal_prefill_tp) secRec += `<div><strong>Optimal Prefill TP:</strong> ${rec.optimal_prefill_tp.tp} <span style="color:#64748b">(${rec.optimal_prefill_tp.tpsg} tokens/s/GPU)</span></div>`;
            if (rec.pd_tests_count) secRec += `<div><strong>PD Splits Tested:</strong> ${rec.pd_tests_count}</div>`;
            if (rec.ep_tests_count) secRec += `<div><strong>EP Configs Tested:</strong> ${rec.ep_tests_count}</div>`;
            secRec += '</div>';
        }
        secRec += '</div></div>';
    }
    secRec += '<div class="stats">';
    secRec += `<div class="stat-card"><div class="val">${summary.successful_tests}</div><div class="lbl">Successful Tests (${summary.total_tests} total)</div></div>`;
    if (best.lowest_latency) secRec += `<div class="stat-card"><div class="val">${best.lowest_latency.ttft_p90.toFixed(1)} ms</div><div class="lbl">Best TTFT P90</div></div>`;
    if (best.highest_throughput) secRec += `<div class="stat-card"><div class="val">${best.highest_throughput.throughput_p90.toFixed(2)} req/s</div><div class="lbl">Best Throughput</div></div>`;
    if (best.most_efficient) secRec += `<div class="stat-card"><div class="val">${best.most_efficient.efficiency.toFixed(3)}</div><div class="lbl">Best Efficiency (req/s/GPU)</div></div>`;
    secRec += '</div>';

    // --- TP CALIBRATION ---
    secTP += '<div class="grid2"><div class="chart-box"><h3>Decode TP Sweep</h3><div id="tp-dec" style="height:430px"></div></div>';
    secTP += '<div class="chart-box"><h3>Prefill TP Sweep</h3><div id="tp-pre" style="height:430px"></div></div></div>';
    secTP += '<div class="chart-box"><h3>TP Calibration (Pareto)</h3><div id="p1" style="height:430px"></div></div>';

    // --- CONFIGURATIONS ---
    secCfg += '<div class="grid2"><div class="chart-box"><h3>Throughput vs Latency</h3><div id="p2" style="height:430px"></div></div>';
    secCfg += '<div class="chart-box"><h3>GPU Efficiency</h3><div id="p3" style="height:430px"></div></div></div>';
    secCfg += '<div class="chart-box"><h3>Architecture Comparison</h3><div id="p4" style="height:430px"></div></div>';
    if (hasPD) {
        secCfg += '<div class="chart-box"><h3>PD Configurations TTFT</h3><div id="pd-ttft" style="height:500px"></div></div>';
        secCfg += '<div class="chart-box"><h3>TTFT vs Throughput Trade-off</h3><div id="pd-tradeoff" style="height:500px"></div></div>';
    }
    if (charts.pareto.pareto_table.length) {
        secCfg += '<div class="chart-box"><h3>Pareto Optimal Configurations</h3><table><tr><th>Config</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>GPUs</th><th>Efficiency</th></tr>';
        charts.pareto.pareto_table.forEach((p, idx) => {
            const metrics = [
                {name: 'TTFT (ms)', p50: p.ttft_p50, p90: p.ttft_p90, p95: p.ttft_p95, p99: p.ttft_p99},
                {name: 'ITL (ms)', p50: p.itl_p50, p90: p.itl_p90, p95: p.itl_p95, p99: p.itl_p99},
                {name: 'Throughput (req/s)', p50: p.throughput_p50, p90: p.throughput_p90, p95: p.throughput_p95, p99: p.throughput_p99},
            ];
            const bt = idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
            metrics.forEach((m, mi) => {
                const bs = mi === 0 && idx > 0 ? bt : '';
                secCfg += `<tr class="pareto">`;
                if (mi === 0) secCfg += `<td rowspan="3" style="vertical-align:middle; font-weight:700;${bs}">${p.config_name}<br><span style="font-weight:400; font-size:0.85em; color:#64748b;">${p.architecture}</span></td>`;
                secCfg += `<td style="color:#64748b;${bs}">${m.name}</td>`;
                secCfg += `<td style="${bs}">${m.p50 ?? '-'}</td><td style="${bs}">${m.p90 ?? '-'}</td><td style="${bs}">${m.p95 ?? '-'}</td><td style="${bs}">${m.p99 ?? '-'}</td>`;
                if (mi === 0) secCfg += `<td rowspan="3" style="vertical-align:middle;${bs}">${p.gpus}</td><td rowspan="3" style="vertical-align:middle;${bs}">${p.efficiency}</td>`;
                secCfg += '</tr>';
            });
        });
        secCfg += '</table></div>';
    }
    if (allRes.length) {
        secCfg += '<div class="chart-box"><h3>All Results (sorted by TTFT)</h3><table><tr><th>Config</th><th>Arch</th><th>TTFT P90</th><th>ITL P90</th><th>Throughput P90</th><th>GPUs</th><th>Efficiency</th></tr>';
        const pn = new Set(charts.pareto.pareto_table.map(p => p.config_name));
        allRes.forEach(r => { secCfg += `<tr${pn.has(r.config_name) ? ' class="pareto"' : ''}><td>${r.config_name}</td><td>${r.architecture}</td><td>${r.ttft_p90}</td><td>${r.itl_p90 ?? 'N/A'}</td><td>${r.throughput_p90}</td><td>${r.gpus}</td><td>${r.efficiency}</td></tr>`; });
        secCfg += '</table></div>';
    }

    // --- COMPARISON (Step 8) ---
    if (rec && rec.pd_vs_agg) {
        const cmp = rec.pd_vs_agg;
        const ttftColor = cmp.ttft_winner === 'PD' ? '#10b981' : '#f59e0b';
        const tputColor = cmp.throughput_winner === 'PD' ? '#10b981' : '#f59e0b';
        secCmp += '<div style="margin-top:16px; border-radius:10px; overflow:hidden; border:2px solid #6366f1; border-left:6px solid #6366f1;">';
        secCmp += '<div style="background:linear-gradient(135deg,#6366f1,#8b5cf6); padding:12px 20px; color:white; font-weight:700;">PD vs Aggregated Comparison</div>';
        secCmp += '<table><tr><th>Metric</th><th>PD (best)</th><th>Aggregated</th><th>Winner</th></tr>';
        secCmp += `<tr><td><strong>TTFT P90</strong></td><td>${cmp.pd.ttft_p90} ms</td><td>${cmp.aggregated.ttft_p90} ms</td><td style="color:${ttftColor}; font-weight:700;">${cmp.ttft_winner} (${cmp.ttft_diff_pct}% better)</td></tr>`;
        secCmp += `<tr><td><strong>Throughput P90</strong></td><td>${cmp.pd.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_p90} req/s</td><td style="color:${tputColor}; font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
        secCmp += '</table></div>';
    }
    if (rec && rec.ep_vs_agg) {
        const cmp = rec.ep_vs_agg;
        const ttftColor = cmp.ttft_winner === 'EP' ? '#10b981' : '#f59e0b';
        const tputColor = cmp.throughput_winner === 'EP' ? '#10b981' : '#f59e0b';
        secCmp += '<div style="margin-top:16px; border-radius:10px; overflow:hidden; border:2px solid #6366f1; border-left:6px solid #6366f1;">';
        secCmp += '<div style="background:linear-gradient(135deg,#6366f1,#8b5cf6); padding:12px 20px; color:white; font-weight:700;">EP vs Aggregated Comparison</div>';
        secCmp += '<table><tr><th>Metric</th><th>EP (best)</th><th>Aggregated</th><th>Winner</th></tr>';
        secCmp += `<tr><td><strong>TTFT P90</strong></td><td>${cmp.ep.ttft_p90} ms</td><td>${cmp.aggregated.ttft_p90} ms</td><td style="color:${ttftColor}; font-weight:700;">${cmp.ttft_winner} (${cmp.ttft_diff_pct}% better)</td></tr>`;
        secCmp += `<tr><td><strong>Throughput P90</strong></td><td>${cmp.ep.throughput_p90} req/s</td><td>${cmp.aggregated.throughput_p90} req/s</td><td style="color:${tputColor}; font-weight:700;">${cmp.throughput_winner} (${cmp.throughput_diff_pct}% better)</td></tr>`;
        secCmp += '</table></div>';
    }

    // --- STEP 9: LATENCY SEARCH ---
    if (data.latency_search && data.latency_search.trials && data.latency_search.trials.length) {
        const ls = data.latency_search;
        const byArch = ls.by_architecture || {};
        const archKeys = Object.keys(byArch);
        const firstTrial = ls.trials[0];
        const targetMs = firstTrial.target_ms;
        const targetPct = firstTrial.target_percentile || 'p90';
        const metricKey = 'ttft_' + targetPct;

        secStep9 += '<div style="border-radius:10px; overflow:hidden; border:2px solid #8b5cf6; border-left:6px solid #8b5cf6; margin-bottom:20px;">';
        secStep9 += '<div style="background:linear-gradient(135deg,#7c3aed,#8b5cf6); padding:12px 20px; color:white; font-weight:700;">Step 9: Latency-Bounded Throughput Search</div>';
        secStep9 += `<div style="padding:12px 20px; font-size:0.95em;">Binary search over concurrency to find max throughput keeping TTFT ${targetPct.toUpperCase()} under <strong>${targetMs} ms</strong>.</div>`;

        const dlArchConfigs = ls.arch_configs || {};
        archKeys.forEach((arch, ai) => {
            const trials = byArch[arch];
            const passing = trials.filter(t => t.meets_sla);
            const bestPassing = passing.length ? passing.reduce((a, b) => a.concurrency > b.concurrency ? a : b) : null;
            const cfgLabel = dlArchConfigs[arch] || arch.toUpperCase();
            if (bestPassing) {
                const latVal = bestPassing[metricKey] != null ? bestPassing[metricKey].toFixed(1) : '-';
                const dlS9TputKey = 'throughput_' + targetPct;
                const tputVal = bestPassing[dlS9TputKey] != null ? bestPassing[dlS9TputKey].toFixed(2) : (bestPassing.throughput_p90 != null ? bestPassing.throughput_p90.toFixed(2) : '-');
                secStep9 += `<div style="padding:8px 20px;"><strong>${cfgLabel}</strong>: Optimal concurrency = <strong>${bestPassing.concurrency}</strong> (TTFT ${targetPct.toUpperCase()}: ${latVal} ms, Throughput ${targetPct.toUpperCase()}: ${tputVal} req/s, ${trials.length} tests)</div>`;
            }
            secStep9 += `<div id="dl-step9-chart-${ai}" style="height:430px; margin:0 16px;"></div>`;
            secStep9 += `<div style="margin:16px;"><div style="font-weight:700; margin-bottom:8px;">Latency Cost of Throughput</div>`;
            secStep9 += `<div id="dl-step9-cost-${ai}" style="height:400px;"></div>`;
            const dlSorted = [...trials].sort((a, b) => a.concurrency - b.concurrency);
            secStep9 += '<table style="font-size:0.85em;"><tr><th>Concurrency</th><th>TTFT P50</th><th>TTFT P90</th><th>TTFT P95</th><th>TTFT P99</th><th>Throughput P90</th><th>SLA</th></tr>';
            dlSorted.forEach(t => {
                const sla = t.meets_sla ? '<span style="color:#059669;">Yes</span>' : '<span style="color:#dc2626;">No</span>';
                secStep9 += `<tr><td style="font-weight:700;">${t.concurrency}</td><td>${t.ttft_p50 != null ? t.ttft_p50.toFixed(1) : '-'}</td><td>${t.ttft_p90 != null ? t.ttft_p90.toFixed(1) : '-'}</td><td>${t.ttft_p95 != null ? t.ttft_p95.toFixed(1) : '-'}</td><td>${t.ttft_p99 != null ? t.ttft_p99.toFixed(1) : '-'}</td><td>${t.throughput_p90 != null ? t.throughput_p90.toFixed(2) : '-'}</td><td>${sla}</td></tr>`;
            });
            secStep9 += '</table></div>';
        });

        const dlS9TputKey2 = 'throughput_' + targetPct;
        secStep9 += `<table><tr><th>Arch</th><th>#</th><th>Phase</th><th>Concurrency</th><th>TTFT ${targetPct.toUpperCase()}</th><th>Throughput ${targetPct.toUpperCase()}</th><th>Meets SLA</th></tr>`;
        ls.trials.forEach(t => {
            const latVal = t[metricKey] != null ? t[metricKey].toFixed(1) + ' ms' : '-';
            const dlTputRaw = t[dlS9TputKey2] != null ? t[dlS9TputKey2] : t.throughput_p90;
            const tputVal = dlTputRaw != null ? dlTputRaw.toFixed(2) + ' req/s' : '-';
            const slaStyle = t.meets_sla ? 'color:#059669; font-weight:700;' : 'color:#dc2626; font-weight:700;';
            secStep9 += `<tr><td>${t.architecture}</td><td>${t.trial_number}</td><td>${t.search_phase}</td><td style="font-weight:700;">${t.concurrency}</td><td>${latVal}</td><td>${tputVal}</td><td style="${slaStyle}">${t.meets_sla ? 'Yes' : 'No'}</td></tr>`;
        });
        secStep9 += '</table></div>';
    }

    // --- CALIBRATED LOAD (Step 10) ---
    if (hasStep10) {
        const cal = data.calibrated_qps;
        const primary = cal.pd || cal.ep;
        const primaryLabel = cal.pd ? 'PD' : 'EP';
        const dlIsBalanced = !!(cal.pd && cal.ep);
        if (cal.gpu_sizing) {
            const s = cal.gpu_sizing;
            secCal += '<div style="padding:12px 20px; background:#ecfdf5; border:1px solid #6ee7b7; border-radius:8px; margin-bottom:16px; font-size:0.9em; color:#065f46;">';
            secCal += '<div style="font-weight:700; margin-bottom:8px;">Cluster Capacity Analysis</div>';
            secCal += '<table style="width:auto; margin:0; font-size:0.95em; border:none;">';
            secCal += '<tr style="background:none;"><td style="border:none; padding:2px 16px 2px 0; color:#047857;"><strong>GPU Cost per Request</strong></td><td style="border:none;"></td></tr>';
            secCal += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Prefill</td><td style="border:none;">${s.isl} ISL / ${s.prefill_tpsg} TPSG = <strong>${s.prefill_cost} GPU-sec</strong></td></tr>`;
            secCal += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Decode</td><td style="border:none;">${s.osl} OSL / ${s.decode_tpsg} TPSG = <strong>${s.decode_cost} GPU-sec</strong></td></tr>`;
            secCal += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Total</td><td style="border:none;"><strong>${s.total_cost} GPU-sec/request</strong></td></tr>`;
            secCal += '<tr style="background:none;"><td style="border:none; padding:6px 16px 2px 0; color:#047857;"><strong>Sustainable Throughput</strong></td><td style="border:none;"></td></tr>';
            secCal += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Cluster capacity</td><td style="border:none;">${s.total_gpus} GPUs / ${s.total_cost} GPU-sec / ${s.headroom}x headroom = <strong>${s.sustainable_throughput_rps || s.sustainable_qps} req/s</strong> (${s.sustainable_concurrency || '?'} concurrent users)</td></tr>`;
            secCal += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Concurrency tested</td><td style="border:none;"><strong>${s.concurrency} simultaneous requests</strong></td></tr>`;
            secCal += `<tr style="background:none;"><td style="border:none; padding:1px 16px 1px 12px;">Ideal P/D ratio</td><td style="border:none;"><strong>${s.ideal_prefill_pct}% prefill</strong></td></tr>`;
            secCal += '</table></div>';
        }
        const dlRequestedRps = cal.requested_rps != null ? cal.requested_rps : null;
        const dlRpsLabel = dlRequestedRps != null ? ` at ${Math.round(dlRequestedRps)} concurrent` : '';
        const dlEntries = [];
        if (cal.pd) dlEntries.push({label: 'PD', entry: cal.pd});
        if (cal.aggregated) dlEntries.push({label: 'Aggregated', entry: cal.aggregated});
        if (dlIsBalanced && cal.ep) dlEntries.push({label: 'EP', entry: cal.ep});
        secCal += `<div class="chart-box"><h3>Percentile Breakdown${dlRpsLabel}</h3>`;
        secCal += '<table><tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th></tr>';
        function dlFindBest(metric, lower) { const v = dlEntries.map(e => e.entry[metric]).filter(x => x != null); return !v.length ? null : lower ? Math.min(...v) : Math.max(...v); }
        const dlBT = dlFindBest('ttft_p90', true), dlBP = dlFindBest('throughput_p90', false), dlBI = dlFindBest('itl_p90', true);
        const dlHl = (v, b) => v != null && v === b ? 'color:#059669; font-weight:700;' : '';
        dlEntries.forEach(({label, entry}, idx) => {
            [{name:'TTFT (ms)',p50:entry.ttft_p50,p90:entry.ttft_p90,p95:entry.ttft_p95,p99:entry.ttft_p99,b:dlBT,k:'ttft_p90'},
             {name:'ITL (ms)',p50:entry.itl_p50,p90:entry.itl_p90,p95:entry.itl_p95,p99:entry.itl_p99,b:dlBI,k:'itl_p90'},
             {name:'Throughput (req/s)',p50:entry.throughput_p50,p90:entry.throughput_p90,p95:entry.throughput_p95,p99:entry.throughput_p99,b:dlBP,k:'throughput_p90'}
            ].forEach((m, mi) => {
                const bs = mi === 0 && idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
                secCal += '<tr>';
                if (mi === 0) secCal += `<td rowspan="3" style="vertical-align:middle; font-weight:700;${bs}">${label}</td>`;
                secCal += `<td style="color:#64748b;${bs}">${m.name}</td><td style="${bs}">${m.p50 ?? '-'}</td><td style="${dlHl(entry[m.k],m.b)}${bs}">${m.p90 ?? '-'}</td><td style="${bs}">${m.p95 ?? '-'}</td><td style="${bs}">${m.p99 ?? '-'}</td></tr>`;
            });
        });
        secCal += '</table></div>';
        const dlOverload = cal.overloaded_pd || cal.overloaded_ep;
        if (dlOverload) {
            secCal += `<div class="chart-box"><h3>Overload Impact: ${primaryLabel} at Calibrated vs Overloaded Load</h3>`;
            secCal += '<table><tr><th>Configuration</th><th>Load</th><th>TTFT P90</th><th>Throughput P90</th></tr>';
            secCal += `<tr><td><strong>${primaryLabel} (calibrated)</strong></td><td>${dlRequestedRps != null ? Math.round(dlRequestedRps) + ' concurrent' : '-'}</td><td style="color:#059669; font-weight:700;">${primary.ttft_p90} ms</td><td style="color:#059669; font-weight:700;">${primary.throughput_p90} req/s</td></tr>`;
            secCal += `<tr><td><strong>${primaryLabel} (overloaded)</strong></td><td>${cal.concurrency != null ? cal.concurrency + ' concurrent' : '-'}</td><td style="color:#94a3b8;">${dlOverload.ttft_p90} ms</td><td style="color:#94a3b8;">${dlOverload.throughput_p90} req/s</td></tr>`;
            secCal += '</table></div>';
        }
    }

    // --- vLLM METRICS ---
    if (hasVLLM) {
        secVLLM += '<div class="grid2"><div class="chart-box"><h3>TTFT Percentiles</h3><div id="v1" style="height:430px"></div></div><div class="chart-box"><h3>ITL Percentiles</h3><div id="v2" style="height:430px"></div></div></div>';
        secVLLM += '<div class="grid2"><div class="chart-box"><h3>E2E Latency</h3><div id="v3" style="height:430px"></div></div><div class="chart-box"><h3>Token Throughput</h3><div id="v4" style="height:430px"></div></div></div>';
        secVLLM += '<div class="grid2"><div class="chart-box"><h3>Request Queue & KV Cache</h3><div id="v5" style="height:430px"></div></div><div class="chart-box"><h3>Time Breakdown & Preemptions</h3><div id="v6" style="height:430px"></div></div></div>';
        if (charts.vllm.network && charts.vllm.network.pod_tx.some(v => v > 0)) {
            secVLLM += '<div class="grid2"><div class="chart-box"><h3>Pod Network</h3><div id="v7" style="height:430px"></div></div>';
            if (charts.vllm.network.ib_rx.some(v => v > 0)) secVLLM += '<div class="chart-box"><h3>InfiniBand RDMA</h3><div id="v8" style="height:430px"></div></div>';
            secVLLM += '</div>';
        }
    }

    // === Assemble tab bar + panes ===
    const dlTabs = [];
    if (secRec) dlTabs.push({ id: 'rec', label: '&#9733; Recommendation', html: secRec });
    if (secTP) dlTabs.push({ id: 'tp', label: '&#9881; TP Calibration', html: secTP });
    if (secCfg) dlTabs.push({ id: 'cfg', label: '&#9776; Configurations', html: secCfg });
    if (secCmp) dlTabs.push({ id: 'cmp', label: '&#8596; Comparison', html: secCmp });
    if (secStep9) dlTabs.push({ id: 'step9', label: '&#128269; Latency Search', html: secStep9 });
    if (secCal) dlTabs.push({ id: 'cal', label: '&#9878; Calibrated Load', html: secCal });
    if (secVLLM) dlTabs.push({ id: 'vllm', label: '&#9889; vLLM Metrics', html: secVLLM });

    if (dlTabs.length > 1) {
        html += '<div class="dl-tab-bar">';
        dlTabs.forEach((t, i) => { html += `<div class="dl-tab${i === 0 ? ' active' : ''}" onclick="switchDlTab('${t.id}')">${t.label}</div>`; });
        html += '</div>';
    }
    dlTabs.forEach((t, i) => {
        html += `<div id="dl-pane-${t.id}" class="dl-pane${i === 0 ? ' active' : ''}">${t.html}</div>`;
    });

    // --- Chart rendering + tab switching script ---
    html += '<script>';
    html += `function switchDlTab(id){document.querySelectorAll('.dl-tab').forEach(function(t){t.classList.remove('active')});document.querySelectorAll('.dl-pane').forEach(function(p){p.classList.remove('active')});var tab=document.querySelector('.dl-tab[onclick*=\"'+id+'\"]');if(tab)tab.classList.add('active');var pane=document.getElementById('dl-pane-'+id);if(pane){pane.classList.add('active');pane.querySelectorAll('[class*="js-plotly"]').forEach(function(p){Plotly.Plots.resize(p)});}}`;
    html += 'var cd=' + JSON.stringify(charts) + ';';
    html += 'var ar=' + JSON.stringify(allRes) + ';';
    html += 'var lo={margin:{t:30,b:40,l:50,r:20},height:430,font:{family:"sans-serif"}};';
    html += 'var co={responsive:true};';
    html += 'function fmtSI(v,d){if(v==null)return"-";d=d!=null?d:1;if(Math.abs(v)>=1e6)return(v/1e6).toFixed(d)+"M";if(Math.abs(v)>=1e3)return(v/1e3).toFixed(d)+"K";return v.toFixed(d)}';
    html += 'function arrAnn(xs,ys,o){o=o||{};var c=o.color||"#333",d=o.decimals!=null?o.decimals:1,s=o.suffix||"",sp=o.spread||30;var offs=[{ax:0,ay:-sp},{ax:sp*0.9,ay:sp*0.7},{ax:-sp*0.8,ay:-sp*1.2},{ax:sp*1.1,ay:-sp*0.5},{ax:0,ay:sp*1.1},{ax:-sp,ay:sp*0.8},{ax:sp*1.3,ay:-sp*1.3},{ax:-sp*1.2,ay:sp*1.3}];return ys.map(function(v,i){if(v==null)return null;var p=offs[i%offs.length];return{x:xs[i],y:v,xref:"x",yref:o.yref||"y",text:fmtSI(v,d)+s,showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:"#94a3b8",ax:p.ax,ay:p.ay,font:{size:10,color:c},borderpad:2}}).filter(Boolean)}';
    html += 'var vl={...lo,margin:{...lo.margin,b:100},barmode:"group",showlegend:true,legend:{x:0,y:1.15,orientation:"h"}};';
    html += 'var pc={p50:"#60a5fa",p90:"#3b82f6",p95:"#f59e0b",p99:"#ef4444"};';

    // Show all panes for initial Plotly rendering, then hide
    html += 'document.querySelectorAll(".dl-pane").forEach(function(p){p.style.display="block"});';

    // TP calibration charts
    html += 'if(cd.pareto&&cd.pareto.traces){cd.pareto.traces.forEach(function(t){';
    html += '  var tgt=t.name==="Decode"?"tp-dec":"tp-pre";';
    html += '  if(document.getElementById(tgt)){';
    html += '    var tps=t.x.map(function(_,i){return"TP"+t.x[i]});';
    html += '    Plotly.newPlot(tgt,[{x:tps,y:t.y,type:"bar",marker:{color:t.color},hovertext:t.text,hoverinfo:"text",text:t.y.map(function(v){return fmtSI(v)}),textposition:"outside",textfont:{size:11,color:"#1e293b"},cliponaxis:false,constraintext:"none"}],{...lo,title:{text:t.name+" TP Sweep"},yaxis:{title:"TTFT P90 (ms)",tickformat:".2s"}},co);';
    html += '}});}';

    // Core charts
    html += 'if(cd.pareto.traces.length){var pxv=[...new Set(cd.pareto.traces.flatMap(function(t){return t.x}))].sort(function(a,b){return a-b});Plotly.newPlot("p1",cd.pareto.traces.map(function(t){return{x:t.x,y:t.y,text:t.text,name:t.name,mode:"markers+lines",marker:{size:14,color:t.color,symbol:"diamond",line:{width:2,color:"white"}},line:{width:2,dash:"dot"},hovertemplate:"<b>%{text}</b><extra></extra>"}}),{...lo,xaxis:{title:"GPUs",tickvals:pxv},yaxis:{title:"TTFT P90 (ms)"},showlegend:true},co);}';
    html += 'if(cd.scatter.traces.length){Plotly.newPlot("p2",cd.scatter.traces.map(function(t){return{x:t.x,y:t.y,text:t.text,name:t.name,mode:"markers",marker:{size:t.sizes,color:t.color,opacity:0.7,line:{width:1,color:"white"}},hovertemplate:"<b>%{text}</b><extra></extra>"}}),{...lo,xaxis:{title:"TTFT P90 (ms)"},yaxis:{title:"Throughput P90 (req/s)"},showlegend:true},co);}';
    html += 'if(cd.efficiency.configs.length){Plotly.newPlot("p3",[{x:cd.efficiency.configs,y:cd.efficiency.values,type:"bar",marker:{color:cd.efficiency.colors},text:cd.efficiency.values.map(function(v){return v!=null?v.toFixed(3):""}),textposition:"outside",textfont:{size:11,color:"#333"},cliponaxis:false,constraintext:"none"}],{...lo,margin:{...lo.margin,b:120},xaxis:{tickangle:-45},yaxis:{title:"req/s/GPU"}},co);}';
    html += 'if(cd.architecture.architectures.length){var a=cd.architecture;Plotly.newPlot("p4",[{x:a.architectures,y:a.avg_ttft,type:"bar",marker:{color:"#3b82f6"},text:a.avg_ttft.map(function(v){return fmtSI(v)+" ms"}),textposition:"auto",name:"Avg TTFT P90",xaxis:"x",yaxis:"y"},{x:a.architectures,y:a.best_ttft,type:"bar",marker:{color:"#93c5fd"},text:a.best_ttft.map(function(v){return fmtSI(v)+" ms"}),textposition:"auto",name:"Best TTFT P90",xaxis:"x",yaxis:"y"},{x:a.architectures,y:a.avg_throughput,type:"bar",marker:{color:"#f59e0b"},text:a.avg_throughput.map(function(v){return v.toFixed(2)+" req/s"}),textposition:"auto",name:"Avg Throughput P90",xaxis:"x2",yaxis:"y2"}],{...lo,margin:{t:30,b:50,l:60,r:60},barmode:"group",showlegend:true,legend:{x:0,y:1.18,orientation:"h"},xaxis:{domain:[0,0.45]},yaxis:{title:"TTFT (ms)",titlefont:{color:"#3b82f6"},tickformat:".2s"},xaxis2:{domain:[0.55,1],anchor:"y2"},yaxis2:{title:"Throughput (req/s)",anchor:"x2",titlefont:{color:"#f59e0b"}}},co);}';

    // PD split charts
    html += 'var pd=ar.filter(function(r){return r.architecture==="PD"});';
    html += 'if(pd.length&&document.getElementById("pd-ttft")){';
    html += '  pd.sort(function(a,b){return a.prefill_pods-b.prefill_pods});';
    html += '  var lbls=pd.map(function(r){return r.prefill_pods+"P : "+r.decode_pods+"D"});';
    html += '  var ttft=pd.map(function(r){return r.ttft_p90});';
    html += '  var best=Math.min.apply(null,ttft);';
    html += '  var clrs=ttft.map(function(v){return v===best?"#10b981":"#3b82f6"});';
    html += '  var szs=ttft.map(function(v){return v===best?22:14});';
    html += '  var ttftAnn=arrAnn(lbls,ttft,{color:"#1e40af",decimals:0,suffix:"ms",spread:35});';
    html += '  Plotly.newPlot("pd-ttft",[{x:lbls,y:ttft,type:"scatter",mode:"lines+markers",line:{color:"#3b82f6",width:3,shape:"spline"},marker:{color:clrs,size:szs,symbol:"circle",line:{width:2,color:"white"}},fill:"tozeroy",fillcolor:"rgba(59,130,246,0.08)"}],{...lo,height:500,margin:{t:30,b:80,l:60,r:20},xaxis:{title:"Prefill : Decode Pod Ratio"},yaxis:{title:"TTFT P90 (ms)",tickformat:".2s"},showlegend:false,annotations:ttftAnn},co);';
    html += '  var tput=pd.map(function(r){return r.throughput_p90});';
    html += '  Plotly.newPlot("pd-tradeoff",[';
    html += '    {x:lbls,y:ttft,name:"TTFT P90",type:"scatter",mode:"lines+markers",line:{color:"#2563eb",width:3,shape:"spline"},marker:{color:"#2563eb",size:10}},';
    html += '    {x:lbls,y:tput,name:"Throughput P90",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3,shape:"spline"},marker:{color:"#f59e0b",size:10,symbol:"diamond"}}';
    html += '  ],{...lo,height:500,margin:{t:30,b:80,l:60,r:60},xaxis:{title:"Prefill : Decode Pod Ratio"},yaxis:{title:"TTFT (ms)",titlefont:{color:"#3b82f6"},tickfont:{color:"#3b82f6"}},yaxis2:{title:"Throughput (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},showlegend:true,legend:{x:0,y:1.18,orientation:"h"}},co);';
    html += '}';

    // Step 9 latency search charts
    if (data.latency_search && data.latency_search.by_architecture) {
        html += 'var lsData=' + JSON.stringify(data.latency_search) + ';';
        html += 'if(lsData&&lsData.by_architecture){var acfg=lsData.arch_configs||{};Object.keys(lsData.by_architecture).forEach(function(arch,ai){';
        html += '  var el=document.getElementById("dl-step9-chart-"+ai);if(!el)return;';
        html += '  var trials=lsData.by_architecture[arch];';
        html += '  var tgtMs=trials[0].target_ms;var tgtPct=trials[0].target_percentile||"p90";var mk="ttft_"+tgtPct;';
        html += '  var cl=acfg[arch]||arch.toUpperCase();';
        html += '  var xl=trials.map(function(t){return cl+" c="+t.concurrency});';
        html += '  var ll=trials.map(function(t){return t[mk]});';
        html += '  var tpk="throughput_"+tgtPct;var tp=trials.map(function(t){return t[tpk]!=null?t[tpk]:t.throughput_p90});';
        html += '  var mc=trials.map(function(t){return t.meets_sla?"#10b981":"#ef4444"});';
        html += '  var ms=trials.map(function(t){return t.search_phase==="ramp_up"?"circle":"diamond"});';
        html += '  Plotly.newPlot(el,[';
        html += '    {x:xl,y:ll,name:"TTFT "+tgtPct.toUpperCase(),type:"scatter",mode:"lines+markers",line:{color:"#3b82f6",width:3},marker:{color:mc,size:14,symbol:ms,line:{width:2,color:"white"}}},';
        html += '    {x:xl,y:tp,name:"Throughput "+tgtPct.toUpperCase(),type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:2,dash:"dot"},marker:{color:"#f59e0b",size:8,symbol:"square"}}';
        html += '  ],{...lo,height:430,margin:{t:40,b:90,l:60,r:60},';
        html += '    title:{text:cl+" — Concurrency vs Latency",font:{size:14}},';
        html += '    xaxis:{title:"Test Configuration",tickangle:-25},';
        html += '    yaxis:{title:"TTFT "+tgtPct.toUpperCase()+" (ms)",side:"left",titlefont:{color:"#3b82f6"}},';
        html += '    yaxis2:{title:"Throughput "+tgtPct.toUpperCase()+" (req/s)",side:"right",overlaying:"y",titlefont:{color:"#f59e0b"},tickfont:{color:"#f59e0b"}},';
        html += '    showlegend:true,legend:{x:0,y:1.15,orientation:"h"},';
        html += '    shapes:[{type:"line",x0:-0.5,x1:xl.length-0.5,y0:tgtMs,y1:tgtMs,yref:"y",line:{color:"#ef4444",width:2,dash:"dash"}}],';
        html += '    annotations:[{x:xl.length-1,y:tgtMs,yref:"y",text:"SLA: "+tgtMs+" ms",showarrow:false,font:{color:"#ef4444",size:11},xanchor:"right",yanchor:"bottom",yshift:5,bgcolor:"rgba(255,255,255,0.85)"}]';
        html += '  },co);';
        html += '  var cel=document.getElementById("dl-step9-cost-"+ai);if(cel){';
        html += '    var st=[].concat(trials).sort(function(a,b){return a.concurrency-b.concurrency});';
        html += '    var cx=st.map(function(t){return t.concurrency});';
        html += '    var pcts=[{k:"ttft_p50",n:"TTFT P50",c:"#60a5fa",d:"dot"},{k:"ttft_p90",n:"TTFT P90",c:"#3b82f6",d:"solid"},{k:"ttft_p95",n:"TTFT P95",c:"#f59e0b",d:"dash"},{k:"ttft_p99",n:"TTFT P99",c:"#ef4444",d:"dashdot"}];';
        html += '    var ct=pcts.map(function(p){return{x:cx,y:st.map(function(t){return t[p.k]}),name:p.n,type:"scatter",mode:"lines+markers",line:{color:p.c,width:2.5,dash:p.d},marker:{color:p.c,size:8}}});';
        html += '    Plotly.newPlot(cel,ct,{...lo,height:400,margin:{t:40,b:60,l:70,r:30},title:{text:cl+" — Latency Cost of Throughput",font:{size:14}},xaxis:{title:"Concurrent Users"},yaxis:{title:"TTFT (ms)",tickformat:".2s"},showlegend:true,legend:{x:0,y:1.15,orientation:"h"},shapes:[{type:"line",x0:cx[0],x1:cx[cx.length-1],y0:tgtMs,y1:tgtMs,yref:"y",line:{color:"#ef4444",width:2,dash:"dash"}}],annotations:[{x:cx[cx.length-1],y:tgtMs,yref:"y",text:"SLA: "+tgtMs+" ms",showarrow:false,font:{color:"#ef4444",size:11},xanchor:"right",yanchor:"bottom",yshift:5}]},co);';
        html += '  }';
        html += '});}';
    }

    // vLLM charts
    html += 'if(cd.vllm&&cd.vllm.configs.length){var v=cd.vllm;';
    html += 'Plotly.newPlot("v1",[{x:v.configs,y:v.ttft.p50,name:"P50",type:"bar",marker:{color:pc.p50}},{x:v.configs,y:v.ttft.p90,name:"P90",type:"bar",marker:{color:pc.p90}},{x:v.configs,y:v.ttft.p95,name:"P95",type:"bar",marker:{color:pc.p95}},{x:v.configs,y:v.ttft.p99,name:"P99",type:"bar",marker:{color:pc.p99}}],{...vl,title:{text:"TTFT Percentiles"},xaxis:{tickangle:-35},yaxis:{title:"TTFT (ms)"}},co);';
    html += 'Plotly.newPlot("v2",[{x:v.configs,y:v.itl.p50,name:"P50",type:"bar",marker:{color:pc.p50}},{x:v.configs,y:v.itl.p90,name:"P90",type:"bar",marker:{color:pc.p90}},{x:v.configs,y:v.itl.p95,name:"P95",type:"bar",marker:{color:pc.p95}},{x:v.configs,y:v.itl.p99,name:"P99",type:"bar",marker:{color:pc.p99}}],{...vl,title:{text:"ITL Percentiles"},xaxis:{tickangle:-35},yaxis:{title:"ITL (ms)"}},co);';
    html += 'Plotly.newPlot("v3",[{x:v.configs,y:v.e2e.p50,name:"P50",type:"bar",marker:{color:pc.p50}},{x:v.configs,y:v.e2e.p90,name:"P90",type:"bar",marker:{color:pc.p90}},{x:v.configs,y:v.e2e.p95,name:"P95",type:"bar",marker:{color:pc.p95}},{x:v.configs,y:v.e2e.p99,name:"P99",type:"bar",marker:{color:pc.p99}}],{...vl,title:{text:"E2E Latency"},xaxis:{tickangle:-35},yaxis:{title:"E2E (seconds)"}},co);';
    html += 'Plotly.newPlot("v4",[{x:v.configs,y:v.token_rates.prompt,name:"Prompt Tokens/s",type:"bar",marker:{color:"#6366f1"}},{x:v.configs,y:v.token_rates.generation,name:"Generation Tokens/s",type:"bar",marker:{color:"#10b981"}}],{...vl,title:{text:"Token Throughput"},xaxis:{tickangle:-35},yaxis:{title:"Tokens/s"}},co);';
    html += 'Plotly.newPlot("v5",[{x:v.configs,y:v.request_state.running,name:"Running",type:"bar",marker:{color:"#3b82f6"}},{x:v.configs,y:v.request_state.waiting,name:"Waiting",type:"bar",marker:{color:"#ef4444"}},{x:v.configs,y:v.request_state.kv_cache,name:"KV Cache %",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b",width:3},marker:{size:10,symbol:"diamond",color:"#f59e0b"}}],{...vl,title:{text:"Request Queue & KV Cache"},margin:{...vl.margin,r:60},xaxis:{tickangle:-35},yaxis:{title:"Count"},yaxis2:{title:"KV Cache %",side:"right",overlaying:"y",range:[0,105]}},co);';
    html += 'Plotly.newPlot("v6",[{x:v.configs,y:v.time_breakdown.prefill,name:"Prefill",type:"bar",marker:{color:"#6366f1"}},{x:v.configs,y:v.time_breakdown.decode,name:"Decode",type:"bar",marker:{color:"#3b82f6"}},{x:v.configs,y:v.time_breakdown.queue,name:"Queue",type:"bar",marker:{color:"#94a3b8"}},{x:v.configs,y:v.time_breakdown.preemptions,name:"Preemptions/s",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#ef4444",width:3},marker:{size:10,symbol:"triangle-up",color:"#ef4444"}}],{...vl,barmode:"stack",title:{text:"Time Breakdown & Preemptions"},margin:{...vl.margin,r:60},xaxis:{tickangle:-35},yaxis:{title:"Time Rate (s/s)"},yaxis2:{title:"Preemptions/s",side:"right",overlaying:"y"}},co);';
    html += 'if(v.network&&v.network.pod_tx.some(function(x){return x>0})){Plotly.newPlot("v7",[{x:v.configs,y:v.network.pod_tx,name:"TX (MB/s)",type:"bar",marker:{color:"#3b82f6"}},{x:v.configs,y:v.network.pod_rx,name:"RX (MB/s)",type:"bar",marker:{color:"#10b981"}}],{...vl,title:{text:"Pod Network Throughput"},xaxis:{tickangle:-35},yaxis:{title:"MB/s"}},co);}';
    html += 'if(v.network&&v.network.ib_rx.some(function(x){return x>0})){Plotly.newPlot("v8",[{x:v.configs,y:v.network.ib_rx,name:"IB RX (GB/s)",type:"bar",marker:{color:"#8b5cf6"},text:v.network.ib_rx.map(function(x){return x>0?x.toFixed(2):""}),textposition:"outside",textfont:{size:11,color:"#1e293b"},cliponaxis:false,constraintext:"none"}],{...vl,title:{text:"InfiniBand RDMA Throughput"},xaxis:{tickangle:-35},yaxis:{title:"GB/s"}},co);}';
    html += '}';

    // After all charts rendered, hide non-active panes
    html += 'setTimeout(function(){document.querySelectorAll(".dl-pane").forEach(function(p){p.style.display="";});},100);';

    html += '<\/script></body></html>';

    const blob = new Blob([html], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `inferecipe-report-run-${runId}.html`;
    a.click();
    URL.revokeObjectURL(url);
}

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
