# product-update-digest

A daily cron job that scrapes product updates from Cribl and Ocient (blog posts, press releases, product page changes), summarizes them with an LLM via [OpenRouter](https://openrouter.ai), publishes a static feed to GitHub Pages, and stores embeddings in a [Chroma](https://www.trychroma.com) vector database for retrieval by an AI assistant.

## What it does

1. **Scrapes** Cribl and Ocient websites for new or changed content
2. **Deduplicates** using SQLite — skips unchanged content via URL tracking and SHA-256 content hashing
3. **Summarizes** each new item using a configurable LLM (default: `anthropic/claude-sonnet-4-5` via OpenRouter)
4. **Stores** summaries and vector embeddings in Chroma for semantic search
5. **Publishes** a static HTML digest to GitHub Pages

## Requirements

- Python 3.13
- A running [Chroma HTTP server](https://docs.trychroma.com/production/chroma-server/client-server-mode) (localhost:8000)
- [OpenRouter](https://openrouter.ai) API key
- GitHub personal access token with `repo` scope (for pushing to GitHub Pages)

## Setup

```bash
git clone https://github.com/<you>/product-update-digest.git
cd product-update-digest
make venv
cp .env.example .env
# edit .env with your API keys and config
```

`make venv` creates `.venv/`, installs dependencies, and downloads the Playwright Chromium browser (used as a fallback for JS-rendered pages).

## Configuration

All configuration is via environment variables (`.env` file locally, system env in production):

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `OPENROUTER_SUMMARIZATION_MODEL` | LLM for summaries (default: `anthropic/claude-sonnet-4-5`) |
| `OPENROUTER_EMBEDDING_MODEL` | Embedding model (default: `openai/text-embedding-3-small`) |
| `CHROMA_HOST` | Chroma server host (default: `localhost`) |
| `CHROMA_PORT` | Chroma server port (default: `8000`) |
| `CHROMA_COLLECTION_NAME` | Chroma collection name (default: `product_updates`) |
| `SQLITE_DB_PATH` | Path to SQLite database (default: `data/product_updates.db`) |
| `GITHUB_TOKEN` | GitHub PAT for pushing to gh-pages |
| `GITHUB_REPO` | Target GitHub repo for Pages (e.g., `username/product-updates`) |
| `GITHUB_PAGES_BRANCH` | Branch to publish to (default: `gh-pages`) |

## Usage

```bash
.venv/bin/python main.py                   # full run
.venv/bin/python main.py --dry-run         # scrape only, no LLM calls or publishing
.venv/bin/python main.py --site cribl      # run one scraper only
```

## Running tests

```bash
make test
```

62 tests, no external services required (SQLite uses in-memory DB; HTTP calls are mocked).

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for full instructions covering:
- Chroma systemd service setup
- GitHub Pages first-time initialization
- Cron job configuration

## Architecture

![Pipeline diagram](docs/flow-diagram.svg)

```
main.py                        # orchestration entry point
config.py                      # pydantic-settings config
summarizer.py                  # LangChain summarization chain
scrapers/
  base.py                      # abstract scraper (dedup loop, Playwright fallback)
  cribl.py                     # Cribl scraper
  ocient.py                    # Ocient scraper
storage/
  models.py                    # Pydantic models: ScrapedPage, ArticleRecord, ProductUpdate
  db.py                        # SQLite client (URL tracking, deduplication)
  chroma_client.py             # Chroma client (vector storage, semantic search)
publisher/
  github_pages.py              # Jinja2 HTML rendering + git push to gh-pages
  templates/
    index.html.j2              # top 20 updates across all companies
    company_index.html.j2      # full history for one company, grouped by month
tools/
  search.py                    # CLI for querying the Chroma collection
docs/
  infographic.svg              # pipeline diagram
  plan.md                      # implementation plan
```

**Storage split**: SQLite handles deduplication and operational metadata; Chroma handles embeddings and semantic search. The two are cross-referenced via a `chroma_id` field (MD5 of the normalized URL).

## Chroma collection schema

The `product_updates` collection is also queried by [zeroclaw](https://github.com/zeroclaw-labs/zeroclaw). The schema is stable:

| Field | Type | Notes |
|---|---|---|
| `url` | string | canonical article URL |
| `company` | string | `cribl` or `ocient` |
| `category` | string | `blog`, `press`, `product` |
| `title` | string | article title |
| `scraped_at` | string | ISO 8601 |
| `published_date` | string | ISO 8601, may be empty |
| `summary` | string | LLM-generated summary |
| document text | string | truncated raw article text (embedded) |

## License

MIT
