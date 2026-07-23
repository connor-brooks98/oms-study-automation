import logging
from collections.abc import Callable

from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
    BackgroundScheduler,
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def build_scheduler(
    timezone: str,
    sync_once: Callable[[], None] | None,
    canvas_worker_once: Callable[[], object] | None = None,
    panopto_poll_once: Callable[[], object] | None = None,
    panopto_worker_once: Callable[[], object] | None = None,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=timezone)

    if sync_once is not None:
        configured_sync = sync_once

        def guarded_sync() -> None:
            try:
                configured_sync()
            except Exception:
                logger.exception("Outlook synchronization failed")

        scheduler.add_job(
            guarded_sync,
            CronTrigger(hour="5,17", minute=0, timezone=timezone),
            id="outlook-sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    if canvas_worker_once is not None:
        scheduler.add_job(
            canvas_worker_once,
            "interval",
            seconds=5,
            id="canvas-worker",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if panopto_poll_once is not None:
        configured_poll = panopto_poll_once

        def guarded_panopto_poll() -> None:
            try:
                configured_poll()
            except Exception:
                logger.exception("Panopto discovery failed")

        scheduler.add_job(
            guarded_panopto_poll,
            "interval",
            minutes=15,
            id="panopto-poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if panopto_worker_once is not None:
        scheduler.add_job(
            panopto_worker_once,
            "interval",
            seconds=5,
            id="panopto-worker",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return scheduler
