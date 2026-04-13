"""Channel and peer name validation and DM normalization."""

import re

CHANNEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")
PEER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")


def is_dm_channel(channel: str) -> bool:
    """Check if a channel name is a DM channel (case-insensitive prefix)."""
    return channel.lower().startswith("dm-")


def normalize_channel(channel: str) -> tuple[str, str | None]:
    """Normalize DM channel name ordering. No access control.
    Returns (normalized_name, error_or_None).

    DM format: dm-<name1>-<name2> where names cannot contain hyphens.
    Peers with hyphens in their names should use underscores in DM channels.
    The entire channel name is lowercased (prefix + peer names) so that
    'DM-Alice-Bob' and 'dm-alice-bob' collapse to the same channel.
    """
    if not is_dm_channel(channel):
        return channel, None

    # Strip prefix regardless of its case
    rest = channel[3:]
    if not rest:
        return channel, "DM channel must have exactly two peer names: dm-name1-name2"

    parts = rest.split("-")
    if len(parts) != 2:
        return channel, (
            "DM channel must have exactly two peer names separated by a single hyphen: "
            "dm-name1-name2. Peer names in DMs cannot contain hyphens."
        )

    if not parts[0] or not parts[1]:
        return channel, "DM peer names cannot be empty: dm-name1-name2"

    sorted_parts = sorted(p.lower() for p in parts)
    normalized = f"dm-{sorted_parts[0]}-{sorted_parts[1]}"
    return normalized, None


__all__ = [
    "CHANNEL_NAME_RE",
    "PEER_NAME_RE",
    "is_dm_channel",
    "normalize_channel",
]
