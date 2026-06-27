#!/usr/bin/env python3
"""Standalone MCP server exposing semantic_search and rag_query over the
product-update-digest sqlite-vec database.

Runs as the hermes system user; has no access to the project source tree or
.env file.  All secrets are injected via environment variables by hermes's
MCP launcher (OPENROUTER_API_KEY from hermes's own .env, DIGEST_DB_PATH from
config.yaml mcp_servers env block).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import struct
import time
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("digest-mcp")

# ── Configuration from environment ────────────────────────────────────────────

DB_PATH = os.environ.get("DIGEST_DB_PATH", "/opt/digest/product_updates.db")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("OPENROUTER_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMENSIONS", "4096"))
SCORE_THRESHOLD = float(os.environ.get("SEARCH_SCORE_THRESHOLD", "0.10"))
# Total embedding attempts before giving up; exponential backoff between tries.
EMBED_MAX_RETRIES = int(os.environ.get("EMBED_MAX_RETRIES", "5"))

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# HTTP statuses worth retrying: 429 (rate limited) and transient 5xx upstream errors.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

mcp = FastMCP("digest-search")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _backoff_seconds(resp: Optional[httpx.Response], attempt: int) -> float:
    """Respect a Retry-After header if present, else exponential backoff (2→30s)."""
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
    return min(2.0 * (2 ** (attempt - 1)), 30.0)


def _embed(text: str) -> list[float]:
    """Call OpenRouter embeddings endpoint and return a float list.

    Retries on HTTP 429 / transient 5xx and transport errors with exponential
    backoff (honoring Retry-After when provided), so a momentary OpenRouter rate
    limit doesn't fail the whole MCP tool call.
    """
    last_exc: Exception | None = None
    for attempt in range(1, EMBED_MAX_RETRIES + 1):
        try:
            resp = httpx.post(
                f"{OPENROUTER_BASE}/embeddings",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={"model": EMBEDDING_MODEL, "input": text},
                timeout=60,
            )
        except httpx.TransportError as exc:
            # Connection/timeout error — transient, always worth retrying.
            last_exc = exc
            if attempt >= EMBED_MAX_RETRIES:
                break
            wait = _backoff_seconds(None, attempt)
            logger.warning(
                "embeddings transport error (attempt %d/%d): %s — retrying in %.1fs",
                attempt, EMBED_MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)
            continue

        if resp.status_code in _RETRYABLE_STATUS and attempt < EMBED_MAX_RETRIES:
            wait = _backoff_seconds(resp, attempt)
            logger.warning(
                "embeddings HTTP %d (attempt %d/%d) — retrying in %.1fs",
                resp.status_code, attempt, EMBED_MAX_RETRIES, wait,
            )
            last_exc = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
            time.sleep(wait)
            continue

        # Non-retryable status (e.g. 4xx) raises immediately; success returns.
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    raise RuntimeError(
        f"Embedding request failed after {EMBED_MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


def _serialize(vec: list[float]) -> bytes:
    """Pack a float list into the little-endian bytes sqlite-vec expects."""
    return struct.pack(f"{len(vec)}f", *vec)


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _score(distance: float) -> float:
    """Convert cosine distance (0=identical, 2=opposite) to similarity score (0–1)."""
    return round(1.0 - distance / 2.0, 4)


def _company_clause(company: Optional[str], param_start: int = 1) -> tuple[str, list]:
    """Return optional WHERE/AND clause and params for company filtering."""
    if company:
        return f" AND vi.company = ?", [company.lower()]
    return "", []


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def semantic_search(
    query: str,
    company: Optional[str] = None,
    n_results: int = 5,
) -> str:
    """Search product update digests semantically.

    Returns the most relevant articles for a natural-language query.
    Use this for broad discovery ("what's new with Cribl?", "any press releases
    about XSIAM?").

    Args:
        query: Natural language search query.
        company: Optional filter — one of 'cribl', 'ocient', or 'xsiam'.
        n_results: Number of results to return (default 5, max 20).
    """
    n_results = min(int(n_results), 20)
    vec = _serialize(_embed(query))
    company_sql, company_params = _company_clause(company)

    conn = _open_db()
    try:
        # KNN search on whole-document embeddings, then join metadata
        rows = conn.execute(
            f"""
            SELECT vi.title, vi.company, vi.category, vi.url,
                   vi.published_date, vi.summary, e.distance
            FROM vec_embeddings e
            INNER JOIN vec_items vi ON e.id = vi.id
            WHERE e.embedding MATCH ? AND k = ?{company_sql}
            ORDER BY e.distance
            """,
            [vec, n_results * 4, *company_params],  # over-fetch, filter below
        ).fetchall()
    finally:
        conn.close()

    results = [r for r in rows if _score(r["distance"]) >= SCORE_THRESHOLD][:n_results]

    if not results:
        return "No results found above the relevance threshold for that query."

    lines = [f"**Semantic search results for:** {query}\n"]
    for i, r in enumerate(results, 1):
        score = _score(r["distance"])
        date = r["published_date"] or r["published_date"] or "unknown date"
        lines.append(
            f"**{i}. {r['title']}**\n"
            f"  Company: {r['company']} | Category: {r['category']} | Date: {date} | Score: {score}\n"
            f"  {r['summary']}\n"
            f"  <{r['url']}>\n"
        )
    return "\n".join(lines)


@mcp.tool()
def rag_query(
    question: str,
    company: Optional[str] = None,
    n_chunks: int = 5,
) -> str:
    """Retrieve relevant passage chunks for a specific question.

    Returns the most relevant article passages so you can synthesize a
    grounded answer with numbered source citations.  Use this for specific
    factual questions ("Does Cribl support HIPAA?", "How can an AI agent interact with Ocient?").

    Args:
        question: The specific question to answer.
        company: Optional filter — one of 'cribl', 'ocient', or 'xsiam'.
        n_chunks: Number of passage chunks to retrieve (default 5, max 10).
    """
    n_chunks = min(int(n_chunks), 10)
    vec = _serialize(_embed(question))
    company_sql, company_params = _company_clause(company)

    conn = _open_db()
    try:
        rows = conn.execute(
            f"""
            SELECT ci.chunk_text, ci.title, ci.url, ci.company,
                   ci.published_date, ci.chunk_index, e.distance
            FROM vec_chunk_embeddings e
            INNER JOIN vec_chunk_items ci ON e.id = ci.id
            WHERE e.embedding MATCH ? AND k = ?{company_sql}
            ORDER BY e.distance
            """,
            [vec, n_chunks * 4, *company_params],
        ).fetchall()
    finally:
        conn.close()

    chunks = [r for r in rows if _score(r["distance"]) >= SCORE_THRESHOLD][:n_chunks]

    if not chunks:
        return "No relevant passages found for that question."

    lines = [f"**Passage chunks for:** {question}\n"]
    for i, c in enumerate(chunks, 1):
        score = _score(c["distance"])
        date = c["published_date"] or "n/d"
        lines.append(
            f"**[{i}] {c['title']}** ({c['company']}, {date}) | Score: {score}\n"
            f"<{c['url']}>\n\n"
            f"{c['chunk_text']}"
        )
    return "\n\n---\n\n".join(lines)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not OPENROUTER_API_KEY:
        raise SystemExit("OPENROUTER_API_KEY is not set — cannot start digest MCP server")
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Database not found at {DB_PATH} — run the digest pipeline first")
    mcp.run(transport="stdio")
