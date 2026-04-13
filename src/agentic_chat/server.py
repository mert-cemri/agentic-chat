"""FastMCP server setup, app builder, and serve command."""

import argparse
import asyncio
import logging

from mcp.server.fastmcp import FastMCP

from .config import CONFIG, load_config, validate_config
from . import db as _db_mod
from .auth import TokenAuthMiddleware

log = logging.getLogger("relay")

mcp = FastMCP(
    "claude-relay",
    instructions=(
        "You are connected to a Claude Relay -- a messaging system that lets you "
        "communicate with other Claude Code instances and users in real time.\n\n"
        "KEY PRINCIPLES:\n"
        "- Your identity is automatic. It comes from your auth token. NEVER ask the user "
        "for their name or who they are -- you already know from the heartbeat response.\n"
        "- Never expose tool names to the user. Don't say 'I'll call heartbeat' -- just "
        "do the action and report results naturally, e.g. 'You have 3 unread messages from alice.'\n"
        "- Pull-based only. Do NOT poll, auto-check, or loop. Only act when the user asks.\n\n"
        "COMMON PATTERNS:\n"
        "- When the user says 'any messages?' or 'check the relay': call heartbeat first to "
        "see unread counts, then call receive if total_unread > 0. Summarize naturally.\n"
        "- When the user says 'tell X something' or 'message X': construct the DM channel "
        "as dm-{yourname}-{X} and call send. The server sorts the names, so order doesn't matter. "
        "Confirm delivery naturally, e.g. 'Sent to alice.'\n"
        "- When the user says 'broadcast ...' or 'tell everyone ...': send to channel='general'.\n"
        "- When the user says 'who's online?': call heartbeat or list_peers and report who's around.\n"
        "- When the user asks about a specific person's messages: call receive with "
        "channel='dm-{yourname}-{theirname}'.\n\n"
        "BATCHING: If the user asks multiple things at once (e.g. 'who's online and any messages "
        "from bob?'), make parallel tool calls when possible.\n\n"
        "TONE: Be conversational. Say 'alice is online' not 'peer alice has status online'. "
        "Say 'no new messages' not 'heartbeat returned total_unread: 0'."
    ),
)

# Import tools and dashboard to register @mcp.tool() decorators and custom routes
from . import tools  # noqa: E402, F401
from . import dashboard  # noqa: E402, F401


def cmd_serve(args: argparse.Namespace, skip_config_load: bool = False) -> None:
    """Start the relay server."""
    global CONFIG
    import agentic_chat.config as config_module
    if not skip_config_load:
        config_module.CONFIG.update(load_config())
        validate_config(config_module.CONFIG)

    # Configure FastMCP transport security:
    # - The default DNS-rebinding protection only allows localhost variants.
    # - When the relay is behind a tunnel/reverse proxy with a custom hostname,
    #   that host arrives in the Host header and gets rejected with HTTP 421.
    # - If `public_url` is set in config, derive its hostname and include it
    #   in `allowed_hosts` (alongside the localhost defaults).
    from mcp.server.fastmcp.server import TransportSecuritySettings
    from urllib.parse import urlparse

    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"]
    allowed_origins = [
        "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
        "https://127.0.0.1:*", "https://localhost:*",
    ]
    public_url = config_module.CONFIG.get("public_url")
    if public_url:
        parsed = urlparse(public_url)
        if parsed.hostname:
            host_with_port = parsed.netloc  # includes port if specified
            allowed_hosts.append(host_with_port)
            allowed_hosts.append(parsed.hostname)  # without port (Host: header may omit it)
            allowed_origins.append(f"{parsed.scheme}://{host_with_port}")
            allowed_origins.append(f"{parsed.scheme}://{parsed.hostname}")
            log.info("Allowing public_url host in transport security: %s", parsed.netloc)

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    async def _run() -> None:
        await _db_mod.db.connect(config_module.CONFIG["db_path"])

        app = mcp.streamable_http_app()
        app = TokenAuthMiddleware(app)

        import uvicorn

        config = uvicorn.Config(
            app,
            host=config_module.CONFIG["host"],
            port=config_module.CONFIG["port"],
            log_level="info",
        )
        server = uvicorn.Server(config)
        log.info("Starting claude-relay on %s:%d", config_module.CONFIG["host"], config_module.CONFIG["port"])
        await server.serve()

    asyncio.run(_run())


__all__ = ["mcp", "cmd_serve"]
