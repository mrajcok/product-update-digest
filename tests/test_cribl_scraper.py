"""Tests for scrapers/cribl.py — mocks _fetch_page to avoid network/Playwright."""
from pathlib import Path
import pytest
from bs4 import BeautifulSoup

from scrapers.cribl import CriblScraper

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def scraper():
    s = CriblScraper()
    yield s
    s.close()


class TestExtractTitle:
    def test_prefers_og_title(self, scraper):
        html = '<html><head><meta property="og:title" content="OG Title"/></head><body><h1>H1 Title</h1></body></html>'
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
        html = '<html><head><meta property="article:published_time" content="2026-03-15T12:00:00Z"/></head></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_date(soup) == "2026-03-15"

    def test_extracts_time_datetime_attribute(self, scraper):
        html = '<html><body><time datetime="2026-04-01T00:00:00Z">April 1, 2026</time></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_date(soup) == "2026-04-01"

    def test_returns_none_when_no_date(self, scraper):
        soup = BeautifulSoup("<html><body><p>No date here</p></body></html>", "lxml")
        assert scraper._extract_date(soup) is None


class TestScrapePageWithFixture:
    def test_scrapes_article_fixture(self, scraper, mocker):
        html = _load("cribl_article.html")
        mocker.patch.object(scraper, "_fetch_page", return_value=html)
        page = scraper.scrape_page("https://cribl.io/blog/cribl-stream-4-0-released/", "blog")
        assert page is not None
        assert page.company == "cribl"
        assert page.category == "blog"
        assert page.title == "Cribl Stream 4.0 Released"
        assert page.published_date == "2026-03-15"
        assert len(page.raw_text) > 50
        assert page.content_hash != ""

    def test_returns_none_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", side_effect=Exception("network error"))
        result = scraper.scrape_page("https://cribl.io/blog/fail/", "blog")
        assert result is None


class TestDiscoverFromSitemap:
    _SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://cribl.io/blog/cribl-stream-4-0-released/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://cribl.io/blog/cribl-edge/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://cribl.io/blog/company-culture/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://cribl.io/news/press-release-1/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://cribl.io/unrelated/page/</loc><lastmod>2026-05-01</lastmod></url>
</urlset>"""

    def test_returns_blog_and_news_urls(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=self._SITEMAP_XML)
        blog_urls, news_urls = scraper._discover_from_sitemap()
        assert "https://cribl.io/blog/cribl-stream-4-0-released/" in blog_urls
        assert "https://cribl.io/news/press-release-1/" in news_urls
        assert "https://cribl.io/unrelated/page/" not in blog_urls
        assert "https://cribl.io/unrelated/page/" not in news_urls

    def test_no_duplicates_in_results(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=self._SITEMAP_XML)
        blog_urls, news_urls = scraper._discover_from_sitemap()
        assert len(blog_urls) == len(set(blog_urls))
        assert len(news_urls) == len(set(news_urls))

    def test_returns_empty_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", side_effect=Exception("network error"))
        blog_urls, news_urls = scraper._discover_from_sitemap()
        assert blog_urls == []
        assert news_urls == []
