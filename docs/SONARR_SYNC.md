# Sonarr Wanted -> AniWorld sync

The sync reads the same data as Sonarr's **Wanted -> Missing** page through
`GET /api/v3/wanted/missing`. It keeps only monitored, already-aired episodes,
finds the matching episode on AniWorld, and places it directly in the series'
real season directory. There is no completed/download hand-off directory for
these jobs. When the queue item finishes, AniWorld Downloader runs
`RescanSeries` so Sonarr indexes the new files.

## 1. Mount the same library in both containers

AniWorld Downloader needs write access to the Shows library. The Dokploy
Compose file already mounts the whole storage box as `/media`, so a Sonarr
series at `/media/jellyfin/storagebox/Shows/Frieren` maps to
`/media/Shows/Frieren` in AniWorld Downloader.

For a plain Compose install, add an equivalent bind mount:

```yaml
services:
  aniworld:
    volumes:
      - /media/jellyfin/storagebox:/media
```

## 2. Environment

```ini
TZ=Europe/Berlin

SONARR_URL=http://192.168.178.42:8989
SONARR_API_KEY=replace-me

# AniWorld path first, Sonarr path second. The sync applies it in reverse when
# it reads series/file paths from Sonarr.
ANIWORLD_ARR_PATH_MAP=/media:/media/jellyfin/storagebox

# Direct API targets outside this tree are rejected.
ANIWORLD_DIRECT_DOWNLOAD_ROOTS=/media/Shows

# The scheduler runs inside AniWorld Downloader. This is local container time.
ANIWORLD_SONARR_SYNC_ENABLED=1
ANIWORLD_SONARR_SYNC_CRON=0 0 * * *

ANIWORLD_URL=http://127.0.0.1:8080
# The direct-download endpoint requires an admin/environment API key.
ANIWORLD_API_KEY=replace-me-too
SONARR_ANIWORLD_MAP_FILE=/config/sonarr-aniworld-map.json
SONARR_SYNC_LANGUAGE=German Dub
SONARR_SYNC_PROVIDER=VOE
SONARR_SYNC_PRIORITY=10
SONARR_SYNC_MAX_EPISODES=100
SONARR_SYNC_INCLUDE_SPECIALS=0
SONARR_SYNC_AUTO_MATCH=1
```

An exact title/alternate-title match is selected automatically. The sync never
guesses a fuzzy match, because downloading an identically named but different
series directly into a library would be difficult to undo.

For names that differ, create `/config/sonarr-aniworld-map.json`:

```json
{
  "sonarr:7": "https://aniworld.to/anime/stream/frieren",
  "tvdb:252083": "https://aniworld.to/anime/stream/highschool-dxd",
  "A Sonarr Title": "https://aniworld.to/anime/stream/the-aniworld-slug"
}
```

`sonarr:<id>` is the least ambiguous key. A missing mapping is reported in the
app log with the exact key to add.

## 3. Test before enabling downloads

The command is a dry-run unless `--apply` is present:

```bash
docker exec aniworld-downloader aniworld-sonarr-sync
docker exec aniworld-downloader aniworld-sonarr-sync --series-id 7
docker exec aniworld-downloader aniworld-sonarr-sync --apply --series-id 7
```

The scheduled run uses `--apply` behavior. On a fresh install it starts at the
next configured time after the container starts. Active Sonarr episode IDs
already in the AniWorld queue are excluded, so overlapping invocations do not
enqueue the same episode twice.

## 4. Optional Sonarr Connect hook

Sonarr's **Settings -> Connect -> Custom Script** page has event triggers but
no clock/cron trigger. The midnight run therefore belongs to the scheduler
above. The Connect hook is useful in addition: with **On Series Add** selected,
a newly added series is checked immediately instead of waiting until midnight.

Copy [`scripts/sonarr-connect-aniworld.sh`](../scripts/sonarr-connect-aniworld.sh)
to a path mounted inside the Sonarr container and make it executable:

```bash
chmod 755 /path/on-host/sonarr-connect-aniworld.sh
```

Example Sonarr Compose settings:

```yaml
services:
  sonarr:
    environment:
      ANIWORLD_URL: http://aniworld:8080
      # Must be the admin/environment key used by AniWorld Downloader.
      ANIWORLD_API_KEY: replace-me-too
    volumes:
      - /path/on-host/sonarr-connect-aniworld.sh:/config/scripts/sonarr-connect-aniworld.sh:ro
```

In the screen shown in Sonarr:

1. Name: `AniWorld Downloader`
2. Notification Triggers: only `On Series Add`
3. Path: `/config/scripts/sonarr-connect-aniworld.sh`
4. Press **Test**, then **Save**

Do not select **On Grab**. At that moment Sonarr has chosen its own release but
does not have a file yet, so the episode still looks missing and a second copy
could be requested from AniWorld. The supplied hook ignores every event except
`Test` and `SeriesAdd` as an additional guard.

## Directory selection

For a season that already contains files, the sync uses the existing file
directory reported by Sonarr. This preserves custom names such as `Staffel 01`.
For an empty/new season it reads Sonarr's `seasonFolderFormat` from
`/api/v3/config/naming`; `Season {season:00}` becomes `Season 02`. When season
folders are disabled for the series, it writes directly to the series folder.

Every dynamic destination is canonicalized and checked against
`ANIWORLD_DIRECT_DOWNLOAD_ROOTS` before it enters the queue. A direct job is
verified after download and never passed through `ANIWORLD_COMPLETED_PATH`.
