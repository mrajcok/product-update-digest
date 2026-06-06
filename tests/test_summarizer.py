"""Tests for summarizer.py — mocks the LLM chain, no real API calls."""
import pytest
from unittest.mock import MagicMock, patch

from digest.storage.models import ScrapedPage


@pytest.fixture
def mock_chain(mocker):
    """Patch ChatOpenAI so no real API call is made."""
    mock = MagicMock()
    mock.invoke.return_value = "This is a mocked summary of the article."
    mocker.patch("digest.summarizer.ChatOpenAI", return_value=MagicMock())
    mocker.patch("digest.summarizer._PROMPT.__or__", return_value=MagicMock(__or__=lambda s, o: mock))
    return mock


class TestSummarizer:
    def test_returns_summary_on_success(self, mocker):
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = "Cribl Stream 4.0 released with new features."
        mocker.patch("digest.summarizer.ChatOpenAI")
        mocker.patch("langchain_core.prompts.ChatPromptTemplate.__or__", return_value=MagicMock(
            __or__=lambda s, o: mock_chain
        ))

        from digest.summarizer import Summarizer
        s = Summarizer()
        s._chain = mock_chain

        page = ScrapedPage(
            url="https://cribl.io/blog/stream-4/",
            company="cribl",
            category="blog",
            title="Stream 4.0",
            raw_text="Cribl Stream 4.0 includes adaptive sampling and better edge support.",
        )
        result = s.summarize(page)
        assert result == "Cribl Stream 4.0 released with new features."
        mock_chain.invoke.assert_called_once()

    def test_prompt_receives_correct_fields(self, mocker):
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = "summary"
        mocker.patch("digest.summarizer.ChatOpenAI")

        from digest.summarizer import Summarizer
        s = Summarizer()
        s._chain = mock_chain

        page = ScrapedPage(
            url="https://ocient.com/blog/post/",
            company="ocient",
            category="blog",
            title="My Title",
            raw_text="x" * 100,
        )
        s.summarize(page)
        call_kwargs = mock_chain.invoke.call_args[0][0]
        assert call_kwargs["company"] == "ocient"
        assert call_kwargs["category"] == "blog"
        assert call_kwargs["title"] == "My Title"
        assert "content" in call_kwargs

    def test_content_truncated_to_limit(self, mocker):
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = "summary"
        mocker.patch("digest.summarizer.ChatOpenAI")
        from digest.config import settings
        mocker.patch.object(settings, "summarizer_content_chars", 6000)

        from digest.summarizer import Summarizer
        s = Summarizer()
        s._chain = mock_chain

        page = ScrapedPage(
            url="https://cribl.io/blog/long/",
            company="cribl",
            category="blog",
            title="Long Post",
            raw_text="a" * 10_000,
        )
        s.summarize(page)
        call_kwargs = mock_chain.invoke.call_args[0][0]
        assert len(call_kwargs["content"]) == 6000

    def test_fallback_on_llm_failure(self, mocker):
        mock_chain = MagicMock()
        mock_chain.invoke.side_effect = Exception("API timeout")
        mocker.patch("digest.summarizer.ChatOpenAI")
        from digest.config import settings
        mocker.patch.object(settings, "max_api_retries", 1)

        from digest.summarizer import Summarizer
        s = Summarizer()
        s._chain = mock_chain

        raw = "This is the raw content that should appear in the fallback output."
        page = ScrapedPage(
            url="https://cribl.io/blog/fail/",
            company="cribl",
            category="blog",
            title="Fail",
            raw_text=raw,
        )
        result = s.summarize(page)
        assert result == raw[:300]
