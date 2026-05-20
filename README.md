# SdrScripts

Pipeline orchestration scripts for the SDR stack. Manages the full lifecycle
of AcquisitionApp, AnalysisApp, SdrResourceManager, PostgreSQL, and the
signal logger in both local (hardware-test) and Kubernetes deployments.

## Quick start — hardware test

```bash
# FM broadcast band only
./scan.sh 88-108

# Full sweep 80 MHz – 1 GHz
./scan.sh 80-1000

# Skip AnalysisApp — fastest scan, detections only
./scan.sh 80-1000 --no-analysis

# Read captured signals (SQLite)
python3 read_signals.py --db signals.db
```

## scan.sh

Starts the entire local pipeline against a USB-attached PlutoSDR.

```
Usage: scan.sh <start_mhz>-<stop_mhz> [options]

Options:
  --db PATH        SQLite database path (default: signals.db)
  --onnx           Force ONNX classifier (auto-enabled when model found)
  --no-analysis    Detections only — fastest sweep, no signal identification
  --no-pause       No analysis window between sweeps (may starve AnalysisApp)
```

### Services started (in order)

| # | Service | Container | Notes |
|---|---------|-----------|-------|
| 1 | **PostgreSQL 16** | `sdr-postgres` | `postgres:16-alpine`; data in `.pgdata/` |
| 2 | **Artemis AMQP broker** | `sdr-artemis` | `activemq-artemis:latest-alpine` |
| 3 | **SdrResourceManager** | `sdr-controller` | `sdr-controller-hw:2.0`; `--privileged` for USB PlutoSDR |
| 4 | **signal_logger** | host process | Dual-writes to SQLite + PostgreSQL |
| 5 | **AcquisitionApp** | `sdr-acquisition` | `sdr-acquisition:hw-test` |
| 6 | **AnalysisApp** | `sdr-analysis` | `sdr-analysis:hw-test` |

All containers use `--network=host`. Ctrl+C stops everything cleanly.

### PostgreSQL

Each start applies `AcquisitionApp/schema/init.sql` and
`AnalysisApp/schema/init.sql` (both idempotent — `IF NOT EXISTS`). Data
persists in `.pgdata/` between runs. Connect locally:

```bash
podman exec -it sdr-postgres psql -U sdr -d sdr_scanner
```

Useful queries:

```sql
-- Last 10 classified signals
SELECT freq_mhz, modulation, hypothesis_system, hyp_conf, analyzed_at
FROM recent_classifications ORDER BY analyzed_at DESC LIMIT 10;

-- Signal counts by frequency
SELECT * FROM freq_activity LIMIT 20;
```

### signal_logger

Subscribes to `rf.detections` + `rf.analysis` over AMQP, deduplicates within
a 100 kHz / 10-minute window, and writes to both backends:

- **SQLite** (`signals.db`) — local, queryable with `read_signals.py`
- **PostgreSQL** (`signals` table) — persists across runs, queryable via SQL

If `psycopg2-binary` is not installed on the host, the logger prints a warning
and falls back to SQLite-only (no crash).

```bash
# Install postgres driver to enable dual-write
pip install psycopg2-binary

# Read SQLite DB
python3 read_signals.py --db signals.db
```

### Scan cycle (with AnalysisApp)

```
AcquisitionApp sweeps 80–1000 MHz      ~8 s
  → publishes RF_DETECTION per signal
  → pauses 3 s (analysis_pause_ms)

AnalysisApp classifies up to 6 signals  ~3–10 s
  Fast path:  ONNX on embedded snapshot  ~5 ms   (when model loaded)
  Slow path:  NARROWBAND collect → rules ~3–4 s

AcquisitionApp re-submits SCAN task     <50 ms  (persistent AMQP channel)
```

## Tools

| Script | Purpose |
|--------|---------|
| `read_signals.py` | Pretty-print SQLite signal log |
| `replay_detections.py` | Replay recorded detections from DB through AnalysisApp |
| `annotate_signals.py` | Manually annotate signals in SQLite |
| `clear_signals.py` | Wipe the SQLite signal database |
| `signal_logger.py` | AMQP subscriber that writes detections to SQLite / PostgreSQL |

## Kubernetes / k3s

Use `SdrResourceManager/k8s/deploy.sh` to deploy the full stack:

```bash
cd ../SdrResourceManager
./k8s/deploy.sh            # prompts for AMQP + DB passwords
./k8s/deploy.sh --dry-run  # preview what would be applied

# Or non-interactive:
AMQP_PASSWORD=s3cr3t DB_PASSWORD=s3cr3t ./k8s/deploy.sh
```

The deploy script applies manifests in dependency order:
1. Namespace + `sdr-credentials` Secret
2. PostgreSQL StatefulSet (20 Gi PVC)
3. Artemis broker + SdrResourceManager controller
4. AcquisitionApp DaemonSet (one pod per `sdr-usb=true` node)
5. AnalysisApp Deployment
6. signal-logger Deployment

Label each SDR-attached node before deploying:

```bash
kubectl label node <node-name> sdr-usb=true
```

## Hardware

PlutoSDR accessible via USB (`uri=usb:` in `hw-test/devices-direct.xml`).
The controller container runs with `--privileged` to claim the USB device.

Credentials: AMQP `sdr_ctrl` / `sdr_hw_test`, PostgreSQL `sdr` / `sdr_hw_test`.
Set in scan.sh via `BROKER_PASS` and `PG_PASS` variables.
