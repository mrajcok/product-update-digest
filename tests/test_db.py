"""Tests for storage/db.py — ArticleDB SQLite client."""
import time

import pytest

from storage.db import ArticleDB
from storage.models import ArticleRecord, ScrapedPage, vec_id_for, normalize_url


def make_record(url: str, company="cribl", category="blog", raw_text="hello world content here") -> ArticleRecord:
    page = ScrapedPage(url=url, company=company, category=category, title="Test", raw_text=raw_text)
    return ArticleRecord.from_scraped_page(page, vec_id=vec_id_for(url))


class TestGetByUrl:
    def test_returns_none_for_unknown_url(self, db):
        assert db.get_by_url("https://cribl.io/blog/unknown/") is None

    def test_finds_exact_url(self, db):
        r = make_record("https://cribl.io/blog/post/")
        db.upsert(r)
        found = db.get_by_url("https://cribl.io/blog/post/")
        assert found is not None
        assert found.url == "https://cribl.io/blog/post/"

    def test_normalizes_http_to_https(self, db):
        db.upsert(make_record("https://cribl.io/blog/post/"))
        assert db.get_by_url("http://cribl.io/blog/post/") is not None

    def test_strips_trailing_slash(self, db):
        db.upsert(make_record("https://cribl.io/blog/post/"))
        assert db.get_by_url("https://cribl.io/blog/post") is not None

    def test_normalizes_uppercase_host(self, db):
        db.upsert(make_record("https://cribl.io/blog/post/"))
        assert db.get_by_url("https://CRIBL.IO/blog/post/") is not None

    def test_strips_fragment(self, db):
        db.upsert(make_record("https://cribl.io/blog/post/"))
        assert db.get_by_url("https://cribl.io/blog/post/#section") is not None

    def test_sorts_query_params(self, db):
        db.upsert(make_record("https://cribl.io/blog/post/?a=1&b=2"))
        assert db.get_by_url("https://cribl.io/blog/post/?b=2&a=1") is not None


class TestUpsert:
    def test_inserts_new_record(self, db):
        db.upsert(make_record("https://cribl.io/blog/new/"))
        assert db.get_by_url("https://cribl.io/blog/new/") is not None

    def test_updates_existing_record(self, db):
        url = "https://cribl.io/blog/post/"
        db.upsert(make_record(url, raw_text="original content for testing"))
        original = db.get_by_url(url)

        updated_page = ScrapedPage(url=url, company="cribl", category="blog", title="Updated Title", raw_text="updated content here now")
        updated_record = ArticleRecord.from_scraped_page(
            updated_page,
            vec_id=vec_id_for(url),
            first_scraped_at=original.first_scraped_at,
        )
        db.upsert(updated_record)

        found = db.get_by_url(url)
        assert found.title == "Updated Title"
        assert found.content_hash == updated_page.content_hash

    def test_preserves_first_scraped_at_on_update(self, db):
        url = "https://cribl.io/blog/post/"
        db.upsert(make_record(url))
        original = db.get_by_url(url)

        page2 = ScrapedPage(url=url, company="cribl", category="blog", title="T", raw_text="new content here is different")
        record2 = ArticleRecord.from_scraped_page(page2, first_scraped_at=original.first_scraped_at)
        db.upsert(record2)

        found = db.get_by_url(url)
        assert found.first_scraped_at == original.first_scraped_at

    def test_upsert_different_companies(self, db):
        db.upsert(make_record("https://cribl.io/blog/a/", company="cribl"))
        db.upsert(make_record("https://ocient.com/blog/b/", company="ocient"))
        assert len(db.get_all()) == 2


class TestGetAll:
    def test_returns_all_records(self, db):
        db.upsert(make_record("https://cribl.io/blog/a/"))
        db.upsert(make_record("https://cribl.io/blog/b/"))
        db.upsert(make_record("https://ocient.com/blog/c/", company="ocient"))
        assert len(db.get_all()) == 3

    def test_filters_by_company(self, db):
        db.upsert(make_record("https://cribl.io/blog/a/", company="cribl"))
        db.upsert(make_record("https://ocient.com/blog/b/", company="ocient"))
        cribl_only = db.get_all(company="cribl")
        assert len(cribl_only) == 1
        assert cribl_only[0].company == "cribl"

    def test_returns_empty_list_when_no_records(self, db):
        assert db.get_all() == []
