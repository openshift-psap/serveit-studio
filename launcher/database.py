"""Launcher database — users and instance metadata."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

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
        CREATE TABLE IF NOT EXISTS instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'creating',
            namespace TEXT NOT NULL,
            deployment_name TEXT NOT NULL,
            pvc_name TEXT NOT NULL,
            service_name TEXT NOT NULL,
            service_url TEXT,
            kubeconfig_secret TEXT,
            target_cluster TEXT DEFAULT 'local',
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            UNIQUE(owner_id, name)
        );
    ''')
    conn.close()
    print(f"  Launcher DB: {DB_PATH}")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
