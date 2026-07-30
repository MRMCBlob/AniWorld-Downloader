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

from aniworld import autodeps, env
from aniworld.env import in_docker

# Captured before the autouse fixture below stubs it out, so the probe itself
# can still be tested.
_real_cgroup_probe = env._cgroup_names_a_container


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("ANIWORLD_DOCKER", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    # The machine running the suite may itself be containerised; pin the probe
    # so these tests measure the code and not their own CI runner.
    monkeypatch.setattr(env, "_cgroup_names_a_container", lambda: False)
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


def test_podman_is_detected_from_containerenv(monkeypatch):
    """Podman writes /run/.containerenv and never /.dockerenv."""
    import os

    monkeypatch.setattr(os.path, "exists", lambda p: p == "/run/.containerenv")

    assert in_docker() is True


def test_kubernetes_is_detected_from_the_injected_service_env(monkeypatch):
    import os

    monkeypatch.setattr(os.path, "exists", lambda p: False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")

    assert in_docker() is True


def test_a_runtime_without_marker_files_is_detected_from_cgroup(monkeypatch):
    """Rootless and nested runtimes leave no marker file; cgroup still names them."""
    import os

    monkeypatch.setattr(os.path, "exists", lambda p: False)
    monkeypatch.setattr(env, "_cgroup_names_a_container", lambda: True)

    assert in_docker() is True


def test_the_cgroup_probe_ignores_an_ordinary_host(monkeypatch, tmp_path):
    """A desktop's cgroup must not read as a container — that would disable
    the browser install for everyone running under systemd."""
    host = tmp_path / "host-cgroup"
    host.write_text("0::/user.slice/user-1000.slice/session-3.scope\n")
    container = tmp_path / "container-cgroup"
    container.write_text("0::/docker/8f2c0b1e4a\n")

    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/self/cgroup":
            return real_open(fake_open.target, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    fake_open.target = host
    assert _real_cgroup_probe() is False

    fake_open.target = container
    assert _real_cgroup_probe() is True


def test_no_browser_install_is_attempted_in_a_container(monkeypatch):
    """The regression: this used to shell out and fail on every start."""
    monkeypatch.setenv("ANIWORLD_DOCKER", "1")

    def fail(*args, **kwargs):
        pytest.fail("ensure_patchright_chromium must not shell out in a container")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)

    autodeps.ensure_patchright_chromium()


def test_install_is_still_attempted_outside_a_container(monkeypatch, tmp_path):
    """The container guard must not disable the feature for normal installs."""
    import os

    monkeypatch.delenv("ANIWORLD_DOCKER", raising=False)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    # An empty, writable browser directory: nothing installed yet, so the
    # install must run. Without this the result would depend on whether the
    # machine running the suite happens to have Chromium already.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))

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
