"""Launcher authentication — signup, login, session management."""

import sqlite3
from datetime import datetime, timedelta
from flask import render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash

from launcher.database import DB_PATH


def has_any_users():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
    count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    return count > 0


def create_user(username, password):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
        (username, generate_password_hash(password), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def check_auth(username, password):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute('SELECT id, password_hash FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    if row and check_password_hash(row[1], password):
        return row[0]
    return None


def get_user_id():
    return session.get('user_id')


def get_username():
    return session.get('username')


def register_auth_routes(app):
    """Register launcher auth routes."""

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
                create_user(username, password)
                user_id = check_auth(username, password)
                session.permanent = True
                session['user_id'] = user_id
                session['username'] = username
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
            user_id = check_auth(username, password)
            if user_id:
                session.permanent = bool(request.form.get('remember'))
                session['user_id'] = user_id
                session['username'] = username
                return redirect(url_for('dashboard'))
            error = 'Invalid username or password.'
        return render_template('login.html', setup=False, error=error)

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.before_request
    def require_auth():
        if request.endpoint in ('login', 'setup', 'static'):
            return
        if not has_any_users():
            return redirect(url_for('setup'))
        if 'user_id' not in session:
            return redirect(url_for('login'))
