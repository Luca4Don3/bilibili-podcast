"""Pure helpers for parsing Bilibili URLs without API or database access."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


_UID_RE = re.compile(r"\d{5,}")


def parse_space_source(text: str | None) -> dict[str, Any] | None:
    """Return a canonical space source for a plain UID or supported space URL."""
    value = (text or "").strip()
    if not value:
        return None

    if _UID_RE.fullmatch(value):
        uid = int(value)
    else:
        candidate = value if "://" in value else f"//{value}"
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return None
        if parsed.username or parsed.password:
            return None

        host = (parsed.hostname or "").lower()
        path_parts = [part for part in parsed.path.split("/") if part]
        if host == "space.bilibili.com" and len(path_parts) == 1:
            uid_text = path_parts[0]
        elif host == "www.bilibili.com" and len(path_parts) == 2 and path_parts[0] == "space":
            uid_text = path_parts[1]
        else:
            return None
        if not _UID_RE.fullmatch(uid_text):
            return None
        uid = int(uid_text)

    return {
        "type": "space",
        "uid": uid,
        "sid": None,
        "space_url": f"https://space.bilibili.com/{uid}",
    }
