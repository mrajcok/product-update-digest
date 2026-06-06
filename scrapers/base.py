import logging
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx
from bs4 import BeautifulSoup

from config import settings
from storage.db import ArticleDB
from storage.models import ArticleRecord, ScrapedPage

Category = Literal["blog", "press_release", "product"]

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _is_too_old(published_date: str | None, cutoff: date) -> bool:
    """Return True if published_date is known and older than cutoff. None = unknown = keep."""
    if not published_date:
        return False
    try:
        return date.fromisoformat(published_date) < cutoff
    except ValueError:
        return False


class BaseScraper(ABC):
    company: str
    _sleep_between_requests: float = 1.0
    _user_agent: str = "product-update-digest/1.0"

    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": self._user_agent},
            follow_redirects=True,
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, db: ArticleDB, limit: int | None = None, category: str | None = None) -> list[ScrapedPage]:
        """Discover URLs, deduplicate via SQLite, return pages needing summarization."""
        urls = self.discover_urls()
        if not urls:
            logger.warning(
                "%s: discover_urls() returned 0 URLs — site structure may have changed",
                self.company,
            )
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.max_article_age_days)).date()

        results: list[ScrapedPage] = []
        for i, (url, url_category) in enumerate(urls, 1):
            if category and url_category != category:
                continue
            if limit is not None and len(results) >= limit:
                break
            logger.info("%s: [%d/%d] %s", self.company, i, len(urls), url)
            try:
                page = self._process_url(url, url_category, db)
                if page is not None:
                    if _is_too_old(page.published_date, cutoff):
                        logger.debug(
                            "%s: skipping article older than %d days (%s) %s",
                            self.company, settings.max_article_age_days, page.published_date, url,
                        )
                        continue
                    results.append(page)
            except Exception:
                logger.exception("%s: unexpected error processing %s", self.company, url)
            time.sleep(self._sleep_between_requests)

        logger.info("%s: %d new/changed page(s) from %d discovered URL(s)", self.company, len(results), len(urls))
        return results

    # ------------------------------------------------------------------
    # Abstract interface for subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    def discover_urls(self) -> list[tuple[str, Category]]:
        """Return list of (url, category) tuples to scrape."""

    @abstractmethod
    def scrape_page(self, url: str, category: Category) -> ScrapedPage | None:
        """Fetch and parse one page into a ScrapedPage. Return None on failure."""

    # ------------------------------------------------------------------
    # Deduplication hooks — override in subclasses for site-specific logic
    # ------------------------------------------------------------------

    def pre_check(self, url: str, existing: ArticleRecord) -> bool | None:
        """
        Lightweight change check before a full re-scrape.

        Returns:
            True  — definitely changed, proceed to full scrape
            False — definitely unchanged, skip
            None  — inconclusive, fall through to full scrape + hash comparison
        """
        try:
            resp = self.client.head(url)
            last_modified = resp.headers.get("last-modified")
            if last_modified:
                lm_dt = parsedate_to_datetime(last_modified)
                last_scraped = datetime.fromisoformat(existing.last_scraped_at)
                if last_scraped.tzinfo is None:
                    last_scraped = last_scraped.replace(tzinfo=timezone.utc)
                return lm_dt > last_scraped
        except Exception:
            logger.debug("%s: pre_check HEAD failed for %s", self.company, url)
        return None

    def should_process(self, page: ScrapedPage, existing: ArticleRecord | None) -> bool:
        """Compare content hash to decide whether changed content needs re-summarization."""
        if existing is None:
            return True
        return page.content_hash != existing.content_hash

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_url(self, url: str, category: Category, db: ArticleDB) -> ScrapedPage | None:
        existing = db.get_by_url(url)

        if existing is None:
            return self._safe_scrape(url, category)

        changed = self.pre_check(url, existing)
        if changed is False:
            logger.debug("%s: unchanged (pre_check), skipping %s", self.company, url)
            return None
        if changed is True:
            logger.debug("%s: changed (pre_check), scraping %s", self.company, url)
            return self._safe_scrape(url, category)

        # pre_check inconclusive — full re-scrape + hash comparison
        page = self._safe_scrape(url, category)
        if page is None:
            return None
        if self.should_process(page, existing):
            return page
        logger.debug("%s: unchanged (hash), skipping %s", self.company, url)
        return None

    def _safe_scrape(self, url: str, category: Category) -> ScrapedPage | None:
        """Call scrape_page and catch all exceptions so one failure doesn't abort the run."""
        try:
            return self.scrape_page(url, category)
        except Exception:
            logger.warning("%s: scrape_page failed for %s", self.company, url, exc_info=True)
            return None

    def _fetch_page(self, url: str) -> str:
        return self._fetch_with_httpx(url)

    def _fetch_with_httpx(self, url: str) -> str:
        delay = 0.5
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(3):
            try:
                resp = self.client.get(url)
                if resp.status_code in _RETRY_STATUSES:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp.text
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < 2:
                    logger.debug("%s: attempt %d failed (%s), retrying in %.1fs", self.company, attempt + 1, exc, delay)
                    time.sleep(delay)
                    delay *= 2
        raise last_exc

    @staticmethod
    def extract_text(html: str) -> str:
        """Strip nav/footer/script noise and return plain text from HTML."""
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, *_) -> None:
        self.close()
