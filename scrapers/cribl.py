import json
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

# Blog slugs to skip — off-topic for a product-update digest.
_BLOG_BLOCKLIST = ("cribl-edge", "company-culture")


class CriblScraper(BaseScraper):
    sources = [
        f"{_SITEMAP_URL} — blog posts and news releases",
        *_PRODUCT_URLS,
    ]
    exclusions = [
        *[f'blog URLs containing "{b}"' for b in _BLOG_BLOCKLIST],
        "articles with sitemap lastmod older than MAX_ARTICLE_AGE_DAYS days",
    ]

    company = "cribl"
    def __init__(self) -> None:
        super().__init__()
        # Populated during _discover_from_sitemap; used as date fallback in scrape_page.
        self._sitemap_lastmod: dict[str, str] = {}

    def discover_urls(self) -> list[tuple[str, Category]]:
        blog_urls, news_urls = self._discover_from_sitemap()
        seen: set[str] = set()
        urls: list[tuple[str, Category]] = []
        for u in blog_urls:
            if any(blocked in u for blocked in _BLOG_BLOCKLIST):
                continue
            if u not in seen:
                seen.add(u)
                urls.append((u, "blog"))
        for u in news_urls:
            if u not in seen:
                seen.add(u)
                urls.append((u, "press_release"))
        for u in _PRODUCT_URLS:
            if u not in seen:
                seen.add(u)
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
                lastmod_str = lastmod_el.text[:10]
                try:
                    if date.fromisoformat(lastmod_str) < cutoff:
                        continue
                except ValueError:
                    lastmod_str = ""
                if lastmod_str:
                    self._sitemap_lastmod[loc] = lastmod_str

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
            published_date = self._extract_date(soup) or self._sitemap_lastmod.get(url)
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
        # JSON-LD is the most reliable source on Next.js App Router pages.
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                for key in ("datePublished", "dateCreated"):
                    val = data.get(key)
                    if val:
                        return str(val)[:10]
            except (json.JSONDecodeError, AttributeError):
                pass
        for prop in ("article:published_time", "og:article:published_time"):
            meta = soup.find("meta", property=prop)
            if meta and meta.get("content"):
                return str(meta["content"])[:10]
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            return str(time_tag["datetime"])[:10]
        return None
