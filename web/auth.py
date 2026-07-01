"""Authentication — login, setup, session guard, rate limiting."""

import os
import sqlite3
from datetime import datetime, timedelta
from flask import render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash

from web.app_context import app, DB_PATH

# When managed by the launcher, auth is disabled
AUTH_DISABLED = os.environ.get('AUTH_DISABLED', 'false').lower() == 'true'

_login_attempts = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 60


def _has_any_users():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
    count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    return count > 0


def _create_user(username, password):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
        (username, generate_password_hash(password), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def _check_auth(username, password):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute('SELECT password_hash FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    return row is not None and check_password_hash(row[0], password)


def _is_rate_limited(ip):
    now = datetime.now().timestamp()
    if ip in _login_attempts:
        count, first_time = _login_attempts[ip]
        if now - first_time > _LOGIN_WINDOW_SECONDS:
            del _login_attempts[ip]
            return False
        return count >= _LOGIN_MAX_ATTEMPTS
    return False


def _record_failed_attempt(ip):
    now = datetime.now().timestamp()
    if ip in _login_attempts:
        count, first_time = _login_attempts[ip]
        if now - first_time > _LOGIN_WINDOW_SECONDS:
            _login_attempts[ip] = (1, now)
        else:
            _login_attempts[ip] = (count + 1, first_time)
    else:
        _login_attempts[ip] = (1, now)


def register_auth_routes():
    """Register auth routes on the Flask app. Call once at startup."""

    @app.route('/setup', methods=['GET', 'POST'])
    def setup():
        if _has_any_users():
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
                _create_user(username, password)
                session.permanent = True
                app.permanent_session_lifetime = timedelta(hours=24)
                session['user'] = username
                session['_created'] = datetime.now().timestamp()
                return redirect(url_for('index'))
        return render_template('setup.html', error=error)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if not _has_any_users():
            return redirect(url_for('setup'))
        error = None
        if request.method == 'POST':
            client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            if _is_rate_limited(client_ip):
                error = 'Too many login attempts. Try again in 60 seconds.'
            else:
                username = request.form.get('username', '')
                password = request.form.get('password', '')
                if _check_auth(username, password):
                    remember = request.form.get('remember')
                    session.permanent = True
                    if remember:
                        app.permanent_session_lifetime = timedelta(days=30)
                    else:
                        app.permanent_session_lifetime = timedelta(hours=24)
                    session['user'] = username
                    session['_created'] = datetime.now().timestamp()
                    _login_attempts.pop(client_ip, None)
                    return redirect(url_for('index'))
                _record_failed_attempt(client_ip)
                error = 'Invalid username or password.'
        return render_template('login.html', error=error)

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.route('/auto-login')
    def auto_login():
        """Token-based auto-login for launcher integration."""
        token = request.args.get('token', '')
        if not token:
            return redirect(url_for('login'))
        expected = os.environ.get('AUTO_LOGIN_TOKEN', '')
        if not expected or token != expected:
            return redirect(url_for('login'))
        # Find the first user and log them in
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute('SELECT username FROM users ORDER BY id LIMIT 1').fetchone()
        conn.close()
        if row:
            session.permanent = True
            session['user'] = row[0]
            session['_created'] = datetime.now().timestamp()
        return redirect(url_for('index'))

    @app.before_request
    def require_auth():
        if AUTH_DISABLED:
            return
        if request.endpoint in ('login', 'setup', 'auto_login', 'static',
                                  'backup_database', 'backup_artifacts', 'restore_artifacts'):
            return
        if not _has_any_users():
            return redirect(url_for('setup'))
        if 'user' not in session:
            return redirect(url_for('login'))
