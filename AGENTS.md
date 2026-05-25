# Agent Instructions

## Documentation Lookups
When using any external library (e.g., LangChain, Chroma), use `chub` (https://github.com/andrewyng/context-hub) to fetch the latest documentation before writing or debugging code against that library.

---

## Project Overview
A daily cron job that scrapes Cribl and Ocient websites for product updates (blog posts, press releases, partnership announcements, product page changes), summarizes them via an LLM, publishes summaries to GitHub Pages, and stores them in a Chroma vector database for later retrieval by the zeroclaw AI assistant.

## Target Sites & Sections
- **Cribl** (cribl.io): blog/news, press releases/partnerships, product pages
- **Ocient** (ocient.com): blog/news, press releases/partnerships, product pages

## Python & Venv

- **Python version**: 3.13
- **Venv location**: `.venv/` in the project root (never use system Python)
- **Setup**: `make venv` (runs `python3.13 -m venv .venv && pip install -r requirements.txt && playwright install chromium`)
- **Running tests**: `make test` or `.venv/bin/pytest tests/ -v`
- **Running the program**: `.venv/bin/python main.py`
- All imports and tooling assume the venv is active or commands are prefixed with `.venv/bin/`

## Architecture

### Data Flow
1. Scraper discovers URLs for each site/section
2. Deduplication check: URL lookup in SQLite → if found, pre-check then hash comparison
3. New or changed content is summarized via LangChain + OpenRouter LLM
4. Summary + metadata written to both SQLite (operational) and Chroma (vector)
5. GitHub Pages HTML regenerated and pushed

### Components
- `main.py` — cron entry point, orchestrates the full pipeline
- `config.py` — loads all env vars
- `scrapers/base.py` — base scraper class
- `scrapers/cribl.py` — Cribl-specific scraping and change detection logic
- `scrapers/ocient.py` — Ocient-specific scraping and change detection logic
- `summarizer.py` — LangChain chain using OpenRouter LLM
- `storage/chroma_client.py` — Chroma read/write interface
- `storage/models.py` — Pydantic data models
- `publisher/github_pages.py` — HTML generation and git push to gh-pages

## Storage Architecture

Two separate stores with distinct responsibilities:

**SQLite** (`storage/db.py`) — operational tracking, deduplication:
- `url`, `normalized_url`, `company`, `category`, `title`
- `first_scraped_at`, `last_scraped_at` (ISO 8601)
- `content_hash` — SHA-256 of raw text, used for change detection
- `published_date` (ISO 8601, nullable) — article's own date, used for feed ordering
- `chroma_id` — cross-reference to the Chroma document
- `status` — `"ok"` | `"error"` | `"skipped"`

**Chroma** (`storage/chroma_client.py`) — vector storage, semantic search only:
- Document text: `source_text` (truncated raw content — this is what gets embedded)
- Metadata: `url`, `company`, `category`, `title`, `scraped_at`, `published_date`, `summary`

## Deduplication & Change Detection
Deduplication is handled by SQLite, not Chroma. Each site may require its own detection strategy. General approach:
1. Check if URL exists in SQLite (`db.get_by_url(normalized_url)`)
2. If not found: scrape, summarize, write to both SQLite and Chroma
3. If found: lightweight pre-check (HTTP HEAD for `Last-Modified`/`ETag`)
   - Inconclusive: re-scrape, compare `content_hash` against SQLite record
   - Changed: re-summarize, update both SQLite and Chroma records
   - Unchanged: skip

Note: Site-specific logic (e.g., checking a `last-modified` header, a date field, or a version string) should be implemented in each scraper class and may override or supplement the hash comparison.

## Configuration (Environment Variables)
All secrets and tunables are set via environment variables (`.env` file locally, system env on VPS):

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | API key for OpenRouter |
| `OPENROUTER_SUMMARIZATION_MODEL` | Model used to generate summaries (e.g., `anthropic/claude-sonnet-4-5`) |
| `OPENROUTER_EMBEDDING_MODEL` | Model used to generate vector embeddings (e.g., `openai/text-embedding-3-small`) |
| `CHROMA_HOST` | Chroma server host (default: `localhost`) |
| `CHROMA_PORT` | Chroma server port (default: `8000`) |
| `SQLITE_DB_PATH` | Path to SQLite database file (default: `data/product_updates.db`) |
| `GITHUB_TOKEN` | GitHub personal access token for pushing to gh-pages |
| `GITHUB_REPO` | GitHub repo for Pages (e.g., `username/product-updates`) |

## GitHub Pages Structure
```
index.html          # recent updates feed with links to company directories
cribl/
  index.html        # full Cribl update history, organized by date
ocient/
  index.html        # full Ocient update history, organized by date
```

## Infrastructure
- **Python**: 3.13; venv at `.venv/` in project root
- **VPS**: runs this program typically via cron, Chroma, and zeroclaw — all colocated
- **Local dev**: WSL2 Ubuntu 22.04 — same services run locally for development
- **Chroma**: accessed via HTTP client (not in-process); same instance shared by this program and zeroclaw
- **Cron command**: `cd /home/mark/product-update-digest && .venv/bin/python main.py >> logs/cron.log 2>&1`

## Zeroclaw Integration
[zeroclaw](https://github.com/zeroclaw-labs/zeroclaw) is an AI personal assistant running on the same VPS. It queries the Chroma collection directly to retrieve relevant product update summaries. When implementing Chroma queries, ensure the collection name and schema are stable and documented so zeroclaw can rely on them.
