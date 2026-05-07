"""Launcher database — users, groups, and instance metadata."""

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
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups_ (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT '📦',
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            UNIQUE(owner_id, name)
        );
        CREATE TABLE IF NOT EXISTS instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            group_id INTEGER,
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
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            FOREIGN KEY (group_id) REFERENCES groups_(id),
            UNIQUE(owner_id, name)
        );
    ''')

    # Migrations for existing databases
    cur = conn.execute("PRAGMA table_info(instances)")
    cols = {row[1] for row in cur.fetchall()}
    if 'workload_namespace' not in cols:
        conn.execute("ALTER TABLE instances ADD COLUMN workload_namespace TEXT NOT NULL DEFAULT ''")
    if 'group_id' not in cols:
        conn.execute("ALTER TABLE instances ADD COLUMN group_id INTEGER REFERENCES groups_(id)")
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
