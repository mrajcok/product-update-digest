import argparse
import logging
import sys

from config import setup_logging, settings
from publisher.github_pages import GitHubPagesPublisher
from scrapers.cribl import CriblScraper
from scrapers.ocient import OcientScraper
from storage.chroma_client import ProductUpdatesChromaClient
from storage.db import ArticleDB
from storage.models import ArticleRecord, ProductUpdate, chroma_id_for
from summarizer import Summarizer

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape product updates and publish to GitHub Pages")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape only — skip LLM summarization, Chroma writes, and GitHub push",
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
            for scraper in scrapers:
                pages = scraper.run(db)
                logger.info("[dry-run] %s: %d new/updated pages found", scraper.company, len(pages))
            logger.info("[dry-run] Skipping LLM, Chroma, and GitHub push")
            return

        summarizer = Summarizer()
        chroma = ProductUpdatesChromaClient()
        publisher = GitHubPagesPublisher(db)
        new_updates: list[ProductUpdate] = []

        for scraper in scrapers:
            pages = scraper.run(db)
            logger.info("%s: %d new/updated pages", scraper.company, len(pages))

            for page in pages:
                summary = summarizer.summarize(page)
                cid = chroma_id_for(page.url)

                update = ProductUpdate.from_scraped_page(page, summary)
                chroma.upsert(update, cid)

                existing = db.get_by_url(page.url)
                first_scraped_at = existing.first_scraped_at if existing else None
                record = ArticleRecord.from_scraped_page(
                    page,
                    chroma_id=cid,
                    first_scraped_at=first_scraped_at,
                    summary=summary,
                )
                db.upsert(record)
                new_updates.append(update)
                logger.debug("Processed %s", page.url)

        if new_updates:
            logger.info("Publishing %d updates to GitHub Pages", len(new_updates))
            publisher.publish()
        else:
            logger.info("No new updates — skipping publish")


if __name__ == "__main__":
    main()
