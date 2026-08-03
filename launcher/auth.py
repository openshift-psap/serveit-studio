"""Launcher authentication — signup, login, session management."""

import sqlite3
from datetime import datetime
from flask import render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

from launcher.database import DB_PATH


def has_any_users():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, must_reset INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)')
    count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    return count > 0


def create_user(username, password, is_admin=False):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT INTO users (username, password_hash, is_admin, must_reset, created_at) VALUES (?, ?, ?, 0, ?)',
        (username, generate_password_hash(password), 1 if is_admin else 0, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def check_auth(username, password):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute('SELECT id, password_hash, is_admin, must_reset FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    if row and check_password_hash(row[1], password):
        return row[0], bool(row[2]), bool(row[3])
    return None, False, False


def reset_password(user_id, new_password):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE users SET password_hash = ?, must_reset = 1 WHERE id = ?',
        (generate_password_hash(new_password), user_id)
    )
    conn.commit()
    conn.close()


def clear_must_reset(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE users SET must_reset = 0 WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()


def change_own_password(user_id, new_password):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE users SET password_hash = ?, must_reset = 0 WHERE id = ?',
        (generate_password_hash(new_password), user_id)
    )
    conn.commit()
    conn.close()


def get_user_id():
    return session.get('user_id')


def get_username():
    return session.get('username')


def is_admin():
    return session.get('is_admin', False)


def register_auth_routes(app):

    @app.route('/setup', methods=['GET', 'POST'])
    def setup():
        if has_any_users():
            return redirect(url_for('login'))
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            confirm = request.form.get('confirm_password', '')
            if len(username) < 3:
                error = 'Username must be at least 3 characters.'
            elif len(password) < 6:
                error = 'Password must be at least 6 characters.'
            elif password != confirm:
                error = 'Passwords do not match.'
            else:
                create_user(username, password, is_admin=True)
                user_id, admin, _ = check_auth(username, password)
                session.permanent = True
                session['user_id'] = user_id
                session['username'] = username
                session['is_admin'] = admin
                return redirect(url_for('dashboard'))
        return render_template('login.html', setup=True, error=error)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if not has_any_users():
            return redirect(url_for('setup'))
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '')
            password = request.form.get('password', '')
            user_id, admin, must_reset = check_auth(username, password)
            if user_id:
                session.permanent = True
                session['user_id'] = user_id
                session['username'] = username
                session['is_admin'] = admin
                if must_reset:
                    return redirect(url_for('reset_password_page'))
                return redirect(url_for('dashboard'))
            error = 'Invalid username or password.'
        return render_template('login.html', setup=False, error=error)

    @app.route('/reset-password', methods=['GET', 'POST'])
    def reset_password_page():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        error = None
        if request.method == 'POST':
            password = request.form.get('password', '')
            confirm = request.form.get('confirm_password', '')
            if len(password) < 6:
                error = 'Password must be at least 6 characters.'
            elif password != confirm:
                error = 'Passwords do not match.'
            else:
                change_own_password(session['user_id'], password)
                return redirect(url_for('dashboard'))
        return render_template('login.html', reset=True, error=error)

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.before_request
    def require_auth():
        if request.endpoint in ('login', 'setup', 'reset_password_page', 'static'):
            return
        if not has_any_users():
            if request.path.startswith('/api/'):
                return jsonify({'error': 'setup_required'}), 401
            return redirect(url_for('setup'))
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'session_expired'}), 401
            return redirect(url_for('login'))
