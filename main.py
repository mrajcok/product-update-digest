import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import httpx

from config import setup_logging, settings
from publisher.github_pages import GitHubPagesPublisher
from scrapers.cribl import CriblScraper
from scrapers.ocient import OcientScraper
from storage.db import ArticleDB
from storage.models import ArticleRecord, ProductUpdate, ScrapedPage, normalize_url, vec_id_for
from storage.vec_client import VecClient
from summarizer import Summarizer

logger = logging.getLogger(__name__)

_DRY_RUN_DIR = Path("data/dry-run")


# ---------------------------------------------------------------------------
# Model availability checks
# ---------------------------------------------------------------------------

def _assert_ollama_available(model: str) -> None:
    """Exit with a clear error if Ollama is not reachable or the model is not available."""
    base_url = settings.ollama_base_url.rstrip("/")
    try:
        resp = httpx.get(f"{base_url}/models", timeout=5.0)
    except Exception as exc:
        logger.error(
            "Cannot reach Ollama at %s: %s\n"
            "Ensure Ollama is running (ollama serve) on port 11434 by default.",
            base_url, exc,
        )
        sys.exit(1)

    if not resp.is_success:
        logger.error("Ollama server at %s returned HTTP %d.", base_url, resp.status_code)
        sys.exit(1)

    available = [m["id"] for m in resp.json().get("data", [])]
    if available and model not in available:
        logger.error(
            "Model %r not found in Ollama.\nAvailable model(s): %s\nRun: ollama pull %s",
            model, ", ".join(available), model,
        )
        sys.exit(1)


def _assert_model_available(model_id: str) -> None:
    """Exit with a clear error if model_id is not usable on OpenRouter."""
    if settings.openrouter_api_key == "dummy":
        return  # no key — let the actual call produce the 401

    # Probe with a 1-token call — the only reliable way to confirm a model
    # has working providers (listed models can still have no active providers).
    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json={"model": model_id, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=15.0,
        )
    except Exception as exc:
        logger.warning("Could not probe model %r (%s) — skipping check", model_id, exc)
        return

    if resp.status_code == 200:
        return

    _FREE_PREFIXES = ("google/", "meta-llama/", "mistralai/")
    suggestions: list[str] = []
    try:
        models_resp = httpx.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            timeout=10.0,
        )
        if models_resp.is_success:
            suggestions = sorted(
                m["id"] for m in models_resp.json().get("data", [])
                if m["id"].endswith(":free") and m["id"].startswith(_FREE_PREFIXES)
            )
    except Exception:
        pass

    env_var = "OPENROUTER_DRY_RUN_SUMMARIZATION_MODEL" if ":free" in model_id else "OPENROUTER_SUMMARIZATION_MODEL"
    logger.error(
        "Model %r is not available on OpenRouter (status %d).\n"
        "Update %s in your .env.\n"
        "Available free models (Google / Meta / Mistral):\n  %s",
        model_id,
        resp.status_code,
        env_var,
        "\n  ".join(suggestions) if suggestions else "(none found)",
    )
    sys.exit(1)


def _make_summarizer(dry_run: bool) -> Summarizer:
    if settings.ollama_base_url:
        model = (
            settings.ollama_dry_run_summarization_model or settings.ollama_summarization_model
            if dry_run
            else settings.ollama_summarization_model
        )
        return Summarizer(model=model, base_url=settings.ollama_base_url, api_key="ollama")
    return Summarizer(model=settings.openrouter_dry_run_summarization_model if dry_run else None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_scrapers(site: str | None) -> list:
    scrapers = []
    if site in (None, "ocient"):
        scrapers.append(OcientScraper())
    if site in (None, "cribl"):
        scrapers.append(CriblScraper())
    return scrapers


def _scraper_infos(scrapers: list) -> list[dict]:
    return [{"company": s.company, "sources": s.sources, "exclusions": s.exclusions} for s in scrapers]


def _scrape_and_cache(scraper, db: ArticleDB, limit: int, category: str | None = None) -> list[ScrapedPage]:
    """Run scraper, persist each page to DB and article_text cache, return pages."""
    pages = scraper.run(db, limit=limit, category=category)
    for page in pages:
        nurl = normalize_url(page.url)
        db.save_text(nurl, page.raw_text)
    return pages


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

def _run_scrape(args: argparse.Namespace, db: ArticleDB) -> None:
    scrapers = _build_scrapers(args.site)
    all_pages: list[ScrapedPage] = []
    for scraper in scrapers:
        pages = _scrape_and_cache(scraper, db, limit=args.limit, category=args.category)
        logger.info("[stage:scrape] %s: %d page(s) scraped", scraper.company, len(pages))
        for i, page in enumerate(pages, 1):
            preview = " ".join(page.raw_text.split())[:400]
            if page.published_date:
                try:
                    age_days = (date.today() - date.fromisoformat(page.published_date)).days
                    age_str = f"{age_days}d ago"
                except ValueError:
                    age_str = "unknown age"
            else:
                age_str = "unknown age"
            logger.info(
                "[stage:scrape] [%d/%d] %s | %s | %s | %s\n  Title: %s\n  URL:   %s\n  Text (%s chars): %s%s",
                i, len(pages), page.company, page.category,
                page.published_date or "no date", age_str,
                page.title, page.url,
                f"{len(page.raw_text):,}",
                preview, "…" if len(page.raw_text) > 400 else "",
            )
        all_pages.extend(pages)

    publisher = GitHubPagesPublisher(db)
    publisher.render_scrape_preview(all_pages, _DRY_RUN_DIR, _scraper_infos(scrapers))


def _run_summarize(args: argparse.Namespace, db: ArticleDB) -> None:
    if settings.ollama_base_url:
        model = settings.ollama_dry_run_summarization_model or settings.ollama_summarization_model
        _assert_ollama_available(model)
    else:
        _assert_model_available(settings.openrouter_dry_run_summarization_model)

    scrapers = _build_scrapers(args.site)
    summarizer = _make_summarizer(dry_run=True)
    records: list[ArticleRecord] = []

    for scraper in scrapers:
        cached = db.latest_article_with_text(scraper.company, category=args.category)
        if cached:
            raw_text = db.get_text(cached.normalized_url) or ""
            page = ScrapedPage(
                url=cached.url,
                company=cached.company,
                category=cached.category,
                title=cached.title,
                raw_text=raw_text,
                published_date=cached.published_date,
            )
            logger.info("[stage:summarize] %s: using cached article %s", scraper.company, cached.url)
        else:
            logger.info("[stage:summarize] %s: no cached article — scraping %d", scraper.company, args.limit)
            pages = _scrape_and_cache(scraper, db, limit=args.limit, category=args.category)
            if not pages:
                logger.warning("[stage:summarize] %s: no pages found", scraper.company)
                continue
            page = pages[0]

        logger.info("[stage:summarize] summarizing %s", page.url)
        summary = summarizer.summarize(page)

        # Persist summary back to DB
        existing = db.get_by_url(page.url)
        record = ArticleRecord.from_scraped_page(
            page,
            vec_id=existing.vec_id if existing else None,
            first_scraped_at=existing.first_scraped_at if existing else None,
            summary=summary,
        )
        db.upsert(record)
        records.append(record)

    publisher = GitHubPagesPublisher(db)
    publisher.render_summary_preview(records, _DRY_RUN_DIR, _scraper_infos(scrapers))


def _run_vector(args: argparse.Namespace, db: ArticleDB) -> None:
    scrapers = _build_scrapers(args.site)

    # Ensure each requested company has at least one cached article
    for scraper in scrapers:
        if not db.latest_article_with_text(scraper.company, category=args.category):
            logger.info("[stage:vector] %s: no cached text — scraping %d", scraper.company, args.limit)
            _scrape_and_cache(scraper, db, limit=args.limit, category=args.category)

    # Rebuild vector store from all cached articles
    vec = VecClient()
    vec._conn.execute("DELETE FROM vec_items")
    vec._conn.execute("DELETE FROM vec_embeddings")
    vec._conn.commit()
    logger.info("[stage:vector] cleared existing vector store")

    all_records = db.get_all(company=args.site, category=args.category)
    upserted = 0
    for record in all_records:
        raw_text = db.get_text(record.normalized_url)
        if not raw_text:
            continue
        page = ScrapedPage(
            url=record.url,
            company=record.company,
            category=record.category,
            title=record.title,
            raw_text=raw_text,
            published_date=record.published_date,
        )
        update = ProductUpdate.from_scraped_page(page, summary=record.summary)
        vid = vec_id_for(record.url)
        vec.upsert(update, vid)
        upserted += 1

    vec.close()
    logger.info("[stage:vector] indexed %d document(s) — run: python tools/search.py", upserted)

    all_updates = VecClient().get_all(company=args.site)
    publisher = GitHubPagesPublisher(db)
    publisher.render_vector_preview(all_updates, _DRY_RUN_DIR)


def _run_render(args: argparse.Namespace, db: ArticleDB) -> None:
    publisher = GitHubPagesPublisher(db)
    publisher.render_from_db(_DRY_RUN_DIR, _scraper_infos(_build_scrapers(args.site)))
    logger.info("[stage:render] HTML written to %s — review, then run --stage publish", _DRY_RUN_DIR)


def _run_publish(args: argparse.Namespace, db: ArticleDB) -> None:
    publisher = GitHubPagesPublisher(db)
    publisher.publish(_scraper_infos(_build_scrapers(args.site)))


def _run_full_pipeline(args: argparse.Namespace, db: ArticleDB) -> None:
    if settings.ollama_base_url:
        _assert_ollama_available(settings.ollama_summarization_model)
    else:
        _assert_model_available(settings.openrouter_summarization_model)

    scrapers = _build_scrapers(args.site)
    summarizer = _make_summarizer(dry_run=False)
    vec = VecClient()
    publisher = GitHubPagesPublisher(db)
    new_updates: list[ProductUpdate] = []

    for scraper in scrapers:
        pages = scraper.run(db)
        logger.info("%s: %d new/updated pages", scraper.company, len(pages))

        for page in pages:
            # Persist raw text for future staged runs
            db.save_text(normalize_url(page.url), page.raw_text)

            summary = summarizer.summarize(page)
            vid = vec_id_for(page.url)

            update = ProductUpdate.from_scraped_page(page, summary)
            vec.upsert(update, vid)

            existing = db.get_by_url(page.url)
            first_scraped_at = existing.first_scraped_at if existing else None
            record = ArticleRecord.from_scraped_page(
                page,
                vec_id=vid,
                first_scraped_at=first_scraped_at,
                summary=summary,
            )
            db.upsert(record)
            new_updates.append(update)
            logger.debug("Processed %s", page.url)

    if new_updates:
        logger.info("Publishing %d updates to GitHub Pages", len(new_updates))
        publisher.publish(_scraper_infos(scrapers))
    else:
        logger.info("No new updates — skipping publish")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape news and blog posts and publish to GitHub Pages")
    parser.add_argument(
        "--stage",
        choices=["scrape", "summarize", "vector", "render", "publish"],
        help=(
            "Run one pipeline stage: "
            "scrape (fetch + cache, render preview), "
            "summarize (LLM summary from cache, render preview), "
            "vector (rebuild vec store from cache, render preview), "
            "render (render full site from DB to data/dry-run/ for review), "
            "publish (rebuild full site from DB and push to GitHub Pages)"
        ),
    )
    parser.add_argument(
        "--site",
        choices=["cribl", "ocient"],
        help="Run only one scraper (default: both)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        metavar="N",
        help="Max articles per company when scraping in stage mode (default: 1)",
    )
    parser.add_argument(
        "--category",
        choices=["blog", "press_release", "product"],
        help="Filter to one article category (default: all)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    setup_logging()

    logger.info("Starting product-update-digest (stage=%s, site=%s)", args.stage, args.site)

    with ArticleDB(settings.sqlite_db_path) as db:
        if args.stage == "scrape":
            _run_scrape(args, db)
        elif args.stage == "summarize":
            _run_summarize(args, db)
        elif args.stage == "vector":
            _run_vector(args, db)
        elif args.stage == "render":
            _run_render(args, db)
        elif args.stage == "publish":
            _run_publish(args, db)
        else:
            _run_full_pipeline(args, db)


if __name__ == "__main__":
    main()
