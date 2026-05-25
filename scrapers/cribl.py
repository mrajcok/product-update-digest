import logging
import re
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup

from config import settings
from scrapers.base import BaseScraper, Category
from storage.models import ScrapedPage

logger = logging.getLogger(__name__)

# cribl.io is a Next.js App Router SPA. The blog/news listing pages render post
# cards via client-side JS with no <a href> links, so scraping the listing is
# unreliable. The sitemap is server-generated and is the authoritative URL source.
_SITEMAP_URL = "https://cribl.io/sitemap.xml"

_PRODUCT_URLS = [
    "https://cribl.io/products/stream/",
    "https://cribl.io/products/lake/",
    "https://cribl.io/products/search/",
]

_BLOG_RE = re.compile(r"^https://cribl\.io/blog/[^/]+/")
_NEWS_RE = re.compile(r"^https://cribl\.io/news/[^/]+/")

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


class CriblScraper(BaseScraper):
    company = "cribl"
    _use_playwright = False  # Next.js App Router — content is SSR'd into initial HTML

    def discover_urls(self) -> list[tuple[str, Category]]:
        blog_urls, news_urls = self._discover_from_sitemap()
        urls: list[tuple[str, Category]] = []
        for u in blog_urls:
            urls.append((u, "blog"))
        for u in news_urls:
            urls.append((u, "press_release"))
        for u in _PRODUCT_URLS:
            urls.append((u, "product"))
        return urls

    def _discover_from_sitemap(self) -> tuple[list[str], list[str]]:
        try:
            xml = self._fetch_with_httpx(_SITEMAP_URL)
        except Exception:
            logger.warning("cribl: failed to fetch sitemap %s", _SITEMAP_URL, exc_info=True)
            return [], []

        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            logger.warning("cribl: sitemap XML parse error")
            return [], []

        ns = {"sm": _SITEMAP_NS}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.max_article_age_days)).date()
        blog_urls: list[str] = []
        news_urls: list[str] = []

        for url_el in root.findall("sm:url", ns):
            loc_el = url_el.find("sm:loc", ns)
            if loc_el is None or not loc_el.text:
                continue
            loc = loc_el.text.strip()

            lastmod_el = url_el.find("sm:lastmod", ns)
            if lastmod_el is not None and lastmod_el.text:
                try:
                    if date.fromisoformat(lastmod_el.text[:10]) < cutoff:
                        continue
                except ValueError:
                    pass

            if _BLOG_RE.match(loc):
                blog_urls.append(loc)
            elif _NEWS_RE.match(loc):
                news_urls.append(loc)

        logger.info("cribl: discovered %d blog(s), %d news URL(s) from sitemap (cutoff %s)", len(blog_urls), len(news_urls), cutoff)
        return blog_urls, news_urls

    def scrape_page(self, url: str, category: Category) -> ScrapedPage | None:
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
