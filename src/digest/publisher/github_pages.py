import html as html_module
import logging
import re
import shutil
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from git import Repo
import markdown as md
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from digest.storage.db import ArticleDB
from digest.storage.models import ArticleRecord, ProductUpdate, ScrapedPage

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
COMPANIES = ["cribl", "ocient", "xsiam"]

def _sort_key(record: ArticleRecord) -> str:
    return record.published_date or record.last_scraped_at or ""


def _top_per_company(company_updates: dict[str, list[ArticleRecord]], n: int) -> list[ArticleRecord]:
    """Return up to n most-recent records per company, in COMPANIES order."""
    result = []
    for records in company_updates.values():
        result.extend(records[:n])
    return result


class GitHubPagesPublisher:
    def __init__(self, db: ArticleDB) -> None:
        self._db = db
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=True,
        )
        self._env.filters["markdown"] = lambda text: Markup(md.markdown(text or ""))
        self._env.filters["plaintitle"] = lambda text: html_module.unescape(re.sub(r"<[^>]+>", "", text or ""))

    def publish(self, scraper_infos: list[dict] | None = None) -> None:
        all_records = self._db.get_all()
        ok_records = [r for r in all_records if r.status == "ok" and r.summary]

        from digest.config import settings
        company_updates: dict[str, list[ArticleRecord]] = {
            c: sorted(
                [r for r in ok_records if r.company == c],
                key=_sort_key,
                reverse=True,
            )[:settings.company_page_limit]
            for c in COMPANIES
        }
        top_updates = _top_per_company(company_updates, settings.index_per_company)

        html_files = self._render(top_updates, company_updates, scraper_infos)
        self._push_to_github(html_files)

    def render_from_db(self, out_dir: Path, scraper_infos: list[dict] | None = None, limit: int | None = None) -> None:
        """Render pages from DB to out_dir (no push). limit caps articles per company on company pages."""
        from digest.config import settings
        effective_limit = limit if limit is not None else settings.company_page_limit
        all_records = self._db.get_all()
        ok_records = [r for r in all_records if r.status == "ok" and r.summary]
        company_updates: dict[str, list[ArticleRecord]] = {
            c: sorted([r for r in ok_records if r.company == c], key=_sort_key, reverse=True)[:effective_limit]
            for c in COMPANIES
        }
        top_updates = _top_per_company(company_updates, settings.index_per_company)
        html_files = self._render(top_updates, company_updates, scraper_infos)
        self._write_to_dir(html_files, out_dir)
        rendered = sum(len(v) for v in company_updates.values())
        logger.info(
            "Rendered %d/%d record(s) from DB to %s (limit=%d per company)",
            rendered, len(ok_records), out_dir, effective_limit,
        )

    def render_scrape_preview(
        self,
        pages: list[ScrapedPage],
        out_dir: Path,
        scraper_infos: list[dict] | None = None,
    ) -> None:
        """Render scraped-page preview (no summaries) to out_dir."""
        from digest.config import settings
        records = [
            ArticleRecord.from_scraped_page(p, summary="[summary not generated]")
            for p in pages
        ]
        company_updates: dict[str, list[ArticleRecord]] = {
            c: sorted([r for r in records if r.company == c], key=_sort_key, reverse=True)
            for c in COMPANIES
        }
        top_updates = _top_per_company(company_updates, settings.index_per_company)
        html_files = self._render(top_updates, company_updates, scraper_infos)
        self._write_to_dir(html_files, out_dir)
        logger.info("[stage:scrape] HTML written to %s/index.html", out_dir)

    def render_summary_preview(
        self,
        records: list[ArticleRecord],
        out_dir: Path,
        scraper_infos: list[dict] | None = None,
    ) -> None:
        """Render index using production templates from already-summarized DB records."""
        from digest.config import settings
        company_updates: dict[str, list[ArticleRecord]] = {
            c: sorted([r for r in records if r.company == c], key=_sort_key, reverse=True)
            for c in COMPANIES
        }
        top_updates = _top_per_company(company_updates, settings.index_per_company)
        html_files = self._render(top_updates, company_updates, scraper_infos)
        self._write_to_dir(html_files, out_dir)
        logger.info("[stage:summarize] HTML written to %s/index.html", out_dir)

    def render_vector_preview(
        self,
        updates: list[ProductUpdate],
        out_dir: Path,
    ) -> None:
        """Render a full-text listing of all vector-indexed documents to out_dir."""
        tmpl = self._env.get_template("vector_preview.html.j2")
        html = tmpl.render(
            updates=updates,
            generated_at=datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p %Z"),
        )
        self._write_to_dir({"index.html": html}, out_dir)
        logger.info("[stage:vector] HTML written to %s/index.html", out_dir)

    def _render(
        self,
        top_updates: list[ArticleRecord],
        company_updates: dict[str, list[ArticleRecord]],
        scraper_infos: list[dict] | None = None,
    ) -> dict[str, str]:
        files: dict[str, str] = {}

        index_tmpl = self._env.get_template("index.html.j2")
        from digest.config import settings
        files["index.html"] = index_tmpl.render(
            updates=top_updates,
            index_per_company=settings.index_per_company,
            generated_at=datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p %Z"),
            scraper_infos=scraper_infos or [],
        )

        company_tmpl = self._env.get_template("company_index.html.j2")
        for company, records in company_updates.items():
            grouped = _group_by_month(records)
            files[f"{company}/index.html"] = company_tmpl.render(
                company=company,
                grouped=grouped,
                article_count=len(records),
                company_page_limit=settings.company_page_limit,
                generated_at=datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p %Z"),
            )

        return files

    def _write_to_dir(self, html_files: dict[str, str], out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in html_files.items():
            dest = out_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    def _push_to_github(self, html_files: dict[str, str]) -> None:
        from digest.config import settings  # deferred to avoid module-level Settings() at import time

        repo_url = (
            f"https://{settings.github_token}@github.com/{settings.github_repo}.git"
        )
        tmp = tempfile.mkdtemp(prefix="gh-pages-")
        try:
            logger.info("Cloning %s branch %s", settings.github_repo, settings.github_pages_branch)
            repo = Repo.clone_from(
                repo_url,
                tmp,
                branch=settings.github_pages_branch,
                depth=1,
            )

            for rel_path, content in html_files.items():
                dest = Path(tmp) / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                logger.debug("Wrote %s", rel_path)

            repo.git.add(A=True)
            if repo.is_dirty(index=True):
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                repo.index.commit(f"chore: update product feed [{ts}]")
                origin = repo.remote("origin")
                origin.push()
                logger.info("Pushed updated pages to %s", settings.github_pages_branch)
            else:
                logger.info("No changes to publish")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _group_by_month(records: list[ArticleRecord]) -> list[tuple[str, list[ArticleRecord]]]:
    """Return [(month_label, [records]), ...] sorted newest-first."""
    groups: dict[str, list[ArticleRecord]] = {}
    for r in records:
        date_str = r.published_date or r.last_scraped_at or ""
        try:
            dt = datetime.fromisoformat(date_str[:10])
            label = dt.strftime("%B %Y")
        except (ValueError, TypeError):
            label = "Unknown"
        groups.setdefault(label, []).append(r)

    def _month_sort(item: tuple[str, list[ArticleRecord]]) -> str:
        label = item[0]
        try:
            return datetime.strptime(label, "%B %Y").strftime("%Y-%m")
        except ValueError:
            return ""

    return sorted(groups.items(), key=_month_sort, reverse=True)
