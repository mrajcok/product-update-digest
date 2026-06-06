"""Tests for scrapers/ocient.py — mocks _fetch_page to avoid network calls."""
from pathlib import Path
import pytest
from bs4 import BeautifulSoup

from digest.scrapers.ocient import OcientScraper

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def scraper():
    s = OcientScraper()
    yield s
    s.close()


class TestExtractTitle:
    def test_prefers_og_title(self, scraper):
        html = '<html><head><meta property="og:title" content="OG Title"/></head><body><h1>H1</h1></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_title(soup) == "OG Title"

    def test_falls_back_to_h1(self, scraper):
        html = "<html><head></head><body><h1>Article Heading</h1></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_title(soup) == "Article Heading"


class TestExtractDate:
    def test_extracts_article_published_time(self, scraper):
        html = '<html><head><meta property="article:published_time" content="2026-04-20T09:00:00Z"/></head></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_date(soup) == "2026-04-20"

    def test_extracts_time_tag(self, scraper):
        html = '<html><body><time datetime="2026-05-01T00:00:00+00:00">May 1, 2026</time></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._extract_date(soup) == "2026-05-01"

    def test_returns_none_when_no_date(self, scraper):
        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        assert scraper._extract_date(soup) is None


class TestUrlsFromSitemap:
    _SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ocient.com/blog/ocient-new-feature/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://ocient.com/blog/employee-spotlight/</loc><lastmod>2026-05-01</lastmod></url>
  <url><loc>https://ocient.com/news/press-release-1/</loc><lastmod>2026-05-01</lastmod></url>
</urlset>"""

    def test_returns_urls_from_sitemap(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=self._SITEMAP_XML)
        urls = scraper._urls_from_sitemap("https://ocient.com/blog_post-sitemap.xml")
        assert "https://ocient.com/blog/ocient-new-feature/" in urls
        assert "https://ocient.com/blog/employee-spotlight/" in urls

    def test_returns_empty_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", side_effect=Exception("network error"))
        assert scraper._urls_from_sitemap("https://ocient.com/blog_post-sitemap.xml") == []

    def test_returns_empty_on_xml_parse_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value="not xml")
        assert scraper._urls_from_sitemap("https://ocient.com/blog_post-sitemap.xml") == []


class TestScrapeArticle:
    def test_scrapes_ocient_article(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", return_value=_load("ocient_article.html"))
        page = scraper.scrape_page("https://ocient.com/blog/ocient-announces-new-feature/", "blog")
        assert page is not None
        assert page.company == "ocient"
        assert page.title == "Ocient Announces New Hyperscale Feature"
        assert page.published_date == "2026-04-20"
        assert len(page.raw_text) > 50
        assert page.content_hash != ""

    def test_returns_none_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", side_effect=Exception("network error"))
        result = scraper.scrape_page("https://ocient.com/blog/fail/", "blog")
        assert result is None
