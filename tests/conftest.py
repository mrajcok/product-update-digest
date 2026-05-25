"""
Shared fixtures for the product-update-digest test suite.

Run tests via:  make test
            or  .venv/bin/pytest tests/ -v
"""
import importlib
import os
import sqlite3

import pytest

from storage.db import ArticleDB
from storage.models import ScrapedPage, chroma_id_for


# ---------------------------------------------------------------------------
# Settings / env patching
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    """Provide minimal env vars so config.Settings validates without a real .env file."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", "test-user/test-repo")


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    """In-memory ArticleDB — isolated per test, no disk I/O."""
    with ArticleDB(":memory:") as database:
        yield database


# ---------------------------------------------------------------------------
# Model fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cribl_page():
    return ScrapedPage(
        url="https://cribl.io/blog/test-post/",
        company="cribl",
        category="blog",
        title="Test Post",
        raw_text="This is a test blog post about Cribl Stream 4.0 with new features.",
    )


@pytest.fixture
def ocient_page():
    return ScrapedPage(
        url="https://ocient.com/blog/test-post/",
        company="ocient",
        category="blog",
        title="Ocient Test Post",
        raw_text="Ocient announces new partnership with a major cloud provider.",
    )
