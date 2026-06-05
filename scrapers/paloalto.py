import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from config import settings
from scrapers.base import BaseScraper, Category
from storage.models import ScrapedPage

logger = logging.getLogger(__name__)

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

# Blog posts: scraped from the XSIAM tag page (static HTML, no JS required).
# The tag page lists XSIAM-tagged posts; blog post URLs don't contain "xsiam"
# in the slug, so sitemap URL-pattern filtering misses them entirely.
_BLOG_TAG_URL = "https://www.paloaltonetworks.com/blog/tag/xsiam/"

# Blog article URL pattern: full absolute URL, at least 3 path segments after
# /blog/ (category + slug), no /category/, /author/, or /tag/ in the path.
_BLOG_ARTICLE_RE = re.compile(
    r"^https://www\.paloaltonetworks\.com/blog/[^/]+/[^/]+/",
)
_BLOG_SKIP_RE = re.compile(r"/(category|author|tag)/")

# Press releases live in the main sitemap (flat urlset, ~5000 URLs);
# filter to /company/press/ paths that also contain "xsiam".
_MAIN_SITEMAP_URL = "https://www.paloaltonetworks.com/sitemap.xml"

_XSIAM_RE = re.compile(r"xsiam", re.IGNORECASE)

# Press: only /company/press/ URLs that also contain "xsiam"
_PRESS_URL_FILTER: Callable[[str], bool] = (
    lambda u: "/company/press/" in u and bool(_XSIAM_RE.search(u))
)


class PaloAltoScraper(BaseScraper):
    company = "xsiam"
    sources = [
        f"{_BLOG_TAG_URL} — XSIAM-tagged blog posts",
        f"{_MAIN_SITEMAP_URL} — press releases filtered for /company/press/ + XSIAM",
    ]
    exclusions: list[str] = []

    # Use browser UA — paloaltonetworks.com blocks generic bot UAs
    _user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self) -> None:
        super().__init__()
        self._sitemap_lastmod: dict[str, str] = {}

    def discover_urls(self) -> list[tuple[str, Category]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.max_article_age_days)).date()
        seen: set[str] = set()
        urls: list[tuple[str, Category]] = []

        for u in self._blog_urls_from_tag_page():
            if u not in seen:
                seen.add(u)
                urls.append((u, "blog"))

        for u in self._urls_from_source(_MAIN_SITEMAP_URL, cutoff, url_filter=_PRESS_URL_FILTER):
            if u not in seen:
                seen.add(u)
                urls.append((u, "press_release"))

        return urls

    def _blog_urls_from_tag_page(self) -> list[str]:
        """Scrape the XSIAM tag page for blog article URLs (renders without JS)."""
        try:
            html = self._fetch_with_httpx(_BLOG_TAG_URL)
        except Exception:
            logger.warning("xsiam: failed to fetch tag page %s", _BLOG_TAG_URL, exc_info=True)
            return []

        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        urls: list[str] = []

        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            if (
                _BLOG_ARTICLE_RE.match(href)
                and not _BLOG_SKIP_RE.search(href)
                and href not in seen
            ):
                seen.add(href)
                urls.append(href)

        logger.info("xsiam: discovered %d blog URL(s) from tag page", len(urls))
        return urls

    def _urls_from_source(
        self,
        url: str,
        cutoff: date,
        *,
        url_filter: Callable[[str], bool] | None = None,
    ) -> list[str]:
        """Fetch a URL that is either a sitemapindex or a urlset and return article URLs."""
        try:
            xml = self._fetch_with_httpx(url)
        except Exception:
            logger.warning("xsiam: failed to fetch %s", url, exc_info=True)
            return []

        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            logger.warning("xsiam: XML parse error for %s", url)
            return []

        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        if tag == "sitemapindex":
            return self._handle_sitemap_index(root, cutoff, url_filter=url_filter)
        return self._handle_urlset(root, url, cutoff, url_filter=url_filter)

    def _handle_sitemap_index(
        self,
        root: ET.Element,
        cutoff: date,
        *,
        url_filter: Callable[[str], bool] | None = None,
    ) -> list[str]:
        """Fetch each child sitemap referenced by a sitemapindex, filtering by lastmod."""
        ns = {"sm": _SITEMAP_NS}
        all_urls: list[str] = []

        for sitemap_el in root.findall("sm:sitemap", ns):
            loc_el = sitemap_el.find("sm:loc", ns)
            if loc_el is None or not loc_el.text:
                continue
            child_url = loc_el.text.strip()

            lastmod_el = sitemap_el.find("sm:lastmod", ns)
            if lastmod_el is not None and lastmod_el.text:
                try:
                    if date.fromisoformat(lastmod_el.text[:10]) < cutoff:
                        continue
                except ValueError:
                    pass

            child_urls = self._urls_from_source(child_url, cutoff, url_filter=url_filter)
            all_urls.extend(child_urls)

        return all_urls

    def _handle_urlset(
        self,
        root: ET.Element,
        source_url: str,
        cutoff: date,
        *,
        url_filter: Callable[[str], bool] | None = None,
    ) -> list[str]:
        """Parse a urlset, applying url_filter and date filter, recording lastmod for scrape_page fallback."""
        ns = {"sm": _SITEMAP_NS}
        urls: list[str] = []

        for url_el in root.findall("sm:url", ns):
            loc_el = url_el.find("sm:loc", ns)
            if loc_el is None or not loc_el.text:
                continue
            loc = loc_el.text.strip()

            if url_filter is not None and not url_filter(loc):
                continue

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

            urls.append(loc)

        logger.info("xsiam: %d URL(s) from %s (cutoff %s)", len(urls), source_url, cutoff)
        return urls

    def scrape_page(self, url: str, category: Category) -> ScrapedPage | None:
        try:
            html = self._fetch_page(url)
            soup = BeautifulSoup(html, "lxml")
            title = self._extract_title(soup)
            published_date = self._extract_date(soup) or self._sitemap_lastmod.get(url)
            text = self.extract_text(html)
            if len(text) < 200:
                logger.warning("xsiam: thin content (%d chars) at %s", len(text), url)
            return ScrapedPage(
                url=url,
                company="xsiam",
                category=category,
                title=title,
                raw_text=text,
                published_date=published_date,
            )
        except Exception:
            logger.warning("xsiam: failed to scrape %s", url, exc_info=True)
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
                return str(meta["content"])[:10]
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            return str(time_tag["datetime"])[:10]
        return None
