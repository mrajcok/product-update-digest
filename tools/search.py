#!/usr/bin/env python3
"""
Interactive semantic search over the news and blog posts vector store.

Usage:
    uv run python tools/search.py
    uv run python tools/search.py --company cribl
    uv run python tools/search.py --results 10
    uv run python tools/search.py --temp        # search the --stage vector dry-run store
    uv run python tools/search.py --discord     # print Discord-formatted output instead
"""
import argparse
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402 — after load_dotenv
from storage.models import ProductUpdate  # noqa: E402
from storage.vec_client import VecClient  # noqa: E402

_VEC_TEST_DB = Path("data/dry-run/vec_test.db")


# ---------------------------------------------------------------------------
# Discord formatter
# ---------------------------------------------------------------------------

def _strip_md_headers(text: str) -> str:
    """Convert ## headings to bold; leave everything else intact."""
    def _replace(m: re.Match) -> str:
        heading = m.group(1).strip()
        return f"**{heading}**" if heading else ""
    return re.sub(r"^#{1,6}\s*(.*)", _replace, text, flags=re.MULTILINE)


def format_results_for_discord(results: list[tuple[ProductUpdate, float]]) -> str:
    """Format search results as Discord markdown (importable for a Discord bot)."""
    parts = []
    for i, (update, distance) in enumerate(results, 1):
        category = update.category.replace("_", " ")
        date = update.published_date or update.scraped_at[:10]
        score = 1 - distance
        header = f"**[{i}] {update.company.upper()}** • {category} • {date} • score={score:.2f}"
        title = f"**{update.title}**"
        summary = _strip_md_headers(update.summary) if update.summary else ""
        parts.append(f"{header}\n{title}\n{summary}\n{update.url}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Terminal renderer (rich)
# ---------------------------------------------------------------------------

def _print_results_rich(results: list[tuple[ProductUpdate, float]]) -> None:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.rule import Rule

    console = Console()
    COMPANY_COLORS = {"cribl": "bright_blue", "ocient": "bright_yellow"}

    for i, (update, distance) in enumerate(results, 1):
        color = COMPANY_COLORS.get(update.company, "white")
        category = update.category.replace("_", " ")
        date = update.published_date or update.scraped_at[:10]
        score = 1 - distance

        console.print()
        console.print(
            f"[bold][{i}][/bold] [{color}]{update.company.upper()}[/{color}]"
            f"  [dim]{category}  {date}  score={score:.2f}[/dim]"
        )
        console.print(f"[bold]{update.title}[/bold]")
        if update.summary:
            console.print(Markdown(update.summary))
        console.print(f"[dim]{update.url}[/dim]")

    console.print(Rule(style="dim"))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _run(company_filter: str | None, n_results: int, use_temp: bool, discord_mode: bool,
         min_score: float | None) -> None:
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
    print(f"Embedding model: {settings.openrouter_embedding_model}  |  search score threshold: {settings.search_score_threshold:.2f}" + (f"  [override: {min_score:.2f}]" if min_score is not None else ""))
    print("Performs semantic search (cosine similarity) over whole-article vectors and returns matching article summaries.")
    print("Type a query and press Enter. Ctrl-C or empty input to exit.\n")

    from rich.console import Console
    console = Console()

    try:
        while True:
            try:
                query = input("Search: ").strip()
            except EOFError:
                break

            if not query:
                break

            try:
                with console.status("[dim]Searching stored articles…[/dim]"):
                    results, n_candidates = vec.search(query, company=company_filter,
                                                       n_results=n_results, min_score=min_score)
            except Exception as e:
                print(f"Query error: {e}")
                continue

            if not results:
                if n_candidates:
                    threshold = min_score if min_score is not None else settings.search_score_threshold
                    print(f"No results above score threshold ({threshold}). "
                          f"{n_candidates} candidate(s) found but all scored too low. "
                          f"Try --min-score 0 to see them.")
                else:
                    print("No results found.")
                continue

            if discord_mode:
                print(format_results_for_discord(results))
            else:
                _print_results_rich(results)

    except KeyboardInterrupt:
        pass

    vec.close()
    print("Bye.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic search over news and blog posts")
    parser.add_argument("--company", choices=["cribl", "ocient"], help="Filter to one company")
    parser.add_argument("--results", type=int, default=5, metavar="N", help="Number of results (default: 5)")
    parser.add_argument("--temp", action="store_true", help="Search the --stage vector dry-run store instead of production")
    parser.add_argument("--discord", action="store_true", help="Print Discord-formatted output (for testing bot output)")
    parser.add_argument("--min-score", type=float, default=None, metavar="N",
                        help="Override SEARCH_SCORE_THRESHOLD for this run (e.g. 0 to see all results)")
    args = parser.parse_args()
    _run(company_filter=args.company, n_results=args.results, use_temp=args.temp,
         discord_mode=args.discord, min_score=args.min_score)


if __name__ == "__main__":
    main()
