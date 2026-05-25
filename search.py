#!/usr/bin/env python3
"""
Interactive semantic search over the product updates Chroma collection.

Usage:
    .venv/bin/python search.py
    .venv/bin/python search.py --company cribl
    .venv/bin/python search.py --results 10
"""
import argparse
import sys
import textwrap
from typing import cast

import chromadb
from chromadb.api.types import Embeddable, EmbeddingFunction, IncludeEnum, QueryResult, Where
from chromadb.utils.embedding_functions.openai_embedding_function import OpenAIEmbeddingFunction
from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402 — after load_dotenv

COMPANY_COLORS = {"cribl": "\033[94m", "ocient": "\033[93m"}  # blue / yellow
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

_INCLUDE: list[IncludeEnum] = [
    IncludeEnum.documents,
    IncludeEnum.metadatas,
    IncludeEnum.distances,
]


def _build_collection() -> chromadb.Collection:
    ef = cast(
        EmbeddingFunction[Embeddable],
        OpenAIEmbeddingFunction(
            api_key=settings.openrouter_api_key,
            api_base="https://openrouter.ai/api/v1",
            model_name=settings.openrouter_embedding_model,
        ),
    )
    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    return client.get_or_create_collection(
        name=settings.chroma_collection_name,
        embedding_function=ef,
    )


def _display(results: QueryResult) -> None:
    docs = results.get("documents") or [[]]
    metas = results.get("metadatas") or [[]]
    distances = results.get("distances") or [[]]

    if not docs[0]:
        print("No results found.")
        return

    for i, (_, meta, dist) in enumerate(zip(docs[0], metas[0], distances[0]), 1):
        company = str(meta.get("company", ""))
        color = COMPANY_COLORS.get(company, "")
        category = str(meta.get("category", "")).replace("_", " ")
        date = str(meta.get("published_date") or str(meta.get("scraped_at", ""))[:10])
        title = str(meta.get("title", "(no title)"))
        url = str(meta.get("url", ""))
        summary = str(meta.get("summary", ""))
        score = 1 - dist  # cosine distance → similarity

        print(f"\n{BOLD}[{i}]{RESET} {color}{company.upper()}{RESET}  {DIM}{category}  {date}  score={score:.2f}{RESET}")
        print(f"  {BOLD}{title}{RESET}")
        if summary:
            wrapped = textwrap.fill(summary, width=90, initial_indent="  ", subsequent_indent="  ")
            print(wrapped)
        print(f"  {DIM}{url}{RESET}")


def _run(company_filter: str | None, n_results: int) -> None:
    try:
        collection = _build_collection()
    except Exception as e:
        print(f"Error connecting to Chroma at {settings.chroma_host}:{settings.chroma_port}: {e}")
        sys.exit(1)

    where: Where | None = {"company": {"$eq": company_filter}} if company_filter else None

    print(f"Connected to collection '{settings.chroma_collection_name}' ({collection.count()} documents)")
    if company_filter:
        print(f"Filtering to: {company_filter}")
    print("Type a query and press Enter. Ctrl-C or empty input to exit.\n")

    try:
        while True:
            try:
                query = input("Search: ").strip()
            except EOFError:
                break

            if not query:
                break

            try:
                results = collection.query(
                    query_texts=[query],
                    n_results=min(n_results, collection.count() or 1),
                    where=where,
                    include=_INCLUDE,
                )
            except Exception as e:
                print(f"Query error: {e}")
                continue

            _display(results)
            print()

    except KeyboardInterrupt:
        pass

    print("Bye.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic search over product updates")
    parser.add_argument("--company", choices=["cribl", "ocient"], help="Filter to one company")
    parser.add_argument("--results", type=int, default=5, metavar="N", help="Number of results (default: 5)")
    args = parser.parse_args()
    _run(company_filter=args.company, n_results=args.results)


if __name__ == "__main__":
    main()
