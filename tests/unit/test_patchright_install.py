"""The startup Chromium check in autodeps.ensure_patchright_chromium().

It used to shell out to the patchright driver on every start and, when that
failed, log nothing but an exit status:

    patchright chromium install failed: Command '[...]' returned non-zero exit
    status 1

Two problems, both pinned here. The check never asked whether the browser was
already on disk, so it ran a doomed install against a read-only browser
directory; and it threw the driver's output away, leaving the reason for the
failure unknowable from a log file.
"""

import json
import subprocess

import pytest

from aniworld import autodeps, env

REVISION = "1228"


@pytest.fixture(autouse=True)
def outside_a_container(monkeypatch):
    monkeypatch.delenv("ANIWORLD_DOCKER", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(env, "_cgroup_names_a_container", lambda: False)
    monkeypatch.setattr(env.os.path, "exists", lambda p: False)
    # The container entrypoint always exports one; without it the Xvfb branch
    # runs first and gets in the way of what these tests measure.
    monkeypatch.setenv("DISPLAY", ":99")


@pytest.fixture
def driver(tmp_path, monkeypatch):
    """A fake patchright driver: a node binary and the real browsers.json shape."""
    package = tmp_path / "driver" / "package"
    package.mkdir(parents=True)
    node = tmp_path / "driver" / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)
    cli = package / "cli.js"
    cli.write_text("")
    (package / "browsers.json").write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium", "revision": REVISION},
                    {"name": "chromium-headless-shell", "revision": REVISION},
                    {"name": "firefox", "revision": "1500"},
                ]
            }
        )
    )

    import patchright._impl._driver as pw_driver

    monkeypatch.setattr(
        pw_driver, "compute_driver_executable", lambda: (str(node), str(cli))
    )
    monkeypatch.setattr(pw_driver, "get_driver_env", dict)
    return cli


def _install_chromium(browsers_path, revision=REVISION, complete=True):
    for name in (f"chromium-{revision}", f"chromium_headless_shell-{revision}"):
        directory = browsers_path / name
        directory.mkdir(parents=True)
        if complete:
            (directory / "INSTALLATION_COMPLETE").write_text("")


@pytest.fixture
def no_subprocess(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("the driver must not be run in this case")

    monkeypatch.setattr(subprocess, "run", fail)
    return fail


def test_an_existing_install_is_not_reinstalled(
    driver, tmp_path, monkeypatch, no_subprocess
):
    """The regression: this shelled out on every start, once per container boot."""
    browsers = tmp_path / "ms-playwright"
    _install_chromium(browsers)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))

    autodeps.ensure_patchright_chromium()


def test_a_half_finished_download_is_reinstalled(driver, tmp_path, monkeypatch):
    """Without the marker file the directory exists but the browser does not."""
    browsers = tmp_path / "ms-playwright"
    _install_chromium(browsers, complete=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))

    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, ""),
    )

    autodeps.ensure_patchright_chromium()

    assert len(calls) == 1


def test_a_stale_revision_is_reinstalled(driver, tmp_path, monkeypatch):
    """An upgraded patchright wants a build the old one never downloaded."""
    browsers = tmp_path / "ms-playwright"
    _install_chromium(browsers, revision="1000")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))

    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, ""),
    )

    autodeps.ensure_patchright_chromium()

    assert len(calls) == 1


def test_a_missing_headless_shell_is_allowed_with_no_shell_install(
    driver, tmp_path, monkeypatch, no_subprocess
):
    """Upstream v5 deliberately installs Chromium with ``--no-shell``."""
    browsers = tmp_path / "ms-playwright"
    (browsers / f"chromium-{REVISION}").mkdir(parents=True)
    (browsers / f"chromium-{REVISION}" / "INSTALLATION_COMPLETE").write_text("")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))

    autodeps.ensure_patchright_chromium()


def test_a_read_only_browser_directory_is_reported_not_attempted(
    driver, tmp_path, monkeypatch, no_subprocess, caplog
):
    """What the container hit: an install that cannot possibly succeed."""
    browsers = tmp_path / "ms-playwright"
    browsers.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    monkeypatch.setattr(
        autodeps.os, "access", lambda path, mode: str(path) != str(browsers)
    )

    with caplog.at_level("WARNING"):
        autodeps.ensure_patchright_chromium()

    assert "not writable" in caplog.text
    assert str(browsers) in caplog.text


def test_a_failed_install_logs_the_drivers_output(
    driver, tmp_path, monkeypatch, caplog
):
    """A bare exit status is not something anyone can act on."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "ms-playwright"))

    def failed(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, "Error: self signed certificate in certificate chain"
        )

    monkeypatch.setattr(subprocess, "run", failed)

    with caplog.at_level("WARNING"):
        autodeps.ensure_patchright_chromium()

    assert "self signed certificate" in caplog.text
    assert "exit 1" in caplog.text


def test_the_install_output_is_captured_not_discarded(driver, tmp_path, monkeypatch):
    recorded = {}

    def record(cmd, **kwargs):
        recorded.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "")

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "ms-playwright"))
    monkeypatch.setattr(subprocess, "run", record)

    autodeps.ensure_patchright_chromium()

    assert recorded["stdout"] is subprocess.PIPE
    assert recorded["stderr"] is subprocess.STDOUT
    # check=False: the exit status is handled and logged with its output, and
    # an exception would lose that output again.
    assert recorded["check"] is False


def test_a_long_failure_message_is_trimmed():
    trimmed = autodeps._tail("x" * 5000)

    assert len(trimmed) < 1500
    assert trimmed.startswith("...")


def test_the_failure_message_drops_colours_and_retry_noise():
    """The driver retries a download three times, in colour. One copy is enough."""
    attempt = (
        "Downloading Chrome for Testing\n"
        "\x1b[2m  from https://cdn.example/chrome-linux64.zip\x1b[22m\n"
        "Error: connect ECONNREFUSED 127.0.0.1:9\n"
    )

    condensed = autodeps._tail(attempt * 3 + "Failed to install browsers\n")

    assert "\x1b" not in condensed
    assert condensed.count("ECONNREFUSED") == 1
    assert condensed.endswith("Failed to install browsers")


def test_browsers_path_zero_resolves_next_to_the_driver(driver, monkeypatch):
    """PLAYWRIGHT_BROWSERS_PATH=0 is the driver's 'keep it in the package' mode."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")

    assert (
        autodeps._browser_registry_dir(str(driver)) == driver.parent / ".local-browsers"
    )
