"""Event bus and the download abort registry."""

import threading

import pytest

from aniworld import events
from aniworld.models.common import common


@pytest.fixture(autouse=True)
def clean_bus():
    events.clear()
    yield
    events.clear()


def test_events_reach_every_subscriber():
    seen_a, seen_b = [], []
    events.subscribe(seen_a.append)
    events.subscribe(seen_b.append)

    events.emit(events.DOWNLOAD_COMPLETED, title="Show", path="/x.mkv")

    assert len(seen_a) == len(seen_b) == 1
    assert seen_a[0].title == "Show"


def test_subscribing_twice_does_not_double_deliver():
    seen = []
    events.subscribe(seen.append)
    events.subscribe(seen.append)

    events.emit(events.DOWNLOAD_STARTED)

    assert len(seen) == 1


def test_a_failing_subscriber_cannot_break_the_publisher():
    seen = []

    def explode(_):
        raise RuntimeError("receiver is broken")

    events.subscribe(explode)
    events.subscribe(seen.append)

    events.emit(events.DOWNLOAD_FAILED, error="nope")

    assert len(seen) == 1, "a broken subscriber must not stop the others"


def test_unsubscribe_stops_delivery():
    seen = []
    events.subscribe(seen.append)
    events.unsubscribe(seen.append)

    events.emit(events.DOWNLOAD_STARTED)

    assert seen == []


def test_payload_matches_the_documented_shape():
    payload = events.Event(
        event=events.DOWNLOAD_COMPLETED,
        type="series",
        title="Example",
        season=1,
        episode=1,
        path="/tv/Example/Season 01/file.mkv",
        queue_id=4,
    ).to_payload()

    assert payload["event"] == "download_completed"
    assert payload["type"] == "series"
    assert payload["title"] == "Example"
    assert payload["season"] == 1
    assert payload["episode"] == 1
    assert payload["path"] == "/tv/Example/Season 01/file.mkv"
    assert "timestamp" in payload


def test_payload_drops_empty_fields_and_flattens_extra():
    payload = events.Event(
        event=events.IMPORT_COMPLETED, title="X", extra={"library": "Anime"}
    ).to_payload()

    assert "season" not in payload
    assert "extra" not in payload
    assert payload["library"] == "Anime"


# --------------------------------------------------------------------------- #
# Abort registry
# --------------------------------------------------------------------------- #


def test_abort_is_scoped_to_the_current_job():
    common.set_current_job(7)
    try:
        assert common.abort_requested() is False
        common.request_abort(7)
        assert common.abort_requested() is True
        assert common.abort_requested(8) is False
    finally:
        common.clear_abort(7)
        common.set_current_job(None)


def test_clearing_an_abort_lets_the_job_run_again():
    common.request_abort(3)
    common.clear_abort(3)

    assert common.abort_requested(3) is False


def test_a_thread_without_a_job_never_reports_an_abort():
    common.request_abort(1)
    result = {}

    def check():
        result["aborted"] = common.abort_requested()

    thread = threading.Thread(target=check)
    thread.start()
    thread.join()
    common.clear_abort(1)

    assert result["aborted"] is False


def test_the_current_job_does_not_leak_between_threads():
    common.set_current_job(42)
    seen = {}

    def check():
        seen["job"] = common.get_current_job()

    thread = threading.Thread(target=check)
    thread.start()
    thread.join()
    common.set_current_job(None)

    assert seen["job"] is None
