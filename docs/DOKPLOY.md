# Deploying on Dokploy

[Dokploy](https://dokploy.com) runs this as a Compose service. The short
version: point it at `docker-compose.dokploy.yaml`, let it build from your
branch, and put the configuration in the Environment tab. No registry, no
GitHub Actions run, no image tag to keep in sync.

- [Before you start](#before-you-start)
- [1. Create the Dokploy service](#1-create-the-dokploy-service)
- [2. Connect it to Sonarr, Radarr and Jellyfin](#2-connect-it-to-sonarr-radarr-and-jellyfin)
- [3. Check the path mapping](#3-check-the-path-mapping)
- [4. Verify](#4-verify)
- [Updating](#updating)
- [Alternative: a prebuilt image from a registry](#alternative-a-prebuilt-image-from-a-registry)
- [Troubleshooting](#troubleshooting)

---

## Before you start

**The storage mount has to exist on the Dokploy host.** `docker-compose.dokploy.yaml`
bind-mounts `/media/jellyfin/storagebox`. If Dokploy runs on a different machine
than the SSHFS mount, either mount the box there too or add the media host as a
Dokploy *Remote Server* and deploy to it.

```bash
findmnt /media/jellyfin/storagebox     # should print the fuse.sshfs mount
touch /media/jellyfin/storagebox/.aniworld-storage
```

The second command creates a mount sentinel. Set
`ANIWORLD_STORAGE_SENTINEL=/media/.aniworld-storage` in Dokploy so a missing
mount causes startup and healthchecks to fail instead of letting downloads land
in an empty local mountpoint.

**Use the `Compose` service type, not `Stack`.** Stack is Docker Swarm, which
ignores `restart: unless-stopped` and cannot build images.

**Do not enable "Isolated Deployment"** for this service. It gives the app its
own network, which is exactly what stops it reaching Sonarr and Radarr.

**Building on the host is the default**, and it is the simpler path: the deploy
always matches the branch Dokploy cloned, and there is no registry, no image
visibility setting and no tag to keep in sync.

The build is not cheap — the Dockerfile downloads Chromium, UPX-compresses it
and squashes the whole root filesystem — so budget a few GB of disk and 15-30
minutes the first time. Layer caching makes later deploys much faster. If your
Dokploy host is too small for that, see
[the registry alternative](#alternative-a-prebuilt-image-from-a-registry).

---

## 1. Create the Dokploy service

1. Project → **Create Service** → **Compose**
2. **Provider**: GitHub → your fork → the branch you want to deploy
3. **Compose Path**: `docker-compose.dokploy.yaml`
4. **Environment** tab — Dokploy writes this to a `.env` file next to the
   compose file, which is what `env_file: .env` reads:

   ```ini
   SONARR_URL=http://sonarr:8989
   SONARR_API_KEY=
   RADARR_URL=http://radarr:7878
   RADARR_API_KEY=
   JELLYFIN_URL=http://jellyfin:8096
   JELLYFIN_API_KEY=

   # See step 3 — leave empty until you have checked.
   ANIWORLD_ARR_PATH_MAP=

   # For scripts and monitoring: openssl rand -hex 32
   ANIWORLD_API_KEY=

   # Recommended for an SSHFS/NFS-backed /media mount.
   ANIWORLD_STORAGE_SENTINEL=/media/.aniworld-storage
   ```

   `.env.example` in the repository root lists everything else. Nothing is
   required to get a first deploy up — the integrations are all optional.

5. **Deploy**, then use **Preview Compose** to confirm what Dokploy actually
   runs — it shows the file with its own additions merged in.

The Web UI is on port `8080` of the host.

> Port `8080` is bound directly for LAN access. If you later add a domain in
> Dokploy's Domains tab, Traefik makes the UI reachable from the internet —
> set `ANIWORLD_WEB_AUTH=1` with `ANIWORLD_WEB_ADMIN_USER` and
> `ANIWORLD_WEB_ADMIN_PASS` before you do. `ANIWORLD_API_KEY` only covers
> machine access, not the browser session.

---

## 2. Connect it to Sonarr, Radarr and Jellyfin

Dokploy isolates compose projects from each other, so `http://sonarr:8989` does
not resolve out of the box. `docker-compose.dokploy.yaml` already joins
`dokploy-network`; your *arr stack has to join it too:

```yaml
services:
  sonarr:
    networks:
      - dokploy-network
  radarr:
    networks:
      - dokploy-network
  jellyfin:
    networks:
      - dokploy-network

networks:
  dokploy-network:
    external: true
```

Compose registers the **service name** as a network alias, so `sonarr` resolves
as long as the service in that stack is called `sonarr`.

Check it from inside the container:

```bash
docker exec -it "$(docker ps -qf name=aniworld)" python - <<'EOF'
import niquests
print(niquests.get("http://sonarr:8989/api/v3/system/status",
                   headers={"X-Api-Key": "YOUR_KEY"}, timeout=10).status_code)
EOF
```

`200` means you are done. Anything else — use the host's LAN IP instead, which
always works and costs nothing:

```ini
SONARR_URL=http://192.168.1.10:8989
```

---

## 3. Check the path mapping

**This is the single most common reason imports fail.** Sonarr and Radarr
resolve every path we hand them inside *their own* filesystem. If they mount the
storage somewhere else, our path does not exist for them and the import fails
with a misleading "file not found".

```bash
docker inspect "$(docker ps -qf name=sonarr)" \
  --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

| Sonarr mounts `/media/jellyfin/storagebox` at | Set |
|---|---|
| `/media` (same as us) | `ANIWORLD_ARR_PATH_MAP=` (empty) |
| `/data` | `ANIWORLD_ARR_PATH_MAP=/media:/data` |
| something else | `ANIWORLD_ARR_PATH_MAP=/media:/whatever` |

Several mappings are comma-separated. See
[INTEGRATIONS.md](INTEGRATIONS.md#why-the-import-is-a-two-step-flow) for the
detail.

---

## 4. Verify

```bash
# Healthy means the queue worker is alive, not just that HTTP answers.
docker ps --filter name=aniworld --format '{{.Names}}\t{{.Status}}'

KEY=<your ANIWORLD_API_KEY>

# If "integrations" is missing you are running an older build, not this branch.
curl -s -H "X-Api-Key: $KEY" localhost:8080/api/status | jq '.version, .integrations'

# Are the mounts visible and is there room?
curl -s -H "X-Api-Key: $KEY" localhost:8080/api/status | jq '.paths'

# End-to-end check of the Sonarr connection.
curl -s -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{}' localhost:8080/api/sonarr/scan | jq
```

Then download one episode from the Web UI with `docker compose logs -f` open.
Expected: the file appears under
`/media/jellyfin/storagebox/downloads/aniworld/incomplete/…`, moves to
`…/completed/…`, Sonarr's Activity shows a `ManualImport` command, and the queue
item goes `queued → running → completed` with `import_status: imported`.

If it stops at `completed`, the UI shows *"Not imported: &lt;reason&gt;"* — the
reasons and their fixes are in
[INTEGRATIONS.md](INTEGRATIONS.md#failure-modes).

**Restart test.** Hit *Redeploy* in Dokploy mid-download. The item should come
back as `queued` and carry on — that is what the queue hardening is for.

---

## Updating

Push to the branch Dokploy tracks and hit **Redeploy** (or let the auto-deploy
webhook do it). Dokploy re-clones the repository and rebuilds.

`pull_policy: build` in `docker-compose.dokploy.yaml` makes this explicit: a
redeploy rebuilds the local image even if Dokploy invokes Compose without
`--build`. You can confirm the resulting image timestamp with:

```bash
docker image inspect aniworld-downloader:local --format '{{.Created}}'
```

If that timestamp is older than your last deploy, the host is using an outdated
Compose implementation that does not honor the policy. Update Docker Compose,
or set a custom deploy command that includes `--build`.

The `/config` volume survives redeploys, so the queue, settings and login stay
put. Back it up as described in [DOCKER.md](DOCKER.md#backup).

---

## Alternative: a prebuilt image from a registry

Worth it if the Dokploy host is too small to build, or you want the exact same
image on several machines. Delete the `build:` block **and the
`pull_policy: build` line** from `docker-compose.dokploy.yaml`, then set
`ANIWORLD_IMAGE` in the Environment tab.

### Build it in GitHub Actions

1. GitHub → **Actions** → **Build Docker Image** → **Run workflow**
2. Branch: the one you want, `push`: **true**, `platforms`: `linux/amd64`

> Leave `arm64` off unless you deploy to ARM. That leg builds under QEMU
> emulation, and for an image that UPX-compresses Chromium it takes hours and
> can hit the 6-hour job limit.

**The tag is not `latest`.** The workflow uses `docker/metadata-action` with its
default rules (`type=ref,event=branch|tag|pr`); `latest` only appears for tag
and semver builds. A manual run from a branch publishes:

```
ghcr.io/<owner>/aniworld-downloader:<branch-name-with-slashes-as-dashes>
```

So `claude/aniworld-docker-homelab-soy5tl` becomes the tag
`claude-aniworld-docker-homelab-soy5tl`, with the owner lowercased. The exact
name is printed in the workflow's *Extract Docker metadata* step.

Publishing a **release** from the default branch does produce a real `:latest`.

### If the workflow will not run

On a **fork, GitHub disables all inherited workflows.** The Actions tab shows a
banner — *"Workflows aren't being run on this forked repository"* — and until
you click **"I understand my workflows, go ahead and enable them"** the
*Build Docker Image* entry has no **Run workflow** button, and the API returns
404 for it. Workflows you add yourself afterwards are unaffected, which is why
`Tests` can be running while this one appears to be missing entirely.

`workflow_dispatch` also needs the workflow file to exist on the **default
branch**, and the inputs form is read from that copy — a new input added only
on a feature branch will not show up until it is merged.

### Make the image pullable

Packages published from a fork are **private** by default.

**Either** make it public — GitHub → your profile → **Packages** →
`aniworld-downloader` → *Package settings* → *Change visibility* → Public.

**Or** keep it private and give Dokploy credentials — Dokploy → **Settings** →
**Registry**:

| Field | Value |
|---|---|
| Registry URL | `ghcr.io` |
| Username | your GitHub username |
| Password | a PAT with the `read:packages` scope |

---

## Troubleshooting

**The build runs out of disk**

The `FROM scratch` stage copies the entire root filesystem to squash the layers,
so peak usage is roughly double the final image. Free some space
(`docker system prune -a`) or switch to
[the registry alternative](#alternative-a-prebuilt-image-from-a-registry).

**A redeploy did not pick up my changes**

Compose reuses an existing local image. See [Updating](#updating) for how to
confirm it rebuilt and how to force it.

**`denied` or `manifest unknown` when pulling**

Only applies to the registry path: the package is still private, or the tag is
wrong. See
[the registry alternative](#alternative-a-prebuilt-image-from-a-registry) and
check the tag under **Packages** — remember it is the branch name, not
`latest`.

**Container is `unhealthy`**

```bash
docker inspect --format '{{json .State.Health}}' "$(docker ps -qf name=aniworld)" \
  | jq -r '.Log[-1].Output'
```

`the download queue worker is not running` means the worker thread died and the
entrypoint will exit after three consecutive failures so `restart:
unless-stopped` can recover it. `/config/logs/aniworld.log` has the reason.

`storage sentinel is missing` means `/media` no longer points at the expected
SSHFS/NFS filesystem. Restore the mount; the restart loop will recover without
writing downloads to the host's underlying mountpoint.

**`Connection refused` to Sonarr**

Both stacks need to be on `dokploy-network`, and *Isolated Deployment* has to be
off. See [step 2](#2-connect-it-to-sonarr-radarr-and-jellyfin), or just use the
host's LAN IP.

**Permission denied on `/media`**

The SSHFS mount needs `allow_other`, and the mounting user needs write access to
the share. See [DOCKER.md](DOCKER.md#mounting-a-hetzner-storage-box).

**Logs or metrics are empty in the Dokploy UI**

Something reintroduced `container_name` into the compose file. Dokploy needs to
name containers itself.
