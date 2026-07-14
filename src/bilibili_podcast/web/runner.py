"""Validated Web process entry point."""

from __future__ import annotations

import argparse
import ipaddress
import sys

import uvicorn

from ..config import ConfigError, ConfigManager
from .server import create_app


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def _host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("host override must be a loopback IP address") from None
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("host override must be a loopback IP address")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Bilibili Podcast Web service.")
    parser.add_argument(
        "--host", type=_host,
        help="Override the configured host with a loopback IP for this process only.",
    )
    parser.add_argument("--port", type=_port, help="Override the configured port for this process only.")
    args = parser.parse_args(argv)
    try:
        manager = ConfigManager()
        snapshot = manager.load()
        app = create_app(snapshot, manager=manager)
    except (ConfigError, RuntimeError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 2)
    uvicorn.run(
        app,
        host=args.host or snapshot.web.server.host,
        port=args.port or snapshot.web.server.port,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
