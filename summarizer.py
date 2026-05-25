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

_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You are a technical analyst summarizing product updates for a software company."),
    ("human", (
        "Summarize the following {category} content from {company}.\n"
        "Focus on: what changed or was announced, why it matters, and any specific "
        "product names or versions mentioned.\n\n"
        "Title: {title}\n\n"
        "Content:\n{content}\n\n"
        "Summary:"
    )),
])


class Summarizer:
    """LangChain chain that summarizes a ScrapedPage via OpenRouter."""

    def __init__(self) -> None:
        llm = ChatOpenAI(
            model=settings.openrouter_summarization_model,
            api_key=SecretStr(settings.openrouter_api_key),
            base_url="https://openrouter.ai/api/v1",
        )
        self._chain = _PROMPT | llm | StrOutputParser()

    def summarize(self, page: ScrapedPage) -> str:
        try:
            return self._chain.invoke({
                "company": page.company,
                "category": page.category,
                "title": page.title,
                "content": page.raw_text[:_CONTENT_CHAR_LIMIT],
            })
        except Exception:
            logger.error(
                "summarizer: LLM call failed for %s — using truncated raw text as fallback",
                page.url,
                exc_info=True,
            )
            return page.raw_text[:_FALLBACK_CHARS]
