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
import os
import sqlite3
import struct
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# ── Configuration from environment ────────────────────────────────────────────

DB_PATH = os.environ.get("DIGEST_DB_PATH", "/home/mark/digest-data/product_updates.db")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("OPENROUTER_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMENSIONS", "4096"))
RAG_MODEL = os.environ.get("OPENROUTER_RAG_MODEL", "qwen/qwen3.7-plus")
SCORE_THRESHOLD = float(os.environ.get("SEARCH_SCORE_THRESHOLD", "0.10"))

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

mcp = FastMCP("digest-search")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _embed(text: str) -> list[float]:
    """Call OpenRouter embeddings endpoint and return a float list."""
    resp = httpx.post(
        f"{OPENROUTER_BASE}/embeddings",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        json={"model": EMBEDDING_MODEL, "input": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


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
    about Palo Alto?").

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
    """Answer a specific question using Retrieval-Augmented Generation over
    product update digests.

    Retrieves the most relevant passage chunks, then calls an LLM for a
    grounded answer with source citations.  Use this for specific factual
    questions ("Does Cribl support HIPAA?", "What embedding models does Ocient
    use?").

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

    context_parts = []
    for i, c in enumerate(chunks, 1):
        context_parts.append(
            f"[Source {i}] {c['title']} ({c['company']}, {c['published_date'] or 'n/d'})\n{c['chunk_text']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    prompt = (
        f"Answer the following question using only the provided sources. "
        f"Cite sources by number (e.g. [1]). "
        f"If the sources do not contain enough information, say so.\n\n"
        f"Question: {question}\n\n"
        f"Sources:\n{context}"
    )

    resp = httpx.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        json={
            "model": RAG_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    answer = resp.json()["choices"][0]["message"]["content"]

    source_lines = []
    for i, c in enumerate(chunks, 1):
        source_lines.append(f"  [{i}] {c['title']} — <{c['url']}>")

    return (
        f"**Q: {question}**\n\n"
        f"{answer}\n\n"
        f"**Sources:**\n" + "\n".join(source_lines)
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not OPENROUTER_API_KEY:
        raise SystemExit("OPENROUTER_API_KEY is not set — cannot start digest MCP server")
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Database not found at {DB_PATH} — run the digest pipeline first")
    mcp.run(transport="stdio")
