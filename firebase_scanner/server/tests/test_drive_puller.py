"""Unit tests for drive_puller.py — parse_folder_id is a pure function."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch

with patch("google.auth.default", return_value=(MagicMock(), "test-project")):
    from drive_puller import parse_folder_id


class TestParseFolderId:
    def test_bare_id(self):
        assert parse_folder_id("1abc-XYZ_def") == "1abc-XYZ_def"

    def test_full_url_with_folders(self):
        url = "https://drive.google.com/drive/folders/1abc-XYZ_def?usp=sharing"
        assert parse_folder_id(url) == "1abc-XYZ_def"

    def test_url_with_id_param(self):
        url = "https://drive.google.com/drive/u/0/folders?id=1abc-XYZ_def"
        assert parse_folder_id(url) == "1abc-XYZ_def"

    def test_url_with_open_id(self):
        url = "https://drive.google.com/open?id=1abc-XYZ_def"
        assert parse_folder_id(url) == "1abc-XYZ_def"

    def test_none_returns_empty(self):
        assert parse_folder_id(None) == ""

    def test_empty_returns_empty(self):
        assert parse_folder_id("") == ""

    def test_whitespace_stripped(self):
        assert parse_folder_id("  1abc  ") == "1abc"

    def test_long_folder_id(self):
        fid = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        url = f"https://drive.google.com/drive/folders/{fid}"
        assert parse_folder_id(url) == fid

    def test_mobile_url(self):
        url = "https://drive.google.com/drive/mobile/folders/1abc-XYZ_def"
        assert parse_folder_id(url) == "1abc-XYZ_def"
