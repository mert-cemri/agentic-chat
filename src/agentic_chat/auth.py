"""Authentication middleware and helpers."""

import hashlib
import logging
import time
from typing import Any

from starlette.responses import JSONResponse

from .config import CONFIG, now_ms
from .channels import validate_session_peer_name
from . import db as _db_mod

log = logging.getLogger("relay")


async def resolve_token(raw_token: str) -> dict | None:
    """Look up a bearer token and return {owner_name, namespace} or None.

    Shared between the ASGI middleware (MCP tool calls) and the dashboard
    helpers so auth behaves identically across entry points.
    """
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = await _db_mod.db.fetchone(
        "SELECT owner_name, namespace FROM tokens WHERE token_hash = ?",
        (token_hash,),
    )
    return dict(row) if row else None


def resolve_peer_name(owner_name: str, declared: str | None) -> tuple[str, str | None]:
    """Resolve the session's peer_name from the header (or default to owner).

    Returns (peer_name, error). If ``declared`` is None/empty the peer_name
    defaults to ``owner_name`` (back-compat for clients that don't send the
    header). Otherwise the declared name must pass
    :func:`validate_session_peer_name`.
    """
    if not declared:
        return owner_name, None
    err = validate_session_peer_name(declared, owner_name)
    if err:
        return declared, err
    return declared, None


class TokenBucket:
    """Simple token bucket for burst-friendly rate limiting.

    Allows short bursts up to `capacity` requests, with sustained throughput
    limited to `refill_rate` per second. The bucket refills continuously.
    """

    __slots__ = ("capacity", "refill_rate", "tokens", "last_refill")

    def __init__(self, capacity: int, refill_rate: float, now: float):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last_refill = now

    def try_consume(self, now: float) -> bool:
        """Attempt to consume one token. Returns True if allowed.

        Uses a small epsilon in the comparison to tolerate floating-point
        drift from the refill computation (e.g., 0.2 * 10.0 = 1.9999...).
        """
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0 - 1e-9:
            self.tokens = max(0.0, self.tokens - 1.0)
            return True
        return False


class TokenAuthMiddleware:
    """ASGI middleware: validates bearer token, injects peer identity, rate-limits.

    Rate limiting uses a per-token bucket with burst capacity (default 30) and
    sustained refill (default 5/s). This accommodates the burst of requests
    MCP clients fire during initialization while still catching runaway loops.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._buckets: dict[str, TokenBucket] = {}
        # Defaults; overridden from CONFIG at request time if available.
        self._burst = 30
        self._refill = 5.0

    def _get_bucket(self, token_hash: str, now: float) -> TokenBucket:
        # Pick up config overrides lazily — allows tests and runtime changes
        # to CONFIG without recreating the middleware.
        burst = CONFIG.get("rate_limit_burst", self._burst) if CONFIG else self._burst
        refill = (
            CONFIG.get("rate_limit_refill_per_sec", self._refill)
            if CONFIG else self._refill
        )
        bucket = self._buckets.get(token_hash)
        if bucket is None or bucket.capacity != burst or bucket.refill_rate != refill:
            bucket = TokenBucket(burst, refill, now)
            self._buckets[token_hash] = bucket
        return bucket

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        # /health, /join/, and dashboard routes are public (dashboard API
        # endpoints handle their own auth via _authenticate_dashboard_request).
        if (
            path == "/health"
            or path.startswith("/dashboard")
            or path.startswith("/join/")
        ):
            return await self.app(scope, receive, send)

        # Extract bearer token
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()

        if not auth.startswith("Bearer "):
            response = JSONResponse(
                {
                    "error": "Missing or invalid Authorization header",
                    "hint": "Include header: Authorization: Bearer <your_token>",
                },
                status_code=401,
            )
            return await response(scope, receive, send)

        raw_token = auth[7:]
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        # Authenticate FIRST (before rate limiting to prevent attacker-controlled dict growth)
        row = await resolve_token(raw_token)

        if not row:
            log.warning("Auth failed for token_hash=%s", token_hash[:12])
            response = JSONResponse(
                {
                    "error": "Invalid or revoked token",
                    "hint": "Check your token or ask the relay operator for a new one.",
                },
                status_code=403,
            )
            return await response(scope, receive, send)

        owner_name = row["owner_name"]
        namespace = row["namespace"]

        # Session-time peer_name: X-Peer-Name header, or fall back to owner.
        declared = headers.get(b"x-peer-name", b"").decode().strip() or None
        peer_name, err = resolve_peer_name(owner_name, declared)
        if err:
            log.warning(
                "Peer name rejected: owner=%s/%s declared=%r: %s",
                namespace, owner_name, declared, err,
            )
            response = JSONResponse(
                {
                    "error": err,
                    "hint": (
                        "X-Peer-Name must equal the token's owner or start "
                        f"with {owner_name + '-'!r}."
                    ),
                },
                status_code=403,
            )
            return await response(scope, receive, send)

        # Token bucket rate limiting (post-auth to prevent attacker dict growth)
        now_mono = time.monotonic()
        bucket = self._get_bucket(token_hash, now_mono)
        if not bucket.try_consume(now_mono):
            log.warning("Rate limited: %s/%s", namespace, peer_name)
            response = JSONResponse(
                {
                    "error": "Too many requests. Please slow down.",
                    "hint": (
                        f"Your token bucket is empty. Sustained rate: "
                        f"{bucket.refill_rate:g}/sec, burst: {int(bucket.capacity)}."
                    ),
                },
                status_code=429,
            )
            return await response(scope, receive, send)

        # Bound dict size to prevent unbounded growth
        if len(self._buckets) > 1000:
            cutoff = now_mono - 300  # evict buckets untouched for > 5 min
            self._buckets = {
                k: b for k, b in self._buckets.items() if b.last_refill > cutoff
            }

        # Inject peer identity into ASGI scope. Consumers read peer_name
        # for attribution/cursors; owner_name is available for policy checks.
        scope["relay_peer"] = {
            "peer_name": peer_name,
            "namespace": namespace,
            "owner_name": owner_name,
        }

        log.debug("Authenticated: %s/%s (owner=%s)", namespace, peer_name, owner_name)

        # Update last_used_at
        await _db_mod.db.execute(
            "UPDATE tokens SET last_used_at = ? WHERE token_hash = ?",
            (now_ms(), token_hash),
        )

        # Ensure peer row exists. Set `last_heartbeat_monotonic` so the stale
        # peer cleanup actually considers this peer (NULL never compares < cutoff).
        # On re-auth for an existing peer, this is a no-op (ON CONFLICT DO NOTHING).
        await _db_mod.db.execute(
            """INSERT INTO peers (peer_name, namespace, status, last_heartbeat,
               last_heartbeat_monotonic, first_seen)
               VALUES (?, ?, 'online', ?, ?, ?)
               ON CONFLICT(namespace, peer_name) DO NOTHING""",
            (
                peer_name,
                namespace,
                now_ms(),
                time.monotonic(),
                now_ms(),
            ),
        )

        await self.app(scope, receive, send)


def get_caller(ctx: "Context") -> dict:
    """Extract authenticated peer identity from MCP Context -> ASGI scope."""
    try:
        return ctx.request_context.request.scope["relay_peer"]
    except (AttributeError, KeyError):
        raise RuntimeError("No peer identity in scope -- auth middleware not applied")


__all__ = [
    "TokenBucket",
    "TokenAuthMiddleware",
    "get_caller",
    "resolve_token",
    "resolve_peer_name",
]
