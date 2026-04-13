"""Lazy batched cleanup of expired messages."""

import logging
import time

from .config import CONFIG, now_ms
from . import db as _db_mod

log = logging.getLogger("relay")

_last_cleanup_mono: float = 0.0


async def maybe_cleanup() -> None:
    """Lazy batched cleanup of expired messages. At most once per hour."""
    global _last_cleanup_mono
    if not CONFIG:
        return
    now_mono = time.monotonic()
    if now_mono - _last_cleanup_mono < 3600:
        return
    _last_cleanup_mono = now_mono

    cutoff = now_ms() - (CONFIG["message_retention_days"] * 86400 * 1000)
    batch_size = CONFIG["cleanup_batch_size"]

    total_deleted = 0
    while True:
        cursor = await _db_mod.db.execute(
            "DELETE FROM messages WHERE rowid IN "
            "(SELECT rowid FROM messages WHERE created_at < ? LIMIT ?)",
            (cutoff, batch_size),
        )
        deleted = cursor.rowcount
        total_deleted += deleted
        if deleted < batch_size:
            break

    if total_deleted > 0:
        log.info("Cleanup: deleted %d expired messages", total_deleted)


__all__ = ["maybe_cleanup"]
