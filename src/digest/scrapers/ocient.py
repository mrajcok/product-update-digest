import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from digest.config import settings
from digest.scrapers.base import BaseScraper, Category
from digest.storage.db import ArticleDB
from digest.storage.models import ScrapedPage

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

_PRODUCT_URLS: list[str] = []

_ARTICLE_CONTENT_SELS = [
    "div.single-content", "article", "div.entry-content", "div.post-content", "main",
]

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


_BLOG_BLOCKLIST = ("employee-spotlight",)

# docs.ocient.com serves docs pages as raw Markdown at the ".md" URL variant
# (Mintlify convention) — no HTML parsing needed. Every release on this page
# is its own "## X.Y" heading with nothing else on the line, newest first, so
# splitting on that heading pattern gives one section of text per release.
_RELEASE_NOTES_URL = "https://docs.ocient.com/ocientaiq-unified-data-platform-release-notes.md"
_RELEASE_HEADING_RE = re.compile(r"^## (\d+(?:\.\d+)*)\s*$", re.MULTILINE)

# The page defines MDX macros up top ('export const Parquet = "Apache® Parquet™";')
# and references them inline as "{Parquet}" — left unresolved, these leak into
# both the LLM summary and the stored/embedded text as literal "{Parquet}".
_MDX_CONST_RE = re.compile(r'export const (\w+) = "([^"]*)";')


def _parse_version(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0,)


def _resolve_mdx_constants(text: str, constants: dict[str, str]) -> str:
    if not constants:
        return text
    return re.sub(r"\{(\w+)\}", lambda m: constants.get(m.group(1), m.group(0)), text)


def _clean_markdown_escapes(text: str) -> str:
    """Strip Mintlify's backslash-escaping of literal _ [ ] characters — noise, not content."""
    return text.replace("\\_", "_").replace("\\[", "[").replace("\\]", "]")


def _parse_release_sections(markdown_text: str) -> dict[str, str]:
    """Split release-notes markdown into {version: section_text} on '## X.Y' headings."""
    constants = dict(_MDX_CONST_RE.findall(markdown_text))
    matches = list(_RELEASE_HEADING_RE.finditer(markdown_text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown_text)
        section_text = markdown_text[start:end].strip()
        section_text = _resolve_mdx_constants(section_text, constants)
        section_text = _clean_markdown_escapes(section_text)
        sections[m.group(1)] = section_text
    return sections


def _release_url(version: str) -> str:
    # A query param (not a #fragment) so normalize_url() keeps each release
    # as a distinct, dedup-able URL — normalize_url() strips fragments.
    return f"{_RELEASE_NOTES_URL}?release={version}"


def _version_from_url(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("release")
    return values[0] if values else None


class OcientScraper(BaseScraper):
    company = "ocient"

    def __init__(self) -> None:
        super().__init__()
        self._sitemap_lastmod: dict[str, str] = {}
        self._release_sections: dict[str, str] = {}
        self._db: ArticleDB | None = None

    sources = [
        f"{_BLOG_SITEMAP_URL} — blog posts",
        f"{_NEWS_SITEMAP_URL} — press releases",
        f"{_RELEASE_NOTES_URL} — release notes",
        *_PRODUCT_URLS,
    ]
    exclusions = [
        *[f'blog URLs containing "{b}"' for b in _BLOG_BLOCKLIST]
    ]

    _user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def run(self, db: ArticleDB, limit: int | None = None, category: str | None = None) -> list[ScrapedPage]:
        # Stash db so discover_urls() (called with no args by BaseScraper.run)
        # can check which releases have already been scraped and summarized.
        self._db = db
        return super().run(db, limit=limit, category=category)

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
        for u, cat in self._discover_release_notes():
            if u not in seen:
                seen.add(u)
                urls.append((u, cat))
        return urls

    def _discover_release_notes(self) -> list[tuple[str, Category]]:
        """Return (url, category) for each release newer than what's already stored."""
        try:
            markdown_text = self._fetch_with_httpx(_RELEASE_NOTES_URL)
        except Exception:
            logger.warning("ocient: failed to fetch release notes %s", _RELEASE_NOTES_URL, exc_info=True)
            return []

        self._release_sections = _parse_release_sections(markdown_text)
        if not self._release_sections:
            logger.warning("ocient: no release sections found at %s", _RELEASE_NOTES_URL)
            return []

        known_versions = self._known_release_versions()
        if known_versions:
            latest_known = max(known_versions, key=_parse_version)
            new_versions = [
                v for v in self._release_sections
                if _parse_version(v) > _parse_version(latest_known)
            ]
        else:
            # Nothing recorded yet — bootstrap on just the newest release
            # instead of backfilling the entire release history at once.
            latest_known = None
            new_versions = [max(self._release_sections, key=_parse_version)]

        if not new_versions:
            logger.debug("ocient: release notes unchanged, latest known is %s", latest_known)
            return []

        logger.info(
            "ocient: %d new release(s) found (latest known: %s): %s",
            len(new_versions), latest_known or "none",
            ", ".join(sorted(new_versions, key=_parse_version)),
        )
        return [(_release_url(v), "release_notes") for v in new_versions]

    def _known_release_versions(self) -> set[str]:
        if self._db is None:
            return set()
        records = self._db.get_all(company="ocient", category="release_notes")
        return {v for r in records if (v := _version_from_url(r.url)) is not None}

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

            lastmod_el = url_el.find("sm:lastmod", ns)
            if lastmod_el is not None and lastmod_el.text:
                lastmod_str = lastmod_el.text[:10]
                try:
                    if cutoff is not None and date.fromisoformat(lastmod_str) < cutoff:
                        continue
                except ValueError:
                    lastmod_str = ""
                if lastmod_str:
                    self._sitemap_lastmod[loc] = lastmod_str

            urls.append(loc)

        logger.info("ocient: discovered %d URL(s) from %s (cutoff %s)", len(urls), sitemap_url, cutoff)
        return urls

    def scrape_page(self, url: str, category: Category) -> ScrapedPage | None:
        if category == "release_notes":
            return self._scrape_release_note(url)
        try:
            return self._scrape_article(url, category)
        except Exception:
            logger.warning("ocient: failed to scrape %s", url, exc_info=True)
            return None

    def _scrape_release_note(self, url: str) -> ScrapedPage | None:
        version = _version_from_url(url)
        if version is None:
            logger.warning("ocient: malformed release-notes URL %s", url)
            return None

        text = self._release_sections.get(version)
        if text is None:
            # Cache miss — e.g. scrape_page() called without a prior
            # discover_urls() in this instance. Fetch and re-parse.
            try:
                markdown_text = self._fetch_with_httpx(_RELEASE_NOTES_URL)
            except Exception:
                logger.warning("ocient: failed to fetch release notes %s", _RELEASE_NOTES_URL, exc_info=True)
                return None
            self._release_sections = _parse_release_sections(markdown_text)
            text = self._release_sections.get(version)

        if text is None:
            logger.warning("ocient: release %s no longer found on release notes page", version)
            return None

        return ScrapedPage(
            url=url,
            company="ocient",
            category="release_notes",
            title=f"OcientAIQ Unified Data Platform Release Notes — {version}",
            raw_text=text,
        )

    def _scrape_article(self, url: str, category: Category) -> ScrapedPage | None:
        html = self._fetch_page(url)
        soup = BeautifulSoup(html, "lxml")
        title = self._extract_title(soup)
        published_date = self._extract_date(soup) or self._sitemap_lastmod.get(url)

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
