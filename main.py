import argparse
import logging
from datetime import date
from pathlib import Path

from config import setup_logging, settings
from publisher.github_pages import GitHubPagesPublisher
from scrapers.cribl import CriblScraper
from scrapers.ocient import OcientScraper
from storage.db import ArticleDB
from storage.models import ArticleRecord, ProductUpdate, vec_id_for
from storage.vec_client import VecClient
from summarizer import Summarizer

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape product updates and publish to GitHub Pages")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape only — skip LLM summarization, vector store writes, and GitHub push",
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

    with ArticleDB(settings.sqlite_db_path) as db:
        scrapers = []
        if args.site in (None, "cribl"):
            scrapers.append(CriblScraper())
        if args.site in (None, "ocient"):
            scrapers.append(OcientScraper())

        if args.dry_run:
            all_pages = []
            for scraper in scrapers:
                pages = scraper.run(db)
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
            scraper_infos = [
                {"company": s.company, "sources": s.sources, "exclusions": s.exclusions}
                for s in scrapers
            ]
            publisher = GitHubPagesPublisher(db)
            publisher.render_dry_run(all_pages, Path("data/dry-run"), scraper_infos)
            logger.info("[dry-run] Skipping LLM, vector store, and GitHub push")
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
