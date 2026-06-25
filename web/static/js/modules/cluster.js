// cluster.js — PVC listing, test plan generation, cluster scan

function fetchAvailablePVCs() {
    // Skip if already fetched
    if (pvcsFetched) {
        return;
    }
    pvcsFetched = true;
    socket.emit('list_pvcs', {});
}

document.getElementById('next-step5').addEventListener('click', () => {
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

        // Validate PVC size against model size
        if (config.model) {
            var sizeMatch = config.model.match(/(\d+\.?\d*)b/i);
            if (sizeMatch) {
                var modelSizeB = parseFloat(sizeMatch[1]);
                var minPvcGb = Math.ceil(modelSizeB * 1.5) + 5;
                var pvcSize = parseInt(document.getElementById('pvc-size-input').value) || 50;
                if (pvcSize < minPvcGb) {
                    logToConsole('❌ PVC size ' + pvcSize + 'Gi is too small for ' + config.model + '. Model needs ~' + Math.round(modelSizeB) + 'GB for weights + overhead. Minimum required: ' + minPvcGb + 'Gi.', 'error');
                    return;
                }
            }
        }
    }

    if (!maxGpus) {
        logToConsole('❌ Please scan cluster resources first', 'error');
        return;
    }

    // Validate node selection (skip when launcher presets are active)
    var hasPresets = config.cluster_resources && (config.cluster_resources.preset_max_gpus || config.cluster_resources.preset_nodes);
    if (!hasPresets && config.selected_nodes && config.selected_nodes.length > 0) {
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

    // Go to step 6 (EPP Config)
    goToStep(6);
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
    document.getElementById('config-summary-qps-mode').textContent = config.use_achievable_qps ? 'Sustainable Concurrency (auto-scaled)' : 'User-defined Concurrent Users';
    document.getElementById('config-summary-gpus').textContent = config.max_gpus || config.cluster_resources?.total_gpus;
    document.getElementById('config-summary-achievable-qps').textContent = config.use_achievable_qps ? 'Enabled' : 'Disabled';
    document.getElementById('config-summary-pvc').textContent = config.existing_pvc_name || 'Not set';
    document.getElementById('config-summary-namespace').textContent = config.namespace || 'Not set';
    const imgEl2 = document.getElementById('config-summary-image');
    if (imgEl2) imgEl2.textContent = config.image || document.getElementById('image-repo-input')?.value || '-';
    const schImgEl2 = document.getElementById('config-summary-scheduler-image');
    if (schImgEl2) schImgEl2.textContent = config.scheduler_image || document.getElementById('scheduler-image-input')?.value || '-';
    const atEl2 = document.getElementById('config-summary-autotune');
    if (atEl2) atEl2.textContent = config.advanced_vllm_custom_enabled ? 'Enabled' : 'Upstream Default';
    const eppEl2 = document.getElementById('config-summary-epp');
    if (eppEl2) {
        const presetNames = { balanced: 'Balanced (3:2:2)', cache_optimized: 'Cache Optimized (5:1:2)', queue_balanced: 'Queue Balanced (2:2:3)', custom: 'Custom' };
        eppEl2.textContent = config.epp_custom_enabled ? (presetNames[config.epp_preset] || config.epp_preset) : 'Upstream Default';
    }
    const eppSmartEl2 = document.getElementById('config-summary-epp-smart');
    if (eppSmartEl2) eppSmartEl2.textContent = config.epp_benchmark ? 'Enabled' : 'Upstream Default';
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

    // Save config to database before starting — ensures backend reads latest toggle states
    saveConfig();

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
        tp_pair_top_n: config.tp_pair_top_n || 4,
        allow_asymmetric_tp: !!config.allow_asymmetric_tp,
        pd_search_mode: config.pd_search_mode || 'smart',
        selected_nodes: config.selected_nodes || [],
        workload_mode: config.workload_mode || 'synthetic',
        dataset_source: config.dataset_source || null,
        dataset_column: config.dataset_column || null,
        dataset_max_output: config.dataset_max_output || 256,
        rate_type: config.rate_type || 'concurrent',
        prefix_cache_hit_pct: config.prefix_cache_hit_pct || 0,
        prefix_cache_mode: config.prefix_cache_mode || 'identical',
        prefix_cache_groups: config.prefix_cache_groups || 5,
        run_description: config.run_description || '',
        epp_custom_enabled: config.epp_custom_enabled === true,
        epp_preset: config.epp_preset || 'balanced',
        epp_benchmark: config.epp_benchmark || false,
        epp_config: config.epp_config || null,
        advanced_vllm_custom_enabled: config.advanced_vllm_custom_enabled === true,
        advanced_vllm: config.advanced_vllm || null,
        image: config.image || null,
        scheduler_image: config.scheduler_image || null,
        single_test_architecture: config.single_test_architecture || null,
        single_test_tp: config.single_test_tp || (document.getElementById('single-test-tp') ? parseInt(document.getElementById('single-test-tp').value) || null : null),
        single_test_replicas: config.single_test_replicas || (document.getElementById('single-test-replicas') ? parseInt(document.getElementById('single-test-replicas').value) || null : null),
        single_test_prefill_tp: config.single_test_prefill_tp || (document.getElementById('single-test-prefill-tp') ? parseInt(document.getElementById('single-test-prefill-tp').value) || null : null),
        single_test_decode_tp: config.single_test_decode_tp || (document.getElementById('single-test-decode-tp') ? parseInt(document.getElementById('single-test-decode-tp').value) || null : null),
        single_test_prefill_pods: config.single_test_prefill_pods || (document.getElementById('single-test-prefill-pods') ? parseInt(document.getElementById('single-test-prefill-pods').value) || null : null),
        single_test_decode_pods: config.single_test_decode_pods || (document.getElementById('single-test-decode-pods') ? parseInt(document.getElementById('single-test-decode-pods').value) || null : null),
        per_node_storage: !!config.per_node_storage,
        node_nfs_pvcs: config.node_nfs_pvcs || [],
        calibrated_load_enabled: !!config.calibrated_load_enabled,
        inferencex_sweep_enabled: !!config.inferencex_sweep_enabled
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
    const nextStep5Btn = document.getElementById('next-step5');
    nextStep5Btn.disabled = true;
    nextStep5Btn.textContent = 'Continue to EPP Config → (Scanning...)';
    nextStep5Btn.style.opacity = '0.6';

    logToConsole('🔍 Re-scanning cluster resources...', 'info');
    socket.emit('scan_cluster', {});
}

