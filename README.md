# Delta Exchange India — BTC Options & Futures Tick Data

Loads Delta Exchange India BTC **options trades** (`data/BTC_*.csv`) and
**BTCUSD perpetual futures trades** (`data/BTCUSD_*.csv`) into **TimescaleDB**
running in Docker. All project data lives under `D:\Projects\options` — nothing
project-related is stored on `C:`.

## Stack
- **TimescaleDB** (PostgreSQL + time-series extension) in Docker
- **Python 3** + `psycopg` (psycopg 3) for ingestion
- Schema: `options_trades`, `futures_trades` (hypertables, monthly chunks),
  plus a ready-but-empty `funding` table for future use

## Quick start
```powershell
# 1) Start the DB (data written to .\pgdata on D:)
docker compose up -d

# 2) Create virtualenv + deps (venv lives in .\.venv on D:)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r ingest\requirements.txt

# 3) Create schema (tables, hypertables, indexes)
python ingest\load.py init

# 4) Load everything (all 56 monthly CSVs)
python ingest\load.py load

# 5) Enable compression (shrinks ~200M rows to ~2-4 GB)
python ingest\load.py compress

# 6) Verify
python ingest\load.py verify
```

### Useful loader options
- `python ingest\load.py load --limit 100000` — quick test load (first 100k rows/file)
- `python ingest\load.py load --only BTC_2025-12.csv` — single file

## Data facts (verified)
- Timestamps in CSVs are **IST (UTC+05:30)** → loader converts to UTC
- Contract multiplier: **0.001 BTC/contract** (perpetual and options)
- BTC options expire **17:30 IST daily** (daily/weekly/monthly expiries mixed)
- Options symbol: `[C|P]-BTC-<strike>-<DDMMYY>` (strikes on 200 grid)
- `buyer_role`: `taker` = buyer was aggressor; `maker` = seller aggressed into bid
- Futures are perpetual-only (`BTCUSD`)
- July 2026 files are partial (through ~Jul 26)

## Docker data on D:
`docker-compose.yml` bind-mounts `./pgdata:/var/lib/postgresql` so the entire
database lives at `D:\Projects\options\pgdata`. The Docker image cache itself
lives in Docker Desktop's storage (C: by default) — see Docker Desktop
Settings → Resources → Disk image location to move it to D: if desired.
