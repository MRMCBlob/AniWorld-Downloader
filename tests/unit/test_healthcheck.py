"""Container healthcheck storage preflight."""

import importlib.util
from pathlib import Path

import pytest

HEALTHCHECK_PATH = Path(__file__).parents[2] / "docker" / "healthcheck.py"
SPEC = importlib.util.spec_from_file_location("container_healthcheck", HEALTHCHECK_PATH)
healthcheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(healthcheck)


@pytest.fixture(autouse=True)
def clean_health_env(monkeypatch):
    for name in (
        "ANIWORLD_INSTALL_FOLDER",
        "ANIWORLD_DOWNLOAD_PATH",
        "ANIWORLD_COMPLETED_PATH",
        "ANIWORLD_STORAGE_SENTINEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_storage_check_accepts_available_directories(monkeypatch, tmp_path):
    config = tmp_path / "config"
    downloads = tmp_path / "downloads"
    config.mkdir()
    downloads.mkdir(exist_ok=True)
    monkeypatch.setenv("ANIWORLD_INSTALL_FOLDER", str(config))
    monkeypatch.setenv("ANIWORLD_DOWNLOAD_PATH", str(downloads))

    assert healthcheck.storage_error() is None


def test_storage_check_reports_a_missing_directory(monkeypatch, tmp_path):
    missing = tmp_path / "not-mounted"
    monkeypatch.setenv("ANIWORLD_DOWNLOAD_PATH", str(missing))

    assert str(missing) in healthcheck.storage_error()


def test_storage_sentinel_detects_a_lost_remote_mount(monkeypatch, tmp_path):
    sentinel = tmp_path / ".aniworld-storage"
    monkeypatch.setenv("ANIWORLD_STORAGE_SENTINEL", str(sentinel))

    assert "sentinel is missing" in healthcheck.storage_error()

    sentinel.touch()
    assert healthcheck.storage_error() is None
