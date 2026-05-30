"""Tests for article_text table in storage/db.py."""
import pytest

from storage.db import ArticleDB
from storage.models import ArticleRecord, ScrapedPage, normalize_url, vec_id_for


def _make_record(url: str, company: str = "cribl", published_date: str | None = "2026-03-15") -> ArticleRecord:
    page = ScrapedPage(url=url, company=company, category="blog", title="Test", raw_text="test content")
    return ArticleRecord.from_scraped_page(page, vec_id=vec_id_for(url), published_date_override=None)


def _upsert_with_text(db: ArticleDB, url: str, company: str = "cribl", raw_text: str = "some raw text") -> ArticleRecord:
    page = ScrapedPage(url=url, company=company, category="blog", title="T", raw_text=raw_text)
    record = ArticleRecord.from_scraped_page(page, vec_id=vec_id_for(url))
    db.upsert(record)
    db.save_text(normalize_url(url), raw_text)
    return record


class TestArticleTextTable:
    def test_table_created_on_init(self, db: ArticleDB):
        rows = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='article_text'"
        ).fetchall()
        assert len(rows) == 1

    def test_save_and_get_roundtrip(self, db: ArticleDB):
        url = "https://cribl.io/blog/post/"
        nurl = normalize_url(url)
        page = ScrapedPage(url=url, company="cribl", category="blog", title="T", raw_text="x")
        db.upsert(ArticleRecord.from_scraped_page(page))
        db.save_text(nurl, "hello world content")
        assert db.get_text(nurl) == "hello world content"

    def test_save_overwrites_existing(self, db: ArticleDB):
        url = "https://cribl.io/blog/post/"
        nurl = normalize_url(url)
        page = ScrapedPage(url=url, company="cribl", category="blog", title="T", raw_text="x")
        db.upsert(ArticleRecord.from_scraped_page(page))
        db.save_text(nurl, "first version")
        db.save_text(nurl, "second version")
        assert db.get_text(nurl) == "second version"

    def test_get_text_returns_none_for_missing(self, db: ArticleDB):
        assert db.get_text("https://cribl.io/blog/nonexistent/") is None

    def test_delete_text(self, db: ArticleDB):
        url = "https://cribl.io/blog/post/"
        nurl = normalize_url(url)
        page = ScrapedPage(url=url, company="cribl", category="blog", title="T", raw_text="x")
        db.upsert(ArticleRecord.from_scraped_page(page))
        db.save_text(nurl, "content to delete")
        db.delete_text(nurl)
        assert db.get_text(nurl) is None

    def test_delete_text_nonexistent_is_noop(self, db: ArticleDB):
        db.delete_text("https://example.com/nope/")  # should not raise


class TestLatestArticleWithText:
    def test_returns_none_when_no_articles(self, db: ArticleDB):
        assert db.latest_article_with_text("cribl") is None

    def test_returns_none_when_no_text_cached(self, db: ArticleDB):
        page = ScrapedPage(url="https://cribl.io/blog/post/", company="cribl", category="blog", title="T", raw_text="x")
        db.upsert(ArticleRecord.from_scraped_page(page))
        # No save_text call — article_text table is empty
        assert db.latest_article_with_text("cribl") is None

    def test_returns_article_with_cached_text(self, db: ArticleDB):
        _upsert_with_text(db, "https://cribl.io/blog/post/", company="cribl")
        result = db.latest_article_with_text("cribl")
        assert result is not None
        assert result.company == "cribl"

    def test_returns_most_recent_by_published_date(self, db: ArticleDB):
        older_page = ScrapedPage(
            url="https://cribl.io/blog/older/", company="cribl", category="blog",
            title="Older", raw_text="older text", published_date="2026-01-01",
        )
        newer_page = ScrapedPage(
            url="https://cribl.io/blog/newer/", company="cribl", category="blog",
            title="Newer", raw_text="newer text", published_date="2026-05-01",
        )
        for page in [older_page, newer_page]:
            db.upsert(ArticleRecord.from_scraped_page(page))
            db.save_text(normalize_url(page.url), page.raw_text)

        result = db.latest_article_with_text("cribl")
        assert result is not None
        assert result.url == "https://cribl.io/blog/newer/"

    def test_filters_by_company(self, db: ArticleDB):
        _upsert_with_text(db, "https://cribl.io/blog/c/", company="cribl")
        _upsert_with_text(db, "https://ocient.com/blog/o/", company="ocient")

        cribl = db.latest_article_with_text("cribl")
        ocient = db.latest_article_with_text("ocient")
        assert cribl is not None and cribl.company == "cribl"
        assert ocient is not None and ocient.company == "ocient"
