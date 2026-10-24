from pathlib import Path

import pytest

from emailsupersearch.paths import data_root, ensure_layout, validate_data_root


def test_data_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(tmp_path))
    assert data_root() == tmp_path


def test_data_root_default_is_localappdata(monkeypatch):
    monkeypatch.delenv("EMAILSUPERSEARCH_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert data_root() == Path(r"C:\Users\x\AppData\Local\eMailSuperSearch")


@pytest.mark.parametrize("bad", [r"C:\Users\x\OneDrive\ess", r"D:\onedrive\ess", r"C:\Users\x\OneDrive - Contoso\ess", r"C:\Users\x\Documents\ess", r"C:\Users\x\Desktop\ess"])
def test_validate_data_root_refuses_synced_and_known_folders(bad):
    assert validate_data_root(Path(bad)) is not None


def test_validate_data_root_refuses_real_userprofile_known_folders(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert validate_data_root(tmp_path / "Documents" / "ess") is not None
    assert validate_data_root(tmp_path / "Desktop" / "ess") is not None
    assert validate_data_root(tmp_path / "AppData" / "Local" / "ess") is None


def test_validate_data_root_accepts_localappdata():
    assert validate_data_root(Path(r"C:\Users\x\AppData\Local\eMailSuperSearch")) is None


def test_ensure_layout_creates_dirs(tmp_path):
    ensure_layout(tmp_path)
    assert all((tmp_path / d).is_dir() for d in ["projects", "cache/parsed", "models", "logs"])
