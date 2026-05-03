# InfeRecipe Web Interface

Flask-based web UI for configuring and monitoring optimization runs.

## Structure

```
web/
├── __init__.py
├── server.py           # Flask + SocketIO web server
├── static/             # CSS, JS files (future)
└── templates/          # HTML templates
    └── index.html      # Main UI
```

## server.py

Main Flask application with SocketIO for real-time updates.

**Features**:
- **Flask** - Web framework
- **SocketIO** - Real-time bidirectional communication
- **gevent** - Async server (not deprecated eventlet)
- **SQLite** - Database for storing optimization runs
- **REST API** - Endpoints for status, config, results

**Routes**:
- `GET /` - Main UI page
- `GET /api/status` - Current optimization status
- `GET /api/config` - Get configuration
- `POST /api/config` - Update configuration
- `GET /api/runs` - List all optimization runs
- `GET /api/runs/<id>/configurations` - Get test configurations for a run

**SocketIO Events**:
- `connect` - Client connects to server
- `disconnect` - Client disconnects
- `start_optimization` - User starts an optimization run
- `stop_optimization` - User stops running optimization
- `status_update` - Server broadcasts status changes
- `console_log` - Server sends console messages to UI
- `optimization_progress` - Progress updates during tests
- `decision_log` - AI decision-making logs

## index.html

Main UI with tabbed interface.

**Tabs**:
1. **Home** - Welcome page with feature overview
2. **Optimize Configuration** - Input form for ISL/OSL/Users
3. **Results** - Results comparison and recommendations

**Features**:
- Red gradient theme
- Live console viewer with color-coded logs
- Real-time status updates via SocketIO
- Form validation
- Responsive design

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export INFE_RECIPE_PATH=/path/to/InfeRecipe
export DB_PATH=./inferecipe.db
export OPTIMIZATION_OUTPUT_DIR=./optimization-runs

# Run the server
cd InfeRecipe
python3 -m web.server
```

Access at: http://localhost:5000

## Running in Container

The web server runs inside the InfeRecipe benchmark pod:

```bash
# Deploy to cluster
./scripts/deploy.sh deploy

# Access via OpenShift Route
https://inferecipe-benchmark-ui-llm-d.apps.psap-gpu.ibm-rh-ai.rhperfscale.org
```

## Database Schema

### optimization_runs
```sql
CREATE TABLE optimization_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_name TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL,
    isl INTEGER NOT NULL,
    osl INTEGER NOT NULL,
    num_users INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    optimal_config TEXT,
    notes TEXT
)
```

### test_configurations
```sql
CREATE TABLE test_configurations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    config_name TEXT NOT NULL,
    prefill_pods INTEGER NOT NULL,
    decode_pods INTEGER NOT NULL,
    tensor_parallelism INTEGER NOT NULL,
    status TEXT NOT NULL,
    ttft_p50 REAL, ttft_p95 REAL, ttft_p99 REAL,
    itl_p50 REAL, itl_p95 REAL, itl_p99 REAL,
    throughput REAL,
    gpu_utilization REAL,
    started_at TEXT,
    completed_at TEXT,
    metrics_json TEXT,
    FOREIGN KEY (run_id) REFERENCES optimization_runs (id),
    UNIQUE(run_id, config_name)
)
```

## Future Enhancements

- [ ] Add static/ directory for CSS and JS files
- [ ] Separate CSS from HTML
- [ ] Add client-side form validation
- [ ] Progressive Web App (PWA) support
- [ ] Dark/light theme toggle
- [ ] Export results as PDF
