# Implementation Plan: `product-update-digest`

## Environment Notes (pre-checked)
- Python 3.13 required — install via `sudo apt-get install python3.13 python3.13-venv python3.13-dev`
- Project uses a local venv at `.venv/` — set up with `make venv`
- Run tests with `make test` (uses `.venv/bin/pytest`) or `.venv/bin/pytest tests/ -v`
- All deps installed into `.venv/` — never rely on system-level packages

---

## Step 1 — Project Scaffolding

### Directory Layout
```
product-update-digest/
├── .env.example
├── .env                   # gitignored
├── .gitignore
├── requirements.txt
├── AGENTS.md
├── plan.md
├── config.py
├── main.py
├── summarizer.py
├── scrapers/
│   ├── __init__.py
│   ├── base.py
│   ├── cribl.py
│   └── ocient.py
├── storage/
│   ├── __init__.py
│   ├── models.py
│   ├── db.py              # SQLite client — URL tracking & deduplication
│   └── chroma_client.py   # Chroma client — vector storage & semantic search
├── publisher/
│   ├── __init__.py
│   ├── github_pages.py
│   └── templates/
│       ├── index.html.j2
│       └── company_index.html.j2
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_deduplication.py
│   ├── test_summarizer.py
│   ├── test_publisher.py
│   ├── test_cribl_scraper.py
│   ├── test_ocient_scraper.py
│   └── fixtures/
│       ├── cribl_blog_page.html
│       └── ocient_blog_page.html
├── data/
│   └── .gitkeep           # product_updates.db created here at runtime (gitignored)
└── logs/
    └── .gitkeep
```

### `requirements.txt`
```
langchain>=0.3,<0.4
langchain-openai>=0.2,<0.3         # OpenRouter is OpenAI-compatible
langchain-community>=0.3,<0.4
chromadb>=0.6,<1.0
pydantic>=2.11,<3
pydantic-settings>=2.10,<3
python-dotenv>=1.1,<2
httpx>=0.28,<1                     # primary HTTP client (sync + async, HTTP/2)
beautifulsoup4>=4.13,<5
lxml>=5.4,<6
Jinja2>=3.1,<4
gitpython>=3.1,<4
playwright>=1.49,<2                # fallback for JS-rendered pages
pytest>=8,<9
pytest-mock>=3.14,<4
respx>=0.22,<1                     # mock httpx in tests (replaces `responses`)
```

### `.env.example`
```
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

---

## Step 2 — `config.py`

Use `pydantic-settings` `BaseSettings` with a module-level `settings` singleton.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    openrouter_api_key: str
    openrouter_summarization_model: str = "anthropic/claude-sonnet-4-5"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    chroma_collection_name: str = "product_updates"
    sqlite_db_path: str = "data/product_updates.db"
    github_token: str
    github_repo: str
    github_pages_branch: str = "gh-pages"
    log_level: str = "INFO"

settings = Settings()
```

Note: Tests must monkeypatch env vars before import and force-reload `config` module.

---

## Step 3 — `storage/models.py`

Three Pydantic v2 models plus a URL normalization helper.

**`ScrapedPage`** — transient, in-memory only:
- `url`, `company`, `category`, `title`, `raw_text`, `content_hash`, `scraped_at`, `http_last_modified`, `published_date`
- `content_hash` = `sha256(raw_text.encode()).hexdigest()`, computed at scrape time
- `published_date` = article's own publish date (extracted from page), optional

**`ArticleRecord`** — stored in / retrieved from SQLite (operational tracking):
- `url`, `normalized_url`, `company`, `category`, `title`
- `first_scraped_at`, `last_scraped_at` (ISO 8601 strings)
- `content_hash` — used for change detection on re-scrape
- `published_date` (ISO 8601 string, nullable)
- `chroma_id` (nullable) — cross-reference to the Chroma document ID
- `status` — `"ok"` | `"error"` | `"skipped"`

`ArticleRecord.from_scraped_page(page)` classmethod for construction.

**`ProductUpdate`** — stored in / retrieved from Chroma (vector storage & semantic search):
- All metadata fields must be `str | int | float | bool` (Chroma constraint)
- Fields: `url`, `company`, `category`, `title`, `scraped_at`, `published_date`, `summary`
- `source_text` truncated to `MAX_SOURCE_TEXT_CHARS = 8000` (stored as document text, gets embedded)
- Does NOT hold `content_hash` — that lives authoritatively in SQLite

`ProductUpdate.from_scraped_page(page, summary)` classmethod handles:
- `datetime` → ISO 8601 string conversion
- `source_text` truncation
- Field mapping from `ScrapedPage`

**URL normalization** before any hashing or lookup:
- Lowercase scheme and host
- Strip fragments (`#...`)
- Strip trailing slashes
- Sort query parameters
- Normalize `http://` → `https://`

Helper: `normalize_url(url: str) -> str` in `models.py`.

**Chroma document ID**: `md5(normalize_url(url).encode()).hexdigest()` — stored as `chroma_id` in SQLite for cross-reference.

---

## Step 4a — `storage/chroma_client.py`

Responsible only for vector storage and semantic search — no deduplication logic here.

```python
class ProductUpdatesChromaClient:
    def __init__(self): ...          # creates HttpClient, gets/creates collection with embedding fn

    def upsert(self, update: ProductUpdate, chroma_id: str) -> None: ...
    def get_recent(self, company: str | None, limit: int) -> list[ProductUpdate]: ...
    def get_all(self, company: str | None = None) -> list[ProductUpdate]: ...
```

**Embedding function** via `chromadb.utils.embedding_functions.OpenAIEmbeddingFunction`:
```python
ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key=settings.openrouter_api_key,
    api_base="https://openrouter.ai/api/v1",
    model_name=settings.openrouter_embedding_model,
)
```

**⚠ Verification required**: OpenRouter's embedding endpoint compatibility varies by model. Before implementation, use `chub` to check whether `OpenAIEmbeddingFunction` works with OpenRouter's `/api/v1/embeddings` for the configured model. If not, implement a custom `EmbeddingFunction` subclass that calls the OpenRouter API directly via `httpx`.

**Query strategy**:
- `upsert`: called with the precomputed `chroma_id` (from SQLite's `ArticleRecord.chroma_id`)
- Feed queries: `collection.get(where={"company": company})` — metadata filter
- `get_recent`/`get_all`: sort by `published_date` (or `scraped_at`) in Python — Chroma has no ORDER BY

**Zeroclaw contract** (stable interface — document in README):

| Field | Value |
|---|---|
| Collection name | `product_updates` (from `CHROMA_COLLECTION_NAME` env var) |
| Embedding model | `openai/text-embedding-3-small` via OpenRouter (default) |
| Chroma host/port | `localhost:8000` |
| Document text | `source_text` (truncated raw content — this is what gets embedded) |
| Metadata fields | `url`, `company`, `category`, `title`, `scraped_at`, `published_date`, `summary` |
| Filter examples | `where={"company": "cribl"}`, `where={"category": "blog"}` |

---

## Step 4b — `storage/db.py`

SQLite client responsible for URL tracking and deduplication. Uses Python's built-in `sqlite3` — no extra dependency.

```python
class ArticleDB:
    def __init__(self, db_path: str): ...   # creates DB file and schema if needed

    def get_by_url(self, url: str) -> ArticleRecord | None: ...
    def upsert(self, record: ArticleRecord) -> None: ...
    def get_all(self, company: str | None = None) -> list[ArticleRecord]: ...
```

**SQLite schema** (`scraped_articles` table):
```sql
CREATE TABLE IF NOT EXISTS scraped_articles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT NOT NULL UNIQUE,
    normalized_url    TEXT NOT NULL UNIQUE,
    company           TEXT NOT NULL,
    category          TEXT NOT NULL,
    title             TEXT,
    first_scraped_at  TEXT NOT NULL,
    last_scraped_at   TEXT NOT NULL,
    content_hash      TEXT,
    published_date    TEXT,
    chroma_id         TEXT,
    status            TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_company ON scraped_articles(company);
CREATE INDEX IF NOT EXISTS idx_last_scraped ON scraped_articles(last_scraped_at);
```

**`get_by_url`**: looks up by `normalized_url` (not raw URL) so minor URL variations don't create duplicates.

**`upsert`**: INSERT OR REPLACE keyed on `normalized_url`. On update, preserves `first_scraped_at` from the existing row and updates `last_scraped_at`, `content_hash`, `title`, `chroma_id`, and `status`.

**Thread safety**: `sqlite3` connections are not thread-safe across threads; since this agent is single-threaded, a single shared connection is fine. Open with `check_same_thread=False` only if future async use is needed.

**`db_path`** comes from `settings.sqlite_db_path`. The `data/` directory is created on first run if it doesn't exist.

---

## Step 5 — `scrapers/base.py`

Abstract base class:

```python
class BaseScraper(ABC):
    company: str
    client: httpx.Client            # shared httpx client with retry transport
    _use_playwright: bool = False   # subclass sets True if site needs JS rendering

    def run(self, db: ArticleDB) -> list[ScrapedPage]: ...

    @abstractmethod
    def discover_urls(self) -> list[tuple[str, str]]: ...   # (url, category)

    @abstractmethod
    def scrape_page(self, url: str, category: str) -> ScrapedPage | None: ...

    def pre_check(self, url: str, existing: ArticleRecord) -> bool | None: ...
    def should_process(self, page: ScrapedPage, existing: ArticleRecord | None) -> bool: ...

    def _fetch_page(self, url: str) -> str: ...  # httpx first, Playwright fallback
```

**`_fetch_page(url)`** — two-tier fetch:
1. Try `self.client.get(url)` (httpx, fast, no JS)
2. If `self._use_playwright` is True, or if the response body is suspiciously small (< 200 chars of text after stripping tags), retry with Playwright:
   ```python
   with sync_playwright() as p:
       browser = p.chromium.launch(headless=True)
       page = browser.new_page()
       page.goto(url, wait_until="networkidle")
       html = page.content()
       browser.close()
   ```
3. Subclasses can set `_use_playwright = True` to always use Playwright for a site

**`run()` loop**:
1. Call `discover_urls()`
2. For each URL: check **SQLite** via `db.get_by_url(normalized_url)`
3. If not found: scrape and yield
4. If found: call `pre_check()` first (lightweight HEAD request or listing-page date)
   - Returns `True` → changed, proceed to full scrape
   - Returns `False` → skip
   - Returns `None` → inconclusive, fall through to full scrape + hash comparison
5. Full re-scrape, call `should_process()` (compares `content_hash` against `ArticleRecord.content_hash`), yield only if changed

**`pre_check()` default implementation**: HTTP HEAD request for `Last-Modified` or `ETag` header. Compares against `existing.last_scraped_at` from SQLite. Subclasses can override with site-specific logic (e.g., checking a date in the listing page HTML without fetching the full article).

**Resilience**:
- `httpx.Client` with retry transport: 3 retries, backoff 0.5s, retry on 429/500/502/503/504
- `scrape_page()` returns `None` on any exception (logged warning) — one bad page doesn't abort the run
- Configurable `_sleep_between_requests: float = 1.0` between page fetches
- Warn if `discover_urls()` returns 0 results (silent breakage guard)

**Playwright setup note**: Run `playwright install chromium` after `pip install playwright` — this downloads the browser binary. Add to deployment setup steps.

---

## Step 6 — `scrapers/cribl.py` and `scrapers/ocient.py`

### `scrapers/cribl.py`

**URL Discovery**:
- Blog/News: paginate `https://cribl.io/blog/` — stop when articles older than 30 days or no next-page link
- Press: paginate `https://cribl.io/press/`
- Product: static list of known URLs (e.g., `/stream/`, `/edge/`, `/lake/`)

**`should_process()` override**: check `Last-Modified` HTTP header first; fall back to hash comparison.

### `scrapers/ocient.py`

**URL Discovery**:
- Blog: paginate `https://ocient.com/blog/`
- News/Press: paginate `https://ocient.com/news/` (verify live URL)
- Product: static list

**`should_process()` override**: extract `<time>` or `<meta property="article:published_time">` as pre-check.

**Shared concern**: CSS selectors documented with validation date. Minimum content length check in `scrape_page()` — if extracted text < 200 chars, log a warning.

---

## Step 7 — `summarizer.py`

```python
class Summarizer:
    def __init__(self):
        self.llm = ChatOpenAI(
            model=settings.openrouter_summarization_model,
            openai_api_key=settings.openrouter_api_key,
            openai_api_base="https://openrouter.ai/api/v1",
        )
        self.chain = prompt_template | self.llm | StrOutputParser()

    def summarize(self, page: ScrapedPage) -> str: ...
```

**Prompt template**:
```
You are a technical analyst summarizing product updates.
Summarize the following {category} content from {company} in 2-4 sentences.
Focus on: what changed or was announced, why it matters, and any specific product names or versions.
Title: {title}
Content: {content}
Summary:
```

- Content truncated to ~6000 chars before sending (token budget guard)
- On LLM failure: return first 300 chars of `raw_text` as fallback, log error
- Instantiated once in `main.py`, reused across all pages in a run
- Note: in `langchain-openai >= 0.2`, the base URL param may be `base_url` not `openai_api_base` — verify with `chub` at implementation time

---

## Step 8 — `publisher/github_pages.py`

```python
class GitHubPagesPublisher:
    def publish(self) -> None:
        updates_by_company = self._fetch_all_updates()   # from Chroma
        html_files = self._render_html(updates_by_company)
        self._push_to_github(html_files)
```

**`_push_to_github`**:
1. Clone `gh-pages` branch into `tempfile.mkdtemp()`
2. Write all HTML files
3. `git add -A`, commit with timestamp, push
4. Clean up temp dir

**Jinja2 templates**:
- `index.html.j2`: top 20 updates across both companies, sorted by `published_date` descending (falls back to `scraped_at` if no publish date); cards with company badge, category tag, title (linked), date, summary
- `company_index.html.j2`: all updates for one company, grouped by month (using `published_date`); breadcrumb to root

**GitHub Pages repo structure**:
```
index.html
cribl/
  index.html
ocient/
  index.html
```

---

## Step 9 — `main.py`

```python
def main():
    setup_logging()
    db = ArticleDB(settings.sqlite_db_path)
    chroma = ProductUpdatesChromaClient()
    summarizer = Summarizer()
    publisher = GitHubPagesPublisher(db)   # reads from SQLite for feed ordering

    scrapers = [CriblScraper(), OcientScraper()]
    new_updates = []

    for scraper in scrapers:
        pages = scraper.run(db)            # dedup check hits SQLite
        for page in pages:
            summary = summarizer.summarize(page)
            chroma_id = chroma_id_for(page.url)   # md5(normalized_url)
            update = ProductUpdate.from_scraped_page(page, summary)
            chroma.upsert(update, chroma_id)       # store embedding + summary
            record = ArticleRecord.from_scraped_page(page, chroma_id)
            db.upsert(record)                      # store operational metadata
            new_updates.append(update)

    if new_updates:
        publisher.publish()

if __name__ == "__main__":
    main()
```

Note: `GitHubPagesPublisher` reads from SQLite (via `db.get_all()`) for ordering by `published_date`, then pulls the summary text from Chroma by `chroma_id` — or alternatively, summaries can be stored redundantly in SQLite to avoid the cross-DB join. Decide at implementation time based on simplicity preference.

- `--dry-run` flag via `argparse`: scrapes but skips LLM, Chroma writes, and GitHub push
- `--site {cribl,ocient}` flag: run only one scraper (useful for development and debugging)
- Logging to stdout + rotating file in `logs/agent.log`
- Publisher only called if `new_updates` is non-empty

---

## Step 10 — Tests

### Structure
```
tests/
├── conftest.py              # mock_settings, sample_scraped_page, in-memory SQLite db fixture
├── test_deduplication.py    # 3 cases: new URL / same hash / changed hash (SQLite-based)
├── test_db.py               # ArticleDB unit tests: upsert, get_by_url, URL normalization
├── test_summarizer.py       # mock ChatOpenAI, verify prompt rendering
├── test_publisher.py        # mock SQLite + Chroma data, verify HTML output
├── test_cribl_scraper.py    # respx mocks httpx calls, HTML fixtures
├── test_ocient_scraper.py
└── fixtures/
    ├── cribl_blog_page.html
    └── ocient_blog_page.html
```

**SQLite testing**: Use `ArticleDB(":memory:")` for an in-memory DB in tests — no temp files needed, fast and isolated per test.

Run with: `make test` or `.venv/bin/pytest tests/ -v`

---

## Step 11 — Deployment

### Chroma HTTP Server

**VPS** — systemd unit at `/etc/systemd/system/chroma.service`:
```ini
[Unit]
Description=Chroma Vector DB HTTP Server
After=network.target

[Service]
User=<user>
WorkingDirectory=/home/<user>/chroma-data
ExecStart=/home/<user>/.local/bin/chroma run --host 127.0.0.1 --port 8000 --path /home/<user>/chroma-data
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**WSL2 dev**: `chroma run --host localhost --port 8000 --path ~/chroma-data`

Security: bound to `127.0.0.1` since both this program and zeroclaw run on the same host. Port 8000 is not exposed publicly.

### Cron Job (VPS)

```cron
0 6 * * * cd /home/<user>/product-update-digest && .venv/bin/python main.py >> logs/cron.log 2>&1
```

Create venv (from the project directory):
```bash
make venv
```

Or manually:
```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Note: cron does not source `.bashrc`. The `cd` before the python command ensures pydantic-settings finds `.env` via relative path.

### GitHub Pages — First-Time Setup

```bash
git clone https://<token>@github.com/<repo>.git /tmp/pages-init
cd /tmp/pages-init
git checkout --orphan gh-pages
git rm -rf .
echo "<h1>Coming soon</h1>" > index.html
git add index.html
git commit -m "Init gh-pages"
git push origin gh-pages
```

---

## Architectural Trade-offs

| Decision | Choice | Alternative / Trade-off |
|---|---|---|
| URL deduplication & tracking | SQLite (`storage/db.py`) | Chroma can do metadata lookups but is wrong tool — SQLite is fast, exact, zero extra deps |
| Vector storage & semantic search | Chroma (`storage/chroma_client.py`) | Only handles embeddings + retrieval; no dedup responsibility |
| HTTP client | `httpx` (sync mode) with Playwright fallback | Modern, supports HTTP/2; Playwright adds ~150MB for Chromium binary |
| Chroma document ID | `md5(normalized_url)` stored as `chroma_id` in SQLite | Precomputed in `main.py`, passed to both `db.upsert()` and `chroma.upsert()` |
| Publisher data source | SQLite for ordering, Chroma for summaries | Or store summary in SQLite too (simpler, avoids cross-DB join) — decide at implementation |
| LangChain vs raw OpenAI SDK | LangChain (per spec) | Raw `openai` SDK would reduce dep weight; revisit if LangChain causes issues |
| HTML generation | Stateless full re-render | Noisy git history; optimize later with delta rendering if needed |
| Partial run failure | Naturally idempotent | SQLite and Chroma upserts are safe to re-run; publisher retries on next cron run |
| Feed ordering | `published_date` (article's own date) | Falls back to `scraped_at` if page has no extractable date |

---

## Critical Files (in order of importance)

1. `storage/db.py` — SQLite schema, URL normalization, deduplication logic
2. `storage/chroma_client.py` — embedding fn config, zeroclaw contract
3. `scrapers/base.py` — deduplication loop (SQLite-backed), Playwright fallback
4. `storage/models.py` — data contract between all components (`ArticleRecord`, `ProductUpdate`, `ScrapedPage`)
5. `config.py` — misconfiguration causes silent failures
6. `main.py` — orchestration order, dual-write to SQLite + Chroma, dry-run behavior
