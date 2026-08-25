"""APScheduler background jobs (M-12, TECH 3.1 / N-4).

Five jobs:
1. fetch_mail            every POLL_INTERVAL_SECONDS (90s) - IngestService
2. auto_close_sessions   hourly - close stale conversations
3. sla_overdue_scan      every 30 min - alert on overdue high-risk tickets
4. retention_timeout_scan every 30 min - alert + auto-release stale
   compensation drafts
5. heartbeat             every 30s - freshness source for /api/v1/healthz

APScheduler here is a *timer*, not a task queue (red line): the mail job still
processes emails synchronously, one at a time, inside ``fetch_and_process``.
Each job opens its own DB session from the session factory.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import Settings
from app.db.session import get_session_factory
from app.llm.client import build_llm_client
from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.models.system_state import SystemState
from app.models.ticket import Ticket
from app.services.alerting import AlertingService
from app.services.acknowledgment import resolve_review_tickets
from app.services.audit import log_action, utcnow
from app.services.ingest import IngestService
from app.services.mailer import MailerService
from app.services.replier import ReplierService
from app.services.translator import TranslatorService

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 60  # TECH N-4: heartbeat older than 60s => unhealthy
SCAN_INTERVAL_MINUTES = 30
RETENTION_TIMEOUT_HOURS = 24
REVIEW_TIMEOUT_HOURS = 24

# Process-local dedupe so the boss is not spammed every scan cycle. Restarting
# the process clears these sets; an audit row is written on first alert.
_alerted_sla_ticket_ids: set[int] = set()
_alerted_retention_reply_ids: set[int] = set()
_alerted_review_reply_ids: set[int] = set()

_scheduler_service: "SchedulerService | None" = None


class SchedulerService:
    """Owns the BackgroundScheduler instance and the heartbeat state."""

    def __init__(
        self,
        settings: Settings,
        session_factory=None,
        smtp_class: type | None = None,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._smtp_class = smtp_class
        self._scheduler = BackgroundScheduler(daemon=True)
        self._last_heartbeat = time.time()
        self._running = False

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._running:
            return
        settings = self.settings
        jobs = [
            (
                "fetch_mail",
                self._job_fetch_mail,
                {"seconds": max(30, settings.poll_interval_seconds)},
            ),
            (
                "prefill_translations",
                self._job_prefill_translations,
                {"seconds": max(30, settings.poll_interval_seconds)},
            ),
            ("auto_close_sessions", self._job_auto_close_sessions, {"hours": 1}),
            (
                "sla_overdue_scan",
                self._job_sla_overdue_scan,
                {"minutes": SCAN_INTERVAL_MINUTES},
            ),
            (
                "retention_timeout_scan",
                self._job_retention_timeout_scan,
                {"minutes": SCAN_INTERVAL_MINUTES},
            ),
            (
                "review_timeout_scan",
                self._job_review_timeout_scan,
                {"minutes": SCAN_INTERVAL_MINUTES},
            ),
            (
                "heartbeat",
                self._job_heartbeat,
                {"seconds": HEARTBEAT_INTERVAL_SECONDS},
            ),
        ]
        for job_id, fn, trigger in jobs:
            self._scheduler.add_job(
                fn,
                "interval",
                id=job_id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
                **trigger,
            )
        self._scheduler.start()
        self._running = True
        logger.info("APScheduler started with %s jobs", len(jobs))

    def shutdown(self) -> None:
        if self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            logger.info("APScheduler stopped")

    @property
    def running(self) -> bool:
        return self._running and self._scheduler.running

    def last_heartbeat_ts(self) -> float:
        return self._last_heartbeat

    def is_healthy(self) -> bool:
        """Scheduler is healthy while running with a fresh heartbeat (N-4)."""

        return self.running and (
            time.time() - self._last_heartbeat <= HEARTBEAT_STALE_SECONDS
        )

    # ---------- helpers ----------

    def _factory(self):
        return self._session_factory or get_session_factory(self.settings)

    def _job_heartbeat(self) -> None:
        self._last_heartbeat = time.time()

    # ---------- job 1: fetch mail ----------

    def _job_fetch_mail(self) -> None:
        factory = self._factory()
        try:
            with factory() as db:
                service = IngestService(db, self.settings, session_factory=factory)
                summary = service.fetch_and_process()
                logger.info("Scheduled poll summary: %s", summary)
        except Exception as exc:  # noqa: BLE001 - the timer must survive
            logger.exception("Scheduled poll failed: %s", exc)

    # ---------- job 1b: full-text translation prefill ----------

    def _job_prefill_translations(self) -> None:
        """Translate inbound emails that lack a cached Chinese full text.

        Runs on the same cadence as the mail poll. Each round takes up to
        ``translation_prefill_batch_size`` emails (oldest first) so a burst of
        unread mail is backfilled over a few rounds; when nothing is pending
        the query is a fast empty scan. A single failure must not stall the
        rest of the batch, and never kills the timer.

        The LLM calls run concurrently (each blocks 16-125s on the reasoning
        model, and the OpenAI client is thread-safe), but results are persisted
        serially by this thread so SQLite never sees concurrent writers.
        Workers only call the translator and never touch a DB session.
        """

        factory = self._factory()
        with factory() as db:
            q = (
                select(Email)
                .where(
                    Email.is_inbound.is_(True),
                    Email.content_cn.is_(None),
                    Email.body_text.isnot(None),
                )
                .order_by(Email.received_at.asc())
            )
            state = db.get(SystemState, 1)
            if state is not None and state.test_mode:
                # Test mode: prefill only whitelisted senders, so no LLM tokens
                # are spent on backlog mail the boss is deliberately ignoring.
                whitelist = {
                    w.strip().lower()
                    for w in (state.test_whitelist or "").split(",")
                    if w.strip()
                }
                if not whitelist:
                    return  # test mode with no sender whitelisted: nothing to prefill
                q = q.where(Email.from_email.in_(whitelist))
            pending = db.execute(
                q.limit(self.settings.translation_prefill_batch_size)
            ).scalars().all()
        jobs: list[tuple[int, str]] = [
            (email.id, (email.body_text or "").strip())
            for email in pending
            if (email.body_text or "").strip()
        ]
        if not jobs:
            return
        translator = TranslatorService(build_llm_client(self.settings))
        workers = min(self.settings.translation_prefill_concurrency, len(jobs))
        results: dict[int, str] = {}
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="prefill"
        ) as pool:
            futures = {
                pool.submit(translator.translate_to_chinese, body): email_id
                for email_id, body in jobs
            }
            for fut in as_completed(futures):
                email_id = futures[fut]
                try:
                    results[email_id] = fut.result()
                except Exception as exc:  # noqa: BLE001 - one mail must not block the batch
                    logger.warning(
                        "Prefill translation failed for email=%s: %s", email_id, exc
                    )
        translated = 0
        for email_id, content_cn in results.items():
            with factory() as db:
                email = db.get(Email, email_id)
                if email is None:
                    continue
                try:
                    email.content_cn = content_cn
                    db.commit()
                    translated += 1
                except Exception as exc:  # noqa: BLE001 - one mail must not block the batch
                    db.rollback()
                    logger.warning(
                        "Prefill persist failed for email=%s: %s", email_id, exc
                    )
        if translated:
            logger.info("Prefilled %s translation(s)", translated)

    # ---------- job 2: auto-close stale sessions ----------

    def _job_auto_close_sessions(self) -> None:
        cutoff = utcnow() - timedelta(days=self.settings.session_auto_close_days)
        factory = self._factory()
        with factory() as db:
            stale = db.execute(
                select(Conversation).where(
                    Conversation.last_activity_at < cutoff,
                    Conversation.status != "resolved",
                )
            ).scalars().all()
            for conv in stale:
                conv.status = "resolved"
                log_action(db, "auto_close", "conversation", conv.id, commit=False)
            db.commit()
            if stale:
                logger.info("Auto-closed %s stale conversation(s)", len(stale))

    # ---------- job 3: SLA overdue alert (PRD edge case 12) ----------

    def _job_sla_overdue_scan(self) -> None:
        now = utcnow()
        factory = self._factory()
        with factory() as db:
            overdue = db.execute(
                select(Ticket).where(
                    Ticket.status.in_(("pending", "in_progress")),
                    Ticket.sla_deadline < now,
                )
            ).scalars().all()
            alerting = AlertingService(self.settings)
            for ticket in overdue:
                if ticket.id in _alerted_sla_ticket_ids:
                    continue
                alerting.send_alert(
                    "SLA 工单逾期",
                    f"工单 #{ticket.id}（会话 #{ticket.conversation_id}）已超过 "
                    "24h 截止时间仍未处理，请尽快登录后台处理。",
                )
                log_action(db, "sla_overdue", "ticket", ticket.id, commit=False)
                _alerted_sla_ticket_ids.add(ticket.id)
            db.commit()

    # ---------- job 4: retention compensation review timeout ----------

    def _job_retention_timeout_scan(self) -> None:
        cutoff = utcnow() - timedelta(hours=RETENTION_TIMEOUT_HOURS)
        factory = self._factory()
        with factory() as db:
            state = db.get(SystemState, 1)
            if state is not None and state.ai_paused:
                return
            sender_whitelist = None
            if state is not None and state.test_mode:
                sender_whitelist = {
                    w.strip().lower()
                    for w in (state.test_whitelist or "").split(",")
                    if w.strip()
                }
                if not sender_whitelist:
                    return
            stale = db.execute(
                select(Reply).where(
                    Reply.reply_type == "retention_compensation",
                    Reply.status == "pending_review",
                    Reply.created_at < cutoff,
                )
            ).scalars().all()
            if not stale:
                return
            alerting = AlertingService(self.settings)
            mailer = MailerService(db, self.settings, smtp_class=self._smtp_class)
            replier = ReplierService(db, self.settings, build_llm_client(self.settings))
            for reply in stale:
                try:
                    email = db.get(Email, reply.email_id)
                    conversation = db.get(Conversation, reply.conversation_id)
                    if email is None or conversation is None:
                        continue
                    if (
                        sender_whitelist is not None
                        and email.from_email.lower() not in sender_whitelist
                    ):
                        continue
                    if reply.id not in _alerted_retention_reply_ids:
                        alerting.send_alert(
                            "补偿挽留待审核超时",
                            f"回复草稿 #{reply.id}（会话 #{reply.conversation_id}）"
                            f"已超过 {RETENTION_TIMEOUT_HOURS}h 未审核。如仍无人处理，"
                            "系统将按客户原退货请求自动放行。",
                        )
                        log_action(
                            db,
                            "retention_timeout_alert",
                            "reply",
                            reply.id,
                            commit=False,
                        )
                        db.commit()  # persist the alert audit even if the
                        # release below fails (audit must not be lost).
                        _alerted_retention_reply_ids.add(reply.id)

                    # Already auto-released after this draft? Never send twice.
                    already_released = db.execute(
                        select(Reply).where(
                            Reply.conversation_id == reply.conversation_id,
                            Reply.reply_type == "retention_release",
                            Reply.status == "sent",
                            Reply.created_at > reply.created_at,
                        )
                    ).scalars().first()
                    if already_released is not None:
                        continue

                    # Same release path as retention.py (generate + SMTP send).
                    release = replier.generate_and_send(
                        email_row=email,
                        conversation=conversation,
                        mailer=mailer,
                        reply_type="retention_release",
                        return_policy_text=self.settings.return_policy_text,
                    )
                    if release.status == "sent":
                        # The compensation draft is superseded so the boss can
                        # no longer approve it and double-send (edge case 22).
                        reply.status = "superseded"
                        resolve_review_tickets(db, conversation.id)
                    log_action(
                        db,
                        "retention_auto_released",
                        "reply",
                        release.id,
                        commit=False,
                    )
                    db.commit()  # one small transaction per draft
                    logger.info(
                        "Auto-released return for conversation=%s "
                        "(draft reply=%s, status=%s)",
                        conversation.id,
                        reply.id,
                        release.status,
                    )
                except Exception as exc:  # noqa: BLE001 - one draft must not
                    # block the rest of the batch.
                    db.rollback()
                    logger.exception(
                        "Retention timeout handling failed for reply=%s: %s",
                        reply.id,
                        exc,
                    )

    # ---------- job 5b: ordinary review-draft timeout alert ----------

    def _job_review_timeout_scan(self) -> None:
        """Alert when an ordinary pending_review draft sits >24h unaudited.

        Never auto-sends (only retention-compensation drafts auto-release); a
        stale review draft is escalated to the boss as a reminder to act. The
        retention compensation drafts are excluded — they have their own job
        with the auto-release path.
        """

        cutoff = utcnow() - timedelta(hours=REVIEW_TIMEOUT_HOURS)
        factory = self._factory()
        with factory() as db:
            state = db.get(SystemState, 1)
            if state is not None and state.ai_paused:
                return
            stale = (
                db.execute(
                    select(Reply).where(
                        Reply.status == "pending_review",
                        Reply.reply_type != "retention_compensation",
                        Reply.created_at < cutoff,
                    )
                )
                .scalars()
                .all()
            )
            if not stale:
                return
            alerting = AlertingService(self.settings)
            for reply in stale:
                if reply.id in _alerted_review_reply_ids:
                    continue
                alerting.send_alert(
                    "待审核草稿超时",
                    f"回复草稿 #{reply.id}（会话 #{reply.conversation_id}）已超过 "
                    f"{REVIEW_TIMEOUT_HOURS}h 未审核，请尽快登录后台处理。",
                )
                log_action(db, "review_overdue_alert", "reply", reply.id, commit=False)
                _alerted_review_reply_ids.add(reply.id)
            db.commit()


def set_scheduler_service(service: SchedulerService | None) -> None:
    """Register the process-wide scheduler instance (lifespan hook)."""

    global _scheduler_service
    _scheduler_service = service


def get_scheduler_service() -> SchedulerService | None:
    """Return the process-wide scheduler, or None when not started."""

    return _scheduler_service
