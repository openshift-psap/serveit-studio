// settings.js — Advanced vLLM settings, TP pairs, PD search, EPP config, prefix cache

// Max requests input
var maxReqInput = document.getElementById('max-requests-input');
if (maxReqInput) {
    maxReqInput.addEventListener('change', (e) => {
        config.max_requests = parseInt(e.target.value);
        saveConfig();
    });
}

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

// Auto-Scale Concurrency checkbox
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
var advValueFields = ['max-model-len','gpu-memory-utilization','max-num-seqs','max-num-batched-tokens','dtype','kv-cache-dtype','pipeline-parallel-size','block-size','tool-call-parser','reasoning-parser','chat-template-content-format'];
var advToggleFields = ['enable-prefix-caching','disable-custom-all-reduce','enable-auto-tool-choice','enable-expert-parallel','trust-remote-code','disable-log-requests','vllm-debug-logs','nccl-debug-logs'];

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
    // Include raw text mode info
    adv._mode = config.advanced_vllm_mode || 'form';
    adv._raw_text = config.advanced_vllm_raw || '';
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
    // Restore raw text mode
    var advMode = config.advanced_vllm_mode || (adv && adv._mode) || 'form';
    var advRaw = config.advanced_vllm_raw || (adv && adv._raw_text) || '';
    if (advMode === 'raw') {
        setAdvVllmMode('raw');
        var textarea = document.getElementById('adv-vllm-raw-text');
        if (textarea && advRaw) textarea.value = advRaw;
    }
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

function setAdvVllmMode(mode) {
    var formBtn = document.getElementById('adv-mode-form-btn');
    var rawBtn = document.getElementById('adv-mode-raw-btn');
    var formSection = document.getElementById('adv-vllm-form-section');
    var rawSection = document.getElementById('adv-vllm-raw-section');
    if (mode === 'raw') {
        formBtn.style.borderColor = '#cbd5e1'; formBtn.style.background = '#fafafa';
        rawBtn.style.borderColor = 'var(--rh-red-primary)'; rawBtn.style.background = 'linear-gradient(135deg,#fef2f2,#fee2e2)';
        if (formSection) formSection.style.display = 'none';
        if (rawSection) rawSection.style.display = 'block';
        populateRawFromForm();
    } else {
        rawBtn.style.borderColor = '#cbd5e1'; rawBtn.style.background = '#fafafa';
        formBtn.style.borderColor = 'var(--rh-red-primary)'; formBtn.style.background = 'linear-gradient(135deg,#fef2f2,#fee2e2)';
        if (formSection) formSection.style.display = 'block';
        if (rawSection) rawSection.style.display = 'none';
    }
    config.advanced_vllm_mode = mode;
    saveConfig();
}

function populateRawFromForm() {
    var lines = [];
    var isl = config.isl || 2048;
    var osl = config.osl || 512;

    // Auto-computed values (what the optimizer would set)
    var autoMaxModelLen = Math.ceil((isl + osl) * 1.05);
    var autoBlockSize = Math.pow(2, Math.ceil(Math.log2(Math.max(1, Math.sqrt(isl + osl)))));
    autoBlockSize = Math.max(8, Math.min(512, autoBlockSize));

    var valueDefaults = {
        'max-model-len': autoMaxModelLen,
        'block-size': autoBlockSize,
    };

    var toggleDefaults = {
        'trust-remote-code': true, 'disable-log-requests': true, 'enable-prefix-caching': true,
        'disable-custom-all-reduce': false, 'enable-auto-tool-choice': false,
        'enable-expert-parallel': false, 'vllm-debug-logs': false, 'nccl-debug-logs': false
    };

    // Value settings: use custom if set, otherwise auto-computed defaults
    advValueFields.forEach(function(f) {
        var modeEl = document.getElementById('adv-' + f + '-mode');
        var valEl = document.getElementById('adv-' + f + '-val');
        if (!modeEl || !valEl) return;
        if (modeEl.value === 'custom' && valEl.value) {
            lines.push('--' + f + ' ' + valEl.value);
        } else if (valueDefaults[f] != null) {
            lines.push('--' + f + ' ' + valueDefaults[f]);
        }
    });

    // Toggle flags
    advToggleFields.forEach(function(f) {
        var modeEl = document.getElementById('adv-' + f + '-mode');
        if (!modeEl) return;
        var mode = modeEl.value;
        var on = mode === 'auto' ? (toggleDefaults[f] || false) : mode === 'on';
        if (on) lines.push('--' + f);
    });

    var textarea = document.getElementById('adv-vllm-raw-text');
    if (textarea) {
        var header = '# vLLM serve flags — edit freely, one flag per line\n';
        header += '# Model name, --port, --tensor-parallel-size, and\n';
        header += '# --gpu-memory-utilization are set by the optimizer\n\n';
        textarea.value = header + lines.join('\n');
    }
}

function updateAdvVllmRaw() {
    var textarea = document.getElementById('adv-vllm-raw-text');
    if (!textarea) return;
    config.advanced_vllm_raw = textarea.value;
    if (!config.advanced_vllm) config.advanced_vllm = {};
    config.advanced_vllm._mode = 'raw';
    config.advanced_vllm._raw_text = textarea.value;
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

function setPrefixCacheMode(mode) {
    config.prefix_cache_mode = mode;
    var btnMap = {identical: 'pcm-identical', shared_prefix: 'pcm-shared', multi_group: 'pcm-multi'};
    Object.keys(btnMap).forEach(m => {
        var btn = document.getElementById(btnMap[m]);
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
    var groupsWrap = document.getElementById('prefix-cache-groups-wrap');
    if (groupsWrap) groupsWrap.style.display = mode === 'multi_group' ? 'block' : 'none';
    updatePrefixCacheModeDesc();
    saveConfig();
}

function updatePrefixCacheModeDesc() {
    var desc = document.getElementById('prefix-cache-mode-desc');
    if (!desc) return;
    var pct = config.prefix_cache_hit_pct || 0;
    var mode = config.prefix_cache_mode || 'identical';
    var unique = 100 - pct;
    if (mode === 'shared_prefix') {
        desc.innerHTML = '<strong style="color:#0369a1;">Shared Prefix</strong> &mdash; Every prompt starts with the same <strong>' + pct + '%</strong> of tokens (the shared prefix), followed by <strong>' + unique + '%</strong> unique tokens. All requests get partial cache hits &mdash; the shared portion is served from KV cache, only the unique suffix needs computation.<br><br>' +
            '<span style="color:#64748b;">Simulates: system prompts, few-shot examples, or shared context that is common across all requests.</span>';
    } else if (mode === 'multi_group') {
        var groups = config.prefix_cache_groups || 5;
        desc.innerHTML = '<strong style="color:#0369a1;">Multi-Group</strong> &mdash; <strong>' + groups + ' distinct prompt groups</strong>, each with its own shared prefix. <strong>' + pct + '%</strong> of requests belong to one of the groups (cache hit if routed to the right pod). The remaining <strong>' + unique + '%</strong> are fully unique (cache miss).<br><br>' +
            '<span style="color:#64748b;">Simulates: multi-tenant deployments where different applications or users have different system prompts. Tests EPP routing precision &mdash; the gateway must route each request to the pod that has that specific group\'s prefix cached.</span>';
    } else {
        desc.innerHTML = '<strong style="color:#0369a1;">Identical Prompts</strong> &mdash; <strong>' + pct + '%</strong> of requests use the exact same prompt (full cache hit). The remaining <strong>' + unique + '%</strong> are completely unique prompts (cache miss).<br><br>' +
            '<span style="color:#64748b;">Simulates: popular queries, FAQ-style workloads, or repeated API calls with the same input.</span>';
    }
}

function toggleEppCustomMode(enabled) {
    config.epp_custom_enabled = enabled;
    var section = document.getElementById('epp-custom-section');
    if (section) section.style.display = enabled ? 'block' : 'none';
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
        precise_prefix_cache: { enabled: document.getElementById('epp-plugin-precise-prefix-cache').checked, weight: parseFloat(document.getElementById('epp-weight-precise-prefix-cache').value) },
        active_request: { enabled: document.getElementById('epp-plugin-active-request').checked, weight: parseFloat(document.getElementById('epp-weight-active-request').value) },
        no_hit_lru: { enabled: document.getElementById('epp-plugin-no-hit-lru').checked, weight: parseFloat(document.getElementById('epp-weight-no-hit-lru').value) },
        session_aware: { enabled: document.getElementById('epp-plugin-session-aware').checked, weight: parseFloat(document.getElementById('epp-weight-session-aware').value) },
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

    var cacheHitPct = config.prefix_cache_hit_pct || 0;

    // With high cache hit %, increase maxPrefixBlocksToMatch to check more blocks for hits
    // Base: ceil(ISL / block_size). With cache: multiply by 1.5-2x to catch longer shared prefixes
    var prefixMultiplier = cacheHitPct > 50 ? 2.0 : (cacheHitPct > 0 ? 1.5 : 1.0);
    var autoMaxPrefixBlocks = Math.ceil(isl / blockSize * prefixMultiplier);

    // nonCachedTokens: with high cache hits, lower threshold routes more to decode (prefix is already cached)
    var autoNonCachedTokens = cacheHitPct > 50
        ? Math.min(8, Math.max(1, Math.floor(isl / 200)))
        : Math.min(16, Math.max(1, Math.floor(isl / 100)));

    // LRU capacity from GPU VRAM
    var gpuVramMb = 80 * 1024; // default 80GB
    if (config.cluster_resources && config.cluster_resources.gpu_memory_per_gpu_mb) {
        gpuVramMb = config.cluster_resources.gpu_memory_per_gpu_mb;
    }
    // ~40% of VRAM for KV cache, each block holds block_size tokens at ~0.5KB/token
    // With high cache hit %, increase LRU capacity to track more cached prefixes
    var kvFraction = cacheHitPct > 50 ? 0.5 : 0.4;
    var availableKvMb = gpuVramMb * kvFraction;
    var kvPerBlockKb = blockSize * 0.5;
    var autoLruCapacity = Math.max(1000, Math.floor(availableKvMb * 1024 / kvPerBlockKb));

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
        'balanced': 'Balanced Performance',
        'aggregated_only': 'Aggregated Only',
        'pd_only': 'Prefill/Decode Only',
        'ep_only': 'Expert Parallelism Only',
        'single_test': 'Single Test',
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
    const qpsMode = config.use_achievable_qps ? 'Sustainable Concurrency (auto-scaled)' : 'User-defined Concurrent Users';
    const stopInfo = config.stop_mode === 'max_requests' ? `${config.max_requests} requests` : `${config.duration}s`;
    logToConsole(`\n📋 Step 4 Complete: Test Config = ${config.users} users, ${stopInfo}, Mode: ${qpsMode}`, 'success');
    goToStep(5);
});

// Step 6: EPP Config (next-step6 is in step5_epp.html)
document.getElementById('next-step6').addEventListener('click', () => {
    logToConsole(`\n📋 Step 6 Complete: EPP Config = ${config.epp_preset || 'balanced'}`, 'success');
    goToStep(7);
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
var pvcsFetched = false;

// Fetch available PVCs from cluster
