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
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import Settings
from app.db.session import get_session_factory
from app.llm.client import build_llm_client
from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.models.ticket import Ticket
from app.services.alerting import AlertingService
from app.services.audit import log_action, utcnow
from app.services.ingest import IngestService
from app.services.mailer import MailerService
from app.services.replier import ReplierService

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 60  # TECH N-4: heartbeat older than 60s => unhealthy
SCAN_INTERVAL_MINUTES = 30
RETENTION_TIMEOUT_HOURS = 24

# Process-local dedupe so the boss is not spammed every scan cycle. Restarting
# the process clears these sets; an audit row is written on first alert.
_alerted_sla_ticket_ids: set[int] = set()
_alerted_retention_reply_ids: set[int] = set()

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
                    Ticket.is_deleted.is_(False),
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


def set_scheduler_service(service: SchedulerService | None) -> None:
    """Register the process-wide scheduler instance (lifespan hook)."""

    global _scheduler_service
    _scheduler_service = service


def get_scheduler_service() -> SchedulerService | None:
    """Return the process-wide scheduler, or None when not started."""

    return _scheduler_service
