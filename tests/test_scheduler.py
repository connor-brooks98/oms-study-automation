import logging

from oms_hub.scheduler import build_scheduler


def test_scheduler_has_one_guarded_outlook_job(caplog):
    calls = 0

    def sync_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")

    scheduler = build_scheduler("America/New_York", sync_once)
    job = scheduler.get_job("outlook-sync")

    assert job is not None
    assert len(scheduler.get_jobs()) == 1
    with caplog.at_level(logging.ERROR):
        job.func()
        job.func()
    assert calls == 2
    assert "Outlook synchronization failed" in caplog.text
