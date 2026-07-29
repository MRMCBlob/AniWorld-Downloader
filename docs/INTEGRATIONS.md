# Sonarr, Radarr & Jellyfin

Reference for the adapters in `src/aniworld/integrations/`, verified against
the upstream specifications rather than older community examples.

| Service | API | Auth | Spec used |
|---|---|---|---|
| Sonarr | v3 (`/api/v3`), served by Sonarr v4 and v5 | `X-Api-Key` header | [`openapi.json`](https://github.com/Sonarr/Sonarr/blob/develop/src/Sonarr.Api.V3/openapi.json) |
| Radarr | v3 (`/api/v3`) | `X-Api-Key` header | [`openapi.json`](https://github.com/Radarr/Radarr/blob/develop/src/Radarr.Api.V3/openapi.json) |
| Jellyfin | stable | `Authorization` header | <https://api.jellyfin.org> |

Both Servarr specs declare two security schemes: the `X-Api-Key` header and an
`apikey` query parameter. The adapters use the header.

---

## Why the import is a two-step flow

The obvious approach — `POST /api/v3/command` with `DownloadedEpisodesScan` —
**does not work on Sonarr v4 or newer.** That command was removed. Most guides
still online predate the change.

The current flow is:

1. `GET /api/v3/manualimport?folder=…` — Sonarr scans the folder and returns
   what it thinks each file is, including any rejections.
2. `POST /api/v3/manualimport` — optional re-parse with our corrections, so
   quality and languages are filled in.
3. `POST /api/v3/command` with `ManualImport` — the actual import.
4. `GET /api/v3/command/{id}` — poll until it reaches a terminal state.

### Two traps worth knowing about

**Never send `seriesId` or `movieId` on the GET.** Both controllers
short-circuit as soon as it is present:

```csharp
// Sonarr.Api.V3/ManualImport/ManualImportController.cs
public List<ManualImportResource> GetMediaFiles(string folder, string downloadId,
                                                int? seriesId, int? seasonNumber,
                                                bool filterExistingFiles = true)
{
    if (seriesId.HasValue)
    {
        return _manualImportService.GetMediaFiles(seriesId.Value, seasonNumber)...;
    }
    return _manualImportService.GetMediaFiles(folder, downloadId, seriesId, filterExistingFiles)...;
}
```

With an id, `folder` is ignored entirely and you get the series' *existing*
files back. It fails silently — the call succeeds, it just describes the wrong
files.

**Refresh and rescan disagree on singular vs. plural.**

| Command | Field | Type |
|---|---|---|
| `RefreshSeries` | `seriesIds` | list |
| `RescanSeries` | `seriesId` | nullable int |
| `RefreshMovie` | `movieIds` | list |
| `RescanMovie` | `movieId` | nullable int |

`RefreshSeriesCommand` also exposes a singular `SeriesId`, but its setter only
appends to `SeriesIds` and loses if the two fields deserialize in the wrong
order. The adapter always sends the plural.

---

## Sonarr

### Endpoints used

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v3/system/status` | Reachability + version for `/api/status` |
| `GET` | `/api/v3/series` | Find a series already in the library |
| `GET` | `/api/v3/series/lookup?term=` | Look up by `tvdb:`, `imdb:`, `tmdb:` or title |
| `POST` | `/api/v3/series` | Auto-add (opt-in) |
| `GET` | `/api/v3/episode?seriesId=&seasonNumber=` | Resolve an episode id ourselves |
| `GET` | `/api/v3/manualimport?folder=` | Step 1 |
| `POST` | `/api/v3/manualimport` | Step 2 (re-parse) |
| `POST` | `/api/v3/command` | Step 3 |
| `GET` | `/api/v3/command/{id}` | Step 4 |
| `GET` | `/api/v3/queue` | Sonarr's own download queue |

### Permissions

An API key from *Settings → General → API Key*. Sonarr has no scopes; the key
is full access. If Sonarr is behind authentication, the key still works —
`/api/v3` accepts it directly.

### Example: import an episode

```http
GET /api/v3/manualimport?folder=%2Fdata%2Fdownloads%2Faniworld%2Fcompleted%2FHighschool%20DxD%2FSeason%2001&filterExistingFiles=true
X-Api-Key: 1a2b3c…
```

```json
[
  {
    "id": 1,
    "path": "/data/downloads/aniworld/completed/Highschool DxD/Season 01/Highschool DxD S01E01.mkv",
    "relativePath": "Highschool DxD S01E01.mkv",
    "folderName": "Season 01",
    "name": "Highschool DxD S01E01.mkv",
    "size": 734003200,
    "seasonNumber": 1,
    "episodes": [{ "id": 4242, "episodeNumber": 1, "seasonNumber": 1 }],
    "quality": { "quality": { "id": 4, "name": "HDTV-720p" }, "revision": { "version": 1 } },
    "languages": [{ "id": 4, "name": "German" }],
    "releaseGroup": null,
    "indexerFlags": 0,
    "rejections": []
  }
]
```

`episodes` is frequently **empty**: our filenames carry no scene release group
and Sonarr's parser gives up on them. The adapter then resolves the id itself
from the season and episode numbers it already knows:

```http
GET /api/v3/episode?seriesId=7&seasonNumber=1
```

Then the import:

```http
POST /api/v3/command
X-Api-Key: 1a2b3c…
Content-Type: application/json

{
  "name": "ManualImport",
  "importMode": "Move",
  "files": [
    {
      "path": "/data/downloads/aniworld/completed/Highschool DxD/Season 01/Highschool DxD S01E01.mkv",
      "folderName": "Season 01",
      "seriesId": 7,
      "episodeIds": [4242],
      "quality": { "quality": { "id": 4, "name": "HDTV-720p" }, "revision": { "version": 1 } },
      "languages": [{ "id": 4, "name": "German" }],
      "releaseGroup": "",
      "indexerFlags": 0
    }
  ]
}
```

```json
{ "id": 4711, "name": "ManualImport", "status": "queued", "trigger": "manual" }
```

```http
GET /api/v3/command/4711
```

```json
{ "id": 4711, "name": "ManualImport", "status": "completed", "duration": "00:00:02.41" }
```

`importMode` maps to `ImportMode` in `NzbDrone.Core.MediaFiles`: `Auto = 0`,
`Move = 1`, `Copy = 2`. Sent capitalised.

### Environment

```ini
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=
SONARR_AUTO_ADD=0
SONARR_ROOT_FOLDER=
SONARR_QUALITY_PROFILE_ID=
```

`SONARR_AUTO_ADD` is off by default: creating library entries in someone's
Sonarr as a side effect of downloading one episode is a surprising thing for a
downloader to do. With it off, an unknown series leaves the file in the
completed folder and the UI reports `series_not_in_sonarr`.

---

## Radarr

Structurally identical, with a movie in place of a series and no episode ids.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v3/system/status` | Reachability + version |
| `GET` | `/api/v3/movie` | Find a movie in the library |
| `GET` | `/api/v3/movie/lookup?term=` | Look up by `tmdb:`, `imdb:` or title |
| `POST` | `/api/v3/movie` | Auto-add (opt-in) |
| `GET` | `/api/v3/manualimport?folder=` | Step 1 |
| `POST` | `/api/v3/manualimport` | Step 2 |
| `POST` | `/api/v3/command` | Step 3 |
| `GET` | `/api/v3/command/{id}` | Step 4 |
| `GET` | `/api/v3/queue` | Radarr's own queue |

```http
POST /api/v3/command
{
  "name": "ManualImport",
  "importMode": "Move",
  "files": [
    {
      "path": "/data/downloads/aniworld/completed/Spirited Away (2001)/Spirited Away (2001).mkv",
      "folderName": "Spirited Away (2001)",
      "movieId": 3,
      "quality": { "quality": { "id": 7, "name": "Bluray-1080p" }, "revision": { "version": 1 } },
      "languages": [{ "id": 8, "name": "Japanese" }],
      "releaseGroup": "",
      "indexerFlags": 0
    }
  ]
}
```

Matching prefers `tmdbId`, then `imdbId`, then title. On a title match the year
breaks ties, and an ambiguous title with no year is **refused** rather than
guessed — importing into the wrong movie is worse than not importing.

```ini
RADARR_URL=http://radarr:7878
RADARR_API_KEY=
RADARR_AUTO_ADD=0
RADARR_ROOT_FOLDER=
RADARR_QUALITY_PROFILE_ID=
```

---

## Jellyfin

Jellyfin puts the key in the standard `Authorization` header rather than a
custom one:

```http
Authorization: MediaBrowser Token="d3f4…"
```

`X-Emby-Token` is sent alongside for older servers, which costs nothing.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/System/Info` | Reachability + version |
| `GET` | `/Library/VirtualFolders` | Find which library owns a path |
| `POST` | `/Items/{itemId}/Refresh` | Scan one library |
| `POST` | `/Library/Refresh` | Scan everything |

A targeted scan is preferred. On a large library over network storage a full
scan runs for many minutes, and triggering one after every single episode is
not viable. The adapter reads `Locations` from `/Library/VirtualFolders`, picks
the library whose location contains the imported path — longest match wins —
and refreshes only that:

```json
[
  {
    "Name": "Shows",
    "ItemId": "f137a2dd21bbc1b99aa5c0f6bf02a805",
    "CollectionType": "tvshows",
    "Locations": ["/data/TV", "/data/Anime"]
  }
]
```

```http
POST /Items/f137a2dd21bbc1b99aa5c0f6bf02a805/Refresh?metadataRefreshMode=Default&imageRefreshMode=Default&replaceAllMetadata=false&replaceAllImages=false
Authorization: MediaBrowser Token="d3f4…"
```

`204 No Content`. When no library matches — usually because Jellyfin mounts the
storage somewhere else — it falls back to `POST /Library/Refresh`, which is
correct, just slower.

The key comes from *Dashboard → Advanced → API Keys*.

```ini
JELLYFIN_URL=http://jellyfin:8096
JELLYFIN_API_KEY=
JELLYFIN_SCAN_ENABLED=1
```

---

## Series or movie?

`src/aniworld/integrations/classify.py` decides, using metadata the site models
already carry. It never fetches anything extra.

| Signal | Result |
|---|---|
| `episode.is_movie` is true | movie |
| `season.are_movies` is true | movie |
| `episode.media_type == "movie"` | movie |
| a season number is present | series |
| nothing conclusive | `ANIWORLD_DEFAULT_MEDIA_TYPE` (default `series`) |

An explicit `media_type` on the queue item overrides all of it.

Reads are defensive: these are lazy properties that may hit the network, return
`None`, or raise on a page that no longer parses. None of that is worth failing
a download over, so every read is guarded and falls through to the next signal.

A series with no season number is assumed to be season 1 — a series file
without a season cannot be imported, and season 1 is both the only sane guess
and what the naming template already renders.

---

## Failure modes

Nothing here can fail a download. Once the file is verified and staged the
download counts as successful; the import is enrichment on top. The reason is
recorded on the queue item and shown in the UI.

| `import_status` | Meaning | Fix |
|---|---|---|
| `not_configured` | No URL/API key for that service | Set them, then retry the item |
| `series_not_in_sonarr` / `movie_not_in_radarr` | Not in the library | Add it, or enable auto-add |
| `file_not_visible_to_sonarr` / `…_radarr` | The *arr cannot see our path | `ANIWORLD_ARR_PATH_MAP` |
| `episode_not_matched` | Series known, episode not | Refresh the series in Sonarr |
| `command_failed` | The *arr rejected the import | Check its own Activity log |
| `integration_error` | Unreachable, or a bad API key | Check `/api/status` |

Retrying a queue item from the UI re-runs the whole pipeline, so once the
underlying problem is fixed the file gets imported without downloading again.

---

## Re-verifying these notes

The specs move. To re-check:

```bash
curl -sL -o sonarr.json https://raw.githubusercontent.com/Sonarr/Sonarr/develop/src/Sonarr.Api.V3/openapi.json
curl -sL -o radarr.json https://raw.githubusercontent.com/Radarr/Radarr/develop/src/Radarr.Api.V3/openapi.json
curl -sL -o jellyfin.json https://api.jellyfin.org/openapi/jellyfin-openapi-stable.json
```

Command bodies are **not** in the OpenAPI documents — `CommandResource` is
declared with `additionalProperties: false` and the polymorphic command fields
are invisible to the generator (Sonarr issue #5416). Read the C# instead:

- `src/NzbDrone.Core/MediaFiles/EpisodeImport/Manual/ManualImportCommand.cs`
- `src/NzbDrone.Core/MediaFiles/EpisodeImport/Manual/ManualImportFile.cs`
