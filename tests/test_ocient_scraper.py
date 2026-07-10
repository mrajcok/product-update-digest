"""Tests for scrapers/ocient.py — mocks _fetch_page to avoid network calls."""
from pathlib import Path
import pytest
from bs4 import BeautifulSoup

from digest.scrapers.ocient import (
    OcientScraper,
    _parse_release_sections,
    _parse_version,
    _release_url,
    _version_from_url,
)
from digest.storage.db import ArticleDB
from digest.storage.models import ArticleRecord, ScrapedPage

FIXTURES = Path(__file__).parent / "fixtures"

_SAMPLE_RELEASE_NOTES_MD = """# OcientAIQ Unified Data Platform Release Notes

export const Ocient = "Ocient®";
export const Parquet = "Apache® Parquet™";

## 27.1

**Release Highlights**

* New feature A for 27.1, powered by {Ocient} and {Parquet}.
* Ticket \\[DB-1234\\] fixed for STRIP\\_FIELD\\_QUOTES.

## 27.0

**Release Highlights**

* New feature B for 27.0.

## 26.1

**Release Highlights**

* Older feature C.
"""


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def _seed_known_release(db: ArticleDB, version: str) -> None:
    page = ScrapedPage(
        url=_release_url(version),
        company="ocient",
        category="release_notes",
        title=f"OcientAIQ Unified Data Platform Release Notes — {version}",
        raw_text="seed content",
    )
    db.upsert(ArticleRecord.from_scraped_page(page, summary="seed summary"))


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


class TestParseReleaseSections:
    def test_splits_into_versions(self):
        sections = _parse_release_sections(_SAMPLE_RELEASE_NOTES_MD)
        assert set(sections) == {"27.1", "27.0", "26.1"}
        assert "New feature A for 27.1" in sections["27.1"]
        assert "New feature B for 27.0" in sections["27.0"]
        assert "Older feature C" in sections["26.1"]

    def test_resolves_mdx_constant_macros(self):
        sections = _parse_release_sections(_SAMPLE_RELEASE_NOTES_MD)
        assert "{Ocient}" not in sections["27.1"]
        assert "{Parquet}" not in sections["27.1"]
        assert "powered by Ocient® and Apache® Parquet™" in sections["27.1"]

    def test_strips_markdown_escape_backslashes(self):
        sections = _parse_release_sections(_SAMPLE_RELEASE_NOTES_MD)
        assert "\\[" not in sections["27.1"]
        assert "\\_" not in sections["27.1"]
        assert "[DB-1234]" in sections["27.1"]
        assert "STRIP_FIELD_QUOTES" in sections["27.1"]

    def test_no_headings_returns_empty(self):
        assert _parse_release_sections("no release headings here") == {}


class TestVersionHelpers:
    def test_parse_version_orders_correctly(self):
        assert _parse_version("27.1") > _parse_version("27.0")
        assert _parse_version("27.0") > _parse_version("26.1")

    def test_release_url_roundtrips_through_version_from_url(self):
        assert _version_from_url(_release_url("27.1")) == "27.1"

    def test_version_from_url_none_for_unrelated_url(self):
        assert _version_from_url("https://ocient.com/blog/post/") is None


class TestDiscoverReleaseNotes:
    def test_bootstraps_on_latest_only_when_nothing_known(self, scraper, db, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_SAMPLE_RELEASE_NOTES_MD)
        scraper._db = db
        assert scraper._discover_release_notes() == [(_release_url("27.1"), "release_notes")]

    def test_returns_only_versions_newer_than_known(self, scraper, db, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_SAMPLE_RELEASE_NOTES_MD)
        _seed_known_release(db, "27.0")
        scraper._db = db
        assert scraper._discover_release_notes() == [(_release_url("27.1"), "release_notes")]

    def test_skips_older_releases_never_seen(self, scraper, db, mocker):
        """26.1 predates the known watermark (27.0) and must not be (re-)proposed."""
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_SAMPLE_RELEASE_NOTES_MD)
        _seed_known_release(db, "27.0")
        scraper._db = db
        results = scraper._discover_release_notes()
        assert _release_url("26.1") not in [u for u, _ in results]

    def test_returns_empty_when_up_to_date(self, scraper, db, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_SAMPLE_RELEASE_NOTES_MD)
        _seed_known_release(db, "27.1")
        scraper._db = db
        assert scraper._discover_release_notes() == []

    def test_returns_empty_on_fetch_error(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", side_effect=Exception("network error"))
        assert scraper._discover_release_notes() == []


class TestScrapeReleaseNote:
    def test_scrapes_cached_section(self, scraper):
        scraper._release_sections = _parse_release_sections(_SAMPLE_RELEASE_NOTES_MD)
        page = scraper.scrape_page(_release_url("27.1"), "release_notes")
        assert page is not None
        assert page.company == "ocient"
        assert page.category == "release_notes"
        assert "27.1" in page.title
        assert "New feature A for 27.1" in page.raw_text

    def test_refetches_on_cache_miss(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_SAMPLE_RELEASE_NOTES_MD)
        page = scraper.scrape_page(_release_url("26.1"), "release_notes")
        assert page is not None
        assert "Older feature C" in page.raw_text

    def test_returns_none_for_malformed_url(self, scraper):
        base_url = "https://docs.ocient.com/ocientaiq-unified-data-platform-release-notes.md"
        assert scraper.scrape_page(base_url, "release_notes") is None

    def test_returns_none_when_version_missing_from_page(self, scraper, mocker):
        mocker.patch.object(scraper, "_fetch_with_httpx", return_value=_SAMPLE_RELEASE_NOTES_MD)
        assert scraper.scrape_page(_release_url("99.9"), "release_notes") is None
