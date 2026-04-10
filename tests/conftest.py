"""Shared fixtures for claude-relay tests."""

import hashlib
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Add parent dir to path so we can import relay
sys.path.insert(0, str(Path(__file__).parent.parent))

import relay as relay_module
from relay import RelayDB, SCHEMA_SQL, now_ms, DEFAULT_CONFIG


@pytest_asyncio.fixture
async def test_db(tmp_path):
    """Async test DB with schema initialized.
    Also replaces the module-level `db` singleton so tool handlers
    and the auth middleware use this test DB.

    The fresh DB per test ensures test isolation even though the FastMCP
    app (and its lifespan) is session-scoped.
    """
    d = RelayDB()
    db_path = str(tmp_path / "test.db")
    await d.connect(db_path)

    # Replace the module-level singleton so tool handlers use this DB
    original_db = relay_module.db
    relay_module.db = d

    yield d

    relay_module.db = original_db
    await d.close()


@pytest_asyncio.fixture
async def seeded_db(test_db):
    """DB with 3 test peers (alice, bob, carol) and their tokens.
    Returns (db, tokens_dict) where tokens_dict maps name -> raw_token.
    """
    raw_tokens = {}
    for name in ["alice", "bob", "carol"]:
        raw = f"relay_tok_test_{name}"
        h = hashlib.sha256(raw.encode()).hexdigest()
        await test_db.execute(
            "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
            "VALUES (?, ?, 'default', ?)",
            (h, name, now_ms()),
        )
        # Create peer row
        await test_db.execute(
            "INSERT INTO peers (peer_name, namespace, status, last_heartbeat, "
            "last_heartbeat_monotonic, first_seen) VALUES (?, 'default', 'offline', ?, ?, ?)",
            (name, now_ms(), 0.0, now_ms()),
        )
        raw_tokens[name] = raw
    return test_db, raw_tokens


@pytest.fixture(autouse=True)
def set_config():
    """Ensure CONFIG is populated for all tests."""
    relay_module.CONFIG = dict(DEFAULT_CONFIG)
    yield
    relay_module.CONFIG = {}


# ------------------------------------------------------------------
# HTTP client fixture — tests the full stack via ASGI (no real port)
# ------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def _live_app():
    """Session-scoped: build FastMCP app and run its lifespan ONCE.

    FastMCP's StreamableHTTPSessionManager.run() can only be called once
    per instance, so we must share the app across all tests and reset
    the DB per-test via the module-level singleton swap.

    DNS rebinding protection is DISABLED for tests (httpx ASGI transport
    doesn't populate the Host header the way a real HTTP server does).

    The token bucket rate limiter is effectively disabled for tests by
    setting a very large burst capacity, so tests can fire requests at
    full speed without false 429s.
    """
    from asgi_lifespan import LifespanManager
    from mcp.server.fastmcp.server import TransportSecuritySettings
    from relay import mcp, TokenAuthMiddleware

    # Disable DNS rebinding protection for ASGI test transport
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

    app = mcp.streamable_http_app()
    async with LifespanManager(app) as manager:
        middleware = TokenAuthMiddleware(manager.app)
        # Very large burst so tests don't hit the limiter unless they
        # specifically test for it (which re-scopes the config locally).
        middleware._burst = 100_000
        middleware._refill = 100_000.0
        yield middleware


@pytest_asyncio.fixture
async def http_client(test_db, _live_app):
    """Function-scoped HTTP client using the session-scoped app.

    test_db ensures each test sees a fresh DB (via module-level swap).
    _live_app provides the running FastMCP app.

    Rate limiter state is cleared between tests so one test's requests
    don't affect another test. The CONFIG is also reset to the large
    test burst to override anything a previous test may have set.
    """
    import httpx

    # Clear per-token buckets between tests
    _live_app._buckets.clear()
    # Make sure CONFIG has the large test burst (in case a previous test
    # changed rate_limit_burst or rate_limit_refill_per_sec)
    relay_module.CONFIG["rate_limit_burst"] = 100_000
    relay_module.CONFIG["rate_limit_refill_per_sec"] = 100_000.0

    transport = httpx.ASGITransport(app=_live_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        yield client


class MCPClient:
    """Thin helper that wraps httpx + MCP protocol for test code."""

    def __init__(self, http_client, token: str, name: str = "test"):
        self.http = http_client
        self.token = token
        self.name = name
        self.session_id: str | None = None

    @property
    def headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def initialize(self) -> dict:
        """Perform the MCP initialize handshake. Returns the init result."""
        resp = await self.http.post(
            "/mcp",
            headers=self.headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": self.name, "version": "1.0"},
                },
            },
        )
        self.session_id = resp.headers.get("mcp-session-id")
        # Send initialized notification
        await self.http.post(
            "/mcp",
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return self._parse_sse(resp.text)

    async def call_tool(self, name: str, args: dict) -> dict:
        """Call an MCP tool. Returns the parsed JSON content of the result."""
        resp = await self.http.post(
            "/mcp",
            headers=self.headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )
        return self._parse_tool_result(resp.text)

    async def list_tools(self) -> dict:
        """List all MCP tools."""
        resp = await self.http.post(
            "/mcp",
            headers=self.headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )
        return self._parse_sse(resp.text)

    @staticmethod
    def _parse_sse(text: str) -> dict:
        """Parse Server-Sent Event response into JSON."""
        import json
        for line in text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return {}

    @staticmethod
    def _parse_tool_result(text: str) -> dict:
        """Parse tool call result into the inner JSON dict."""
        import json
        data = MCPClient._parse_sse(text)
        for c in data.get("result", {}).get("content", []):
            if c.get("type") == "text":
                return json.loads(c["text"])
        return {"_raw": data}


@pytest.fixture
def mcp_client(http_client):
    """Factory for creating MCPClient instances."""
    def _make(token: str, name: str = "test") -> MCPClient:
        return MCPClient(http_client, token, name)
    return _make
