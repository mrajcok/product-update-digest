import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from git import Repo
from jinja2 import Environment, FileSystemLoader

from storage.db import ArticleDB
from storage.models import ArticleRecord

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
COMPANIES = ["cribl", "ocient"]


def _sort_key(record: ArticleRecord) -> str:
    return record.published_date or record.last_scraped_at or ""


class GitHubPagesPublisher:
    def __init__(self, db: ArticleDB) -> None:
        self._db = db
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=True,
        )

    def publish(self) -> None:
        all_records = self._db.get_all()
        ok_records = [r for r in all_records if r.status == "ok" and r.summary]

        top_updates = sorted(ok_records, key=_sort_key, reverse=True)[:20]

        company_updates: dict[str, list[ArticleRecord]] = {
            c: sorted(
                [r for r in ok_records if r.company == c],
                key=_sort_key,
                reverse=True,
            )
            for c in COMPANIES
        }

        html_files = self._render(top_updates, company_updates)
        self._push_to_github(html_files)

    def _render(
        self,
        top_updates: list[ArticleRecord],
        company_updates: dict[str, list[ArticleRecord]],
    ) -> dict[str, str]:
        files: dict[str, str] = {}

        index_tmpl = self._env.get_template("index.html.j2")
        files["index.html"] = index_tmpl.render(
            updates=top_updates,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )

        company_tmpl = self._env.get_template("company_index.html.j2")
        for company, records in company_updates.items():
            grouped = _group_by_month(records)
            files[f"{company}/index.html"] = company_tmpl.render(
                company=company,
                grouped=grouped,
                generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            )

        return files

    def _push_to_github(self, html_files: dict[str, str]) -> None:
        from config import settings  # deferred to avoid module-level Settings() at import time

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
