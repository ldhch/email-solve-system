"""Phase-1 CLI: DB init, polling runner, kill switch, offline simulation.

Usage (from backend/):
    python -m app.cli init-db
    python -m app.cli poll --once
    python -m app.cli run                 # poll every POLL_INTERVAL_SECONDS
    python -m app.cli status
    python -m app.cli pause --reason "..."
    python -m app.cli resume
    python -m app.cli simulate --risk low --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from app.config import get_settings
from app.core.exceptions import ConfigurationError, IMAPError
from app.core.logging import get_logger
from app.db.session import get_session_factory, init_db
from app.llm.client import MockLLMClient
from app.models.system_state import SystemState
from app.services.audit import log_action, utcnow
from app.services.ingest import IngestService, ParsedEmail

logger = get_logger(__name__)


def cmd_init_db(_args: argparse.Namespace) -> int:
    init_db()
    logger.info("Database initialized (create_all + seed).")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    settings = get_settings()
    init_db(settings)
    factory = get_session_factory(settings)
    with factory() as db:
        service = IngestService(db, settings)
        summary = service.fetch_and_process()
        logger.info("Poll summary: %s", summary)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    init_db(settings)
    interval = settings.poll_interval_seconds
    logger.info("Polling loop started (every %ss). Ctrl+C to stop.", interval)
    while True:
        try:
            factory = get_session_factory(settings)
            with factory() as db:
                service = IngestService(db, settings)
                summary = service.fetch_and_process()
                logger.info("Poll summary: %s", summary)
        except (IMAPError, ConfigurationError) as exc:
            logger.error("Poll cycle failed: %s", exc)
        time.sleep(interval)


def _state(db) -> SystemState:
    state = db.get(SystemState, 1)
    if state is None:
        raise RuntimeError("system_state missing; run `python -m app.cli init-db` first")
    return state


def cmd_status(_args: argparse.Namespace) -> int:
    settings = get_settings()
    init_db(settings)
    factory = get_session_factory(settings)
    with factory() as db:
        state = _state(db)
        print(f"ai_paused={state.ai_paused}")
        print(f"paused_at={state.paused_at}")
        print(f"paused_reason={state.paused_reason}")
        print(f"resumed_at={state.resumed_at}")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    settings = get_settings()
    init_db(settings)
    factory = get_session_factory(settings)
    with factory() as db:
        state = _state(db)
        state.ai_paused = True
        state.paused_at = utcnow()
        state.paused_reason = args.reason
        state.resumed_at = None
        log_action(db, "pause", "system", state.id, ip="cli")
        db.commit()
        logger.info("AI auto-reply PAUSED. Reason: %s", args.reason or "(none)")
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    settings = get_settings()
    init_db(settings)
    factory = get_session_factory(settings)
    with factory() as db:
        state = _state(db)
        state.ai_paused = False
        state.resumed_at = utcnow()
        log_action(db, "resume", "system", state.id, ip="cli")
        db.commit()
        logger.info("AI auto-reply RESUMED.")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """Offline demo: inject a synthetic email and run the Phase-1 pipeline.

    Uses the mock LLM provider so no API key is needed; `--dry-run` skips real
    SMTP and just logs what would be sent.
    """

    settings = get_settings()
    init_db(settings)
    factory = get_session_factory(settings)
    with factory() as db:
        from app.services.mailer import MailerService

        llm = MockLLMClient(settings)
        service = IngestService(db, settings, llm_client=llm)
        if args.dry_run:
            service.mailer = _DryRunMailer()

        risk = args.risk
        if risk == "high":
            body = "This product is defective and I will file a chargeback with my bank if you don't fix it."
        elif risk == "medium":
            body = "I want a refund for the item I ordered last week."
        else:
            body = "Hi, what is the size of the XL t-shirt in centimeters? Thanks!"

        parsed = ParsedEmail(
            message_id=f"sim-{args.risk}-{int(time.time())}@local",
            subject=f"Question about my order ({risk})",
            from_email=args.from_email,
            from_name="Sim Customer",
            to_email=settings.email_username,
            body_text=body,
            body_html=None,
            received_at=utcnow(),
        )
        result = service.process_one(parsed)
        print(f"result: action={result.action} risk={result.risk_level} "
              f"category={result.category} email_id={result.email_id} "
              f"conversation_id={result.conversation_id} reply_id={result.reply_id}")
        if result.error:
            print(f"error: {result.error}")
    return 0


class _DryRunMailer:
    """Log-only mailer for offline simulation."""

    def send(self, reply, to_email: str, subject: str) -> None:
        print(f"[dry-run] would send to {to_email}: subject={subject!r}")
        print(f"[dry-run] body: {reply.content_en}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="shouhou-agent Phase 1 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create tables + seed").set_defaults(func=cmd_init_db)

    poll = sub.add_parser("poll", help="run one IMAP poll cycle")
    poll.add_argument("--once", action="store_true", help="single pass (default)")
    poll.set_defaults(func=cmd_poll)

    sub.add_parser("run", help="run the 90s polling loop").set_defaults(func=cmd_run)
    sub.add_parser("status", help="show pause state").set_defaults(func=cmd_status)

    pause = sub.add_parser("pause", help="emergency pause AI auto-reply")
    pause.add_argument("--reason", default="", help="pause reason")
    pause.set_defaults(func=cmd_pause)

    sub.add_parser("resume", help="resume AI auto-reply").set_defaults(func=cmd_resume)

    simulate = sub.add_parser("simulate", help="offline demo email (mock LLM)")
    simulate.add_argument("--risk", choices=["low", "medium", "high"], default="low")
    simulate.add_argument("--from-email", default="customer@example.com")
    simulate.add_argument("--dry-run", action="store_true", help="do not actually send SMTP")
    simulate.set_defaults(func=cmd_simulate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
