# product-update-digest

A daily cron job that scrapes news and blog posts from Cribl and Ocient (blog posts, press releases, product page changes), summarizes them with an LLM via [OpenRouter](https://openrouter.ai), publishes a static feed to GitHub Pages, and stores embeddings in a [sqlite-vec](https://github.com/asg017/sqlite-vec) vector database for retrieval by an AI assistant or the `tools/search.py` CLI.

## What it does

1. **Scrapes** Cribl and Ocient websites for new or changed content
2. **Deduplicates** using SQLite — skips unchanged content via URL tracking and SHA-256 content hashing
3. **Summarizes** each new item using a configurable LLM (default: `anthropic/claude-sonnet-4-5` via OpenRouter)
4. **Stores** summaries and vector embeddings in sqlite-vec for semantic search
5. **Publishes** a static HTML digest to GitHub Pages

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/) — fast Python package manager
- [OpenRouter](https://openrouter.ai) API key
- GitHub personal access token with `repo` scope (for pushing to GitHub Pages)

## Setup

```bash
# install uv (once per machine)
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/<you>/product-update-digest.git
cd product-update-digest
make venv
cp .env.example .env
# edit .env with your API keys and config
```

`make venv` runs `uv venv --python 3.13 && uv pip install -r requirements.txt`. No separate services required.

## Configuration

All configuration is via environment variables (`.env` file locally, system env in production):

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `OPENROUTER_SUMMARIZATION_MODEL` | LLM for summaries (default: `anthropic/claude-sonnet-4-5`) |
| `OPENROUTER_EMBEDDING_MODEL` | Embedding model (default: `openai/text-embedding-3-small`) |
| `EMBEDDING_DIMENSIONS` | Vector dimensions matching the embedding model (default: `1536`) |
| `SQLITE_DB_PATH` | Path to SQLite database (default: `data/product_updates.db`) |
| `GITHUB_TOKEN` | GitHub PAT for pushing to gh-pages |
| `GITHUB_REPO` | Target GitHub repo for Pages (e.g., `username/product-updates`) |
| `GITHUB_PAGES_BRANCH` | Branch to publish to (default: `gh-pages`) |
| `MAX_ARTICLE_AGE_DAYS` | How far back to index articles (default: `30`) |

## Usage

```bash
.venv/bin/python main.py                   # full run
.venv/bin/python main.py --dry-run         # scrape only, no LLM calls or publishing
.venv/bin/python main.py --dry-run --summarize  # scrape + free model summaries
.venv/bin/python main.py --site cribl      # run one scraper only
```

`--dry-run` writes a local `data/dry-run/index.html` you can open in a browser to preview what was found. Adding `--summarize` calls a free OpenRouter model (`OPENROUTER_DRY_RUN_SUMMARIZATION_MODEL`, default: `meta-llama/llama-3.3-70b-instruct:free`) to populate the summary cards — the only requirement is a valid `OPENROUTER_API_KEY` in your `.env` (no billing needed for free-tier models).

## Semantic search

```bash
.venv/bin/python tools/search.py
.venv/bin/python tools/search.py --company cribl
.venv/bin/python tools/search.py --results 10
```

## Running tests

```bash
make test
```

59 tests, no external services required (SQLite uses in-memory DB; HTTP calls are mocked).

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for full instructions covering GitHub Pages first-time initialization and cron job configuration.

## Architecture

![Pipeline diagram](docs/flow-diagram.svg)

```
main.py                        # orchestration entry point
config.py                      # pydantic-settings config
summarizer.py                  # LangChain summarization chain
scrapers/
  base.py                      # abstract scraper (dedup loop, retry logic)
  cribl.py                     # Cribl scraper (sitemap-based discovery)
  ocient.py                    # Ocient scraper (sitemap-based discovery)
storage/
  models.py                    # Pydantic models: ScrapedPage, ArticleRecord, ProductUpdate
  db.py                        # SQLite client (URL tracking, deduplication)
  vec_client.py                # sqlite-vec client (vector storage, semantic search)
publisher/
  github_pages.py              # Jinja2 HTML rendering + git push to gh-pages
  templates/
    index.html.j2              # top 20 updates across all companies
    company_index.html.j2      # full history for one company, grouped by month
tools/
  search.py                    # CLI for semantic search over the vector store
docs/
  plan.md                      # implementation plan
```

**Storage split**: SQLite handles deduplication and operational metadata; sqlite-vec handles embeddings and semantic search. Both live in the same `.db` file and are cross-referenced via a `vec_id` field (MD5 of the normalized URL).

## Vector store schema

The `vec_items` table (and the `vec_embeddings` virtual table alongside it) stores the following, also used by the `tools/search.py` CLI and any external consumers:

| Field | Type | Notes |
|---|---|---|
| `id` | string | MD5 of normalized URL (`vec_id_for(url)`) |
| `url` | string | canonical article URL |
| `company` | string | `cribl` or `ocient` |
| `category` | string | `blog`, `press_release`, or `product` |
| `title` | string | article title |
| `scraped_at` | string | ISO 8601 |
| `published_date` | string | ISO 8601, nullable |
| `summary` | string | LLM-generated summary |
| `source_text` | string | truncated raw article text (what gets embedded) |

## Design Notes

Scrapers use the sitemap.xml files for URL discovery rather than scraping listing pages, which avoids JS-rendered pagination and gives reliable `lastmod` dates for pre-filtering old articles. All HTTP fetching uses httpx only — no headless browser needed.

Vector storage uses sqlite-vec (a ~163KB SQLite extension) instead of a separate Chroma server. This eliminates the need for a running HTTP service and reduces the venv from ~500MB to ~260MB by dropping onnxruntime, numpy, kubernetes, and grpcio.

Moving to uv dropped the .venv size to a mere 118 MB.

## License

MIT
