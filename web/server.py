"""
ServeIt Studio llm-d optimizer — Web Application Entry Point

This is the thin orchestrator that imports all modules and starts the server.
Business logic lives in:
  - web/auth.py           — login, setup, session guard
  - web/database.py       — DB init, state persistence, deployment templates
  - web/routes_api.py     — REST API endpoints
  - web/optimization.py   — optimization runner, log streaming
  - web/realtime.py       — SocketIO event handlers
  - web/app_context.py    — shared app, socketio, state, constants
"""

# IMPORTANT: Monkey patch MUST happen BEFORE any other imports
from gevent import monkey
monkey.patch_all()

import os
import sys

# Add parent directory to path for core module imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import shared context (creates app + socketio)
from web.app_context import app, socketio, DB_PATH, STATE_DIR, OPTIMIZATION_OUTPUT_DIR

# Import and register auth routes
from web.auth import register_auth_routes
register_auth_routes()

# Import database init and state management
from web.database import init_db, load_state, cleanup_stale_optimizations

# Import API routes (registers on import via @app.route decorators)
import web.routes_api  # noqa: F401

# Import optimization runner (provides log_to_ui, run_optimization_background, etc.)
import web.optimization  # noqa: F401

# Import SocketIO event handlers (registers on import via @socketio.on decorators)
import web.realtime  # noqa: F401


def main():
    """Main application entry point."""
    print("=" * 60)
    print("ServeIt Studio llm-d optimizer")
    print("Intelligent Search for Optimal llm-d Inference Configuration")
    print("=" * 60)

    init_db()
    load_state()
    cleanup_stale_optimizations()

    os.makedirs(OPTIMIZATION_OUTPUT_DIR, exist_ok=True)

    print(f"  Output directory: {OPTIMIZATION_OUTPUT_DIR}")
    print(f"  Database: {DB_PATH}")
    print(f"  State directory: {STATE_DIR}")
    print("  Starting web server on port 5000...")
    print("=" * 60)

    socketio.run(app, host='0.0.0.0', port=5000, debug=False)


if __name__ == '__main__':
    if os.environ.get('INFTUNE_MODE') == 'launcher':
        from launcher.app import main as launcher_main
        launcher_main()
    else:
        main()
