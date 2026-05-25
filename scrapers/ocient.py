import logging
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from config import settings
from scrapers.base import BaseScraper, Category
from storage.models import ScrapedPage

logger = logging.getLogger(__name__)

# ocient.com is WordPress — static HTML is sufficient for article pages.
# The blog listing uses an AJAX "Load More" button, so discovery via the
# listing page only returns the first batch. The WordPress Yoast sitemap index
# is the authoritative URL source and avoids that limitation entirely.
#
# Flywheel (the WordPress host) blocks non-browser User-Agents on sitemap/XML
# paths, so a realistic browser UA is required (_user_agent override below).

_BLOG_SITEMAP_URL = "https://ocient.com/blog_post-sitemap.xml"
_NEWS_SITEMAP_URL = "https://ocient.com/news_release-sitemap.xml"

_PRODUCT_URLS = [
    "https://ocient.com/platform/",
    "https://ocient.com/solutions/",
]

_ARTICLE_CONTENT_SELS = ["article", "div.entry-content", "div.post-content", "main"]

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


_BLOG_BLOCKLIST = ("employee-spotlight",)


class OcientScraper(BaseScraper):
    company = "ocient"
    sources = [
        f"{_BLOG_SITEMAP_URL} — blog posts",
        f"{_NEWS_SITEMAP_URL} — press releases",
        *_PRODUCT_URLS,
    ]
    exclusions = [
        *[f'blog URLs containing "{b}"' for b in _BLOG_BLOCKLIST],
        "articles with sitemap lastmod older than MAX_ARTICLE_AGE_DAYS days",
    ]

    _user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def discover_urls(self) -> list[tuple[str, Category]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.max_article_age_days)).date()
        seen: set[str] = set()
        urls: list[tuple[str, Category]] = []
        for u in self._urls_from_sitemap(_BLOG_SITEMAP_URL, cutoff):
            if any(blocked in u for blocked in _BLOG_BLOCKLIST):
                continue
            if u not in seen:
                seen.add(u)
                urls.append((u, "blog"))
        for u in self._urls_from_sitemap(_NEWS_SITEMAP_URL, cutoff):
            if u not in seen:
                seen.add(u)
                urls.append((u, "press_release"))
        for u in _PRODUCT_URLS:
            if u not in seen:
                seen.add(u)
                urls.append((u, "product"))
        return urls

    def _urls_from_sitemap(self, sitemap_url: str, cutoff: date | None = None) -> list[str]:
        try:
            xml = self._fetch_with_httpx(sitemap_url)
        except Exception:
            logger.warning("ocient: failed to fetch sitemap %s", sitemap_url, exc_info=True)
            return []

        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            logger.warning("ocient: sitemap XML parse error for %s", sitemap_url)
            return []

        ns = {"sm": _SITEMAP_NS}
        urls = []
        for url_el in root.findall("sm:url", ns):
            loc_el = url_el.find("sm:loc", ns)
            if loc_el is None or not loc_el.text:
                continue
            loc = loc_el.text.strip()

            if cutoff is not None:
                lastmod_el = url_el.find("sm:lastmod", ns)
                if lastmod_el is not None and lastmod_el.text:
                    try:
                        if date.fromisoformat(lastmod_el.text[:10]) < cutoff:
                            continue
                    except ValueError:
                        pass

            urls.append(loc)

        logger.info("ocient: discovered %d URL(s) from %s (cutoff %s)", len(urls), sitemap_url, cutoff)
        return urls

    def scrape_page(self, url: str, category: Category) -> ScrapedPage | None:
        try:
            return self._scrape_article(url, category)
        except Exception:
            logger.warning("ocient: failed to scrape %s", url, exc_info=True)
            return None

    def _scrape_article(self, url: str, category: Category) -> ScrapedPage | None:
        html = self._fetch_page(url)
        soup = BeautifulSoup(html, "lxml")
        title = self._extract_title(soup)
        published_date = self._extract_date(soup)

        content_html = ""
        for sel in _ARTICLE_CONTENT_SELS:
            tag = soup.select_one(sel)
            if tag:
                content_html = str(tag)
                break
        if not content_html:
            content_html = html

        text = self.extract_text(content_html)
        if len(text) < 200:
            logger.warning("ocient: thin content (%d chars) at %s", len(text), url)
        return ScrapedPage(
            url=url,
            company="ocient",
            category=category,
            title=title,
            raw_text=text,
            published_date=published_date,
        )

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
        meta = soup.find("meta", property="article:published_time")
        if meta and meta.get("content"):
            return str(meta["content"])[:10]
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            return str(time_tag["datetime"])[:10]
        return None
