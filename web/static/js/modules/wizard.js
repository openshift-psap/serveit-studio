// wizard.js — Model selector, workload mode, multi-turn, stop mode, rate type


function renderModels(models) {
    const modelList = document.getElementById('model-list');
    models.forEach(model => {
        const card = document.createElement('div');
        card.className = 'model-card';
        card.dataset.model = model.id;
        card.dataset.category = model.category;
        card.innerHTML = `
            <div class="model-name">${model.name}</div>
            <div class="model-desc">${model.description}</div>
        `;
        if (config.model === model.id) {
            card.classList.add('selected');
        }
        card.addEventListener('click', function() {
            document.querySelectorAll('.model-card').forEach(c => c.classList.remove('selected'));
            this.classList.add('selected');
            config.model = this.dataset.model;
            saveConfig();
        });
        modelList.appendChild(card);
    });
}

var activeCategory = null;

function buildCategoryFilters() {
    const container = document.getElementById('model-category-filters');
    if (!container || !allModels.length) return;
    const cats = [...new Set(allModels.map(m => m.category))];
    const btnStyle = 'padding:6px 14px; border:2px solid #cbd5e1; border-radius:16px; background:white; color:#475569; font-weight:600; cursor:pointer; font-size:0.82em; font-family:inherit; transition:all 0.2s;';
    const activeStyle = 'padding:6px 14px; border:2px solid var(--rh-red-primary); border-radius:16px; background:var(--rh-red-primary); color:white; font-weight:600; cursor:pointer; font-size:0.82em; font-family:inherit; transition:all 0.2s;';

    container.innerHTML = '';
    const allBtn = document.createElement('button');
    allBtn.textContent = 'All';
    allBtn.style.cssText = activeCategory === null ? activeStyle : btnStyle;
    allBtn.onclick = () => { activeCategory = null; applyModelFilters(); buildCategoryFilters(); };
    container.appendChild(allBtn);

    cats.forEach(cat => {
        const btn = document.createElement('button');
        btn.textContent = cat;
        btn.style.cssText = activeCategory === cat ? activeStyle : btnStyle;
        btn.onclick = () => { activeCategory = cat; applyModelFilters(); buildCategoryFilters(); };
        container.appendChild(btn);
    });
}

function applyModelFilters() {
    const searchTerm = (document.getElementById('model-search').value || '').toLowerCase();
    let filtered = allModels;
    if (activeCategory) {
        filtered = filtered.filter(m => m.category === activeCategory);
    }
    if (searchTerm) {
        filtered = filtered.filter(m =>
            m.name.toLowerCase().includes(searchTerm) ||
            m.id.toLowerCase().includes(searchTerm) ||
            (m.description || '').toLowerCase().includes(searchTerm)
        );
    }
    const modelList = document.getElementById('model-list');
    modelList.innerHTML = '';
    renderModels(filtered);
}

// Fetch models on page load
fetch('/api/models')
    .then(response => response.json())
    .then(models => {
        allModels = models;
        buildCategoryFilters();
        renderModels(models);
    })
    .catch(error => {
        console.error('Failed to load models:', error);
        document.getElementById('model-list').innerHTML = '<div style="padding: 20px; text-align: center; color: #e53e3e;">Failed to load models. Please refresh the page.</div>';
    });

// Model search
document.getElementById('model-search').addEventListener('input', () => applyModelFilters());

// Custom model
document.getElementById('custom-model').addEventListener('input', (e) => {
    if (e.target.value) {
        document.querySelectorAll('.model-card').forEach(c => c.classList.remove('selected'));
        config.model = e.target.value;
        saveConfig();
    }
});

// HuggingFace token
document.getElementById('hf-token').addEventListener('input', (e) => {
    config.hf_token = e.target.value || null;
    saveConfig();
});

// Number inputs
['isl', 'osl', 'users', 'duration'].forEach(field => {
    document.getElementById(`${field}-input`).addEventListener('change', (e) => {
        config[field] = parseInt(e.target.value);
        saveConfig();
    });
});

// ISL/OSL stdev inputs
document.getElementById('isl-stdev-input').addEventListener('change', (e) => {
    config.isl_stdev = e.target.value ? parseInt(e.target.value) : null;
    saveConfig();
});
document.getElementById('osl-stdev-input').addEventListener('change', (e) => {
    config.osl_stdev = e.target.value ? parseInt(e.target.value) : null;
    saveConfig();
});

// Dataset input listeners
document.getElementById('dataset-source-input').addEventListener('change', (e) => {
    config.dataset_source = e.target.value || null;
    saveConfig();
});
document.getElementById('dataset-column-input').addEventListener('change', (e) => {
    config.dataset_column = e.target.value || null;
    saveConfig();
});
document.getElementById('dataset-max-output-input').addEventListener('change', (e) => {
    config.dataset_max_output = parseInt(e.target.value) || 256;
    saveConfig();
});

// Workload mode toggle
function toggleWorkloadMode() {
    var synPanel = document.getElementById('synthetic-workload-panel');
    var dsPanel = document.getElementById('dataset-workload-panel');
    var isDataset = synPanel.style.display !== 'none';
    var hiding = isDataset ? synPanel : dsPanel;
    var showing = isDataset ? dsPanel : synPanel;

    hiding.classList.remove('flipping-in');
    hiding.classList.add('flipping-out');

    setTimeout(function() {
        hiding.style.display = 'none';
        hiding.classList.remove('flipping-out');
        showing.style.display = 'block';
        showing.classList.add('flipping-in');
        setTimeout(function() { showing.classList.remove('flipping-in'); }, 350);
    }, 350);

    config.workload_mode = isDataset ? 'dataset' : 'synthetic';
    saveConfig();
}

function handleDatasetUpload(input) {
    if (!input.files || !input.files[0]) return;
    var file = input.files[0];
    var statusEl = document.getElementById('dataset-upload-status');
    statusEl.textContent = 'Uploading ' + file.name + '...';
    statusEl.style.display = 'block';

    var formData = new FormData();
    formData.append('file', file);
    fetch('/api/upload-dataset', { method: 'POST', body: formData })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                statusEl.textContent = '✅ ' + file.name + ' uploaded';
                statusEl.style.color = '#059669';
                document.getElementById('dataset-source-input').value = data.path;
            } else {
                statusEl.textContent = '❌ ' + (data.error || 'Upload failed');
                statusEl.style.color = '#dc2626';
            }
        })
        .catch(function(err) {
            statusEl.textContent = '❌ ' + err.message;
            statusEl.style.color = '#dc2626';
        });
}

function toggleMultiTurn() {
    const enabled = document.getElementById('multi-turn-enabled').checked;
    if (enabled) {
        config.turns = Math.max(2, parseInt(document.getElementById('turns-input').value) || 3);
    } else {
        config.turns = 1;
    }
    saveConfig();
}
var turnsInput = document.getElementById('turns-input');
if (turnsInput) {
    turnsInput.addEventListener('change', (e) => {
        config.turns = Math.max(2, parseInt(e.target.value) || 3);
        saveConfig();
    });
}

// Workload Type Selector (Continuous vs Multi-Turn)
var workloadType = config.workload_type || 'continuous';

function setWorkloadType(type) {
    workloadType = type;
    config.workload_type = type;
    saveConfig();

    const continuousBtn = document.getElementById('workload-type-continuous');
    const multiturnBtn = document.getElementById('workload-type-multiturn');
    const continuousSettings = document.getElementById('continuous-settings');
    const multiturnSettings = document.getElementById('multiturn-settings');

    if (type === 'continuous') {
        continuousBtn.style.background = '#3b82f6';
        continuousBtn.style.color = 'white';
        continuousBtn.style.borderColor = '#3b82f6';
        multiturnBtn.style.background = '#f3f4f6';
        multiturnBtn.style.color = '#1f2937';
        multiturnBtn.style.borderColor = '#cbd5e1';
        continuousSettings.style.display = 'block';
        multiturnSettings.style.display = 'none';
        // Sync inputs from multi-turn to continuous if needed
        var isl_mt = document.getElementById('isl-input-mt');
        var osl_mt = document.getElementById('osl-input-mt');
        if (isl_mt && osl_mt) {
            document.getElementById('isl-input').value = isl_mt.value || 3000;
            document.getElementById('osl-input').value = osl_mt.value || 100;
        }
    } else {
        multiturnBtn.style.background = '#3b82f6';
        multiturnBtn.style.color = 'white';
        multiturnBtn.style.borderColor = '#3b82f6';
        continuousBtn.style.background = '#f3f4f6';
        continuousBtn.style.color = '#1f2937';
        continuousBtn.style.borderColor = '#cbd5e1';
        continuousSettings.style.display = 'none';
        multiturnSettings.style.display = 'block';
        // Sync inputs from continuous to multi-turn if needed
        var isl = document.getElementById('isl-input');
        var osl = document.getElementById('osl-input');
        if (isl && osl) {
            document.getElementById('isl-input-mt').value = isl.value || 2000;
            document.getElementById('osl-input-mt').value = osl.value || 500;
        }
        // Ensure turns is set
        config.turns = Math.max(2, parseInt(document.getElementById('turns-input-mt').value) || 10);
    }
}

function workloadTypeHover(btn, type) {
    if (type === 'continuous' && workloadType === 'continuous') {
        btn.style.background = '#1d4ed8';
    } else if (type === 'multiturn' && workloadType === 'multiturn') {
        btn.style.background = '#1d4ed8';
    } else if (type === 'continuous' && workloadType !== 'continuous') {
        btn.style.background = '#e5e7eb';
    } else if (type === 'multiturn' && workloadType !== 'multiturn') {
        btn.style.background = '#e5e7eb';
    }
}

function workloadTypeOut(btn, type) {
    if (type === 'continuous' && workloadType === 'continuous') {
        btn.style.background = '#3b82f6';
    } else if (type === 'multiturn' && workloadType === 'multiturn') {
        btn.style.background = '#3b82f6';
    } else if (type === 'continuous' && workloadType !== 'continuous') {
        btn.style.background = '#f3f4f6';
    } else if (type === 'multiturn' && workloadType !== 'multiturn') {
        btn.style.background = '#f3f4f6';
    }
}

// Initialize workload type button styles on page load
document.addEventListener('DOMContentLoaded', function() {
    setTimeout(function() {
        setWorkloadType(workloadType);
    }, 100);
});

// Add event listeners for multi-turn inputs
['isl', 'osl'].forEach(field => {
    var el = document.getElementById(`${field}-input-mt`);
    if (el) {
        el.addEventListener('change', (e) => {
            config[field] = parseInt(e.target.value);
            saveConfig();
        });
    }
});

// Multi-turn stdev listeners
document.getElementById('isl-stdev-input-mt').addEventListener('change', (e) => {
    config.isl_stdev = e.target.value ? parseInt(e.target.value) : null;
    saveConfig();
});
document.getElementById('osl-stdev-input-mt').addEventListener('change', (e) => {
    config.osl_stdev = e.target.value ? parseInt(e.target.value) : null;
    saveConfig();
});

var turnsInputMt = document.getElementById('turns-input-mt');
if (turnsInputMt) {
    turnsInputMt.addEventListener('change', (e) => {
        config.turns = Math.max(2, parseInt(e.target.value) || 10);
        saveConfig();
    });
}

// Shared Prefix Percentage - Continuous Workload
function updateSharedPrefixPercentage(pct) {
    var isl = parseInt(document.getElementById('isl-input').value) || 3000;
    var prefixTokens = Math.floor(isl * pct / 100);
    config.prefix_tokens = prefixTokens > 0 ? prefixTokens : null;
    document.getElementById('prefix-pct-value').textContent = pct + '%';
    document.getElementById('prefix-tokens-display').textContent = 'Calculated: ' + prefixTokens + ' tokens of ' + isl;
    saveConfig();
}

// Shared Prefix Percentage - Multi-Turn Workload
function updateSharedPrefixPercentageMT(pct) {
    var isl = parseInt(document.getElementById('isl-input-mt').value) || 2000;
    var prefixTokens = Math.floor(isl * pct / 100);
    config.prefix_tokens = prefixTokens > 0 ? prefixTokens : null;
    document.getElementById('prefix-pct-value-mt').textContent = pct + '%';
    document.getElementById('prefix-tokens-display-mt').textContent = 'Calculated: ' + prefixTokens + ' tokens of ' + isl;
    saveConfig();
}

// Update shared prefix display when ISL changes
document.getElementById('isl-input').addEventListener('change', function() {
    var pct = parseInt(document.getElementById('prefix-pct-slider').value) || 0;
    if (pct > 0) updateSharedPrefixPercentage(pct);
});

document.getElementById('isl-input-mt').addEventListener('change', function() {
    var pct = parseInt(document.getElementById('prefix-pct-slider-mt').value) || 0;
    if (pct > 0) updateSharedPrefixPercentageMT(pct);
});

// Unique prefixes slider - Continuous
function updatePrefixCount(val) {
    config.prefix_count = parseInt(val);
    document.getElementById('prefix-count-value').textContent = val;
    saveConfig();
}

// Unique prefixes slider - Multi-Turn
function updatePrefixCountMT(val) {
    config.prefix_count = parseInt(val);
    document.getElementById('prefix-count-value-mt').textContent = val;
    saveConfig();
}
