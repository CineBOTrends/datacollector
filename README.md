# cinebotrends-datacollector

A high-performance distributed scraper that tracks movie ticket sales and advance bookings from BookMyShow across 10,000+ theatres in India.

## Features

- **9 parallel scraper shards** covering all Indian theatres
- **Advance & Daily modes** — future date bookings and real-time sales
- **Anti-bot stealth** — UA rotation, IP spoofing, CloudFlare bypass, session persistence
- **Async district API** (shard 9) with exponential backoff
- **REST API** to serve scraped data (Flask + Gunicorn)
- **Scheduler** with configurable IST cron times (APScheduler)
- **Cloudflare R2** upload with automatic cache purge
- **Docker** ready (API server + scheduler in one container)

## Setup

```bash
# Clone
git clone https://github.com/CineBOTrends/datacollector.git
cd datacollector

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

### Full Pipeline (all 9 shards + combine)

```bash
python cli.py run --mode advance --date 20260319
python cli.py run --mode daily
python cli.py run --mode both --date 20260320
```

### Single Shard

```bash
python cli.py scrape --mode advance --shard 1 --date 20260319
python cli.py scrape --mode advance --shard all
```

### Combine Only

```bash
python cli.py combine --mode advance --date 20260319
```

### API Server

```bash
python cli.py serve --port 8080
```

### Scheduler (Automated Cron)

```bash
# Start scheduler (runs jobs at times defined in schedule_config.yaml)
python cli.py scheduler

# Use a custom config file
python cli.py scheduler --config my_schedule.yaml
```

Edit `schedule_config.yaml` to set exact IST times for each mode:
```yaml
timezone: "Asia/Kolkata"

advance:
  - "09:00"
  - "12:00"
  - "15:00"
  - "18:00"
  - "21:00"
  - "23:30"

daily:
  - "03:00"
  - "05:00"
  - "07:00"
  - "08:00"
  - "10:00"
  - "11:00"
  - "13:00"
  - "14:00"
  - "16:00"
  - "17:00"
  - "19:00"
  - "20:00"
  - "22:00"

cleanup:
  time: "02:00"
  retain_days: 10
```

### Cleanup Old Files

```bash
# Manual cleanup
python cli.py cleanup

# Automatic: runs daily at 02:00 IST via scheduler (deletes folders older than 10 days)
```

## Output

Data is saved to:
- `advance/data/YYYYMMDD/finaldetailed.json` — all show records
- `advance/data/YYYYMMDD/finalsummary.json` — per-movie aggregated stats
- `daily/data/YYYYMMDD/` — same structure for daily data

R2 upload path: `tt_gross/v1/{mode}/{year}/{month}/{day}.json` (finalsummary only)

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /api/advance/<date>` | Advance summary for date |
| `GET /api/advance/<date>/detailed` | Advance detailed data (local only) |
| `GET /api/daily/<date>` | Daily summary for date |
| `GET /api/daily/<date>/detailed` | Daily detailed data (local only) |
| `GET /api/advance/latest` | Latest advance data |
| `GET /api/daily/latest` | Latest daily data |
| `GET /api/dates` | List all available dates |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATE_CODE` | No | Override scrape date (YYYYMMDD) |
| `SCRAPE_MODE` | No | `advance`, `daily`, or `both` |
| `R2_ENDPOINT` | No | R2 endpoint URL |
| `R2_ACCESS_KEY` | No | R2 access key |
| `R2_SECRET_KEY` | No | R2 secret key |
| `R2_BUCKET_NAME` | No | R2 bucket (default: `cinebotrends-data`) |
| `R2_KEY_PREFIX` | No | Object key prefix (default: `tt_gross`) |
| `CF_ZONE_ID` | No | Cloudflare zone ID (for cache purge after R2 upload) |
| `CF_API_TOKEN` | No | Cloudflare API token (cache purge permission) |
| `CF_PURGE_BASE_URL` | No | R2 subdomain URL (e.g. `https://data.cinebotrends.com`) |
| `DATA_PATH` | No | Data directory for API server (default: `.`) |
| `CONCURRENCY` | No | Async shard concurrency (default: `20`) |

## Docker

```bash
# Build
docker build -t cinebotrends .

# Run (starts both API server on :8080 and scheduler)
docker run -p 8080:8080 cinebotrends

# Run scraper manually
docker run cinebotrends python cli.py run --mode advance --date 20260319
```

In Docker, `start.sh` launches both the API server (background) and the scheduler (foreground).

## Project Structure

```
├── cli.py                  # CLI entry point
├── runner.py               # Parallel orchestrator
├── scheduler.py            # APScheduler cron scheduler
├── schedule_config.yaml    # Cron times config (IST)
├── start.sh                # Docker entrypoint (API + scheduler)
├── Dockerfile
├── scraper/
│   ├── config.py           # Date/path/env config
│   ├── stealth.py          # Anti-bot: UA, fake IP, CloudScraper
│   ├── fetcher_sync.py     # Sync engine (shards 1-8)
│   ├── fetcher_async.py    # Async engine (shard 9)
│   ├── parser.py           # BMS + District API parsers
│   ├── summary.py          # Movie summary builder
│   ├── dedupe.py           # Deduplication
│   ├── daily_merge.py      # Daily incremental merge
│   └── scrape.py           # Unified run_shard() entry
├── combiner/
│   └── combine.py          # Merge 9 shards into final output
├── services/
│   ├── logger.py           # Custom box-banner logger
│   ├── r2_storage.py       # Cloudflare R2 integration
│   ├── hooks.py            # Post-execution hooks (CF cache purge)
│   ├── api_server.py       # Flask REST API
│   └── cleanup.py          # Old data folder cleanup
└── venues/                 # Theatre venue data (8 shards + district)
```
