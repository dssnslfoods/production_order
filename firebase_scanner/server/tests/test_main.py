"""Unit tests for main.py — API endpoints and helper functions."""
import io
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient
from fastapi import HTTPException

# The submodules must be imported before they can be patched by name; without
# this the file only collects when another test module happens to import them
# first, and running it on its own fails.
import firebase_admin.firestore  # noqa: F401
import firebase_admin.storage    # noqa: F401

# Patch firebase_admin before importing main to avoid init errors
with patch.dict(os.environ, {"STORAGE_BUCKET": "test-bucket"}):
    with patch("firebase_admin.initialize_app"), \
         patch("firebase_admin.firestore"), \
         patch("firebase_admin.storage"), \
         patch("google.auth.default", return_value=(MagicMock(), "test-project")):
        import main
        from main import app, _api_key, _masked_config


def _mock_user(role="admin"):
    return {"uid": "test-uid", "email": "test@test.com", "role": role}


async def _fake_verify_token(authorization: str = ""):
    return _mock_user()


@pytest.fixture
def client():
    app.dependency_overrides[main.auth.verify_token] = _fake_verify_token
    app.dependency_overrides[main.admin_only] = _fake_verify_token
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# _api_key helper
# ---------------------------------------------------------------------------
class TestApiKey:
    def test_from_settings(self):
        with patch("main.store") as mock_store:
            mock_store.get_settings.return_value = {
                "api_keys": {"claude": "sk-from-settings"}
            }
            assert _api_key("claude") == "sk-from-settings"

    def test_from_env_fallback(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_API_KEY", "sk-from-env")
        with patch("main.store") as mock_store:
            mock_store.get_settings.return_value = {"api_keys": {"claude": ""}}
            assert _api_key("claude") == "sk-from-env"

    def test_empty_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        with patch("main.store") as mock_store:
            mock_store.get_settings.return_value = {"api_keys": {}}
            assert _api_key("claude") == ""


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
class TestHealth:
    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Config endpoint
# ---------------------------------------------------------------------------
class TestConfig:
    def test_get_config(self, client):
        fake_settings = {
            "provider": "claude",
            "models": {"claude": "m", "gemini": "g", "openai": "o"},
            "api_keys": {"claude": "sk-x", "gemini": "", "openai": ""},
            "drive_folder_id": "",
        }
        with patch("main.store") as mock_store:
            mock_store.get_settings.return_value = fake_settings
            mock_store.get_permissions.return_value = {"admin": ["dashboard"]}
            resp = client.get("/api/config", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        data = resp.json()
        assert "provider" in data
        assert "role" in data
        assert data["role"] == "admin"


# ---------------------------------------------------------------------------
# Orders endpoints
# ---------------------------------------------------------------------------
class TestOrders:
    def test_list_orders(self, client):
        with patch("main.store") as mock_store:
            mock_store.list_orders.return_value = (
                [{"id": "1", "order_no": "OD001", "lines": []}], None
            )
            resp = client.get("/api/orders", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        assert len(resp.json()["orders"]) == 1
        assert resp.json()["next_cursor"] is None

    def test_get_order_not_found(self, client):
        with patch("main.store") as mock_store:
            mock_store.get_order.return_value = None
            resp = client.get("/api/orders/nonexist", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 404

    def test_get_order_found(self, client):
        with patch("main.store") as mock_store:
            mock_store.get_order.return_value = {
                "id": "abc", "order_no": "OD001", "source_image": "scans/img.jpg"
            }
            resp = client.get("/api/orders/abc", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        assert resp.json()["has_image"] is True

    def test_delete_order(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user()
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"admin": ["delete"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001"}
            mock_store.delete_order = MagicMock()
            mock_store.log_activity = MagicMock()
            resp = client.delete("/api/orders/abc", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "abc"

    def test_update_order(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user()
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"admin": ["edit_order", "confirm_review"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "pending_review"}
            mock_store.update_order.return_value = {"id": "abc", "order_no": "OD002"}
            mock_store.log_activity = MagicMock()
            resp = client.put("/api/orders/abc",
                              json={"order_no": "OD002"},
                              headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200

    def test_update_order_blocked_after_approval(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user()
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"admin": ["edit_order"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "approved"}
            mock_store.log_activity = MagicMock()
            resp = client.put("/api/orders/abc",
                              json={"order_no": "OD002"},
                              headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400

    def test_approve_order(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user()
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"admin": ["approve"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "pending_approval"}
            mock_store.approve_order.return_value = {"id": "abc", "status": "approved"}
            mock_store.log_activity = MagicMock()
            resp = client.post("/api/orders/abc/approve",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200

    def test_approve_order_wrong_status_rejected(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user()
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"admin": ["approve"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "pending_review"}
            mock_store.log_activity = MagicMock()
            resp = client.post("/api/orders/abc/approve",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400

    def test_confirm_review(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user(role="reviewer")
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"reviewer": ["confirm_review"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "pending_review"}
            mock_store.confirm_review.return_value = {"id": "abc", "status": "pending_approval"}
            mock_store.log_activity = MagicMock()
            resp = client.post("/api/orders/abc/confirm-review",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200

    def test_confirm_review_allows_returned(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user(role="reviewer")
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"reviewer": ["confirm_review"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "returned_to_review"}
            mock_store.confirm_review.return_value = {"id": "abc", "status": "pending_approval"}
            mock_store.log_activity = MagicMock()
            resp = client.post("/api/orders/abc/confirm-review",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200

    def test_reviewer_forwards_draft_straight_to_approval(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user(role="reviewer")
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"reviewer": ["confirm_review"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "draft"}
            mock_store.confirm_review.return_value = {"id": "abc", "status": "pending_approval"}
            resp = client.post("/api/orders/abc/confirm-review",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        mock_store.confirm_review.assert_called_once()
        assert "จากฉบับร่าง" in mock_store.log_activity.call_args[0][3]

    def test_confirm_review_wrong_status_rejected(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user(role="reviewer")
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"reviewer": ["confirm_review"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "approved"}
            mock_store.log_activity = MagicMock()
            resp = client.post("/api/orders/abc/confirm-review",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400

    def test_confirm_review_requires_permission(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user(role="staff")
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"staff": []}
            resp = client.post("/api/orders/abc/confirm-review",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 403

    def test_return_to_review(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user(role="approver")
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"approver": ["return_to_review"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "pending_approval"}
            mock_store.return_order_to_review.return_value = {"id": "abc",
                                                              "status": "returned_to_review"}
            mock_store.log_activity = MagicMock()
            resp = client.post("/api/orders/abc/return-to-review",
                               json={"reason": "ยอดไม่ตรง"},
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200

    def test_return_to_review_requires_reason(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user(role="approver")
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"approver": ["return_to_review"]}
            resp = client.post("/api/orders/abc/return-to-review",
                               json={"reason": ""},
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400

    def test_return_to_review_wrong_status_rejected(self, client):
        async def fake_verify(auth_header=""):
            return _mock_user(role="approver")
        with patch("auth.verify_token", new=fake_verify), \
             patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"approver": ["return_to_review"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "pending_review"}
            resp = client.post("/api/orders/abc/return-to-review",
                               json={"reason": "ยอดไม่ตรง"},
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400

    def _as(self, role, factory_id=None):
        async def fake_verify(authorization: str = ""):
            return {"uid": "u", "email": f"{role}@test.com", "role": role,
                    "factory_id": factory_id}
        app.dependency_overrides[main.auth.verify_token] = fake_verify
        self._auth_patch = patch("auth.verify_token", new=fake_verify)
        self._auth_patch.start()

    def teardown_method(self):
        if getattr(self, "_auth_patch", None):
            self._auth_patch.stop()
            self._auth_patch = None

    def test_submit_review(self, client):
        self._as("staff")
        with patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"staff": ["edit_order"]}
            mock_store.get_order.return_value = {"id": "abc", "order_no": "OD001",
                                                 "status": "draft"}
            mock_store.submit_for_review.return_value = {"id": "abc", "status": "pending_review"}
            resp = client.post("/api/orders/abc/submit-review",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        assert mock_store.log_activity.call_args[0][0] == "submit_review"

    def test_submit_review_only_from_draft(self, client):
        self._as("staff")
        with patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"staff": ["edit_order"]}
            mock_store.get_order.return_value = {"id": "abc", "status": "returned_to_review"}
            resp = client.post("/api/orders/abc/submit-review",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400
        mock_store.submit_for_review.assert_not_called()

    def test_staff_edits_draft(self, client):
        self._as("staff")
        with patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"staff": ["edit_order"]}
            mock_store.get_order.return_value = {"id": "abc", "status": "draft"}
            resp = client.put("/api/orders/abc", json={"order_no": "X"},
                              headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200

    def test_staff_cannot_edit_once_in_review(self, client):
        self._as("staff")
        with patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"staff": ["edit_order"]}
            mock_store.get_order.return_value = {"id": "abc", "status": "pending_review"}
            resp = client.put("/api/orders/abc", json={"order_no": "X"},
                              headers={"Authorization": "Bearer test"})
        assert resp.status_code == 403
        mock_store.update_order.assert_not_called()

    def test_nobody_edits_pending_approval(self, client):
        self._as("reviewer")
        with patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"reviewer": ["edit_order", "confirm_review"]}
            mock_store.get_order.return_value = {"id": "abc", "status": "pending_approval"}
            resp = client.put("/api/orders/abc", json={"order_no": "X"},
                              headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400

    def test_other_factory_order_is_not_found(self, client):
        self._as("approver", factory_id="f1")
        with patch("main.store") as mock_store:
            mock_store.get_permissions.return_value = {"approver": ["approve"]}
            mock_store.get_order.return_value = {"id": "abc", "status": "pending_approval",
                                                 "factory_id": "f2"}
            resp = client.post("/api/orders/abc/approve",
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 404
        mock_store.approve_order.assert_not_called()


# ---------------------------------------------------------------------------
# Export endpoint
# ---------------------------------------------------------------------------
class TestExport:
    def test_export_xlsx(self, client):
        with patch("main.store") as mock_store, \
             patch("main.excel_export") as mock_excel:
            mock_store.list_orders.return_value = (
                [{"order_no": "OD001", "status": "approved", "lines": []}], None
            )
            mock_excel.build_workbook.return_value = b"fake-xlsx-data"
            resp = client.get("/api/export", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]

    def test_export_filters_by_status(self, client):
        with patch("main.store") as mock_store, \
             patch("main.excel_export") as mock_excel:
            mock_store.list_orders.return_value = (
                [{"order_no": "A", "status": "approved", "lines": []},
                 {"order_no": "B", "status": "pending_approval", "lines": []}], None
            )
            mock_excel.build_workbook.return_value = b"data"
            resp = client.get("/api/export?status=approved",
                              headers={"Authorization": "Bearer test"})
        call_args = mock_excel.build_workbook.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0]["order_no"] == "A"

    def test_export_date_filter(self, client):
        with patch("main.store") as mock_store, \
             patch("main.excel_export") as mock_excel:
            mock_store.list_orders.return_value = (
                [{"order_no": "A", "status": "approved",
                  "document_date": "2026-07-01", "lines": []},
                 {"order_no": "B", "status": "approved",
                  "document_date": "2026-06-15", "lines": []}], None
            )
            mock_excel.build_workbook.return_value = b"data"
            resp = client.get("/api/export?from_date=2026-07-01&status=approved",
                              headers={"Authorization": "Bearer test"})
        call_args = mock_excel.build_workbook.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0]["order_no"] == "A"


# ---------------------------------------------------------------------------
# Cron endpoint
# ---------------------------------------------------------------------------
class TestCron:
    def test_invalid_cron_key(self, client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "my-secret")
        resp = client.post("/api/cron/process", headers={"x-cron-key": "wrong"})
        assert resp.status_code == 403

    def test_missing_cron_secret(self, client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "")
        resp = client.post("/api/cron/process", headers={"x-cron-key": ""})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Pending endpoints
# ---------------------------------------------------------------------------
class TestPending:
    def test_list_pending(self, client):
        with patch("main.store") as mock_store:
            mock_store.list_pending.return_value = []
            resp = client.get("/api/pending", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        assert resp.json()["pending"] == []

    def test_delete_pending_not_found(self, client):
        with patch("main.store") as mock_store:
            mock_store.get_pending.return_value = None
            resp = client.delete("/api/pending/xyz",
                                 headers={"Authorization": "Bearer test"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# User management endpoints
# ---------------------------------------------------------------------------
class TestUsers:
    def test_list_users(self, client):
        with patch("main.store") as mock_store:
            mock_store.list_users.return_value = [
                {"uid": "u1", "email": "a@test.com", "role": "admin"}
            ]
            resp = client.get("/api/users", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        assert len(resp.json()["users"]) == 1

    def test_create_user_invalid_role(self, client):
        with patch("main.store"):
            resp = client.post("/api/users",
                               json={"email": "x@test.com", "password": "123456", "role": "god"},
                               headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400

    def test_delete_self_blocked(self, client):
        with patch("main.store") as mock_store:
            mock_store.get_user_doc.return_value = {"uid": "test-uid", "email": "test@test.com"}
            resp = client.delete("/api/users/test-uid",
                                 headers={"Authorization": "Bearer test"})
        assert resp.status_code == 400
        assert "ลบตัวเอง" in resp.json()["detail"]

    def test_update_user_not_found(self, client):
        with patch("main.store") as mock_store:
            mock_store.get_user_doc.return_value = None
            resp = client.put("/api/users/nonexist",
                              json={"role": "staff"},
                              headers={"Authorization": "Bearer test"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# _process_queue helper
# ---------------------------------------------------------------------------
class TestProcessQueue:
    def test_no_api_key_returns_error(self):
        with patch("main.store") as mock_store, \
             patch("main._api_key", return_value=""):
            mock_store.get_settings.return_value = {"provider": "claude", "models": {"claude": "m"}}
            result = main._process_queue()
        assert "error" in result
        assert result["processed"] == 0

    def test_empty_queue(self):
        with patch("main.store") as mock_store, \
             patch("main._api_key", return_value="sk-key"):
            mock_store.get_settings.return_value = {"provider": "claude", "models": {"claude": "m"}}
            mock_store.list_pending.return_value = []
            result = main._process_queue()
        assert result["processed"] == 0
        assert result["succeeded"] == 0

    def test_skips_completed(self):
        with patch("main.store") as mock_store, \
             patch("main._api_key", return_value="sk-key"):
            mock_store.get_settings.return_value = {"provider": "claude", "models": {"claude": "m"}}
            mock_store.list_pending.return_value = [
                {"id": "p1", "status": "processing", "filename": "x.jpg", "storage_path": "p/x"}
            ]
            result = main._process_queue()
        assert result["processed"] == 0

    def test_retries_failed_items(self):
        with patch("main.store") as mock_store, \
             patch("main._api_key", return_value="sk-key"), \
             patch("main.extractor") as mock_ext:
            mock_store.get_settings.return_value = {"provider": "claude", "models": {"claude": "m"}}
            mock_store.list_pending.return_value = [
                {"id": "p1", "status": "failed", "filename": "x.jpg",
                 "storage_path": "p/x", "retry_count": 1}
            ]
            mock_store.download_bytes.return_value = b"fake-img"
            mock_ext.images_from_upload.return_value = [("image/jpeg", b"img")]
            mock_ext.extract.return_value = {"order_no": "OD001", "lines": []}
            mock_store.find_by_order_no.return_value = None
            result = main._process_queue()
        assert result["processed"] == 1
        assert result["succeeded"] == 1


# ---------------------------------------------------------------------------
# Standardized error responses
# ---------------------------------------------------------------------------
class TestErrorFormat:
    def test_404_has_error_field(self, client):
        with patch("main.store") as mock_store:
            mock_store.get_order.return_value = None
            resp = client.get("/api/orders/nonexist",
                              headers={"Authorization": "Bearer test"})
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] is True
        assert body["code"] == 404
        assert "detail" in body

    def test_unhandled_returns_500(self):
        with TestClient(app, raise_server_exceptions=False) as c:
            app.dependency_overrides[main.auth.verify_token] = _fake_verify_token
            app.dependency_overrides[main.admin_only] = _fake_verify_token
            with patch("main.store") as mock_store:
                mock_store.list_orders.side_effect = RuntimeError("db down")
                resp = c.get("/api/orders",
                             headers={"Authorization": "Bearer test"})
            app.dependency_overrides.clear()
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] is True
        assert body["code"] == 500


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
class TestPagination:
    def test_orders_with_cursor(self, client):
        with patch("main.store") as mock_store:
            mock_store.list_orders.return_value = (
                [{"id": "2", "order_no": "OD002", "lines": []}], None
            )
            resp = client.get("/api/orders?cursor=abc123",
                              headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        mock_store.list_orders.assert_called_once_with(limit=100, cursor="abc123", factory_id=None)

    def test_orders_returns_next_cursor(self, client):
        with patch("main.store") as mock_store:
            mock_store.list_orders.return_value = (
                [{"id": "1"}, {"id": "2"}], "next-id"
            )
            resp = client.get("/api/orders?limit=2",
                              headers={"Authorization": "Bearer test"})
        assert resp.json()["next_cursor"] == "next-id"


# ---------------------------------------------------------------------------
# Image optimization
# ---------------------------------------------------------------------------
class TestImageOptimization:
    def test_optimize_small_image_unchanged(self):
        from PIL import Image as PILImage
        img = PILImage.new("RGB", (100, 100), "red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw = buf.getvalue()
        optimized, ct = main._optimize_image(raw, "image/jpeg")
        assert ct == "image/jpeg"
        assert len(optimized) > 0

    def test_optimize_large_image_resized(self):
        from PIL import Image as PILImage
        img = PILImage.new("RGB", (4000, 3000), "blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        optimized, ct = main._optimize_image(raw, "image/png")
        assert ct == "image/jpeg"
        result_img = PILImage.open(io.BytesIO(optimized))
        assert max(result_img.size) <= main.IMAGE_MAX_PIXELS

    def test_optimize_non_image_passthrough(self):
        raw = b"not an image"
        optimized, ct = main._optimize_image(raw, "application/pdf")
        assert optimized == raw
        assert ct == "application/pdf"


# ---------------------------------------------------------------------------
# Flush — irreversible, so the guards matter more than the happy path
# ---------------------------------------------------------------------------
class TestFlush:
    def test_wrong_confirmation_phrase_deletes_nothing(self, client):
        with patch("main.store") as st:
            st.FLUSH_CONFIRM = "ล้างข้อมูล"
            r = client.post("/api/admin/flush",
                            json={"scope": "all", "confirm": "ลบ"})
            assert r.status_code == 400
            st.flush_data.assert_not_called()

    def test_empty_confirmation_deletes_nothing(self, client):
        with patch("main.store") as st:
            st.FLUSH_CONFIRM = "ล้างข้อมูล"
            r = client.post("/api/admin/flush", json={"scope": "all"})
            assert r.status_code == 400
            st.flush_data.assert_not_called()

    def test_unknown_scope_is_rejected(self, client):
        with patch("main.store") as st:
            st.FLUSH_CONFIRM = "ล้างข้อมูล"
            r = client.post("/api/admin/flush",
                            json={"scope": "everything", "confirm": "ล้างข้อมูล"})
            assert r.status_code == 400
            st.flush_data.assert_not_called()

    def test_default_scope_spares_real_data(self, client):
        # Omitting scope must not wipe production data.
        with patch("main.store") as st, patch("main.analytics"):
            st.FLUSH_CONFIRM = "ล้างข้อมูล"
            st.flush_data.return_value = {"orders": 4, "pending": 0,
                                          "dead_letter": 0, "activity": 0, "images": 0}
            r = client.post("/api/admin/flush", json={"confirm": "ล้างข้อมูล"})
            assert r.status_code == 200
            assert st.flush_data.call_args.kwargs["mock_only"] is True

    def test_scope_all_wipes_everything_but_activity_by_default(self, client):
        with patch("main.store") as st, patch("main.analytics"):
            st.FLUSH_CONFIRM = "ล้างข้อมูล"
            st.flush_data.return_value = {"orders": 434, "pending": 2,
                                          "dead_letter": 1, "activity": 0, "images": 5}
            r = client.post("/api/admin/flush",
                            json={"scope": "all", "confirm": "ล้างข้อมูล"})
            assert r.status_code == 200
            assert st.flush_data.call_args.kwargs == {"mock_only": False,
                                                      "include_activity": False}
            assert r.json()["deleted"]["orders"] == 434

    def test_flush_is_recorded_in_the_activity_log(self, client):
        with patch("main.store") as st, patch("main.analytics"):
            st.FLUSH_CONFIRM = "ล้างข้อมูล"
            st.flush_data.return_value = {"orders": 3, "pending": 0,
                                          "dead_letter": 0, "activity": 0, "images": 0}
            client.post("/api/admin/flush",
                        json={"scope": "all", "confirm": "ล้างข้อมูล"})
            st.log_activity.assert_called_once()
            assert st.log_activity.call_args[0][0] == "flush_data"

    def test_flush_clears_the_analytics_cache(self, client):
        with patch("main.store") as st, patch("main.analytics") as an:
            st.FLUSH_CONFIRM = "ล้างข้อมูล"
            st.flush_data.return_value = {"orders": 1, "pending": 0,
                                          "dead_letter": 0, "activity": 0, "images": 0}
            client.post("/api/admin/flush",
                        json={"scope": "all", "confirm": "ล้างข้อมูล"})
            an.invalidate_cache.assert_called_once()

    def test_preview_does_not_delete(self, client):
        with patch("main.store") as st:
            st.FLUSH_CONFIRM = "ล้างข้อมูล"
            st.flush_preview.return_value = {"orders": 434, "pending": 0,
                                             "dead_letter": 0, "activity": 0, "images": 12}
            r = client.get("/api/admin/flush/preview?scope=all")
            assert r.status_code == 200
            assert r.json()["counts"]["orders"] == 434
            st.flush_data.assert_not_called()


# ---------------------------------------------------------------------------
# SAP hand-off tracking — the point is that nothing silently goes unexported
# ---------------------------------------------------------------------------
def _order_row(oid, status="approved", exported=False):
    row = {"id": oid, "order_no": oid, "status": status,
           "document_date": "2026-07-01", "lines": [],
           "approved_by": "test@test.com"}
    if exported:
        row["exported_at"] = "2026-07-02T10:00:00"
    return row


class TestExportTracking:
    # Only an approver's export is the real SAP hand-over; admin exports are tests.
    @pytest.fixture
    def client(self):
        async def approver(authorization: str = ""):
            return _mock_user(role="approver")
        app.dependency_overrides[main.auth.verify_token] = approver
        app.dependency_overrides[main.admin_only] = _fake_verify_token
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_export_marks_only_approved_orders(self, client):
        rows = [_order_row("a"), _order_row("b"),
                _order_row("c", status="draft")]
        with patch("main.store") as st, patch("main.excel_export") as xl, \
             patch("main.analytics"):
            st.list_orders.return_value = (rows, None)
            xl.build_workbook.return_value = b"xlsx"
            st.mark_exported.return_value = "batch1"
            r = client.get("/api/export?status=all")
            assert r.status_code == 200
            assert st.mark_exported.call_args[0][0] == ["a", "b"]

    def test_only_new_skips_already_exported(self, client):
        rows = [_order_row("a", exported=True), _order_row("b")]
        with patch("main.store") as st, patch("main.excel_export") as xl, \
             patch("main.analytics"):
            st.list_orders.return_value = (rows, None)
            xl.build_workbook.return_value = b"xlsx"
            st.mark_exported.return_value = "batch2"
            client.get("/api/export?only_new=true")
            assert st.mark_exported.call_args[0][0] == ["b"]

    def test_empty_result_is_an_error_not_an_empty_file(self, client):
        # Handing someone a zero-row workbook looks like a successful export.
        with patch("main.store") as st, patch("main.excel_export") as xl:
            st.list_orders.return_value = ([_order_row("a", exported=True)], None)
            r = client.get("/api/export?only_new=true")
            assert r.status_code == 404
            xl.build_workbook.assert_not_called()

    def test_mark_false_downloads_without_marking(self, client):
        with patch("main.store") as st, patch("main.excel_export") as xl, \
             patch("main.analytics"):
            st.list_orders.return_value = ([_order_row("a")], None)
            xl.build_workbook.return_value = b"xlsx"
            r = client.get("/api/export?mark=false")
            assert r.status_code == 200
            st.mark_exported.assert_not_called()

    def test_export_is_logged(self, client):
        with patch("main.store") as st, patch("main.excel_export") as xl, \
             patch("main.analytics"):
            st.list_orders.return_value = ([_order_row("a")], None)
            xl.build_workbook.return_value = b"xlsx"
            st.mark_exported.return_value = "batch3"
            client.get("/api/export")
            assert st.log_activity.call_args[0][0] == "export"

    def test_status_endpoint(self, client):
        with patch("main.store") as st:
            st.export_status.return_value = {"pending": 7, "exported": 100,
                                             "oldest_pending": "2026-06-01"}
            r = client.get("/api/export/status")
            assert r.json()["pending"] == 7

    def test_undo_restores_and_is_logged(self, client):
        with patch("main.store") as st:
            st.undo_export_batch.return_value = 5
            r = client.post("/api/export/batches/b1/undo")
            assert r.json()["cleared"] == 5
            assert st.log_activity.call_args[0][0] == "export_undo"
