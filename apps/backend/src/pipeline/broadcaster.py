"""Broadcast worker: sends one message body to N WhatsApp group JIDs through
a single connected number. Each send is throttled with a random pause inside
[min_delay_seconds, max_delay_seconds] — keeps the cadence human-shaped and
avoids the burst signature that triggers WhatsApp's anti-spam ML."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from sqlalchemy import select, update

from src.clients import wa_sidecar_client
from src.db.models import BroadcastJob, OutboundMessage, WhatsAppNumber
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)


async def run_broadcast(job_id: int) -> None:
    """Iterate every queued OutboundMessage row for this job, calling the
    sidecar to send each. Updates per-message rows + rolling counts on the
    parent job row. Idempotent on retry: only `status='queued'` rows are
    processed, so a crash mid-run doesn't double-send."""
    async with SessionLocal() as s:
        job = await s.get(BroadcastJob, job_id)
        if job is None:
            logger.error("broadcast job %s not found", job_id)
            return
        number = await s.get(WhatsAppNumber, job.number_id)
        if number is None:
            logger.error("broadcast %s: number %s missing", job_id, job.number_id)
            await s.execute(
                update(BroadcastJob)
                .where(BroadcastJob.id == job_id)
                .values(status="failed", error="number missing", completed_at=datetime.now(timezone.utc))
            )
            await s.commit()
            return
        if number.status != "connected":
            logger.warning("broadcast %s: number %s not connected (%s)", job_id, number.id, number.status)
            await s.execute(
                update(BroadcastJob)
                .where(BroadcastJob.id == job_id)
                .values(
                    status="failed",
                    error=f"number not connected ({number.status})",
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await s.commit()
            return

        # Mark running.
        await s.execute(
            update(BroadcastJob)
            .where(BroadcastJob.id == job_id)
            .values(status="running", started_at=datetime.now(timezone.utc))
        )
        await s.commit()

        targets = list(
            (
                await s.execute(
                    select(OutboundMessage)
                    .where(
                        OutboundMessage.job_id == job_id,
                        OutboundMessage.status == "queued",
                    )
                    .order_by(OutboundMessage.id)
                )
            ).scalars()
        )

        body = job.body
        session_id = number.session_id
        min_d = max(0, job.min_delay_seconds)
        max_d = max(min_d, job.max_delay_seconds)

    sent = 0
    failed = 0

    for i, msg in enumerate(targets):
        # Throttle BEFORE every send except the first — feels human-paced.
        if i > 0:
            delay = random.uniform(min_d, max_d) if max_d > 0 else 0
            await asyncio.sleep(delay)

        attempted_at = datetime.now(timezone.utc)
        try:
            await wa_sidecar_client.send_message(session_id, msg.jid, body)
            status = "sent"
            error = None
            sent += 1
        except wa_sidecar_client.NotConnected as e:
            # Hard stop — re-queueing the rest is pointless until reconnect.
            logger.warning("broadcast %s: number went offline mid-run", job_id)
            async with SessionLocal() as s:
                await s.execute(
                    update(OutboundMessage)
                    .where(OutboundMessage.id == msg.id)
                    .values(status="failed", error=str(e), attempted_at=attempted_at)
                )
                await s.execute(
                    update(BroadcastJob)
                    .where(BroadcastJob.id == job_id)
                    .values(
                        status="failed",
                        error=f"number disconnected after {sent}/{len(targets)} sent",
                        sent_count=sent,
                        failed_count=failed + 1,
                        completed_at=datetime.now(timezone.utc),
                    )
                )
                await s.commit()
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("broadcast %s: send to %s failed", job_id, msg.jid)
            status = "failed"
            error = str(e)[:500]
            failed += 1

        async with SessionLocal() as s:
            await s.execute(
                update(OutboundMessage)
                .where(OutboundMessage.id == msg.id)
                .values(status=status, error=error, attempted_at=attempted_at)
            )
            await s.execute(
                update(BroadcastJob)
                .where(BroadcastJob.id == job_id)
                .values(sent_count=sent, failed_count=failed)
            )
            await s.commit()

    # Final mark.
    async with SessionLocal() as s:
        await s.execute(
            update(BroadcastJob)
            .where(BroadcastJob.id == job_id)
            .values(status="completed", completed_at=datetime.now(timezone.utc))
        )
        await s.commit()

    logger.info("broadcast %s done: sent=%s failed=%s", job_id, sent, failed)
