import logging
import sqlite3
from pathlib import Path

from storage.models import ArticleRecord, normalize_url

logger = logging.getLogger(__name__)

_SCHEMA = """
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
    vec_id         TEXT,
    summary           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_company       ON scraped_articles(company);
CREATE INDEX IF NOT EXISTS idx_last_scraped  ON scraped_articles(last_scraped_at);
"""

_UPSERT_SQL = """
INSERT INTO scraped_articles
    (url, normalized_url, company, category, title,
     first_scraped_at, last_scraped_at, content_hash,
     published_date, vec_id, summary, status)
VALUES
    (:url, :normalized_url, :company, :category, :title,
     :first_scraped_at, :last_scraped_at, :content_hash,
     :published_date, :vec_id, :summary, :status)
ON CONFLICT(normalized_url) DO UPDATE SET
    url             = excluded.url,
    title           = excluded.title,
    last_scraped_at = excluded.last_scraped_at,
    content_hash    = excluded.content_hash,
    published_date  = excluded.published_date,
    vec_id       = excluded.vec_id,
    summary         = excluded.summary,
    status          = excluded.status
    -- first_scraped_at intentionally preserved on conflict
"""


def _row_to_record(row: sqlite3.Row) -> ArticleRecord:
    return ArticleRecord(
        url=row["url"],
        normalized_url=row["normalized_url"],
        company=row["company"],
        category=row["category"],
        title=row["title"] or "",
        first_scraped_at=row["first_scraped_at"],
        last_scraped_at=row["last_scraped_at"],
        content_hash=row["content_hash"] or "",
        published_date=row["published_date"],
        vec_id=row["vec_id"],
        summary=row["summary"] or "",
        status=row["status"],
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """One-time column rename: chroma_id → vec_id for databases created before the sqlite-vec migration."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scraped_articles)")}
    if "chroma_id" in cols and "vec_id" not in cols:
        conn.execute("ALTER TABLE scraped_articles RENAME COLUMN chroma_id TO vec_id")
        logger.info("DB migration: renamed column chroma_id → vec_id")


class ArticleDB:
    """SQLite-backed store for scrape history and deduplication state."""

    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        _migrate(self._conn)
        self._conn.commit()
        logger.info("ArticleDB opened at %r", db_path)

    def get_by_url(self, url: str) -> ArticleRecord | None:
        """Look up by normalized URL so minor variations don't create duplicates."""
        nurl = normalize_url(url)
        row = self._conn.execute(
            "SELECT * FROM scraped_articles WHERE normalized_url = ?", (nurl,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def upsert(self, record: ArticleRecord) -> None:
        self._conn.execute(_UPSERT_SQL, record.model_dump())
        self._conn.commit()
        logger.debug("DB upsert: url=%s status=%s", record.url, record.status)

    def get_all(self, company: str | None = None) -> list[ArticleRecord]:
        if company:
            rows = self._conn.execute(
                "SELECT * FROM scraped_articles WHERE company = ? ORDER BY last_scraped_at DESC",
                (company,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM scraped_articles ORDER BY last_scraped_at DESC"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ArticleDB":
        return self

    def __exit__(self, *_) -> None:
        self.close()
