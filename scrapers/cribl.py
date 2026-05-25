import logging
import re
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from storage.models import ScrapedPage

logger = logging.getLogger(__name__)

# Selectors / patterns validated against cribl.io on 2026-05-24.
# cribl.io is a Next.js SPA — all content is JS-rendered; Playwright is required.

_BLOG_LISTING_URL = "https://cribl.io/blog/"
_NEWS_LISTING_URL = "https://cribl.io/newsroom/"

_PRODUCT_URLS = [
    "https://cribl.io/stream/",
    "https://cribl.io/edge/",
    "https://cribl.io/lake/",
    "https://cribl.io/search/",
]

# Only follow links that look like individual article pages (not tag/category pages).
_BLOG_HREF_RE = re.compile(r"^/blog/[^/]+/$")
_NEWS_HREF_RE = re.compile(r"^/news/[^/]+/$")

_MAX_LISTING_PAGES = 5  # pagination cap; Cribl uses /blog/page/N/ URL pattern


class CriblScraper(BaseScraper):
    company = "cribl"
    _use_playwright = True  # Next.js SPA; httpx returns a skeleton shell

    def discover_urls(self) -> list[tuple[str, str]]:
        urls: list[tuple[str, str]] = []
        urls.extend(self._discover_listing(_BLOG_LISTING_URL, "blog", _BLOG_HREF_RE))
        urls.extend(self._discover_listing(_NEWS_LISTING_URL, "press_release", _NEWS_HREF_RE))
        urls.extend((url, "product") for url in _PRODUCT_URLS)
        return urls

    def _discover_listing(
        self,
        base_url: str,
        category: str,
        href_re: re.Pattern,
    ) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        seen: set[str] = set()

        for page_num in range(1, _MAX_LISTING_PAGES + 1):
            listing_url = base_url if page_num == 1 else f"{base_url}page/{page_num}/"
            try:
                html = self._fetch_page(listing_url)
            except Exception:
                logger.debug("cribl: listing page not available, stopping: %s", listing_url)
                break

            soup = BeautifulSoup(html, "lxml")
            links = soup.find_all("a", href=href_re)
            if not links:
                break

            new_this_page = 0
            for a in links:
                href: str = a["href"]
                full_url = f"https://cribl.io{href}" if href.startswith("/") else href
                if full_url not in seen:
                    seen.add(full_url)
                    found.append((full_url, category))
                    new_this_page += 1

            if new_this_page == 0:
                break  # duplicate page — pagination exhausted

        logger.info("cribl: discovered %d %s URL(s)", len(found), category)
        return found

    def scrape_page(self, url: str, category: str) -> ScrapedPage | None:
        try:
            html = self._fetch_page(url)
            soup = BeautifulSoup(html, "lxml")
            title = self._extract_title(soup)
            published_date = self._extract_date(soup)
            text = self.extract_text(html)
            if len(text) < 200:
                logger.warning("cribl: thin content (%d chars) at %s", len(text), url)
            return ScrapedPage(
                url=url,
                company="cribl",
                category=category,
                title=title,
                raw_text=text,
                published_date=published_date,
            )
        except Exception:
            logger.warning("cribl: failed to scrape %s", url, exc_info=True)
            return None

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str:
        # OG title is most reliable on Next.js (set server-side)
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return str(og["content"]).strip()
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        title_tag = soup.find("title")
        return title_tag.get_text(strip=True) if title_tag else ""

    @staticmethod
    def _extract_date(soup: BeautifulSoup) -> str | None:
        for prop in ("article:published_time", "og:article:published_time"):
            meta = soup.find("meta", property=prop)
            if meta and meta.get("content"):
                return str(meta["content"])[:10]  # YYYY-MM-DD
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            return str(time_tag["datetime"])[:10]
        return None
