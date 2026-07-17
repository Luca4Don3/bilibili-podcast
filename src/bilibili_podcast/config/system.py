"""Restricted operator/Nginx manifest managed inside the config root."""

from __future__ import annotations

import json
import os
import pwd
import stat
import tomllib
from pathlib import Path
from typing import Any

from ..secure_files import atomic_write_bytes
from .models import NginxSystemConfig, OperatorConfig, SystemConfigSnapshot


SYSTEM_CONFIG_NAME = "system.toml"


def _error(message: str):
    from .manager import ConfigError

    return ConfigError(message)


def _unsafe(message: str):
    from .manager import UnsafeConfigError

    return UnsafeConfigError(message)


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        if path.suffix.lower() == ".json":
            value = json.loads(payload)
        else:
            value = tomllib.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _error(f"cannot read system manifest: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise _error("invalid system manifest")
    return value


def _validate_source(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise _unsafe("system manifest is missing or unsafe")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
        raise _unsafe("system manifest must be a single-link 0600 file")


def _snapshot(root: Path, value: dict[str, Any]) -> SystemConfigSnapshot:
    if set(value) != {"operator", "nginx"}:
        raise _error("system manifest must contain only operator and nginx")
    operator = value["operator"]
    nginx = value["nginx"]
    if not isinstance(operator, dict) or set(operator) != {"user"}:
        raise _error("invalid system manifest operator")
    expected_nginx = {
        "user",
        "group",
        "config_path",
        "access_log_path",
        "error_log_path",
    }
    if not isinstance(nginx, dict) or set(nginx) != expected_nginx:
        raise _error("invalid system manifest nginx")
    strings = (
        operator["user"],
        nginx["user"],
        nginx["group"],
        nginx["config_path"],
        nginx["access_log_path"],
        nginx["error_log_path"],
    )
    if any(
        not isinstance(item, str)
        or not item
        or any(ord(character) < 32 for character in item)
        for item in strings
    ):
        raise _error("invalid system manifest value")
    paths = tuple(Path(item).expanduser() for item in strings[3:])
    if any(not path.is_absolute() for path in paths):
        raise _error("system manifest paths must be absolute")
    return SystemConfigSnapshot(
        root=root,
        operator=OperatorConfig(operator["user"]),
        nginx=NginxSystemConfig(
            nginx["user"],
            nginx["group"],
            paths[0],
            paths[1],
            paths[2],
        ),
    )


def _serialize(snapshot: SystemConfigSnapshot) -> bytes:
    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return (
        "[operator]\n"
        f"user = {quote(snapshot.operator.user)}\n\n"
        "[nginx]\n"
        f"user = {quote(snapshot.nginx.user)}\n"
        f"group = {quote(snapshot.nginx.group)}\n"
        f"config_path = {quote(str(snapshot.nginx.config_path))}\n"
        f"access_log_path = {quote(str(snapshot.nginx.access_log_path))}\n"
        f"error_log_path = {quote(str(snapshot.nginx.error_log_path))}\n"
    ).encode("utf-8")


def load_system_config(root: str | Path) -> SystemConfigSnapshot:
    config_root = Path(root).expanduser().resolve()
    path = config_root / SYSTEM_CONFIG_NAME
    _validate_source(path)
    snapshot = _snapshot(config_root, _read_mapping(path))
    try:
        operator_uid = pwd.getpwnam(snapshot.operator.user).pw_uid
    except KeyError:
        raise _error("configured operator account does not exist") from None
    if path.stat().st_uid != operator_uid:
        raise _unsafe("system.toml is not owned by the configured operator")
    return snapshot


def import_system_manifest(root: str | Path, source: str | Path) -> SystemConfigSnapshot:
    config_root = Path(root).expanduser().resolve()
    source_path = Path(source).expanduser()
    _validate_source(source_path)
    snapshot = _snapshot(config_root, _read_mapping(source_path))
    try:
        operator_uid = pwd.getpwnam(snapshot.operator.user).pw_uid
    except KeyError:
        raise _error("configured operator account does not exist") from None
    target = config_root / SYSTEM_CONFIG_NAME
    atomic_write_bytes(target, _serialize(snapshot), mode=0o600)
    if os.geteuid() == 0 and target.stat().st_uid != operator_uid:
        os.chown(target, operator_uid, -1)
    return load_system_config(config_root)
