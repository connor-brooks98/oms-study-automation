import logging
from collections.abc import Callable

from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
    BackgroundScheduler,
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def build_scheduler(
    timezone: str,
    sync_once: Callable[[], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=timezone)

    def guarded_sync() -> None:
        try:
            sync_once()
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
    return scheduler
