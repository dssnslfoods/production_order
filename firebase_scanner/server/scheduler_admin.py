"""Read/update the Cloud Scheduler job that drives auto-scans, from the web UI."""
import os

import google.auth
from google.cloud import scheduler_v1

_, _DEFAULT_PROJECT = google.auth.default()
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or _DEFAULT_PROJECT
LOCATION = os.environ.get("SCHEDULER_LOCATION", "asia-southeast1")
JOB_ID = os.environ.get("SCHEDULER_JOB", "scan-queue-3h")


def _client():
    return scheduler_v1.CloudSchedulerClient()


def _job_name():
    return f"projects/{PROJECT}/locations/{LOCATION}/jobs/{JOB_ID}"


def _iso(dt):
    try:
        if dt and dt.year > 1971:
            return dt.isoformat()
    except Exception:  # noqa: BLE001
        pass
    return None


def get_schedule():
    job = _client().get_job(name=_job_name())
    return {
        "cron": job.schedule,
        "timezone": job.time_zone or "Asia/Bangkok",
        "state": job.state.name if hasattr(job.state, "name") else str(job.state),
        "next_run": _iso(getattr(job, "schedule_time", None)),
        "last_run": _iso(getattr(job, "last_attempt_time", None)),
    }


def update_schedule(cron, timezone="Asia/Bangkok"):
    client = _client()
    job = client.get_job(name=_job_name())
    job.schedule = cron
    job.time_zone = timezone
    client.update_job(job=job, update_mask={"paths": ["schedule", "time_zone"]})
    client.resume_job(name=_job_name())
    return get_schedule()


def pause_job():
    _client().pause_job(name=_job_name())
    return get_schedule()


def resume_job():
    _client().resume_job(name=_job_name())
    return get_schedule()
