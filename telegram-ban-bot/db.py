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

logger = logging.getLogger(__name__)

DB_DIR = os.path.dirname(__file__)
DB_FILE = os.path.join(DB_DIR, "bot.db")

# JSON file paths (for migration)
CONFIG_FILE = os.path.join(DB_DIR, "config.json")
DATA_FILE = os.path.join(DB_DIR, "data.json")
USERS_FILE = os.path.join(DB_DIR, "users.json")
PROTOKOLL_FILE = os.path.join(DB_DIR, "protokoll.json")

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return _local.conn


def init_db():
    """Create tables if they don't exist and run migration."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            data TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.commit()

    # Auto-migrate from JSON files if kv_store is empty
    cursor = conn.execute("SELECT COUNT(*) FROM kv_store")
    count = cursor.fetchone()[0]
    if count == 0:
        logger.info("SQLite DB is empty — migrating from JSON files...")
        _migrate_from_json(conn)
    else:
        logger.info(f"SQLite DB loaded with {count} keys")


def _migrate_from_json(conn: sqlite3.Connection):
    """Import all JSON files into SQLite on first run."""
    migrations = {
        "config": CONFIG_FILE,
        "data": DATA_FILE,
        "users": USERS_FILE,
        "protokoll": PROTOKOLL_FILE,
    }

    for key, filepath in migrations.items():
        data = {}
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    data = json.loads(content)
                logger.info(f"Migrated {filepath} → SQLite key '{key}' ({len(content)} bytes)")
            except Exception as e:
                logger.error(f"Failed to migrate {filepath}: {e}")
                # Try backup
                bak = filepath + ".bak"
                if os.path.exists(bak):
                    try:
                        with open(bak, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        logger.info(f"Migrated from backup {bak}")
                    except Exception:
                        pass

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
    cursor = conn.execute("SELECT data FROM kv_store WHERE key = ?", (key,))
    row = cursor.fetchone()
    if row:
        data = json.loads(row[0])
    else:
        data = default if default is not None else {}

    with _cache_lock:
        _cache[key] = data

    return data


def kv_save(key: str, data: dict):
    """Save a JSON value by key (atomic via SQLite transaction)."""
    conn = _get_conn()
    json_str = json.dumps(data, ensure_ascii=False, indent=None)
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
