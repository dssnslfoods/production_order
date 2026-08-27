"""Unit tests for auth.py — token verification and role gating."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

import auth


# ---------------------------------------------------------------------------
# _allowed: env var parsing
# ---------------------------------------------------------------------------
class TestAllowed:
    def test_empty_env(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
        assert auth._allowed() == set()

    def test_single_email(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_EMAILS", "user@test.com")
        assert auth._allowed() == {"user@test.com"}

    def test_multiple_emails(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_EMAILS", "a@test.com, B@test.com,  c@test.com  ")
        result = auth._allowed()
        assert result == {"a@test.com", "b@test.com", "c@test.com"}

    def test_lowercased(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_EMAILS", "Admin@Test.COM")
        assert auth._allowed() == {"admin@test.com"}

    def test_empty_entries_ignored(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_EMAILS", "a@test.com,,, ,b@test.com")
        assert auth._allowed() == {"a@test.com", "b@test.com"}


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------
class TestVerifyToken:
    @pytest.mark.asyncio
    async def test_missing_token_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            await auth.verify_token("")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_bearer_prefix_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            await auth.verify_token("Token abc123")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_raises_401(self):
        with patch("auth.store") as mock_store, \
             patch("auth.fb_auth") as mock_fb:
            mock_store._init = MagicMock()
            mock_fb.verify_id_token.side_effect = Exception("invalid")
            with pytest.raises(HTTPException) as exc_info:
                await auth.verify_token("Bearer bad-token")
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_existing_user_bypasses_allowlist(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_EMAILS", "other@test.com")
        with patch("auth.store") as mock_store, \
             patch("auth.fb_auth") as mock_fb:
            mock_store._init = MagicMock()
            mock_fb.verify_id_token.return_value = {"uid": "u1", "email": "notinlist@test.com"}
            mock_store.get_user_doc.return_value = {"uid": "u1", "role": "admin"}
            mock_store.get_user_info.return_value = {"role": "admin", "factory_id": "f1", "factory_code": "FAC1", "factory_name": "Factory 1"}
            result = await auth.verify_token("Bearer valid-token")
        assert result["uid"] == "u1"
        assert result["role"] == "admin"

    @pytest.mark.asyncio
    async def test_new_user_not_in_allowlist_raises_403(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_EMAILS", "allowed@test.com")
        with patch("auth.store") as mock_store, \
             patch("auth.fb_auth") as mock_fb:
            mock_store._init = MagicMock()
            mock_fb.verify_id_token.return_value = {"uid": "u2", "email": "blocked@test.com"}
            mock_store.get_user_doc.return_value = None
            with pytest.raises(HTTPException) as exc_info:
                await auth.verify_token("Bearer valid-token")
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_allowlist_permits_all(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
        with patch("auth.store") as mock_store, \
             patch("auth.fb_auth") as mock_fb:
            mock_store._init = MagicMock()
            mock_fb.verify_id_token.return_value = {"uid": "u3", "email": "anyone@test.com"}
            mock_store.get_user_doc.return_value = None
            mock_store.get_user_info.return_value = {"role": "staff", "factory_id": None, "factory_code": None, "factory_name": None}
            result = await auth.verify_token("Bearer valid-token")
        assert result["role"] == "staff"


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------
class TestRequireRole:
    @pytest.mark.asyncio
    async def test_allowed_role(self):
        check = auth.require_role("admin", "supervisor")
        with patch("auth.verify_token") as mock_verify:
            mock_verify.return_value = {"uid": "u1", "email": "a@test.com", "role": "admin"}
            result = await check("Bearer token")
        assert result["role"] == "admin"

    @pytest.mark.asyncio
    async def test_denied_role(self):
        check = auth.require_role("admin")
        with patch("auth.verify_token") as mock_verify:
            mock_verify.return_value = {"uid": "u1", "email": "a@test.com", "role": "staff"}
            with pytest.raises(HTTPException) as exc_info:
                await check("Bearer token")
            assert exc_info.value.status_code == 403
