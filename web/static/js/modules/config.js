// config.js — Configuration save/load, wizard UI state restoration

function saveConfig() {
    socket.emit('save_config', {
        config: config,
        current_step: currentStep
    });

    // Also keep in localStorage as fallback
    localStorage.setItem('serveit-config', JSON.stringify(config));
    localStorage.setItem('serveit-step', currentStep.toString());
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
        updateSingleTestVisibility();
    }

    // Restore single test deployment fields — must happen BEFORE selectSingleTestArch
    // which calls syncSingleTestToConfig and would overwrite config with HTML defaults
    if (config.single_test_architecture) {
        var el;
        el = document.getElementById('single-test-prefill-tp');
        if (el && config.single_test_prefill_tp) el.value = config.single_test_prefill_tp;
        el = document.getElementById('single-test-decode-tp');
        if (el && config.single_test_decode_tp) el.value = config.single_test_decode_tp;
        el = document.getElementById('single-test-prefill-pods');
        if (el && config.single_test_prefill_pods) el.value = config.single_test_prefill_pods;
        el = document.getElementById('single-test-decode-pods');
        if (el && config.single_test_decode_pods) el.value = config.single_test_decode_pods;
        el = document.getElementById('single-test-tp');
        if (el && config.single_test_tp) el.value = config.single_test_tp;
        el = document.getElementById('single-test-replicas');
        if (el && config.single_test_replicas) el.value = config.single_test_replicas;
        selectSingleTestArch(config.single_test_architecture);
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
            document.getElementById('custom-model-enabled').checked = true;
            document.getElementById('custom-model-body').style.display = 'block';
            var cmSw = document.getElementById('custom-model-switch');
            if (cmSw) { cmSw.style.background = '#0ea5e9'; cmSw.querySelector('span').style.transform = 'translateX(18px)'; }
            var gs = document.getElementById('model-gallery-section');
            var od = document.getElementById('model-or-divider');
            if (gs) { gs.style.opacity = '0.35'; gs.style.pointerEvents = 'none'; }
            if (od) { od.style.opacity = '0.35'; }
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
    if (document.getElementById('length-variation-enabled')) {
        var hasStdev = (config.isl_stdev && config.isl_stdev > 0) || (config.osl_stdev && config.osl_stdev > 0);
        document.getElementById('length-variation-enabled').checked = hasStdev;
        if (hasStdev) {
            if (config.isl_stdev) document.getElementById('isl-stdev-input').value = config.isl_stdev;
            if (config.osl_stdev) document.getElementById('osl-stdev-input').value = config.osl_stdev;
            var lvBody = document.getElementById('length-variation-body');
            if (lvBody) lvBody.style.display = 'block';
            var lvInner = document.getElementById('length-variation-inner');
            if (lvInner) lvInner.style.opacity = '1';
            var lvSw = document.getElementById('length-variation-switch');
            if (lvSw) { lvSw.style.background = '#15803d'; lvSw.querySelector('span').style.transform = 'translateX(18px)'; }
        }
    }
    if (config.length_unit) setLengthUnit(config.length_unit, true);
    if (document.getElementById('multi-turn-enabled')) {
        if (config.turns && config.turns > 1) {
            document.getElementById('multi-turn-enabled').checked = true;
            document.getElementById('turns-input').value = config.turns;
            var mtBody = document.getElementById('multi-turn-body');
            if (mtBody) mtBody.style.display = 'block';
            var mtInner = document.getElementById('multi-turn-inner');
            if (mtInner) mtInner.style.opacity = '1';
            var mtSw = document.getElementById('multi-turn-switch');
            if (mtSw) { mtSw.style.background = '#15803d'; mtSw.querySelector('span').style.transform = 'translateX(18px)'; }
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
        var on = config.latency_constraint_enabled || false;
        document.getElementById('latency-constraint-enabled').checked = on;
        if (on) {
            document.getElementById('latency-sla-body').style.display = 'block';
            var li = document.getElementById('latency-sla-inner');
            if (li) li.style.opacity = '1';
            var sw = document.getElementById('latency-sla-switch');
            if (sw) { sw.style.background = '#d97706'; sw.querySelector('span').style.transform = 'translateX(18px)'; }
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
    if (document.getElementById('allow-asymmetric-tp')) {
        var atpOn = config.allow_asymmetric_tp === true;
        document.getElementById('allow-asymmetric-tp').checked = atpOn;
        var atpSw = document.getElementById('asymmetric-tp-switch');
        if (atpSw) {
            atpSw.style.background = atpOn ? '#d97706' : '#ccc';
            atpSw.querySelector('span').style.transform = atpOn ? 'translateX(18px)' : 'translateX(0)';
        }
        var atpOpts = document.getElementById('asymmetric-tp-options');
        if (atpOpts) atpOpts.style.display = atpOn ? 'flex' : 'none';
        var dgp = document.getElementById('asymmetric-decode-gt-prefill');
        if (dgp) dgp.checked = config.asymmetric_allow_decode_gt_prefill !== false;
        var pgd = document.getElementById('asymmetric-prefill-gt-decode');
        if (pgd) pgd.checked = config.asymmetric_allow_prefill_gt_decode !== false;
    }

    // Restore calibrated load toggles
    if (document.getElementById('calibrated-load-enabled')) {
        var clOn = config.calibrated_load_enabled === true;
        document.getElementById('calibrated-load-enabled').checked = clOn;
        var clSw = document.getElementById('calibrated-load-switch');
        if (clSw) {
            clSw.style.background = clOn ? '#059669' : '#ccc';
            clSw.querySelector('span').style.transform = clOn ? 'translateX(18px)' : 'translateX(0)';
        }
        var ixRow = document.getElementById('inferencex-sweep-row');
        if (ixRow) { ixRow.style.opacity = clOn ? '1' : '0.4'; ixRow.style.pointerEvents = clOn ? 'auto' : 'none'; }
    }
    if (document.getElementById('inferencex-sweep-enabled')) {
        var ixOn = config.inferencex_sweep_enabled === true;
        document.getElementById('inferencex-sweep-enabled').checked = ixOn;
        var ixSw = document.getElementById('inferencex-sweep-switch');
        if (ixSw) {
            ixSw.style.background = ixOn ? '#059669' : '#ccc';
            ixSw.querySelector('span').style.transform = ixOn ? 'translateX(18px)' : 'translateX(0)';
        }
        var cslRow = document.getElementById('concurrency-sweep-levels-row');
        if (cslRow) cslRow.style.display = ixOn ? 'block' : 'none';
        if (config.concurrency_sweep_count && document.getElementById('concurrency-sweep-count')) {
            document.getElementById('concurrency-sweep-count').value = config.concurrency_sweep_count;
        }
        if (config.concurrency_sweep_step_pct && document.getElementById('concurrency-sweep-step-pct')) {
            document.getElementById('concurrency-sweep-step-pct').value = config.concurrency_sweep_step_pct;
        }
        if (config.concurrency_sweep_levels && document.getElementById('concurrency-sweep-levels')) {
            document.getElementById('concurrency-sweep-levels').value = config.concurrency_sweep_levels.join(', ');
        }
        if (document.getElementById('sweep-all-configs')) {
            var allOn = config.concurrency_sweep_all_configs === true;
            document.getElementById('sweep-all-configs').checked = allOn;
            var allSw = document.getElementById('sweep-all-switch');
            if (allSw) { allSw.style.background = allOn ? '#059669' : '#ccc'; allSw.querySelector('span').style.transform = allOn ? 'translateX(18px)' : 'translateX(0)'; }
            var maxRow = document.getElementById('sweep-max-configs-row');
            if (maxRow) maxRow.style.display = allOn ? 'flex' : 'none';
            if (config.concurrency_sweep_max_configs && document.getElementById('sweep-max-configs')) {
                document.getElementById('sweep-max-configs').value = config.concurrency_sweep_max_configs;
            }
        }
        if (document.getElementById('sweep-epp-tuned')) {
            var eppOn = config.concurrency_sweep_use_epp_tuned === true;
            document.getElementById('sweep-epp-tuned').checked = eppOn;
            var eppSw = document.getElementById('sweep-epp-switch');
            if (eppSw) { eppSw.style.background = eppOn ? '#059669' : '#ccc'; eppSw.querySelector('span').style.transform = eppOn ? 'translateX(18px)' : 'translateX(0)'; }
        }
    }

    // Restore cache sweep toggles
    if (document.getElementById('cache-sweep-enabled')) {
        var csOn = config.cache_sweep_enabled === true;
        document.getElementById('cache-sweep-enabled').checked = csOn;
        var csSw = document.getElementById('cache-sweep-switch');
        if (csSw) {
            csSw.style.background = csOn ? '#7c3aed' : '#ccc';
            csSw.querySelector('span').style.transform = csOn ? 'translateX(18px)' : 'translateX(0)';
        }
    }
    if (document.getElementById('cache-sweep-calibrated')) {
        var ccOn = config.cache_sweep_use_calibrated === true;
        document.getElementById('cache-sweep-calibrated').checked = ccOn;
        var ccSw = document.getElementById('cache-sweep-cal-switch');
        if (ccSw) {
            ccSw.style.background = ccOn ? '#7c3aed' : '#ccc';
            ccSw.querySelector('span').style.transform = ccOn ? 'translateX(18px)' : 'translateX(0)';
        }
    }
    if (config.cache_sweep_mode) {
        var radios = document.querySelectorAll('input[name="cache-sweep-mode"]');
        radios.forEach(function(r) { r.checked = r.value === config.cache_sweep_mode; });
        if (config.cache_sweep_mode === 'multi_group') {
            var gr = document.getElementById('cache-sweep-groups-row');
            if (gr) gr.style.display = 'flex';
        }
    }
    if (config.structured_prefix) {
        var csSpEl = document.getElementById('cache-sweep-structured-prefix');
        if (csSpEl) {
            csSpEl.checked = true;
            var csSpSw = document.getElementById('cache-sweep-structured-switch');
            if (csSpSw) { csSpSw.style.background = '#7c3aed'; csSpSw.querySelector('span').style.transform = 'translateX(18px)'; }
        }
    }
    if (config.cache_sweep_count && document.getElementById('cache-sweep-count')) {
        document.getElementById('cache-sweep-count').value = config.cache_sweep_count;
    }
    if (config.cache_sweep_step_pct && document.getElementById('cache-sweep-step-pct')) {
        document.getElementById('cache-sweep-step-pct').value = config.cache_sweep_step_pct;
    }
    if (config.cache_sweep_levels && document.getElementById('cache-sweep-levels')) {
        document.getElementById('cache-sweep-levels').value = config.cache_sweep_levels.join(', ');
    }
    if (config.cache_sweep_groups && document.getElementById('cache-sweep-groups')) {
        document.getElementById('cache-sweep-groups').value = config.cache_sweep_groups;
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

    // Restore prefix cache toggle + slider
    if (document.getElementById('prefix-cache-slider')) {
        var pv = config.prefix_cache_hit_pct || 0;
        document.getElementById('prefix-cache-slider').value = pv;
        document.getElementById('prefix-cache-value').textContent = pv + '%';
        if (pv > 0 && document.getElementById('prefix-cache-enabled')) {
            document.getElementById('prefix-cache-enabled').checked = true;
            var pcBody = document.getElementById('prefix-cache-body');
            if (pcBody) pcBody.style.display = 'block';
            var pcInner = document.getElementById('prefix-cache-inner');
            if (pcInner) pcInner.style.opacity = '1';
            var pcSw = document.getElementById('prefix-cache-switch');
            if (pcSw) { pcSw.style.background = '#15803d'; pcSw.querySelector('span').style.transform = 'translateX(18px)'; }
        }
    }
    if (config.structured_prefix) {
        var spEl = document.getElementById('structured-prefix-enabled');
        if (spEl) {
            spEl.checked = true;
            var spSw = document.getElementById('structured-prefix-switch');
            if (spSw) { spSw.style.background = '#15803d'; spSw.querySelector('span').style.transform = 'translateX(18px)'; }
        }
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

    // Restore EPP custom mode toggle
    if (document.getElementById('epp-custom-enabled')) {
        var eppCustom = !!config.epp_custom_enabled;
        document.getElementById('epp-custom-enabled').checked = eppCustom;
        var eppToggle = document.getElementById('epp-custom-toggle');
        if (eppToggle) { if (eppCustom) eppToggle.classList.add('active'); else eppToggle.classList.remove('active'); }
        var eppSection = document.getElementById('epp-custom-section');
        if (eppSection) eppSection.style.display = eppCustom ? 'block' : 'none';
    }

    // Restore EPP settings (update UI without re-saving)
    if (config.epp_preset && document.querySelector('[data-epp]')) {
        document.querySelectorAll('[data-epp]').forEach(function(el) {
            if (el.dataset.epp === config.epp_preset) {
                el.style.borderColor = 'var(--rh-red-primary)';
                el.style.background = 'linear-gradient(135deg,#fef2f2,#fee2e2)';
            } else {
                el.style.borderColor = '#cbd5e1';
                el.style.background = '#fafafa';
            }
        });
        var editor = document.getElementById('epp-custom-editor');
        if (editor) editor.style.display = config.epp_preset === 'custom' ? 'block' : 'none';
    }
    if (document.getElementById('epp-benchmark-enabled')) {
        var eppCb = document.getElementById('epp-benchmark-enabled');
        eppCb.checked = !!config.epp_benchmark;
        var eppInner = document.getElementById('epp-benchmark-inner');
        if (eppInner) eppInner.style.opacity = eppCb.checked ? '1' : '0.5';
    }
    if (config.epp_config && document.getElementById('epp-plugin-prefix-cache')) {
        var ec = config.epp_config;
        var plugins = ['prefix-cache', 'kv-cache', 'queue', 'slo', 'precise-prefix-cache', 'active-request', 'no-hit-lru', 'session-aware'];
        var keys = ['prefix_cache', 'kv_cache', 'queue', 'slo', 'precise_prefix_cache', 'active_request', 'no_hit_lru', 'session_aware'];
        for (var pi = 0; pi < plugins.length; pi++) {
            var plugEl = document.getElementById('epp-plugin-' + plugins[pi]);
            var wEl = document.getElementById('epp-weight-' + plugins[pi]);
            if (plugEl && ec[keys[pi]]) {
                plugEl.checked = !!ec[keys[pi]].enabled;
                if (wEl) wEl.value = ec[keys[pi]].weight || 0;
            }
        }
    }

    // Restore image selection (single input with full path)
    if (config.image && document.getElementById('image-repo-input')) {
        document.getElementById('image-repo-input').value = config.image;
    }

    // Restore advanced vLLM custom toggle
    if (document.getElementById('adv-vllm-custom-enabled')) {
        var advCustom = config.advanced_vllm_custom_enabled !== false;
        document.getElementById('adv-vllm-custom-enabled').checked = advCustom;
        var advBody = document.getElementById('advanced-vllm-body');
        if (advBody) advBody.style.display = advCustom ? 'block' : 'none';
        var advSection = document.getElementById('adv-vllm-custom-section');
        if (advSection) advSection.style.display = advCustom ? 'block' : 'none';
        var sw = document.getElementById('adv-vllm-switch');
        if (sw) { sw.style.background = advCustom ? '#64748b' : '#ccc'; sw.querySelector('span').style.transform = advCustom ? 'translateX(18px)' : 'translateX(0)'; }
    }

    // Restore extra env vars
    if (config.extra_env_vars && document.getElementById('adv-extra-env-text')) {
        document.getElementById('adv-extra-env-text').value = config.extra_env_vars.map(function(e) { return e.name + '=' + e.value; }).join('\n');
    }

    // Restore scheduler image
    if (config.scheduler_image && document.getElementById('scheduler-image-input')) {
        document.getElementById('scheduler-image-input').value = config.scheduler_image;
    }

    // Restore RHAIIS version dropdown and sync images
    if (document.getElementById('rhaiis-version-select')) {
        var rhaiisVer = config.rhaiis_version || '3.5.1';
        document.getElementById('rhaiis-version-select').value = rhaiisVer;
        var preset = (typeof RHAIIS_VERSIONS !== 'undefined') ? RHAIIS_VERSIONS[rhaiisVer] : null;
        if (preset) {
            var currentTag = (config.image || '').split(':').pop();
            if (currentTag !== preset.cuda) {
                applyRhaiisVersion(rhaiisVer);
            }
        } else if (rhaiisVer === 'custom' && typeof markImagesCustom === 'function') {
            markImagesCustom();
        }
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
        var pvcToggle = document.getElementById('existing-pvc-toggle');
        if (pvcToggle) pvcToggle.classList.add('active');

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
function setLengthUnit(unit, skipSave) {
    config.length_unit = unit;
    var toggle = document.getElementById('length-unit-toggle');
    if (toggle) {
        toggle.querySelectorAll('button').forEach(function(btn) {
            if (btn.dataset.unit === unit) {
                btn.style.background = '#059669';
                btn.style.color = 'white';
            } else {
                btn.style.background = 'white';
                btn.style.color = '#6b7280';
            }
        });
    }
    var hint = document.getElementById('length-unit-hint');
    var isChars = unit === 'characters';
    if (hint) hint.textContent = isChars ? '1 token ≈ 4 characters' : 'Tokens are the internal units LLMs process';

    var islLabel = document.getElementById('isl-label');
    var oslLabel = document.getElementById('osl-label');
    var islHelp = document.getElementById('isl-help');
    var oslHelp = document.getElementById('osl-help');
    var islVarHelp = document.getElementById('isl-var-help');
    var oslVarHelp = document.getElementById('osl-var-help');

    if (isChars) {
        if (islLabel) islLabel.textContent = 'Average Input Length (characters)';
        if (oslLabel) oslLabel.textContent = 'Average Output Length (characters)';
        if (islHelp) islHelp.textContent = 'How many characters of text your users typically send. For example, a 500-word prompt is about 2500 characters.';
        if (oslHelp) oslHelp.textContent = 'How many characters of text you want the AI to write. For example, a 200-word response is about 1000 characters.';
        if (islVarHelp) islVarHelp.textContent = 'Adds random variation in characters. For example, ISL=10000 with variation=5000 means prompts range from 10000 to 15000 characters.';
        if (oslVarHelp) oslVarHelp.textContent = 'Adds random variation in characters. For example, OSL=500 with variation=200 means replies range from 500 to 700 characters.';
    } else {
        if (islLabel) islLabel.textContent = 'Average Input Length (ISL)';
        if (oslLabel) oslLabel.textContent = 'Average Output Length (OSL)';
        if (islHelp) islHelp.textContent = 'How long is the text your users typically send? A word is roughly 1.3 tokens. For example, a 500-word prompt is about 650 tokens.';
        if (oslHelp) oslHelp.textContent = 'How long is the reply you want the AI to write? A 200-word response is roughly 260 tokens.';
        if (islVarHelp) islVarHelp.textContent = 'Adds random variation around your Average Input Length above. For example, ISL=3000 with variation=1000 means prompts range from 3000 to 4000 tokens. Leave empty for fixed-length prompts.';
        if (oslVarHelp) oslVarHelp.textContent = 'Adds random variation around your Average Output Length above. For example, OSL=100 with variation=50 means replies range from 100 to 150 tokens. Leave empty for fixed-length replies.';
    }
    if (!skipSave) saveConfig();
}

function loadConfig() {
    // Server config is authoritative — only use localStorage if server fails
    socket.emit('load_config');

    // Set a timeout: if server doesn't respond in 3s, fall back to localStorage
    window._serverConfigReceived = false;
    setTimeout(function() {
        if (window._serverConfigReceived) return;
        const saved = localStorage.getItem('serveit-config');
        const savedStep = localStorage.getItem('serveit-step');
        if (saved) {
            const loadedConfig = JSON.parse(saved);
            config = { ...config, ...loadedConfig };
            if (savedStep) currentStep = parseInt(savedStep);
            updateUIFromConfig();
            goToStep(currentStep, true);
        }
    }, 3000);
}

// Restore Cluster Resources display
function restoreClusterResources() {
    const data = config.cluster_resources;
    if (!data) return;

    // Apply GPU/node presets immediately from cached data
    applyGpuPresets(data);

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
    if (data.gpu_warning) {
        gpuDisplay = `<span style="color:#dc2626;">0 GPUs</span> <span style="color:#b45309;font-size:0.85em;">⚠️ ${data.gpu_warning}</span>`;
    } else if (data.gpu_vendor && data.gpu_vendor !== 'unknown') {
        gpuDisplay += ` × ${data.gpu_vendor}`;
        if (data.gpu_model && data.gpu_model !== 'unknown') {
            gpuDisplay += ` ${data.gpu_model}`;
        }
        gpuDisplay += ` (${Math.round(data.gpu_memory_per_gpu_mb / 1024)} GB each)`;
    } else {
        gpuDisplay += ` × ${data.gpu_type}`;
        gpuDisplay += ` (${Math.round(data.gpu_memory_per_gpu_mb / 1024)} GB each)`;
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
        const gpuNodeCount = data.gpu_node_count || 0;
        const gpusPerNode = data.max_gpus_per_node || 0;
        // Hide internal backing SCs (LSO local-nvme is internal to HPP)
        const hiddenSCs = new Set();
        data.storage_classes.forEach(sc => {
            if (sc.provisioner === 'kubernetes.io/no-provisioner') hiddenSCs.add(sc.name);
        });
        data.storage_classes.forEach(sc => {
            if (hiddenSCs.has(sc.name)) return;
            const option = document.createElement('option');
            option.value = sc.name;
            if (sc.is_local) {
                const gpuInfo = gpusPerNode ? ` × ${gpusPerNode} GPUs` : '';
                if (sc.gpu_nodes_covered >= gpuNodeCount && gpuNodeCount > 0) {
                    option.textContent = `${sc.name} — Local Disk (${sc.gpu_nodes_covered} nodes${gpuInfo})`;
                } else if (sc.gpu_nodes_covered > 0) {
                    option.textContent = `${sc.name} — Local Disk (${sc.gpu_nodes_covered}/${gpuNodeCount} nodes${gpuInfo})`;
                } else {
                    option.textContent = `${sc.name} — Local Disk (no nodes ready)`;
                    option.disabled = true;
                }
            } else {
                const rwx = sc.access_mode === 'ReadWriteMany' ? 'RWX' : 'RWO';
                option.textContent = `${sc.name} (${sc.provisioner}) [${rwx}]`;
            }
            option.dataset.accessMode = sc.access_mode || 'ReadWriteOnce';
            storageSelect.appendChild(option);
        });
        // Restore selected storage class
        if (config.storage_class) {
            storageSelect.value = config.storage_class;
        }
    } else if (storageSelect && (!data.storage_classes || data.storage_classes.length === 0)) {
        if (!window._clusterScanInProgress) {
            logToConsole('🔍 Storage classes missing from saved config, re-scanning...', 'info');
            window._clusterScanInProgress = true;
            socket.emit('scan_cluster', {});
        }
    }

    // Check if running from launcher with presets
    var hasPresets = data.preset_max_gpus || (data.preset_nodes && data.preset_nodes.length > 0);

    if (hasPresets) {
        applyGpuPresets(data);
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
            if (nodeEnabled) {
                var nsToggle = document.getElementById('node-select-toggle');
                if (nsToggle) nsToggle.classList.add('active');
                var nsBody = document.getElementById('node-select-body');
                if (nsBody) nsBody.style.display = 'block';
            }
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
    // Skip validation when presets are active (launcher already validated)
    if (config.cluster_resources && (config.cluster_resources.preset_max_gpus || config.cluster_resources.preset_nodes)) {
        warningEl.style.display = 'none';
        return;
    }
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
        'balanced': 'Full Coverage'
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
    document.getElementById('config-summary-pvc').textContent = config.existing_pvc_name || 'Auto-generated';
    document.getElementById('config-summary-namespace').textContent = config.namespace || 'Not set';
    const lcEl = document.getElementById('config-summary-latency-constraint');
    if (lcEl) {
        lcEl.textContent = config.latency_constraint_enabled
            ? `${config.latency_constraint_ms}ms @ ${config.latency_constraint_percentile.toUpperCase()}`
            : 'Disabled';
    }
    const imgEl = document.getElementById('config-summary-image');
    if (imgEl) imgEl.textContent = config.image || document.getElementById('image-repo-input')?.value || '-';
    const schImgEl = document.getElementById('config-summary-scheduler-image');
    if (schImgEl) schImgEl.textContent = config.scheduler_image || document.getElementById('scheduler-image-input')?.value || '-';
    const atEl = document.getElementById('config-summary-autotune');
    if (atEl) atEl.textContent = config.advanced_vllm_custom_enabled ? 'Enabled' : 'Upstream Default';
    const eppEl = document.getElementById('config-summary-epp');
    if (eppEl) {
        const presetNames = { balanced: 'Balanced (3:2:2)', cache_optimized: 'Cache Optimized (5:1:2)', queue_balanced: 'Queue Balanced (2:2:3)', custom: 'Custom' };
        eppEl.textContent = config.epp_custom_enabled ? (presetNames[config.epp_preset] || config.epp_preset) : 'Upstream Default';
    }
    const eppSmartEl = document.getElementById('config-summary-epp-smart');
    if (eppSmartEl) eppSmartEl.textContent = config.epp_benchmark ? 'Enabled' : 'Upstream Default';
    const tdEl = document.getElementById('config-summary-tp-depth');
    if (tdEl) {
        const labels = {1: '1 (Fast)', 2: '2 (Default)', 3: '3 (Deep)', 4: '4 (Full)'};
        tdEl.textContent = labels[config.tp_pair_top_n] || config.tp_pair_top_n;
    }
    const atpEl = document.getElementById('config-summary-asymmetric-tp');
    if (atpEl) atpEl.textContent = config.allow_asymmetric_tp ? 'Enabled' : 'Disabled';
    const clEl = document.getElementById('config-summary-calibrated-load');
    if (clEl) clEl.textContent = config.calibrated_load_enabled ? (config.inferencex_sweep_enabled ? 'Enabled + InferenceX Sweep' : 'Enabled') : 'Disabled';
    const ixEl = document.getElementById('config-summary-inferencex-sweep');
    if (ixEl) ixEl.textContent = config.inferencex_sweep_enabled ? 'Enabled' : 'Disabled';
    const csEl = document.getElementById('config-summary-cache-sweep');
    if (csEl) {
        var csParts = [];
        if (config.cache_sweep_enabled) csParts.push('User Concurrency');
        if (config.cache_sweep_use_calibrated) csParts.push('Calibrated');
        csEl.textContent = csParts.length ? csParts.join(' + ') + ' (' + (config.cache_sweep_mode || 'identical') + ')' : 'Disabled';
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
restoreConsole();

// Goal selection
document.querySelectorAll('[data-goal]').forEach(card => {
    card.addEventListener('click', function() {
        document.querySelectorAll('[data-goal]').forEach(c => c.classList.remove('selected'));
        this.classList.add('selected');
        config.goal = this.dataset.goal;
        updateSingleTestVisibility();
        saveConfig();
    });
});

// Apply GPU/node presets from cluster scan data (works with live scan or cached config.cluster_resources)
function applyGpuPresets(data) {
    if (!data) return;
    var hasPresets = data.preset_max_gpus || (data.preset_nodes && data.preset_nodes.length > 0);
    if (!hasPresets) return;

    var maxGpuSelect = document.getElementById('max-gpu-select');
    if (maxGpuSelect) {
        maxGpuSelect.innerHTML = '<option value="' + (data.preset_max_gpus || data.total_gpus) + '" selected>' + (data.preset_max_gpus || data.total_gpus) + ' GPUs</option>';
        maxGpuSelect.style.display = 'none';
        config.max_gpus = data.preset_max_gpus || data.total_gpus;
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
        config.selected_nodes = data.preset_nodes;
    }
    presetHtml += '</div>';

    var gpuGroup = document.getElementById('max-gpu-group');
    if (gpuGroup) {
        gpuGroup.style.display = 'block';
        var existingPreset = document.getElementById('launcher-preset-card');
        if (existingPreset) existingPreset.remove();
        var presetDiv = document.createElement('div');
        presetDiv.id = 'launcher-preset-card';
        presetDiv.innerHTML = presetHtml;
        gpuGroup.appendChild(presetDiv);
        var gpuLabels = gpuGroup.querySelectorAll('h3, p, .info-label');
        gpuLabels.forEach(function(el) { el.style.display = 'none'; });
    }
}

// Single Test: show/hide deployment config and update GPU summary
var _singleTestVisLock = false;
function updateSingleTestVisibility() {
    if (_singleTestVisLock) return;
    _singleTestVisLock = true;
    var isSingle = config.goal === 'single_test';
    var el = document.getElementById('single-test-config');
    if (el) {
        el.style.display = isSingle ? 'block' : 'none';
    }
    ['sweep-tp-section', 'sweep-pd-section', 'sweep-epp-section', 'sweep-latency-section'].forEach(function(id) {
        var s = document.getElementById(id);
        if (s) s.style.display = isSingle ? 'none' : '';
    });
    var mcRow = document.getElementById('multi-config-sweep-toggle');
    if (mcRow) {
        mcRow.style.opacity = isSingle ? '0.4' : '1';
        mcRow.style.pointerEvents = isSingle ? 'none' : 'auto';
        if (isSingle) {
            config.concurrency_sweep_all_configs = false;
            var cb = document.getElementById('sweep-all-configs');
            if (cb) cb.checked = false;
            var sw = document.getElementById('sweep-all-switch');
            if (sw) { sw.style.background = '#ccc'; sw.querySelector('span').style.transform = 'translateX(0)'; }
            var mr = document.getElementById('sweep-max-configs-row');
            if (mr) mr.style.display = 'none';
        }
    }
    _singleTestVisLock = false;
}

var _pendingSingleTestId = null;

function showSingleTestModal(recId) {
    if (isOptimizationRunning()) {
        document.getElementById('running-modal').classList.add('active');
        return;
    }
    var rc = (window._recConfigs || {})[recId];
    if (!rc) return;

    var arch = rc.architecture || 'aggregated';
    var tp = rc.tp || 1;
    var prefillTp = rc.prefill_tp || tp;
    var decodeTp = rc.decode_tp || tp;
    var prefillPods = rc.prefill_pods || 0;
    var decodePods = rc.decode_pods || 0;
    var replicas = rc.replicas || (rc.gpus ? Math.floor(rc.gpus / tp) : 1);
    var totalGpus = arch === 'pd' ? (prefillTp * prefillPods) + (decodeTp * decodePods) : tp * replicas;

    var maxGpus = config.max_gpus || (config.cluster_resources ? config.cluster_resources.total_gpus : null);
    var presetMax = config.cluster_resources ? config.cluster_resources.preset_max_gpus : null;
    var gpuLimit = presetMax || maxGpus;

    var warningEl = document.getElementById('single-test-modal-gpu-warning');
    if (gpuLimit && totalGpus > gpuLimit) {
        document.getElementById('single-test-modal-gpu-msg').textContent =
            'This configuration requires ' + totalGpus + ' GPUs but your instance is limited to ' + gpuLimit + ' GPUs.' +
            (presetMax ? ' (Set by launcher admin)' : '') +
            ' You can adjust the configuration in the wizard.';
        warningEl.style.display = 'block';
    } else {
        warningEl.style.display = 'none';
    }

    var ts = rc.test_settings || {};
    var configHtml = '<strong>Architecture:</strong> ' + arch.toUpperCase() + '<br>';
    if (arch === 'pd') {
        configHtml += '<strong>Prefill:</strong> ' + prefillPods + ' pods × TP' + prefillTp + '<br>';
        configHtml += '<strong>Decode:</strong> ' + decodePods + ' pods × TP' + decodeTp + '<br>';
    } else {
        configHtml += '<strong>TP:</strong> ' + tp + ' &nbsp; <strong>Replicas:</strong> ' + replicas + '<br>';
    }
    configHtml += '<strong>Total GPUs:</strong> ' + totalGpus;
    if (ts.isl) configHtml += '<br><strong>Workload:</strong> ISL=' + ts.isl + ' OSL=' + (ts.osl || '?') + ' × ' + (ts.num_users || '?') + ' users';
    if (ts.prefix_cache_hit_pct) configHtml += '<br><strong>Prefix Cache:</strong> ' + ts.prefix_cache_hit_pct + '%';
    if (rc.epp_config) {
        var plugins = (rc.epp_config.plugins || {});
        var parts = [];
        if (plugins.prefix_cache) parts.push('cache:' + (plugins.prefix_cache.weight || '?'));
        if (plugins.kv_cache) parts.push('kv:' + (plugins.kv_cache.weight || '?'));
        if (plugins.queue) parts.push('queue:' + (plugins.queue.weight || '?'));
        if (parts.length) configHtml += '<br><strong>EPP Weights:</strong> ' + parts.join(', ');
    }
    document.getElementById('single-test-modal-config').innerHTML = configHtml;

    _pendingSingleTestId = recId;
    document.getElementById('single-test-modal').classList.add('active');
}


function confirmSingleTest() {
    document.getElementById('single-test-modal').classList.remove('active');
    if (!_pendingSingleTestId) return;
    var rc = (window._recConfigs || {})[_pendingSingleTestId];
    _pendingSingleTestId = null;
    if (!rc) return;

    var arch = rc.architecture || 'aggregated';
    var ts = rc.test_settings || {};

    config.goal = 'single_test';
    config.single_test_architecture = arch;

    // Restore workload settings from the original test
    if (ts.isl != null) config.isl = ts.isl;
    if (ts.osl != null) config.osl = ts.osl;
    if (ts.isl_stdev != null) config.isl_stdev = ts.isl_stdev;
    if (ts.osl_stdev != null) config.osl_stdev = ts.osl_stdev;
    if (ts.num_users != null) config.users = ts.num_users;
    if (ts.turns != null) config.turns = ts.turns;
    if (ts.rate_type) config.rate_type = ts.rate_type;
    if (ts.test_duration != null) config.duration = ts.test_duration;
    if (ts.stop_mode) config.stop_mode = ts.stop_mode;
    if (ts.max_requests != null) config.max_requests = ts.max_requests;
    var isInternalDataset = ts.dataset_source && ts.dataset_source.indexOf('prefix-cache-datasets') !== -1;
    if (isInternalDataset) {
        config.workload_mode = 'synthetic';
        config.dataset_source = null;
        config.dataset_column = null;
    } else {
        if (ts.workload_mode) config.workload_mode = ts.workload_mode;
        if (ts.dataset_source) config.dataset_source = ts.dataset_source;
        if (ts.dataset_column) config.dataset_column = ts.dataset_column;
        if (ts.dataset_max_output != null) config.dataset_max_output = ts.dataset_max_output;
    }
    if (ts.prefix_cache_hit_pct != null) config.prefix_cache_hit_pct = ts.prefix_cache_hit_pct;
    if (ts.prefix_cache_mode) config.prefix_cache_mode = ts.prefix_cache_mode;
    if (ts.prefix_cache_groups) config.prefix_cache_groups = ts.prefix_cache_groups;
    if (ts.advanced_vllm) config.advanced_vllm = ts.advanced_vllm;
    if (rc.epp_config) config.epp_config = rc.epp_config;
    if (ts.tp_pair_top_n != null) config.tp_pair_top_n = ts.tp_pair_top_n;
    if (ts.pd_search_mode) config.pd_search_mode = ts.pd_search_mode;
    if (ts.use_achievable_qps != null) config.use_achievable_qps = ts.use_achievable_qps;
    if (ts.latency_constraint_enabled != null) config.latency_constraint_enabled = ts.latency_constraint_enabled;
    if (ts.latency_constraint_ms != null) config.latency_constraint_ms = ts.latency_constraint_ms;
    if (ts.latency_constraint_percentile) config.latency_constraint_percentile = ts.latency_constraint_percentile;
    if (ts.epp_preset) config.epp_preset = ts.epp_preset;
    if (ts.epp_benchmark != null) config.epp_benchmark = ts.epp_benchmark;

    // Set deployment config
    document.querySelectorAll('[data-goal]').forEach(function(c) { c.classList.remove('selected'); });
    var stCard = document.querySelector('[data-goal="single_test"]');
    if (stCard) stCard.classList.add('selected');

    var tp = rc.tp || 1;
    if (arch === 'pd') {
        var ptpEl = document.getElementById('single-test-prefill-tp');
        var dtpEl = document.getElementById('single-test-decode-tp');
        var ppEl = document.getElementById('single-test-prefill-pods');
        var dpEl = document.getElementById('single-test-decode-pods');
        if (ptpEl) ptpEl.value = rc.prefill_tp || tp;
        if (dtpEl) dtpEl.value = rc.decode_tp || tp;
        if (ppEl) ppEl.value = rc.prefill_pods || 1;
        if (dpEl) dpEl.value = rc.decode_pods || 1;
    } else {
        var tpEl = document.getElementById('single-test-tp');
        var repEl = document.getElementById('single-test-replicas');
        if (tpEl) tpEl.value = tp;
        if (repEl) repEl.value = rc.replicas || (rc.gpus ? Math.floor(rc.gpus / tp) : 1);
    }

    selectSingleTestArch(arch);
    updateSingleTestVisibility();

    saveConfig();

    // Close the report overlay then navigate to workload step
    var chartsOverlay = document.getElementById('charts-overlay');
    if (chartsOverlay) chartsOverlay.classList.remove('active');

    setTimeout(function() {
        goToStep(3);
        updateUIFromConfig();
    }, 150);
}

function applyReportConfig(recId) {
    if (isOptimizationRunning()) {
        document.getElementById('running-modal').classList.add('active');
        return;
    }
    var rc = (window._recConfigs || {})[recId];
    if (!rc) { console.warn('applyReportConfig: no config for', recId, 'available:', Object.keys(window._recConfigs || {})); return; }
    console.log('applyReportConfig:', recId, rc);

    var arch = rc.architecture || 'aggregated';
    var ts = rc.test_settings || {};
    var tp = rc.tp || 1;

    // Pre-fill deployment config as starting point (keeps current goal)
    config.single_test_architecture = arch;
    if (arch === 'pd' || arch === 'ep') {
        config.single_test_prefill_tp = rc.prefill_tp || tp;
        config.single_test_decode_tp = rc.decode_tp || tp;
        config.single_test_prefill_pods = rc.prefill_pods || 1;
        config.single_test_decode_pods = rc.decode_pods || 1;
    } else {
        config.single_test_tp = tp;
        config.single_test_replicas = rc.replicas || (rc.gpus ? Math.floor(rc.gpus / tp) : 1);
    }

    // Restore model and image (prefer per-test image over run-level)
    if (rc.model) config.model = rc.model;
    config.image = ts.image || rc.image || null;

    // Restore ALL settings — reset to test values or defaults
    // Step 3: Workload
    config.isl = ts.isl || 2048;
    config.osl = ts.osl || 512;
    config.isl_stdev = ts.isl_stdev || null;
    config.osl_stdev = ts.osl_stdev || null;
    config.users = ts.num_users || 100;
    config.rate_type = ts.rate_type || 'concurrent';
    config.turns = ts.turns || 1;
    config.prefix_cache_hit_pct = ts.prefix_cache_hit_pct || 0;
    config.prefix_cache_mode = ts.prefix_cache_mode || 'identical';
    config.prefix_cache_groups = ts.prefix_cache_groups || 5;
    config.workload_mode = ts.workload_mode || 'synthetic';
    config.dataset_source = ts.dataset_source || null;
    config.dataset_column = ts.dataset_column || null;
    config.dataset_max_output = ts.dataset_max_output || 256;

    // Step 4: Test config
    config.run_description = '';
    config.duration = ts.test_duration || 300;
    config.stop_mode = ts.stop_mode || 'duration';
    config.max_requests = ts.max_requests || null;
    config.latency_constraint_enabled = !!ts.latency_constraint_enabled;
    config.latency_constraint_ms = ts.latency_constraint_ms || 500;
    config.latency_constraint_percentile = ts.latency_constraint_percentile || 'p90';
    config.tp_pair_top_n = ts.tp_pair_top_n || 4;
    config.pd_search_mode = ts.pd_search_mode || 'smart';
    config.use_achievable_qps = !!ts.use_achievable_qps;
    config.allow_asymmetric_tp = !!ts.allow_asymmetric_tp;
    config.cache_sweep_enabled = !!ts.cache_sweep_enabled;
    config.cache_sweep_use_calibrated = !!ts.cache_sweep_use_calibrated;
    config.cache_sweep_mode = ts.cache_sweep_mode || 'identical';
    config.cache_sweep_levels = ts.cache_sweep_levels || [0, 10, 30, 50, 70, 100];
    config.cache_sweep_groups = ts.cache_sweep_groups || 5;
    config.advanced_vllm = ts.advanced_vllm || null;
    config.advanced_vllm_custom_enabled = ts.advanced_vllm_custom_enabled != null ? !!ts.advanced_vllm_custom_enabled : (ts.advanced_vllm ? true : false);

    // Step 5: EPP
    config.epp_config = rc.epp_config || null;
    config.epp_preset = ts.epp_preset || 'balanced';
    config.epp_custom_enabled = ts.epp_custom_enabled != null ? !!ts.epp_custom_enabled : (rc.epp_config && rc.epp_config.preset !== 'default' ? true : false);
    config.epp_benchmark = ts.epp_benchmark != null ? !!ts.epp_benchmark : false;

    // Step 6: Infrastructure
    config.scheduler_image = null;
    config.selected_nodes = [];

    saveConfig();

    var chartsOverlay = document.getElementById('charts-overlay');
    if (chartsOverlay) chartsOverlay.classList.remove('active');

    // Save the values before selectSingleTestArch clobbers them via syncSingleTestToConfig
    var _prefillTp = config.single_test_prefill_tp;
    var _decodeTp = config.single_test_decode_tp;
    var _prefillPods = config.single_test_prefill_pods;
    var _decodePods = config.single_test_decode_pods;
    var _aggTp = config.single_test_tp;
    var _aggReplicas = config.single_test_replicas;

    setTimeout(function() {
        goToStep(1);
        updateUIFromConfig();
        selectSingleTestArch(arch);
        // Populate the deployment fields with saved values
        if (arch === 'pd' || arch === 'ep') {
            var el;
            el = document.getElementById('single-test-prefill-tp');
            if (el) el.value = _prefillTp || 1;
            el = document.getElementById('single-test-decode-tp');
            if (el) el.value = _decodeTp || 1;
            el = document.getElementById('single-test-prefill-pods');
            if (el) el.value = _prefillPods || 1;
            el = document.getElementById('single-test-decode-pods');
            if (el) el.value = _decodePods || 1;
            config.single_test_prefill_tp = _prefillTp;
            config.single_test_decode_tp = _decodeTp;
            config.single_test_prefill_pods = _prefillPods;
            config.single_test_decode_pods = _decodePods;
        } else {
            var el;
            el = document.getElementById('single-test-tp');
            if (el) el.value = _aggTp || 1;
            el = document.getElementById('single-test-replicas');
            if (el) el.value = _aggReplicas || 1;
            config.single_test_tp = _aggTp;
            config.single_test_replicas = _aggReplicas;
        }
        updateSingleTestGpuSummary();
        saveConfig();
    }, 150);
}

function selectSingleTestArch(arch) {
    document.querySelectorAll('#single-test-arch-cards > div').forEach(function(c) {
        c.classList.remove('selected');
        c.style.borderColor = '#e2e8f0';
        c.style.background = '';
    });
    var card = document.querySelector('#single-test-arch-cards [data-arch="' + arch + '"]');
    if (card) {
        card.classList.add('selected');
        card.style.borderColor = '#8b5cf6';
        card.style.background = '#f5f3ff';
    }
    config.single_test_architecture = arch;
    var aggFields = document.getElementById('single-test-agg-fields');
    var pdFields = document.getElementById('single-test-pd-fields');
    if (aggFields) aggFields.style.display = arch !== 'pd' ? 'block' : 'none';
    if (pdFields) pdFields.style.display = arch === 'pd' ? 'block' : 'none';
    syncSingleTestToConfig();
    updateSingleTestGpuSummary();
    saveConfig();
}

function syncSingleTestToConfig() {
    var arch = config.single_test_architecture || 'aggregated';
    if (arch === 'pd') {
        var el;
        el = document.getElementById('single-test-prefill-tp');
        if (el) config.single_test_prefill_tp = parseInt(el.value) || 1;
        el = document.getElementById('single-test-decode-tp');
        if (el) config.single_test_decode_tp = parseInt(el.value) || 1;
        el = document.getElementById('single-test-prefill-pods');
        if (el) config.single_test_prefill_pods = parseInt(el.value) || 1;
        el = document.getElementById('single-test-decode-pods');
        if (el) config.single_test_decode_pods = parseInt(el.value) || 1;
        config.single_test_tp = null;
        config.single_test_replicas = null;
    } else {
        var el;
        el = document.getElementById('single-test-tp');
        if (el) config.single_test_tp = parseInt(el.value) || 1;
        el = document.getElementById('single-test-replicas');
        if (el) config.single_test_replicas = parseInt(el.value) || 1;
        config.single_test_prefill_tp = null;
        config.single_test_decode_tp = null;
        config.single_test_prefill_pods = null;
        config.single_test_decode_pods = null;
    }
}

function updateSingleTestGpuSummary() {
    syncSingleTestToConfig();
    var arch = config.single_test_architecture || 'aggregated';
    if (arch === 'pd') {
        var ptpEl = document.getElementById('single-test-prefill-tp');
        var ppEl = document.getElementById('single-test-prefill-pods');
        var dtpEl = document.getElementById('single-test-decode-tp');
        var dpEl = document.getElementById('single-test-decode-pods');
        if (!ptpEl || !ppEl || !dtpEl || !dpEl) return;
        var ptp = parseInt(ptpEl.value) || 4;
        var pp = parseInt(ppEl.value) || 1;
        var dtp = parseInt(dtpEl.value) || 8;
        var dp = parseInt(dpEl.value) || 1;
        var total = (ptp * pp) + (dtp * dp);
        var el = document.getElementById('single-test-pd-gpu-summary');
        if (el) el.textContent = 'Total GPUs: ' + total + ' (' + pp + ' prefill × TP' + ptp + ' + ' + dp + ' decode × TP' + dtp + ')';
    } else {
        var tpEl = document.getElementById('single-test-tp');
        var repEl = document.getElementById('single-test-replicas');
        if (!tpEl || !repEl) return;
        var tp = parseInt(tpEl.value) || 4;
        var reps = parseInt(repEl.value) || 1;
        var total = tp * reps;
        var el = document.getElementById('single-test-gpu-summary');
        if (el) el.textContent = 'Total GPUs: ' + total + ' (' + reps + ' pods × TP' + tp + ')';
    }
}

['single-test-tp', 'single-test-replicas'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('change', updateSingleTestGpuSummary);
});
['single-test-prefill-tp', 'single-test-prefill-pods', 'single-test-decode-tp', 'single-test-decode-pods'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('change', updateSingleTestGpuSummary);
});

// Image tag fetching
function fetchImageTags() {
    var full = (document.getElementById('image-repo-input').value || 'ghcr.io/llm-d/llm-d-cuda:v0.8.0').trim();
    var repo = full.split(':')[0];
    var statusEl = document.getElementById('image-tag-status');
    statusEl.textContent = 'Fetching tags...';
    statusEl.style.color = '#0369a1';

    socket.emit('fetch_image_tags', { repo: repo });
}

socket.on('image_tags_result', function(data) {
    var selectEl = document.getElementById('image-tag-select');
    var statusEl = document.getElementById('image-tag-status');

    if (data.error) {
        statusEl.textContent = 'Failed to fetch tags: ' + data.error;
        statusEl.style.color = '#dc2626';
        return;
    }

    var tags = data.tags || [];
    statusEl.textContent = tags.length + ' tags found';
    statusEl.style.color = '#22c55e';

    var currentTag = config.image ? config.image.split(':').pop() : 'v0.6.0';
    selectEl.innerHTML = '';
    tags.forEach(function(tag) {
        var opt = document.createElement('option');
        opt.value = tag;
        opt.textContent = tag;
        if (tag === currentTag) opt.selected = true;
        selectEl.appendChild(opt);
    });
    selectEl.style.display = 'block';

    updateSelectedImage();
});

function updateSelectedImage() {
    var input = document.getElementById('image-repo-input');
    var repo = input.value.split(':')[0] || 'ghcr.io/llm-d/llm-d-cuda';
    var selectEl = document.getElementById('image-tag-select');
    if (selectEl && selectEl.value) {
        input.value = repo + ':' + selectEl.value;
    }
    config.image = input.value;
    saveConfig();
}

var RHAIIS_VERSIONS = {
    '3.3.4': { cuda: 'v0.4.0', scheduler: 'v0.4.0', vllm: '0.13.0' },
    '3.4.0': { cuda: 'v0.6.0', scheduler: 'v0.7.1', vllm: '0.18.0' },
    '3.4.1': { cuda: 'v0.6.0', scheduler: 'v0.7.1', vllm: '0.18.0' },
    '3.5.0': { cuda: 'v0.8.0', scheduler: 'v0.8.0', vllm: '0.24.0' },
    '3.5.1': { cuda: 'v0.8.1', scheduler: 'v0.9.0', vllm: '0.24.0' },
};

function applyRhaiisVersion(version) {
    var preset = RHAIIS_VERSIONS[version];
    if (!preset) return; // 'custom' selected — do nothing

    // Set cuda image (full path in single input)
    var cudaInput = document.getElementById('image-repo-input');
    var cudaRepo = cudaInput.value.split(':')[0] || 'ghcr.io/llm-d/llm-d-cuda';
    cudaInput.value = cudaRepo + ':' + preset.cuda;
    config.image = cudaInput.value;

    // Update tag dropdown if populated
    var tagSelect = document.getElementById('image-tag-select');
    if (tagSelect && tagSelect.options.length > 0) {
        for (var i = 0; i < tagSelect.options.length; i++) {
            if (tagSelect.options[i].value === preset.cuda) {
                tagSelect.selectedIndex = i;
                break;
            }
        }
    }

    // Set scheduler image
    var schedInput = document.getElementById('scheduler-image-input');
    var schedRepo = schedInput.value.split(':')[0] || 'ghcr.io/llm-d/llm-d-router-endpoint-picker';
    schedInput.value = schedRepo + ':' + preset.scheduler;
    config.scheduler_image = schedInput.value;

    // Update scheduler tag dropdown if populated
    var schedSelect = document.getElementById('scheduler-tag-select');
    if (schedSelect && schedSelect.options.length > 0) {
        for (var j = 0; j < schedSelect.options.length; j++) {
            if (schedSelect.options[j].value === preset.scheduler) {
                schedSelect.selectedIndex = j;
                break;
            }
        }
    }

    config.rhaiis_version = version;
    saveConfig();
}

function markImagesCustom() {
    var sel = document.getElementById('rhaiis-version-select');
    if (!sel) return;
    var currentCuda = (document.getElementById('image-repo-input').value || '').split(':').pop();
    var currentSched = (document.getElementById('scheduler-image-input').value || '').split(':').pop();

    // Check if current images match any preset
    var matched = false;
    for (var ver in RHAIIS_VERSIONS) {
        var p = RHAIIS_VERSIONS[ver];
        if (p.cuda === currentCuda && p.scheduler === currentSched) {
            sel.value = ver;
            matched = true;
            break;
        }
    }
    if (!matched) sel.value = 'custom';
    config.rhaiis_version = sel.value;
    saveConfig();
}

function fetchSchedulerTags() {
    var input = document.getElementById('scheduler-image-input');
    var full = (input.value || 'ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0').trim();
    var repo = full.split(':')[0];
    var statusEl = document.getElementById('scheduler-tag-status');
    statusEl.textContent = 'Fetching tags...';
    statusEl.style.color = '#0369a1';
    socket.emit('fetch_image_tags', { repo: repo, target: 'scheduler' });
}

socket.on('scheduler_tags_result', function(data) {
    var selectEl = document.getElementById('scheduler-tag-select');
    var statusEl = document.getElementById('scheduler-tag-status');
    if (data.error) {
        statusEl.textContent = 'Failed: ' + data.error;
        statusEl.style.color = '#dc2626';
        return;
    }
    var tags = data.tags || [];
    statusEl.textContent = tags.length + ' tags found';
    statusEl.style.color = '#22c55e';
    var currentTag = config.scheduler_image ? config.scheduler_image.split(':').pop() : 'v0.9.0';
    selectEl.innerHTML = '';
    tags.forEach(function(tag) {
        var opt = document.createElement('option');
        opt.value = tag;
        opt.textContent = tag;
        if (tag === currentTag) opt.selected = true;
        selectEl.appendChild(opt);
    });
    selectEl.style.display = 'block';
    updateSelectedScheduler();
});

function updateSelectedScheduler() {
    var input = document.getElementById('scheduler-image-input');
    var repo = input.value.split(':')[0] || 'ghcr.io/llm-d/llm-d-router-endpoint-picker';
    var selectEl = document.getElementById('scheduler-tag-select');
    if (selectEl && selectEl.value) {
        input.value = repo + ':' + selectEl.value;
    }
    config.scheduler_image = input.value;
    saveConfig();
}

// Load models from API
var allModels = [];
var displayedModels = 0;
var modelsPerPage = 16;
