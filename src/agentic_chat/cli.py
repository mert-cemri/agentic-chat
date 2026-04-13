"""CLI: init, token create/list/revoke, check, main()."""

import argparse
import hashlib
import json
import logging
import secrets
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config, validate_config, now_ms, ms_to_iso
from .channels import PEER_NAME_RE
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

        # Create operator's peer token
        raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        conn.execute(
            "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
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
    print(f"\nNote: All relay administration is via the CLI on this machine.")
    print(f"\nTo create a peer token:")
    print(f"  python relay.py token create --name shubham --namespace {namespace}")
    print(f"\nTo start the server:")
    print(f"  python relay.py serve")


def cmd_token_create(args: argparse.Namespace) -> None:
    """Generate a new peer token."""
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])

    name = args.name
    namespace = args.namespace

    if not PEER_NAME_RE.match(name):
        print(
            f"Error: peer name must match {PEER_NAME_RE.pattern}", file=sys.stderr
        )
        sys.exit(1)

    raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    try:
        conn.execute(
            "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, name, namespace, now_ms()),
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

    print(f"\nToken for '{name}' in namespace '{namespace}':")
    print(f"  {raw_token}")

    if relay_url:
        join_link = f"{relay_url.rstrip('/')}/join/{raw_token}"
        print(f"\nSend this link to {name}:")
        print(f"  {join_link}")
        print(f"\nThey open it in a browser, copy one command, done.")
    else:
        print(f"\nGive them this command to connect:")
        print(f"  claude mcp add --transport http \\")
        print(f'    --header "Authorization: Bearer {raw_token}" \\')
        print(f"    -- relay https://YOUR_RELAY_HOST/mcp")
        print(f"\n  (the `--` is REQUIRED -- --header is variadic and will")
        print(f"   otherwise eat the positional 'relay' argument)")
        print(f"\n  Tip: use --url to generate a clickable join link:")
        print(f"  python relay.py token create --name {name} --url https://relay.example.com")

    print(f"\nPost-setup: tell them to say 'check the relay' in Claude Code.")


def cmd_token_list(args: argparse.Namespace) -> None:
    """List all tokens (shows hashes and peer names, not raw tokens)."""
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT token_hash, peer_name, namespace, created_at, last_used_at "
        "FROM tokens ORDER BY namespace, peer_name"
    ).fetchall()
    conn.close()

    if not rows:
        print("No tokens found.")
        return

    print(
        f"{'Peer':<20} {'Namespace':<15} {'Created':<22} "
        f"{'Last Used':<22} {'Hash (first 12)'}"
    )
    print("-" * 100)
    for r in rows:
        created = ms_to_iso(r["created_at"]) if r["created_at"] else "never"
        used = ms_to_iso(r["last_used_at"]) if r["last_used_at"] else "never"
        print(
            f"{r['peer_name']:<20} {r['namespace']:<15} {created:<22} "
            f"{used:<22} {r['token_hash'][:12]}..."
        )


def cmd_token_revoke(args: argparse.Namespace) -> None:
    """Revoke a token by deleting its row. Also cleans up cursors."""
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])

    name = args.name
    namespace = args.namespace

    deleted = conn.execute(
        "DELETE FROM tokens WHERE peer_name = ? AND namespace = ?",
        (name, namespace),
    ).rowcount

    if deleted == 0:
        print(
            f"No token found for '{name}' in namespace '{namespace}'.",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(1)

    conn.execute(
        "DELETE FROM cursors WHERE peer_name = ? AND namespace = ?",
        (name, namespace),
    )
    conn.commit()
    conn.close()

    print(f"Token for '{name}' in namespace '{namespace}' has been revoked.")
    print("Cursors cleaned up. The peer can no longer authenticate.")
    print(f"\nTo re-create a token for this peer:")
    print(f"  python relay.py token create --name {name} --namespace {namespace}")


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


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the relay server (delegates to server module)."""
    from .server import cmd_serve as _serve
    _serve(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="Claude Relay -- message relay server for Claude Code instances",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="First-time setup")
    subparsers.add_parser("serve", help="Start the relay server")

    token_parser = subparsers.add_parser("token", help="Token management")
    token_sub = token_parser.add_subparsers(dest="token_command")

    tc = token_sub.add_parser("create", help="Create a peer token")
    tc.add_argument("--name", required=True, help="Peer name")
    tc.add_argument(
        "--namespace", default="default", help="Namespace (default: 'default')"
    )
    tc.add_argument(
        "--url", help="Relay URL (e.g. https://relay.example.com) to generate a join link"
    )

    token_sub.add_parser("list", help="List all tokens")

    tr = token_sub.add_parser("revoke", help="Revoke a peer token")
    tr.add_argument("--name", required=True, help="Peer name")
    tr.add_argument(
        "--namespace", default="default", help="Namespace (default: 'default')"
    )

    chk = subparsers.add_parser("check", help="Verify deployment")
    chk.add_argument(
        "--url", help="Server URL to test (e.g. https://relay.example.com)"
    )

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "serve":
        cmd_serve(args)
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
    else:
        parser.print_help()


__all__ = ["main"]
