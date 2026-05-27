import logging

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from config import settings
from storage.models import ScrapedPage

logger = logging.getLogger(__name__)

_CONTENT_CHAR_LIMIT = 6000  # token-budget guard before sending to LLM
_FALLBACK_CHARS = 300       # chars of raw_text used when LLM call fails

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
        "Write summaries that help a busy reader decide in seconds whether the item "
        "is relevant to them.",
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
    return "One short lead sentence, then up to 4 bullet points for the key technical details."


class Summarizer:
    """LangChain chain that summarizes a ScrapedPage via OpenRouter."""

    def __init__(self, model: str | None = None) -> None:
        llm = ChatOpenAI(
            model=model or settings.openrouter_summarization_model,
            api_key=SecretStr(settings.openrouter_api_key),
            base_url="https://openrouter.ai/api/v1",
        )
        self._chain = _PROMPT | llm | StrOutputParser()

    def summarize(self, page: ScrapedPage) -> str:
        content = page.raw_text[:_CONTENT_CHAR_LIMIT]
        try:
            return self._chain.invoke({
                "company": page.company,
                "category": page.category,
                "title": page.title,
                "content": content,
                "length_guidance": _length_guidance(len(page.raw_text)),
                "category_instruction": _CATEGORY_INSTRUCTIONS.get(
                    page.category,
                    "Focus on what changed or was announced and why it matters.",
                ),
            })
        except Exception:
            logger.error(
                "summarizer: LLM call failed for %s — using truncated raw text as fallback",
                page.url,
                exc_info=True,
            )
            return page.raw_text[:_FALLBACK_CHARS]
