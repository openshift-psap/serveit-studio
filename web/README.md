# ServeIt Studio Web Interface

Flask-based web UI for configuring and monitoring optimization runs.

## Structure

```
web/
├── __init__.py
├── server.py              # Flask + SocketIO + gevent web server
├── static/
│   ├── css/style.css      # Red Hat branded UI styles
│   ├── js/app.js          # Single-page app logic
│   └── img/logo.png       # ServeIt Studio logo
└── templates/
    ├── index.html          # Main UI (wizard + report + console)
    ├── login.html          # Login page
    ├── setup.html          # Initial setup page
    └── partials/           # Wizard step templates
        ├── step1.html      # Optimization goal selection
        ├── step3.html      # Workload configuration (ISL, OSL, model)
        ├── step4.html      # Test configuration (benchmark, search strategy, advanced)
        ├── step5.html      # Storage & deployment setup
        └── step6.html      # Pre-optimization review & report
```

## server.py

Main Flask application with SocketIO for real-time updates.

**Features**:
- **Flask** — Web framework with session auth
- **SocketIO** — Real-time bidirectional communication (console logs, status)
- **gevent** — Async server with monkey-patching
- **SQLite** — Database for optimization runs, test results, console logs
- **REST API** — Endpoints for runs, charts, manifests, hardware scans

**REST API Routes**:

| Route | Method | Description |
|---|---|---|
| `/` | GET | Main UI page |
| `/login` | GET/POST | Authentication |
| `/api/runs` | GET | List all optimization runs |
| `/api/runs/<id>/charts` | GET | Full report data (charts, recommendations, results) |
| `/api/runs/<id>/configurations` | GET | Test configurations for a run |
| `/api/run/<id>/config/<test_id>/manifest/<type>` | GET | Download K8s manifest YAML |
| `/api/runs_for_resume` | GET | Runs with completed test counts |
| `/api/hardware_scan` | GET | Cluster hardware scan results |

**SocketIO Events**:

| Event | Direction | Description |
|---|---|---|
| `setup_storage` | Client → Server | Start optimization (download model + run) |
| `start_optimization` | Client → Server | Start optimization directly |
| `stop_optimization` | Client → Server | Stop running optimization |
| `resume_optimization` | Client → Server | Resume a previous run |
| `save_config` | Client → Server | Save UI configuration |
| `load_config` | Client → Server | Load saved configuration |
| `scan_hardware` | Client → Server | Trigger cluster hardware scan |
| `console_log` | Server → Client | Console log messages |
| `status_update` | Server → Client | Optimization status changes |
| `config_updated` | Server → Client | Config changed by another client |

## Running Locally

```bash
# Set environment variables
export INFTUNE_PATH=/path/to/ServeIt Studio
export DB_PATH=./inftune.db

# Run the server
python3 web/server.py
```

Access at: http://localhost:5000

## Database Schema

### optimization_runs
Stores run-level configuration and status.

Key columns: `run_name`, `model`, `isl`, `osl`, `num_users`, `status`, `goal`, `config_json`, `notes`, `prefix_cache_hit_pct`, `pd_search_mode` (via config_json), `workload_mode`, `dataset_source`

### test_configurations
Stores per-test results and metrics.

Key columns: `config_name`, `architecture`, `tensor_parallelism`, `decode_tp`, `ttft_p50/p90/p95/p99`, `itl_p50/p90/p95/p99`, `throughput_p50/p90/p95/p99`, `metrics_json`, `manifests_yaml`, `test_config_json`, `guidellm_raw_json`

### console_logs
Real-time console output, persisted across server restarts.

### hardware_scans
Cached cluster hardware scan results (GPUs, NICs, storage classes).
