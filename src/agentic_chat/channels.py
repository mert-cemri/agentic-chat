"""Channel and peer name validation, DM/group normalization, channel types."""

import re

# Channel names: alphanumeric + hyphens, 1-64 chars
CHANNEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")

# Peer names: alphanumeric + underscores + hyphens, 1-32 chars
PEER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")


# -- Channel type detection --


def channel_type(channel: str) -> str:
    """Determine the type of a channel from its name.

    Returns one of:
        'broadcast'  — #general or any channel without a special prefix
        'dm'         — dm-alice-bob (2 participants)
        'group'      — group-alice-bob-carol (3+ participants)
        'self'       — self-alice (personal notes channel)
    """
    lower = channel.lower()
    if lower.startswith("dm-"):
        return "dm"
    if lower.startswith("group-"):
        return "group"
    if lower.startswith("self-"):
        return "self"
    return "broadcast"


def is_dm_channel(channel: str) -> bool:
    """Check if a channel name is a DM channel (case-insensitive prefix)."""
    return channel.lower().startswith("dm-")


def is_group_channel(channel: str) -> bool:
    """Check if a channel name is a group DM channel."""
    return channel.lower().startswith("group-")


def is_self_channel(channel: str) -> bool:
    """Check if a channel name is a personal/self channel."""
    return channel.lower().startswith("self-")


# -- Normalization --


def normalize_channel(channel: str, caller_name: str | None = None) -> tuple[str, str | None]:
    """Normalize channel name. Returns (normalized_name, error_or_None).

    Channel types and their normalization:

    dm-<name1>-<name2>
        Two-person DM. Names are sorted alphabetically and lowercased.
        Both orderings resolve to the same channel.

    group-<name1>-<name2>-<name3>-...
        Group DM. All names sorted and lowercased. Any number of participants.
        Duplicate names are removed.

    self-<name>
        Personal notes channel. Only meaningful to the named peer.
        If caller_name is provided, auto-creates as self-{caller_name}.

    <anything else>
        Regular broadcast channel. Returned as-is (no normalization).
    """
    lower = channel.lower()

    # -- Self channel --
    if lower.startswith("self-"):
        rest = channel[5:]
        if not rest and caller_name:
            return f"self-{caller_name.lower()}", None
        if not rest:
            return channel, "Self channel must include your name: self-yourname"
        return f"self-{rest.lower()}", None

    # -- Group channel --
    if lower.startswith("group-"):
        rest = channel[6:]
        if not rest:
            return channel, "Group channel must include participant names: group-alice-bob-carol"

        parts = rest.split("-")
        parts = [p.strip().lower() for p in parts if p.strip()]

        if len(parts) < 2:
            return channel, "Group channel needs at least 2 participants: group-alice-bob"

        # Remove duplicates, sort
        unique_sorted = sorted(set(parts))
        normalized = "group-" + "-".join(unique_sorted)
        return normalized, None

    # -- DM channel --
    if lower.startswith("dm-"):
        rest = channel[3:]
        if not rest:
            return channel, "DM channel must have exactly two peer names: dm-name1-name2"

        parts = rest.split("-")
        if len(parts) != 2:
            # Maybe they meant a group DM? Suggest the correct prefix.
            if len(parts) > 2:
                suggested = "group-" + "-".join(sorted(p.lower() for p in parts if p))
                return channel, (
                    f"DM channels are for exactly 2 people. "
                    f"For {len(parts)} people, use a group channel: {suggested}"
                )
            return channel, "DM channel must have exactly two peer names: dm-name1-name2"

        if not parts[0] or not parts[1]:
            return channel, "DM peer names cannot be empty: dm-name1-name2"

        sorted_parts = sorted(p.lower() for p in parts)
        normalized = f"dm-{sorted_parts[0]}-{sorted_parts[1]}"
        return normalized, None

    # -- Broadcast channel (no normalization) --
    return channel, None


__all__ = [
    "CHANNEL_NAME_RE",
    "PEER_NAME_RE",
    "channel_type",
    "is_dm_channel",
    "is_group_channel",
    "is_self_channel",
    "normalize_channel",
]
