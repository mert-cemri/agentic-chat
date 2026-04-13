"""Database layer: schema and async SQLite wrapper."""

import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger("relay")

SCHEMA_SQL = """\
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    peer_name TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT 'default',
    created_at INTEGER NOT NULL,
    last_used_at INTEGER
);

CREATE TABLE IF NOT EXISTS peers (
    peer_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offline',
    status_message TEXT,
    last_heartbeat INTEGER,
    last_heartbeat_monotonic REAL,
    first_seen INTEGER NOT NULL,
    PRIMARY KEY (namespace, peer_name)
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(namespace, name)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL CHECK(length(content) > 0),
    created_at INTEGER NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);

CREATE TABLE IF NOT EXISTS cursors (
    namespace TEXT NOT NULL,
    peer_name TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    last_read_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, peer_name, channel_id),
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_cursor
    ON messages(channel_id, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_namespace
    ON messages(namespace, created_at);
CREATE INDEX IF NOT EXISTS idx_peers_heartbeat
    ON peers(last_heartbeat);
"""


class RelayDB:
    """Async SQLite wrapper using aiosqlite. Module-level singleton."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: str | None = None

    async def connect(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        await self._db.execute("UPDATE peers SET status = 'offline'")
        await self._db.commit()
        log.info("Database connected: %s", db_path)

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        assert self._db is not None, "DB not connected"
        cursor = await self._db.execute(sql, params)
        await self._db.commit()
        return cursor

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        assert self._db is not None, "DB not connected"
        cursor = await self._db.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        assert self._db is not None, "DB not connected"
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def close(self) -> None:
        if self._db:
            try:
                await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            await self._db.close()
            self._db = None
            log.info("Database closed")

    @property
    def path(self) -> str | None:
        return self._db_path


# Module-level singleton -- connected in cmd_serve()
db = RelayDB()

__all__ = ["SCHEMA_SQL", "RelayDB", "db"]
