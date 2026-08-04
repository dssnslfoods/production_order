"""Unit tests for firestore_store.py — logic tests with mocked Firestore."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch
import firestore_store as store


# ---------------------------------------------------------------------------
# get_permissions: admin lockout prevention
# ---------------------------------------------------------------------------
class TestGetPermissions:
    def test_defaults_when_no_data(self):
        mock_doc = MagicMock()
        mock_doc.exists = False
        with patch.object(store, "db") as mock_db:
            mock_db().collection().document().get.return_value = mock_doc
            perms = store.get_permissions()
        assert "admin" in perms
        assert "supervisor" in perms
        assert "staff" in perms
        assert "users" in perms["admin"]
        assert "settings" in perms["admin"]

    def test_admin_always_keeps_must_have_perms(self):
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {
            "role_permissions": {
                "admin": ["dashboard", "scan"],
                "supervisor": ["dashboard"],
                "staff": ["dashboard"],
            }
        }
        with patch.object(store, "db") as mock_db:
            mock_db().collection().document().get.return_value = mock_doc
            perms = store.get_permissions()
        assert "users" in perms["admin"]
        assert "settings" in perms["admin"]
        assert "dashboard" in perms["admin"]

    def test_missing_role_gets_default(self):
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {
            "role_permissions": {
                "admin": ["dashboard", "users", "settings"],
            }
        }
        with patch.object(store, "db") as mock_db:
            mock_db().collection().document().get.return_value = mock_doc
            perms = store.get_permissions()
        assert "supervisor" in perms
        assert "staff" in perms


# ---------------------------------------------------------------------------
# save_permissions: admin lockout prevention
# ---------------------------------------------------------------------------
class TestSavePermissions:
    def test_admin_must_have_enforced(self):
        input_perms = {"admin": ["scan"], "supervisor": [], "staff": []}
        with patch.object(store, "db") as mock_db, \
             patch.object(store, "get_permissions", return_value=input_perms):
            mock_db().collection().document().set = MagicMock()
            store.save_permissions(input_perms)
        assert "users" in input_perms["admin"]
        assert "settings" in input_perms["admin"]
        assert "dashboard" in input_perms["admin"]


# ---------------------------------------------------------------------------
# save_settings: selective merge
# ---------------------------------------------------------------------------
class TestSaveSettings:
    def test_provider_update(self):
        with patch.object(store, "get_settings", return_value=dict(store.DEFAULT_SETTINGS)), \
             patch.object(store, "db") as mock_db:
            mock_db().collection().document().set = MagicMock()
            result = store.save_settings({"provider": "gemini"})
        assert result["provider"] == "gemini"

    def test_empty_api_key_not_overwritten(self):
        existing = dict(store.DEFAULT_SETTINGS)
        existing["api_keys"] = {"claude": "sk-real-key", "gemini": "", "openai": ""}
        with patch.object(store, "get_settings", return_value=existing), \
             patch.object(store, "db") as mock_db:
            mock_db().collection().document().set = MagicMock()
            result = store.save_settings({"api_keys": {"claude": ""}})
        # empty value should NOT overwrite existing key
        assert result["api_keys"]["claude"] == "sk-real-key"

    def test_new_api_key_overwrites(self):
        existing = dict(store.DEFAULT_SETTINGS)
        existing["api_keys"] = {"claude": "old-key", "gemini": "", "openai": ""}
        with patch.object(store, "get_settings", return_value=existing), \
             patch.object(store, "db") as mock_db:
            mock_db().collection().document().set = MagicMock()
            result = store.save_settings({"api_keys": {"claude": "new-key"}})
        assert result["api_keys"]["claude"] == "new-key"

    def test_drive_folder_update(self):
        with patch.object(store, "get_settings", return_value=dict(store.DEFAULT_SETTINGS)), \
             patch.object(store, "db") as mock_db:
            mock_db().collection().document().set = MagicMock()
            result = store.save_settings({"drive_folder_id": "abc123"})
        assert result["drive_folder_id"] == "abc123"

    def test_models_merge(self):
        existing = dict(store.DEFAULT_SETTINGS)
        existing["models"] = {"claude": "old-model", "gemini": "g1", "openai": "o1"}
        with patch.object(store, "get_settings", return_value=existing), \
             patch.object(store, "db") as mock_db:
            mock_db().collection().document().set = MagicMock()
            result = store.save_settings({"models": {"claude": "new-model"}})
        assert result["models"]["claude"] == "new-model"
        assert result["models"]["gemini"] == "g1"


# ---------------------------------------------------------------------------
# update_order: allowed fields filter
# ---------------------------------------------------------------------------
class TestUpdateOrder:
    def test_filters_disallowed_fields(self):
        with patch.object(store, "db") as mock_db, \
             patch.object(store, "get_order", return_value={"id": "123"}):
            mock_update = MagicMock()
            mock_db().collection().document().update = mock_update
            store.update_order("123", {
                "order_no": "OD001",
                "status": "approved",  # should be filtered out
                "scanned_by": "hacker",  # should be filtered out
                "product_name": "ขนมปัง",
            })
        call_args = mock_update.call_args[0][0]
        assert "order_no" in call_args
        assert "product_name" in call_args
        assert "status" not in call_args
        assert "scanned_by" not in call_args
        assert call_args["edited"] is True


# ---------------------------------------------------------------------------
# get_user_role: first-user-is-admin logic
# ---------------------------------------------------------------------------
class TestGetUserRole:
    def test_existing_user_returns_role(self):
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {"role": "supervisor"}
        with patch.object(store, "db") as mock_db:
            mock_db().collection().document().get.return_value = mock_doc
            role = store.get_user_role("uid1", "user@test.com")
        assert role == "supervisor"

    def test_first_user_becomes_admin(self):
        mock_doc = MagicMock()
        mock_doc.exists = False
        with patch.object(store, "db") as mock_db, \
             patch.object(store, "_count_users", return_value=0), \
             patch.object(store, "_create_user_doc") as mock_create:
            mock_db().collection().document().get.return_value = mock_doc
            role = store.get_user_role("uid1", "first@test.com")
        assert role == "admin"
        mock_create.assert_called_once_with("uid1", "first@test.com", "admin", "system")

    def test_subsequent_user_becomes_staff(self):
        mock_doc = MagicMock()
        mock_doc.exists = False
        with patch.object(store, "db") as mock_db, \
             patch.object(store, "_count_users", return_value=3), \
             patch.object(store, "_create_user_doc") as mock_create:
            mock_db().collection().document().get.return_value = mock_doc
            role = store.get_user_role("uid2", "new@test.com")
        assert role == "staff"
        mock_create.assert_called_once_with("uid2", "new@test.com", "staff", "auto")


# ---------------------------------------------------------------------------
# find_by_order_no: duplicate detection
# ---------------------------------------------------------------------------
class TestFindByOrderNo:
    def test_none_returns_none(self):
        assert store.find_by_order_no(None) is None

    def test_empty_returns_none(self):
        assert store.find_by_order_no("") is None


# ---------------------------------------------------------------------------
# update_user_role: validation
# ---------------------------------------------------------------------------
class TestUpdateUserRole:
    def test_invalid_role_raises(self):
        with pytest.raises(ValueError, match="role"):
            store.update_user_role("uid1", "superadmin")


# ---------------------------------------------------------------------------
# DEFAULT constants validation
# ---------------------------------------------------------------------------
class TestDefaults:
    def test_valid_roles(self):
        assert store.VALID_ROLES == {"admin", "supervisor", "staff"}

    def test_all_default_roles_present(self):
        for role in store.VALID_ROLES:
            assert role in store.DEFAULT_PERMISSIONS

    def test_admin_has_all_perms(self):
        admin_perms = store.DEFAULT_PERMISSIONS["admin"]
        assert "users" in admin_perms
        assert "settings" in admin_perms
        assert "approve" in admin_perms
        assert "delete" in admin_perms
        assert "export" in admin_perms

    def test_dead_letter_constants(self):
        assert store.MAX_RETRY == 3
        assert store.LOG_RETENTION_DAYS == 90
        assert store.DEAD_LETTER == "dead_letter"


# ---------------------------------------------------------------------------
# fail_pending: dead letter queue after MAX_RETRY
# ---------------------------------------------------------------------------
class TestFailPending:
    def test_first_failure_stays_pending(self):
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {"status": "pending", "retry_count": 0}
        with patch.object(store, "db") as mock_db:
            mock_db().collection().document().get.return_value = mock_doc
            mock_db().collection().document().update = MagicMock()
            result = store.fail_pending("p1", Exception("oops"))
        assert result == "failed"

    def test_moves_to_dead_letter_after_max_retry(self):
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {"status": "failed", "retry_count": 2,
                                          "filename": "x.jpg", "storage_path": "p/x"}
        with patch.object(store, "db") as mock_db:
            mock_db().collection().document().get.return_value = mock_doc
            mock_db().collection().document().set = MagicMock()
            mock_db().collection().document().delete = MagicMock()
            result = store.fail_pending("p1", Exception("still failing"))
        assert result == "dead"


# ---------------------------------------------------------------------------
# Permission migration: a release that adds pages must not remove access
# ---------------------------------------------------------------------------
class TestPermissionMigration:
    def test_defaults_include_the_analytics_pages(self):
        for role in ("admin", "supervisor", "staff"):
            granted = store.DEFAULT_PERMISSIONS[role]
            for key in ("ask", "forecast", "health"):
                assert key in granted, f"{role} missing {key}"

    def test_old_saved_permissions_are_topped_up(self):
        old = {
            "admin": ["dashboard", "scan", "orders", "users", "settings"],
            "supervisor": ["dashboard", "scan", "orders"],
            "staff": ["dashboard", "scan"],
        }
        with patch.object(store, "get_settings",
                          return_value={"role_permissions": old, "perm_version": 1}), \
             patch.object(store, "db") as db:
            perms = store.get_permissions()
            db.assert_called()      # persisted once, not recomputed on every read
        for role in ("admin", "supervisor", "staff"):
            for key in ("ask", "forecast", "health"):
                assert key in perms[role]

    def test_current_version_is_left_alone(self):
        saved = {
            "admin": ["dashboard", "users", "settings"],
            "supervisor": ["dashboard"],
            "staff": ["dashboard"],
        }
        with patch.object(store, "get_settings",
                          return_value={"role_permissions": saved,
                                        "perm_version": store.PERM_VERSION}), \
             patch.object(store, "db") as db:
            perms = store.get_permissions()
            db.assert_not_called()
        # An admin who deliberately removed a page keeps it removed.
        assert "ask" not in perms["supervisor"]

    def test_admin_cannot_be_locked_out(self):
        with patch.object(store, "get_settings",
                          return_value={"role_permissions": {"admin": []},
                                        "perm_version": store.PERM_VERSION}), \
             patch.object(store, "db"):
            perms = store.get_permissions()
        for must in ("dashboard", "users", "settings"):
            assert must in perms["admin"]
