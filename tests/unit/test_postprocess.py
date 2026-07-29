"""Post-processing pipeline: verify, stage, classify, import, scan.

The pipeline's whole point is that it degrades instead of failing, so most of
these tests are about what happens when a step goes wrong: the file must still
end up safe in the completed folder.
"""

import os

import pytest

from aniworld import events, postprocess
from aniworld.postprocess import FinalizeResult, stage_file, verify_media


class Series:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Episode:
    """Duck-typed stand-in for a site model's episode object."""

    def __init__(self, path, selected_path, **kwargs):
        self._episode_path = path
        self.selected_path = str(selected_path)
        self.season = None
        self.series = Series(title="Some Show", release_year=2012)
        self.episode_number = 1
        self.is_movie = False
        self.__dict__.update(kwargs)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    incomplete = tmp_path / "incomplete"
    completed = tmp_path / "completed"
    incomplete.mkdir()
    monkeypatch.setenv("ANIWORLD_COMPLETED_PATH", str(completed))
    return incomplete, completed


@pytest.fixture(autouse=True)
def no_subscribers():
    events.clear()
    yield
    events.clear()


def make_media(directory, name="Some Show S01E01.mkv", size=postprocess.MIN_MEDIA_BYTES * 2):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\0" * size)
    return path


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def test_verify_rejects_a_missing_file(tmp_path):
    ok, detail = verify_media(tmp_path / "nope.mkv")
    assert ok is False
    assert "does not exist" in detail


def test_verify_rejects_a_truncated_file(tmp_path):
    path = tmp_path / "stub.mkv"
    path.write_bytes(b"error page")

    ok, detail = verify_media(path)

    assert ok is False
    assert "bytes" in detail


def test_verify_falls_back_to_size_when_ffprobe_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(postprocess.shutil, "which", lambda _: None)
    path = make_media(tmp_path)

    ok, detail = verify_media(path)

    assert ok is True
    assert "ffprobe unavailable" in detail


def test_verify_rejects_a_file_with_no_video_stream(tmp_path, monkeypatch):
    path = make_media(tmp_path)
    monkeypatch.setattr(postprocess.shutil, "which", lambda _: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        postprocess.subprocess,
        "run",
        lambda *a, **k: _Completed(
            0, '{"format":{"duration":"120.0"},"streams":[{"codec_type":"audio"}]}'
        ),
    )

    ok, detail = verify_media(path)

    assert ok is False
    assert "no video stream" in detail


def test_verify_accepts_a_healthy_file(tmp_path, monkeypatch):
    path = make_media(tmp_path)
    monkeypatch.setattr(postprocess.shutil, "which", lambda _: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        postprocess.subprocess,
        "run",
        lambda *a, **k: _Completed(
            0,
            '{"format":{"duration":"1422.0"},'
            '"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
        ),
    )

    ok, _ = verify_media(path)

    assert ok is True


class _Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --------------------------------------------------------------------------- #
# staging
# --------------------------------------------------------------------------- #


def test_stage_preserves_the_relative_layout(roots):
    incomplete, completed = roots
    source = make_media(incomplete / "Some Show (2012)" / "Season 01")

    destination = stage_file(source, incomplete, completed)

    assert destination == completed / "Some Show (2012)" / "Season 01" / source.name
    assert destination.exists()
    assert not source.exists()


def test_stage_removes_the_directories_it_emptied(roots):
    incomplete, completed = roots
    source = make_media(incomplete / "Show" / "Season 01")

    stage_file(source, incomplete, completed)

    assert not (incomplete / "Show").exists()
    assert incomplete.exists(), "the staging root itself must survive"


def test_stage_keeps_a_sibling_file_and_its_directory(roots):
    incomplete, completed = roots
    source = make_media(incomplete / "Show" / "Season 01")
    sibling = make_media(incomplete / "Show" / "Season 01", name="Some Show S01E02.mkv")

    stage_file(source, incomplete, completed)

    assert sibling.exists()
    assert (incomplete / "Show" / "Season 01").exists()


def test_stage_outside_the_root_falls_back_to_the_file_name(tmp_path, roots):
    _, completed = roots
    source = make_media(tmp_path / "elsewhere")

    destination = stage_file(source, tmp_path / "incomplete", completed)

    assert destination == completed / source.name


def test_stage_is_a_no_op_when_staging_is_disabled(tmp_path):
    source = make_media(tmp_path / "in")

    assert stage_file(source, tmp_path / "in", None) == source
    assert source.exists()


def test_stage_overwrites_an_existing_destination(roots):
    incomplete, completed = roots
    source = make_media(incomplete / "Show")
    stale = completed / "Show" / source.name
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    destination = stage_file(source, incomplete, completed)

    assert destination.stat().st_size == postprocess.MIN_MEDIA_BYTES * 2


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #


@pytest.fixture
def healthy_probe(monkeypatch):
    monkeypatch.setattr(postprocess.shutil, "which", lambda _: None)


def test_finalize_stages_and_imports(roots, healthy_probe, monkeypatch):
    incomplete, completed = roots
    path = make_media(incomplete / "Some Show" / "Season 01")
    episode = Episode(path, incomplete)

    imported = {}

    def fake_import(file_path, info, root=None):
        imported["path"] = str(file_path)
        imported["type"] = info.media_type
        return {"ok": True, "destination_path": "/data/TV/Some Show"}

    monkeypatch.setattr(postprocess, "import_media", fake_import)
    monkeypatch.setattr(postprocess, "trigger_jellyfin_scan", lambda r: {"ok": True})

    result = postprocess.finalize(episode, queue_id=3)

    assert result.ok is True
    assert result.imported is True
    assert result.status == "imported"
    assert imported["type"] == "series"
    assert imported["path"] == str(completed / "Some Show" / "Season 01" / path.name)
    assert not path.exists()


def test_finalize_fails_before_moving_an_unplayable_file(roots, monkeypatch):
    incomplete, completed = roots
    path = make_media(incomplete / "Show", size=10)
    episode = Episode(path, incomplete)
    monkeypatch.setattr(
        postprocess, "import_media", lambda *a, **k: pytest.fail("must not import")
    )

    result = postprocess.finalize(episode)

    assert result.ok is False
    assert result.status == "failed"
    assert "verification failed" in result.error
    assert path.exists(), "a rejected file stays put so it can be inspected"


def test_a_failed_import_still_counts_as_a_completed_download(
    roots, healthy_probe, monkeypatch
):
    incomplete, completed = roots
    path = make_media(incomplete / "Show")
    episode = Episode(path, incomplete)
    monkeypatch.setattr(
        postprocess,
        "import_media",
        lambda *a, **k: {"ok": False, "reason": "series_not_in_sonarr"},
    )

    result = postprocess.finalize(episode)

    assert result.ok is True
    assert result.imported is False
    assert result.status == "completed"
    assert (completed / "Show" / path.name).exists()


def test_jellyfin_is_not_scanned_when_the_import_failed(roots, healthy_probe, monkeypatch):
    incomplete, _ = roots
    episode = Episode(make_media(incomplete / "Show"), incomplete)
    monkeypatch.setattr(postprocess, "import_media", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(
        postprocess,
        "trigger_jellyfin_scan",
        lambda r: pytest.fail("must not scan without an import"),
    )

    assert postprocess.finalize(episode).imported is False


def test_events_are_published_for_a_full_run(roots, healthy_probe, monkeypatch):
    incomplete, _ = roots
    episode = Episode(make_media(incomplete / "Show"), incomplete)
    monkeypatch.setattr(postprocess, "import_media", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(postprocess, "trigger_jellyfin_scan", lambda r: {"ok": True})

    seen = []
    events.subscribe(seen.append)

    postprocess.finalize(episode, queue_id=11)

    assert [e.event for e in seen] == [
        events.DOWNLOAD_COMPLETED,
        events.IMPORT_COMPLETED,
    ]
    assert seen[0].queue_id == 11
    assert seen[0].type == "series"


def test_stage_callbacks_report_progress(roots, healthy_probe, monkeypatch):
    incomplete, _ = roots
    episode = Episode(make_media(incomplete / "Show"), incomplete)
    monkeypatch.setattr(postprocess, "import_media", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(postprocess, "trigger_jellyfin_scan", lambda r: {"ok": True})

    stages = []
    postprocess.finalize(episode, on_stage=stages.append)

    assert stages == ["verifying", "staging", "importing", "scanning", "done"]


def test_a_broken_stage_callback_does_not_break_the_pipeline(
    roots, healthy_probe, monkeypatch
):
    incomplete, _ = roots
    episode = Episode(make_media(incomplete / "Show"), incomplete)
    monkeypatch.setattr(postprocess, "import_media", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(postprocess, "trigger_jellyfin_scan", lambda r: {"ok": True})

    def explode(_):
        raise RuntimeError("db is down")

    assert postprocess.finalize(episode, on_stage=explode).ok is True


def test_movie_is_routed_by_classification(roots, healthy_probe, monkeypatch):
    incomplete, _ = roots
    path = make_media(incomplete / "Spirited Away (2001)", name="Spirited Away (2001).mkv")
    episode = Episode(path, incomplete, is_movie=True)
    episode.series = Series(title="Spirited Away", release_year=2001)

    seen = {}
    monkeypatch.setattr(
        postprocess,
        "import_media",
        lambda p, info, root=None: seen.update(type=info.media_type) or {"ok": True},
    )
    monkeypatch.setattr(postprocess, "trigger_jellyfin_scan", lambda r: {"ok": True})

    result = postprocess.finalize(episode)

    assert seen["type"] == "movie"
    assert result.media_type == "movie"


def test_media_type_override_wins(roots, healthy_probe, monkeypatch):
    incomplete, _ = roots
    episode = Episode(make_media(incomplete / "Show"), incomplete, is_movie=True)

    seen = {}
    monkeypatch.setattr(
        postprocess,
        "import_media",
        lambda p, info, root=None: seen.update(type=info.media_type) or {"ok": True},
    )
    monkeypatch.setattr(postprocess, "trigger_jellyfin_scan", lambda r: {"ok": True})

    postprocess.finalize(episode, media_type_override="series")

    assert seen["type"] == "series"


def test_unknown_file_path_is_reported():
    class Pathless:
        pass

    result = postprocess.finalize(Pathless())

    assert result.ok is False
    assert "file path" in result.error


def test_import_media_reports_an_unconfigured_service(monkeypatch):
    from aniworld.integrations.classify import MediaInfo

    monkeypatch.setattr("aniworld.integrations.get_client_for", lambda t: None)

    result = postprocess.import_media("/x.mkv", MediaInfo(media_type="movie"))

    assert result["ok"] is False
    assert result["reason"] == "not_configured"
    assert "Radarr" in result["detail"]


def test_completed_root_resolves_a_relative_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANIWORLD_COMPLETED_PATH", "media/done")

    assert postprocess.completed_root() == tmp_path / "media" / "done"


def test_completed_root_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("ANIWORLD_COMPLETED_PATH", raising=False)

    assert postprocess.completed_root() is None


def test_finalize_result_status_mapping():
    assert FinalizeResult(ok=False).status == "failed"
    assert FinalizeResult(ok=True, imported=False).status == "completed"
    assert FinalizeResult(ok=True, imported=True).status == "imported"


def test_environment_stays_clean():
    assert "ANIWORLD_COMPLETED_PATH" not in os.environ
