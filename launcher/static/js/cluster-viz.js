/**
 * Cluster Visualization — renders a visual diagram of cluster nodes and GPUs.
 * Separate module for modularity.
 */

function renderClusterDiagram(container, data) {
    if (!data || !data.nodes) {
        container.innerHTML = '<div style="text-align:center;padding:40px;color:#999">No cluster data available</div>';
        return;
    }

    var s = data.summary;
    var gpuNodes = data.nodes.filter(function(n) { return n.gpus > 0; });
    var nonGpuNodes = data.nodes.filter(function(n) { return n.gpus === 0; });

    var html = '';

    // Summary bar
    html += '<div class="viz-summary">';
    html += '<div class="viz-stat"><div class="viz-stat-value">' + s.total_gpus + '</div><div class="viz-stat-label">Total GPUs</div></div>';
    var inUse = s.gpus_in_use || 0;
    var avail = s.gpus_available != null ? s.gpus_available : s.total_gpus;
    var usageColor = inUse > 0 ? (avail > 0 ? '#F0AB00' : '#dc2626') : '#3BAA3B';
    html += '<div class="viz-stat"><div class="viz-stat-value" style="color:' + usageColor + '">' + avail + ' / ' + s.total_gpus + '</div><div class="viz-stat-label">Available GPUs</div></div>';
    html += '<div class="viz-stat"><div class="viz-stat-value">' + s.gpu_node_count + '</div><div class="viz-stat-label">GPU Nodes</div></div>';
    html += '<div class="viz-stat"><div class="viz-stat-value">' + (s.gpu_model || 'N/A') + '</div><div class="viz-stat-label">GPU Model</div></div>';
    html += '<div class="viz-stat"><div class="viz-stat-value">' + Math.round((s.gpu_memory_per_gpu_mb || 0) / 1024) + ' GB</div><div class="viz-stat-label">VRAM / GPU</div></div>';
    html += '<div class="viz-stat"><div class="viz-stat-value">' + (s.has_rdma ? 'Yes' : 'No') + '</div><div class="viz-stat-label">RDMA</div></div>';
    html += '<div class="viz-stat"><div class="viz-stat-value">' + (s.cloud_provider || 'unknown') + '</div><div class="viz-stat-label">Provider</div></div>';
    html += '</div>';

    // GPU Nodes grid
    if (gpuNodes.length > 0) {
        html += '<div class="viz-section-title">GPU Nodes</div>';
        html += '<div class="viz-nodes">';
        gpuNodes.forEach(function(node) {
            var statusClass = node.status === 'Ready' ? 'viz-status-ready' : (node.status === 'NotReady' ? 'viz-status-notready' : 'viz-status-unknown');
            var vram = node.gpu_memory_gb ? node.gpu_memory_gb + ' GB' : '';

            html += '<div class="viz-node">';
            // Server chassis top — status LED + name
            html += '<div class="viz-server-top">';
            html += '<span class="viz-led ' + statusClass + '"></span>';
            html += '<span class="viz-led ' + (node.status === 'Ready' ? 'viz-led-activity' : statusClass) + '"></span>';
            html += '<span class="viz-node-name">' + _shortName(node.name) + '</span>';
            html += '</div>';

            // Server chassis body — GPU slots
            html += '<div class="viz-server-body">';
            html += '<div class="viz-gpu-bay">';
            for (var i = 0; i < node.gpus; i++) {
                html += '<div class="viz-gpu-slot" title="' + (node.gpu_model || 'GPU') + ' ' + vram + '">';
                html += '<svg width="14" height="14" viewBox="0 0 24 24" fill="#2A7B88" opacity="0.85"><rect x="2" y="4" width="20" height="16" rx="2"/><rect x="5" y="7" width="4" height="4" rx="0.5" fill="#1B5E6B"/><rect x="10" y="7" width="4" height="4" rx="0.5" fill="#1B5E6B"/><rect x="15" y="7" width="4" height="4" rx="0.5" fill="#1B5E6B"/><rect x="5" y="14" width="14" height="2" rx="0.5" fill="#1B5E6B"/></svg>';
                html += '</div>';
            }
            html += '</div>';
            html += '<div class="viz-server-specs">';
            html += '<span>' + node.gpus + '× ' + (node.gpu_model || 'GPU') + '</span>';
            if (vram) html += '<span>' + vram + ' ea</span>';
            html += '</div>';
            html += '</div>';

            // Server chassis bottom — CPU/RAM/RDMA
            html += '<div class="viz-server-bottom">';
            html += '<span>' + node.cpu_cores + ' CPUs</span>';
            html += '<span>' + node.memory_gb + ' GB</span>';
            if (node.has_rdma) html += '<span class="viz-rdma-badge">RDMA</span>';
            html += '</div>';
            html += '</div>';
        });
        html += '</div>';
    }

    // Non-GPU nodes (compact)
    if (nonGpuNodes.length > 0) {
        html += '<div class="viz-section-title" style="margin-top:16px">Other Nodes (' + nonGpuNodes.length + ')</div>';
        html += '<div class="viz-other-nodes">';
        nonGpuNodes.forEach(function(node) {
            var statusClass = node.status === 'Ready' ? 'viz-status-ready' : 'viz-status-notready';
            html += '<span class="viz-other-node ' + statusClass + '" title="' + node.name + ' — ' + node.cpu_cores + ' CPUs, ' + node.memory_gb + ' GB RAM">';
            html += _shortName(node.name);
            html += '</span>';
        });
        html += '</div>';
    }

    container.innerHTML = html;
}

function _shortName(name) {
    return name;
}

function _handleScanResponse(r) {
    if (r.redirected || r.status === 302 || r.status === 401 || r.status === 403) {
        return Promise.reject('session_expired');
    }
    var ct = r.headers.get('content-type') || '';
    if (ct.indexOf('application/json') === -1) {
        return Promise.reject('session_expired');
    }
    return r.json();
}

function _showSessionExpired(container) {
    container.innerHTML = '<div style="text-align:center;padding:40px;"><div style="font-size:2em;margin-bottom:12px">🔒</div><div style="color:#4A4A4A;font-weight:600;margin-bottom:8px">Session expired</div><div style="color:#999;font-size:0.9em">Please <a href="/logout" style="color:#2A7B88;font-weight:600">log in again</a> to continue.</div></div>';
}

function scanAndRenderCluster(clusterId, container, forceRescan) {
    if (forceRescan) {
        container.innerHTML = '<div style="text-align:center;padding:40px;color:#999"><div style="margin-bottom:12px">🔍</div>Scanning cluster resources…</div>';
        fetch('/api/clusters/' + clusterId + '/scan', { method: 'POST' })
        .then(_handleScanResponse)
        .then(function(data) {
            if (data.error) { container.innerHTML = '<div style="text-align:center;padding:40px;color:#dc2626">Scan failed: ' + data.error + '</div>'; return; }
            renderClusterDiagram(container, data);
        })
        .catch(function(err) {
            if (err === 'session_expired') { _showSessionExpired(container); return; }
            container.innerHTML = '<div style="text-align:center;padding:40px;color:#dc2626">Scan error: ' + err + '</div>';
        });
        return;
    }
    // Try cached from DB first
    fetch('/api/clusters/' + clusterId + '/scan')
    .then(_handleScanResponse)
    .then(function(data) {
        if (data.not_scanned || data.error) {
            // No cached data — trigger a fresh scan
            scanAndRenderCluster(clusterId, container, true);
            return;
        }
        renderClusterDiagram(container, data);
    })
    .catch(function(err) {
        if (err === 'session_expired') { _showSessionExpired(container); return; }
        // GET failed — trigger a fresh scan
        scanAndRenderCluster(clusterId, container, true);
    });
}
