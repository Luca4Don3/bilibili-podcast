"""Validated Web process entry point."""

from __future__ import annotations

import sys

import uvicorn

from ..config import ConfigError, ConfigManager
from .server import create_app


def main() -> int:
    try:
        manager = ConfigManager()
        snapshot = manager.load()
        app = create_app(snapshot, manager=manager)
    except (ConfigError, RuntimeError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 2)
    uvicorn.run(
        app, host=snapshot.web.server.host, port=snapshot.web.server.port,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
