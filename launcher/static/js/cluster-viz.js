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

    // Show warning if scan had limited permissions
    if (data.scan_warning) {
        container.innerHTML = '<div style="padding:24px;text-align:center;">' +
            '<div style="background:#fffbeb;border:1.5px solid #f59e0b;border-radius:10px;padding:16px 20px;margin-bottom:16px;text-align:left;">' +
            '<div style="font-weight:700;color:#92400e;font-size:0.95em;margin-bottom:4px;">⚠️ Limited Cluster Access</div>' +
            '<div style="color:#78350f;font-size:0.85em;line-height:1.6;">Could not scan cluster resources — the service account may lack node-level permissions. ' +
            'This is normal for launcher-only clusters without GPUs. Instance creation and remote cluster management will still work.</div>' +
            '</div>' +
            '<div style="color:#999;font-size:0.85em;">Tip: Add remote clusters with GPU access to run optimizations.</div>' +
            '</div>';
        return;
    }

    if (s.total_gpus === 0 && data.nodes.length === 0) {
        container.innerHTML = '<div style="padding:24px;text-align:center;">' +
            '<div style="color:#999;font-size:0.95em;">No GPU nodes detected on this cluster.</div>' +
            '<div style="color:#999;font-size:0.85em;margin-top:8px;">Add a remote cluster with GPUs to run optimizations.</div>' +
            '</div>';
        return;
    }

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

    // Infrastructure component versions
    if (data.infra_versions && Object.keys(data.infra_versions).length > 0) {
        var iv = data.infra_versions;
        var versionLabels = {
            openshift: 'OpenShift', k8s: 'Kubernetes',
            gpu_operator: 'GPU Operator', gpu_driver: 'GPU Driver', cuda_runtime: 'CUDA Runtime',
            network_operator: 'Network Operator', mofed: 'MOFED/DOCA',
            istio: 'Istio', service_mesh: 'Service Mesh', epp: 'EPP Scheduler',
            nfd: 'NFD', lws: 'LWS'
        };
        var versionOrder = ['openshift', 'k8s', 'gpu_operator', 'gpu_driver', 'cuda_runtime', 'network_operator', 'mofed', 'istio', 'service_mesh', 'epp', 'nfd', 'lws'];
        html += '<div style="background:#f0fdf4;border:1.5px solid #86efac;border-radius:10px;padding:14px 18px;margin-bottom:16px;">';
        html += '<div style="font-weight:700;color:#065f46;font-size:0.9em;margin-bottom:8px;">Component Versions</div>';
        html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:4px 24px;">';
        versionOrder.forEach(function(k) {
            if (iv[k]) {
                var label = versionLabels[k] || k;
                html += '<div style="font-size:0.82em;line-height:1.8;"><span style="color:#64748b;">' + label + ':</span> <span style="font-family:monospace;color:#065f46;">' + iv[k] + '</span></div>';
            }
        });
        // Any keys not in versionOrder
        Object.keys(iv).forEach(function(k) {
            if (versionOrder.indexOf(k) === -1) {
                html += '<div style="font-size:0.82em;line-height:1.8;"><span style="color:#64748b;">' + k + ':</span> <span style="font-family:monospace;color:#065f46;">' + iv[k] + '</span></div>';
            }
        });
        html += '</div></div>';
    }

    // Infrastructure warnings (missing LWS, Istio, etc.)
    if (data.infra_warnings && data.infra_warnings.length > 0) {
        html += '<div style="background:#fef2f2;border:1.5px solid #dc2626;border-radius:10px;padding:14px 18px;margin-bottom:16px;">';
        html += '<div style="font-weight:700;color:#dc2626;font-size:0.9em;margin-bottom:6px;">⚠️ Missing Infrastructure</div>';
        data.infra_warnings.forEach(function(w) {
            html += '<div style="color:#7f1d1d;font-size:0.82em;line-height:1.6;padding:2px 0;">• ' + w + '</div>';
        });
        html += '</div>';
    }

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
                html += '<div class="viz-gpu-slot" title="' + (node.gpu_model || 'GPU') + ' #' + (i+1) + ' ' + vram + '"></div>';
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
