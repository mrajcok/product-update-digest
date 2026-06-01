# Agent Instructions

## Documentation Lookups
When using any external library (e.g., LangChain, sqlite-vec, openai), use `chub` (https://github.com/andrewyng/context-hub) to fetch the latest documentation before writing or debugging code against that library.

---

## Project Overview
A daily cron job that scrapes Cribl and Ocient websites for news and blog posts (blog posts, press releases, partnership announcements, product page changes), summarizes them via an LLM, publishes summaries to GitHub Pages, and stores them in a sqlite-vec vector database for later retrieval.

## Hard Rules
- always update `README.md` with new features, architecture changes, and instructions

## Target Sites & Sections
- **Cribl** (cribl.io): blog/news, press releases/partnerships, product pages
- **Ocient** (ocient.com): blog/news, press releases/partnerships, product pages

## Python & Venv

- **Python version**: 3.13
- **Venv location**: `.venv/` in the project root (never use system Python)
- **Setup**: `make venv` (runs `uv venv --python 3.13 && uv pip install -r requirements.txt`; install uv first: `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Running tests**: `make test` or `.venv/bin/pytest tests/ -v`
- **Running the program**: `.venv/bin/python main.py`
- All imports and tooling assume the venv is active or commands are prefixed with `.venv/bin/`

## Architecture

### Data Flow
1. Scraper discovers URLs via sitemap.xml for each site
2. Deduplication check: URL lookup in SQLite → if found, pre-check then hash comparison
3. New or changed content is summarized via LangChain + OpenRouter LLM
4. Summary + metadata written to SQLite (operational) and sqlite-vec (vector)
5. GitHub Pages HTML regenerated and pushed

### Components
- `main.py` — cron entry point, orchestrates the full pipeline
- `config.py` — loads all env vars via pydantic-settings
- `scrapers/base.py` — abstract base scraper (dedup loop, HTTP retry, `pre_check` hook)
- `scrapers/cribl.py` — Cribl scraper; discovers URLs from sitemap.xml, stores `_sitemap_lastmod` as date fallback
- `scrapers/ocient.py` — Ocient scraper; discovers from `blog_post-sitemap.xml` + `news_release-sitemap.xml`, uses Chrome UA to bypass Flywheel WAF on XML paths
- `summarizer.py` — LangChain chain using OpenRouter LLM
- `storage/db.py` — SQLite client for deduplication and operational metadata
- `storage/vec_client.py` — sqlite-vec client for vector storage and semantic search
- `storage/models.py` — Pydantic data models: `ScrapedPage`, `ArticleRecord`, `ProductUpdate`
- `publisher/github_pages.py` — HTML generation and git push to gh-pages
- `tools/search.py` — interactive CLI for semantic search over the vector store

## Storage Architecture

Two stores with distinct responsibilities, both backed by the same `.db` file:

**SQLite** (`storage/db.py`) — operational tracking, deduplication:
- `url`, `normalized_url`, `company`, `category`, `title`
- `first_scraped_at`, `last_scraped_at` (ISO 8601)
- `content_hash` — SHA-256 of raw text, used for change detection
- `published_date` (ISO 8601, nullable) — article's own date, used for feed ordering
- `vec_id` — MD5 of normalized URL; cross-reference to the sqlite-vec document
- `status` — `"ok"` | `"error"` | `"skipped"`

**sqlite-vec** (`storage/vec_client.py`) — vector storage, semantic search:
- `vec_items` table: `id`, `url`, `company`, `category`, `title`, `scraped_at`, `published_date`, `summary`, `source_text`
- `vec_embeddings` virtual table (`vec0`): `id`, `embedding float[1536]`
- Embeddings generated via openai SDK against OpenRouter's `/v1/embeddings` endpoint
- KNN search uses a CTE: `WITH knn AS (SELECT id, distance FROM vec_embeddings WHERE embedding MATCH ? AND k = ?) ...`
- Upsert deletes then re-inserts (vec0 virtual tables don't support `ON CONFLICT`)

## Deduplication & Change Detection
Handled by SQLite. General approach:
1. Check if URL exists in SQLite (`db.get_by_url(normalized_url)`)
2. If not found: scrape, summarize, write to both SQLite and sqlite-vec
3. If found: lightweight pre-check (HTTP HEAD for `Last-Modified`)
   - Inconclusive: re-scrape, compare `content_hash` against SQLite record
   - Changed: re-summarize, update both SQLite and sqlite-vec records
   - Unchanged: skip

## Scraper Discovery Strategy
Both scrapers use sitemap.xml rather than scraping listing pages:
- Sitemaps provide `lastmod` dates for pre-filtering articles older than `MAX_ARTICLE_AGE_DAYS`
- Cribl: single `sitemap.xml` — filter by `_BLOG_RE` and `_NEWS_RE` patterns
- Ocient: separate `blog_post-sitemap.xml` and `news_release-sitemap.xml`; Flywheel WAF requires a Chrome `User-Agent` on XML paths
- Both scrapers maintain a `_BLOG_BLOCKLIST` tuple of URL substrings to skip

## Configuration (Environment Variables)
All secrets and tunables are set via environment variables (`.env` file locally, system env on VPS):

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | API key for OpenRouter |
| `OPENROUTER_SUMMARIZATION_MODEL` | Model used to generate summaries (default: `anthropic/claude-sonnet-4-5`) |
| `OPENROUTER_EMBEDDING_MODEL` | Model used to generate vector embeddings (default: `openai/text-embedding-3-small`) |
| `EMBEDDING_DIMENSIONS` | Vector dimensions matching the embedding model (default: `1536`) |
| `SQLITE_DB_PATH` | Path to SQLite database file (default: `data/product_updates.db`) |
| `GITHUB_TOKEN` | GitHub personal access token for pushing to gh-pages |
| `GITHUB_REPO` | GitHub repo for Pages (e.g., `username/product-updates`) |
| `GITHUB_PAGES_BRANCH` | Branch to publish to (default: `gh-pages`) |
| `MAX_ARTICLE_AGE_DAYS` | How far back to index articles (default: `30`) |
| `LOG_LEVEL` | Logging level (default: `INFO`) |

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
- **VPS**: runs this program via cron — no external services required (sqlite-vec is in-process)
- **Local dev**: WSL2 Ubuntu 22.04 — same setup as production
- **Cron command**: `cd /home/<user>/product-update-digest && .venv/bin/python main.py >> logs/cron.log 2>&1`
