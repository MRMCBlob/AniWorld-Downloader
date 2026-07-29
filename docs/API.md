# REST API

The Web UI and any external caller talk to the same endpoints.

## Authentication

When `ANIWORLD_WEB_AUTH=1`, requests need either a login session (what the
browser uses) or an API key:

```ini
ANIWORLD_API_KEY=          # openssl rand -hex 32
```

```bash
curl -H "X-Api-Key: $ANIWORLD_API_KEY" http://localhost:8080/api/queue
curl "http://localhost:8080/api/queue?apikey=$ANIWORLD_API_KEY"
```

The query parameter exists for callers that cannot set headers; prefer the
header, since URLs end up in logs.

The key is a single full-privilege credential — there are no scopes, and it
satisfies admin-only endpoints too. It is compared in constant time and never
appears in a response. Docker secrets work via `ANIWORLD_API_KEY_FILE`.

Leaving `ANIWORLD_API_KEY` unset disables key authentication; it never means
"no authentication required".

`/api/status` is reachable without credentials so the container healthcheck
works, but the unauthenticated response is limited to version, worker liveness
and queue counts.

---

## Endpoints

### `GET /api/status`

Health and state. Backs the container `HEALTHCHECK`.

```json
{
  "status": "ok",
  "version": "4.8.6",
  "uptime_seconds": 84213.4,
  "worker_running": true,
  "queue": {
    "total": 42,
    "by_status": { "queued": 3, "downloading": 1, "imported": 38 },
    "active": 1,
    "current": { "title": "Highschool DxD", "current_episode": 4, "total_episodes": 12 }
  },
  "integrations": {
    "sonarr":   { "configured": true,  "reachable": true,  "version": "4.0.15.2941", "app_name": "Sonarr" },
    "radarr":   { "configured": true,  "reachable": true,  "version": "5.26.2.10099", "app_name": "Radarr" },
    "jellyfin": { "configured": true,  "reachable": false, "error": "HTTP 502" }
  },
  "paths": {
    "config":     { "path": "/config", "exists": true, "free_bytes": 31785418752, "total_bytes": 270553174016 },
    "incomplete": { "path": "/media/downloads/aniworld/incomplete", "exists": true, "free_bytes": 4398046511104 },
    "completed":  { "path": "/media/downloads/aniworld/completed",  "exists": true, "free_bytes": 4398046511104 }
  },
  "api_key_required": true,
  "webhooks": { "configured": 1, "pending": 0 }
}
```

`integrations`, `paths`, `api_key_required` and `webhooks` are present only for
an authenticated caller.

`worker_running` is the field to alert on: a false here means the UI is up but
the download queue is dead.

### `GET /api/queue`

```json
{
  "items": [
    {
      "id": 12,
      "title": "Highschool DxD",
      "status": "downloading",
      "current_episode": 3,
      "total_episodes": 12,
      "priority": 0,
      "attempts": 0,
      "max_attempts": null,
      "next_attempt_at": null,
      "last_error": null,
      "media_type": "series",
      "import_status": null,
      "errors": "[]"
    }
  ],
  "ffmpeg_progress": { "percent": 42.1, "time": "00:09:58.12", "speed": "3.4x", "active": true }
}
```

Statuses: `queued`, `downloading`, `verifying`, `completed`, `imported`,
`failed`, `cancelled`, `paused`.

`completed` and `imported` are different answers: `completed` means the file is
downloaded and verified but Sonarr/Radarr did not take it — `import_status`
says why.

### `POST /api/download`

```json
{
  "title": "Highschool DxD",
  "series_url": "https://aniworld.to/anime/stream/highschool-dxd",
  "episodes": ["https://aniworld.to/anime/stream/highschool-dxd/staffel-1/episode-1"],
  "language": "German Dub",
  "provider": "VOE",
  "priority": 0,
  "media_type": "series"
}
```

`priority` (higher runs first, default 0) and `media_type` (`series` | `movie`,
overriding auto-detection) are optional. Returns `{"queue_id": 12}`.

### Queue actions

| Method | Path | Alias | Notes |
|---|---|---|---|
| `POST` | `/api/queue/{id}/retry` | `/api/retry/{id}` | Also clears the attempt counter and backoff |
| `POST` | `/api/queue/{id}/cancel` | `/api/cancel/{id}` | Stops after the current episode |
| `POST` | `/api/queue/{id}/force_cancel` | — | Kills ffmpeg, removes partial files |
| `POST` | `/api/queue/{id}/pause` | `/api/pause/{id}` | Queued items only |
| `POST` | `/api/queue/{id}/resume` | `/api/resume/{id}` | Clears any pending backoff |
| `POST` | `/api/queue/{id}/priority` | — | Body `{"priority": 10}` |
| `POST` | `/api/queue/{id}/move` | — | Body `{"direction": "up"}` |
| `DELETE` | `/api/queue/{id}` | — | Remove from the queue |
| `DELETE` | `/api/queue/completed` | — | Clear finished items |

Retrying re-runs the whole pipeline, including the import — so after fixing a
path mapping or adding the series in Sonarr, a retry imports the existing file
without downloading it again.

### Scans

```bash
curl -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"series_id": 7}' http://localhost:8080/api/sonarr/scan
```

| Path | Body | Effect |
|---|---|---|
| `POST /api/sonarr/scan` | `{"series_id": 7}` or `{}` | `RefreshSeries` + `RescanSeries` |
| `POST /api/radarr/scan` | `{"movie_id": 3}` or `{}` | `RefreshMovie` + `RescanMovie` |
| `POST /api/jellyfin/scan` | `{"path": "/data/TV"}` or `{}` | Targeted or full library scan |

An empty body targets the whole library.

```json
{
  "ok": true,
  "series_id": 7,
  "refresh": { "id": 4711, "status": "completed" },
  "rescan":  { "id": 4712, "status": "completed" }
}
```

`503` when the service is not configured, `502` when it is unreachable.

### `GET /api/logs`

`?lines=200` (max 2000). Admin only.

```json
{ "path": "/config/logs/aniworld.log", "lines": ["2026-07-29 22:16:07 - WARNING - ..."] }
```

---

## Status codes

| Code | Meaning |
|---|---|
| `400` | Bad request body, or an action invalid for that item's state |
| `401` | Authentication required |
| `403` | Admin required |
| `404` | No such queue item |
| `502` | An external service was unreachable or errored |
| `503` | The requested integration is not configured |

---

## Monitoring

```yaml
# Uptime Kuma — HTTP(s) - Keyword
url: http://aniworld:8080/api/status
keyword: '"worker_running": true'
```

```bash
# Plain shell check
curl -fsS localhost:8080/api/status | jq -e '.worker_running == true' >/dev/null \
  || echo "aniworld queue worker is down"
```
