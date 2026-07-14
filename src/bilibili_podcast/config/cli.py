"""``bilibili-podcast-config`` command line interface."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .manager import ConfigError, ConfigManager
from .migration import migrate_legacy, upgrade_installation
from .repositories import SQLiteSeriesRepository


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _flatten(data: dict[str, Any], prefix: str = ""):
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _flatten(value, path)
        else:
            yield path, value


def _manager(args: argparse.Namespace) -> ConfigManager:
    return ConfigManager(args.root) if args.root else ConfigManager()


def cmd_validate(args: argparse.Namespace) -> int:
    snapshot = _manager(args).load(templates=args.templates)
    print(f"valid configuration: {snapshot.root}")
    if not args.templates:
        try:
            count = SQLiteSeriesRepository(snapshot.app.database.path).access_rule_count()
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        if count:
            print(f"legacy status: access_rule contains {count} row(s); semantics were preserved")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    manager = _manager(args)
    data = manager.redacted(scope=args.scope)
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default))
    else:
        for key, value in _flatten(data):
            print(f"{key} = {value}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    result = migrate_legacy(
        legacy_env=args.legacy_env,
        legacy_web_env=args.legacy_web_env,
        legacy_series_dir=args.legacy_series_dir,
        legacy_rss_users=args.legacy_rss_users,
        output_root=args.output_root,
        apply=args.apply,
    )
    action = "applied" if result.applied else "dry-run"
    print(f"migration {action}: {len(result.files)} config files, {result.series_count} series")
    for item in result.normalizations:
        print(f"normalized: {item}")
    if not result.applied:
        print("no files were written; rerun with --apply")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    if not args.root:
        raise ConfigError("upgrade requires --root")
    result = upgrade_installation(args.root, apply=args.apply)
    action = "applied" if result.applied else "dry-run"
    plan = result.plan
    print(
        f"upgrade {action}: version {plan.source_version} -> {plan.target_version}; "
        f"{len(plan.steps)} step(s)"
    )
    for step in plan.steps:
        print(f"step: {step}")
    if result.backup_root is not None:
        print(f"backup: {result.backup_root}")
    if not result.applied and plan.steps:
        print("no files were written; rerun with --apply")
    return 0


def _exec_environment(snapshot, scope: str) -> dict[str, str]:
    env = dict(os.environ)
    env["BILIBILI_PODCAST_CONFIG_ROOT"] = str(snapshot.root)
    env["BILIBILI_PODCAST_INTERNAL_CONFIG_EXEC"] = "1"
    if scope in {"sync", "web", "scheduler"}:
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(snapshot.sync.browser.playwright_browsers_path)
    return env


def cmd_exec(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ConfigError("exec requires COMMAND")
    snapshot = _manager(args).load()
    try:
        result = subprocess.run(command, env=_exec_environment(snapshot, args.scope))
    except OSError as exc:
        raise ConfigError(f"cannot execute command: {type(exc).__name__}") from None
    return 128 + abs(result.returncode) if result.returncode < 0 else result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and migrate Bilibili Podcast unified configuration.")
    parser.add_argument("--root", help="Configuration root (normally BILIBILI_PODCAST_CONFIG_ROOT).")
    subparsers = parser.add_subparsers(dest="action", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--templates", action="store_true")
    validate.set_defaults(handler=cmd_validate)
    show = subparsers.add_parser("show")
    show.add_argument("--scope", choices=("app", "sync", "web", "scheduler", "publish", "manual-media", "rss-users"))
    show.add_argument("--format", choices=("text", "json"), default="text")
    show.set_defaults(handler=cmd_show)
    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("--legacy-env", required=True)
    migrate.add_argument("--legacy-web-env", required=True)
    migrate.add_argument("--legacy-series-dir", required=True)
    migrate.add_argument("--legacy-rss-users", required=True)
    migrate.add_argument("--output-root", required=True)
    migrate.add_argument("--apply", action="store_true")
    migrate.set_defaults(handler=cmd_migrate)
    upgrade = subparsers.add_parser("upgrade")
    upgrade.add_argument("--apply", action="store_true")
    upgrade.set_defaults(handler=cmd_upgrade)
    execute = subparsers.add_parser("exec")
    execute.add_argument("--scope", choices=("sync", "web", "scheduler", "publish"), required=True)
    execute.add_argument("command", nargs=argparse.REMAINDER)
    execute.set_defaults(handler=cmd_exec)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
