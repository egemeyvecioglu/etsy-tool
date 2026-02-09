"""Background job runner that processes queued Etsy price-adjustment jobs."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.admin_bypass import is_admin_bypass_user, mock_inventory_payload
from app.config import get_settings
from app.etsy_client import EtsyAPIError, EtsyClient
from app.models import AuditLog, Job, JobItem, JobItemStatus, JobStatus, User
from app.pricing import apply_adjustment_to_inventory
from app.schemas import PriceAdjustJobRequest
from app.services import get_or_refresh_access_token


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=timezone.utc)


def summarize_inventory(inventory_payload: dict[str, Any]) -> dict[str, Any]:
    """Create a lightweight summary of listing inventory before update."""

    total_products = 0
    total_offerings = 0

    for product in inventory_payload.get("products", []):
        total_products += 1
        total_offerings += len(product.get("offerings", []))

    return {
        "total_products": total_products,
        "total_offerings": total_offerings,
    }


class JobWorker:
    """Single-process background worker for queued jobs.

    The worker polls the database and executes one queued job at a time,
    recording per-listing outcomes to `JobItem` rows.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Create worker with DB session factory and runtime controls."""

        self._settings = get_settings()
        self._session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="etsy-job-worker")
        self._last_request_by_user: dict[int, float] = {}

    def start(self) -> None:
        """Start worker thread if enabled by configuration."""

        if not self._settings.enable_worker:
            return
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        """Signal worker shutdown and wait briefly for thread exit."""

        if not self._settings.enable_worker:
            return
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        """Main polling loop for discovering and processing queued jobs."""

        while not self._stop_event.is_set():
            processed = self._process_next_job_if_available()
            if processed:
                continue
            time.sleep(self._settings.job_poll_interval_seconds)

    def _process_next_job_if_available(self) -> bool:
        """Fetch and process the oldest queued job if one exists."""

        with self._session_factory() as db:
            job = db.scalar(
                select(Job)
                .where(Job.status == JobStatus.QUEUED.value)
                .order_by(Job.created_at.asc())
                .limit(1)
            )

            if job is None:
                return False

            job.status = JobStatus.RUNNING.value
            job.started_at = utcnow()
            db.add(job)
            db.commit()

            self._execute_job(db, job.id)
            return True

    def _respect_user_rate_limit(self, user_id: int) -> None:
        """Throttle outbound Etsy calls to avoid bursty request patterns."""

        min_interval = self._settings.per_user_min_interval_seconds
        last_request_at = self._last_request_by_user.get(user_id)
        now_ts = time.time()

        if last_request_at is not None:
            delta = now_ts - last_request_at
            if delta < min_interval:
                time.sleep(min_interval - delta)

        self._last_request_by_user[user_id] = time.time()

    def _execute_job(self, db: Session, job_id: int) -> None:
        """Run full job lifecycle for one queued job."""

        job = db.get(Job, job_id)
        if job is None:
            return

        user = db.get(User, job.user_id)
        if user is None:
            job.status = JobStatus.FAILED.value
            job.finished_at = utcnow()
            db.commit()
            return

        client = EtsyClient()

        try:
            request_model = PriceAdjustJobRequest.model_validate(job.request_payload)
            items = db.scalars(
                select(JobItem)
                .where(JobItem.job_id == job.id)
                .order_by(JobItem.id.asc())
            ).all()

            if is_admin_bypass_user(user):
                self._execute_job_items_with_mock_data(db, job, request_model, items)
                self._finalize_job(db, job)
                return

            for item in items:
                # Reload current job state each iteration to honor cancellation requests.
                db.refresh(job)
                if job.cancel_requested:
                    if item.status == JobItemStatus.PENDING.value:
                        item.status = JobItemStatus.SKIPPED.value
                        item.error = "Skipped because job was cancelled"
                        item.updated_at = utcnow()
                        db.add(item)
                        db.commit()
                    continue

                if item.status != JobItemStatus.PENDING.value:
                    continue

                try:
                    self._respect_user_rate_limit(user.id)
                    access_token = get_or_refresh_access_token(db, user, client)
                    inventory = client.get_listing_inventory(access_token, item.listing_id)

                    before_summary = summarize_inventory(inventory)
                    updated_inventory, after_summary = apply_adjustment_to_inventory(
                        inventory,
                        request_model.adjustment,
                        request_model.options,
                    )

                    item.before_summary = before_summary
                    item.after_summary = after_summary

                    if after_summary.get("changed_offerings", 0) == 0:
                        item.status = JobItemStatus.SKIPPED.value
                        item.error = "No price changes required"
                    elif request_model.options.dry_run:
                        item.status = JobItemStatus.SUCCEEDED.value
                        item.error = None
                    else:
                        self._respect_user_rate_limit(user.id)
                        client.update_listing_inventory(access_token, item.listing_id, updated_inventory)
                        item.status = JobItemStatus.SUCCEEDED.value
                        item.error = None

                    item.updated_at = utcnow()
                    db.add(item)
                    db.commit()

                except (EtsyAPIError, ValueError, RuntimeError) as exc:
                    item.status = JobItemStatus.FAILED.value
                    item.error = str(exc)
                    item.updated_at = utcnow()
                    db.add(item)
                    db.commit()

            self._finalize_job(db, job)

        finally:
            client.close()

    def _execute_job_items_with_mock_data(
        self,
        db: Session,
        job: Job,
        request_model: PriceAdjustJobRequest,
        items: list[JobItem],
    ) -> None:
        """Process job items using deterministic mock inventory payloads."""

        for item in items:
            db.refresh(job)
            if job.cancel_requested:
                if item.status == JobItemStatus.PENDING.value:
                    item.status = JobItemStatus.SKIPPED.value
                    item.error = "Skipped because job was cancelled"
                    item.updated_at = utcnow()
                    db.add(item)
                    db.commit()
                continue

            if item.status != JobItemStatus.PENDING.value:
                continue

            try:
                inventory = mock_inventory_payload(item.listing_id)
                before_summary = summarize_inventory(inventory)
                _updated_inventory, after_summary = apply_adjustment_to_inventory(
                    inventory,
                    request_model.adjustment,
                    request_model.options,
                )

                item.before_summary = before_summary
                item.after_summary = after_summary

                if after_summary.get("changed_offerings", 0) == 0:
                    item.status = JobItemStatus.SKIPPED.value
                    item.error = "No price changes required"
                else:
                    item.status = JobItemStatus.SUCCEEDED.value
                    item.error = None

                item.updated_at = utcnow()
                db.add(item)
                db.commit()

            except ValueError as exc:
                item.status = JobItemStatus.FAILED.value
                item.error = str(exc)
                item.updated_at = utcnow()
                db.add(item)
                db.commit()

    def _finalize_job(self, db: Session, job: Job) -> None:
        """Derive terminal job state from item statuses and cancellation flag."""

        items = db.scalars(select(JobItem).where(JobItem.job_id == job.id)).all()

        failed = sum(1 for item in items if item.status == JobItemStatus.FAILED.value)
        succeeded = sum(1 for item in items if item.status == JobItemStatus.SUCCEEDED.value)
        skipped = sum(1 for item in items if item.status == JobItemStatus.SKIPPED.value)

        if job.cancel_requested:
            job.status = JobStatus.CANCELLED.value
        elif failed > 0:
            job.status = JobStatus.FAILED.value
        else:
            job.status = JobStatus.SUCCEEDED.value

        job.summary_payload = {
            "total_items": len(items),
            "succeeded_items": succeeded,
            "failed_items": failed,
            "skipped_items": skipped,
        }
        job.finished_at = utcnow()

        db.add(job)
        db.add(
            AuditLog(
                user_id=job.user_id,
                job_id=job.id,
                action="JOB_FINISHED",
                payload={
                    "status": job.status,
                    "summary": job.summary_payload,
                },
            )
        )
        db.commit()
