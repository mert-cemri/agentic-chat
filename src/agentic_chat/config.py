"""Configuration loading, validation, and time utilities."""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("relay")

DEFAULT_CONFIG: dict[str, Any] = {
    "port": 4444,
    "host": "0.0.0.0",
    "db_path": "./data/relay.db",
    "heartbeat_timeout_seconds": 120,
    "message_retention_days": 7,
    "max_message_length": 50000,
    "cleanup_batch_size": 5000,
    "max_receive_response_bytes": 102400,  # 100KB
    # Token bucket rate limiter: burst of N requests, refilled at R/s.
    # Default allows 30-request bursts (covers MCP init + tool calls) and
    # sustains 5 req/s per authenticated token.
    "rate_limit_burst": 30,
    "rate_limit_refill_per_sec": 5.0,
    # Public URL used for generating join links. If null, the request's
    # Host header is used (convenient for localhost dev, but vulnerable
    # to header poisoning on public deployments — set explicitly).
    "public_url": None,
}

CONFIG: dict[str, Any] = {}


def load_config() -> dict[str, Any]:
    """Load config from relay.config.json, falling back to defaults."""
    config = dict(DEFAULT_CONFIG)
    config_path = Path("relay.config.json")
    if config_path.exists():
        with open(config_path) as f:
            overrides = json.load(f)
        config.update(overrides)
        log.info("Loaded config from %s", config_path)
    else:
        log.info("No config file found, using defaults")
    return config


def validate_config(config: dict[str, Any]) -> None:
    """Validate config types and ranges. Raises ValueError on invalid config."""
    int_checks = {
        "port": (int, 1, 65535),
        "heartbeat_timeout_seconds": (int, 10, 3600),
        "message_retention_days": (int, 1, 365),
        "max_message_length": (int, 100, 1_000_000),
        "cleanup_batch_size": (int, 100, 100_000),
        "max_receive_response_bytes": (int, 1024, 10_000_000),
        "rate_limit_burst": (int, 1, 10_000),
    }
    for key, (expected_type, min_val, max_val) in int_checks.items():
        val = config.get(key)
        if val is None:
            raise ValueError(f"Missing config key: {key}")
        if not isinstance(val, expected_type):
            raise ValueError(
                f"Config '{key}' must be {expected_type.__name__}, "
                f"got {type(val).__name__}"
            )
        if not (min_val <= val <= max_val):
            raise ValueError(
                f"Config '{key}' must be between {min_val} and {max_val}, got {val}"
            )

    refill = config.get("rate_limit_refill_per_sec")
    if not isinstance(refill, (int, float)) or not (0.1 <= refill <= 1000):
        raise ValueError(
            "Config 'rate_limit_refill_per_sec' must be a number between 0.1 and 1000"
        )

    if not isinstance(config.get("host"), str):
        raise ValueError("Config 'host' must be a string")
    if not isinstance(config.get("db_path"), str):
        raise ValueError("Config 'db_path' must be a string")

    public_url = config.get("public_url")
    if public_url is not None and not isinstance(public_url, str):
        raise ValueError("Config 'public_url' must be a string or null")


def now_ms() -> int:
    """Current time as unix timestamp in milliseconds."""
    return int(time.time() * 1000)


def ms_to_iso(ms: int) -> str:
    """Convert unix ms timestamp to ISO 8601 string."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


__all__ = [
    "DEFAULT_CONFIG",
    "CONFIG",
    "load_config",
    "validate_config",
    "now_ms",
    "ms_to_iso",
]
