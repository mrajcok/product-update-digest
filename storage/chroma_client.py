import logging

import chromadb
from chromadb.utils import embedding_functions

from config import settings
from storage.models import ProductUpdate

logger = logging.getLogger(__name__)

# Chroma metadata fields that map 1:1 to ProductUpdate fields.
# source_text is stored as the document body (gets embedded), not in metadata.
_METADATA_FIELDS = ("url", "company", "category", "title", "scraped_at", "published_date", "summary")


def _build_embedding_function() -> embedding_functions.EmbeddingFunction:
    # OpenAIEmbeddingFunction works with OpenRouter's OpenAI-compatible endpoint.
    # If the configured model is not supported, replace with a custom EmbeddingFunction
    # subclass that calls https://openrouter.ai/api/v1/embeddings via httpx directly.
    return embedding_functions.OpenAIEmbeddingFunction(
        api_key=settings.openrouter_api_key,
        api_base="https://openrouter.ai/api/v1",
        model_name=settings.openrouter_embedding_model,
    )


def _row_to_product_update(doc: str, meta: dict) -> ProductUpdate:
    return ProductUpdate(
        url=meta["url"],
        company=meta["company"],
        category=meta["category"],
        title=meta["title"],
        scraped_at=meta["scraped_at"],
        published_date=meta.get("published_date"),
        summary=meta["summary"],
        source_text=doc,
    )


class ProductUpdatesChromaClient:
    """Vector storage and semantic search only — no deduplication logic here."""

    def __init__(self) -> None:
        client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
        )
        self._collection = client.get_or_create_collection(
            name=settings.chroma_collection_name,
            embedding_function=_build_embedding_function(),
        )
        logger.info(
            "Connected to Chroma collection %r at %s:%s",
            settings.chroma_collection_name,
            settings.chroma_host,
            settings.chroma_port,
        )

    def upsert(self, update: ProductUpdate, chroma_id: str) -> None:
        meta: dict = {
            "url": update.url,
            "company": update.company,
            "category": update.category,
            "title": update.title,
            "scraped_at": update.scraped_at,
            "published_date": update.published_date or "",
            "summary": update.summary,
        }
        self._collection.upsert(
            ids=[chroma_id],
            documents=[update.source_text],
            metadatas=[meta],
        )
        logger.debug("Chroma upsert: id=%s url=%s", chroma_id, update.url)

    def get_all(self, company: str | None = None) -> list[ProductUpdate]:
        where = {"company": company} if company else None
        result = self._collection.get(where=where, include=["documents", "metadatas"])
        updates = [
            _row_to_product_update(doc, meta)
            for doc, meta in zip(result["documents"], result["metadatas"])
        ]
        updates.sort(key=lambda u: u.published_date or u.scraped_at, reverse=True)
        return updates

    def get_recent(self, company: str | None = None, limit: int = 20) -> list[ProductUpdate]:
        return self.get_all(company=company)[:limit]
