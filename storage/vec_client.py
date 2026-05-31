import logging
import sqlite3
import time
from pathlib import Path

import sqlite_vec
from openai import OpenAI
from tenacity import Retrying, before_sleep_log, stop_after_attempt, wait_exponential

from config import settings
from storage.models import ProductUpdate

logger = logging.getLogger(__name__)

_CREATE_ITEMS = """
CREATE TABLE IF NOT EXISTS vec_items (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    company      TEXT NOT NULL,
    category     TEXT NOT NULL,
    title        TEXT NOT NULL,
    scraped_at   TEXT NOT NULL,
    published_date TEXT,
    summary      TEXT NOT NULL DEFAULT '',
    source_text  TEXT NOT NULL DEFAULT ''
);
"""

_CREATE_EMBEDDINGS = """
CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
    id TEXT PRIMARY KEY,
    embedding float[{dims}]
);
"""


def _open_conn(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


class VecClient:
    """Vector storage and semantic search backed by sqlite-vec."""

    def __init__(self, db_path: str | None = None) -> None:
        path = db_path or settings.sqlite_db_path
        self._conn = _open_conn(path)
        self._conn.executescript(_CREATE_ITEMS)
        self._conn.executescript(_CREATE_EMBEDDINGS.format(dims=settings.embedding_dimensions))
        self._conn.commit()
        self._openai = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        logger.info("VecClient opened at %r", path)

    def _embed(self, text: str) -> list[float]:
        t0 = time.monotonic()
        for attempt in Retrying(
            stop=stop_after_attempt(settings.max_api_retries),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                resp = self._openai.embeddings.create(
                    input=text,
                    model=settings.openrouter_embedding_model,
                )
                embedding = resp.data[0].embedding
                logger.info(
                    "embed done in %.1fs — input %d chars, output %d dims, model %s",
                    time.monotonic() - t0, len(text), len(embedding), settings.openrouter_embedding_model,
                )
                return embedding
        raise AssertionError("unreachable: tenacity reraise=True always raises on exhaustion")

    def upsert(self, update: ProductUpdate, vec_id: str) -> None:
        # vec0 virtual tables don't support ON CONFLICT, so delete + insert.
        self._conn.execute("DELETE FROM vec_embeddings WHERE id = ?", (vec_id,))
        self._conn.execute("DELETE FROM vec_items WHERE id = ?", (vec_id,))

        embedding = self._embed(update.source_text)

        self._conn.execute(
            """INSERT INTO vec_items (id, url, company, category, title, scraped_at, published_date, summary, source_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vec_id, update.url, update.company, update.category, update.title,
             update.scraped_at, update.published_date, update.summary, update.source_text),
        )
        self._conn.execute(
            "INSERT INTO vec_embeddings (id, embedding) VALUES (?, ?)",
            (vec_id, sqlite_vec.serialize_float32(embedding)),
        )
        self._conn.commit()
        logger.debug("VecClient upsert: id=%s url=%s", vec_id, update.url)

    def search(self, query: str, company: str | None = None, n_results: int = 5) -> list[tuple[ProductUpdate, float]]:
        query_vec = self._embed(query)
        company_filter = "AND vi.company = :company" if company else ""
        sql = f"""
            WITH knn AS (
                SELECT id, distance
                FROM vec_embeddings
                WHERE embedding MATCH :vec AND k = :k
            )
            SELECT vi.*, knn.distance
            FROM knn
            JOIN vec_items vi ON vi.id = knn.id
            {company_filter}
            ORDER BY knn.distance
            LIMIT :limit
        """
        params: dict = {
            "vec": sqlite_vec.serialize_float32(query_vec),
            "k": n_results * 10,  # fetch extra so company filter doesn't starve results
            "limit": n_results,
        }
        if company:
            params["company"] = company

        rows = self._conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            update = ProductUpdate(
                url=row["url"],
                company=row["company"],
                category=row["category"],
                title=row["title"],
                scraped_at=row["scraped_at"],
                published_date=row["published_date"],
                summary=row["summary"],
                source_text=row["source_text"],
            )
            results.append((update, row["distance"]))
        return results

    def get_all(self, company: str | None = None) -> list[ProductUpdate]:
        if company:
            rows = self._conn.execute(
                "SELECT * FROM vec_items WHERE company = ? ORDER BY published_date DESC, scraped_at DESC",
                (company,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM vec_items ORDER BY published_date DESC, scraped_at DESC"
            ).fetchall()
        return [
            ProductUpdate(
                url=r["url"], company=r["company"], category=r["category"],
                title=r["title"], scraped_at=r["scraped_at"],
                published_date=r["published_date"], summary=r["summary"],
                source_text=r["source_text"],
            )
            for r in rows
        ]

    def count(self, company: str | None = None) -> int:
        if company:
            return self._conn.execute(
                "SELECT COUNT(*) FROM vec_items WHERE company = ?", (company,)
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM vec_items").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "VecClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()
