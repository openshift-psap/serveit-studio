"""Launcher database — users, clusters, and instance metadata."""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get('LAUNCHER_DB_PATH', '/mnt/storage/launcher.db')


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            must_reset INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT '🖥️',
            owner_id INTEGER NOT NULL,
            kubeconfig_secret TEXT,
            target_cluster TEXT DEFAULT 'local',
            storage_class TEXT,
            proxy TEXT,
            description TEXT,
            scan_data TEXT,
            scanned_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            UNIQUE(owner_id, name)
        );
        CREATE TABLE IF NOT EXISTS instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            cluster_id INTEGER,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'creating',
            namespace TEXT NOT NULL,
            workload_namespace TEXT NOT NULL DEFAULT '',
            deployment_name TEXT NOT NULL,
            pvc_name TEXT NOT NULL,
            service_name TEXT NOT NULL,
            service_url TEXT,
            kubeconfig_secret TEXT,
            target_cluster TEXT DEFAULT 'local',
            auto_login_token TEXT,
            preset_gpus INTEGER,
            preset_nodes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            FOREIGN KEY (cluster_id) REFERENCES clusters(id),
            UNIQUE(owner_id, name, cluster_id)
        );
        CREATE TABLE IF NOT EXISTS instance_access (
            user_id INTEGER NOT NULL,
            instance_id INTEGER NOT NULL,
            granted_at TEXT NOT NULL,
            granted_by INTEGER,
            PRIMARY KEY (user_id, instance_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE,
            FOREIGN KEY (granted_by) REFERENCES users(id)
        );
    ''')

    # Migrations for existing databases
    cur = conn.execute("PRAGMA table_info(users)")
    user_cols = {row[1] for row in cur.fetchall()}
    if 'is_admin' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET is_admin=1 WHERE id=(SELECT MIN(id) FROM users)")
    if 'must_reset' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN must_reset INTEGER NOT NULL DEFAULT 0")

    # Migrate groups_ → clusters
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if 'groups_' in tables and 'clusters' not in tables:
        conn.execute("ALTER TABLE groups_ RENAME TO clusters")
        conn.execute("ALTER TABLE clusters ADD COLUMN kubeconfig_secret TEXT")
        conn.execute("ALTER TABLE clusters ADD COLUMN target_cluster TEXT DEFAULT 'local'")
        conn.execute("ALTER TABLE clusters ADD COLUMN storage_class TEXT")
    elif 'clusters' in tables:
        cur = conn.execute("PRAGMA table_info(clusters)")
        cluster_cols = {row[1] for row in cur.fetchall()}
        if 'kubeconfig_secret' not in cluster_cols:
            conn.execute("ALTER TABLE clusters ADD COLUMN kubeconfig_secret TEXT")
        if 'target_cluster' not in cluster_cols:
            conn.execute("ALTER TABLE clusters ADD COLUMN target_cluster TEXT DEFAULT 'local'")
        if 'storage_class' not in cluster_cols:
            conn.execute("ALTER TABLE clusters ADD COLUMN storage_class TEXT")
        if 'scan_data' not in cluster_cols:
            conn.execute("ALTER TABLE clusters ADD COLUMN scan_data TEXT")
        if 'scanned_at' not in cluster_cols:
            conn.execute("ALTER TABLE clusters ADD COLUMN scanned_at TEXT")
        if 'proxy' not in cluster_cols:
            conn.execute("ALTER TABLE clusters ADD COLUMN proxy TEXT")
        if 'description' not in cluster_cols:
            conn.execute("ALTER TABLE clusters ADD COLUMN description TEXT")

    # Migrate group_id → cluster_id
    cur = conn.execute("PRAGMA table_info(instances)")
    inst_cols = {row[1] for row in cur.fetchall()}
    if 'workload_namespace' not in inst_cols:
        conn.execute("ALTER TABLE instances ADD COLUMN workload_namespace TEXT NOT NULL DEFAULT ''")
    if 'group_id' in inst_cols and 'cluster_id' not in inst_cols:
        conn.execute("ALTER TABLE instances ADD COLUMN cluster_id INTEGER REFERENCES clusters(id)")
        conn.execute("UPDATE instances SET cluster_id = group_id")
    elif 'cluster_id' not in inst_cols:
        conn.execute("ALTER TABLE instances ADD COLUMN cluster_id INTEGER REFERENCES clusters(id)")
    if 'auto_login_token' not in inst_cols:
        conn.execute("ALTER TABLE instances ADD COLUMN auto_login_token TEXT")
    if 'preset_gpus' not in inst_cols:
        conn.execute("ALTER TABLE instances ADD COLUMN preset_gpus INTEGER")
    if 'preset_nodes' not in inst_cols:
        conn.execute("ALTER TABLE instances ADD COLUMN preset_nodes TEXT")
    if 'storage_class' not in inst_cols:
        conn.execute("ALTER TABLE instances ADD COLUMN storage_class TEXT")
    if 'storage_size' not in inst_cols:
        conn.execute("ALTER TABLE instances ADD COLUMN storage_size TEXT")

    # Allow same instance name on different clusters
    try:
        conn.execute("DROP INDEX IF EXISTS sqlite_autoindex_instances_1")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_owner_name_cluster ON instances(owner_id, name, cluster_id)")
    except Exception:
        pass

    # Settings table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_rescan', 'true')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rescan_interval_min', '10')")

    conn.commit()
    conn.close()
    print(f"  Launcher DB: {DB_PATH}")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
