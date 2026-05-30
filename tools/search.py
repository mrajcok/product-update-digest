#!/usr/bin/env python3
"""
Interactive semantic search over the news and blog posts vector store.

Usage:
    uv run python tools/search.py
    uv run python tools/search.py --company cribl
    uv run python tools/search.py --results 10
    uv run python tools/search.py --temp        # search the --stage vector dry-run store
"""
import argparse
import sys
import textwrap
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402 — after load_dotenv
from storage.vec_client import VecClient  # noqa: E402

_VEC_TEST_DB = Path("data/dry-run/vec_test.db")

COMPANY_COLORS = {"cribl": "\033[94m", "ocient": "\033[93m"}  # blue / yellow
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _run(company_filter: str | None, n_results: int, use_temp: bool) -> None:
    if use_temp:
        db_path = str(_VEC_TEST_DB)
        if not _VEC_TEST_DB.exists():
            print(f"Temp vector store not found at {db_path}. Run --stage vector first.")
            sys.exit(1)
    else:
        db_path = settings.sqlite_db_path

    try:
        vec = VecClient(db_path)
    except Exception as e:
        print(f"Error opening vector store at {db_path}: {e}")
        sys.exit(1)

    label = f"temp ({db_path})" if use_temp else db_path
    total = vec.count(company=company_filter)
    print(f"Vector store: {total} document(s)" + (f" for {company_filter}" if company_filter else "") + f"  [{label}]")
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
                results = vec.search(query, company=company_filter, n_results=n_results)
            except Exception as e:
                print(f"Query error: {e}")
                continue

            if not results:
                print("No results found.")
                continue

            for i, (update, distance) in enumerate(results, 1):
                color = COMPANY_COLORS.get(update.company, "")
                category = update.category.replace("_", " ")
                date = update.published_date or update.scraped_at[:10]
                score = 1 - distance  # cosine distance → similarity
                print(f"\n{BOLD}[{i}]{RESET} {color}{update.company.upper()}{RESET}  {DIM}{category}  {date}  score={score:.2f}{RESET}")
                print(f"  {BOLD}{update.title}{RESET}")
                if update.summary:
                    wrapped = textwrap.fill(update.summary, width=90, initial_indent="  ", subsequent_indent="  ")
                    print(wrapped)
                print(f"  {DIM}{update.url}{RESET}")

            print()

    except KeyboardInterrupt:
        pass

    vec.close()
    print("Bye.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic search over news and blog posts")
    parser.add_argument("--company", choices=["cribl", "ocient"], help="Filter to one company")
    parser.add_argument("--results", type=int, default=5, metavar="N", help="Number of results (default: 5)")
    parser.add_argument("--temp", action="store_true", help="Search the --stage vector dry-run store instead of production")
    args = parser.parse_args()
    _run(company_filter=args.company, n_results=args.results, use_temp=args.temp)


if __name__ == "__main__":
    main()
