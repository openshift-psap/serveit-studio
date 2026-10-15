// recipes.js — Tested recipe library overlay
function loadRecipes() {
    const content = document.getElementById('recipes-content');
    content.innerHTML = '<div class="resume-empty">Loading recipes...</div>';
    fetch('/api/recipes')
        .then(r => r.json())
        .then(data => {
            if (data.error) throw new Error(data.error);
            allRecipes = data.recipes || [];
            filterRecipes();
        })
        .catch(err => {
            content.innerHTML = `<div class="resume-empty">Failed to load recipes: ${err.message}</div>`;
        });
}

let allRecipes = [];

function filterRecipes() {
    const q = (document.getElementById('recipe-search').value || '').toLowerCase().trim();
    let list = allRecipes.filter(r =>
        !q || ((r.name || '') + ' ' + (r.model || '') + ' ' + (r.description || '')).toLowerCase().includes(q));
    list.sort((a, b) => a.name.localeCompare(b.name));
    renderRecipes(list);
}

function renderRecipes(recipes) {
    const content = document.getElementById('recipes-content');
    if (!recipes.length) {
        content.innerHTML = `<div class="resume-empty">${allRecipes.length ? 'No recipes match your search.' : 'No recipes found. Add YAML files to the recipes/ directory or load one from a file.'}</div>`;
        return;
    }
    let html = '';
    recipes.forEach(r => {
        const avail = r.available
            ? '<span style="background:#f0fdf4;color:#16a34a;border:1px solid #bbf7d0;border-radius:4px;padding:1px 8px;font-size:0.85em;">✓ Available</span>'
            : '<span style="background:#fef2f2;color:#dc2626;border:1px solid #fecaca;border-radius:4px;padding:1px 8px;font-size:0.85em;">✗ Unavailable</span>';
        const reasons = (r.unavailable_reasons || [])
            .map(x => `<div style="color:#dc2626;font-size:0.85em;">• ${x}</div>`).join('');
        const ev = r.evidence || {};
        const evBits = [];
        if (ev.tested_on) evBits.push(`Tested: ${ev.tested_on}`);
        if (ev.source) evBits.push(`Source: ${ev.source}`);
        if (ev.results) evBits.push(`Results: ${JSON.stringify(ev.results)}`);
        html += `
        <div style="border:1px solid #e2e8f0;border-radius:10px;padding:16px 18px;margin-bottom:12px;background:${r.available ? '#ffffff' : '#fafafa'};">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;">
                <div style="flex:1;">
                    <div style="font-weight:700;font-size:1.05em;color:#1e293b;">${r.name}</div>
                    <div style="color:#64748b;font-size:0.88em;margin-top:4px;line-height:1.5;">${r.description || ''}</div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
                        <span style="background:#eff6ff;color:#1d4ed8;border-radius:4px;padding:1px 8px;font-size:0.82em;">${r.model || '?'}</span>
                        <span style="background:#f5f3ff;color:#6d28d9;border-radius:4px;padding:1px 8px;font-size:0.82em;">goal: ${r.goal}</span>
                        ${r.min_gpus ? `<span style="background:#fff7ed;color:#c2410c;border-radius:4px;padding:1px 8px;font-size:0.82em;">≥${r.min_gpus} GPUs</span>` : ''}
                    </div>
                    ${evBits.length ? `<div style="color:#059669;font-size:0.82em;margin-top:6px;">${evBits.join(' · ')}</div>` : ''}
                    ${reasons}
                </div>
                <button class="apply-recipe-btn" data-recipe-id="${r.recipe_id}" ${r.available ? '' : 'disabled title="Cluster does not meet requirements"'}
                    style="font-size:0.9em;padding:6px 16px;background:${r.available ? '#f0fdf4' : '#f5f5f5'};color:${r.available ? '#16a34a' : '#94a3b8'};border:1px solid ${r.available ? '#bbf7d0' : '#e2e8f0'};border-radius:6px;cursor:${r.available ? 'pointer' : 'not-allowed'};white-space:nowrap;">
                    ⚙️ Apply
                </button>
            </div>
        </div>`;
    });
    content.innerHTML = html;
    content.querySelectorAll('.apply-recipe-btn').forEach(btn => {
        btn.addEventListener('click', () => applyRecipe(btn.dataset.recipeId, btn));
    });
}

function applyRecipe(recipeId, btn) {
    if (btn) { btn.textContent = '⏳ Applying…'; btn.disabled = true; }
    fetch(`/api/recipes/apply/${encodeURIComponent(recipeId)}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.error) throw new Error(data.error);
            applyLoadedConfig(data.config, `🧪 Applied recipe "${recipeId}" — review and start when ready`);
        })
        .catch(err => {
            logToConsole(`Failed to apply recipe: ${err.message}`, 'error');
            if (btn) { btn.textContent = '⚙️ Apply'; btn.disabled = false; }
        });
}

// Shared apply path — also used by file import
function applyLoadedConfig(loadedConfig, successMsg) {
    window._serverConfigReceived = true;
    // Reset advanced-vLLM + preset-driven sections to defaults BEFORE the
    // merge: older templates predate current options, and absent fields
    // must fall back to defaults — not keep stale values from whatever was
    // applied earlier in this session.
    if (typeof resetAdvVllm === 'function') resetAdvVllm();
    ['speculative_preset', 'attention_preset', 'ctx_parallel_preset', 'kv_offload_preset']
        .forEach(function(k) { if (!(k in loadedConfig)) config[k] = 'off'; });
    ['lora_enabled', 'lora_sweep_enabled']
        .forEach(function(k) { if (!(k in loadedConfig)) config[k] = false; });
    ['lora_num_adapters', 'lora_max_loras', 'lora_max_rank',
     'lora_adapter_size_mb', 'lora_sweep_levels', 'lora_sweep_count']
        .forEach(function(k) { if (!(k in loadedConfig)) config[k] = null; });
    config = Object.assign({}, config, loadedConfig);
    document.getElementById('recipes-overlay').classList.remove('active');
    goToStep(3, true);
    if (typeof updateUIFromConfig === 'function') updateUIFromConfig();
    saveConfig();
    logToConsole(successMsg, 'success');
}

document.getElementById('recipes-indicator').addEventListener('click', () => {
    document.getElementById('recipes-overlay').classList.add('active');
    loadRecipes();
});
document.getElementById('recipes-close').addEventListener('click', () => {
    document.getElementById('recipes-overlay').classList.remove('active');
});
document.getElementById('recipes-overlay').addEventListener('click', (e) => {
    if (e.target.id === 'recipes-overlay') {
        document.getElementById('recipes-overlay').classList.remove('active');
    }
});
document.getElementById('recipe-import-input').addEventListener('change', function () {
    if (!this.files || !this.files[0]) return;
    const file = this.files[0];
    const fd = new FormData();
    fd.append('file', file);
    fetch('/api/recipes/import', { method: 'POST', body: fd })
        .then(r => r.json())
        .then(data => {
            if (data.error) throw new Error(data.error);
            applyLoadedConfig(data.config, `📂 Loaded settings from ${file.name} — review and start when ready`);
        })
        .catch(err => logToConsole(`Failed to load ${file.name}: ${err.message}`, 'error'))
        .finally(() => { this.value = ''; });
});
