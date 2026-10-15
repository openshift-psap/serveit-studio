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
    const goalColors = {
        ttft: '#2563eb', throughput: '#059669', balanced: '#4f46e5',
        pd_only: '#7c3aed', ep_only: '#0891b2', dpep_only: '#be185d',
        aggregated_only: '#d97706', single_test: '#ea580c',
    };
    let html = '';
    recipes.forEach(r => {
        const gc = goalColors[r.goal] || '#64748b';
        const avail = r.available;
        const headerBg = avail
            ? `background:linear-gradient(135deg,${gc},${gc}cc);`
            : 'background:linear-gradient(135deg,#94a3b8,#64748b);';
        const badge = (txt, bg, fg) =>
            `<span style="background:${bg};color:${fg};border-radius:4px;padding:2px 8px;font-size:0.78em;font-weight:600;white-space:nowrap;">${txt}</span>`;
        const ev = r.evidence || {};
        const evBits = [];
        if (ev.tested_on) evBits.push(`Tested on ${ev.tested_on}`);
        if (ev.results) evBits.push(typeof ev.results === 'object'
            ? Object.entries(ev.results).map(([k, v]) => `${k}: ${v}`).join(' · ') : ev.results);
        if (ev.source) evBits.push(ev.source);
        const usage = r.times_applied
            ? `applied ${r.times_applied}×` + (r.last_applied_at ? ` · last ${r.last_applied_at.slice(0, 10)}` : '')
            : 'never applied';
        html += `
        <div style="background:white;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.08);margin-bottom:14px;overflow:hidden;${avail ? `border-left:6px solid ${gc};` : 'border-left:6px solid #94a3b8;opacity:0.85;'}">
            <div style="${headerBg}padding:12px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
                <div style="color:white;">
                    <div style="font-weight:700;font-size:1.02em;">${r.name}</div>
                    <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;">
                        ${badge(r.model || '?', 'rgba(255,255,255,0.2)', 'white')}
                        ${badge(r.goal, 'rgba(255,255,255,0.2)', 'white')}
                        ${r.min_gpus ? badge(`≥${r.min_gpus} GPUs`, 'rgba(255,255,255,0.2)', 'white') : ''}
                    </div>
                </div>
                <button class="apply-recipe-btn" data-recipe-id="${r.recipe_id}" ${avail ? '' : 'disabled title="Cluster does not meet requirements"'}
                    style="font-size:0.9em;padding:8px 22px;background:${avail ? 'rgba(255,255,255,0.95)' : 'rgba(255,255,255,0.5)'};color:${avail ? gc : '#94a3b8'};border:none;border-radius:6px;cursor:${avail ? 'pointer' : 'not-allowed'};font-weight:700;white-space:nowrap;box-shadow:${avail ? '0 2px 6px rgba(0,0,0,0.15)' : 'none'};">
                    ${avail ? '⚙️ Apply' : '✗ Unavailable'}
                </button>
            </div>
            <div style="padding:14px 18px;color:#475569;font-size:0.9em;line-height:1.6;">
                ${r.description || ''}
                ${(r.unavailable_reasons || []).map(x => `<div style="color:#dc2626;font-size:0.85em;margin-top:4px;">✗ ${x}</div>`).join('')}
                ${evBits.length ? `<div style="color:#059669;font-size:0.83em;margin-top:8px;border-top:1px dashed #e2e8f0;padding-top:8px;">🔬 ${evBits.join(' · ')}</div>` : ''}
                <div style="color:#94a3b8;font-size:0.78em;margin-top:6px;text-align:right;">${usage}</div>
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
