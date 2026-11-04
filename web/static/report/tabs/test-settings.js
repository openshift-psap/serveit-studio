// tabs/test-settings.js — Test Settings report tab (run configuration).
// One implementation for live + offline reports. Pure tables — no charts.
(function() {
    // Config label tagger (ported from charts.js _tagLabel)
    function _tagLabel(name, arch) {
        if (!name || name.indexOf('(') >= 0) return name || '';
        var a = (arch || '').toUpperCase();
        if (a === 'PD') return name + ' (PD)';
        if (a === 'EP') return name + ' (EP)';
        return name + ' (AG)';
    }

    function build(data, ctx) {
        if (!data.run_config) return '';
        var html = '';
    // ============================================================
const rc = data.run_config;
const na = 'N/A';
const adv = rc.advanced_vllm || {};
const advVal = (key, fallback) => { const s = adv[key]; return s && s.mode === 'custom' && s.value != null ? s.value : (fallback != null ? fallback : 'auto'); };
const advToggle = (key, fallback) => { const s = adv[key]; return s ? (s.mode === 'on' ? 'On' : s.mode === 'off' ? 'Off' : fallback) : fallback; };

html += '<div class="chart-card" style="border-left:6px solid #64748b;">' +
    '<div class="chart-card-header" style="background:linear-gradient(135deg,#64748b,#94a3b8); color:white;">Test Settings</div>' +
    '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
    'Complete configuration used for this optimization run — workload parameters, search strategy, infrastructure details, and component versions. ' +
    'These settings apply to every test; only the architecture, TP values, and pod counts vary between configurations.' +
    '</div></div>';
// Settings tables — always display prompt length in characters
var cpt = rc.chars_per_token || 4.5;
var islChars, oslChars, islStdevChars, oslStdevChars;
if (rc.length_unit === 'characters' && rc.isl_original_chars) {
    islChars = rc.isl_original_chars;
    oslChars = rc.osl_original_chars;
    islStdevChars = rc.isl_stdev_original_chars;
    oslStdevChars = rc.osl_stdev_original_chars;
} else {
    islChars = Math.round(rc.isl * cpt);
    oslChars = Math.round(rc.osl * cpt);
    islStdevChars = rc.isl_stdev ? Math.round(rc.isl_stdev * cpt) : null;
    oslStdevChars = rc.osl_stdev ? Math.round(rc.osl_stdev * cpt) : null;
}
var islDisplay = rc.isl.toLocaleString() + (rc.isl_stdev ? ' + &sigma;=' + rc.isl_stdev.toLocaleString() : '') + ' tokens <span style="color:#94a3b8;">(' + islChars.toLocaleString() + ' chars)</span>';
var oslDisplay = rc.osl.toLocaleString() + (rc.osl_stdev ? ' + &sigma;=' + rc.osl_stdev.toLocaleString() : '') + ' tokens <span style="color:#94a3b8;">(' + oslChars.toLocaleString() + ' chars)</span>';

const rv = rc._resolved || {};
const eppPresetLabels = {balanced:'Balanced', cache_optimized:'Cache Optimized', queue_balanced:'Queue Balanced', latency_aware:'Latency Aware', custom:'Custom'};

// Build ISL/OSL range display
var islRange = rc.isl_min || rc.isl_max ? ' [' + (rc.isl_min || '?') + '–' + (rc.isl_max || '?') + ']' : '';
var oslRange = rc.osl_min || rc.osl_max ? ' [' + (rc.osl_min || '?') + '–' + (rc.osl_max || '?') + ']' : '';

// Build unified settings sections
var settingsSections = [
    { title: 'Workload', color: '#059669', rows: [
        ['Model', rc.model_name || na],
        ['Prompt Length (ISL)', islDisplay + islRange],
        ['Output Length (OSL)', oslDisplay + oslRange],
        rc.first_prompt_tokens ? ['First Prompt Tokens', rc.first_prompt_tokens.toLocaleString() + (rc.first_prompt_tokens_stdev ? ' &sigma;=' + rc.first_prompt_tokens_stdev.toLocaleString() : '') + (rc.first_prompt_tokens_min || rc.first_prompt_tokens_max ? ' [' + (rc.first_prompt_tokens_min || '?') + '–' + (rc.first_prompt_tokens_max || '?') + ']' : '')] : null,
        rc.prefix_tokens ? ['Shared Prefix', rc.prefix_tokens.toLocaleString() + ' tokens &times; ' + (rc.prefix_count || 1) + ' prefix(es)'] : null,
        ['Concurrent Users', rc.qps != null ? Math.round(rc.qps) : na],
        ['Rate Type', rc.rate_type || 'concurrent'],
        rc.turns > 1 ? ['Turns per Conversation', rc.turns] : null,
        rc.turns > 1 && rc.turn_delay ? ['Inter-Turn Delay', rc.turn_delay + 's' + (rc.turn_delay_stdev ? ' &sigma;=' + rc.turn_delay_stdev + 's' : '') + (rc.turn_delay_min || rc.turn_delay_max ? ' [' + (rc.turn_delay_min || '?') + '–' + (rc.turn_delay_max || '?') + 's]' : '')] : null,
        ['Test Duration', (rc.test_duration || 300) + 's'],
        ['Stop Mode', rc.stop_mode || 'duration'],
        rc.max_requests ? ['Max Requests', rc.max_requests] : null,
        ['Workload Mode', (function() {
            if (rc.workload_mode === 'dataset')
                return 'Dataset file' + (rc.dataset_source ? ' (' + rc.dataset_source + ')' : '');
            if (rc.use_corpus)
                return 'Real corpus (wikitext-103) — native multi-turn generator';
            return 'Synthetic text';
        })()],
        rc.dataset_source ? ['Dataset', '<span style="word-break:break-all;font-size:0.9em;">' + rc.dataset_source + '</span>'] : null,
        rc.prefix_cache_hit_pct > 0 ? ['Prefix Cache Hit', rc.prefix_cache_hit_pct + '% (' + (rc.prefix_cache_mode || 'identical') + (rc.prefix_cache_groups > 1 ? ', ' + rc.prefix_cache_groups + ' groups' : '') + ')'] : null,
    ]},
    { title: 'Search Strategy', color: '#4f46e5', rows: [
        ['Optimization Goal', (rc.objective || 'ttft').toUpperCase()],
        ['Total GPUs', rc.total_gpus || na],
        ['TP Options', (rc.tp_options || []).join(', ') || na],
        ['TP Pair Breadth', 'Top-' + (rc.tp_pair_top_n || 4)],
        ['P/D Ratio Search', rc.pd_search_mode === 'exhaustive' ? 'Exhaustive' : 'Adaptive'],
        ['Asymmetric TP', rc.allow_asymmetric_tp ? 'Yes' : 'No'],
        ['Skip TP16', rc.skip_tp16 !== false ? 'Yes' : 'No'],
        ['LoRA Serving', rc.lora_enabled ? 'Enabled' : 'Disabled'],
        ['LoRA Adapters', rc.lora_enabled ? (rc.lora_num_adapters || 20) + ' (agent-01…agent-' + String(rc.lora_num_adapters || 20).padStart(2, '0') + ')' : '—'],
        ['LoRA Benchmark Mode', rc.lora_enabled ? ({'round_robin': 'All adapters (round-robin per request)', 'session_pinned': 'Session-pinned (one adapter per conversation)', 'base_only': 'Base model only (no adapter traffic)'}[rc.lora_benchmark_mode] || 'All adapters (round-robin per request)') : '—'],
        ['LoRA Sweep', rc.lora_sweep_enabled ? 'Enabled — levels [' + (rc.lora_sweep_levels || []).join(', ') + ']' : 'Disabled'],
        ['Auto-Scale Concurrency', rc.use_achievable_qps ? 'Yes' : 'No'],
        ['Headroom', (rc.headroom || 1.3) + 'x'],
        ['Latency SLA', rc.latency_constraint_enabled ? rc.latency_constraint_ms + 'ms @ ' + rc.latency_constraint_percentile : 'Disabled'],
        ['Calibrated Load', rc.calibrated_load_enabled ? 'Enabled' : 'Disabled'],
        ['Concurrency Sweep', rc.inferencex_sweep_enabled ? 'Enabled — ' + ({'architecture_aware':'Architecture-Aware','all_recommendations':'All Recommendations','all':'All Configs'}[rc.concurrency_sweep_mode] || 'Architecture-Aware') + (rc.concurrency_sweep_count ? ' (' + rc.concurrency_sweep_count + ' levels)' : '') + (rc.concurrency_sweep_levels ? ' [' + rc.concurrency_sweep_levels.join(', ') + ']' : '') : 'Disabled'],
        ['Cache Sweep', rc.cache_sweep_enabled ? 'Enabled' : 'Disabled'],
    ]},
    { title: 'vLLM Engine', color: '#0891b2', rows: [
        ['Max Model Length', advVal('max_model_len', 'auto')],
        ['Block Size', adv.block_size ? (adv.block_size.mode === 'default' ? 'vLLM Default' : adv.block_size.mode === 'custom' ? adv.block_size.value : 'Auto') : 'Auto'],
        ['KV Cache Dtype', advVal('kv_cache_dtype', 'auto')],
        ['GPU Memory Utilization', advVal('gpu_memory_utilization', 'auto')],
        ['Prefix Caching', advToggle('enable_prefix_caching', 'Auto')],
        ['Expert Parallel', advToggle('enable_expert_parallel', 'Off')],
        ['Auto Tool Choice', advToggle('enable_auto_tool_choice', 'Off')],
        adv.tool_call_parser && adv.tool_call_parser.mode === 'custom' ? ['Tool Call Parser', adv.tool_call_parser.value] : null,
        adv.reasoning_parser && adv.reasoning_parser.mode === 'custom' ? ['Reasoning Parser', adv.reasoning_parser.value] : null,
        ['Bidirectional KV', advToggle('enable_bidirectional_kv', 'Off')],
        ['Skip Asymmetric MLA TP', advToggle('skip_asymmetric_mla_tp', 'Off')],
        adv.cpu_offload_gb && adv.cpu_offload_gb.mode === 'custom' ? ['CPU KV Offload', adv.cpu_offload_gb.value + ' GB'] : null,
        adv.prefix_cache_retention && adv.prefix_cache_retention.mode === 'custom' ? ['Prefix Cache Retention', adv.prefix_cache_retention.value] : null,
        adv.ssm_conv_state_layout && adv.ssm_conv_state_layout.mode === 'custom' ? ['SSM Conv State Layout', adv.ssm_conv_state_layout.value] : null,
        adv.http_timeout_keep_alive && adv.http_timeout_keep_alive.mode === 'custom' ? ['HTTP Keep-Alive Timeout', adv.http_timeout_keep_alive.value + 's'] : null,
        adv.model_loader_extra_config && adv.model_loader_extra_config.mode === 'custom' ? ['Model Loader Config', '<span style="font-size:0.85em;">' + adv.model_loader_extra_config.value + '</span>'] : null,
        advToggle('enable_dbo', null) !== null ? ['Dual Batch Overlap', advToggle('enable_dbo', 'Off')] : null,
        advToggle('enable_eplb', null) !== null ? ['Expert Load Balancing', advToggle('enable_eplb', 'Off')] : null,
        adv.speculative_method ? ['Speculative Method', adv.speculative_method] : null,
        adv.num_speculative_tokens && adv.num_speculative_tokens.mode === 'custom' ? ['Speculative Tokens', adv.num_speculative_tokens.value] : null,
        adv.speculative_model && adv.speculative_model.mode === 'custom' && adv.speculative_model.value ? ['Speculative Model', adv.speculative_model.value] : null,
    ]},
    { title: 'Infrastructure', color: '#d97706', rows: [
        ['Inference Image', '<span style="word-break:break-all;font-size:0.9em;">' + (rc.image || na) + '</span>'],
        ['Scheduler Image', '<span style="word-break:break-all;font-size:0.9em;">' + (rc.scheduler_image || na) + '</span>'],
        ['Namespace', rc.namespace || na],
        ['Storage Class', rc.storage_class || na],
        rc.local_disk_path ? ['Local Disk Path', rc.local_disk_path] : null,
        rc.per_node_storage ? ['Per-Node Storage', 'Yes'] : null,
        ['PVC', rc.pvc_name || na],
        ['Network Type', rc.network_type || na],
        rc.selected_shared_device ? ['Shared Device', rc.selected_shared_device] : null,
        rc.gateway_class ? ['Gateway Class', rc.gateway_class] : null,
        ['NCCL IB HCA', rc.nccl_ib_hca || na],
        rc.rdma_nics_per_node ? ['RDMA NICs/Node', rc.rdma_nics_per_node] : null,
    ]},
    { title: 'EPP Configuration', color: '#6d28d9', rows: (function() {
        var rows = [
            ['Scoring Preset', (rc.epp_custom_enabled !== false) ? (eppPresetLabels[rc.epp_preset] || rc.epp_preset || 'Balanced') : 'llm-d upstream'],
            ['EPP Tuning', rc.epp_benchmark ? 'Enabled' : 'Disabled'],
        ];
        var ec = rc.epp_config || {};
        var plugins = ['prefix_cache', 'kv_cache', 'queue', 'slo', 'active_request', 'token_load', 'no_hit_lru', 'session_aware', 'precise_prefix_cache'];
        var enabledPlugins = plugins.filter(function(p) { return ec[p] && ec[p].enabled; }).map(function(p) { return p.replace(/_/g, '-') + '(' + ec[p].weight + ')'; });
        if (enabledPlugins.length) rows.push(['Scoring Plugins', enabledPlugins.join(', ')]);
        if (ec.prefixCacheAffinityEnabled) rows.push(['Prefix Cache Affinity', 'Enabled']);
        if (ec.peakPrefillThroughput) rows.push(['Peak Prefill Throughput', ec.peakPrefillThroughput]);
        if (ec.lruCapacityPerServer) rows.push(['LRU Capacity/Server', ec.lruCapacityPerServer.toLocaleString()]);
        if (ec.maxPrefixBlocksToMatch) rows.push(['Max Prefix Blocks', ec.maxPrefixBlocksToMatch]);
        if (ec.nonCachedTokens) rows.push(['Non-Cached Tokens', ec.nonCachedTokens]);
        if (ec.prefixCacheAutoTune != null) rows.push(['Prefix Cache Auto-Tune', ec.prefixCacheAutoTune ? 'On' : 'Off']);
        if (ec.usePrefixBasedDecider != null) rows.push(['Prefix-Based Decider', ec.usePrefixBasedDecider ? 'On' : 'Off']);
        return rows;
    })()},
];

// Component Versions
if (data.infra_versions && Object.keys(data.infra_versions).length > 0) {
    var iv = data.infra_versions;
    var versionLabels = {
        openshift: 'OpenShift', k8s: 'Kubernetes',
        gpu_operator: 'GPU Operator', gpu_driver: 'GPU Driver', cuda_runtime: 'CUDA Runtime',
        network_operator: 'Network Operator', mofed: 'MOFED/DOCA',
        istio: 'Istio', service_mesh: 'Service Mesh', epp: 'EPP Scheduler',
        nfd: 'NFD', lws: 'LWS'
    };
    var vRows = [];
    Object.keys(iv).forEach(function(k) { if (iv[k]) vRows.push([versionLabels[k] || k, iv[k]]); });
    settingsSections.push({ title: 'Component Versions', color: '#059669', rows: vRows });
}

// Render as single unified table
html += '<div class="chart-card"><div class="chart-card-header">User Defined Test Settings</div>';
html += '<div class="chart-card-body" style="padding:0;">';
html += '<div style="overflow-x:auto;"><table class="results-table" style="font-size:0.9em;">';
settingsSections.forEach(function(section) {
    html += `<tr><td colspan="2" style="background:#f1f5f9;font-weight:700;color:${section.color};padding:8px 16px;font-size:1em;">${section.title}</td></tr>`;
    section.rows.forEach(function(r) {
        if (!r) return;
        html += `<tr><td style="color:#64748b;padding:4px 16px;width:35%;white-space:nowrap;">${r[0]}</td><td style="padding:4px 16px;word-break:break-all;"><strong>${r[1]}</strong></td></tr>`;
    });
});
html += '</table></div></div></div>';

// Per-Config Tuning Comparison Table
const allConfigs = [];
for (const r of (data.all_results || [])) {
    const tid = r.test_id || r.config_name || '';
    if (tid.startsWith('step2-') || tid.startsWith('step3-') || tid.startsWith('step9-') || tid.startsWith('step11-') || tid.startsWith('step13-')) continue;
    let tc = null;
    if (r.test_config) {
        try { tc = typeof r.test_config === 'string' ? JSON.parse(r.test_config) : r.test_config; } catch(e) {}
    }
    if (tc) {
        if (r.epp_deployed) tc.epp_deployed = r.epp_deployed;
        const arch = (r.architecture || '').toUpperCase();
        allConfigs.push({ label: _tagLabel(r.config_name, r.architecture), arch: arch, tc: tc, ttft: r.ttft_p90 || 1e9 });
    }
}
// Sort: AGGREGATED first (by TP), then EP (by TTFT), then PD (by TTFT)
const archSortOrder = { 'AGGREGATED': 0, 'EP': 1, 'PD': 2 };
allConfigs.sort((a, b) => (archSortOrder[a.arch] || 3) - (archSortOrder[b.arch] || 3) || a.ttft - b.ttft);
const hasEpConfig = allConfigs.some(c => c.arch === 'EP');

if (allConfigs.length > 0) {
    html += '<div class="chart-card" style="margin-top:24px;">';
    html += '<div class="chart-card-header">Tuned Settings vs Upstream Defaults</div>';
    html += '<div style="font-size:0.85em;color:#64748b;padding:8px 20px;text-align:center;">Green = auto-tuned by ServeIt Studio. Gray = upstream default (unchanged).</div>';
    html += '<div class="chart-card-body" style="padding:0;">';

    const hasEp = hasEpConfig;
    const na = '<span style="color:#cbd5e1;">N/A</span>';

    const _eppPresets = {
        balanced: {prefix_cache_weight:3, kv_cache_weight:2, queue_weight:2, active_request_weight:2, decode_prefix_cache_weight:1, decode_active_request_weight:3},
        cache_optimized: {prefix_cache_weight:5, kv_cache_weight:1, queue_weight:2, active_request_weight:1, decode_prefix_cache_weight:1, decode_active_request_weight:3},
        queue_balanced: {prefix_cache_weight:1, kv_cache_weight:1, queue_weight:3, active_request_weight:3, decode_prefix_cache_weight:1, decode_active_request_weight:3},
        latency_aware: {prefix_cache_weight:3, kv_cache_weight:2, queue_weight:2, active_request_weight:2, decode_prefix_cache_weight:1, decode_active_request_weight:3},
    };
    function _eppW(tc) {
        // Use the ARCHIVED configmap weights (what actually deployed)
        if (tc.epp_deployed && tc.epp_deployed.prefix_cache_weight !== undefined) {
            return tc.epp_deployed;
        }
        if (!tc.epp_config) return null;
        const preset = tc.epp_config.preset || rc.epp_preset || 'balanced';
        return _eppPresets[preset] || _eppPresets.balanced;
    }
function getVal(tc, key) { return tc[key] != null ? String(tc[key]) : null; }
    function pdVal(tc, pKey, dKey) {
        const p = tc[pKey], d = tc[dKey];
        if (p != null && d != null && p !== d) return `P=${p} D=${d}`;
        return p != null ? String(p) : (d != null ? String(d) : null);
    }
    function boolVal(tc, key) { return tc[key] === true ? 'On' : tc[key] === false ? 'Off' : null; }

    const sections = [
        { title: 'Memory & Batching', params: [
            { label: 'gpu-memory-utilization', def: '0.90', get: tc => pdVal(tc, 'prefill_gpu_memory_utilization', 'decode_gpu_memory_utilization') || getVal(tc, 'gpu_memory_utilization') },
            { label: 'max-model-len', def: 'auto', get: tc => getVal(tc, 'max_model_len') },
            { label: 'max-num-seqs', def: '256', get: tc => pdVal(tc, 'prefill_max_num_seqs', 'decode_max_num_seqs') || getVal(tc, 'max_num_seqs') },
            { label: 'max-num-batched-tokens', def: 'auto', get: tc => getVal(tc, 'max_num_batched_tokens') },
            { label: 'block-size', def: '16', get: tc => getVal(tc, 'block_size') },
            { label: 'kv-cache-memory-bytes', def: 'auto', get: tc => getVal(tc, 'kv_cache_memory_bytes'), pd_only: true },
        ]},
        { title: 'Precision & Compute', params: [
            { label: 'dtype', def: 'auto', get: tc => getVal(tc, 'dtype') },
            { label: 'kv-cache-dtype', def: 'auto', get: tc => getVal(tc, 'kv_cache_dtype') },
            { label: 'pipeline-parallel-size', def: '1', get: tc => getVal(tc, 'pipeline_parallel_size') },
        ]},
        { title: 'Feature Flags', params: [
            { label: 'enable-prefix-caching', def: 'Off', get: tc => boolVal(tc, 'enable_prefix_caching') },
            { label: 'enable-expert-parallel', def: 'Off', get: tc => boolVal(tc, 'enable_expert_parallel') },
            { label: 'enable-dbo', def: 'Off', get: tc => boolVal(tc, 'enable_dbo') },
            { label: 'enable-eplb', def: 'Off', get: tc => boolVal(tc, 'enable_eplb') },
            { label: 'trust-remote-code', def: 'Off', get: tc => boolVal(tc, 'trust_remote_code') },
        ]},
        { title: 'MoE / Expert Parallel', params: [
            { label: 'moe-dp-chunk-size', def: '256', get: tc => getVal(tc, 'moe_dp_chunk_size'), ep_only: true },
            { label: 'all2all-backend', def: 'auto', get: tc => getVal(tc, 'all2all_backend') || (tc.decode_all2all_backend ? `HT / ${tc.decode_all2all_backend}` : null), ep_only: true },
            { label: 'moe-backend', def: 'auto', get: tc => getVal(tc, 'moe_backend'), ep_only: true },
            { label: 'use-deep-gemm', def: 'auto', get: tc => tc.use_deep_gemm === true ? 'On' : tc.use_deep_gemm === false ? 'Off' : null },
            { label: 'dbo-prefill-threshold', def: '32', get: tc => getVal(tc, 'dbo_prefill_token_threshold') },
            { label: 'dbo-decode-threshold', def: '32', get: tc => getVal(tc, 'dbo_decode_token_threshold') },
            { label: 'num-redundant-experts', def: '32', get: tc => getVal(tc, 'num_redundant_experts'), ep_only: true },
            { label: 'NVSHMEM_SYMMETRIC_SIZE', def: '16G', get: tc => getVal(tc, 'nvshmem_symmetric_size'), ep_only: true },
        ]},
        { title: 'EPP Prefill Routing Weights', params: [
            { label: 'prefix-cache-weight', def: '3', get: tc => { const w = _eppW(tc); return w ? String(w.prefix_cache_weight) : null; } },
            { label: 'kv-cache-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.kv_cache_weight) : null; } },
            { label: 'queue-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.queue_weight) : null; } },
            { label: 'active-request-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.active_request_weight) : null; } },
        ]},
        { title: 'EPP Decode Routing Weights', params: [
            { label: 'decode-prefix-cache-weight', def: '3', get: tc => { const w = _eppW(tc); return w ? String(w.decode_prefix_cache_weight) : null; } },
            { label: 'decode-active-request-weight', def: '2', get: tc => { const w = _eppW(tc); return w ? String(w.decode_active_request_weight) : null; } },
        ]},
        { title: 'EPP Auto-Calculated', params: [
            { label: 'maxPrefixBlocksToMatch', def: 'auto', get: tc => tc.epp_config ? String(tc.epp_config.max_prefix_blocks || tc.epp_config.maxPrefixBlocksToMatch || '-') : null },
            { label: 'lruCapacityPerServer', def: 'auto', get: tc => tc.epp_config ? String(tc.epp_config.lru_capacity || tc.epp_config.lruCapacityPerServer || '-') : null },
            { label: 'nonCachedTokens', def: '16', get: tc => tc.epp_config ? String(tc.epp_config.non_cached_tokens || tc.epp_config.nonCachedTokens || '-') : null },
        ]},
    ];

    const archColors2 = { 'AGGREGATED': '#6366f1', 'PD': '#0ea5e9', 'EP': '#10b981' };
    html += '<div style="overflow-x:auto;"><table class="results-table" style="font-size:0.85em;">';
    html += '<tr><th style="text-align:center;min-width:180px;position:sticky;left:0;background:#1e293b;z-index:1;">Parameter</th><th style="text-align:center;min-width:70px;">Default</th>';
    for (const cfg of allConfigs) {
        const color = archColors2[cfg.arch] || '#64748b';
        html += `<th style="color:${color};text-align:center;min-width:90px;font-size:0.85em;white-space:nowrap;">${cfg.label}</th>`;
    }
    html += '</tr>';

    for (const section of sections) {
        html += `<tr><td colspan="${2 + allConfigs.length}" style="background:#f1f5f9;font-weight:700;color:#475569;padding:8px 10px;font-size:1em;text-align:center;">${section.title}</td></tr>`;
        for (const param of section.params) {
            if (param.ep_only && !hasEp) continue;
            html += `<tr><td style="color:#334155;text-align:center;position:sticky;left:0;background:white;z-index:1;"><code style="font-size:0.9em;">${param.label}</code></td>`;
            html += `<td style="color:#94a3b8;text-align:center;">${param.def}</td>`;
            for (const cfg of allConfigs) {
                const tc = cfg.tc;
                if (param.ep_only && cfg.arch !== 'EP') { html += `<td style="text-align:center;">${na}</td>`; continue; }
                if (param.pd_only && cfg.arch === 'AGGREGATED') { html += `<td style="text-align:center;">${na}</td>`; continue; }
                const val = param.get(tc);
                const display = (val === null || val === 'null' || val === undefined) ? 'off' : (val || param.def);
                const changed = val && val !== param.def && val !== 'null' && val !== undefined;
                const style = changed ? 'font-weight:700;color:#059669;' : '';
                html += `<td style="text-align:center;${style}">${display}</td>`;
            }
            html += '</tr>';
        }
    }

    html += '</table></div></div></div>';
}

        return html;
    }

    ReportTabs.register({
        id: 'test-settings',
        label: 'Test Settings',
        icon: '&#9881;',
        position: 'before:estimator',
        build: build
    });
})();
