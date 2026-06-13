import logging
import subprocess

import httpx

from digest.config import settings

logger = logging.getLogger(__name__)


def post_discord_summary(stats: dict[str, dict[str, int]]) -> None:
    if not settings.discord_notify:
        return
    lines = ["**Daily digest complete**"]
    total_found = 0
    total_processed = 0
    for company, counts in stats.items():
        found = counts["found"]
        processed = counts["processed"]
        total_found += found
        total_processed += processed
        if found == 0:
            lines.append(f"• {company}: 0 new articles")
        elif found == processed:
            lines.append(f"• {company}: {found} new article{'s' if found != 1 else ''}")
        else:
            failed = found - processed
            lines.append(f"• {company}: {found} found, {processed} processed ({failed} failed)")
    lines.append(f"**Total: {total_found} found, {total_processed} processed**")
    message = "\n".join(lines)
    if settings.discord_notify_method == "webhook":
        _post_via_webhook(message)
    elif settings.discord_notify_method == "hermes":
        _post_via_hermes(message)
    else:
        logger.warning("Unknown discord_notify_method %r — skipping", settings.discord_notify_method)


def _post_via_webhook(message: str) -> None:
    if not settings.discord_webhook_url:
        logger.warning("discord_notify_method=webhook but DISCORD_WEBHOOK_URL not set — skipping")
        return
    try:
        resp = httpx.post(settings.discord_webhook_url, json={"content": message}, timeout=10.0)
        resp.raise_for_status()
        logger.info("Discord notification sent via webhook")
    except Exception as exc:
        logger.warning("Discord webhook notification failed: %s", exc)


def _post_via_hermes(message: str) -> None:
    if not settings.discord_hermes_channel:
        logger.warning("discord_notify_method=hermes but DISCORD_HERMES_CHANNEL not set — skipping")
        return
    try:
        result = subprocess.run(
            ["sudo", "-n", "-u", "hermes", settings.discord_hermes_bin, "send", "--to", settings.discord_hermes_channel, message],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning("hermes send failed (rc=%d): %s", result.returncode, result.stderr)
        else:
            logger.info("Discord notification sent via hermes")
    except Exception as exc:
        logger.warning("Discord hermes notification failed: %s", exc)
