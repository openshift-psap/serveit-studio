// navigation.js — Wizard step navigation, optimization start/stop

function selectNetwork(netId) {
    document.getElementById('selected-network-type').value = netId;
    config.network_type = netId;
    document.querySelectorAll('.net-card').forEach(function(card) {
        var selected = card.dataset.net === netId;
        card.style.borderColor = selected ? '#2A7B88' : '#CCC';
        card.style.background = selected ? '#F0F9FA' : 'white';
    });
    // Show/hide NAD selector for RDMA networks
    var nadSection = document.getElementById('nad-selector-section');
    if (nadSection) {
        nadSection.style.display = (netId !== 'eth0' && window._availableNads && window._availableNads.length > 0) ? 'block' : 'none';
    }
    saveConfig();
}

function selectNad(nadName, nadNamespace) {
    config.rdma_network_annotation = JSON.stringify([{"name": nadName, "namespace": nadNamespace}]);
    var sel = document.getElementById('nad-select');
    if (sel) sel.value = nadName + '/' + nadNamespace;
    saveConfig();
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
    const stepTitles = {1:'Goal', 2:'Model', 3:'Workload', 4:'Test Config', 5:'Infrastructure & Deployment', 6:'EPP Config', 7:'Review & Run'};
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
    if (step > 4 && config.cluster_resources) {
        document.getElementById('step5-value').textContent = `${config.cluster_resources.total_gpus} GPUs`;
    }
    if (step > 5) {
        const presetLabels = {balanced:'Balanced', cache_optimized:'Cache Opt.', queue_balanced:'Queue Bal.', latency_aware:'Latency', custom:'Custom'};
        document.getElementById('step6-value').textContent = presetLabels[config.epp_preset] || 'Balanced';
    }
    if (step === 6) {
        updateEppAutoSuggestion();
        if (config.epp_preset) setEppPreset(config.epp_preset);
    }
    if (step === 5) {
        // Show/hide single test deployment config
        if (typeof updateSingleTestVisibility === 'function') updateSingleTestVisibility();

        // Check if cluster resources already scanned
        if (config.cluster_resources && document.getElementById('cluster-resources').style.display !== 'none') {
            // Resources already loaded - enable button immediately
            const nextStep5Btn = document.getElementById('next-step5');
            nextStep5Btn.disabled = false;
            nextStep5Btn.textContent = 'Continue to EPP Config →';
            nextStep5Btn.style.opacity = '1';
        } else {
            // Auto-scan cluster resources when entering step 5
            // Disable next button and show scanning status
            const nextStep5Btn = document.getElementById('next-step5');
            nextStep5Btn.disabled = true;
            nextStep5Btn.textContent = 'Continue to EPP Config → (Scanning...)';
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
    localStorage.removeItem('serveit-console');
});

var _configSyncLock = false;
socket.on('config_updated', function(data) {
    if (_configSyncLock) return;
    _configSyncLock = true;
    // Another client updated config - sync our state without saving back
    config = { ...config, ...data.config };
    currentStep = (data.current_step !== null && data.current_step !== undefined) ? data.current_step : 1;

    updateUIFromConfig();
    goToStep(currentStep, true);
    setTimeout(function() { _configSyncLock = false; }, 100);
});

socket.on('load_config_result', function(data) {
    if (data.success) {
        console.log('Loaded config from server:', data);
        config = { ...config, ...data.config };
        if (data.namespace) {
            config.namespace = data.namespace;
            var instanceName = data.namespace.replace(/^serveit-/, '');
            var bcEl = document.getElementById('breadcrumb-instance');
            var bcSep = document.getElementById('breadcrumb-instance-sep');
            if (bcEl && instanceName) {
                bcEl.textContent = instanceName;
                bcEl.classList.remove('hidden');
                if (bcSep) bcSep.classList.remove('hidden');
            }
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

    // Provider display
    let providerName = data.provider || 'Unknown';
    const providerMap = {'ibm_cloud':'IBM Cloud','coreweave':'CoreWeave','baremetal':'Bare Metal','aws':'AWS','gcp':'GCP','azure':'Azure'};
    if (providerMap[providerName]) providerName = providerMap[providerName];

    const detectedDiv = document.getElementById('detected-config-summary');
    if (detectedDiv) {
        detectedDiv.innerHTML = '<div style="font-size:0.92em;color:#4A4A4A;"><strong>Provider:</strong> ' + providerName +
            (data.has_rdma ? ' &nbsp;·&nbsp; <strong>RDMA:</strong> Available' : '') + '</div>';
    }

    // Network selection cards
    const networkCards = document.getElementById('network-select-cards');
    const networks = data.available_networks || [];
    const autoDetected = data.network_type || 'eth0';
    const savedNetwork = config.network_type || autoDetected;

    if (networkCards && networks.length > 0) {
        let cardsHtml = '';
        const icons = {'eth0':'🔌','nad':'🔗','dra':'⚡','shared_device':'📡'};
        networks.forEach(function(net) {
            const isSelected = net.id === savedNetwork;
            const isRecommended = net.id === autoDetected;
            const disabled = !net.available;
            const borderColor = isSelected ? '#2A7B88' : (disabled ? '#E0E0E0' : '#CCC');
            const bg = isSelected ? '#F0F9FA' : (disabled ? '#F8F8F8' : 'white');
            const opacity = disabled ? '0.5' : '1';
            const cursor = disabled ? 'not-allowed' : 'pointer';

            cardsHtml += '<div class="net-card" data-net="' + net.id + '" ' +
                'onclick="' + (disabled ? '' : 'selectNetwork(\'' + net.id + '\')') + '" ' +
                'style="border:2px solid ' + borderColor + ';border-radius:10px;padding:14px;background:' + bg +
                ';opacity:' + opacity + ';cursor:' + cursor + ';transition:border-color 0.12s;">';
            cardsHtml += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">';
            cardsHtml += '<span style="font-size:1.3em;">' + (icons[net.id] || '🌐') + '</span>';
            cardsHtml += '<span style="font-weight:700;font-size:0.95em;color:' + (disabled ? '#999' : '#1A1A1A') + ';">' + net.name + '</span>';
            if (net.rdma && net.available) cardsHtml += '<span style="font-size:0.7em;background:#E0F2F4;color:#2A7B88;padding:1px 6px;border-radius:4px;">RDMA</span>';
            cardsHtml += '</div>';
            cardsHtml += '<div style="font-size:0.8em;color:#999;line-height:1.4;">' + net.description + '</div>';
            if (!net.available && net.reason) cardsHtml += '<div style="font-size:0.75em;color:#dc2626;margin-top:4px;">' + net.reason + '</div>';
            cardsHtml += '</div>';
        });
        networkCards.innerHTML = cardsHtml;
        document.getElementById('selected-network-type').value = savedNetwork;

        // Build NAD selector dropdown for RDMA network types
        var allNads = [];
        networks.forEach(function(net) {
            if (net.available_nads) {
                net.available_nads.forEach(function(nad) {
                    var key = nad.name + '/' + nad.namespace;
                    if (!allNads.some(function(n) { return n.name + '/' + n.namespace === key; })) {
                        allNads.push(nad);
                    }
                });
            }
        });
        window._availableNads = allNads;

        if (allNads.length > 0) {
            var nadHtml = '<div id="nad-selector-section" style="margin-top:12px;padding:12px 16px;background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;display:' +
                (savedNetwork !== 'eth0' ? 'block' : 'none') + ';">';
            nadHtml += '<label style="font-weight:600;font-size:0.9em;color:#0c4a6e;margin-bottom:6px;display:block;">Network Attachment Definition</label>';
            nadHtml += '<div style="font-size:0.8em;color:#075985;margin-bottom:8px;">Select which NAD to attach to inference pods for RDMA communication</div>';
            nadHtml += '<select id="nad-select" onchange="var v=this.value.split(\'/\');selectNad(v[0],v[1]);" style="width:100%;padding:8px 12px;border:1.5px solid #bae6fd;border-radius:6px;font-size:0.9em;">';
            var savedNad = config.rdma_network_annotation ? JSON.parse(config.rdma_network_annotation)[0] : null;
            allNads.forEach(function(nad) {
                var val = nad.name + '/' + nad.namespace;
                var selected = savedNad && savedNad.name === nad.name && savedNad.namespace === nad.namespace;
                var label = nad.name + ' (' + nad.namespace + ')';
                nadHtml += '<option value="' + val + '"' + (selected ? ' selected' : '') + '>' + label + '</option>';
            });
            nadHtml += '</select></div>';
            networkCards.insertAdjacentHTML('afterend', nadHtml);

            // Auto-select first NAD if none saved
            if (!config.rdma_network_annotation && allNads.length > 0) {
                // Prefer multi-nic-inference
                var preferred = allNads.find(function(n) { return n.name === 'multi-nic-inference'; }) || allNads[0];
                selectNad(preferred.name, preferred.namespace);
            }
        }
    }

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
    if (config.storage_class) {
        select.value = config.storage_class;
    }

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

    // Display GPU usage information (hide in launcher mode)
    var _hasPresets = data.preset_max_gpus || (data.preset_nodes && data.preset_nodes.length > 0);
    const gpuUsageInfo = document.getElementById('gpu-usage-info');
    if (!_hasPresets && data.gpus_in_use && data.gpus_in_use > 0) {
        gpuUsageInfo.innerHTML = `⚠️ <strong>${data.gpus_in_use} GPU${data.gpus_in_use > 1 ? 's' : ''}</strong> currently in use by other workloads. <strong>${data.gpus_available} GPU${data.gpus_available !== 1 ? 's' : ''}</strong> available.`;
        gpuUsageInfo.style.display = 'block';
    } else {
        gpuUsageInfo.style.display = 'none';
    }

    // Show max GPU selection group
    document.getElementById('max-gpu-group').style.display = 'block';

    // Populate node selection checkboxes (skip if launcher presets are active)
    var hasPresets = data.preset_max_gpus || (data.preset_nodes && data.preset_nodes.length > 0);
    if (!hasPresets && data.nodes_detail && data.nodes_detail.length > 0) {
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

        var nodeEnabled = config.selected_nodes && config.selected_nodes.length > 0;
        document.getElementById('enable-node-select').checked = nodeEnabled;
        document.getElementById('node-select-list').style.opacity = nodeEnabled ? '1' : '0.5';
        document.querySelectorAll('.node-select-cb').forEach(function(cb) { cb.disabled = !nodeEnabled; });
        if (nodeEnabled) validateNodeSelection();
    }

    // Hide scanning status and show resources section
    document.getElementById('scanning-status').style.display = 'none';
    document.getElementById('cluster-resources').style.display = 'block';

    // Show re-scan button
    document.getElementById('rescan-cluster-btn').style.display = 'inline-block';

    // Enable Continue to Review button
    const nextStep5Btn = document.getElementById('next-step5');
    nextStep5Btn.disabled = false;
    nextStep5Btn.textContent = 'Continue to Review →';
    nextStep5Btn.style.opacity = '1';

    // Store in config
    config.cluster_resources = data;
    saveConfig();

    // Re-apply single test visibility after scan rebuilds the UI
    if (typeof updateSingleTestVisibility === 'function') updateSingleTestVisibility();
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
            var dbNs = (config.namespace || '').replace(/^serveit-/, '') || 'optimizer';
            var dbDate = new Date().toISOString().slice(0, 10);
            a.download = 'serveit-' + dbNs + '-' + dbDate + '.db.gz';
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

// Download raw data button — compress test artifacts + results, then download
document.getElementById('download-raw-indicator').addEventListener('click', () => {
    logToConsole('\n📦 Compressing raw test data for download...', 'info');

    var modal = document.getElementById('compress-modal');
    var bar = document.getElementById('compress-bar');
    var pctEl = document.getElementById('compress-percent');
    var statusEl = document.getElementById('compress-status');
    var sizeEl = document.getElementById('compress-size-info');

    bar.style.width = '0%';
    pctEl.textContent = '0%';
    statusEl.textContent = 'Compressing raw data...';
    sizeEl.textContent = '';
    modal.classList.add('active');

    socket.emit('compress_raw_data');
});

socket.on('raw_compression_progress', function(data) {
    var bar = document.getElementById('compress-bar');
    var pctEl = document.getElementById('compress-percent');
    var statusEl = document.getElementById('compress-status');
    var sizeEl = document.getElementById('compress-size-info');

    bar.style.width = data.percent + '%';
    pctEl.textContent = data.percent + '%';
    if (data.status) statusEl.textContent = data.status;
    if (data.original_size) {
        var mb = (data.original_size / (1024 * 1024)).toFixed(1);
        sizeEl.textContent = 'Total raw data: ' + mb + ' MB';
    }
});

socket.on('raw_compression_complete', function(data) {
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

    fetch('/api/download_raw_data')
        .then(function(response) {
            if (!response.ok) throw new Error('Download failed');
            return response.blob();
        })
        .then(function(blob) {
            var url = window.URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            var dbNs = (config.namespace || '').replace(/^serveit-/, '') || 'optimizer';
            var dbDate = new Date().toISOString().slice(0, 10);
            a.download = 'serveit-' + dbNs + '-raw-data-' + dbDate + '.tar.gz';
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            logToConsole('   Raw data downloaded successfully', 'success');
            setTimeout(function() {
                document.getElementById('compress-modal').classList.remove('active');
            }, 1000);
        })
        .catch(function(err) {
            logToConsole('   Failed to download: ' + err.message, 'error');
            document.getElementById('compress-modal').classList.remove('active');
        });
});

socket.on('raw_compression_error', function(data) {
    logToConsole('   Raw data compression failed: ' + data.error, 'error');
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

