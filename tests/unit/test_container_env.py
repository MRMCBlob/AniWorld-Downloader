"""Container detection and the startup work it must suppress.

The container image ships Chromium under a root-owned PLAYWRIGHT_BROWSERS_PATH,
so a runtime install can only fail. That was guarded by comparing the download
path against a hardcoded "/app/Downloads", which silently stopped working the
moment the path became configurable — the container then tried, and failed, to
install a browser on every single start.

These tests pin the behaviour rather than the mechanism, so the next person to
change a path cannot break it again without a red test.
"""

import subprocess

import pytest

from aniworld import autodeps
from aniworld.env import in_docker


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("ANIWORLD_DOCKER", raising=False)
    # A DISPLAY is always set by the container entrypoint; without it the Xvfb
    # branch runs first and these tests would measure the wrong thing.
    monkeypatch.setenv("DISPLAY", ":99")


def test_docker_is_detected_from_the_env_override(monkeypatch):
    monkeypatch.setattr(autodeps.os.path, "exists", lambda p: False)
    assert in_docker() is False

    monkeypatch.setenv("ANIWORLD_DOCKER", "1")
    assert in_docker() is True


def test_docker_is_detected_from_dockerenv(monkeypatch):
    import os

    monkeypatch.setattr(os.path, "exists", lambda p: p == "/.dockerenv")

    assert in_docker() is True


def test_no_browser_install_is_attempted_in_a_container(monkeypatch):
    """The regression: this used to shell out and fail on every start."""
    monkeypatch.setenv("ANIWORLD_DOCKER", "1")

    def fail(*args, **kwargs):
        pytest.fail("ensure_patchright_chromium must not shell out in a container")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)

    autodeps.ensure_patchright_chromium()


def test_install_is_still_attempted_outside_a_container(monkeypatch):
    """The container guard must not disable the feature for normal installs."""
    import os

    monkeypatch.delenv("ANIWORLD_DOCKER", raising=False)
    monkeypatch.setattr(os.path, "exists", lambda p: False)

    calls = []

    def record(cmd, *args, **kwargs):
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", record)

    autodeps.ensure_patchright_chromium()

    assert len(calls) == 1
    assert "install" in calls[0]


def test_xvfb_is_not_apt_installed_in_a_container(monkeypatch):
    """`sudo apt-get install` cannot work unprivileged and must not be tried."""
    monkeypatch.setenv("ANIWORLD_DOCKER", "1")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(autodeps.shutil, "which", lambda _: None)

    def fail(*args, **kwargs):
        pytest.fail("must not try to apt-get install inside a container")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)

    autodeps._ensure_xvfb()


def test_a_set_display_short_circuits_the_xvfb_path(monkeypatch):
    """The entrypoint starts Xvfb and exports DISPLAY; nothing more to do."""
    monkeypatch.setenv("DISPLAY", ":99")

    def fail(*args, **kwargs):
        pytest.fail("must not touch Xvfb when DISPLAY is already set")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)

    autodeps._ensure_xvfb()


def test_captcha_module_shares_the_same_detection(monkeypatch):
    """Two implementations would drift; captcha.py delegates to env.in_docker."""
    from aniworld.playwright.captcha import _in_docker

    monkeypatch.setenv("ANIWORLD_DOCKER", "1")
    assert _in_docker() is True
