"""Tests for the --stage pipeline runners in main.py."""
import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from storage.db import ArticleDB
from storage.models import ArticleRecord, ScrapedPage, normalize_url, vec_id_for


def _args(stage: str, site: str | None = None, limit: int = 1) -> argparse.Namespace:
    return argparse.Namespace(stage=stage, site=site, limit=limit)


def _cribl_page(url: str = "https://cribl.io/blog/post/") -> ScrapedPage:
    return ScrapedPage(
        url=url, company="cribl", category="blog", title="Cribl Post",
        raw_text="Cribl raw content " * 20, published_date="2026-05-01",
    )


def _ocient_page(url: str = "https://ocient.com/blog/post/") -> ScrapedPage:
    return ScrapedPage(
        url=url, company="ocient", category="blog", title="Ocient Post",
        raw_text="Ocient raw content " * 20, published_date="2026-04-01",
    )


# ---------------------------------------------------------------------------
# _run_scrape
# ---------------------------------------------------------------------------

class TestRunScrape:
    def test_scrape_saves_raw_text_to_db(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()
        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []
        mock_scraper.run.return_value = [page]

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        from main import _run_scrape
        _run_scrape(_args("scrape"), db)

        text = db.get_text(normalize_url(page.url))
        assert text == page.raw_text

    def test_scrape_renders_html(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()
        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []
        mock_scraper.run.return_value = [page]

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        from main import _run_scrape
        _run_scrape(_args("scrape"), db)

        assert (tmp_path / "index.html").exists()


# ---------------------------------------------------------------------------
# _run_summarize
# ---------------------------------------------------------------------------

class TestRunSummarize:
    def test_uses_cached_article_without_scraping(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()
        record = ArticleRecord.from_scraped_page(page)
        db.upsert(record)
        db.save_text(normalize_url(page.url), page.raw_text)

        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        mock_summarizer = MagicMock()
        mock_summarizer.summarize.return_value = "A cached summary."
        mocker.patch("main._make_summarizer", return_value=mock_summarizer)
        mocker.patch("main._assert_ollama_available")
        mocker.patch("main._assert_model_available")

        from main import _run_summarize
        _run_summarize(_args("summarize"), db)

        # Scraper.run should NOT have been called — we had cached text
        mock_scraper.run.assert_not_called()
        mock_summarizer.summarize.assert_called_once()

    def test_falls_back_to_scrape_when_no_cache(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()

        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []
        mock_scraper.run.return_value = [page]

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        mock_summarizer = MagicMock()
        mock_summarizer.summarize.return_value = "A fresh summary."
        mocker.patch("main._make_summarizer", return_value=mock_summarizer)
        mocker.patch("main._assert_ollama_available")
        mocker.patch("main._assert_model_available")

        from main import _run_summarize
        _run_summarize(_args("summarize"), db)

        mock_scraper.run.assert_called_once()
        mock_summarizer.summarize.assert_called_once()

    def test_summary_written_to_db(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()
        record = ArticleRecord.from_scraped_page(page)
        db.upsert(record)
        db.save_text(normalize_url(page.url), page.raw_text)

        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        mock_summarizer = MagicMock()
        mock_summarizer.summarize.return_value = "Persisted summary."
        mocker.patch("main._make_summarizer", return_value=mock_summarizer)
        mocker.patch("main._assert_ollama_available")
        mocker.patch("main._assert_model_available")

        from main import _run_summarize
        _run_summarize(_args("summarize"), db)

        stored = db.get_by_url(page.url)
        assert stored is not None
        assert stored.summary == "Persisted summary."

    def test_renders_html(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()
        record = ArticleRecord.from_scraped_page(page)
        db.upsert(record)
        db.save_text(normalize_url(page.url), page.raw_text)

        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)
        mocker.patch("main._make_summarizer", return_value=MagicMock(summarize=MagicMock(return_value="s")))
        mocker.patch("main._assert_ollama_available")
        mocker.patch("main._assert_model_available")

        from main import _run_summarize
        _run_summarize(_args("summarize"), db)

        assert (tmp_path / "index.html").exists()


# ---------------------------------------------------------------------------
# _run_vector
# ---------------------------------------------------------------------------

class TestRunVector:
    def test_vector_clears_and_rebuilds(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()
        record = ArticleRecord.from_scraped_page(page)
        db.upsert(record)
        db.save_text(normalize_url(page.url), page.raw_text)

        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        upsert_calls = []

        class FakeVecClient:
            def __init__(self, db_path=None):
                self._conn = MagicMock()
                self._conn.execute = MagicMock()
                self._conn.commit = MagicMock()

            def upsert(self, update, vid):
                upsert_calls.append((update, vid))

            def get_all(self, company=None):
                return []

            def close(self):
                pass

        mocker.patch("main.VecClient", FakeVecClient)

        from main import _run_vector
        _run_vector(_args("vector"), db)

        assert len(upsert_calls) == 1
        assert upsert_calls[0][0].url == page.url

    def test_vector_scrapes_when_no_cache(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()

        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []
        mock_scraper.run.return_value = [page]

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        class FakeVecClient:
            def __init__(self, db_path=None):
                self._conn = MagicMock()
                self._conn.execute = MagicMock()
                self._conn.commit = MagicMock()

            def upsert(self, update, vid):
                pass

            def get_all(self, company=None):
                return []

            def close(self):
                pass

        mocker.patch("main.VecClient", FakeVecClient)

        from main import _run_vector
        _run_vector(_args("vector"), db)

        mock_scraper.run.assert_called_once()

    def test_vector_renders_html(self, db: ArticleDB, tmp_path: Path, mocker):
        page = _cribl_page()
        record = ArticleRecord.from_scraped_page(page)
        db.upsert(record)
        db.save_text(normalize_url(page.url), page.raw_text)

        mock_scraper = MagicMock()
        mock_scraper.company = "cribl"
        mock_scraper.sources = []
        mock_scraper.exclusions = []

        mocker.patch("main._build_scrapers", return_value=[mock_scraper])
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        class FakeVecClient:
            def __init__(self, db_path=None):
                self._conn = MagicMock()
                self._conn.execute = MagicMock()
                self._conn.commit = MagicMock()

            def upsert(self, update, vid):
                pass

            def get_all(self, company=None):
                return []

            def close(self):
                pass

        mocker.patch("main.VecClient", FakeVecClient)

        from main import _run_vector
        _run_vector(_args("vector"), db)

        assert (tmp_path / "index.html").exists()


# ---------------------------------------------------------------------------
# _run_publish
# ---------------------------------------------------------------------------

class TestRunPublish:
    def test_errors_when_dir_empty(self, db: ArticleDB, tmp_path: Path, mocker):
        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        import sys
        from main import _run_publish

        with pytest.raises(SystemExit):
            _run_publish(_args("publish"), db)

    def test_errors_when_dir_missing(self, db: ArticleDB, tmp_path: Path, mocker):
        mocker.patch("main._DRY_RUN_DIR", tmp_path / "nonexistent")

        from main import _run_publish

        with pytest.raises(SystemExit):
            _run_publish(_args("publish"), db)

    def test_calls_push_with_html_files(self, db: ArticleDB, tmp_path: Path, mocker):
        (tmp_path / "index.html").write_text("<html>test</html>")
        (tmp_path / "cribl").mkdir()
        (tmp_path / "cribl" / "index.html").write_text("<html>cribl</html>")

        mocker.patch("main._DRY_RUN_DIR", tmp_path)

        from publisher.github_pages import GitHubPagesPublisher
        mock_push = mocker.patch.object(GitHubPagesPublisher, "_push_to_github")

        from main import _run_publish
        _run_publish(_args("publish"), db)

        mock_push.assert_called_once()
        pushed_files = mock_push.call_args[0][0]
        assert "index.html" in pushed_files
        assert "cribl/index.html" in pushed_files
