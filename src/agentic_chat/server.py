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
        "You are connected to a Claude Relay server. "
        "This lets you communicate with other Claude Code instances.\n\n"
        "IMPORTANT USAGE PATTERN:\n"
        "1. Call 'heartbeat' first to see who's online and check for unread messages.\n"
        "2. Use 'send' to message a channel. For DMs: send(channel=\"dm-yourname-theirname\"). "
        "The server normalizes the name order.\n"
        "3. Use 'receive' to read messages. Omit 'channel' to get unread from all channels.\n"
        "4. Use 'send(channel=\"general\", ...)' for messages to everyone.\n\n"
        "Your identity is automatically determined from your auth token -- "
        "you do NOT specify who you are.\n"
        "Do NOT call heartbeat repeatedly in a loop. "
        "Only call it when the user asks or at natural breakpoints."
    ),
)

# Import tools and dashboard to register @mcp.tool() decorators and custom routes
from . import tools  # noqa: E402, F401
from . import dashboard  # noqa: E402, F401


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the relay server."""
    global CONFIG
    import agentic_chat.config as config_module
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
