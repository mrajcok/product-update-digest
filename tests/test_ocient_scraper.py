"""Tests for scrapers/ocient.py — mocks _fetch_page to avoid network calls."""
from pathlib import Path
import pytest
from bs4 import BeautifulSoup

from scrapers.ocient import OcientScraper

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


class TestParseCardDate:
    def test_parses_month_day_year(self):
        assert OcientScraper._parse_card_date("May 11, 2026") == "2026-05-11"
        assert OcientScraper._parse_card_date("April 10, 2026") == "2026-04-10"
        assert OcientScraper._parse_card_date("January 1, 2026") == "2026-01-01"

    def test_returns_none_for_bad_format(self):
        assert OcientScraper._parse_card_date("not a date") is None
        assert OcientScraper._parse_card_date("") is None


class TestDiscoverBlog:
    def test_finds_blog_cards(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", return_value=_load("ocient_blog_listing.html"))
        urls = scraper._discover_blog()
        hrefs = [u for u, _ in urls]
        assert "https://ocient.com/blog/ocient-announces-new-feature/" in hrefs
        assert "https://ocient.com/blog/building-the-data-foundation/" in hrefs
        assert all(cat == "blog" for _, cat in urls)

    def test_returns_empty_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", side_effect=Exception("network error"))
        assert scraper._discover_blog() == []


class TestDiscoverNewsroom:
    def test_finds_external_news_links(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", return_value=_load("ocient_newsroom.html"))
        urls = scraper._discover_newsroom()
        hrefs = [u for u, _ in urls]
        assert "https://techcrunch.com/2026/05/01/ocient-partnership/" in hrefs
        assert "https://venturebeat.com/2026/04/10/ocient-ai/" in hrefs
        assert all(cat == "press_release" for _, cat in urls)

    def test_returns_empty_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", side_effect=Exception("network error"))
        assert scraper._discover_newsroom() == []


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

    def test_external_press_link_returns_none_when_card_not_found(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", return_value=_load("ocient_newsroom.html"))
        result = scraper.scrape_page("https://unknown.com/article/", "press_release")
        assert result is None

    def test_external_press_link_extracts_card_metadata(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_page", return_value=_load("ocient_newsroom.html"))
        page = scraper.scrape_page("https://techcrunch.com/2026/05/01/ocient-partnership/", "press_release")
        assert page is not None
        assert page.title == "Ocient Partners with Major Cloud Provider"
        assert page.published_date == "2026-05-01"
        assert page.company == "ocient"
