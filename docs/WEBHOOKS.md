# Webhooks

Outgoing notifications for download and import lifecycle events.

```ini
ANIWORLD_WEBHOOK_URLS=https://n8n.example.com/webhook/aniworld
ANIWORLD_WEBHOOK_SECRET=                 # optional, enables signing
```

Several URLs are comma-separated; each receives every event.

---

## Events

| Event | When |
|---|---|
| `download_started` | An episode's download begins |
| `download_completed` | Downloaded, verified and moved into the completed folder |
| `download_failed` | The episode failed (a retry may still follow) |
| `import_completed` | Sonarr or Radarr accepted the file into the library |

`download_completed` and `import_completed` are separate on purpose. A download
can complete without ever being imported — Sonarr may not know the series, or
may not be able to see the path — and a workflow that only cares about "the
file is watchable in Jellyfin" wants `import_completed`.

## Payload

```json
{
  "event": "download_completed",
  "type": "series",
  "title": "Example",
  "season": 1,
  "episode": 1,
  "path": "/media/downloads/aniworld/completed/Example/Season 01/Example S01E01.mkv",
  "queue_id": 12,
  "timestamp": 1785000000.123
}
```

Fields that are empty are **omitted**, not sent as `null` — a movie carries no
`season` or `episode`. `download_failed` adds `error`.

`path` is the file as *this container* sees it. After an import Sonarr or
Radarr has moved it into their own root folder, so it will no longer be there.

---

## Signing

With `ANIWORLD_WEBHOOK_SECRET` set, every delivery carries:

```http
X-AniWorld-Signature: sha256=<hex hmac>
Content-Type: application/json
User-Agent: AniWorld-Downloader
```

The HMAC-SHA256 is over the **raw request body**, not a re-serialised version
of it. Verify against the bytes you received — re-encoding the parsed JSON will
not reproduce the digest:

```python
import hashlib
import hmac

def verify(raw_body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header or "")
```

```javascript
const crypto = require("crypto");

function verify(rawBody, header, secret) {
  const expected =
    "sha256=" + crypto.createHmac("sha256", secret).update(rawBody).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(header || ""));
}
```

Use a constant-time comparison; `==` on a signature leaks timing.

---

## Delivery

Deliveries go through a persistent outbox in the queue database rather than
being sent from the thread that produced the event. Two reasons, both about not
letting a receiver hurt the downloader:

- A slow or unreachable endpoint cannot block a download.
- An event is not lost if the container restarts between "download finished"
  and "webhook delivered".

Any 2xx counts as delivered. Anything else — including a connection failure —
is retried with exponential backoff (10s, 20s, 40s … capped at 1h) for up to 8
attempts, after which the row is closed out with the last error kept for
inspection. Delivered rows are purged after 7 days.

Pending deliveries show up in `/api/status`:

```json
{ "webhooks": { "configured": 1, "pending": 3 } }
```

A `pending` count that keeps climbing means the receiver is not accepting
deliveries. `/config/logs/aniworld.log` has the reason.

---

## Example receiver

```python
import hashlib
import hmac
import os

from flask import Flask, abort, request

app = Flask(__name__)
SECRET = os.environ["ANIWORLD_WEBHOOK_SECRET"]


@app.post("/webhook/aniworld")
def aniworld():
    expected = "sha256=" + hmac.new(
        SECRET.encode(), request.get_data(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("X-AniWorld-Signature", "")):
        abort(401)

    event = request.get_json()
    if event["event"] == "import_completed":
        print(f"Now in the library: {event['title']}")
    return "", 204
```

Quick check without writing anything:

```bash
# ANIWORLD_WEBHOOK_URLS=http://<host>:9000/
while true; do printf 'HTTP/1.1 204 No Content\r\n\r\n' | nc -l -p 9000 -q 1; done
```
