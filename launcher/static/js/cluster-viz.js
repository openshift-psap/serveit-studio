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
            html += '<div class="viz-node-header">';
            html += '<span class="viz-node-status ' + statusClass + '"></span>';
            html += '<span class="viz-node-name">' + _shortName(node.name) + '</span>';
            html += '</div>';

            // GPU chips
            html += '<div class="viz-gpus">';
            for (var i = 0; i < node.gpus; i++) {
                html += '<div class="viz-gpu-chip" title="' + (node.gpu_model || 'GPU') + ' ' + vram + '">🟩</div>';
            }
            html += '</div>';

            html += '<div class="viz-node-info">';
            html += '<span>' + node.gpus + '× ' + (node.gpu_model || 'GPU') + '</span>';
            if (vram) html += '<span>' + vram + ' each</span>';
            html += '<span>' + node.cpu_cores + ' CPUs, ' + node.memory_gb + ' GB RAM</span>';
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
    if (name.length <= 20) return name;
    var parts = name.split('-');
    if (parts.length > 2) return parts[0] + '-…-' + parts[parts.length - 1];
    return name.substring(0, 18) + '…';
}

var _clusterScanCache = {};

function scanAndRenderCluster(clusterId, container, forceRescan) {
    if (!forceRescan && _clusterScanCache[clusterId]) {
        renderClusterDiagram(container, _clusterScanCache[clusterId]);
        return;
    }
    container.innerHTML = '<div style="text-align:center;padding:40px;color:#999"><div style="margin-bottom:12px">🔍</div>Scanning cluster resources…</div>';
    fetch('/api/clusters/' + clusterId + '/scan', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) {
            container.innerHTML = '<div style="text-align:center;padding:40px;color:#dc2626">Scan failed: ' + data.error + '</div>';
            return;
        }
        _clusterScanCache[clusterId] = data;
        renderClusterDiagram(container, data);
    })
    .catch(function(err) {
        container.innerHTML = '<div style="text-align:center;padding:40px;color:#dc2626">Scan error: ' + err + '</div>';
    });
}
