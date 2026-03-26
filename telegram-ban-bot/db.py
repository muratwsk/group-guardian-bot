"""
SQLite-backed storage for the Telegram Ban Bot.
Replaces JSON file I/O with atomic SQLite transactions (WAL mode).
Stores config, data, users, and protokoll as JSON blobs in a key-value table.
Auto-migrates from existing JSON files on first run.
"""

import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

DB_DIR = os.path.dirname(__file__)
DB_FILE = os.path.join(DB_DIR, "bot.db")

# JSON file paths (for migration)
CONFIG_FILE = os.path.join(DB_DIR, "config.json")
DATA_FILE = os.path.join(DB_DIR, "data.json")
USERS_FILE = os.path.join(DB_DIR, "users.json")
PROTOKOLL_FILE = os.path.join(DB_DIR, "protokoll.json")

KV_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY,
        data TEXT NOT NULL DEFAULT '{}'
    )
"""

KEY_FILE_MAP = {
    "config": CONFIG_FILE,
    "data": DATA_FILE,
    "users": USERS_FILE,
    "protokoll": PROTOKOLL_FILE,
}

DEFAULT_KV_DATA = {
    "config": {},
    "data": {"groups": [], "banned_users": {}},
    "users": {},
    "protokoll": {},
}

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        _configure_conn(conn)
        _local.conn = conn
    return _local.conn


def _configure_conn(conn: sqlite3.Connection):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")


def _close_local_conn():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None


def _load_json_file(filepath: str, default=None):
    default_value = {} if default is None else default
    for candidate in (filepath, filepath + ".bak"):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                return default_value
            return json.loads(content)
        except Exception as e:
            logger.error(f"Failed to read JSON snapshot {candidate}: {e}")
    return default_value


def _write_json_file_atomic(filepath: str, data):
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, filepath)


def _write_snapshot_for_key(key: str, data):
    filepath = KEY_FILE_MAP.get(key)
    if not filepath:
        return
    try:
        _write_json_file_atomic(filepath, data)
    except Exception as e:
        logger.error(f"Failed to write JSON snapshot for key '{key}': {e}")


def _db_file_is_healthy() -> bool:
    if not os.path.exists(DB_FILE):
        return False

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        _configure_conn(conn)
        result = conn.execute("PRAGMA quick_check").fetchone()
        return bool(result) and result[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _quarantine_corrupt_db():
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        src = f"{DB_FILE}{suffix}"
        if not os.path.exists(src):
            continue
        dst = f"{src}.corrupt.{timestamp}"
        try:
            os.replace(src, dst)
            logger.warning(f"Quarantined corrupt SQLite file: {dst}")
        except OSError as e:
            logger.error(f"Failed to quarantine {src}: {e}")


_recovery_lock = threading.Lock()


def _handle_db_corruption(exc: Exception):
    message = str(exc).lower()
    if "malformed" not in message and "not a database" not in message:
        raise exc

    with _recovery_lock:
        logger.exception("SQLite corruption detected, starting recovery")
        _close_local_conn()

        if _db_file_is_healthy():
            invalidate_cache()
            logger.warning("SQLite connection reset after corruption signal")
            return

        _quarantine_corrupt_db()

        conn = sqlite3.connect(DB_FILE, timeout=10)
        _configure_conn(conn)
        conn.execute(KV_TABLE_SQL)

        restored_cache = {}
        for key, default in DEFAULT_KV_DATA.items():
            data = _load_json_file(KEY_FILE_MAP[key], default)
            conn.execute(
                "INSERT OR REPLACE INTO kv_store (key, data) VALUES (?, ?)",
                (key, json.dumps(data, ensure_ascii=False)),
            )
            restored_cache[key] = data

        conn.commit()
        _local.conn = conn

        with _cache_lock:
            _cache.clear()
            _cache.update(restored_cache)

        logger.warning("SQLite database rebuilt from JSON snapshots")


def init_db():
    """Create tables if they don't exist and run migration."""
    conn = _get_conn()
    try:
        conn.execute(KV_TABLE_SQL)
        conn.commit()

        # Auto-migrate from JSON files if kv_store is empty
        cursor = conn.execute("SELECT COUNT(*) FROM kv_store")
        count = cursor.fetchone()[0]
    except sqlite3.DatabaseError as e:
        _handle_db_corruption(e)
        conn = _get_conn()
        cursor = conn.execute("SELECT COUNT(*) FROM kv_store")
        count = cursor.fetchone()[0]

    if count == 0:
        logger.info("SQLite DB is empty — migrating from JSON files...")
        _migrate_from_json(conn)
    else:
        logger.info(f"SQLite DB loaded with {count} keys")


def _migrate_from_json(conn: sqlite3.Connection):
    """Import all JSON files into SQLite on first run."""
    for key, filepath in KEY_FILE_MAP.items():
        data = _load_json_file(filepath, DEFAULT_KV_DATA.get(key, {}))
        logger.info(f"Migrated {filepath} → SQLite key '{key}'")

        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, data) VALUES (?, ?)",
            (key, json.dumps(data, ensure_ascii=False)),
        )

    conn.commit()
    logger.info("JSON → SQLite migration complete!")


# ── Generic KV operations ──────────────────────────────────────────

# In-memory cache to reduce DB reads
_cache = {}
_cache_lock = threading.Lock()


def kv_load(key: str, default=None) -> dict:
    """Load a JSON value by key, with in-memory cache."""
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    conn = _get_conn()
    try:
        cursor = conn.execute("SELECT data FROM kv_store WHERE key = ?", (key,))
    except sqlite3.DatabaseError as e:
        _handle_db_corruption(e)
        conn = _get_conn()
        cursor = conn.execute("SELECT data FROM kv_store WHERE key = ?", (key,))

    row = cursor.fetchone()
    if row:
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            data = _load_json_file(KEY_FILE_MAP.get(key, ""), default if default is not None else {})
    else:
        data = _load_json_file(
            KEY_FILE_MAP.get(key, ""),
            default if default is not None else DEFAULT_KV_DATA.get(key, {}),
        )

    with _cache_lock:
        _cache[key] = data

    return data


def kv_save(key: str, data: dict):
    """Save a JSON value by key (atomic via SQLite transaction)."""
    conn = _get_conn()
    json_str = json.dumps(data, ensure_ascii=False, indent=None)
    _write_snapshot_for_key(key, data)

    try:
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, data) VALUES (?, ?)",
            (key, json_str),
        )
        conn.commit()
    except sqlite3.DatabaseError as e:
        _handle_db_corruption(e)
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, data) VALUES (?, ?)",
            (key, json_str),
        )
        conn.commit()

    with _cache_lock:
        _cache[key] = data


def invalidate_cache(key: str = None):
    """Clear cache for a specific key or all keys."""
    with _cache_lock:
        if key:
            _cache.pop(key, None)
        else:
            _cache.clear()


# ── Typed accessors (drop-in replacements for JSON functions) ──────

def load_config() -> dict:
    return kv_load("config", {})


def save_config(cfg: dict):
    kv_save("config", cfg)


def load_data() -> dict:
    return kv_load("data", {"groups": [], "banned_users": {}})


def save_data(data: dict):
    kv_save("data", data)


def load_users() -> dict:
    return kv_load("users", {})


def save_users(users: dict):
    kv_save("users", users)


def load_protokoll() -> dict:
    return kv_load("protokoll", {})


def save_protokoll(data: dict):
    kv_save("protokoll", data)


# ── Initialize on import ──────────────────────────────────────────

init_db()
