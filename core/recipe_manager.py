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
from datetime import datetime
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


def sync_recipes_db(conn) -> int:
    """Upsert the recipes directory into the DB index.

    Files are the source of truth: rows are inserted/updated to match,
    rows for deleted files are removed. Usage counters are preserved.
    Returns the number of indexed recipes.
    """
    conn.execute('''
        CREATE TABLE IF NOT EXISTS recipes (
            recipe_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            file_path TEXT NOT NULL,
            model TEXT,
            goal TEXT,
            min_gpus INTEGER,
            requires_json TEXT,
            config_json TEXT NOT NULL,
            evidence_json TEXT,
            file_mtime REAL,
            times_applied INTEGER DEFAULT 0,
            last_applied_at TEXT
        )
    ''')
    recipes = scan_recipes()
    seen_ids = set()
    for r in recipes:
        requires = r.get('requires') or {}
        conn.execute('''
            INSERT INTO recipes (recipe_id, name, description, file_path, model,
                                 goal, min_gpus, requires_json, config_json,
                                 evidence_json, file_mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(recipe_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                file_path=excluded.file_path, model=excluded.model,
                goal=excluded.goal, min_gpus=excluded.min_gpus,
                requires_json=excluded.requires_json,
                config_json=excluded.config_json,
                evidence_json=excluded.evidence_json,
                file_mtime=excluded.file_mtime
        ''', (
            r['recipe_id'], r['name'], r.get('description', ''),
            r['_file'], r.get('model') or r['config'].get('model'),
            r['goal'], requires.get('min_gpus'),
            json.dumps(requires), json.dumps(r['config']),
            json.dumps(r.get('evidence') or {}), r['_mtime'],
        ))
        seen_ids.add(r['recipe_id'])
    # Remove rows whose files disappeared
    if seen_ids:
        conn.execute(
            f"DELETE FROM recipes WHERE recipe_id NOT IN ({','.join('?' * len(seen_ids))})",
            tuple(seen_ids))
    else:
        conn.execute('DELETE FROM recipes')
    conn.commit()
    return len(recipes)


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
    """All indexed recipes + availability + usage, for the UI."""
    sync_recipes_db(conn)
    hw = get_latest_hw_scan(conn)
    out = []
    for row in conn.execute(
            'SELECT recipe_id, name, description, model, goal, min_gpus, '
            'requires_json, evidence_json, times_applied, last_applied_at, '
            'file_path FROM recipes ORDER BY name'):
        recipe = {
            'recipe_id': row[0], 'name': row[1], 'description': row[2],
            'model': row[3], 'goal': row[4], 'min_gpus': row[5],
            'evidence': json.loads(row[7]) if row[7] else {},
            'times_applied': row[8], 'last_applied': row[9],
        }
        requires = json.loads(row[6]) if row[6] else {}
        available, reasons = check_availability(
            {**recipe, 'requires': requires}, hw)
        recipe['available'] = available
        recipe['unavailable_reasons'] = reasons
        recipe['hw_scan_present'] = hw is not None
        out.append(recipe)
    return out


def apply_recipe(conn, recipe_id: str) -> Optional[Dict]:
    """Fetch a recipe's config payload + bump usage counters."""
    row = conn.execute(
        'SELECT config_json FROM recipes WHERE recipe_id=?', (recipe_id,)).fetchone()
    if not row:
        return None
    conn.execute(
        'UPDATE recipes SET times_applied=times_applied+1, last_applied_at=? '
        'WHERE recipe_id=?', (datetime.now().isoformat(), recipe_id))
    conn.commit()
    return normalize_config_payload(json.loads(row[0]))


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
