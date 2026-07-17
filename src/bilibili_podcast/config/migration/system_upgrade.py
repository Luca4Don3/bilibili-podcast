"""Privileged, non-activating v4 system-state upgrade gate.

This step deliberately never creates accounts, changes groups, runs daemon
reload, or starts/enables/restarts a service.  It verifies the externally
managed state and snapshots every system file that a later explicit operator
workflow may replace.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ...secure_files import atomic_write_bytes
from ..manager import ConfigError, ConfigManager, MIGRATION_LOCK_NAME, UnsafeConfigError
from ..models import ConfigSnapshot, SystemConfigSnapshot


@dataclass(frozen=True)
class SystemFile:
    category: str
    path: Path
    sha256: str
    mode: int
    uid: int
    gid: int
    inode: int
    nlink: int


@dataclass(frozen=True)
class SystemUpgradePlan:
    root: Path
    files: tuple[SystemFile, ...]
    unit_count: int
    timer_count: int
    wrapper_count: int


@dataclass(frozen=True)
class SystemUpgradeResult:
    plan: SystemUpgradePlan
    applied: bool
    backup_id: str | None = None


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _account(name: str, label: str) -> None:
    try:
        pwd.getpwnam(name)
    except KeyError:
        raise ConfigError(f"configured {label} does not exist") from None


def _group(name: str, label: str) -> None:
    try:
        grp.getgrnam(name)
    except KeyError:
        raise ConfigError(f"configured {label} does not exist") from None


def _safe_regular(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"configured {label} must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise UnsafeConfigError(f"configured {label} contains a symlink")
    if not path.is_file():
        raise ConfigError(f"configured {label} is missing")
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UnsafeConfigError(f"configured {label} is unsafe")
    if metadata.st_size == 0:
        raise ConfigError(f"configured {label} is empty")
    return path


def _safe_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"configured {label} must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise UnsafeConfigError(f"configured {label} contains a symlink")
    if not path.is_dir():
        raise ConfigError(f"configured {label} is missing")
    return path


def _file(category: str, path: Path) -> SystemFile:
    metadata = path.stat(follow_symlinks=False)
    return SystemFile(
        category,
        path,
        _sha256(path),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_ino,
        metadata.st_nlink,
    )


def _require_dependencies(snapshot: ConfigSnapshot) -> None:
    required = (
        "systemctl",
        "crontab",
        snapshot.app.executables.ffmpeg,
        snapshot.app.executables.ffprobe,
    )
    for executable in required:
        if shutil.which(str(executable)) is None:
            raise ConfigError(
                f"required system dependency is unavailable: {Path(str(executable)).name}"
            )


def _validate_read_only_system_state(
    snapshot: ConfigSnapshot,
    units: tuple[Path, ...],
) -> None:
    result = _run(("systemctl", "show", "--property=LoadState", "--", *(path.name for path in units)))
    if result.returncode:
        raise ConfigError("cannot inspect configured systemd units")
    crontab = _run(("crontab", "-l", "-u", snapshot.scheduler.runtime.user))
    if crontab.returncode not in {0, 1}:
        raise ConfigError("cannot inspect configured service crontab")


def _load(
    root: str | Path,
    *,
    migration_locked: bool = False,
) -> tuple[ConfigSnapshot, SystemConfigSnapshot]:
    manager = ConfigManager(root, environ={})
    # This privileged planner either runs read-only or under the exclusive
    # migration lock; it must never create a lock as a side effect of preview.
    snapshot = manager.load_for_migration()
    return snapshot, manager.load_system()


def _build_plan(
    snapshot: ConfigSnapshot,
    system: SystemConfigSnapshot,
    *,
    inspect_external_state: bool,
) -> SystemUpgradePlan:
    _account(snapshot.scheduler.runtime.user, "service user")
    _group(snapshot.scheduler.runtime.group, "service group")
    _account(system.operator.user, "operator account")
    _account(system.nginx.user, "Nginx user")
    _group(system.nginx.group, "Nginx group")
    _require_dependencies(snapshot)

    systemd_dir = _safe_directory(
        snapshot.scheduler.paths.systemd_dir,
        "systemd directory",
    )
    wrapper_dir = _safe_directory(
        snapshot.scheduler.paths.wrapper_dir,
        "wrapper directory",
    )
    nginx = _safe_regular(system.nginx.config_path, "Nginx configuration")
    web = _safe_regular(
        systemd_dir / snapshot.scheduler.units.web,
        "Web systemd unit",
    )

    sync_glob = snapshot.scheduler.units.sync_glob
    units = tuple(
        sorted(
            path
            for path in systemd_dir.glob(sync_glob)
            if path.name != web.name
        )
    )
    for path in units:
        _safe_regular(path, "sync systemd unit")
    timers = tuple(
        sorted(
            path.with_suffix(".timer")
            for path in units
        )
    )
    for path in timers:
        _safe_regular(path, "sync systemd timer")
    wrappers = tuple(sorted(wrapper_dir.glob("run_*_sync.sh")))
    for path in wrappers:
        _safe_regular(path, "scheduler wrapper")

    all_units = (web, *units, *timers)
    if inspect_external_state:
        _validate_read_only_system_state(snapshot, all_units)

    files = (
        _file("nginx-config", nginx),
        *(_file("systemd-unit", path) for path in (web, *units)),
        *(_file("systemd-timer", path) for path in timers),
        *(_file("wrapper", path) for path in wrappers),
    )
    if len({item.path for item in files}) != len(files):
        raise ConfigError("system upgrade inventory contains duplicate files")
    return SystemUpgradePlan(
        snapshot.root,
        tuple(files),
        1 + len(units),
        len(timers),
        len(wrappers),
    )


def plan_system_upgrade(
    root: str | Path,
    *,
    inspect_external_state: bool = True,
    migration_locked: bool = False,
) -> SystemUpgradePlan:
    snapshot, system = _load(root, migration_locked=migration_locked)
    return _build_plan(
        snapshot,
        system,
        inspect_external_state=inspect_external_state,
    )


def _backup(plan: SystemUpgradePlan) -> tuple[Path, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"system-{stamp}-{uuid.uuid4().hex[:8]}"
    backup = plan.root / ".backups" / backup_id
    backup.mkdir(mode=0o700, parents=True)
    backup.chmod(0o700)
    inventory = []
    for index, item in enumerate(plan.files):
        payload = backup / f"{index:04d}.payload"
        payload.write_bytes(item.path.read_bytes())
        payload.chmod(0o600)
        inventory.append(
            {
                "index": index,
                "path_digest": hashlib.sha256(os.fsencode(item.path)).hexdigest(),
                "sha256": item.sha256,
                "mode": item.mode,
                "uid": item.uid,
                "gid": item.gid,
                "inode": item.inode,
                "nlink": item.nlink,
                "payload": payload.name,
            }
        )
    inventory_path = backup / "inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, separators=(",", ":")),
        encoding="utf-8",
    )
    inventory_path.chmod(0o600)
    manifest = backup / "SHA256SUMS"
    members = sorted(path for path in backup.iterdir() if path.name != manifest.name)
    manifest.write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in members),
        encoding="ascii",
    )
    manifest.chmod(0o600)
    for path in (*members, manifest):
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    descriptor = os.open(backup, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return backup, backup_id


def _load_backup(plan: SystemUpgradePlan, backup: Path) -> list[dict]:
    if (
        backup.parent.resolve() != (plan.root / ".backups").resolve()
        or not re.fullmatch(r"system-[0-9TZ]+-[0-9a-f]{8}", backup.name)
        or backup.is_symlink()
        or not backup.is_dir()
        or stat.S_IMODE(backup.stat().st_mode) != 0o700
    ):
        raise UnsafeConfigError("system backup path is unsafe")
    manifest = backup / "SHA256SUMS"
    try:
        lines = manifest.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise ConfigError("cannot read system backup manifest") from None
    expected = {"inventory.json"} | {
        f"{index:04d}.payload" for index in range(len(plan.files))
    }
    seen = set()
    for line in lines:
        try:
            digest, name = line.split("  ", 1)
        except ValueError:
            raise ConfigError("invalid system backup manifest") from None
        path = backup / name
        if name not in expected or name in seen or _sha256(path) != digest:
            raise ConfigError("system backup checksum verification failed")
        seen.add(name)
    if seen != expected:
        raise ConfigError("system backup manifest is incomplete")
    try:
        inventory = json.loads(
            (backup / "inventory.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("cannot read system backup inventory") from None
    if not isinstance(inventory, list) or len(inventory) != len(plan.files):
        raise ConfigError("invalid system backup inventory")
    for index, (record, item) in enumerate(zip(inventory, plan.files, strict=True)):
        if (
            not isinstance(record, dict)
            or record.get("index") != index
            or record.get("path_digest")
            != hashlib.sha256(os.fsencode(item.path)).hexdigest()
            or record.get("payload") != f"{index:04d}.payload"
        ):
            raise UnsafeConfigError("system backup inventory target changed")
    return inventory


def _verify_unchanged(plan: SystemUpgradePlan) -> None:
    for item in plan.files:
        metadata = item.path.stat(follow_symlinks=False)
        if (
            _sha256(item.path) != item.sha256
            or metadata.st_ino != item.inode
            or metadata.st_uid != item.uid
            or metadata.st_gid != item.gid
            or metadata.st_nlink != item.nlink
            or stat.S_IMODE(metadata.st_mode) != item.mode
        ):
            raise ConfigError("system file changed during upgrade gate")


def restore_system_backup(
    root: str | Path,
    backup_id: str,
    *,
    migration_locked: bool = False,
    sync_locked: bool = False,
) -> SystemUpgradePlan:
    """Restore system files in reverse order from a verified private backup."""
    from .versioning import _application_lock, _migration_lock

    def restore() -> SystemUpgradePlan:
        plan = plan_system_upgrade(
            root,
            inspect_external_state=False,
            migration_locked=migration_locked,
        )
        backup = plan.root / ".backups" / backup_id
        inventory = _load_backup(plan, backup)
        for record, item in reversed(
            tuple(zip(inventory, plan.files, strict=True))
        ):
            payload = backup / str(record["payload"])
            if _sha256(payload) != record.get("sha256"):
                raise ConfigError("system backup payload verification failed")
            atomic_write_bytes(
                item.path,
                payload.read_bytes(),
                mode=int(record["mode"]),
            )
            if os.geteuid() == 0:
                os.chown(item.path, int(record["uid"]), int(record["gid"]))
        return plan_system_upgrade(
            root,
            inspect_external_state=False,
            migration_locked=migration_locked,
        )

    if migration_locked and sync_locked:
        return restore()
    snapshot, _ = _load(root)
    with _migration_lock(snapshot.root):
        with _application_lock(snapshot.sync.paths.lock_file):
            return restore()


def verify_system_applied(
    root: str | Path,
    backup_id: str,
    *,
    migration_locked: bool = False,
) -> SystemUpgradePlan:
    """Revalidate system state and the backup bound to the plan."""
    plan = plan_system_upgrade(
        root,
        inspect_external_state=True,
        migration_locked=migration_locked,
    )
    _load_backup(plan, plan.root / ".backups" / backup_id)
    _verify_unchanged(plan)
    return plan


def run_system_upgrade(
    root: str | Path,
    *,
    apply: bool = False,
    plan_id: str | None = None,
) -> SystemUpgradeResult:
    if apply and not plan_id:
        raise ConfigError("system-upgrade --apply requires --plan-id")
    preview = plan_system_upgrade(root)
    if not apply:
        return SystemUpgradeResult(preview, False)

    from .versioning import (
        _application_lock,
        _migration_lock,
        _transition_loaded_state,
        load_plan,
        verify_plan_post_data,
    )

    snapshot, system = _load(root)
    with _migration_lock(snapshot.root):
        with _application_lock(snapshot.sync.paths.lock_file):
            snapshot, system = _load(root, migration_locked=True)
            state = load_plan(snapshot.root, str(plan_id))
            if state["state"] == "system_applied":
                backup_id = state.get("system_backup_id")
                if not isinstance(backup_id, str):
                    raise ConfigError("system backup reference is missing")
                plan = _build_plan(
                    snapshot,
                    system,
                    inspect_external_state=True,
                )
                _load_backup(plan, plan.root / ".backups" / backup_id)
                _verify_unchanged(plan)
                return SystemUpgradeResult(plan, True, backup_id)
            if state["state"] != "permissions_applied":
                raise ConfigError(
                    "system-upgrade apply requires a permissions_applied plan"
                )
            verify_plan_post_data(snapshot.root, state)
            plan = _build_plan(
                snapshot,
                system,
                inspect_external_state=True,
            )
            backup, backup_id = _backup(plan)
            try:
                _verify_unchanged(plan)
                _load_backup(plan, backup)
            except Exception:
                state["state"] = "failed"
                state["failure_category"] = "system"
                from .versioning import _write_plan

                _write_plan(snapshot.root, state)
                raise
            _transition_loaded_state(
                snapshot.root,
                state,
                expected="permissions_applied",
                new_state="system_applied",
                system_backup_id=backup_id,
            )
            return SystemUpgradeResult(plan, True, backup_id)
