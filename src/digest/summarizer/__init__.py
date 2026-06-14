import logging

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from tenacity import Retrying, before_sleep_log, stop_after_attempt, wait_exponential

from digest.config import settings
from digest.storage.models import ScrapedPage

logger = logging.getLogger(__name__)

_CATEGORY_INSTRUCTIONS: dict[str, str] = {
    "blog": "Focus on the technical insight or capability being introduced.",
    "press_release": (
        "Focus on what was announced, with whom, and the stated "
        "business or technical impact."
    ),
    "product": (
        "Focus on what capabilities exist, what's new or highlighted, "
        "and any pricing or availability signals."
    ),
}

_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a product intelligence analyst tracking two data-infrastructure "
        "companies (Cribl and Ocient) for a team of software engineers and architects. "
        "Write summaries that help a reader decide whether the item is relevant to them. "
        "Output only the summary text in markdown, without any commentary or explanation. "
        "If the content is too long to summarize effectively, produce a concise summary of the most "
        "important details and include a note that the original content should be consulted for more information. ",
    ),
    (
        "human",
        "Write a summary of the following {category} from {company}.\n"
        "{length_guidance}\n\n"
        "{category_instruction}\n\n"
        "Always include: specific product or feature names, version numbers if present, "
        "and the core technical claim or announcement.\n"
        "Never include: generic marketing phrases, \"click here\" calls to action, or "
        "repetition of the article title.\n\n"
        "Title: {title}\n\n"
        "Content:\n{content}",
    ),
])


def _length_guidance(char_count: int) -> str:
    if char_count < 500:
        return "One sentence."
    if char_count < 2000:
        return "2-3 sentences."
    if char_count < 4000:
        return "3-5 sentences."
    return "One short lead sentence, then up to 4 bullet points for the key technical details. Each bullet must start on its own line with '* '."


class Summarizer:
    """LangChain chain that summarizes a ScrapedPage via OpenRouter or a local LM Studio server."""

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None) -> None:
        llm = ChatOpenAI(
            model=model or settings.openrouter_summarization_model,
            api_key=SecretStr(api_key or settings.openrouter_api_key),
            base_url=base_url or "https://openrouter.ai/api/v1",
        )
        self._chain = _PROMPT | llm | StrOutputParser()

    def summarize(self, page: ScrapedPage) -> str:
        content = page.raw_text[:settings.summarizer_content_chars]
        inputs = {
            "company": page.company,
            "category": page.category,
            "title": page.title,
            "content": content,
            "length_guidance": _length_guidance(len(page.raw_text)),
            "category_instruction": _CATEGORY_INSTRUCTIONS.get(
                page.category,
                "Focus on what changed or was announced and why it matters.",
            ),
        }
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(settings.max_api_retries),
                wait=wait_exponential(multiplier=1, min=2, max=30),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            ):
                with attempt:
                    return self._chain.invoke(inputs)
        except Exception as exc:
            logger.error("Summarization failed after %d attempts (%s) — using raw text fallback", settings.max_api_retries, exc)
            return page.raw_text[:300]
        raise AssertionError("unreachable")
