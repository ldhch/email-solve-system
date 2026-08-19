"""Backfill full Simplified-Chinese translations for historical data.

Translates `content_en` for replies and `body_text` for inbound emails that
lack a `content_cn`, then persists it. Idempotent: rows that already have a
translation are skipped, so re-running only fills remaining gaps.

Run from ``backend/`` with a configured LLM provider:
    python scripts/backfill_translations.py --dry-run
    python scripts/backfill_translations.py
"""

from __future__ import annotations

import argparse
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
from app.models.reply import Reply  # noqa: E402
from app.services.translator import TranslatorService  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_translations")


def _translate(llm, text: str) -> str | None:
    try:
        return TranslatorService(llm).translate_to_chinese(text)
    except LLMError as exc:
        logger.error("translation failed: %s", exc)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only report how many rows need translation, make no calls",
    )
    args = parser.parse_args()

    settings = get_settings()
    init_db(settings)  # ensures emails.content_cn exists on older DBs (idempotent)
    llm = build_llm_client(settings)
    factory = get_session_factory(settings)

    with factory() as db:
        replies = db.execute(
            select(Reply).where(
                Reply.content_en.isnot(None),
                Reply.content_en != "",
                or_(Reply.content_cn.is_(None), Reply.content_cn == ""),
            )
        ).scalars().all()
        emails = db.execute(
            select(Email).where(
                Email.body_text.isnot(None),
                Email.body_text != "",
                or_(Email.content_cn.is_(None), Email.content_cn == ""),
            )
        ).scalars().all()

        if args.dry_run:
            logger.info(
                "dry-run: %d replies and %d emails would be translated",
                len(replies),
                len(emails),
            )
            return 0

        ok_r = ok_e = failed = 0
        for r in replies:
            cn = _translate(llm, r.content_en)
            if cn is None:
                failed += 1
                continue
            r.content_cn = cn
            ok_r += 1
            db.flush()
        for e in emails:
            cn = _translate(llm, e.body_text)
            if cn is None:
                failed += 1
                continue
            e.content_cn = cn
            ok_e += 1
            db.flush()
        db.commit()
        logger.info(
            "backfill done: replies=%d emails=%d failed=%d",
            ok_r,
            ok_e,
            failed,
        )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
