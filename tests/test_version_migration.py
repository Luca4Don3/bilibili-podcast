from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from bilibili_podcast.config.manager import ConfigError, UnsafeConfigError
from bilibili_podcast.config import cli as config_cli
from bilibili_podcast.config.migration import (
    LATEST_VERSION,
    VERSION_FILE,
    detect_version,
    plan_upgrade,
    read_legacy_env,
    upgrade_installation,
)
from bilibili_podcast.config.migration import versioning


FIXTURES = Path(__file__).parent / "fixtures" / "version_migration"
OLD_PRODUCT = "bili" + "pod"


def _fixture_version(name: str) -> int:
    with (FIXTURES / f"{name}.toml").open("rb") as handle:
        return int(tomllib.load(handle)["fixture"]["version"])


def _installation(tmp_path: Path, fixture: str, *, database: bool = True) -> Path:
    root = tmp_path / "config"
    root.mkdir()
    server_root = tmp_path / "runtime"
    snapshot_version = fixture
    snapshot_root = FIXTURES / "snapshots" / snapshot_version
    for source in sorted(snapshot_root.glob("*.toml")):
        content = source.read_text(encoding="utf-8")
        content = content.replace("__ROOT__", str(tmp_path))
        content = content.replace("__OLD_PRODUCT__", OLD_PRODUCT)
        target = root / source.name
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)

    version = _fixture_version(fixture)
    if version == LATEST_VERSION:
        (root / VERSION_FILE).write_text(f"{LATEST_VERSION}\n", encoding="ascii")
        (root / VERSION_FILE).chmod(0o600)

    if database:
        database_path = server_root / "state" / "bilibili-podcast.db"
        if version == 1:
            database_path = server_root / "state" / f"{OLD_PRODUCT}.db"
        database_path.parent.mkdir(parents=True)
        with sqlite3.connect(database_path) as connection:
            connection.executescript(
                (FIXTURES / "snapshots" / "schema-v1-v3.sql").read_text(encoding="utf-8")
            )
            if version == LATEST_VERSION:
                connection.execute("INSERT INTO schema_version(version) VALUES(?)", (LATEST_VERSION,))
    return root


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


@pytest.mark.parametrize(("fixture", "source"), (("v1", 1), ("v2", 2), ("v3", 3)))
def test_detects_every_historical_fixture(tmp_path, fixture, source):
    assert detect_version(_installation(tmp_path, fixture)) == source


def test_v1_upgrades_across_every_step_and_preserves_quoted_user(tmp_path):
    root = _installation(tmp_path, "v1")
    users = root / "rss-users.toml"
    preserved_token = f"credential-{OLD_PRODUCT}-must-not-change"
    users.write_text(
        f'[users."fixture.user"]\ntoken = "{preserved_token}"\nseries = ["series-one"]\n'
    )

    result = upgrade_installation(root, apply=True)

    assert result.plan.source_version == 1
    assert len(result.plan.steps) == 2
    assert detect_version(root) == LATEST_VERSION
    with users.open("rb") as handle:
        migrated_user = tomllib.load(handle)["users"]["fixture.user"]
    assert migrated_user["series"] == ["series-one"]
    assert migrated_user["token"] == preserved_token
    with (root / "web.toml").open("rb") as handle:
        security = tomllib.load(handle)["security"]
    assert security["cookie_name"] == "bilibili_podcast_session"
    assert f"{OLD_PRODUCT}_session" in security["previous_cookie_names"]
    assert security["password"] == f"fixture-password-{OLD_PRODUCT}-preserved"
    app = tomllib.loads((root / "app.toml").read_text())["database"]
    executables = tomllib.loads((root / "app.toml").read_text())["executables"]
    assert executables["sync"] == str(tmp_path / "venv" / "bin" / "bilibili-podcast")
    with sqlite3.connect(app["path"]) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(LATEST_VERSION,)]
    assert (tmp_path / "runtime" / "state" / f"{OLD_PRODUCT}.db").exists()
    assert app["path"].endswith(f"/{OLD_PRODUCT}.db")
    assert result.backup_root is not None
    manifest = result.backup_root / "SHA256SUMS"
    for line in manifest.read_text(encoding="ascii").splitlines():
        expected, name = line.split("  ", 1)
        assert hashlib.sha256((result.backup_root / name).read_bytes()).hexdigest() == expected


def test_v1_upgrade_preserves_custom_cookie_as_compatibility_name(tmp_path):
    root = _installation(tmp_path, "v1")
    web = root / "web.toml"
    web.write_text(
        web.read_text(encoding="utf-8").replace(
            f'cookie_name = "{OLD_PRODUCT}_session"',
            'cookie_name = "custom_legacy_session"',
        ),
        encoding="utf-8",
    )

    upgrade_installation(root, apply=True)

    security = tomllib.loads(web.read_text(encoding="utf-8"))["security"]
    assert security["cookie_name"] == "bilibili_podcast_session"
    assert security["previous_cookie_names"] == ["custom_legacy_session"]


def test_v2_upgrades_to_latest_and_sets_database_version(tmp_path):
    root = _installation(tmp_path, "v2")
    result = upgrade_installation(root, apply=True)
    assert result.plan.steps == ("initialize-versioned-installation",)
    app = tomllib.loads((root / "app.toml").read_text())["database"]
    with sqlite3.connect(app["path"]) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(LATEST_VERSION,)]


def test_upgrade_keeps_database_inode_for_live_blue_green_connections(tmp_path):
    root = _installation(tmp_path, "v2")
    app = tomllib.loads((root / "app.toml").read_text())["database"]
    database_path = Path(app["path"])
    inode = database_path.stat().st_ino
    old_connection = sqlite3.connect(database_path)
    try:
        upgrade_installation(root, apply=True)
        assert database_path.stat().st_ino == inode
        old_connection.execute(
            "INSERT INTO schema_version(version) VALUES(?) ON CONFLICT(version) DO NOTHING",
            (LATEST_VERSION,),
        )
        old_connection.commit()
        with sqlite3.connect(database_path) as new_connection:
            assert new_connection.execute(
                "SELECT version FROM schema_version"
            ).fetchall() == [(LATEST_VERSION,)]
    finally:
        old_connection.close()


def test_historical_v3_step_does_not_follow_future_latest_constant(tmp_path, monkeypatch):
    root = _installation(tmp_path, "v2")
    monkeypatch.setattr(versioning, "LATEST_VERSION", 4)

    upgrade_installation(root, apply=True, target_version=3)

    assert (root / VERSION_FILE).read_text(encoding="ascii").strip() == "3"


def test_latest_is_idempotent(tmp_path):
    root = _installation(tmp_path, "v3")
    before = _digest_tree(tmp_path)
    result = upgrade_installation(root, apply=True)
    assert result.plan.steps == ()
    assert result.backup_root is None
    assert _digest_tree(tmp_path) == before


def test_database_created_after_upgrade_gets_current_version(tmp_path):
    from bilibili_podcast import db

    root = _installation(tmp_path, "v2", database=False)
    upgrade_installation(root, apply=True)
    database_path = Path(tomllib.loads((root / "app.toml").read_text())["database"]["path"])

    db.migrate(database_path)

    assert detect_version(root) == LATEST_VERSION


def test_dry_run_writes_nothing(tmp_path):
    root = _installation(tmp_path, "v1")
    before = _digest_tree(tmp_path)
    result = upgrade_installation(root)
    assert not result.applied
    assert _digest_tree(tmp_path) == before


@pytest.mark.parametrize("marker", ("broken", "999"))
def test_rejects_corrupt_and_future_markers(tmp_path, marker):
    root = _installation(tmp_path, "v2")
    (root / VERSION_FILE).write_text(marker)
    with pytest.raises(ConfigError):
        detect_version(root)


def test_rejects_dangling_version_marker_symlink(tmp_path):
    root = _installation(tmp_path, "v2")
    (root / VERSION_FILE).symlink_to(root / "missing-version")
    with pytest.raises(UnsafeConfigError, match="version marker.*symlink"):
        detect_version(root)


def test_rejects_symlinked_version_target_before_apply(tmp_path):
    root = _installation(tmp_path, "v2")
    linked = tmp_path / "linked-sync.toml"
    linked.write_text((root / "sync.toml").read_text(), encoding="utf-8")
    (root / "sync.toml").unlink()
    (root / "sync.toml").symlink_to(linked)
    with pytest.raises(UnsafeConfigError, match="unsafe migration source"):
        upgrade_installation(root, apply=True)


def test_rejects_symlinked_database(tmp_path):
    root = _installation(tmp_path, "v2")
    app = tomllib.loads((root / "app.toml").read_text())
    database_path = Path(app["database"]["path"])
    linked = tmp_path / "linked.db"
    linked.write_bytes(database_path.read_bytes())
    database_path.unlink()
    database_path.symlink_to(linked)
    with pytest.raises(UnsafeConfigError, match="migration database.*symlink"):
        detect_version(root)


def test_rejects_incomplete_installation(tmp_path):
    root = _installation(tmp_path, "v2")
    (root / "sync.toml").rename(root / "sync.toml.missing")
    with pytest.raises(ConfigError, match="missing sync.toml"):
        detect_version(root)


def test_rejects_marker_database_version_mismatch(tmp_path):
    root = _installation(tmp_path, "v3")
    app = tomllib.loads((root / "app.toml").read_text())["database"]
    with sqlite3.connect(app["path"]) as connection:
        connection.execute("UPDATE schema_version SET version=2")
    with pytest.raises(ConfigError, match="version mismatch"):
        detect_version(root)


def test_rejects_missing_registered_step(tmp_path, monkeypatch):
    root = _installation(tmp_path, "v1")
    monkeypatch.delitem(versioning._STEPS, 2)
    with pytest.raises(ConfigError, match="missing migration step"):
        plan_upgrade(root)


def test_lock_conflict_is_explicit(tmp_path):
    root = _installation(tmp_path, "v2")
    with (root / ".migration.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ConfigError, match="another installation migration"):
            upgrade_installation(root, apply=True)


def test_rejects_symlinked_migration_lock(tmp_path):
    root = _installation(tmp_path, "v2")
    linked = tmp_path / "migration-lock"
    linked.write_text("", encoding="utf-8")
    (root / ".migration.lock").symlink_to(linked)
    with pytest.raises(UnsafeConfigError, match="migration lock.*symlink"):
        upgrade_installation(root, apply=True)


def test_apply_rejects_active_application_lock(tmp_path):
    root = _installation(tmp_path, "v2")
    app = tomllib.loads((root / "sync.toml").read_text())
    lock_path = Path(app["paths"]["lock_file"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ConfigError, match="another application process"):
            upgrade_installation(root, apply=True)


def test_failure_after_replacement_rolls_back_every_live_file(tmp_path, monkeypatch):
    root = _installation(tmp_path, "v1")
    before = _digest_tree(root)
    database_path = tmp_path / "runtime" / "state" / f"{OLD_PRODUCT}.db"
    inode = database_path.stat().st_ino
    original_replace = Path.replace

    def fail_marker_replace(path, target):
        if Path(target).name == VERSION_FILE:
            raise RuntimeError("injected marker replacement failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_marker_replace)
    with pytest.raises(RuntimeError, match="injected marker replacement failure"):
        upgrade_installation(root, apply=True)
    after = _digest_tree(root)
    assert {
        key: value for key, value in after.items()
        if ".backups/" not in key and key != ".migration.lock"
    } == before
    assert database_path.stat().st_ino == inode
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == []
        assert connection.execute(
            "SELECT title FROM series WHERE series='series-one'"
        ).fetchone() == ("Fixture Series",)


def test_migrated_database_integrity(tmp_path):
    root = _installation(tmp_path, "v2")
    upgrade_installation(root, apply=True)
    app = tomllib.loads((root / "app.toml").read_text())["database"]
    with sqlite3.connect(app["path"]) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_cli_defaults_to_dry_run_and_never_prints_values(tmp_path, capsys):
    root = _installation(tmp_path, "v2")
    assert config_cli.main(["--root", str(root), "upgrade"]) == 0
    output = capsys.readouterr().out
    assert "upgrade dry-run: version 2 -> 3" in output
    assert "fixture-credential" not in output
    assert not (root / VERSION_FILE).exists()


def test_upgrade_cli_json_is_machine_readable_and_redacted(tmp_path, capsys):
    root = _installation(tmp_path, "v2")
    assert config_cli.main([
        "--root", str(root), "upgrade", "--format", "json",
    ]) == 0
    output = capsys.readouterr().out.strip()
    payload = json.loads(output)
    assert payload["source_version"] == 2
    assert payload["steps"] == ["initialize-versioned-installation"]
    assert "fixture-credential" not in output


def test_legacy_adapter_normalizes_earliest_environment_prefix(tmp_path):
    source = tmp_path / "legacy.env"
    source.write_text(f'{("BILI" + "POD")}_APP_DIR="/tmp/fixture-app"\n')
    assert read_legacy_env(source) == {"BILIBILI_PODCAST_APP_DIR": "/tmp/fixture-app"}
