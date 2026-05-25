import hashlib
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, model_validator

MAX_SOURCE_TEXT_CHARS = 8000


def normalize_url(url: str) -> str:
    """Normalize a URL for consistent deduplication across minor variations."""
    parsed = urlparse(url)
    scheme = "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query)))
    return urlunparse((scheme, netloc, path, "", query, ""))


def vec_id_for(url: str) -> str:
    """Deterministic vector-store document ID derived from the normalized URL."""
    return hashlib.md5(normalize_url(url).encode()).hexdigest()


class ScrapedPage(BaseModel):
    """Transient in-memory representation of a freshly scraped page."""

    url: str
    company: Literal["cribl", "ocient"]
    category: Literal["blog", "press_release", "product"]
    title: str
    raw_text: str
    scraped_at: datetime = None  # type: ignore[assignment]
    content_hash: str = ""
    http_last_modified: str | None = None
    published_date: str | None = None  # ISO 8601, extracted from page HTML

    @model_validator(mode="after")
    def _set_defaults(self) -> "ScrapedPage":
        if self.scraped_at is None:
            self.scraped_at = datetime.now(timezone.utc)
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.raw_text.encode()).hexdigest()
        return self


class ArticleRecord(BaseModel):
    """Operational record stored in SQLite — tracks scrape history and deduplication state."""

    url: str
    normalized_url: str
    company: Literal["cribl", "ocient"]
    category: Literal["blog", "press_release", "product"]
    title: str
    first_scraped_at: str  # ISO 8601
    last_scraped_at: str   # ISO 8601
    content_hash: str
    published_date: str | None = None
    vec_id: str | None = None
    summary: str = ""
    status: Literal["ok", "error", "skipped"] = "ok"

    @classmethod
    def from_scraped_page(
        cls,
        page: ScrapedPage,
        vec_id: str | None = None,
        first_scraped_at: str | None = None,
        summary: str = "",
    ) -> "ArticleRecord":
        now = page.scraped_at.isoformat()
        return cls(
            url=page.url,
            normalized_url=normalize_url(page.url),
            company=page.company,
            category=page.category,
            title=page.title,
            first_scraped_at=first_scraped_at or now,
            last_scraped_at=now,
            content_hash=page.content_hash,
            published_date=page.published_date,
            vec_id=vec_id,
            summary=summary,
            status="ok",
        )


class ProductUpdate(BaseModel):
    """Document stored in sqlite-vec — used for vector search and summary retrieval."""

    url: str
    company: Literal["cribl", "ocient"]
    category: Literal["blog", "press_release", "product"]
    title: str
    scraped_at: str       # ISO 8601 string (Chroma metadata must be str/int/float/bool)
    published_date: str | None = None
    summary: str
    source_text: str      # truncated to MAX_SOURCE_TEXT_CHARS; this is what gets embedded

    @classmethod
    def from_scraped_page(cls, page: ScrapedPage, summary: str) -> "ProductUpdate":
        return cls(
            url=page.url,
            company=page.company,
            category=page.category,
            title=page.title,
            scraped_at=page.scraped_at.isoformat(),
            published_date=page.published_date,
            summary=summary,
            source_text=page.raw_text[:MAX_SOURCE_TEXT_CHARS],
        )
