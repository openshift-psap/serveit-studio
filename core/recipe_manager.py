"""Tested recipe library.

Recipes are validated configuration bundles — model + goal + workload +
advanced settings + the evidence they produced — stored as YAML files in
the ``recipes/`` directory. Files are the source of truth; the ``recipes``
database table is a queryable index plus usage tracking (times applied,
last applied).

A recipe's ``requires`` block is evaluated against the latest cluster
hardware scan: recipes that don't fit the current cluster are shown as
unavailable with the reason, instead of letting users apply a
configuration that cannot work (e.g. DPEP on a cluster without RDMA).

Applying a recipe goes through the same session-config path as the
resume overlay's "Apply Settings": the client merges the config payload
and re-renders the wizard.
"""

import hashlib
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

RECIPES_DIR = os.environ.get('RECIPES_DIR') or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'recipes')

REQUIRED_FIELDS = ('recipe_id', 'name', 'goal', 'config')


def _recipe_files() -> List[str]:
    if not os.path.isdir(RECIPES_DIR):
        return []
    return sorted(
        os.path.join(RECIPES_DIR, f)
        for f in os.listdir(RECIPES_DIR)
        if f.endswith(('.yaml', '.yml', '.json'))
    )


def load_recipe_file(path: str) -> Optional[Dict]:
    """Load and validate one recipe file. Returns None on invalid."""
    try:
        with open(path) as f:
            if path.endswith('.json'):
                recipe = json.load(f)
            else:
                recipe = yaml.safe_load(f)
        if not isinstance(recipe, dict):
            raise ValueError('not a mapping')
        missing = [k for k in REQUIRED_FIELDS if not recipe.get(k)]
        if missing:
            raise ValueError(f'missing fields: {missing}')
        if not isinstance(recipe['config'], dict):
            raise ValueError('config must be a mapping')
        return recipe
    except Exception as e:
        logger.warning(f'Invalid recipe file {path}: {e}')
        return None


def scan_recipes() -> List[Dict]:
    """Load all valid recipes from the recipes directory."""
    recipes = []
    for path in _recipe_files():
        recipe = load_recipe_file(path)
        if recipe:
            recipe['_file'] = os.path.basename(path)
            recipe['_mtime'] = os.path.getmtime(path)
            recipes.append(recipe)
    return recipes


def check_availability(recipe: Dict, hw: Optional[Dict]) -> Tuple[bool, List[str]]:
    """Evaluate a recipe's ``requires`` against a hardware scan dict.

    ``hw`` is the latest hardware scan summary (total_gpus, has_rdma,
    gpu_memory_per_gpu_mb, gpu_model) or None when no scan exists —
    in which case only file-level requirements can be checked.
    """
    requires = recipe.get('requires') or {}
    reasons = []

    min_gpus = requires.get('min_gpus')
    if min_gpus and (not hw or (hw.get('total_gpus') or 0) < min_gpus):
        have = hw.get('total_gpus') if hw else '?'
        reasons.append(f'requires ≥{min_gpus} GPUs (cluster has {have})')

    min_vram = requires.get('gpu_memory_gb')
    if min_vram:
        have_mb = (hw or {}).get('gpu_memory_per_gpu_mb') or 0
        if not hw or have_mb < min_vram * 1024:
            reasons.append(f'requires ≥{min_vram}GB GPU memory '
                           f'(cluster has {round(have_mb / 1024) if have_mb else "?"}GB)')

    network = requires.get('network') or []
    if 'rdma' in network and (not hw or not hw.get('has_rdma')):
        reasons.append('requires RDMA networking')

    if requires.get('gpu_model') and hw:
        want = requires['gpu_model'].lower()
        have = (hw.get('gpu_model') or '').lower()
        if want not in have:
            reasons.append(f"requires {requires['gpu_model']} (cluster has {hw.get('gpu_model') or 'unknown'})")

    return (not reasons, reasons)


def get_latest_hw_scan(conn) -> Optional[Dict]:
    """Latest hardware scan summary from the DB, if any."""
    try:
        row = conn.execute('''
            SELECT total_gpus, has_rdma, gpu_memory_per_gpu_mb, gpu_model
            FROM hardware_scans ORDER BY id DESC LIMIT 1
        ''').fetchone()
        if not row:
            return None
        return {
            'total_gpus': row[0], 'has_rdma': bool(row[1]),
            'gpu_memory_per_gpu_mb': row[2] or 0, 'gpu_model': row[3] or '',
        }
    except Exception:
        return None


def list_recipes_with_availability(conn) -> List[Dict]:
    """All recipes from the directory + availability, for the UI.

    Pure file scan — the directory is the only source of truth. ``conn``
    is used solely to read the latest hardware scan for gating.
    """
    hw = get_latest_hw_scan(conn)
    out = []
    for recipe in scan_recipes():
        requires = recipe.get('requires') or {}
        entry = {
            'recipe_id': recipe['recipe_id'], 'name': recipe['name'],
            'description': recipe.get('description', ''),
            'model': recipe.get('model') or recipe['config'].get('model'),
            'goal': recipe['goal'], 'min_gpus': requires.get('min_gpus'),
            'evidence': recipe.get('evidence') or {},
        }
        available, reasons = check_availability(
            {**entry, 'requires': requires}, hw)
        entry['available'] = available
        entry['unavailable_reasons'] = reasons
        entry['hw_scan_present'] = hw is not None
        out.append(entry)
    out.sort(key=lambda r: r['name'])
    return out


def apply_recipe(conn, recipe_id: str) -> Optional[Dict]:
    """Fetch a recipe's config payload straight from its file.

    The recipes directory is the only source of truth — no DB read, no
    usage counters.
    """
    for path in _recipe_files():
        recipe = load_recipe_file(path)
        if recipe and recipe.get('recipe_id') == recipe_id:
            return normalize_config_payload(recipe['config'])
    return None


def normalize_config_payload(data: Dict) -> Dict:
    """Normalize a config payload for the wizard session.

    Run configs and recipe files may use server-side key spellings
    (model_name) or carry server-internal keys — the wizard expects
    ``model`` and clean keys.
    """
    data = dict(data)
    if 'model_name' in data and 'model' not in data:
        data['model'] = data.pop('model_name')
    if 'objective' in data and 'goal' not in data:
        data['goal'] = data.pop('objective')
    if 'qps' in data and 'users' not in data:
        data['users'] = data.pop('qps')
    for k in ('namespace', 'run_id'):
        data.pop(k, None)
    return {k: v for k, v in data.items() if not k.startswith('_')}


def import_recipe_file(file_storage) -> Dict:
    """Validate an uploaded config file (JSON or YAML).

    Two accepted shapes:
    - a bare wizard config mapping (what Apply Settings consumes), or
    - a full recipe (with recipe_id/name/goal/config) — the config is used.
    Returns the config payload; raises ValueError with a reason on bad input.
    """
    raw = file_storage.read()
    if not raw:
        raise ValueError('empty file')
    try:
        data = json.loads(raw)
    except Exception:
        try:
            data = yaml.safe_load(raw)
        except Exception as e:
            raise ValueError(f'not valid JSON or YAML: {str(e)[:100]}')
    if not isinstance(data, dict):
        raise ValueError('file must contain a settings mapping')
    if 'config' in data and isinstance(data['config'], dict) and data.get('recipe_id'):
        data = data['config']
    if not data.get('model') and not data.get('model_name'):
        raise ValueError('settings missing "model" — not a wizard config?')
    return normalize_config_payload(data)


def recipe_file_digest(path: str) -> str:
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]
