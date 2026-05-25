import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from bs4 import BeautifulSoup

from storage.db import ArticleDB
from storage.models import ArticleRecord, ScrapedPage

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MIN_CONTENT_CHARS = 200  # below this, httpx response is assumed JS-gated


class BaseScraper(ABC):
    company: str
    _use_playwright: bool = False      # set True in subclass to always use Playwright
    _sleep_between_requests: float = 1.0

    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": "product-update-digest/1.0"},
            follow_redirects=True,
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, db: ArticleDB) -> list[ScrapedPage]:
        """Discover URLs, deduplicate via SQLite, return pages needing summarization."""
        urls = self.discover_urls()
        if not urls:
            logger.warning(
                "%s: discover_urls() returned 0 URLs — site structure may have changed",
                self.company,
            )
            return []

        results: list[ScrapedPage] = []
        for url, category in urls:
            try:
                page = self._process_url(url, category, db)
                if page is not None:
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
    def discover_urls(self) -> list[tuple[str, str]]:
        """Return list of (url, category) tuples to scrape."""

    @abstractmethod
    def scrape_page(self, url: str, category: str) -> ScrapedPage | None:
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

    def _process_url(self, url: str, category: str, db: ArticleDB) -> ScrapedPage | None:
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

    def _safe_scrape(self, url: str, category: str) -> ScrapedPage | None:
        """Call scrape_page and catch all exceptions so one failure doesn't abort the run."""
        try:
            return self.scrape_page(url, category)
        except Exception:
            logger.warning("%s: scrape_page failed for %s", self.company, url, exc_info=True)
            return None

    def _fetch_page(self, url: str) -> str:
        """
        Fetch page HTML. Tries httpx first; falls back to Playwright when the
        response looks JS-gated (thin content) or _use_playwright is forced.
        """
        html = self._fetch_with_httpx(url)
        if not self._use_playwright:
            text_preview = BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)
            if len(text_preview) >= _MIN_CONTENT_CHARS:
                return html
            logger.debug(
                "%s: httpx returned thin content (%d chars), retrying with Playwright: %s",
                self.company, len(text_preview), url,
            )
        return self._fetch_with_playwright(url)

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

    def _fetch_with_playwright(self, url: str) -> str:
        from playwright.sync_api import sync_playwright  # lazy — not installed until needed
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                pw_page = browser.new_page()
                pw_page.goto(url, wait_until="networkidle", timeout=30_000)
                html = pw_page.content()
            finally:
                browser.close()
        return html

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
