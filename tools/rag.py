#!/usr/bin/env python3
"""
RAG (Retrieval-Augmented Generation) over the vector store chunk index.

Retrieves the most relevant article chunks for a question, then passes them
to an LLM to produce a grounded answer with citations.

Usage:
    uv run python tools/rag.py
    uv run python tools/rag.py --company cribl
    uv run python tools/rag.py --results 6
    uv run python tools/rag.py --temp           # use --stage vector dry-run store
    uv run python tools/rag.py --show-chunks    # print retrieved chunks before the answer
    uv run python tools/rag.py --discord        # format answer as Discord markdown
"""
import argparse
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402
from storage.vec_client import ChunkResult, VecClient  # noqa: E402

_VEC_TEST_DB = Path("data/dry-run/vec_test.db")

_SYSTEM_PROMPT = """\
You are a product intelligence assistant with access to recent updates from Cribl and Ocient.
Answer the user's question using only the provided source passages.
Be specific: name products, features, and versions when the passages mention them.
Cite each piece of information with the source number in brackets, e.g. [1].
If the passages don't contain enough information to answer confidently, say so clearly — do not speculate.
Format your answer in markdown."""

_CONTEXT_HEADER = "Source passages retrieved from the knowledge base:\n\n"


def _build_context(chunks: list[ChunkResult]) -> str:
    parts = []
    for i, chunk in enumerate(chunks, 1):
        date = chunk.published_date or "unknown date"
        header = f"[{i}] {chunk.company.upper()} | {chunk.category.replace('_', ' ')} | {date} | {chunk.title}"
        parts.append(f"{header}\n{chunk.chunk_text}")
    return _CONTEXT_HEADER + "\n\n---\n\n".join(parts)


def _resolve_llm() -> tuple[str, str, str]:
    """Return (model, base_url, api_key) honouring Ollama override and RAG model fallbacks."""
    if settings.ollama_base_url:
        model = settings.ollama_rag_model or settings.ollama_summarization_model
        return model, settings.ollama_base_url, "ollama"
    model = settings.openrouter_rag_model or settings.openrouter_summarization_model
    return model, "https://openrouter.ai/api/v1", settings.openrouter_api_key


def _call_llm(question: str, chunks: list[ChunkResult]) -> tuple[str, str, float]:
    """Return (answer, model_name, elapsed_seconds)."""
    from openai import OpenAI
    from tenacity import Retrying, before_sleep_log, stop_after_attempt, wait_exponential
    import logging

    logger = logging.getLogger(__name__)
    model, base_url, api_key = _resolve_llm()
    client = OpenAI(api_key=api_key, base_url=base_url)
    context = _build_context(chunks)
    user_message = f"{context}\n\nQuestion: {question}"

    t0 = time.monotonic()
    for attempt in Retrying(
        stop=stop_after_attempt(settings.max_api_retries),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    ):
        with attempt:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
            )
            answer = resp.choices[0].message.content or ""
            return answer, model, time.monotonic() - t0
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _strip_md_headers(text: str) -> str:
    """Convert ## headings to bold for Discord (which ignores heading syntax)."""
    def _replace(m: re.Match) -> str:
        heading = m.group(1).strip()
        return f"**{heading}**" if heading else ""
    return re.sub(r"^#{1,6}\s*(.*)", _replace, text, flags=re.MULTILINE)


def _print_chunks_rich(chunks: list[ChunkResult]) -> None:
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    console.print("\n[bold dim]Retrieved chunks:[/bold dim]")
    for i, chunk in enumerate(chunks, 1):
        date = chunk.published_date or "unknown date"
        title = f"[{i}] {chunk.company.upper()} | {chunk.category.replace('_', ' ')} | {date} | score={chunk.score:.2f}"
        console.print(Panel(chunk.chunk_text, title=title, title_align="left", border_style="dim"))


def _print_answer_rich(answer: str, model: str, elapsed: float) -> None:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.rule import Rule

    console = Console()
    console.print()
    console.print(Markdown(answer))
    console.print(Rule(style="dim"))
    console.print(f"[dim]{model} — {elapsed:.1f}s[/dim]")


def _format_discord(question: str, chunks: list[ChunkResult], answer: str, model: str) -> str:
    answer_discord = _strip_md_headers(answer)
    sources = "\n".join(
        f"[{i}] **{c.company.upper()}** — {c.title} <{c.url}>"
        for i, c in enumerate(chunks, 1)
    )
    return f"**Q: {question}**\n\n{answer_discord}\n\n**Sources:**\n{sources}\n\n*{model}*"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _run(company_filter: str | None, n_results: int, use_temp: bool,
         show_chunks: bool, discord_mode: bool, min_score: float | None) -> None:
    if use_temp:
        db_path = str(_VEC_TEST_DB)
        if not _VEC_TEST_DB.exists():
            print(f"Temp vector store not found at {db_path}. Run: digest --stage vector")
            sys.exit(1)
    else:
        db_path = settings.sqlite_db_path

    try:
        vec = VecClient(db_path)
    except Exception as e:
        print(f"Error opening vector store at {db_path}: {e}")
        sys.exit(1)

    model, _, _ = _resolve_llm()
    label = f"temp ({db_path})" if use_temp else db_path
    total = vec.count(company=company_filter)
    print(f"Vector store: {total} article(s)" + (f" for {company_filter}" if company_filter else "") + f"  [{label}]")
    print(f"Embedding model: {settings.openrouter_embedding_model}  |  RAG model: {model}  |  chunks per query: {n_results}"
          f"  |  search score threshold: {settings.search_score_threshold:.2f}" + (f"  [override: {min_score:.2f}]" if min_score is not None else ""))
    print("Performs semantic search (cosine similarity) over article-chunk vectors and returns an LLM-generated answer.")
    print("Type a question and press Enter. Ctrl-C or empty input to exit.\n")

    from rich.console import Console
    console = Console()

    try:
        while True:
            try:
                question = input("Question: ").strip()
            except EOFError:
                break
            if not question:
                break

            try:
                with console.status("[dim]Finding relevant text from stored articles…[/dim]"):
                    chunks, n_candidates = vec.search_chunks(question, company=company_filter,
                                                             n_results=n_results, min_score=min_score)
            except Exception as e:
                print(f"Retrieval error: {e}")
                continue

            if not chunks:
                if n_candidates:
                    threshold = min_score if min_score is not None else settings.search_score_threshold
                    print(f"No chunks above score threshold ({threshold}). "
                          f"{n_candidates} candidate(s) found but all scored too low. "
                          f"Try --min-score 0 to see them.")
                else:
                    print("No relevant chunks found.")
                continue

            if show_chunks and not discord_mode:
                _print_chunks_rich(chunks)

            try:
                with console.status("[dim]Generating answer…[/dim]"):
                    answer, model_used, elapsed = _call_llm(question, chunks)
            except Exception as e:
                print(f"LLM error: {e}")
                continue

            if discord_mode:
                print(_format_discord(question, chunks, answer, model_used))
            else:
                _print_answer_rich(answer, model_used, elapsed)

    except KeyboardInterrupt:
        pass

    vec.close()
    print("Bye.")


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG question-answering over product update chunks")
    parser.add_argument("--company", choices=["cribl", "ocient"], help="Restrict retrieval to one company")
    parser.add_argument("--results", type=int, default=5, metavar="N", help="Chunks to retrieve per query (default: 5)")
    parser.add_argument("--temp", action="store_true", help="Use --stage vector dry-run store instead of production")
    parser.add_argument("--show-chunks", action="store_true", help="Print retrieved chunks before the answer")
    parser.add_argument("--discord", action="store_true", help="Format answer as Discord markdown")
    parser.add_argument("--min-score", type=float, default=None, metavar="N",
                        help="Override SEARCH_SCORE_THRESHOLD for this run (e.g. 0 to see all results)")
    args = parser.parse_args()
    _run(
        company_filter=args.company,
        n_results=args.results,
        use_temp=args.temp,
        show_chunks=args.show_chunks,
        discord_mode=args.discord,
        min_score=args.min_score,
    )


if __name__ == "__main__":
    main()
