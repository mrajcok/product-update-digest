"""Tests for deduplication logic in BaseScraper — SQLite-backed."""
import pytest

from scrapers.base import BaseScraper
from storage.db import ArticleDB
from storage.models import ArticleRecord, ScrapedPage, vec_id_for


# ---------------------------------------------------------------------------
# Minimal concrete scraper for testing
# ---------------------------------------------------------------------------

class _FixedScraper(BaseScraper):
    """Scraper that returns a fixed page; pre_check always returns None (inconclusive)."""
    company = "cribl"

    def __init__(self, url: str, raw_text: str):
        super().__init__()
        self._url = url
        self._raw_text = raw_text

    def discover_urls(self):
        return [(self._url, "blog")]

    def scrape_page(self, url, category):
        return ScrapedPage(url=url, company="cribl", category="blog", title="Post", raw_text=self._raw_text)


def _seed_db(db: ArticleDB, url: str, raw_text: str) -> ArticleRecord:
    """Insert a record into the DB and return it."""
    page = ScrapedPage(url=url, company="cribl", category="blog", title="Post", raw_text=raw_text)
    record = ArticleRecord.from_scraped_page(page, vec_id=vec_id_for(url))
    db.upsert(record)
    return record


class TestNewUrl:
    def test_new_url_is_returned(self, db):
        scraper = _FixedScraper("https://cribl.io/blog/new/", "some content here for test")
        scraper.pre_check = lambda url, existing: None
        pages = scraper.run(db)
        assert len(pages) == 1
        assert pages[0].url == "https://cribl.io/blog/new/"
        scraper.close()


class TestUnchangedContent:
    def test_same_hash_is_skipped(self, db):
        url = "https://cribl.io/blog/existing/"
        text = "content that will not change at all here"
        _seed_db(db, url, text)

        scraper = _FixedScraper(url, text)
        scraper.pre_check = lambda url, existing: None  # force hash path
        pages = scraper.run(db)
        assert pages == []
        scraper.close()


class TestChangedContent:
    def test_different_hash_is_returned(self, db):
        url = "https://cribl.io/blog/existing/"
        _seed_db(db, url, "original content here for seed")

        scraper = _FixedScraper(url, "completely different updated content now")
        scraper.pre_check = lambda url, existing: None
        pages = scraper.run(db)
        assert len(pages) == 1
        scraper.close()


class TestPreCheck:
    def test_pre_check_false_skips_scrape(self, db):
        url = "https://cribl.io/blog/existing/"
        _seed_db(db, url, "original content here for seed data")
        scrape_calls = []

        class _TrackingScraper(BaseScraper):
            company = "cribl"
            def discover_urls(self): return [(url, "blog")]
            def scrape_page(self, u, c):
                scrape_calls.append(u)
                return ScrapedPage(url=u, company="cribl", category="blog", title="T", raw_text="x")
            def pre_check(self, u, existing): return False

        scraper = _TrackingScraper()
        pages = scraper.run(db)
        assert pages == []
        assert scrape_calls == [], "scrape_page should not be called when pre_check returns False"
        scraper.close()

    def test_pre_check_true_scrapes_without_hash_check(self, db):
        url = "https://cribl.io/blog/existing/"
        _seed_db(db, url, "original content here for seed data")

        class _PreCheckTrueScraper(BaseScraper):
            company = "cribl"
            def discover_urls(self): return [(url, "blog")]
            def scrape_page(self, u, c):
                # Returns same content — but pre_check=True should bypass hash comparison
                return ScrapedPage(url=u, company="cribl", category="blog", title="T", raw_text="original content here for seed data")
            def pre_check(self, u, existing): return True

        scraper = _PreCheckTrueScraper()
        pages = scraper.run(db)
        assert len(pages) == 1, "pre_check=True should return page regardless of hash"
        scraper.close()


class TestResilience:
    def test_scrape_exception_does_not_abort_run(self, db):
        class _FailingScraper(BaseScraper):
            company = "ocient"
            def discover_urls(self): return [("https://ocient.com/blog/post/", "blog")]
            def scrape_page(self, u, c): raise RuntimeError("network error")

        scraper = _FailingScraper()
        pages = scraper.run(db)
        assert pages == []
        scraper.close()

    def test_empty_discover_urls_returns_empty(self, db):
        class _EmptyScraper(BaseScraper):
            company = "cribl"
            def discover_urls(self): return []
            def scrape_page(self, u, c): return None

        pages = _EmptyScraper().run(db)
        assert pages == []
