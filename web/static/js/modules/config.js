// config.js — Configuration save/load, wizard UI state restoration

function saveConfig() {
    socket.emit('save_config', {
        config: config,
        current_step: currentStep
    });

    // Also keep in localStorage as fallback
    localStorage.setItem('inftune-config', JSON.stringify(config));
    localStorage.setItem('inftune-step', currentStep.toString());
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
    if (config.prefix_cache_groups && document.getElementById('prefix-cache-groups-slider')) {
        document.getElementById('prefix-cache-groups-slider').value = config.prefix_cache_groups;
        document.getElementById('prefix-cache-groups-value').textContent = config.prefix_cache_groups;
    }
    if (config.prefix_cache_mode) {
        setPrefixCacheMode(config.prefix_cache_mode);
    } else {
        updatePrefixCacheModeDesc();
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
    const saved = localStorage.getItem('inftune-config');
    const savedStep = localStorage.getItem('inftune-step');

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

    // Check if running from launcher with presets
    var hasPresets = data.preset_max_gpus || (data.preset_nodes && data.preset_nodes.length > 0);

    if (hasPresets) {
        // Launcher mode: set values silently, replace visible UI with read-only card
        var maxGpuSelect = document.getElementById('max-gpu-select');
        if (maxGpuSelect) {
            maxGpuSelect.innerHTML = '<option value="' + (data.preset_max_gpus || data.total_gpus) + '" selected>' + (data.preset_max_gpus || data.total_gpus) + ' GPUs</option>';
            maxGpuSelect.style.display = 'none';
        }
        document.getElementById('node-select-group').style.display = 'none';

        var presetHtml = '<div style="background:#F0F9FA;border:1.5px solid #2A7B88;border-radius:10px;padding:16px 20px;margin-top:12px">';
        presetHtml += '<div style="font-weight:700;color:#2A7B88;font-size:0.95em;margin-bottom:8px;">⚡ Configured by Launcher</div>';
        if (data.preset_max_gpus) {
            presetHtml += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;"><span style="font-size:1.2em">🎛️</span><span><strong>' + data.preset_max_gpus + ' GPUs</strong> allocated</span></div>';
        }
        if (data.preset_nodes && data.preset_nodes.length > 0) {
            presetHtml += '<div style="display:flex;align-items:flex-start;gap:8px;"><span style="font-size:1.2em">📍</span><div><strong>Pinned to ' + data.preset_nodes.length + ' node' + (data.preset_nodes.length > 1 ? 's' : '') + ':</strong>';
            data.preset_nodes.forEach(function(n) {
                presetHtml += '<div style="font-size:0.88em;color:#4A4A4A;margin-top:2px;">' + n + '</div>';
            });
            presetHtml += '</div></div>';
            document.getElementById('enable-node-select').checked = true;
        }
        presetHtml += '</div>';

        // Insert preset card into max-gpu-group without destroying existing elements
        var gpuGroup = document.getElementById('max-gpu-group');
        gpuGroup.style.display = 'block';
        var existingPreset = document.getElementById('launcher-preset-card');
        if (existingPreset) existingPreset.remove();
        var presetDiv = document.createElement('div');
        presetDiv.id = 'launcher-preset-card';
        presetDiv.innerHTML = presetHtml;
        gpuGroup.appendChild(presetDiv);

        // Hide the GPU limit label/description since preset replaces it
        var gpuLabels = gpuGroup.querySelectorAll('h3, p, .info-label');
        gpuLabels.forEach(function(el) { el.style.display = 'none'; });

    } else {
        // Standalone mode: show full interactive GPU/node selection
        const maxGpuSelect = document.getElementById('max-gpu-select');
        if (maxGpuSelect) {
            maxGpuSelect.innerHTML = `<option value="${data.total_gpus}" selected>All (${data.total_gpus} GPUs)</option>`;

            const gpuOptions = [];
            for (let i = 1; i < data.total_gpus; i++) gpuOptions.push(i);
            gpuOptions.forEach(count => {
                const option = document.createElement('option');
                option.value = count;
                option.textContent = `${count} GPU${count > 1 ? 's' : ''}`;
                maxGpuSelect.appendChild(option);
            });

            if (config.max_gpus) maxGpuSelect.value = config.max_gpus;

            const gpuUsageInfo = document.getElementById('gpu-usage-info');
            if (data.gpus_in_use && data.gpus_in_use > 0) {
                gpuUsageInfo.innerHTML = `⚠️ <strong>${data.gpus_in_use} GPU${data.gpus_in_use > 1 ? 's' : ''}</strong> currently in use by other workloads. <strong>${data.gpus_available} GPU${data.gpus_available !== 1 ? 's' : ''}</strong> available.`;
                gpuUsageInfo.style.display = 'block';
            } else {
                gpuUsageInfo.style.display = 'none';
            }
        }

        document.getElementById('max-gpu-group').style.display = 'block';

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

            var nodeEnabled = config.selected_nodes && config.selected_nodes.length > 0;
            document.getElementById('enable-node-select').checked = nodeEnabled;
            document.getElementById('node-select-list').style.opacity = nodeEnabled ? '1' : '0.5';
            document.getElementById('node-select-list').style.pointerEvents = nodeEnabled ? 'auto' : 'none';
            document.querySelectorAll('.node-select-cb').forEach(function(cb) { cb.disabled = !nodeEnabled; });
        }
    }

    // Show re-scan button
    document.getElementById('rescan-cluster-btn').style.display = 'inline-block';
}

// Node selection toggle
document.getElementById('enable-node-select').addEventListener('change', function() {
    var cbs = document.querySelectorAll('.node-select-cb');
    cbs.forEach(function(cb) { cb.disabled = !this.checked; }.bind(this));
    document.getElementById('node-select-list').style.opacity = this.checked ? '1' : '0.5';
    if (!this.checked) {
        document.getElementById('node-select-warning').style.display = 'none';
        cbs.forEach(function(cb) { cb.checked = false; cb.closest('label').style.borderColor = '#e2e8f0'; });
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
    document.getElementById('config-summary-qps-mode').textContent = config.use_achievable_qps ? 'Sustainable Concurrency (auto-scaled)' : 'User-defined Concurrent Users';
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
var allModels = [];
var displayedModels = 0;
var modelsPerPage = 16;
