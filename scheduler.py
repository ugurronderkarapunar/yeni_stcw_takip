"""Gunluk arka plan isi. Kullanim: `python scheduler.py --once` (GitHub Actions / cron)."""
import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from agents import ComplianceAgent, NotificationAgent
from config import get_int, get_secret
from database import init_db, session_scope

log = logging.getLogger("stcw.scheduler")
logging.basicConfig(level=logging.INFO)

_scheduler = None


def run_daily_job() -> dict:
    """1) durumlari guncelle  2) yalnizca yeni kategoriye giren belgeler icin bildirim gonder."""
    with session_scope() as s:
        changed = ComplianceAgent().refresh_statuses(s)
    with session_scope() as s:
        sent = NotificationAgent().run(s)
    log.info("Gunluk is: %s durum degisti, %s bildirim islendi", changed, len(sent))
    return {"status_changed": changed, "notifications": sent}


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone=get_secret("SCHEDULER_TZ", "Europe/Istanbul"))
    _scheduler.add_job(run_daily_job, CronTrigger(hour=get_int("SCHEDULER_HOUR", 9), minute=0),
                       id="daily_stcw_check", replace_existing=True, coalesce=True,
                       max_instances=1, misfire_grace_time=3600)
    _scheduler.start()
    return _scheduler


if __name__ == "__main__":
    init_db()
    if "--once" in sys.argv:
        print(run_daily_job())
    else:
        import time
        start_scheduler()
        while True:
            time.sleep(3600)
