# Tests

Two kinds, kept apart because only one of them can run in CI.

## `tests/unit/` — offline

No network, no browser, no real Sonarr. HTTP is a fake transport that records
the requests it received, so the tests assert on the exact bytes Sonarr, Radarr
and Jellyfin would see.

```bash
pip install -e ".[all,dev]"
pytest
```

`testpaths` in `pyproject.toml` points at this directory, so a bare `pytest`
runs these and nothing else.

| File | Covers |
|---|---|
| `test_sonarr.py` / `test_radarr.py` | The two-step ManualImport flow, wire format, lookup precedence |
| `test_jellyfin.py` | Auth header, targeted vs. full library scan |
| `test_classify.py` | Series vs. movie across the inconsistent site models |
| `test_postprocess.py` | Verify, stage, and how each step degrades |
| `test_queue_db.py` | Schema migration, priority, retry backoff |
| `test_events.py` | Event bus, abort registry |
| `test_webhooks.py` | Outbox, signing, retries |
| `test_api.py` | REST endpoints, API-key auth |
| `test_pipeline_end_to_end.py` | All of it wired together |

## `tests/test_aniworld_*.py` — live

These hit the real streaming sites. They are the original scripts and are
**opt-in**:

```bash
python tests/test_aniworld_models.py
python tests/test_aniworld_providers.py
```

They stay out of CI on purpose: they fail on geo-blocks and captchas, and would
hammer third-party servers on every push. Run them locally when changing an
extractor or a site model — that is the only way to catch a site changing its
markup.
