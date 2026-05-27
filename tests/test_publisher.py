"""Tests for publisher/github_pages.py — mocks git push, no network calls."""
import pytest

from storage.db import ArticleDB
from storage.models import ArticleRecord, normalize_url
from publisher.github_pages import GitHubPagesPublisher, _group_by_month


def _make_record(**kwargs) -> ArticleRecord:
    defaults = dict(
        url="https://cribl.io/blog/post/",
        normalized_url=normalize_url("https://cribl.io/blog/post/"),
        company="cribl",
        category="blog",
        title="Test Post",
        first_scraped_at="2026-03-15T10:00:00",
        last_scraped_at="2026-03-15T10:00:00",
        content_hash="abc123",
        published_date="2026-03-15",
        summary="A short summary of this post.",
        status="ok",
    )
    defaults.update(kwargs)
    return ArticleRecord(**defaults)


@pytest.fixture
def db_with_records(db: ArticleDB) -> ArticleDB:
    records = [
        _make_record(
            url="https://cribl.io/blog/stream-4/",
            normalized_url=normalize_url("https://cribl.io/blog/stream-4/"),
            title="Cribl Stream 4.0",
            published_date="2026-03-15",
            summary="Stream 4.0 released with new features.",
        ),
        _make_record(
            url="https://cribl.io/blog/edge-update/",
            normalized_url=normalize_url("https://cribl.io/blog/edge-update/"),
            title="Cribl Edge Update",
            published_date="2026-02-10",
            summary="Edge agent update with performance improvements.",
        ),
        _make_record(
            url="https://ocient.com/blog/hyperscale/",
            normalized_url=normalize_url("https://ocient.com/blog/hyperscale/"),
            company="ocient",
            title="Ocient Hyperscale",
            published_date="2026-03-20",
            summary="Ocient announces hyperscale analytics.",
        ),
        _make_record(
            url="https://ocient.com/blog/no-summary/",
            normalized_url=normalize_url("https://ocient.com/blog/no-summary/"),
            company="ocient",
            title="Post Without Summary",
            published_date="2026-01-05",
            summary="",  # excluded from feed
        ),
    ]
    for r in records:
        db.upsert(r)
    return db


class TestRenderIndex:
    def test_renders_index_html(self, db_with_records):
        publisher = GitHubPagesPublisher(db_with_records)
        files = publisher._render(
            top_updates=db_with_records.get_all()[:3],
            company_updates={
                "cribl": db_with_records.get_all(company="cribl"),
                "ocient": db_with_records.get_all(company="ocient"),
            },
        )
        assert "index.html" in files
        html = files["index.html"]
        assert "News & Blog Posts" in html
        assert "cribl/" in html
        assert "ocient/" in html

    def test_index_contains_titles(self, db_with_records):
        publisher = GitHubPagesPublisher(db_with_records)
        all_records = db_with_records.get_all()
        ok = [r for r in all_records if r.status == "ok" and r.summary]
        files = publisher._render(top_updates=ok, company_updates={"cribl": [], "ocient": []})
        html = files["index.html"]
        assert "Cribl Stream 4.0" in html
        assert "Ocient Hyperscale" in html

    def test_index_excludes_records_without_summary(self, db_with_records):
        publisher = GitHubPagesPublisher(db_with_records)
        all_records = db_with_records.get_all()
        ok = [r for r in all_records if r.status == "ok" and r.summary]
        files = publisher._render(top_updates=ok, company_updates={"cribl": [], "ocient": []})
        html = files["index.html"]
        assert "Post Without Summary" not in html


class TestRenderCompanyPage:
    def test_renders_company_pages(self, db_with_records):
        publisher = GitHubPagesPublisher(db_with_records)
        cribl_records = db_with_records.get_all(company="cribl")
        files = publisher._render(
            top_updates=[],
            company_updates={"cribl": cribl_records, "ocient": []},
        )
        assert "cribl/index.html" in files
        assert "ocient/index.html" in files

    def test_company_page_has_breadcrumb(self, db_with_records):
        publisher = GitHubPagesPublisher(db_with_records)
        cribl_records = db_with_records.get_all(company="cribl")
        files = publisher._render(
            top_updates=[],
            company_updates={"cribl": cribl_records, "ocient": []},
        )
        html = files["cribl/index.html"]
        assert "All Updates" in html
        assert "../" in html

    def test_company_page_groups_by_month(self, db_with_records):
        publisher = GitHubPagesPublisher(db_with_records)
        cribl_records = db_with_records.get_all(company="cribl")
        files = publisher._render(
            top_updates=[],
            company_updates={"cribl": cribl_records, "ocient": []},
        )
        html = files["cribl/index.html"]
        assert "March 2026" in html
        assert "February 2026" in html


class TestGroupByMonth:
    def test_groups_correctly(self):
        records = [
            _make_record(published_date="2026-03-15"),
            _make_record(
                url="https://cribl.io/blog/b/",
                normalized_url=normalize_url("https://cribl.io/blog/b/"),
                published_date="2026-03-20",
            ),
            _make_record(
                url="https://cribl.io/blog/c/",
                normalized_url=normalize_url("https://cribl.io/blog/c/"),
                published_date="2026-02-10",
            ),
        ]
        grouped = _group_by_month(records)
        labels = [label for label, _ in grouped]
        assert labels[0] == "March 2026"
        assert labels[1] == "February 2026"

    def test_sorted_newest_first(self):
        records = [
            _make_record(published_date="2026-01-05"),
            _make_record(
                url="https://cribl.io/blog/b/",
                normalized_url=normalize_url("https://cribl.io/blog/b/"),
                published_date="2026-05-10",
            ),
        ]
        grouped = _group_by_month(records)
        assert grouped[0][0] == "May 2026"
        assert grouped[1][0] == "January 2026"

    def test_fallback_label_for_missing_date(self):
        records = [
            _make_record(published_date=None, last_scraped_at=""),
        ]
        grouped = _group_by_month(records)
        assert grouped[0][0] == "Unknown"


class TestPublish:
    def test_publish_calls_push(self, db_with_records, mocker):
        publisher = GitHubPagesPublisher(db_with_records)
        mock_push = mocker.patch.object(publisher, "_push_to_github")
        publisher.publish()
        mock_push.assert_called_once()
        _, html_files = mock_push.call_args[0][0], mock_push.call_args[0]
        files = mock_push.call_args[0][0]
        assert "index.html" in files
        assert "cribl/index.html" in files
        assert "ocient/index.html" in files

    def test_publish_only_includes_ok_with_summary(self, db_with_records, mocker):
        publisher = GitHubPagesPublisher(db_with_records)
        mock_push = mocker.patch.object(publisher, "_push_to_github")
        publisher.publish()
        files = mock_push.call_args[0][0]
        html = files["index.html"]
        assert "Post Without Summary" not in html

    def test_publish_top_20_limit(self, db: ArticleDB, mocker):
        for i in range(25):
            db.upsert(_make_record(
                url=f"https://cribl.io/blog/post-{i}/",
                normalized_url=normalize_url(f"https://cribl.io/blog/post-{i}/"),
                title=f"Post {i}",
                published_date=f"2026-01-{i + 1:02d}",
                summary=f"Summary for post {i}.",
            ))
        publisher = GitHubPagesPublisher(db)
        mock_push = mocker.patch.object(publisher, "_push_to_github")
        publisher.publish()
        files = mock_push.call_args[0][0]
        html = files["index.html"]
        # Count occurrences of "class=\"card\"" — should be at most 20
        assert html.count('class="card"') <= 20
