"""Tests for scrapers/paloalto.py — mocks _fetch_with_httpx/_fetch_page to avoid network calls."""
from pathlib import Path
import pytest
from bs4 import BeautifulSoup

from scrapers.paloalto import PaloAltoScraper, _PRESS_URL_FILTER

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def scraper():
    s = PaloAltoScraper()
    yield s
    s.close()


_FLAT_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.paloaltonetworks.com/company/press/2025/palo-alto-networks-cortex-xsiam-update</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://www.paloaltonetworks.com/company/press/2025/some-other-announcement</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://www.paloaltonetworks.com/blog/2026/03/xsiam-autonomous-soc/</loc><lastmod>2026-05-01</lastmod></url>
</urlset>"""

_SITEMAP_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://www.paloaltonetworks.com/blog/sitemap-2026-05.xml</loc>
    <lastmod>2026-05-31</lastmod>
  </sitemap>
  <sitemap>
    <loc>https://www.paloaltonetworks.com/blog/sitemap-2024-01.xml</loc>
    <lastmod>2024-01-31</lastmod>
  </sitemap>
</sitemapindex>"""

_TAG_PAGE_HTML = """<!DOCTYPE html>
<html><body>
  <a href="https://www.paloaltonetworks.com/blog/security-operations/xsiam-new-feature/">XSIAM Feature</a>
  <a href="https://www.paloaltonetworks.com/blog/security-operations/autonomous-soc-update/">Autonomous SOC</a>
  <a href="https://www.paloaltonetworks.com/blog/corporate/">Corporate</a>
  <a href="https://www.paloaltonetworks.com/blog/security-operations/category/product-features/">Category link</a>
  <a href="https://www.paloaltonetworks.com/blog/author/greg-smith/">Author link</a>
  <a href="/blog/tag/xsiam/">Tag link</a>
</body></html>"""


class TestExtractTitle:
    def test_prefers_og_title(self, scraper):
        html = '<html><head><meta property="og:title" content="OG Title"/></head><body><h1>H1</h1></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_title(soup) == "OG Title"

    def test_falls_back_to_h1(self, scraper):
        html = "<html><head></head><body><h1>H1 Title</h1></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_title(soup) == "H1 Title"

    def test_falls_back_to_title_tag(self, scraper):
        html = "<html><head><title>Page Title</title></head><body></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_title(soup) == "Page Title"

    def test_returns_empty_string_when_nothing_found(self, scraper):
        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        assert scraper._extract_title(soup) == ""


class TestExtractDate:
    def test_extracts_article_published_time(self, scraper):
        html = '<html><head><meta property="article:published_time" content="2026-03-20T10:00:00Z"/></head></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_date(soup) == "2026-03-20"

    def test_extracts_time_datetime_attribute(self, scraper):
        html = '<html><body><time datetime="2026-04-01T00:00:00Z">April 1, 2026</time></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_date(soup) == "2026-04-01"

    def test_returns_none_when_no_date(self, scraper):
        soup = BeautifulSoup("<html><body><p>No date here</p></body></html>", "lxml")
        assert scraper._extract_date(soup) is None


class TestScrapePageWithFixture:
    def test_scrapes_article_fixture(self, scraper, mocker):
        html = _load("paloalto_article.html")
        mocker.patch.object(scraper, "_fetch_page", return_value=html)
        page = scraper.scrape_page(
            "https://www.paloaltonetworks.com/blog/security-operations/xsiam-autonomous-soc/",
            "blog",
        )
        assert page is not None
        assert page.company == "xsiam"
        assert page.category == "blog"
        assert page.title == "Cortex XSIAM 3.0: Autonomous SOC Now Available"
        assert page.published_date == "2026-03-20"
        assert len(page.raw_text) > 50
        assert page.content_hash != ""

    def test_returns_none_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", side_effect=Exception("network error"))
        result = scraper.scrape_page(
            "https://www.paloaltonetworks.com/blog/security-operations/xsiam-autonomous-soc/",
            "blog",
        )
        assert result is None


class TestPressUrlFilter:
    def test_accepts_press_xsiam_urls(self):
        assert _PRESS_URL_FILTER(
            "https://www.paloaltonetworks.com/company/press/2025/palo-alto-networks-cortex-xsiam-delivers"
        )

    def test_rejects_blog_xsiam_urls(self):
        assert not _PRESS_URL_FILTER(
            "https://www.paloaltonetworks.com/blog/security-operations/xsiam-update/"
        )

    def test_rejects_press_non_xsiam_urls(self):
        assert not _PRESS_URL_FILTER(
            "https://www.paloaltonetworks.com/company/press/2025/some-other-announcement"
        )


class TestBlogUrlsFromTagPage:
    def test_extracts_article_urls(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_TAG_PAGE_HTML)
        urls = scraper._blog_urls_from_tag_page()
        assert "https://www.paloaltonetworks.com/blog/security-operations/xsiam-new-feature/" in urls
        assert "https://www.paloaltonetworks.com/blog/security-operations/autonomous-soc-update/" in urls

    def test_skips_category_author_tag_links(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_TAG_PAGE_HTML)
        urls = scraper._blog_urls_from_tag_page()
        assert not any("/category/" in u or "/author/" in u or "/tag/" in u for u in urls)

    def test_skips_short_blog_paths(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_TAG_PAGE_HTML)
        urls = scraper._blog_urls_from_tag_page()
        assert "https://www.paloaltonetworks.com/blog/corporate/" not in urls

    def test_no_duplicates(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_TAG_PAGE_HTML)
        urls = scraper._blog_urls_from_tag_page()
        assert len(urls) == len(set(urls))

    def test_returns_empty_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", side_effect=Exception("network error"))
        assert scraper._blog_urls_from_tag_page() == []


class TestHandleUrlset:
    def test_filters_press_xsiam_urls(self, scraper, mocker):
        from datetime import date
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_FLAT_SITEMAP_XML)
        urls = scraper._urls_from_source(
            "https://www.paloaltonetworks.com/sitemap.xml",
            date(2026, 1, 1),
            url_filter=_PRESS_URL_FILTER,
        )
        assert "https://www.paloaltonetworks.com/company/press/2025/palo-alto-networks-cortex-xsiam-update" in urls
        assert "https://www.paloaltonetworks.com/company/press/2025/some-other-announcement" not in urls
        assert "https://www.paloaltonetworks.com/blog/2026/03/xsiam-autonomous-soc/" not in urls

    def test_returns_all_when_no_filter(self, scraper, mocker):
        from datetime import date
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_FLAT_SITEMAP_XML)
        urls = scraper._urls_from_source(
            "https://www.paloaltonetworks.com/sitemap.xml",
            date(2026, 1, 1),
        )
        assert len(urls) == 3

    def test_returns_empty_on_fetch_error(self, scraper, mocker):
        from datetime import date
        mocker.patch.object(scraper, "_fetch_with_httpx", side_effect=Exception("network error"))
        assert scraper._urls_from_source("https://www.paloaltonetworks.com/sitemap.xml", date(2026, 1, 1)) == []


class TestHandleSitemapIndex:
    def test_skips_old_child_sitemaps(self, scraper, mocker):
        from datetime import date
        fetch_calls: list[str] = []

        def fetch_side_effect(url):
            fetch_calls.append(url)
            if "sitemap_index" in url:
                return _SITEMAP_INDEX_XML
            return _FLAT_SITEMAP_XML

        mocker.patch.object(scraper, "_fetch_with_httpx", side_effect=fetch_side_effect)
        scraper._urls_from_source(
            "https://www.paloaltonetworks.com/blog/sitemap_index.xml",
            date(2026, 1, 1),
        )
        fetched_children = [u for u in fetch_calls if "sitemap_index" not in u]
        assert all("2024" not in u for u in fetched_children)
