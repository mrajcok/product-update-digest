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
from storage.models import ArticleRecord, ProductUpdate, vec_id_for
from storage.vec_client import VecClient
from summarizer import Summarizer

logger = logging.getLogger(__name__)


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

    # Fetch the curated suggestion list only on failure
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape news and blog posts and publish to GitHub Pages")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape only — skip vector store writes and GitHub push (add --summarize to also call the LLM)",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="With --dry-run: call the free LLM model to generate summaries (requires a valid OPENROUTER_API_KEY)",
    )
    parser.add_argument(
        "--site",
        choices=["cribl", "ocient"],
        help="Run only one scraper (default: both)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    setup_logging()

    logger.info("Starting product-update-digest (dry_run=%s, site=%s)", args.dry_run, args.site)

    if args.dry_run and args.summarize:
        _assert_model_available(settings.openrouter_dry_run_summarization_model)
    elif not args.dry_run:
        _assert_model_available(settings.openrouter_summarization_model)

    with ArticleDB(settings.sqlite_db_path) as db:
        scrapers = []
        if args.site in (None, "ocient"):
            scrapers.append(OcientScraper())
        if args.site in (None, "cribl"):
            scrapers.append(CriblScraper())

        if args.dry_run:
            all_pages = []
            for scraper in scrapers:
                pages = scraper.run(db, limit=2)
                logger.info("[dry-run] %s: %d new/updated page(s) found", scraper.company, len(pages))
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
                        "[dry-run] [%d/%d] %s | %s | %s | %s\n  Title: %s\n  URL:   %s\n  Text (%s chars): %s%s",
                        i, len(pages), page.company, page.category,
                        page.published_date or "no date", age_str,
                        page.title, page.url,
                        f"{len(page.raw_text):,}",
                        preview, "…" if len(page.raw_text) > 400 else "",
                    )
                all_pages.extend(pages)

            summaries: dict[str, str] = {}
            if args.summarize:
                summarizer = Summarizer(model=settings.openrouter_dry_run_summarization_model)
                for page in all_pages:
                    logger.info("[dry-run] summarizing %s", page.url)
                    summaries[page.url] = summarizer.summarize(page)
            else:
                logger.info("[dry-run] skipping summarization (use --summarize to enable)")

            scraper_infos = [
                {"company": s.company, "sources": s.sources, "exclusions": s.exclusions}
                for s in scrapers
            ]
            publisher = GitHubPagesPublisher(db)
            publisher.render_dry_run(all_pages, Path("data/dry-run"), scraper_infos, summaries)
            logger.info("[dry-run] Skipping vector store writes and GitHub push")
            return

        summarizer = Summarizer()
        vec = VecClient()
        publisher = GitHubPagesPublisher(db)
        new_updates: list[ProductUpdate] = []

        for scraper in scrapers:
            pages = scraper.run(db)
            logger.info("%s: %d new/updated pages", scraper.company, len(pages))

            for page in pages:
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
            scraper_infos = [
                {"company": s.company, "sources": s.sources, "exclusions": s.exclusions}
                for s in scrapers
            ]
            publisher.publish(scraper_infos)
        else:
            logger.info("No new updates — skipping publish")


if __name__ == "__main__":
    main()
