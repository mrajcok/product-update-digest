# Deployment Guide

## Prerequisites

- VPS with Python 3.13 installed
- GitHub repo with a `gh-pages` branch (see setup below)
- OpenRouter API key
- GitHub personal access token with `repo` scope

---

## 1. Install uv

[uv](https://docs.astral.sh/uv/) replaces pip/virtualenv and is the only tool needed to manage the Python environment:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify: `uv --version`

---

## 2. Clone and Set Up

```bash
git clone https://github.com/<you>/product-update-digest.git /home/<user>/product-update-digest
cd /home/<user>/product-update-digest
make venv
```

`make venv` runs:
```bash
uv venv --python 3.13
uv pip install -r requirements.txt
```

---

## 3. Environment Variables

Copy the example and fill in real values:

```bash
cp .env.example .env
nano .env
```

```ini
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_SUMMARIZATION_MODEL=anthropic/claude-sonnet-4-5
OPENROUTER_EMBEDDING_MODEL=openai/text-embedding-3-small
EMBEDDING_DIMENSIONS=1536

SQLITE_DB_PATH=data/product_updates.db

GITHUB_TOKEN=ghp_...
GITHUB_REPO=username/product-updates
GITHUB_PAGES_BRANCH=gh-pages

MAX_ARTICLE_AGE_DAYS=30
LOG_LEVEL=INFO
```

> Note: cron does not source `.bashrc`. The agent uses pydantic-settings to load `.env` from the working directory. The cron command below uses `cd` first to ensure the `.env` file is found.

---

## 4. GitHub Pages — First-Time Setup

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

## 6. Logs

- **stdout**: captured by cron into `logs/cron.log`
- **Rotating file**: `logs/agent.log` (5 MB max, 3 backups) — written by `setup_logging()`

```bash
tail -f logs/cron.log
tail -f logs/agent.log
```

---

## 7. Updating

```bash
cd /home/<user>/product-update-digest
git pull
make venv          # reinstall deps if requirements.txt changed
```

No database migrations needed — `CREATE TABLE IF NOT EXISTS` is idempotent, and `db.py` handles the `chroma_id → vec_id` column rename automatically on first run.
