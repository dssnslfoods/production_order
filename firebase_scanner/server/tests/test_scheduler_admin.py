"""Unit tests for scheduler_admin.py — _job_name, _iso are pure functions."""
import datetime
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch

with patch("google.auth.default", return_value=(MagicMock(), "test-project")):
    import scheduler_admin


class TestJobName:
    def test_format(self, monkeypatch):
        monkeypatch.setattr(scheduler_admin, "PROJECT", "my-project")
        monkeypatch.setattr(scheduler_admin, "LOCATION", "asia-southeast1")
        monkeypatch.setattr(scheduler_admin, "JOB_ID", "scan-queue-3h")
        assert scheduler_admin._job_name() == "projects/my-project/locations/asia-southeast1/jobs/scan-queue-3h"

    def test_custom_values(self, monkeypatch):
        monkeypatch.setattr(scheduler_admin, "PROJECT", "test-proj")
        monkeypatch.setattr(scheduler_admin, "LOCATION", "us-central1")
        monkeypatch.setattr(scheduler_admin, "JOB_ID", "my-job")
        assert scheduler_admin._job_name() == "projects/test-proj/locations/us-central1/jobs/my-job"


class TestIso:
    def test_none(self):
        assert scheduler_admin._iso(None) is None

    def test_valid_datetime(self):
        dt = datetime.datetime(2026, 7, 1, 10, 30, 0)
        assert scheduler_admin._iso(dt) == "2026-07-01T10:30:00"

    def test_old_date_returns_none(self):
        dt = datetime.datetime(1970, 1, 1)
        assert scheduler_admin._iso(dt) is None

    def test_1972_is_valid(self):
        dt = datetime.datetime(1972, 1, 1)
        assert scheduler_admin._iso(dt) == "1972-01-01T00:00:00"

    def test_non_datetime_returns_none(self):
        assert scheduler_admin._iso("not a date") is None

    def test_integer_returns_none(self):
        assert scheduler_admin._iso(12345) is None
