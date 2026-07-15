from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bilibili_podcast.config import cli
from bilibili_podcast.config.manager import ConfigError, UnsafeConfigError
from bilibili_podcast.config.migration import runtime_permissions as permissions


FIXTURES = Path(__file__).parent / "fixtures" / "version_migration" / "snapshots"
OLD_PRODUCT = "bili" + "pod"


def _installation(tmp_path: Path, version: int = 3) -> Path:
    root = tmp_path / "config"
    root.mkdir()
    for source in (FIXTURES / f"v{version}").glob("*.toml"):
        content = source.read_text(encoding="utf-8")
        content = content.replace("__ROOT__", str(tmp_path)).replace("__OLD_PRODUCT__", OLD_PRODUCT)
        target = root / source.name
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
    app = (root / "app.toml").read_text(encoding="utf-8")
    database_name = f"{OLD_PRODUCT}.db" if version == 1 else "bilibili-podcast.db"
    database = tmp_path / "runtime" / "state" / database_name
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE series(series TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO series VALUES(?)", (("alpha",), ("beta",)))
    assert str(database) in app
    for name in ("media", "json"):
        runtime_root = tmp_path / "runtime" / name
        runtime_root.mkdir(parents=True)
        for series in ("alpha", "beta"):
            directory = runtime_root / series
            directory.mkdir()
            (directory / "nested").mkdir()
            (directory / "nested" / f"{series}.dat").write_text(series)
    return root


@pytest.fixture
def harmless_environment(monkeypatch):
    monkeypatch.setattr(permissions, "_require_tools", lambda **kwargs: None)
    monkeypatch.setattr(permissions, "_require_service_user", lambda user: None)
    monkeypatch.setattr(permissions, "_acl_is_compliant", lambda *args, **kwargs: False)


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def test_dry_run_writes_nothing_and_checks_files(tmp_path, harmless_environment):
    root = _installation(tmp_path)
    before = _tree_digest(tmp_path)

    result = permissions.run_runtime_permissions(root)

    assert not result.applied
    assert result.plan.series == ("alpha", "beta")
    assert result.plan.file_count == 4
    assert result.plan.directory_count == 10
    assert result.plan.noncompliant_file_count == 4
    assert _tree_digest(tmp_path) == before
    assert not (root / ".migration.lock").exists()


def test_read_only_plan_includes_committed_wal_rows(tmp_path, harmless_environment):
    root = _installation(tmp_path)
    database = tmp_path / "runtime" / "state" / "bilibili-podcast.db"
    media = tmp_path / "runtime" / "media" / "gamma"
    json_root = tmp_path / "runtime" / "json" / "gamma"
    media.mkdir()
    json_root.mkdir()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO series VALUES('gamma')")
        connection.commit()
        before = _tree_digest(tmp_path)
        plan = permissions.plan_runtime_permissions(root)
        after = _tree_digest(tmp_path)
    finally:
        connection.close()
    assert plan.series == ("alpha", "beta", "gamma")
    assert after == before


@pytest.mark.parametrize("version", (1, 2, 3))
def test_every_config_version_generates_the_same_target_set(
    tmp_path, monkeypatch, harmless_environment, version,
):
    root = _installation(tmp_path, version)
    plan = permissions.plan_runtime_permissions(root)
    relative = sorted(
        (target.kind, target.series or "", target.path.name) for target in plan.targets
    )
    assert len(relative) == 14
    assert plan.series == ("alpha", "beta")


def test_acl_parser_requires_effective_access_and_default_acl(tmp_path, monkeypatch):
    output = "\n".join((
        "user::rwx", "user:service:rwx", "mask::rwx", "group::r-x", "other::---",
        "default:user::rwx", "default:user:service:rwx", "default:mask::r-x",
        "default:group::r-x", "default:other::---",
    ))
    monkeypatch.setattr(
        permissions, "_run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    assert not permissions._acl_is_compliant(tmp_path, "service", directory=True)
    assert permissions._acl_is_compliant(tmp_path, "service", directory=False)


def test_rejects_symlink_and_hard_link(tmp_path, harmless_environment):
    root = _installation(tmp_path)
    media = tmp_path / "runtime" / "media" / "alpha"
    (media / "link").symlink_to(media / "nested")
    with pytest.raises(UnsafeConfigError, match="symlink"):
        permissions.plan_runtime_permissions(root)
    (media / "link").unlink()
    os.link(media / "nested" / "alpha.dat", media / "hard")
    with pytest.raises(UnsafeConfigError, match="hard-linked"):
        permissions.plan_runtime_permissions(root)


def test_rejects_missing_tools_and_service_account(tmp_path, monkeypatch):
    root = _installation(tmp_path)
    monkeypatch.setattr(permissions.shutil, "which", lambda name: None)
    with pytest.raises(ConfigError, match="getfacl"):
        permissions.plan_runtime_permissions(root)
    monkeypatch.setattr(permissions, "_require_tools", lambda **kwargs: None)
    monkeypatch.setattr(permissions.pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError()))
    with pytest.raises(ConfigError, match="service user"):
        permissions.plan_runtime_permissions(root)


def test_rejects_lock_conflict(tmp_path):
    lock = tmp_path / "lock"
    with permissions._exclusive_lock(lock, "occupied"):
        with pytest.raises(ConfigError, match="occupied"):
            with permissions._exclusive_lock(lock, "occupied"):
                pass


def test_timer_guard_rejects_near_and_unparseable_timer(monkeypatch):
    near = int((time.time() + 299) * 1_000_000)
    monkeypatch.setattr(
        permissions, "_run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f'[{{"unit":"bilibili-podcast-sync@alpha.timer","next":{near}}}]',
            stderr="",
        ),
    )
    assert not permissions._timer_window_is_safe()
    monkeypatch.setattr(
        permissions, "_run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='[{"unit":"bilibili-podcast-sync@alpha.timer","next":"unknown"}]',
            stderr="",
        ),
    )
    with pytest.raises(ConfigError, match="interpret"):
        permissions._timer_window_is_safe()


def test_rejects_unsafe_backup_scope(tmp_path, harmless_environment):
    plan = permissions.plan_runtime_permissions(_installation(tmp_path))
    with pytest.raises(UnsafeConfigError, match="outside"):
        permissions._backup_path(plan, tmp_path / "elsewhere" / "permissions-fake")


def test_backup_manifest_checksum_and_modes(tmp_path):
    backup = tmp_path / "permissions-test"
    backup.mkdir(mode=0o700)
    for name, content in (("acl.restore", "# file: /fixture\n"), ("inventory.json", "[]")):
        path = backup / name
        path.write_text(content)
        path.chmod(0o600)
    permissions._write_manifest(backup)
    permissions._verify_manifest(backup)
    assert stat_mode(backup) == 0o700
    assert all(stat_mode(backup / name) == 0o600 for name in ("acl.restore", "inventory.json", "SHA256SUMS"))
    (backup / "inventory.json").write_text("tampered")
    with pytest.raises(ConfigError, match="checksum"):
        permissions._verify_manifest(backup)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_cli_text_and_json_do_not_expose_runtime_paths(tmp_path, monkeypatch, capsys):
    root = _installation(tmp_path)
    target = permissions.PermissionTarget("file", "alpha", tmp_path / "private" / "secret.dat", False)
    plan = permissions.PermissionPlan(
        root, "service", tmp_path / "private-media", tmp_path / "private-json",
        tmp_path / "private.lock", ("alpha",), (target,),
    )
    monkeypatch.setattr(
        cli, "run_runtime_permissions",
        lambda *args, **kwargs: permissions.PermissionResult(plan, False),
    )
    assert cli.main(["--root", str(root), "permissions"]) == 0
    text_output = capsys.readouterr().out
    assert str(tmp_path) not in text_output
    assert "alpha" in text_output
    assert cli.main(["--root", str(root), "permissions", "--format", "json"]) == 0
    json_output = capsys.readouterr().out
    assert str(tmp_path) not in json_output


def test_apply_failure_runs_verified_rollback(tmp_path, monkeypatch, harmless_environment):
    plan = permissions.plan_runtime_permissions(_installation(tmp_path))
    backup = plan.root / ".backups" / "permissions-test"
    calls: list[str] = []
    monkeypatch.setattr(permissions, "plan_runtime_permissions", lambda root: plan)
    monkeypatch.setattr(permissions, "_timer_window_is_safe", lambda: True)
    monkeypatch.setattr(permissions, "_create_backup", lambda value: backup)
    monkeypatch.setattr(permissions, "_load_inventory", lambda value: [])
    monkeypatch.setattr(
        permissions, "_apply_acl",
        lambda value: (_ for _ in ()).throw(ConfigError("injected apply failure")),
    )
    monkeypatch.setattr(
        permissions, "_restore_backup",
        lambda *args, **kwargs: calls.append("restored"),
    )
    with pytest.raises(ConfigError, match="injected apply failure"):
        permissions.run_runtime_permissions(plan.root, apply=True)
    assert calls == ["restored"]


def test_verification_failure_runs_rollback(tmp_path, monkeypatch, harmless_environment):
    plan = permissions.plan_runtime_permissions(_installation(tmp_path))
    backup = plan.root / ".backups" / "permissions-test"
    calls: list[str] = []
    monkeypatch.setattr(permissions, "plan_runtime_permissions", lambda root: plan)
    monkeypatch.setattr(permissions, "_timer_window_is_safe", lambda: True)
    monkeypatch.setattr(permissions, "_create_backup", lambda value: backup)
    monkeypatch.setattr(permissions, "_load_inventory", lambda value: [])
    monkeypatch.setattr(permissions, "_apply_acl", lambda value: None)
    monkeypatch.setattr(
        permissions, "_verify_applied",
        lambda *args: (_ for _ in ()).throw(ConfigError("injected verification failure")),
    )
    monkeypatch.setattr(
        permissions, "_restore_backup",
        lambda *args, **kwargs: calls.append("restored"),
    )
    with pytest.raises(ConfigError, match="injected verification failure"):
        permissions.run_runtime_permissions(plan.root, apply=True)
    assert calls == ["restored"]
