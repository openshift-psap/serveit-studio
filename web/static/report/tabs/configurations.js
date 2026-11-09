// tabs/configurations.js — Configurations report tab. One implementation for live + offline.
(function() {
    var _renders = [];

    function _tagLabel(name, arch) {
        if (!name || name.indexOf('(') >= 0) return name || '';
        var a = (arch || '').toUpperCase();
        if (a === 'PD') return name + ' (PD)';
        if (a === 'EP') return name + ' (EP)';
        return name + ' (AG)';
    }

    function build(data, ctx) {
        var s = '';
        var _sfx = '';
        if (ctx.mode === 'live' && typeof _chartSuffix !== 'undefined') _sfx = _chartSuffix;
        _renders = [];
        function chartCardLocal(title, desc, id) {
            return '<div class="chart-card"><div class="chart-card-header">' + title + '</div>' +
                '<div style="padding:8px 20px 0; color:#1e293b; font-size:0.92em; line-height:1.5;">' + desc + '</div>' +
                '<div class="chart-card-body"><div id="' + ctx.chartId(id) + '" style="width:100%;height:850px;"></div></div></div>';
        }
        var charts = data.charts || {};
        var rec = data.recommendation || {};
        var coreResults = (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || r.config_name || '';
            return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0;
        });
        var runId = ctx.runId;
        var allRes = data.all_results || [];
        // Build manifest/test-id lookups (ported from charts.js)
        var manifestLookup = {};
        var testIdLookup = {};
        allRes.forEach(function(r) {
            if (r.test_id) {
                testIdLookup[r.config_name] = r.test_id;
                if (r.manifests && typeof r.manifests === 'object') {
                    manifestLookup[r.test_id] = r.manifests;
                }
            }
        });
    // Scatter, efficiency → Configurations subtab
    s += '<div class="chart-card" style="border-left:6px solid #6366f1;">' +
        '<div class="chart-card-header" style="background:linear-gradient(135deg,#6366f1,#818cf8); color:white;">Configurations</div>' +
        '<div style="padding:12px 20px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
        'All tested configurations across PD, EP, and Aggregated architectures. Charts show TTFT vs Throughput trade-offs at each percentile. ' +
        'The <strong>Pareto Optimal</strong> table highlights configurations that offer the best trade-offs — no other config is better on both latency and throughput simultaneously.' +
        '</div></div>';
    if (rec && rec.best_by_percentile && rec.best_by_percentile.p90) {
        var _bbpSel = rec.best_by_percentile.p90;
        // Mark primary recommendation config names
        var _recLabel = {};
        if (rec.recommendations) {
            var _rtCfg = rec.recommendations.response_time && rec.recommendations.response_time.config;
            var _tpCfg = rec.recommendations.throughput    && rec.recommendations.throughput.config;
            if (_rtCfg) _recLabel[_rtCfg.config_name] = '★ Primary TTFT recommendation';
            if (_tpCfg) _recLabel[_tpCfg.config_name] = '★ Primary throughput recommendation';
        }

        function _fmtMs(v) { return v == null ? '—' : (v >= 1000 ? (v/1000).toFixed(1)+'s' : Math.round(v)+'ms'); }
        function _fmtTput(v) { return v == null ? '—' : v.toFixed(2)+' req/s'; }
        var _tdBase = 'padding:7px 10px;border:1px solid #e2e8f0;text-align:center;';
        var _tdLeft = 'padding:7px 10px;border:1px solid #e2e8f0;text-align:center;';

        var _archDefs = [
            { key: 'pd',          label: 'PD',          color: '#0ea5e9', bg: '#f0f9ff' },
            { key: 'ep',          label: 'EP',          color: '#10b981', bg: '#f0fdf4' },
            { key: 'aggregated',  label: 'Aggregated',  color: '#6366f1', bg: '#eef2ff' }
        ];
        var _catDefs = [
            { key: 'balanced',       label: 'Best balanced'   },
            { key: 'lowest_ttft',    label: 'Best TTFT'       },
            { key: 'highest_tput',   label: 'Best throughput' },
            { key: 'most_efficient', label: 'Most efficient'  },
            { key: 'lowest_itl',     label: 'Best ITL'        }
        ];

        var _anyArch = _archDefs.some(function(a) { return !!_bbpSel[a.key]; });
        if (_anyArch) {
            var _selTableId = 'cfg-selection-table' + _sfx;
            s += '<div class="chart-card">';
            s += '<div class="chart-card-header">Configuration Selection — Top 5 per Architecture</div>';
            s += '<div id="' + _selTableId + '" style="padding:10px 20px 16px;">';
            s += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">';
            s += '<p style="color:#64748b;font-size:0.82em;margin:0;">The optimizer selects up to 5 configurations per architecture for the concurrency sweep — one per category. ★ = final primary recommendation.</p>';
            s += '<button onclick="downloadTableAsPng(\'' + _selTableId + '\',\'configuration-selection.png\')" style="flex-shrink:0;margin-left:12px;font-size:0.78em;padding:4px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#475569;cursor:pointer;">&#11015; Download PNG</button>';
            s += '</div>';

            _archDefs.forEach(function(arch) {
                var ad = _bbpSel[arch.key];
                if (!ad) return;
                s += '<div style="margin-bottom:18px;">';
                s += '<div style="font-weight:700;font-size:0.85em;color:' + arch.color + ';text-transform:uppercase;letter-spacing:0.06em;border-bottom:2px solid ' + arch.color + ';padding-bottom:4px;margin-bottom:6px;">' + arch.label + ' Architecture</div>';
                s += '<table style="width:100%;font-size:0.83em;border-collapse:collapse;table-layout:fixed;">';
                s += '<colgroup><col style="width:20%"><col style="width:22%"><col style="width:13%"><col style="width:11%"><col style="width:17%"><col style="width:17%"></colgroup>';
                var _tdNow = _tdBase + 'white-space:nowrap;';
                s += '<tr style="background:#f1f5f9;">' +
                    '<th style="' + _tdLeft + 'font-weight:700;">Category</th>' +
                    '<th style="' + _tdLeft + 'font-weight:700;">Config</th>' +
                    '<th style="' + _tdNow + 'font-weight:700;">TTFT P90</th>' +
                    '<th style="' + _tdNow + 'font-weight:700;">ITL P90</th>' +
                    '<th style="' + _tdNow + 'font-weight:700;">Throughput</th>' +
                    '<th style="' + _tdNow + 'font-weight:700;">GPUs</th>' +
                    '</tr>';

                // Deduplicate: group categories by config_name, preserving category order
                var _seen = {}, _deduped = [];
                _catDefs.forEach(function(cat) {
                    var e = ad[cat.key];
                    if (!e) return;
                    var cn = e.config_name;
                    if (_seen[cn]) {
                        _seen[cn].cats.push(cat.label);
                    } else {
                        _seen[cn] = { entry: e, cats: [cat.label] };
                        _deduped.push(cn);
                    }
                });
                _deduped.forEach(function(cn) {
                    var e      = _seen[cn].entry;
                    var cats   = _seen[cn].cats;
                    var recLbl = _recLabel[cn];
                    var isRec  = !!recLbl;
                    var rowBg  = isRec ? 'background:#fffbeb;' : '';
                    var catBadges = cats.map(function(lbl) {
                        return '<span style="display:inline-block;background:' + arch.bg + ';color:' + arch.color + ';font-size:0.78em;font-weight:600;padding:1px 7px;border-radius:8px;margin:1px 2px 1px 0;">' + lbl + '</span>';
                    }).join('');
                    var recBadge = isRec ? ' <span style="display:inline-block;background:#fef3c7;color:#b45309;font-size:0.78em;font-weight:700;padding:1px 7px;border-radius:8px;">' + recLbl + '</span>' : '';
                    s += '<tr style="' + rowBg + '">';
                    s += '<td style="' + _tdLeft + '">' + catBadges + recBadge + '</td>';
                    s += '<td style="' + _tdLeft + 'font-weight:' + (isRec?'700':'400') + ';">' + cn + '</td>';
                    s += '<td style="' + _tdNow + '">' + _fmtMs(e.ttft)                         + '</td>';
                    s += '<td style="' + _tdNow + '">' + _fmtMs(e.itl)                          + '</td>';
                    s += '<td style="' + _tdNow + '">' + _fmtTput(e.throughput_mean || e.throughput) + '</td>';
                    s += '<td style="' + _tdNow + '">' + (e.gpus != null ? e.gpus : '—')        + '</td>';
                    s += '</tr>';
                });

                s += '</table></div>';
            });
            s += '</div></div>';
        }
    }
    s += chartCardLocal('Throughput vs Latency', 'Each bubble is a tested configuration. The <strong>bubble size</strong> represents GPU count. The ideal configuration is in the <strong>top-left corner</strong> (low latency + high throughput).', 'chart-scatter');
    s += chartCardLocal('GPU Efficiency (req/s per GPU)', 'Shows how many requests each configuration can serve per GPU. <strong>Higher bars = better value for money.</strong>', 'chart-efficiency');
    s += chartCardLocal('Per-User Token Throughput (tokens/s per user)', 'Shows how many output tokens each concurrent user receives per second. <strong>Higher = faster streaming for each user.</strong>', 'chart-per-user-efficiency');

    // Cache Hit Rate per configuration
    var cfgCacheData = coreResults.filter(function(r) { return r.cache_hit_pct != null; });
    if (cfgCacheData.length) {
        s += '<div class="chart-card"><div class="chart-card-header">Prefix Cache Hit Rate per Configuration</div>';
        s += '<div style="padding:8px 20px 0;color:#1e293b;font-size:0.92em;">Actual prefix cache hit percentage measured by vLLM during each test. Higher = more requests served from cached KV data.</div>';
        s += '<div class="chart-card-body"><div id="chart-cfg-cache-hit' + _sfx + '" style="width:100%;height:850px;"></div></div></div>';
        var archColors3 = { AGGREGATED: '#1f77b4', PD: '#ff7f0e', EP: '#2ca02c' };
        _renders.push(function() {
            var traces = [];
            ['AGGREGATED', 'PD', 'EP'].forEach(function(arch) {
                var ar = cfgCacheData.filter(function(r) { return r.architecture === arch; });
                if (!ar.length) return;
                traces.push({
                    x: ar.map(function(r) { return _tagLabel(r.config_name, r.architecture); }),
                    y: ar.map(function(r) { return r.cache_hit_pct; }),
                    text: ar.map(function(r) { return r.cache_hit_pct.toFixed(1) + '%'; }),
                    textposition: 'outside', textfont: { size: 13 },
                    cliponaxis: false,
                    name: arch, type: 'bar',
                    marker: { color: archColors3[arch] }
                });
            });
            Plotly.newPlot('chart-cfg-cache-hit' + _sfx, traces, {
                height: 850, barmode: 'group',
                xaxis: { tickangle: -35, gridcolor: '#e2e8f0' },
                yaxis: { title: 'Cache Hit %', gridcolor: '#e2e8f0', range: [0, 110], dtick: 25 },
                showlegend: true, legend: { x: 0, y: 1.02, bgcolor: 'rgba(255,255,255,0.9)' },
                margin: { t: 20, b: 120, l: 60, r: 20 },
                plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
            }, { responsive: true });
        });
    }


    // --- PD configurations TTFT + Throughput charts (one per percentile) ---
    if (coreResults.filter(r => r.architecture === 'PD').length) {
        s += chartCardLocal(
            'PD Configurations — TTFT & Throughput (P90)',
            '<strong style="color:#3b82f6">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P90.',
            'chart-pd-ttft-p90'
        );
        s += chartCardLocal(
            'PD Configurations — TTFT & Throughput (P95)',
            '<strong style="color:#dc2626">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P95.',
            'chart-pd-ttft-p95'
        );
        s += chartCardLocal(
            'PD Configurations — TTFT & Throughput (P99)',
            '<strong style="color:#7c3aed">TTFT</strong> (left axis, lower is better) and <strong style="color:#f59e0b">Throughput</strong> (right axis, higher is better) at P99.',
            'chart-pd-ttft-p99'
        );
    }

    // --- Aggregated configurations chart (all percentiles in one chart) ---
    if (coreResults.filter(r => r.architecture === 'AGGREGATED').length > 1) {
        s += chartCardLocal(
            'Aggregated Configurations — TTFT & Throughput (P90 / P95 / P99)',
            'All aggregated configurations with <strong style="color:#3b82f6">TTFT</strong> (bars, lower is better) and <strong style="color:#f59e0b">Throughput Mean</strong> (line, right axis, higher is better) across percentiles.',
            'chart-agg-ttft-all'
        );
    }

    // --- Pareto table ---
    if (charts.pareto.pareto_table.length) {
        s += '<div class="chart-card"><div class="chart-card-header">Pareto Optimal Configurations</div>';
        s += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">These configurations represent the <strong>best possible trade-offs</strong>. Each one is optimal for a different balance of speed, throughput, and GPU cost. No other tested configuration beats any of these on all metrics at once.</div>';
        s += '<div class="chart-card-body" style="padding:0;">';
        s += '<table class="results-table"><tr><th>Configuration</th><th>Metric</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>GPUs</th><th title="Throughput Mean ÷ Total GPUs (req/s per GPU). Higher = better cost-efficiency.">Efficiency<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th><th>Manifests</th></tr>';
        charts.pareto.pareto_table.forEach((p, idx) => {
            let manifestLinks = '-';
            const pTestId = p.test_id || testIdLookup[p.config_name] || p.config_name;
            const mTypes = manifestLookup[pTestId];
            if (mTypes && mTypes.length) {
                const mlabels2 = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
                manifestLinks = mTypes.filter(t => !t.includes('service')).map(t => {
                    return `<a href="/api/run/${runId}/config/${pTestId}/manifest/${t}" title="Download ${t}.yaml" target="_blank" style="color:#0ea5e9; text-decoration:none; font-size:11px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${mlabels2[t] || t} &#8595;</a>`;
                }).join(' ');
            }
            const borderTop = idx > 0 ? ' border-top:2px solid #cbd5e1;' : '';
            const tputMeanPareto = p.throughput_mean ?? p.throughput_p90;
            const latencyMetrics = [
                {name: 'TTFT (ms)', p50: p.ttft_p50, p90: p.ttft_p90, p95: p.ttft_p95, p99: p.ttft_p99},
                {name: 'ITL (ms)', p50: p.itl_p50, p90: p.itl_p90, p95: p.itl_p95, p99: p.itl_p99},
            ];
            latencyMetrics.forEach((m, mi) => {
                const rowBorder = mi === 0 && idx > 0 ? borderTop : '';
                s += `<tr class="pareto-row">`;
                if (mi === 0) {
                    const pEppBadge = (pTestId && pTestId.startsWith('step11-epp-')) ? '<br><span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
                    s += `<td rowspan="3" style="vertical-align:middle; font-weight:700;${rowBorder}">${p.config_name}<br><span style="font-weight:400; font-size:0.85em; color:#64748b;">${p.architecture}</span>${pEppBadge}</td>`;
                }
                s += `<td style="color:#64748b;${rowBorder}">${m.name}</td>`;
                s += `<td style="${rowBorder}">${m.p50 ?? '-'}</td>`;
                s += `<td style="${rowBorder}">${m.p90 ?? '-'}</td>`;
                s += `<td style="${rowBorder}">${m.p95 ?? '-'}</td>`;
                s += `<td style="${rowBorder}">${m.p99 ?? '-'}</td>`;
                if (mi === 0) {
                    s += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${p.gpus}</td>`;
                    s += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${p.efficiency}</td>`;
                    s += `<td rowspan="3" style="vertical-align:middle;${rowBorder}">${manifestLinks}</td>`;
                }
                s += '</tr>';
            });
            // Throughput mean — single value spanning P50-P99 columns
            s += `<tr class="pareto-row">`;
            s += `<td style="color:#64748b;">Throughput Mean (req/s)</td>`;
            s += `<td colspan="4" style="text-align:center; font-weight:600;">${tputMeanPareto ?? '-'}</td>`;
            s += '</tr>';
        });
        s += '</table></div></div>';
    }

    // --- Token Throughput section ---
    var tokenResults = coreResults.filter(function(r) {
        var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
        var pm = m.prometheus_metrics || {};
        return r.output_tps_mean || m.output_tps_mean || pm.vllm_generation_tokens_rate || r._gen_tps_avg;
    });
    if (tokenResults.length) {
        var tkTableId = 'token-tput-table-' + runId;
        s += '<div class="chart-card"><div class="chart-card-header">Token Throughput — GPU Computed vs Server Total vs Client</div>';
        s += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.93em; line-height:1.6;">' +
            '<strong>Blue bar (GPU computed):</strong> Tokens that required actual GPU compute — prompt tokens that were <em>not</em> served from prefix cache, plus generated output tokens. Calculated as: <code>prompt_tps &times; (1 - cache_hit_rate) + generation_tps</code>.<br>' +
            '<strong>Light blue bar (server total):</strong> All tokens processed by vLLM server-side, including cached prefix tokens. Measured via Prometheus (<code>vllm:prompt_tokens + vllm:generation_tokens</code>).<br>' +
            '<strong>Gold bar (client):</strong> All tokens measured client-side by guidellm (<code>prompt_tokens_mean &times; throughput + output_tokens_mean &times; throughput</code>).<br>' +
            '<strong>The gap between blue and light blue reflects prefix cache efficiency.</strong> The gap between light blue and gold reflects measurement differences (network overhead, timing windows).' +
            '</div>';
        s += '<div class="chart-card-body"><div id="chart-token-tput' + ctx.chartId('') + '" style="height:850px;margin-bottom:16px;"></div></div></div>';

        s += '<div class="chart-card"><div class="chart-card-header">Throughput &amp; Utilization Details</div>';
        s += '<div class="chart-card-body" style="padding:0;">';
        s += '<table class="results-table" id="' + tkTableId + '"><tr>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',1,\'num\')">Prompt tok/s &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">avg (peak)</span></th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',2,\'num\')">Generation tok/s &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">avg (peak)</span></th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',3,\'num\')">Total tok/s &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',4,\'num\')">vLLM req/s &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',5,\'num\')">guidellm req/s &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',6,\'num\')">Avg Running &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',7,\'num\')">Avg Waiting &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',8,\'num\')">Cache Hit &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + tkTableId + '\',9,\'num\')">KV Cache &#x21C5;</th>';
        s += '</tr>';
        tokenResults.forEach(function(r) {
            var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
            var pm = m.prometheus_metrics || {};
            var promptRate = pm.vllm_prompt_tokens_rate || {};
            var genRate = pm.vllm_generation_tokens_rate || {};
            var promptAvg = promptRate.avg != null ? Math.round(promptRate.avg) : (r._prompt_tps_avg != null ? Math.round(r._prompt_tps_avg) : null);
            var promptMax = promptRate.max != null ? Math.round(promptRate.max) : (r._prompt_tps_max != null ? Math.round(r._prompt_tps_max) : null);
            var genAvg = genRate.avg != null ? Math.round(genRate.avg) : (r._gen_tps_avg != null ? Math.round(r._gen_tps_avg) : null);
            var genMax = genRate.max != null ? Math.round(genRate.max) : (r._gen_tps_max != null ? Math.round(r._gen_tps_max) : null);
            var totalAvg = (promptAvg != null && genAvg != null) ? promptAvg + genAvg : null;
            var vllmReqRate = (pm.vllm_e2e_count_rate || {}).avg;
            if (vllmReqRate == null) vllmReqRate = r._vllm_req_rate != null ? r._vllm_req_rate : null;
            var vllmReqStr = vllmReqRate != null ? vllmReqRate.toFixed(2) : 'N/A';
            var guidellmReq = r.throughput_mean != null ? r.throughput_mean.toFixed(2) : 'N/A';
            var avgRunning = (pm.vllm_requests_running || {}).avg;
            if (avgRunning == null) avgRunning = r._avg_running != null ? r._avg_running : null;
            var avgWaiting = (pm.vllm_requests_waiting || {}).avg;
            if (avgWaiting == null) avgWaiting = r._avg_waiting != null ? r._avg_waiting : null;
            var cacheHit = r.cache_hit_pct != null ? r.cache_hit_pct + '%' : 'N/A';
            var kvUsage = m.kv_cache_usage != null ? (m.kv_cache_usage * 100).toFixed(1) + '%' : 'N/A';
            var na = 'N/A';
            var fmtRate = function(avg, max) { return avg != null ? avg.toLocaleString() + ' (' + (max != null ? max.toLocaleString() : '?') + ')' : na; };
            s += '<tr><td>' + r.config_name + '</td>';
            s += '<td data-val="' + (promptAvg || '') + '">' + fmtRate(promptAvg, promptMax) + '</td>';
            s += '<td data-val="' + (genAvg || '') + '">' + fmtRate(genAvg, genMax) + '</td>';
            s += '<td data-val="' + (totalAvg || '') + '">' + (totalAvg != null ? totalAvg.toLocaleString() : na) + '</td>';
            s += '<td data-val="' + (vllmReqRate || '') + '">' + vllmReqStr + '</td>';
            s += '<td data-val="' + (r.throughput_mean || '') + '">' + guidellmReq + '</td>';
            s += '<td data-val="' + (avgRunning || '') + '">' + (avgRunning != null ? avgRunning.toFixed(1) : na) + '</td>';
            s += '<td data-val="' + (avgWaiting || '') + '">' + (avgWaiting != null ? avgWaiting.toFixed(1) : na) + '</td>';
            s += '<td data-val="' + (r.cache_hit_pct || '') + '">' + cacheHit + '</td>';
            s += '<td data-val="' + (m.kv_cache_usage || '') + '">' + kvUsage + '</td>';
            s += '</tr>';
        });
        s += '</table></div></div>';
    }

    // --- All results table ---
    if (coreResults.length) {
        var allCfgTableId = 'all-configs-table-' + runId;
        s += '<div class="chart-card"><div class="chart-card-header">Latency and Efficiency</div>';
        s += '<div style="padding:12px 20px 4px; color:#1e293b; font-size:0.95em;">Complete results from every test that ran successfully. <strong>Green highlighted rows</strong> are Pareto optimal (the best trade-offs). Click any column header to sort.</div>';
        s += '<div class="chart-card-body" style="padding:0;">';
        s += '<table class="results-table" id="' + allCfgTableId + '"><tr>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',0,\'str\')">Configuration &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',1,\'str\')">Architecture &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',2,\'num\')">TTFT P90 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',3,\'num\')">E2E P90 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',4,\'num\')">TTFT P95 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',5,\'num\')">TTFT P99 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',6,\'num\')">Tput Mean &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',7,\'num\')">ITL P90 &#x21C5;</th>';
        s += '<th style="cursor:pointer;" onclick="sortReportTable(\'' + allCfgTableId + '\',8,\'num\')">GPUs &#x21C5;</th>';
        s += '<th style="cursor:pointer;" title="Throughput Mean ÷ Total GPUs (req/s per GPU)" onclick="sortReportTable(\'' + allCfgTableId + '\',9,\'num\')">Efficiency &#x21C5;<br><span style="font-weight:400;font-size:0.75em;color:#64748b;">req/s per GPU</span></th>';
        s += '<th>Manifests</th>';
        s += '</tr>';
        const paretoNames = new Set(charts.pareto.pareto_table.map(p => p.config_name));
        coreResults.forEach((r, idx) => {
            const cls = paretoNames.has(r.config_name) ? ' class="pareto-row"' : '';
            const rTestId = r.test_id || testIdLookup[r.config_name] || r.config_name;
            let manifestLinks = '-';
            if (r.manifest_types && r.manifest_types.length > 0) {
                const mlabels = { lws: 'LWS', prefill: 'Prefill LWS', decode: 'Decode LWS', 'epp-configmap': 'EPP Config' };
                manifestLinks = r.manifest_types.filter(t => !t.includes('service')).map(t => {
                    return `<a href="/api/run/${runId}/config/${rTestId}/manifest/${t}" title="Download ${t}.yaml" target="_blank" style="color:#0ea5e9; text-decoration:none; font-size:11px; padding:2px 6px; background:#f0f9ff; border-radius:4px; border:1px solid #bae6fd; margin:1px; display:inline-block;">${mlabels[t] || t} &#8595;</a>`;
                }).join(' ');
            }
            const na = 'N/A';
            const eppBadge = (rTestId && rTestId.startsWith('step11-epp-')) ? ' <span style="background:#7c3aed;color:white;font-size:0.65em;padding:1px 5px;border-radius:3px;">EPP TUNED</span>' : '';
            const tputMeanVal = r.throughput_mean ?? r.throughput_p90 ?? na;
            const e2eVal = r.e2e_latency_p90 != null ? Math.round(r.e2e_latency_p90) : na;
            s += `<tr${cls}><td>${r.config_name}${eppBadge}</td><td>${r.architecture}</td><td data-val="${r.ttft_p90}">${r.ttft_p90}</td><td data-val="${e2eVal === na ? '' : e2eVal}">${e2eVal}${e2eVal !== na ? ' ms' : ''}</td><td data-val="${r.ttft_p95 ?? ''}">${r.ttft_p95 ?? na}</td><td data-val="${r.ttft_p99 ?? ''}">${r.ttft_p99 ?? na}</td><td data-val="${tputMeanVal}">${tputMeanVal}</td><td data-val="${r.itl_p90 ?? ''}">${r.itl_p90 ?? na}</td><td data-val="${r.gpus}">${r.gpus}</td><td data-val="${r.efficiency}">${r.efficiency}</td><td>${manifestLinks}</td></tr>`;
        });
        s += '</table></div></div>';
    }

    // --- vLLM Engine Metrics for configuration tests ---
    var cfgEngineRes = (data.all_results || []).filter(function(r) {
        if (r.quality === 'discard') return false;
        var tid = r.test_id || '';
        return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
               tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
    });
    if (cfgEngineRes.length >= 1) {
        s += '<div class="chart-card" style="margin-top:16px;"><div class="chart-card-header">vLLM Engine Metrics</div>';
        s += '<div style="padding:8px 20px 4px; font-size:0.85em; color:#64748b;">KV cache usage, running/waiting requests, prefill/decode time across tested configurations.</div>';
        s += '<div id="chart-cfg-engine' + _sfx + '" style="width:100%;"></div></div>';

        _renders.push(function() {
            var labels = [], kvAvg = [], kvMax = [], runAvg = [], runMax = [], runSpread = [], waitAvg = [], prefillT = [], decodeT = [], preempt = [];
            cfgEngineRes.forEach(function(r) {
                var mj = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                var pm = mj.prometheus_metrics || {};
                labels.push(_tagLabel(r.config_name, r.architecture));
                var _rAvg = (pm.vllm_requests_running || {}).avg || r._avg_running || 0;
                var _rMax = (pm.vllm_requests_running || {}).max || r._avg_running || 0;
                var _kvA = ((pm.vllm_kv_cache_pct || {}).avg != null) ? (pm.vllm_kv_cache_pct.avg * 100) : ((r._kv_cache_avg != null) ? (r._kv_cache_avg * 100) : 0);
                var _kvM = ((pm.vllm_kv_cache_pct || {}).max != null) ? (pm.vllm_kv_cache_pct.max * 100) : ((r._kv_cache_max != null) ? (r._kv_cache_max * 100) : 0);
                kvAvg.push(_kvA);
                kvMax.push(_kvM);
                runAvg.push(_rAvg || r._avg_running || 0);
                runMax.push(_rMax || r._avg_running || 0);
                runSpread.push(_rMax - _rAvg);
                waitAvg.push((pm.vllm_requests_waiting || {}).avg || r._avg_waiting || 0);
                prefillT.push((pm.vllm_prefill_time_rate || {}).avg || r._prefill_time_rate || 0);
                decodeT.push((pm.vllm_decode_time_rate || {}).avg || r._decode_time_rate || 0);
                preempt.push((pm.vllm_preemptions_rate || {}).avg || r._preemptions_rate || 0);
            });
            var traces = [
                { x: labels, y: kvAvg, name: 'KV Cache Avg %', type: 'bar', marker: { color: '#3b82f6' } },
                { x: labels, y: kvMax, name: 'KV Cache Max %', type: 'bar', marker: { color: '#93c5fd' } },
                { x: labels, y: runAvg, name: 'Avg Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 3 }, marker: { size: 8 } },
                { x: labels, y: runMax, name: 'Max Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 2, dash: 'dash' }, marker: { size: 6, symbol: 'triangle-up' } },
                { x: labels, y: runSpread, name: 'Load Spread', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 2, dash: 'dashdot' }, marker: { size: 6, symbol: 'diamond' } },
                { x: labels, y: waitAvg, name: 'Avg Waiting Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#ef4444', width: 2, dash: 'dot' }, marker: { size: 6 } },
                { x: labels, y: decodeT, name: 'Decode req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#f59e0b', width: 2 }, marker: { size: 6 } },
                { x: labels, y: prefillT, name: 'Prefill req-s/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#8b5cf6', width: 2 }, marker: { size: 6 } },
            ];
            if (preempt.some(function(v) { return v > 0; })) {
                traces.push({ x: labels, y: preempt, name: 'Preemptions/s', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#dc2626', width: 3 }, marker: { size: 8, symbol: 'x' } });
            }
            Plotly.newPlot('chart-cfg-engine' + _sfx, traces, {
                barmode: 'group',
                xaxis: { tickangle: -35 },
                yaxis: { title: 'KV Cache %', side: 'left', rangemode: 'tozero', gridcolor: '#e2e8f0' },
                yaxis2: { title: 'Count / Rate', overlaying: 'y', side: 'right', rangemode: 'tozero' },
                legend: { x: 0, y: 1.15, orientation: 'h' },
                margin: { t: 40, b: 140, l: 60, r: 60 },
                plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff',
                hovermode: 'x unified',
                height: 850,
            }, { responsive: true });
        });
    }

    // Flush configurations (PD charts + pareto table + all results)

        if (!coreResults.length) return '';
        return s;
    }
    function render(data, ctx) {
        if (typeof Plotly === 'undefined') return;
        var charts = (data.charts || {});
        var LAYOUT = { margin: { t: 10, b: 40, l: 50, r: 20 }, height: 350, plot_bgcolor: '#fff' };
        var CONFIG = { responsive: true, displayModeBar: false };

        // Throughput vs Latency scatter (ported from charts.js)
        var scatterEl = document.getElementById(ctx.chartId('chart-scatter'));
        if (scatterEl && charts.scatter && charts.scatter.traces && charts.scatter.traces.length) {
            var filtered = charts.scatter.traces.map(function(t) {
                var keep = [];
                (t.test_ids || []).forEach(function(tid, i) {
                    if (tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0) keep.push(i);
                });
                if (!keep.length && (t.x || []).length) keep = t.x.map(function(_, i) { return i; });
                return {
                    x: keep.map(function(i) { return (t.x || [])[i]; }),
                    y: keep.map(function(i) { return (t.y || [])[i]; }),
                    text: keep.map(function(i) { return (t.text || [])[i]; }),
                    name: t.name, color: t.color,
                    sizes: keep.map(function(i) { return (t.sizes || [])[i]; })
                };
            }).filter(function(t) { return t.x && t.x.length; });
            var allSizes = [];
            filtered.forEach(function(t) { (t.sizes || []).forEach(function(s) { allSizes.push(s); }); });
            var maxSize = Math.max.apply(null, allSizes) || 1;
            var traces = filtered.map(function(t) {
                return {
                    x: t.x, y: t.y, text: t.text, name: t.name,
                    mode: 'markers',
                    marker: {
                        size: (t.sizes || []).map(function(s) { return 8 + (s / maxSize) * 22; }),
                        color: t.color, opacity: 0.7, line: { width: 1, color: 'white' }
                    },
                    hovertemplate: '<b>%{text}</b><extra></extra>'
                };
            });
            Plotly.newPlot(scatterEl, traces, Object.assign({}, LAYOUT, {
                xaxis: { title: 'TTFT P90 (ms) — lower is better' },
                yaxis: { title: 'Throughput Mean (req/s) — higher is better' },
                showlegend: true, height: 450
            }), CONFIG);
        }

        // GPU Efficiency bar (ported from charts.js)
        var effEl = document.getElementById(ctx.chartId('chart-efficiency'));
        if (effEl && charts.efficiency && charts.efficiency.configs && charts.efficiency.configs.length) {
            Plotly.newPlot(effEl, [{
                x: charts.efficiency.configs,
                y: charts.efficiency.values,
                type: 'bar', marker: { color: charts.efficiency.colors },
                text: charts.efficiency.values.map(function(v) { return v != null ? v.toFixed(3) : ''; }),
                textposition: 'outside', textfont: { size: 14, color: '#333' },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>%{x}</b><br>%{y:.3f} req/s/GPU<extra></extra>'
            }], Object.assign({}, LAYOUT, {
                margin: { t: 10, b: 120, l: 50, r: 20 }, xaxis: { tickangle: -45 },
                yaxis: { title: 'Mean req/s per GPU — higher is better' }, height: 400
            }), CONFIG);
        }

        // Per-User Token Throughput bar (ported from charts.js)
        var puEl = document.getElementById(ctx.chartId('chart-per-user-efficiency'));
        if (puEl && charts.per_user_efficiency && charts.per_user_efficiency.configs && charts.per_user_efficiency.configs.length) {
            Plotly.newPlot(puEl, [{
                x: charts.per_user_efficiency.configs,
                y: charts.per_user_efficiency.values,
                type: 'bar', marker: { color: charts.per_user_efficiency.colors },
                text: charts.per_user_efficiency.values.map(function(v) { return v != null ? v.toFixed(1) : ''; }),
                textposition: 'outside', textfont: { size: 14, color: '#333' },
                cliponaxis: false, constraintext: 'none',
                hovertemplate: '<b>%{x}</b><br>%{y:.1f} tokens/s/user<extra></extra>'
            }], Object.assign({}, LAYOUT, {
                margin: { t: 10, b: 120, l: 50, r: 20 }, xaxis: { tickangle: -45 },
                yaxis: { title: 'Output tokens/s per concurrent user — higher is better' }, height: 400
            }), CONFIG);
        }

        // Token Throughput: GPU Computed vs Server Total vs Client (ported)
        var ttEl = document.getElementById('chart-token-tput' + ctx.chartId(''));
        if (ttEl) {
            var coreResults = (data.all_results || []).filter(function(r) {
                return r.quality !== 'discard';
            });
            var tokenResults = coreResults.filter(function(r) {
                var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                return r.output_tps_mean || m.output_tps_mean || r._gen_tps_avg;
            });
            if (tokenResults.length) {
                var gpuTput = [], serverTput = [], clientTput = [], labels = [];
                tokenResults.forEach(function(r) {
                    var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                    var pm = m.prometheus_metrics || {};
                    labels.push(_tagLabel(r.config_name, r.architecture));
                    // Prometheus-first with offline flat-field fallback
                    var promptTps = r._prompt_tps_avg != null ? r._prompt_tps_avg : ((pm.vllm_prompt_tokens_rate || {}).avg || 0);
                    var genTps = r._gen_tps_avg != null ? r._gen_tps_avg : ((pm.vllm_generation_tokens_rate || {}).avg || 0);
                    var cacheHits = r._cache_hits_avg != null ? r._cache_hits_avg : ((pm.vllm_prefix_cache_hits_rate || {}).avg || 0);
                    var cacheQueries = r._cache_queries_avg != null ? r._cache_queries_avg : ((pm.vllm_prefix_cache_queries_rate || {}).avg || 0);
                    var hitRate = cacheQueries > 0 ? cacheHits / cacheQueries : 0;
                    gpuTput.push(Math.round(promptTps * (1 - hitRate)) + Math.round(genTps));
                    serverTput.push(Math.round(promptTps + genTps));
                    var tput = r.throughput_mean || m.throughput_mean || 0;
                    var pMean = r._prompt_tokens_mean != null ? r._prompt_tokens_mean : (m.prompt_tokens_mean || 0);
                    var oMean = r._output_tokens_mean != null ? r._output_tokens_mean : (m.output_tokens_mean || 0);
                    clientTput.push(Math.round((pMean + oMean) * tput));
                });
                Plotly.newPlot(ttEl, [
                    { x: labels, y: gpuTput, name: 'GPU Computed', type: 'bar', marker: { color: '#3b82f6' }, text: gpuTput.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } },
                    { x: labels, y: serverTput, name: 'Server Total', type: 'bar', marker: { color: '#93c5fd' }, text: serverTput.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } },
                    { x: labels, y: clientTput, name: 'Client', type: 'bar', marker: { color: '#f59e0b' }, text: clientTput.map(function(v) { return v.toLocaleString(); }), textposition: 'outside', textfont: { size: 13, color: '#1e293b' } }
                ], Object.assign({}, LAYOUT, {
                    barmode: 'group', xaxis: { tickangle: -35 }, yaxis: { title: 'Total Tokens/s (Prompt + Generation)' },
                    margin: { t: 60, b: 140, l: 60, r: 20 }, height: 850,
                    legend: { orientation: 'h', y: 1.15 }
                }), CONFIG);
            }
        }

        // PD configurations TTFT charts (one per percentile) — ported from charts.js
        var coreResults = (data.all_results || []).filter(function(r) {
            if (r.quality === 'discard') return false;
            var tid = r.test_id || r.config_name || '';
            return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0;
        });
        [{key:'p90',field:'ttft_p90',itlField:'itl_p90',color:'#3b82f6',chartId:'chart-pd-ttft-p90'},
         {key:'p95',field:'ttft_p95',itlField:'itl_p95',color:'#dc2626',chartId:'chart-pd-ttft-p95'},
         {key:'p99',field:'ttft_p99',itlField:'itl_p99',color:'#7c3aed',chartId:'chart-pd-ttft-p99'}
        ].forEach(function(pctl) {
            var archResults = coreResults.filter(function(r) { return r.architecture === 'PD'; });
            if (!archResults.length) return;
            var sorted = archResults.slice().sort(function(a, b) { return a.prefill_pods - b.prefill_pods; });
            var labels = sorted.map(function(r) { return _tagLabel(r.config_name, r.architecture); });
            var ttftVals = sorted.map(function(r) { return r[pctl.field]; });
            var tputVals = sorted.map(function(r) { return r.throughput_mean || r.throughput_p90; });
            var itlVals = sorted.map(function(r) { return r[pctl.itlField] != null ? r[pctl.itlField] : null; });
            var hasItl = itlVals.some(function(v) { return v != null; });
            var validTtft = ttftVals.filter(function(v) { return v != null; });
            if (!validTtft.length) return;
            var bestTtftIdx = ttftVals.indexOf(Math.min.apply(null, validTtft));
            var bestTputIdx = tputVals.indexOf(Math.max.apply(null, tputVals));
            var pLabel = pctl.key.toUpperCase();

            var el = document.getElementById(ctx.chartId(pctl.chartId));
            if (!el) return;

            var traces = [
                { x: labels, y: ttftVals, type: 'scatter', mode: 'lines+markers', name: 'TTFT ' + pLabel,
                  line: { color: pctl.color, width: 3, shape: 'spline' },
                  marker: { color: pctl.color, size: 12, symbol: 'circle', line: { width: 2, color: 'white' } },
                  fill: 'tozeroy', fillcolor: pctl.color + '14' },
                { x: [labels[bestTtftIdx]], y: [validTtft[0]], type: 'scatter', mode: 'markers', name: 'Best TTFT',
                  marker: { color: '#10b981', size: 22, symbol: 'circle', line: { width: 3, color: 'white' } }, showlegend: true },
                { x: labels, y: tputVals, type: 'scatter', mode: 'lines+markers', name: 'Throughput Mean', yaxis: 'y2',
                  line: { color: '#f59e0b', width: 3, shape: 'spline' },
                  marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } } },
                { x: [labels[bestTputIdx]], y: [tputVals[bestTputIdx]], type: 'scatter', mode: 'markers', name: 'Best Throughput', yaxis: 'y2',
                  marker: { color: '#e11d48', size: 22, symbol: 'diamond', line: { width: 3, color: 'white' } }, showlegend: true },
            ];

            var layout = {
                height: 850,
                margin: { t: 30, b: 180, l: 60, r: 60 },
                xaxis: { title: { text: 'Configuration', standoff: 20 }, tickangle: -40, anchor: hasItl ? 'y3' : 'y' },
                yaxis: { title: 'TTFT ' + pLabel + ' (ms)', side: 'left', titlefont: { color: pctl.color }, tickfont: { color: pctl.color }, tickformat: '.2s', domain: hasItl ? [0.28, 1] : [0, 1] },
                yaxis2: { title: 'Throughput Mean (req/s)', side: 'right', overlaying: 'y', titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
                showlegend: true,
                legend: { x: 0, y: 1.12, orientation: 'h' },
            };

            if (hasItl) {
                var validItl = itlVals.filter(function(v) { return v != null; });
                var bestItlIdx = itlVals.indexOf(Math.min.apply(null, validItl));
                traces.push(
                    { x: labels, y: itlVals, type: 'scatter', mode: 'lines+markers', name: 'ITL ' + pLabel, yaxis: 'y3',
                      line: { color: '#ef4444', width: 2, shape: 'spline' },
                      marker: { color: '#ef4444', size: 8, symbol: 'square', line: { width: 1, color: 'white' } }, connectgaps: true },
                    { x: [labels[bestItlIdx]], y: [itlVals[bestItlIdx]], type: 'scatter', mode: 'markers', name: 'Best ITL', yaxis: 'y3',
                      marker: { color: '#10b981', size: 16, symbol: 'square', line: { width: 2, color: 'white' } }, showlegend: true }
                );
                layout.yaxis3 = { title: 'ITL ' + pLabel + ' (ms)', side: 'left', titlefont: { color: '#ef4444', size: 11 }, tickfont: { color: '#ef4444', size: 10 }, domain: [0, 0.22] };
            }

            Plotly.newPlot(el, traces, layout, CONFIG);
        });

        // Aggregated configurations chart (all percentiles in one grouped bar chart) — ported from charts.js
        var aggResults = coreResults.filter(function(r) { return r.architecture === 'AGGREGATED' && r.ttft_p90; });
        var aggEl = document.getElementById(ctx.chartId('chart-agg-ttft-all'));
        if (aggResults.length > 1 && aggEl) {
            var aggSorted = aggResults.slice().sort(function(a, b) { return (a.tp || 1) - (b.tp || 1); });
            var aggLabels = aggSorted.map(function(r) { return (r.replicas || Math.floor(r.gpus / (r.tp || 1))) + '×TP' + (r.tp || '?') + ((r.pp && r.pp > 1) ? ' PPx' + r.pp : ''); });
            var aggColors = { p90: '#3b82f6', p95: '#dc2626', p99: '#7c3aed' };
            var aggTraces = [];
            ['p90', 'p95', 'p99'].forEach(function(p) {
                aggTraces.push({
                    x: aggLabels,
                    y: aggSorted.map(function(r) { return r['ttft_' + p]; }),
                    name: 'TTFT ' + p.toUpperCase(),
                    type: 'bar',
                    marker: { color: aggColors[p], opacity: 0.85 },
                    hovertemplate: '%{x}<br>TTFT ' + p.toUpperCase() + ': %{y:.1f} ms<extra></extra>',
                });
            });
            var aggTputVals = aggSorted.map(function(r) { return r.throughput_mean || r.throughput_p90; });
            aggTraces.push({
                x: aggLabels, y: aggTputVals,
                name: 'Throughput Mean',
                type: 'scatter', mode: 'lines+markers', yaxis: 'y2',
                line: { color: '#f59e0b', width: 3, shape: 'spline' },
                marker: { color: '#f59e0b', size: 10, symbol: 'diamond', line: { width: 2, color: 'white' } },
                hovertemplate: 'Throughput Mean: %{y:.2f} req/s<extra></extra>',
            });
            var itlDashes = { p90: 'solid', p95: 'dash', p99: 'dot' };
            var itlSymbols = { p90: 'square', p95: 'square-open', p99: 'square-open-dot' };
            var hasAggItl = aggSorted.some(function(r) { return r.itl_p90 != null; });
            if (hasAggItl) {
                ['p90', 'p95', 'p99'].forEach(function(p) {
                    aggTraces.push({
                        x: aggLabels,
                        y: aggSorted.map(function(r) { return r['itl_' + p]; }),
                        name: 'ITL ' + p.toUpperCase(),
                        type: 'scatter', mode: 'lines+markers', yaxis: 'y3',
                        line: { color: '#ef4444', width: 2, dash: itlDashes[p] },
                        marker: { color: '#ef4444', size: 7, symbol: itlSymbols[p] },
                        hovertemplate: 'ITL ' + p.toUpperCase() + ': %{y:.2f} ms<extra></extra>',
                        connectgaps: true,
                    });
                });
            }
            var aggLayout = {
                height: 850,
                barmode: 'group',
                margin: { t: 30, b: 80, l: 60, r: hasAggItl ? 110 : 60 },
                xaxis: { title: 'Aggregated Configuration', domain: hasAggItl ? [0, 0.96] : [0, 1] },
                yaxis: { title: 'TTFT (ms) — lower is better', side: 'left', tickformat: '.2s' },
                yaxis2: { title: 'Throughput Mean (req/s)', side: 'right', overlaying: 'y', anchor: hasAggItl ? 'free' : 'x', position: hasAggItl ? 0.97 : 1, titlefont: { color: '#f59e0b' }, tickfont: { color: '#f59e0b' } },
                showlegend: true,
                legend: { x: 0, y: 1.18, orientation: 'h' },
                plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff'
            };
            if (hasAggItl) {
                aggLayout.yaxis3 = { title: 'ITL (ms)', side: 'right', overlaying: 'y', anchor: 'free', position: 1, titlefont: { color: '#ef4444', size: 11 }, tickfont: { color: '#ef4444', size: 10 } };
            }
            Plotly.newPlot(aggEl, aggTraces, aggLayout, CONFIG);
        }

        // vLLM Engine Metrics (ported from charts.js, uses flat fields)
        var engEl = document.getElementById('chart-cfg-engine' + (typeof _sfx !== 'undefined' ? _sfx : ''));
        if (engEl) {
            var engResults = (data.all_results || []).filter(function(r) {
                var tid = r.test_id || '';
                return tid.indexOf('step2-') !== 0 && tid.indexOf('step3-') !== 0 &&
                       tid.indexOf('step11-') !== 0 && tid.indexOf('step12-') !== 0 && tid.indexOf('step13-') !== 0;
            });
            if (engResults.length) {
                var labels = [], kvAvg = [], kvMax = [], runAvg = [], runMax = [], waitAvg = [], prefillT = [], decodeT = [];
                engResults.forEach(function(r) {
                    var m = r.metrics_json ? (typeof r.metrics_json === 'string' ? JSON.parse(r.metrics_json) : r.metrics_json) : {};
                    var pm = m.prometheus_metrics || {};
                    labels.push(r.config_name || '?');
                    var _rA = (pm.vllm_requests_running || {}).avg || r._avg_running || 0;
                    var _rM = (pm.vllm_requests_running || {}).max || r._avg_running || 0;
                    runAvg.push(_rA); runMax.push(_rM);
                    kvAvg.push((pm.vllm_kv_cache_pct || {}).avg != null ? (pm.vllm_kv_cache_pct.avg * 100) : (r._kv_cache_avg != null ? r._kv_cache_avg * 100 : 0));
                    kvMax.push((pm.vllm_kv_cache_pct || {}).max != null ? (pm.vllm_kv_cache_pct.max * 100) : (r._kv_cache_max != null ? r._kv_cache_max * 100 : 0));
                    waitAvg.push((pm.vllm_requests_waiting || {}).avg || r._avg_waiting || 0);
                    prefillT.push((pm.vllm_prefill_time_rate || {}).avg || r._prefill_time_rate || 0);
                    decodeT.push((pm.vllm_decode_time_rate || {}).avg || r._decode_time_rate || 0);
                });
                Plotly.newPlot(engEl, [
                    { x: labels, y: kvAvg, name: 'KV Cache Avg %', type: 'bar', marker: { color: '#3b82f6' } },
                    { x: labels, y: kvMax, name: 'KV Cache Max %', type: 'bar', marker: { color: '#93c5fd' } },
                    { x: labels, y: runAvg, name: 'Avg Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 3 }, marker: { size: 8 } },
                    { x: labels, y: runMax, name: 'Max Running Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#059669', width: 2, dash: 'dash' }, marker: { size: 6 } },
                    { x: labels, y: waitAvg, name: 'Avg Waiting Reqs', type: 'scatter', mode: 'lines+markers', yaxis: 'y2', line: { color: '#ef4444', width: 2, dash: 'dot' }, marker: { size: 6 } }
                ], {
                    barmode: 'group',
                    margin: { t: 40, b: 140, l: 60, r: 60 },
                    xaxis: { tickangle: -35 },
                    yaxis: { title: 'KV Cache %', side: 'left', rangemode: 'tozero' },
                    yaxis2: { title: 'Count', overlaying: 'y', side: 'right', rangemode: 'tozero' },
                    legend: { x: 0, y: 1.15, orientation: 'h' },
                    height: 500, plot_bgcolor: '#f8fafc', paper_bgcolor: '#fff'
                }, { responsive: true });
            }
        }
    }

    ReportTabs.register({
        id: 'configurations', label: 'Configurations', icon: '&#9776;',
        position: 'after:recommendation',
        build: build,
        render: function(data, ctx) {
            if (typeof Plotly === 'undefined') return;
            // New chart renders (scatter, efficiency, per-user, engine metrics)
            try { render(data, ctx); } catch (e) { console.warn('configurations chart render failed', e); }
            // Legacy queued renders from build (tables, EPP charts)
            _renders.forEach(function(fn) { try { fn(); } catch (e) { console.warn('configurations render failed', e); } });
        }
    });
})();
