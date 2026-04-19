"""CLI: init, token create/list/revoke, check, demo, agent, main()."""

import argparse
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config, validate_config, now_ms, ms_to_iso
from .channels import OWNER_NAME_RE
from .db import SCHEMA_SQL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("relay")


def cmd_init(args: argparse.Namespace) -> None:
    """Interactive first-time setup."""
    import sqlite3

    print("Claude Relay -- first-time setup\n")

    port = input("Port [4444]: ").strip() or "4444"
    namespace = input("Default namespace [default]: ").strip() or "default"

    config = dict(DEFAULT_CONFIG)
    config["port"] = int(port)

    Path("data").mkdir(exist_ok=True)
    with open("relay.config.json", "w") as f:
        json.dump(config, f, indent=2)

    conn = sqlite3.connect(config["db_path"])
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute("UPDATE peers SET status = 'offline'")

        # Create operator's owner token
        raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        conn.execute(
            "INSERT INTO tokens (token_hash, owner_name, namespace, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, "admin", namespace, now_ms()),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"\nConfig written to: relay.config.json")
    print(f"Database created at: {config['db_path']}")
    print(f"\nYour token (SAVE THIS -- shown only once):")
    print(f"  {raw_token}")
    print(f"  Owner: admin. Sessions may connect as 'admin' or 'admin-<suffix>'.")
    print(f"\nNote: All relay administration is via the CLI on this machine.")
    print(f"\nTo create an owner token for someone else:")
    print(f"  python relay.py token create --owner shubham --namespace {namespace}")
    print(f"\nTo start the server:")
    print(f"  python relay.py serve")


def cmd_token_create(args: argparse.Namespace) -> None:
    """Generate a new owner token.

    One token per *person*; each session then picks its own peer_name via
    the ``X-Peer-Name`` header (must equal the owner or start with
    ``{owner}-``). So one human with three Claude Code sessions needs one
    token, not three.
    """
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])

    owner = args.owner
    namespace = args.namespace

    if not OWNER_NAME_RE.match(owner):
        print(
            f"Error: owner name must match {OWNER_NAME_RE.pattern} "
            "(alphanumeric + underscore, no hyphens; hyphens are the "
            "session-suffix separator)",
            file=sys.stderr,
        )
        sys.exit(1)

    raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    try:
        conn.execute(
            "INSERT INTO tokens (token_hash, owner_name, namespace, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, owner, namespace, now_ms()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        print(
            "Error: could not create token (hash collision -- extremely unlikely, try again)",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        conn.close()

    relay_url = args.url if hasattr(args, "url") and args.url else None

    print(f"\nToken for owner '{owner}':")
    print(f"  {raw_token}")
    print(f"\n  One token, N sessions. Each session picks its own peer name")
    print(f"  via the X-Peer-Name header — must be '{owner}' or start with")
    print(f"  '{owner}-' (e.g. '{owner}-laptop', '{owner}-desktop').")

    if relay_url:
        mcp_url = f"{relay_url.rstrip('/')}/mcp"
        join_link = f"{relay_url.rstrip('/')}/join/{raw_token}"
        print(f"\n  Example — run this on each machine (edit the name):")
        print(
            f"  claude mcp add -t http -s user "
            f'-H "Authorization: Bearer {raw_token}" '
            f'-H "X-Peer-Name: {owner}-laptop" '
            f"-- relay {mcp_url}"
        )
        print(f"\n  Or share this join link (shows a copy-paste command):")
        print(f"  {join_link}")
    else:
        print(f"\n  Example — run this on each machine (edit the name):")
        print(
            f"  claude mcp add -t http -s user "
            f'-H "Authorization: Bearer {raw_token}" '
            f'-H "X-Peer-Name: {owner}-laptop" '
            f"-- relay https://YOUR_HOST/mcp"
        )
        print(f"\n  Tip: use --url to generate a clickable join link:")
        print(
            f"  python relay.py token create --owner {owner} "
            f"--url https://your-relay.example.com"
        )

    print(f"\n  After setup, just say \"any messages?\" in Claude Code.")


def cmd_token_list(args: argparse.Namespace) -> None:
    """List all tokens (shows hashes and peer names, not raw tokens)."""
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT token_hash, owner_name, namespace, created_at, last_used_at "
        "FROM tokens ORDER BY namespace, owner_name"
    ).fetchall()
    conn.close()

    if not rows:
        print("No tokens found.")
        return

    print(
        f"{'Owner':<20} {'Namespace':<15} {'Created':<22} "
        f"{'Last Used':<22} {'Hash (first 12)'}"
    )
    print("-" * 100)
    for r in rows:
        created = ms_to_iso(r["created_at"]) if r["created_at"] else "never"
        used = ms_to_iso(r["last_used_at"]) if r["last_used_at"] else "never"
        print(
            f"{r['owner_name']:<20} {r['namespace']:<15} {created:<22} "
            f"{used:<22} {r['token_hash'][:12]}..."
        )


def cmd_token_revoke(args: argparse.Namespace) -> None:
    """Revoke a token by deleting its row.

    Also cleans up cursors for the owner and any ``{owner}-*`` session
    identities that were created under this token.
    """
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])

    owner = args.owner
    namespace = args.namespace

    deleted = conn.execute(
        "DELETE FROM tokens WHERE owner_name = ? AND namespace = ?",
        (owner, namespace),
    ).rowcount

    if deleted == 0:
        print(
            f"No token found for owner '{owner}' in namespace '{namespace}'.",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(1)

    # Cursors live under peer_name, which may be the owner or owner-<suffix>.
    conn.execute(
        "DELETE FROM cursors WHERE namespace = ? AND "
        "(peer_name = ? OR peer_name LIKE ?)",
        (namespace, owner, f"{owner}-%"),
    )
    conn.commit()
    conn.close()

    print(f"Token for owner '{owner}' in namespace '{namespace}' has been revoked.")
    print("Cursors for the owner and its sessions cleaned up.")
    print(f"\nTo re-create a token for this owner:")
    print(f"  python relay.py token create --owner {owner} --namespace {namespace}")


def cmd_check(args: argparse.Namespace) -> None:
    """Verify deployment by checking config, DB, and optionally the HTTP endpoint."""
    import sqlite3

    print("Claude Relay -- deployment check\n")

    config_path = Path("relay.config.json")
    if not config_path.exists():
        print("[FAIL] relay.config.json not found. Run: python relay.py init")
        sys.exit(1)
    print("[OK]   relay.config.json found")

    config = load_config()
    try:
        validate_config(config)
        print("[OK]   Config validation passed")
    except ValueError as e:
        print(f"[FAIL] Config validation: {e}")
        sys.exit(1)

    db_path = Path(config["db_path"])
    if not db_path.exists():
        print(f"[FAIL] Database not found at {db_path}. Run: python relay.py init")
        sys.exit(1)
    print(f"[OK]   Database found: {db_path} ({db_path.stat().st_size} bytes)")

    conn = sqlite3.connect(str(db_path))
    token_count = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
    peer_count = conn.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
    conn.close()
    print(f"[OK]   {token_count} token(s), {peer_count} peer(s)")

    if args.url:
        try:
            import urllib.request

            resp = urllib.request.urlopen(f"{args.url}/health", timeout=5)
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                print(f"[OK]   Server responding at {args.url}/health")
            else:
                print(f"[WARN] Server responded but status is not 'ok': {data}")
        except Exception as e:
            print(f"[FAIL] Could not reach server at {args.url}/health: {e}")
    else:
        print("[SKIP] No --url provided, skipping server connectivity check")

    print("\nDeployment check complete.")


def _find_cloudflared() -> str | None:
    """Return the path to cloudflared if available, else None."""
    path = shutil.which("cloudflared")
    if path:
        return path
    tmp_path = "/tmp/cloudflared"
    if os.path.isfile(tmp_path) and os.access(tmp_path, os.X_OK):
        return tmp_path
    return None


def _start_tunnel(port: int) -> tuple[subprocess.Popen | None, str | None]:
    """Start a cloudflared tunnel and wait for the public URL.

    Returns (process, tunnel_url) or (None, None) if cloudflared is unavailable.
    """
    cf_bin = _find_cloudflared()
    if cf_bin is None:
        print("\n[tunnel] cloudflared not found on PATH or at /tmp/cloudflared.")
        print("[tunnel] Install it to enable tunnels:")
        print("  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared && chmod +x /tmp/cloudflared")
        print("[tunnel] Continuing without tunnel -- server is available on localhost.\n")
        return None, None

    log.info("Starting cloudflared tunnel on port %d...", port)
    proc = subprocess.Popen(
        [cf_bin, "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait up to 15 seconds for the tunnel URL to appear in stderr
    import select
    tunnel_url = None
    deadline = time.monotonic() + 15
    partial = ""
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([proc.stderr], [], [], min(remaining, 0.5))
        if ready:
            chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else ""
            if not chunk:
                # stderr uses TextIOWrapper -- try readline
                line = proc.stderr.readline()
                if line:
                    partial += line
            else:
                partial += chunk
        else:
            # Try a non-blocking readline
            try:
                line = proc.stderr.readline()
                if line:
                    partial += line
            except Exception:
                pass

        # Look for the trycloudflare.com URL in accumulated output
        import re
        m = re.search(r"https://[a-zA-Z0-9_-]+\.trycloudflare\.com", partial)
        if m:
            tunnel_url = m.group(0)
            break

        if proc.poll() is not None:
            log.warning("cloudflared exited prematurely (code %d)", proc.returncode)
            break

    if tunnel_url:
        log.info("Tunnel URL: %s", tunnel_url)
    else:
        log.warning("Could not detect tunnel URL within 15 seconds")

    return proc, tunnel_url


def cmd_demo(args: argparse.Namespace) -> None:
    """Run a fully self-contained demo: create temp DB, tokens, and start server."""
    import sqlite3

    port = getattr(args, "port", 4444) or 4444
    use_tunnel = getattr(args, "tunnel", False)

    # Create temporary data directory
    data_dir = Path("./data/demo")
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "relay.db")

    # Initialize DB
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute("UPDATE peers SET status = 'offline'")

        # Create two demo owner tokens
        tokens = {}
        for name in ("user1", "user2"):
            raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            conn.execute(
                "INSERT OR REPLACE INTO tokens (token_hash, owner_name, namespace, created_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, name, "default", now_ms()),
            )
            tokens[name] = raw_token

        conn.commit()
    finally:
        conn.close()

    log.info("Demo DB initialized at %s with 2 tokens", db_path)

    # Build config for this demo run
    import agentic_chat.config as config_module
    config_module.CONFIG.update(DEFAULT_CONFIG)
    config_module.CONFIG["db_path"] = db_path
    config_module.CONFIG["port"] = port

    # Start tunnel if requested
    tunnel_proc = None
    tunnel_url = None
    if use_tunnel:
        tunnel_proc, tunnel_url = _start_tunnel(port)
        if tunnel_url:
            config_module.CONFIG["public_url"] = tunnel_url

    base_url = tunnel_url or f"http://localhost:{port}"

    # Print the demo box
    _print_demo_box(base_url, tokens, port)
    sys.stdout.flush()

    # Now start the server
    try:
        from .server import cmd_serve as _serve
        # Prepare a namespace for serve -- config is already loaded into CONFIG
        serve_args = argparse.Namespace(tunnel=False)  # tunnel already started
        _serve(serve_args, skip_config_load=True)
    finally:
        if tunnel_proc:
            log.info("Stopping cloudflared tunnel...")
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()


def _print_demo_box(base_url: str, tokens: dict[str, str], port: int) -> None:
    """Print a formatted demo information box."""
    mcp_url = f"{base_url}/mcp"
    dashboard_url = f"{base_url}/dashboard"

    # Each person gets ONE token. Each session picks its own peer name.
    cmd1 = (
        f'claude mcp add --transport http --scope user '
        f'-H "Authorization: Bearer {tokens["user1"]}" '
        f'-H "X-Peer-Name: user1-laptop" '
        f'-- relay {mcp_url}'
    )
    cmd2 = (
        f'claude mcp add --transport http --scope user '
        f'-H "Authorization: Bearer {tokens["user2"]}" '
        f'-H "X-Peer-Name: user2-laptop" '
        f'-- relay {mcp_url}'
    )

    lines_content = [
        "Agentic Chat Demo",
        "",
        f"  Dashboard:  {dashboard_url}",
        f"  Login token: {tokens['user1']}",
        "",
        "  -- Connect Claude Code (edit X-Peer-Name per machine) " + "-" * 6,
        "",
        f"  You (owner=user1) — paste in any terminal:",
        f"  {cmd1}",
        "",
        f"  A friend (owner=user2) — send them this command:",
        f"  {cmd2}",
        "",
        "  One token per human. Each session's X-Peer-Name must be the owner",
        "  (e.g. 'user1') or start with the owner plus a dash ('user1-laptop').",
        '  Then just say: "any messages?" in Claude Code.',
        "",
    ]

    max_len = max(len(line) for line in lines_content)
    w = max_len + 4

    def pad(text: str) -> str:
        return f"\u2551  {text}{' ' * (w - len(text) - 4)}  \u2551"

    print()
    print(f"\u2554{'=' * (w)}{'=' * 2}\u2557")
    print(pad(lines_content[0]))
    print(f"\u2560{'=' * (w)}{'=' * 2}\u2563")
    for line in lines_content[1:]:
        print(pad(line))
    print(f"\u255a{'=' * (w)}{'=' * 2}\u255d")
    print()


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the relay server (delegates to server module)."""
    from .server import cmd_serve as _serve

    use_tunnel = getattr(args, "tunnel", False)

    if use_tunnel:
        import agentic_chat.config as config_module
        config_module.CONFIG.update(load_config())
        validate_config(config_module.CONFIG)
        port = config_module.CONFIG["port"]

        tunnel_proc, tunnel_url = _start_tunnel(port)
        if tunnel_url:
            config_module.CONFIG["public_url"] = tunnel_url
            print(f"\n[tunnel] Public URL: {tunnel_url}")
            print(f"[tunnel] MCP endpoint: {tunnel_url}/mcp")
            print(f"[tunnel] Dashboard: {tunnel_url}/dashboard\n")

        try:
            _serve(args, skip_config_load=True)
        finally:
            if tunnel_proc:
                log.info("Stopping cloudflared tunnel...")
                tunnel_proc.terminate()
                try:
                    tunnel_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tunnel_proc.kill()
    else:
        _serve(args)


def cmd_agent(args: argparse.Namespace) -> None:
    """Run the autonomous agent (delegates to agent module)."""
    # Import the agent module from the repo root
    agent_path = Path(__file__).resolve().parent.parent.parent / "agent.py"
    if not agent_path.exists():
        print(f"Error: agent.py not found at {agent_path}", file=sys.stderr)
        sys.exit(1)

    import importlib.util
    spec = importlib.util.spec_from_file_location("agent", str(agent_path))
    agent_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_mod)

    cwd = str(Path(args.cwd).resolve())
    if not Path(cwd).is_dir():
        print(f"Error: working directory does not exist: {cwd}", file=sys.stderr)
        sys.exit(1)

    allowed_tools = [t.strip() for t in args.tools.split(",")]

    import asyncio

    async def _run():
        relay = agent_mod.RelayClient(args.url, args.token)
        try:
            await relay.connect()

            if not args.quiet:
                await relay.send_message(
                    "general",
                    f"Agent `{relay.peer_name}` is now online and monitoring for tasks. "
                    f"DM me or @{relay.peer_name} in any channel to assign work."
                )

            await agent_mod.agent_loop(
                relay=relay,
                cwd=cwd,
                poll_interval=args.poll_interval,
                allowed_tools=allowed_tools,
                max_turns=args.max_turns,
                model=args.model,
                watch_channels=args.watch,
            )
        except KeyboardInterrupt:
            log.info("Agent shutting down...")
            await relay.send_message(
                "general",
                f"Agent `{relay.peer_name}` is going offline.",
            )
        finally:
            await relay.close()

    asyncio.run(_run())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="Claude Relay -- message relay server for Claude Code instances",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="First-time setup")

    serve_parser = subparsers.add_parser("serve", help="Start the relay server")
    serve_parser.add_argument(
        "--tunnel", action="store_true",
        help="Start a cloudflared tunnel for public access",
    )

    demo_parser = subparsers.add_parser(
        "demo", help="One-command demo: creates DB, tokens, and starts server"
    )
    demo_parser.add_argument(
        "--port", type=int, default=4444,
        help="Port to run the demo server on (default: 4444)",
    )
    demo_parser.add_argument(
        "--tunnel", action="store_true",
        help="Start a cloudflared tunnel for public access",
    )

    token_parser = subparsers.add_parser("token", help="Token management")
    token_sub = token_parser.add_subparsers(dest="token_command")

    tc = token_sub.add_parser(
        "create",
        help="Create an owner token (sessions pick peer names via X-Peer-Name)",
    )
    tc.add_argument(
        "--owner",
        required=True,
        help=(
            "Owner name. Sessions using this token may identify as "
            "OWNER or OWNER-<suffix> via the X-Peer-Name header."
        ),
    )
    tc.add_argument(
        "--namespace", default="default", help="Namespace (default: 'default')"
    )
    tc.add_argument(
        "--url", help="Relay URL (e.g. https://relay.example.com) to generate a join link"
    )

    token_sub.add_parser("list", help="List all tokens")

    tr = token_sub.add_parser("revoke", help="Revoke an owner token")
    tr.add_argument("--owner", required=True, help="Owner name")
    tr.add_argument(
        "--namespace", default="default", help="Namespace (default: 'default')"
    )

    chk = subparsers.add_parser("check", help="Verify deployment")
    chk.add_argument(
        "--url", help="Server URL to test (e.g. https://relay.example.com)"
    )

    agent_parser = subparsers.add_parser(
        "agent",
        help="Run an autonomous agent that monitors the relay and executes tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    agent_parser.add_argument(
        "--token", required=True,
        help="Relay bearer token for this agent (relay_tok_...)",
    )
    agent_parser.add_argument(
        "--url", required=True,
        help="Relay server URL (e.g. http://localhost:4444)",
    )
    agent_parser.add_argument(
        "--cwd", default=".",
        help="Working directory for task execution (default: current directory)",
    )
    agent_parser.add_argument(
        "--poll-interval", type=float, default=3.0,
        help="Seconds between relay polls (default: 3.0)",
    )
    agent_parser.add_argument(
        "--tools", default="Read,Edit,Write,Bash,Glob,Grep",
        help="Comma-separated list of allowed Claude Code tools (default: Read,Edit,Write,Bash,Glob,Grep)",
    )
    agent_parser.add_argument(
        "--max-turns", type=int, default=15,
        help="Max agentic turns per task (default: 15)",
    )
    agent_parser.add_argument(
        "--model", default=None,
        help="Claude model to use (default: whatever Claude Code defaults to)",
    )
    agent_parser.add_argument(
        "--watch", nargs="*", default=None,
        help="Only watch specific channels (in addition to DMs and @mentions)",
    )
    agent_parser.add_argument(
        "--quiet", action="store_true",
        help="Don't announce presence on startup",
    )

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "demo":
        cmd_demo(args)
    elif args.command == "token":
        if args.token_command == "create":
            cmd_token_create(args)
        elif args.token_command == "list":
            cmd_token_list(args)
        elif args.token_command == "revoke":
            cmd_token_revoke(args)
        else:
            token_parser.print_help()
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "agent":
        cmd_agent(args)
    else:
        parser.print_help()


__all__ = ["main"]
