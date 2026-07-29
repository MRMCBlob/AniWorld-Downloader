import os
import random
import sqlite3
import time

from werkzeug.security import check_password_hash, generate_password_hash

from ..config import ANIWORLD_CONFIG_DIR
from ..logger import get_logger

logger = get_logger(__name__)

DB_PATH = ANIWORLD_CONFIG_DIR / "aniworld.db"

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
    auth_method TEXT NOT NULL DEFAULT 'local',
    sso_subject TEXT,
    sso_issuer TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_SSO_INDEX = """\
CREATE UNIQUE INDEX IF NOT EXISTS idx_sso_identity
ON users (sso_issuer, sso_subject)
WHERE sso_issuer IS NOT NULL AND sso_subject IS NOT NULL;
"""


def _retry_db(func, *args, **kwargs):
    delay = 0.1
    for attempt in range(15):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if (
                "locked" in str(e).lower() or "busy" in str(e).lower()
            ) and attempt < 14:
                time.sleep(delay + random.uniform(0, 0.05))
                delay = min(delay * 2, 5.0)
            else:
                raise


class RetryingCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        return _retry_db(super().execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return _retry_db(super().executemany, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return _retry_db(super().executescript, *args, **kwargs)


class RetryingConnection(sqlite3.Connection):
    def cursor(self, factory=RetryingCursor):
        return super().cursor(factory=factory)

    def execute(self, *args, **kwargs):
        cur = self.cursor()
        return cur.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        cur = self.cursor()
        return cur.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        cur = self.cursor()
        return cur.executescript(*args, **kwargs)

    def commit(self):
        return _retry_db(super().commit)


def get_db():
    ANIWORLD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0, factory=RetryingConnection)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=60000;")
    except Exception:
        pass
    return conn


def _migrate_db(conn):
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    columns = {r["name"] for r in rows}

    if "auth_method" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'local'"
        )
    if "sso_subject" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN sso_subject TEXT")
    if "sso_issuer" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN sso_issuer TEXT")

    conn.execute(_CREATE_SSO_INDEX)
    conn.commit()


def init_db():
    conn = get_db()
    try:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_SSO_INDEX)
        conn.commit()
        _migrate_db(conn)
    finally:
        conn.close()

    if not has_any_admin():
        env_user = os.environ.get("ANIWORLD_WEB_ADMIN_USER", "").strip()
        env_pass = os.environ.get("ANIWORLD_WEB_ADMIN_PASS", "").strip()
        if env_user and env_pass:
            create_user(env_user, env_pass, role="admin")
            logger.info("Auto-created admin user '%s' from environment", env_user)


def has_any_admin():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
        ).fetchone()
        return row["cnt"] > 0
    finally:
        conn.close()


def create_user(username, password, role="user"):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), role),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def verify_user(username, password):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, role, auth_method FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            return None, "Invalid username or password."
        if row["auth_method"] != "local":
            return None, "This account uses SSO. Please use the SSO login button."
        if check_password_hash(row["password_hash"], password):
            return {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
            }, None
        return None, "Invalid username or password."
    finally:
        conn.close()


def find_or_create_sso_user(
    issuer, subject, username, admin_username=None, admin_subject=None
):
    def _should_be_admin():
        if admin_subject and subject == admin_subject:
            return True
        if admin_username and username == admin_username:
            return True
        return False

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE sso_issuer = ? AND sso_subject = ?",
            (issuer, subject),
        ).fetchone()

        if row:
            user = {"id": row["id"], "username": row["username"], "role": row["role"]}
            if _should_be_admin() and row["role"] != "admin":
                conn.execute(
                    "UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],)
                )
                conn.commit()
                user["role"] = "admin"
            return user

        # Check for username conflict with local users
        existing = conn.execute(
            "SELECT id, auth_method FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if existing:
            raise ValueError(
                f"Username '{username}' is already taken by a local account."
            )

        role = "admin" if _should_be_admin() else "user"
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, auth_method, sso_subject, sso_issuer) "
            "VALUES (?, ?, ?, 'oidc', ?, ?)",
            (username, "", role, subject, issuer),
        )
        conn.commit()
        return {"id": cur.lastrowid, "username": username, "role": role}
    finally:
        conn.close()


def list_users():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, username, role, auth_method, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_user(user_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return False, "User not found"
        if row["role"] == "admin":
            cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
            ).fetchone()["cnt"]
            if cnt <= 1:
                return False, "Cannot delete the last admin"
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def update_user_role(user_id, new_role):
    if new_role not in ("admin", "user"):
        return False, "Invalid role"
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return False, "User not found"
        if row["role"] == "admin" and new_role != "admin":
            cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
            ).fetchone()["cnt"]
            if cnt <= 1:
                return False, "Cannot demote the last admin"
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
        conn.commit()
        return True, None
    finally:
        conn.close()


# ===== Download Queue =====

#: Every status a queue item can hold, mirrored by the CHECK constraint below.
#:
#: 'downloading' used to be called 'running'. The rename came with the extra
#: states around importing, and is applied by the schema migration.
QUEUE_STATUSES = (
    "queued",
    "downloading",
    "verifying",
    "completed",
    "imported",
    "failed",
    "cancelled",
    "paused",
)

#: Statuses meaning "the worker is on this item right now".
ACTIVE_STATUSES = ("downloading", "verifying")

#: Statuses meaning "this item is done, one way or another".
TERMINAL_STATUSES = ("completed", "imported", "failed", "cancelled")

#: Statuses that occupy a slot: either running, or waiting to run.
PENDING_STATUSES = ("queued", "downloading", "verifying")

_STATUS_CHECK = ",".join(f"'{status}'" for status in QUEUE_STATUSES)

_QUEUE_COLUMNS = """\
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    series_url TEXT NOT NULL,
    episodes TEXT NOT NULL,
    total_episodes INTEGER NOT NULL,
    language TEXT NOT NULL,
    provider TEXT NOT NULL,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ({statuses})),
    current_episode INTEGER NOT NULL DEFAULT 0,
    current_url TEXT,
    errors TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    custom_path_id INTEGER,
    source TEXT NOT NULL DEFAULT 'manual',
    captcha_url TEXT,
    discord_user_id TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER,
    next_attempt_at TEXT,
    last_error TEXT,
    media_type TEXT,
    import_status TEXT
""".format(statuses=_STATUS_CHECK)

_CREATE_QUEUE_TABLE = f"CREATE TABLE IF NOT EXISTS download_queue (\n{_QUEUE_COLUMNS}\n);"

#: Columns of the current schema, in order, for the rebuild migration.
_QUEUE_COLUMN_NAMES = tuple(
    line.strip().split()[0]
    for line in _QUEUE_COLUMNS.splitlines()
    if line.strip() and not line.strip().startswith("CHECK(")
)

_ADDED_COLUMNS = (
    # (name, DDL) — applied to databases that predate the column but already
    # carry the current CHECK constraint.
    ("position", "INTEGER NOT NULL DEFAULT 0"),
    ("custom_path_id", "INTEGER"),
    ("source", "TEXT NOT NULL DEFAULT 'manual'"),
    ("captcha_url", "TEXT"),
    ("discord_user_id", "TEXT"),
    ("priority", "INTEGER NOT NULL DEFAULT 0"),
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("max_attempts", "INTEGER"),
    ("next_attempt_at", "TEXT"),
    ("last_error", "TEXT"),
    ("media_type", "TEXT"),
    ("import_status", "TEXT"),
)


def _queue_column_names(conn):
    return [row["name"] for row in conn.execute("PRAGMA table_info(download_queue)")]


def _rebuild_queue_table(conn):
    """Recreate download_queue with the current CHECK constraint.

    SQLite cannot alter a CHECK constraint in place, and the constraint has to
    change: it hard-coded the old status list, so a plain ALTER could add the
    new columns but every write of 'verifying' or 'imported' would fail. The
    table is therefore rebuilt with the documented rename-copy-drop dance.

    Existing rows are carried over unchanged apart from 'running', which is
    rewritten to 'downloading' during the copy — inserting it as-is would trip
    the new constraint.
    """
    existing = _queue_column_names(conn)
    if not existing:
        return

    shared = [name for name in _QUEUE_COLUMN_NAMES if name in existing]
    select_terms = [
        "CASE WHEN status = 'running' THEN 'downloading' ELSE status END"
        if name == "status"
        else name
        for name in shared
    ]
    columns = ", ".join(shared)

    conn.execute("ALTER TABLE download_queue RENAME TO download_queue_old")
    conn.execute(f"CREATE TABLE download_queue (\n{_QUEUE_COLUMNS}\n);")
    conn.execute(
        f"INSERT INTO download_queue ({columns}) "
        f"SELECT {', '.join(select_terms)} FROM download_queue_old"
    )
    conn.execute("DROP TABLE download_queue_old")


def init_queue_db():
    ANIWORLD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_QUEUE_TABLE)

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'download_queue'"
        ).fetchone()
        schema = (row["sql"] if row else "") or ""

        if "'downloading'" not in schema:
            # Pre-rename database: the CHECK constraint still lists 'running'
            # and knows nothing about verifying/imported/paused.
            _rebuild_queue_table(conn)
        else:
            existing = set(_queue_column_names(conn))
            for name, ddl in _ADDED_COLUMNS:
                if name in existing:
                    continue
                conn.execute(f"ALTER TABLE download_queue ADD COLUMN {name} {ddl}")
                if name == "position":
                    conn.execute(
                        "UPDATE download_queue SET position = id WHERE position = 0"
                    )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_queue_status_ready "
            "ON download_queue (status, priority DESC, position, id)"
        )
        conn.commit()
    finally:
        conn.close()


def add_to_queue(
    title,
    series_url,
    episodes,
    language,
    provider,
    username=None,
    custom_path_id=None,
    source="manual",
    discord_user_id=None,
    priority=0,
    media_type=None,
    max_attempts=None,
):
    import json

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO download_queue (title, series_url, episodes, total_episodes, "
            "language, provider, username, custom_path_id, source, discord_user_id, "
            "priority, media_type, max_attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                series_url,
                json.dumps(episodes),
                len(episodes),
                language,
                provider,
                username,
                custom_path_id,
                source,
                discord_user_id,
                int(priority or 0),
                media_type,
                max_attempts,
            ),
        )
        row_id = cur.lastrowid
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE id = ?", (row_id, row_id)
        )
        conn.commit()
        return row_id
    finally:
        conn.close()


def is_series_queued_or_running(series_url, language=None):
    """Check if a series already has a queued or running item in the download queue."""
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        query = (
            "SELECT COUNT(*) AS cnt FROM download_queue "
            f"WHERE series_url = ? AND status IN ({placeholders})"
        )
        params = [series_url, *PENDING_STATUSES]
        if language:
            query += " AND language = ?"
            params.append(language)

        row = conn.execute(query, tuple(params)).fetchone()
        return row["cnt"] > 0
    finally:
        conn.close()


def get_queue():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM download_queue "
            "ORDER BY priority DESC, position ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_next_queued():
    """The next item the worker should pick up, or None.

    Priority beats manual ordering; ``position`` still decides within a
    priority band. Items sitting out a retry backoff are skipped until their
    ``next_attempt_at`` has passed, so one repeatedly failing download cannot
    monopolise the worker by being retried in a tight loop.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM download_queue WHERE status = 'queued' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) "
            "ORDER BY priority DESC, position ASC, id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_queue_priority(queue_id, priority):
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE download_queue SET priority = ? WHERE id = ?",
            (int(priority), queue_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def pause_queue_item(queue_id):
    """Hold a queued item back without losing its place or its retry counter."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE download_queue SET status = 'paused' WHERE id = ? AND status = 'queued'",
            (queue_id,),
        )
        conn.commit()
        if cur.rowcount > 0:
            return True, None
        return False, "Can only pause queued items"
    finally:
        conn.close()


def resume_queue_item(queue_id):
    """Release a paused item, clearing any leftover backoff so it runs at once."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE download_queue SET status = 'queued', next_attempt_at = NULL "
            "WHERE id = ? AND status = 'paused'",
            (queue_id,),
        )
        conn.commit()
        if cur.rowcount > 0:
            return True, None
        return False, "Item is not paused"
    finally:
        conn.close()


def schedule_queue_retry(queue_id, delay_seconds, error=None):
    """Put a failed item back in the queue after a delay.

    Returns True when a retry was scheduled, False when the item has used up
    its attempts and should be marked failed by the caller.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM download_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        if not row:
            return False

        attempts = (row["attempts"] or 0) + 1
        max_attempts = row["max_attempts"] or default_max_attempts()
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE download_queue SET attempts = ?, last_error = ? WHERE id = ?",
                (attempts, _truncate_error(error), queue_id),
            )
            conn.commit()
            return False

        conn.execute(
            "UPDATE download_queue SET status = 'queued', attempts = ?, "
            "last_error = ?, current_url = NULL, captcha_url = NULL, "
            "completed_at = NULL, "
            "next_attempt_at = datetime('now', ?) WHERE id = ?",
            (
                attempts,
                _truncate_error(error),
                f"+{int(delay_seconds)} seconds",
                queue_id,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def default_max_attempts():
    try:
        return max(1, int(os.getenv("ANIWORLD_MAX_RETRIES", "") or 3))
    except ValueError:
        return 3


def retry_backoff_seconds(attempts):
    """Exponential backoff for retry number ``attempts``, capped at an hour."""
    try:
        base = max(1, int(os.getenv("ANIWORLD_RETRY_BACKOFF_BASE", "") or 60))
    except ValueError:
        base = 60
    return min(base * (2 ** max(0, attempts - 1)), 3600)


def _truncate_error(error):
    if not error:
        return None
    return " ".join(str(error).split())[:500]


def set_queue_media_type(queue_id, media_type):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET media_type = ? WHERE id = ?",
            (media_type, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_queue_import_status(queue_id, import_status):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET import_status = ? WHERE id = ?",
            (import_status, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def move_queue_item(queue_id, direction):
    """Swap position of a queued item with its neighbor. direction: 'up' or 'down'."""
    conn = get_db()
    try:
        item = conn.execute(
            "SELECT id, position FROM download_queue WHERE id = ? AND status = 'queued'",
            (queue_id,),
        ).fetchone()
        if not item:
            return False, "Item not found or not queued"

        if direction == "up":
            neighbor = conn.execute(
                "SELECT id, position FROM download_queue "
                "WHERE status = 'queued' AND position < ? "
                "ORDER BY position DESC LIMIT 1",
                (item["position"],),
            ).fetchone()
        else:
            neighbor = conn.execute(
                "SELECT id, position FROM download_queue "
                "WHERE status = 'queued' AND position > ? "
                "ORDER BY position ASC LIMIT 1",
                (item["position"],),
            ).fetchone()

        if not neighbor:
            return False, "Already at the edge"

        # Swap positions
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE id = ?",
            (neighbor["position"], item["id"]),
        )
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE id = ?",
            (item["position"], neighbor["id"]),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def get_running(limit=1):
    """Items the worker currently holds. Post-processing counts as running."""
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = conn.execute(
            f"SELECT * FROM download_queue WHERE status IN ({placeholders}) "
            "ORDER BY id ASC LIMIT ?",
            (*ACTIVE_STATUSES, limit),
        ).fetchall()
        if limit == 1:
            return dict(rows[0]) if rows else None
        return [dict(row) for row in rows]
    finally:
        conn.close()


def count_running():
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM download_queue WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def update_queue_progress(queue_id, current_episode, current_url):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET current_episode = ?, current_url = ? WHERE id = ?",
            (current_episode, current_url, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_queue_status(queue_id, status, last_error=None):
    conn = get_db()
    try:
        if status in TERMINAL_STATUSES:
            conn.execute(
                "UPDATE download_queue SET status = ?, last_error = ?, "
                "completed_at = datetime('now') WHERE id = ?",
                (status, _truncate_error(last_error), queue_id),
            )
        else:
            conn.execute(
                "UPDATE download_queue SET status = ? WHERE id = ?",
                (status, queue_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_queue_errors(queue_id, errors_json):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET errors = ? WHERE id = ?",
            (errors_json, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def requeue_item(queue_id):
    """Reset a finished item back to 'queued' so the worker retries it.

    Used by the retry action (e.g. after solving the kinox captcha). Clears the
    previous errors, progress and captcha marker, and moves the item to the end
    of the queue so it stays visible: the UI only lists the few most-recent
    finished items, and without the bump a retried-then-failed item would drop
    out of view. Returns True if a row changed.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS pos FROM download_queue"
        ).fetchone()
        next_pos = row["pos"] if row else 0
        # An explicit retry also clears the automatic retry state: someone
        # asking for another attempt should not be turned away by an exhausted
        # attempt counter or made to sit out a backoff window.
        cur = conn.execute(
            "UPDATE download_queue SET status='queued', errors='[]', "
            "current_episode=0, current_url=NULL, completed_at=NULL, "
            "captcha_url=NULL, attempts=0, next_attempt_at=NULL, last_error=NULL, "
            "import_status=NULL, position=? "
            "WHERE id=? AND status IN ('failed','cancelled','completed','imported')",
            (next_pos, queue_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_captcha_url(queue_id: int, url: str):
    """Set the captcha_url field to signal the Web UI that a captcha needs solving."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET captcha_url = ? WHERE id = ?",
            (url, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def clear_captcha_url(queue_id: int):
    """Clear the captcha_url field after the captcha has been solved."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET captcha_url = NULL WHERE id = ?",
            (queue_id,),
        )
        conn.commit()
    finally:
        conn.close()


_force_cancelled_queue_ids = set()


def force_cancel_queue_item(queue_id):
    ok, err = cancel_queue_item(queue_id)
    if not ok:
        # If it's already cancelled, we can still force cancel it
        if err == "Can only cancel running items":
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT status FROM download_queue WHERE id = ?", (queue_id,)
                ).fetchone()
                if row and row["status"] == "cancelled":
                    _force_cancelled_queue_ids.add(queue_id)
                    return True, None
            finally:
                conn.close()
        return False, err
    _force_cancelled_queue_ids.add(queue_id)
    return True, None


def cancel_queue_item(queue_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM download_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] not in ACTIVE_STATUSES:
            return False, "Can only cancel running items"
        conn.execute(
            "UPDATE download_queue SET status = 'cancelled' WHERE id = ?",
            (queue_id,),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def is_queue_cancelled(queue_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM download_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        return row and row["status"] == "cancelled"
    finally:
        conn.close()


def is_queue_force_cancelled(queue_id):
    return queue_id in _force_cancelled_queue_ids


def clear_force_cancelled(queue_id):
    _force_cancelled_queue_ids.discard(queue_id)


def remove_from_queue(queue_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM download_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] != "queued":
            return False, "Can only remove queued items"
        conn.execute("DELETE FROM download_queue WHERE id = ?", (queue_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def delete_completed_queue_item(queue_id):
    """Delete a queue item only if its status is 'completed'. Used by auto-sync cleanup."""
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM download_queue WHERE id = ? AND status = 'completed'",
            (queue_id,),
        )
        conn.commit()
    finally:
        conn.close()


def clear_completed():
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM download_queue WHERE status IN ('completed', 'failed', 'cancelled')"
        )
        conn.commit()
    finally:
        conn.close()


# ===== Custom Download Paths =====

_CREATE_CUSTOM_PATHS_TABLE = """\
CREATE TABLE IF NOT EXISTS custom_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL
);
"""


def init_custom_paths_db():
    ANIWORLD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_CUSTOM_PATHS_TABLE)
        # default_sites: CSV of site keys this path is the default for
        # (aniworld, sto, megakino, mangafire, htv, kinox, burningseries, filmpalast)
        try:
            conn.execute(
                "ALTER TABLE custom_paths ADD COLUMN default_sites TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def get_custom_paths():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, path, default_sites FROM custom_paths ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_custom_path(name, path, default_sites=""):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO custom_paths (name, path, default_sites) VALUES (?, ?, ?)",
            (name, path, default_sites),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_custom_path(path_id, name=None, path=None, default_sites=None):
    fields = []
    values = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if path is not None:
        fields.append("path = ?")
        values.append(path)
    if default_sites is not None:
        fields.append("default_sites = ?")
        values.append(default_sites)
    if not fields:
        return
    values.append(path_id)
    conn = get_db()
    try:
        conn.execute(
            f"UPDATE custom_paths SET {', '.join(fields)} WHERE id = ?", values
        )
        conn.commit()
    finally:
        conn.close()


def remove_custom_path(path_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM custom_paths WHERE id = ?", (path_id,))
        conn.commit()
    finally:
        conn.close()


def get_custom_path_by_id(path_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, path, default_sites FROM custom_paths WHERE id = ?",
            (path_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ===== Auto-Sync Jobs =====

_CREATE_AUTOSYNC_TABLE = """\
CREATE TABLE IF NOT EXISTS autosync_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    series_url TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'German Dub',
    provider TEXT NOT NULL DEFAULT 'VOE',
    custom_path_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_by TEXT,
    last_check TEXT,
    last_new_found TEXT,
    episodes_found INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_autosync_db():
    ANIWORLD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_AUTOSYNC_TABLE)
        # Add UNIQUE index on series_url (migration for existing DBs)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_autosync_series_url "
                "ON autosync_jobs (series_url)"
            )
        except sqlite3.IntegrityError:
            # Duplicates already exist — deduplicate keeping the lowest id
            conn.execute(
                "DELETE FROM autosync_jobs WHERE id NOT IN "
                "(SELECT MIN(id) FROM autosync_jobs GROUP BY series_url)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_autosync_series_url "
                "ON autosync_jobs (series_url)"
            )
        conn.commit()
    finally:
        conn.close()


def add_autosync_job(
    title, series_url, language, provider, custom_path_id=None, added_by=None
):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO autosync_jobs "
            "(title, series_url, language, provider, custom_path_id, added_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, series_url, language, provider, custom_path_id, added_by),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_autosync_jobs(username=None):
    """Return all sync jobs. If *username* is given, only that user's jobs."""
    conn = get_db()
    try:
        if username:
            rows = conn.execute(
                "SELECT * FROM autosync_jobs WHERE added_by = ? ORDER BY id",
                (username,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM autosync_jobs ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_autosync_job(job_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM autosync_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_autosync_by_url(series_url):
    """Return the first sync job that matches *series_url*, or None."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM autosync_jobs WHERE series_url = ? LIMIT 1",
            (series_url,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_autosync_job(job_id, **fields):
    """Update arbitrary columns on a sync job."""
    if not fields:
        return
    allowed = {
        "title",
        "series_url",
        "language",
        "provider",
        "custom_path_id",
        "enabled",
        "last_check",
        "last_new_found",
        "episodes_found",
    }
    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return
    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [job_id]
    conn = get_db()
    try:
        conn.execute(f"UPDATE autosync_jobs SET {set_clause} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def remove_autosync_job(job_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM autosync_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            return False, "Job not found"
        conn.execute("DELETE FROM autosync_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


# ===== Planned Releases =====

_CREATE_PLANNED_TABLE = """\
CREATE TABLE IF NOT EXISTS planned_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    site TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'movie',
    language TEXT NOT NULL DEFAULT 'German Dub',
    provider TEXT NOT NULL DEFAULT 'VOE',
    custom_path_id INTEGER,
    auto_sync INTEGER NOT NULL DEFAULT 0,
    added_by TEXT,
    status TEXT NOT NULL DEFAULT 'waiting',
    last_check TEXT,
    found_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_planned_db():
    ANIWORLD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_PLANNED_TABLE)
        conn.commit()
    finally:
        conn.close()


def add_planned_job(
    title,
    site,
    media_type,
    language,
    provider,
    custom_path_id=None,
    auto_sync=0,
    added_by=None,
):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO planned_jobs "
            "(title, site, media_type, language, provider, custom_path_id, auto_sync, added_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                site,
                media_type,
                language,
                provider,
                custom_path_id,
                1 if auto_sync else 0,
                added_by,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_planned_jobs(added_by=None):
    conn = get_db()
    try:
        if added_by is None:
            rows = conn.execute(
                "SELECT * FROM planned_jobs ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM planned_jobs WHERE added_by = ? ORDER BY id DESC",
                (added_by,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_planned_job(job_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM planned_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_planned_job(job_id, **fields):
    allowed = {"status", "last_check", "found_url", "title", "language", "provider"}
    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return
    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [job_id]
    conn = get_db()
    try:
        conn.execute(f"UPDATE planned_jobs SET {set_clause} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def remove_planned_job(job_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM planned_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            return False, "Job not found"
        conn.execute("DELETE FROM planned_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


# ===== Statistics =====


def get_sync_stats():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) AS cnt FROM autosync_jobs").fetchone()[
            "cnt"
        ]
        enabled = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE enabled = 1"
        ).fetchone()["cnt"]
        disabled = total - enabled
        last_check = conn.execute(
            "SELECT MAX(last_check) AS lc FROM autosync_jobs"
        ).fetchone()["lc"]
        last_new = conn.execute(
            "SELECT MAX(last_new_found) AS ln FROM autosync_jobs"
        ).fetchone()["ln"]
        total_eps = conn.execute(
            "SELECT COALESCE(SUM(episodes_found), 0) AS s FROM autosync_jobs"
        ).fetchone()["s"]
        jobs = conn.execute(
            "SELECT id, title, series_url, language, provider, enabled, "
            "last_check, last_new_found, episodes_found, added_by, created_at "
            "FROM autosync_jobs ORDER BY id"
        ).fetchall()
        return {
            "total_jobs": total,
            "enabled": enabled,
            "disabled": disabled,
            "last_check": last_check,
            "last_new_found": last_new,
            "total_episodes_found": total_eps,
            "jobs": [dict(r) for r in jobs],
        }
    finally:
        conn.close()


def get_queue_stats():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) AS cnt FROM download_queue").fetchone()[
            "cnt"
        ]
        by_status = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM download_queue GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["cnt"]
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        running = conn.execute(
            "SELECT title, current_episode, total_episodes FROM download_queue "
            f"WHERE status IN ({placeholders}) LIMIT 1",
            ACTIVE_STATUSES,
        ).fetchone()
        return {
            "total": total,
            "by_status": by_status,
            "currently_running": dict(running) if running else None,
        }
    finally:
        conn.close()


# ===== Webhook Outbox =====

_CREATE_WEBHOOK_TABLE = """\
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT
);
"""


def init_webhook_db():
    ANIWORLD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_WEBHOOK_TABLE)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_due "
            "ON webhook_outbox (delivered_at, next_attempt_at)"
        )
        conn.commit()
    finally:
        conn.close()


def enqueue_webhook(url, event, payload):
    """Store a delivery for later. Writing a row is all the event path does.

    Persisting rather than sending inline means a restart mid-delivery does not
    lose the event, and a webhook receiver that is slow or down cannot hold up
    a download.
    """
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO webhook_outbox (url, event, payload) VALUES (?, ?, ?)",
            (url, event, payload),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_due_webhooks(limit=20):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM webhook_outbox WHERE delivered_at IS NULL "
            "AND next_attempt_at <= datetime('now') ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_webhook_delivered(webhook_id):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE webhook_outbox SET delivered_at = datetime('now'), last_error = NULL "
            "WHERE id = ?",
            (webhook_id,),
        )
        conn.commit()
    finally:
        conn.close()


def mark_webhook_failed(webhook_id, error, delay_seconds, max_attempts=8):
    """Record a failed delivery and schedule the retry.

    After ``max_attempts`` the row is marked delivered so it stops being
    retried; the error is kept so it is still visible why it never arrived.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT attempts FROM webhook_outbox WHERE id = ?", (webhook_id,)
        ).fetchone()
        if not row:
            return False
        attempts = (row["attempts"] or 0) + 1
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE webhook_outbox SET attempts = ?, last_error = ?, "
                "delivered_at = datetime('now') WHERE id = ?",
                (attempts, _truncate_error(error), webhook_id),
            )
            conn.commit()
            return False
        conn.execute(
            "UPDATE webhook_outbox SET attempts = ?, last_error = ?, "
            "next_attempt_at = datetime('now', ?) WHERE id = ?",
            (
                attempts,
                _truncate_error(error),
                f"+{int(delay_seconds)} seconds",
                webhook_id,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def purge_delivered_webhooks(keep_days=7):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM webhook_outbox WHERE delivered_at IS NOT NULL "
            "AND delivered_at < datetime('now', ?)",
            (f"-{int(keep_days)} days",),
        )
        conn.commit()
    finally:
        conn.close()


def get_webhook_stats():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS pending FROM webhook_outbox WHERE delivered_at IS NULL"
        ).fetchone()
        return {"pending": row["pending"] if row else 0}
    finally:
        conn.close()


def get_general_stats():
    conn = get_db()
    try:
        total_downloads = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'failed')"
        ).fetchone()["cnt"]
        completed = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE status = 'completed'"
        ).fetchone()["cnt"]
        failed = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE status = 'failed'"
        ).fetchone()["cnt"]
        total_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status = 'completed'"
        ).fetchone()["s"]
        last_24h = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status = 'completed' "
            "AND completed_at >= datetime('now', '-1 day')"
        ).fetchone()["cnt"]
        # Average duration (completed items with both timestamps)
        avg_dur = conn.execute(
            "SELECT AVG("
            "  (julianday(completed_at) - julianday(created_at)) * 86400"
            ") AS avg_s FROM download_queue "
            "WHERE status = 'completed' AND completed_at IS NOT NULL"
        ).fetchone()["avg_s"]
        # Most downloaded titles
        top_titles = conn.execute(
            "SELECT title, COUNT(*) AS cnt FROM download_queue "
            "WHERE status = 'completed' GROUP BY title "
            "ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        # Episodes per language
        by_language = conn.execute(
            "SELECT language, COUNT(*) AS cnt, "
            "COALESCE(SUM(total_episodes), 0) AS eps "
            "FROM download_queue WHERE status = 'completed' "
            "GROUP BY language ORDER BY cnt DESC"
        ).fetchall()
        # Anime vs Series (heuristic: aniworld.to = anime, serienstream.to = series)
        anime_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status = 'completed' AND series_url LIKE '%aniworld.to%'"
        ).fetchone()["cnt"]
        series_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status = 'completed' AND ("
            "series_url LIKE '%serienstream.to%' OR series_url LIKE '%s.to%'"
            ")"
        ).fetchone()["cnt"]
        return {
            "total_downloads": total_downloads,
            "completed": completed,
            "failed": failed,
            "total_episodes": total_episodes,
            "last_24h_completed": last_24h,
            "average_duration_seconds": round(avg_dur, 1) if avg_dur else None,
            "top_titles": [
                {"title": r["title"], "count": r["cnt"]} for r in top_titles
            ],
            "by_language": [
                {"language": r["language"], "downloads": r["cnt"], "episodes": r["eps"]}
                for r in by_language
            ],
            "anime_downloads": anime_count,
            "series_downloads": series_count,
        }
    finally:
        conn.close()
