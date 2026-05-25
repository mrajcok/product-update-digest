import logging
from datetime import datetime

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from storage.models import ScrapedPage

logger = logging.getLogger(__name__)

# Selectors validated against ocient.com on 2026-05-24.
# ocient.com is WordPress — content is available in static HTML.
# Pagination on listing pages uses an AJAX "Load More" button; only the initial
# page load is scraped here. Enable Playwright (_use_playwright = True) and
# click the button if deeper coverage is needed in the future.

_BLOG_LISTING_URL = "https://ocient.com/blog/"
_NEWSROOM_LISTING_URL = "https://ocient.com/newsroom/"

_PRODUCT_URLS = [
    "https://ocient.com/platform/",
    "https://ocient.com/solutions/",
]

# Listing page card selectors
_BLOG_CARD_SEL = "a.preview-wrapper.blog-wrapper.resource-wrapper"
_NEWS_CARD_SEL = "a.preview-wrapper.in_the_news-wrapper.resource-wrapper"

# Individual article selectors
_ARTICLE_CONTENT_SELS = ["article", "div.entry-content", "div.post-content", "main"]
_DATE_CARD_SEL = "div.preview-card--source-link span"


class OcientScraper(BaseScraper):
    company = "ocient"
    _use_playwright = False  # WordPress; static HTML sufficient for initial page load

    def discover_urls(self) -> list[tuple[str, str]]:
        urls: list[tuple[str, str]] = []
        urls.extend(self._discover_blog())
        urls.extend(self._discover_newsroom())
        urls.extend((url, "product") for url in _PRODUCT_URLS)
        return urls

    def _discover_blog(self) -> list[tuple[str, str]]:
        try:
            html = self._fetch_page(_BLOG_LISTING_URL)
        except Exception:
            logger.warning("ocient: failed to fetch blog listing")
            return []

        soup = BeautifulSoup(html, "lxml")
        found: list[tuple[str, str]] = []
        for card in soup.select(_BLOG_CARD_SEL):
            href = card.get("href", "")
            if not href:
                continue
            full_url = f"https://ocient.com{href}" if href.startswith("/") else href
            found.append((full_url, "blog"))

        logger.info("ocient: discovered %d blog URL(s)", len(found))
        return found

    def _discover_newsroom(self) -> list[tuple[str, str]]:
        # "In the News" cards link to external press coverage (third-party articles).
        # We record the external URL as the canonical reference but only scrape
        # the metadata visible in the card (title + date) — see scrape_page().
        try:
            html = self._fetch_page(_NEWSROOM_LISTING_URL)
        except Exception:
            logger.warning("ocient: failed to fetch newsroom listing")
            return []

        soup = BeautifulSoup(html, "lxml")
        found: list[tuple[str, str]] = []
        for card in soup.select(_NEWS_CARD_SEL):
            href = card.get("href", "")
            if href:
                found.append((href, "press_release"))

        logger.info("ocient: discovered %d newsroom item(s)", len(found))
        return found

    def scrape_page(self, url: str, category: str) -> ScrapedPage | None:
        try:
            if category == "press_release" and "ocient.com" not in url:
                return self._scrape_news_card_from_listing(url)
            return self._scrape_article(url, category)
        except Exception:
            logger.warning("ocient: failed to scrape %s", url, exc_info=True)
            return None

    def _scrape_article(self, url: str, category: str) -> ScrapedPage | None:
        html = self._fetch_page(url)
        soup = BeautifulSoup(html, "lxml")
        title = self._extract_title(soup)
        published_date = self._extract_date(soup)

        # Try progressively broader content containers
        content_html = ""
        for sel in _ARTICLE_CONTENT_SELS:
            tag = soup.select_one(sel)
            if tag:
                content_html = str(tag)
                break
        if not content_html:
            content_html = html  # fall back to full page

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

    def _scrape_news_card_from_listing(self, external_url: str) -> ScrapedPage | None:
        # For external press links we re-fetch the newsroom listing and find the
        # matching card rather than attempting to scrape a third-party article.
        try:
            html = self._fetch_page(_NEWSROOM_LISTING_URL)
        except Exception:
            return None

        soup = BeautifulSoup(html, "lxml")
        for card in soup.select(_NEWS_CARD_SEL):
            if card.get("href") != external_url:
                continue
            title_tag = card.select_one("div.preview-card--title p")
            title = title_tag.get_text(strip=True) if title_tag else external_url
            date_tag = card.select_one(_DATE_CARD_SEL)
            published_date = self._parse_card_date(date_tag.get_text(strip=True) if date_tag else "")
            # Use the card title + source as the content to embed
            source_tag = card.select_one("div.col--source p")
            source = source_tag.get_text(strip=True) if source_tag else ""
            raw_text = f"{title}. {source}. Source: {external_url}"
            return ScrapedPage(
                url=external_url,
                company="ocient",
                category="press_release",
                title=title,
                raw_text=raw_text,
                published_date=published_date,
            )
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
        # WordPress standard OG meta
        meta = soup.find("meta", property="article:published_time")
        if meta and meta.get("content"):
            return str(meta["content"])[:10]
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            return str(time_tag["datetime"])[:10]
        return None

    @staticmethod
    def _parse_card_date(text: str) -> str | None:
        # Newsroom card date format: "May 11, 2026"
        try:
            return datetime.strptime(text.strip(), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            return None
