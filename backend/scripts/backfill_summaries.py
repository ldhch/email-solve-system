"""Backfill missing Chinese summaries (`emails.summary_cn`) for historical data.

Old inbound emails ingested before the summarisation feature are stored with a
NULL `summary_cn`, which made the frontend fall back to the raw English body.
This script generates the short Chinese summary for every email that lacks one
and persists it. Idempotent: emails that already have a summary are skipped, so
re-running only fills remaining gaps.

Run from ``backend/`` with a configured LLM provider:
    python scripts/backfill_summaries.py --dry-run
    python scripts/backfill_summaries.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.exceptions import LLMError  # noqa: E402
from app.db.session import get_session_factory, init_db  # noqa: E402
from app.llm.client import build_llm_client  # noqa: E402
from app.models.email import Email  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_summaries")

# Mirrors the summary contract of the triage classifier (app/services/classifier.py):
# a short Chinese sentence, 20-40 chars, describing what the customer wants.
SUMMARIZE_SYSTEM_PROMPT = """\
You are an after-sales support analyst. Read the customer email and return ONE
strict JSON object with exactly this key:
{"summary_cn": "<short Chinese summary, 20-40 chars, of what the customer wants>"}

Rules:
- Summarise the customer's latest intent, not the quoted history.
- Always Chinese, concise, one sentence. No preamble, no other keys.
"""


def _summarise(llm, email: Email) -> str | None:
    """Return a Chinese summary for one email, or None on LLM failure."""

    body = (email.body_text or "").strip()
    user_content = (
        f"Subject: {email.subject}\n"
        f"From: {email.from_email}\n"
        f"Body:\n{body}"
    )
    try:
        raw = llm.chat_with_retry(
            messages=[{"role": "user", "content": user_content}],
            system_prompt=SUMMARIZE_SYSTEM_PROMPT,
        )
    except LLMError as exc:
        logger.error("summary LLM call failed for email %s: %s", email.id, exc)
        return None

    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.error("summary returned no JSON for email %s: %r", email.id, raw[:200])
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        logger.error("summary returned invalid JSON for email %s", email.id)
        return None
    summary = str(data.get("summary_cn", "")).strip()
    if not summary:
        logger.error("summary returned empty summary_cn for email %s", email.id)
        return None
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only report how many emails need a summary, make no LLM calls",
    )
    args = parser.parse_args()

    settings = get_settings()
    init_db(settings)
    llm = build_llm_client(settings)
    factory = get_session_factory(settings)

    with factory() as db:
        emails = db.execute(
            select(Email).where(
                Email.body_text.isnot(None),
                Email.body_text != "",
                or_(Email.summary_cn.is_(None), Email.summary_cn == ""),
            )
        ).scalars().all()

        if args.dry_run:
            logger.info("dry-run: %d emails would get a summary", len(emails))
            for e in emails:
                logger.info("  email %d | %s", e.id, e.subject[:60])
            return 0

        ok = failed = 0
        for e in emails:
            summary = _summarise(llm, e)
            if summary is None:
                failed += 1
                continue
            e.summary_cn = summary
            ok += 1
            db.flush()
        db.commit()
        logger.info("backfill done: emails=%d failed=%d", ok, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
