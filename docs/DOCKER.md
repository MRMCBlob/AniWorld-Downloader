# Docker & Homelab Deployment

Running AniWorld-Downloader as a 24/7 service alongside Sonarr, Radarr and
Jellyfin.

- [What the container does](#what-the-container-does)
- [Volumes](#volumes)
- [Quick start](#quick-start)
- [Proxmox](#proxmox)
- [Mounting a Hetzner Storage Box](#mounting-a-hetzner-storage-box)
- [Path mapping between containers](#path-mapping-between-containers)
- [Healthcheck](#healthcheck)
- [Backup](#backup)
- [Troubleshooting](#troubleshooting)

---

## What the container does

1. Downloads an episode into `/media/downloads/aniworld/incomplete`.
2. Verifies it is a playable file (ffprobe: a video stream, non-zero duration).
3. Moves it to `/media/downloads/aniworld/completed`.
4. Works out whether it is a series episode or a movie.
5. Asks Sonarr or Radarr to import it. **They** rename it and move it into
   their own root folder.
6. Optionally triggers a Jellyfin library scan.

Step 5 is the important one for the layout question: this container **never
creates library folders**. It does not need a `TV/` or `Movies/` directory to
exist and will not make one. Where a file finally lands is decided entirely by
the root folders configured in Sonarr and Radarr.

If no integration is configured, steps 5 and 6 are skipped and finished files
simply stay in `completed/`. Nothing breaks; the queue item ends as `completed`
instead of `imported`.

---

## Volumes

| Container path | Host path | Why |
|---|---|---|
| `/media` | `/media/jellyfin/storagebox` | The whole storage mount. Staging lives under `downloads/aniworld/`. |
| `/config` | a **local** Docker volume | Queue database, `.env`, logs, Chromium profile. |

### Why `/config` must not live on the storage box

`/config` holds `aniworld.db`, a SQLite database written continuously by a
service that never stops. SQLite relies on POSIX file locking, and SSHFS and
NFS both implement locking loosely enough that concurrent access can corrupt
the file. The failure is not immediate — it shows up weeks later as
`database disk image is malformed`.

Keep `/config` on local disk and back it up to the box on a schedule. See
[Backup](#backup).

---

## Quick start

```bash
git clone https://github.com/phoenixthrush/AniWorld-Downloader
cd AniWorld-Downloader

cp .env.example .env
$EDITOR .env          # fill in the Sonarr/Radarr/Jellyfin keys

# Staging folders. The library folders are Sonarr's and Radarr's business.
mkdir -p /media/jellyfin/storagebox/downloads/aniworld/incomplete
mkdir -p /media/jellyfin/storagebox/downloads/aniworld/completed

docker compose up -d
docker compose ps      # wait for "healthy"
```

The Web UI is on <http://localhost:8080>.

Verify the integrations came up:

```bash
curl -s -H "X-Api-Key: $ANIWORLD_API_KEY" localhost:8080/api/status | jq .integrations
```

### Same Docker network as the *arr stack

If Sonarr and Radarr run in the same compose project, use service names and
join their network:

```yaml
services:
  aniworld:
    # ...
    networks: [media]
    environment:
      SONARR_URL: http://sonarr:8989
      RADARR_URL: http://radarr:7878
      JELLYFIN_URL: http://jellyfin:8096

networks:
  media:
    external: true
    name: media_default   # `docker network ls` to find the real name
```

Otherwise use the host's LAN IP — `localhost` inside a container is the
container itself, not the host.

---

## Proxmox

### VM (recommended)

Nothing special. Install Docker, mount the storage box, run compose.

### LXC container

An unprivileged LXC needs two features enabled, or neither SSHFS nor Chromium
will start. On the Proxmox host:

```bash
pct set <CTID> --features nesting=1,fuse=1
pct reboot <CTID>
```

- `fuse=1` — SSHFS is a FUSE filesystem.
- `nesting=1` — Docker inside LXC, and Chromium's sandbox namespaces.

If Chromium still fails, check `docker compose logs` for `Failed to move to new
namespace`. A VM avoids this class of problem entirely and is worth it if the
captcha solver is misbehaving.

---

## Mounting a Hetzner Storage Box

Do not put the mount in `/etc/fstab` without `_netdev` — the machine will hang
on boot waiting for a network that is not up yet.

A systemd unit handles reconnects better:

`/etc/systemd/system/media-jellyfin-storagebox.mount`

```ini
[Unit]
Description=Hetzner Storage Box
After=network-online.target
Wants=network-online.target

[Mount]
What=uXXXXXX@uXXXXXX.your-storagebox.de:/home
Where=/media/jellyfin/storagebox
Type=fuse.sshfs
Options=_netdev,allow_other,default_permissions,IdentityFile=/root/.ssh/storagebox,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,port=23

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now media-jellyfin-storagebox.mount
```

The options that matter for an unattended service:

- `reconnect` — reconnect after the SSH session drops, which it will.
- `ServerAliveInterval=15` / `ServerAliveCountMax=3` — notice a dead connection
  in ~45s instead of hanging on a socket forever.
- `allow_other` — the container's user is not the one that mounted the share.
- `_netdev` — do not block boot on this.

### Docker must start after the mount

Otherwise the container starts with an empty directory and writes into the
mountpoint rather than the share:

```bash
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/storagebox.conf <<'EOF'
[Unit]
After=media-jellyfin-storagebox.mount
Requires=media-jellyfin-storagebox.mount
EOF
systemctl daemon-reload
```

---

## Path mapping between containers

Sonarr and Radarr resolve every path we hand them **inside their own
filesystem**. If they mount the same storage somewhere else, an unmapped path
does not exist for them and every import fails with a confusing "file not
found".

Check what path Sonarr sees:

```bash
docker inspect sonarr --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

If Sonarr mounts `/media/jellyfin/storagebox` at `/data` while we mount it at
`/media`, set:

```ini
ANIWORLD_ARR_PATH_MAP=/media:/data
```

Several mappings are comma-separated. Leave it empty when every container
mounts the storage at the same place.

---

## Healthcheck

`docker compose ps` reports `healthy` based on `/api/status`. The check does
not just look for HTTP 200 — it also fails when the download queue worker
thread has died, which is the failure mode where the UI keeps answering while
nothing ever downloads.

```bash
docker compose ps
docker inspect --format '{{json .State.Health}}' aniworld-downloader | jq
```

`restart: unless-stopped` plus the healthcheck means a wedged process gets
restarted, and the queue picks up where it left off: any item left in
`downloading` or `verifying` is handed back to `queued` on startup.

---

## Backup

What actually needs backing up is `/config` — the media is already on the
storage box.

```bash
docker compose exec aniworld \
  python -c "import sqlite3;\
c=sqlite3.connect('/config/aniworld.db');\
c.execute(\"VACUUM INTO '/media/backups/aniworld-\$(date +%F).db'\");\
c.close()"

docker compose cp aniworld:/config/.env /media/jellyfin/storagebox/backups/
```

`VACUUM INTO` (or `.backup`) rather than copying the file: a plain `cp` of a
live SQLite database can capture a torn write and restore as corrupt.

A nightly cron entry on the host:

```cron
0 4 * * * docker compose -f /opt/aniworld/docker-compose.yaml exec -T aniworld python -c "import sqlite3,datetime;p=f\"/media/backups/aniworld-{datetime.date.today()}.db\";c=sqlite3.connect('/config/aniworld.db');c.execute(f\"VACUUM INTO '{p}'\");c.close()"
```

---

## Troubleshooting

**`docker compose ps` shows `unhealthy`**

```bash
docker inspect --format '{{json .State.Health}}' aniworld-downloader | jq -r '.Log[-1].Output'
```

`the download queue worker is not running` means the worker thread died; the
container will be restarted. Check `/config/logs/aniworld.log` for what killed
it.

**Downloads finish but never appear in the library**

The queue item ends as `completed` rather than `imported`, and the UI shows
"Not imported: <reason>".

- `series_not_in_sonarr` — add the series in Sonarr, or set `SONARR_AUTO_ADD=1`
  together with `SONARR_ROOT_FOLDER` and `SONARR_QUALITY_PROFILE_ID`.
- `file_not_visible_to_sonarr` — a path mapping problem. See
  [Path mapping](#path-mapping-between-containers).
- `episode_not_matched` — Sonarr has the series but not that episode; refresh
  the series so it picks up the new season.

**Permission denied writing to `/media`**

The SSHFS mount needs `allow_other`, and the mounting user must have write
access to the share.

**Captcha solving fails**

Chromium needs the namespaces an unprivileged LXC withholds by default. See
[Proxmox](#proxmox). `ANIWORLD_CAPTCHA_DEBUG_LOG=1` puts the browser's console
errors in the log.

**Container stops taking new downloads after a while**

Look for `no progress for Ns, aborting` in the log — the stall watchdog fired.
Tune with `ANIWORLD_STALL_TIMEOUT`; `0` disables it.
