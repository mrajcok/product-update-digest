# Deployment Guide

## Prerequisites

- VPS with Python 3.13 installed
- GitHub repo with a `gh-pages` branch (see setup below)
- OpenRouter API key
- GitHub personal access token with `repo` scope

---

## 1. Clone and Set Up

```bash
git clone https://github.com/<you>/product-update-digest.git /home/<user>/product-update-digest
cd /home/<user>/product-update-digest
make venv
```

`make venv` runs:
```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

---

## 2. Environment Variables

Copy the example and fill in real values:

```bash
cp .env.example .env
nano .env
```

```ini
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_SUMMARIZATION_MODEL=anthropic/claude-sonnet-4-5
OPENROUTER_EMBEDDING_MODEL=openai/text-embedding-3-small

CHROMA_HOST=localhost
CHROMA_PORT=8000
CHROMA_COLLECTION_NAME=product_updates

SQLITE_DB_PATH=data/product_updates.db

GITHUB_TOKEN=ghp_...
GITHUB_REPO=username/product-updates
GITHUB_PAGES_BRANCH=gh-pages

LOG_LEVEL=INFO
```

> Note: cron does not source `.bashrc`. The agent uses pydantic-settings to load `.env` from the working directory. The cron command below uses `cd` first to ensure the `.env` file is found.

---

## 3. GitHub Pages — First-Time Setup

Create an orphan `gh-pages` branch in your GitHub repo:

```bash
git clone https://<token>@github.com/<repo>.git /tmp/pages-init
cd /tmp/pages-init
git checkout --orphan gh-pages
git rm -rf .
echo "<h1>Coming soon</h1>" > index.html
git add index.html
git commit -m "Init gh-pages"
git push origin gh-pages
rm -rf /tmp/pages-init
```

Enable GitHub Pages in the repo settings → Pages → Source: `gh-pages` branch, `/ (root)`.

---

## 4. Chroma HTTP Server

Chroma runs as a systemd service bound to localhost (not exposed publicly, since both this agent and zeroclaw run on the same VPS).

Create `/etc/systemd/system/chroma.service`:

```ini
[Unit]
Description=Chroma Vector DB HTTP Server
After=network.target

[Service]
User=<user>
WorkingDirectory=/home/<user>/chroma-data
ExecStart=/home/<user>/product-update-digest/.venv/bin/chroma run \
  --host 127.0.0.1 \
  --port 8000 \
  --path /home/<user>/chroma-data
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable chroma
sudo systemctl start chroma
sudo systemctl status chroma
```

**Local dev (WSL2)**:

```bash
.venv/bin/chroma run --host localhost --port 8000 --path ~/chroma-data
```

---

## 5. Cron Job

Add to crontab (`crontab -e`):

```cron
0 12 * * * cd /home/<user>/product-update-digest && .venv/bin/python main.py >> logs/cron.log 2>&1
```

This runs daily at 7am EST (noon UTC). Adjust as needed.

To verify the pipeline manually before enabling the cron:

```bash
cd /home/<user>/product-update-digest
.venv/bin/python main.py --dry-run          # scrape only, no LLM or push
.venv/bin/python main.py --site cribl       # one scraper, full pipeline
.venv/bin/python main.py                    # full run
```

---

## 6. Zeroclaw Integration

Zeroclaw queries the Chroma collection directly. It expects:

| Field | Value |
|---|---|
| Collection name | `product_updates` (set via `CHROMA_COLLECTION_NAME`) |
| Chroma host | `localhost:8000` |
| Document fields | `url`, `company`, `category`, `title`, `scraped_at`, `published_date`, `summary`, `source_text` |

The collection name and schema are stable — do not rename fields without updating zeroclaw.

---

## 7. Logs

- **stdout**: captured by cron into `logs/cron.log`
- **Rotating file**: `logs/agent.log` (5 MB max, 3 backups) — written by `setup_logging()`

```bash
tail -f logs/cron.log
tail -f logs/agent.log
```

---

## 8. Updating

```bash
cd /home/<user>/product-update-digest
git pull
make venv          # reinstall deps if requirements.txt changed
```

No database migrations needed — `CREATE TABLE IF NOT EXISTS` is idempotent.
