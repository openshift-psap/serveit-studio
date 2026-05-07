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
    displayedModels += models.length;

    // Show/hide "Load More" button
    const loadMoreBtn = document.getElementById('load-more-models');
    if (displayedModels < allModels.length) {
        loadMoreBtn.style.display = 'inline-block';
    } else {
        loadMoreBtn.style.display = 'none';
    }
}

function loadMoreModels() {
    const nextBatch = allModels.slice(displayedModels, displayedModels + modelsPerPage);
    renderModels(nextBatch);
}

let activeCategory = null;

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
    displayedModels = 0;
    renderModels(filtered.slice(0, modelsPerPage));
    if (!searchTerm && !activeCategory && filtered.length > modelsPerPage) {
        document.getElementById('load-more-models').style.display = 'inline-block';
    } else {
        document.getElementById('load-more-models').style.display = 'none';
    }
}

// Fetch models on page load
fetch('/api/models')
    .then(response => response.json())
    .then(models => {
        allModels = models;
        buildCategoryFilters();
        renderModels(models.slice(0, modelsPerPage));
    })
    .catch(error => {
        console.error('Failed to load models:', error);
        document.getElementById('model-list').innerHTML = '<div style="padding: 20px; text-align: center; color: #e53e3e;">Failed to load models. Please refresh the page.</div>';
    });

// Load More button
document.getElementById('load-more-models').addEventListener('click', loadMoreModels);

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
const turnsInput = document.getElementById('turns-input');
if (turnsInput) {
    turnsInput.addEventListener('change', (e) => {
