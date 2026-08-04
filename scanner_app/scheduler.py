"""Background scheduler that runs scan_once() on the configured interval."""
from apscheduler.schedulers.background import BackgroundScheduler

import config
import scanner

_scheduler = BackgroundScheduler(daemon=True)
_JOB_ID = "auto_scan"


def _job():
    scanner.scan_once(trigger="schedule")


def start():
    if not _scheduler.running:
        _scheduler.start()
    apply_config()


def apply_config():
    """(Re)configure the recurring job from the current config."""
    cfg = config.load_config()
    sched = cfg["schedule"]
    existing = _scheduler.get_job(_JOB_ID)
    if sched.get("enabled"):
        minutes = max(1, int(sched.get("interval_minutes", 30)))
        if existing:
            _scheduler.reschedule_job(_JOB_ID, trigger="interval", minutes=minutes)
        else:
            _scheduler.add_job(_job, "interval", minutes=minutes,
                               id=_JOB_ID, replace_existing=True,
                               max_instances=1, coalesce=True)
    elif existing:
        _scheduler.remove_job(_JOB_ID)


def status():
    job = _scheduler.get_job(_JOB_ID)
    nxt = getattr(job, "next_run_time", None) if job else None
    return {
        "enabled": job is not None,
        "next_run": nxt.isoformat(sep=" ", timespec="seconds") if nxt else None,
    }


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
