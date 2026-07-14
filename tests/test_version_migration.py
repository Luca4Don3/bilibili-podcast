from __future__ import annotations

import fcntl
import hashlib
import sqlite3
import tomllib
from pathlib import Path

import pytest

from bilibili_podcast import db
from bilibili_podcast.config.manager import ConfigError
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
    for source in sorted((Path(__file__).parents[1] / "config").glob("*.toml.example")):
        content = source.read_text(encoding="utf-8")
        content = content.replace("<server_path>", str(server_root))
        content = content.replace("<user_token>", "fixture-credential")
        target = root / source.name.removesuffix(".example")
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)

    version = _fixture_version(fixture)
    if version == 1:
        for name in ("app.toml", "web.toml", "scheduler.toml", "sync.toml"):
            path = root / name
            path.write_text(
                path.read_text(encoding="utf-8").replace("bilibili-podcast", OLD_PRODUCT).replace(
                    "bilibili_podcast", OLD_PRODUCT
                ),
                encoding="utf-8",
            )
        publish = root / "publish.toml"
        publish.write_text(
            publish.read_text(encoding="utf-8").replace(
                "gone_series = []", f'script = "{server_root}/{OLD_PRODUCT}-publish"'
            ),
            encoding="utf-8",
        )
        web = root / "web.toml"
        web.write_text(
            web.read_text(encoding="utf-8").replace("previous_cookie_names = []\n", ""),
            encoding="utf-8",
        )
    if version == LATEST_VERSION:
        (root / VERSION_FILE).write_text(f"{LATEST_VERSION}\n", encoding="ascii")
        (root / VERSION_FILE).chmod(0o600)

    if database:
        database_path = server_root / "state" / "bilibili-podcast.db"
        if version == 1:
            database_path = server_root / "state" / f"{OLD_PRODUCT}.db"
        database_path.parent.mkdir(parents=True)
        db.migrate(database_path)
        if version == LATEST_VERSION:
            with db.transaction(database_path) as connection:
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
    users.write_text('[users."fixture.user"]\ntoken = "fixture-credential"\nseries = ["series-one"]\n')

    result = upgrade_installation(root, apply=True)

    assert result.plan.source_version == 1
    assert len(result.plan.steps) == 2
    assert detect_version(root) == LATEST_VERSION
    with users.open("rb") as handle:
        assert tomllib.load(handle)["users"]["fixture.user"]["series"] == ["series-one"]
    with (root / "web.toml").open("rb") as handle:
        security = tomllib.load(handle)["security"]
    assert security["cookie_name"] == "bilibili_podcast_session"
    assert f"{OLD_PRODUCT}_session" in security["previous_cookie_names"]
    app = tomllib.loads((root / "app.toml").read_text())["database"]
    with sqlite3.connect(app["path"]) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(LATEST_VERSION,)]
    assert (tmp_path / "runtime" / "state" / f"{OLD_PRODUCT}.db").exists()
    assert result.backup_root is not None
    manifest = result.backup_root / "SHA256SUMS"
    for line in manifest.read_text(encoding="ascii").splitlines():
        expected, name = line.split("  ", 1)
        assert hashlib.sha256((result.backup_root / name).read_bytes()).hexdigest() == expected


def test_v2_upgrades_to_latest_and_sets_database_version(tmp_path):
    root = _installation(tmp_path, "v2")
    result = upgrade_installation(root, apply=True)
    assert result.plan.steps == ("initialize-versioned-installation",)
    app = tomllib.loads((root / "app.toml").read_text())["database"]
    with sqlite3.connect(app["path"]) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(LATEST_VERSION,)]


def test_latest_is_idempotent(tmp_path):
    root = _installation(tmp_path, "v3")
    before = _digest_tree(tmp_path)
    result = upgrade_installation(root, apply=True)
    assert result.plan.steps == ()
    assert result.backup_root is None
    assert _digest_tree(tmp_path) == before


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


def test_failure_after_replacement_rolls_back_every_live_file(tmp_path, monkeypatch):
    root = _installation(tmp_path, "v1")
    before = _digest_tree(tmp_path)
    original_replace = Path.replace

    def fail_database_replace(path, target):
        if Path(target).suffix == ".db":
            raise RuntimeError("injected database replacement failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_database_replace)
    with pytest.raises(RuntimeError, match="injected database replacement failure"):
        upgrade_installation(root, apply=True)
    after = _digest_tree(tmp_path)
    assert {
        key: value for key, value in after.items()
        if ".backups/" not in key and key != "config/.migration.lock"
    } == before


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


def test_legacy_adapter_normalizes_earliest_environment_prefix(tmp_path):
    source = tmp_path / "legacy.env"
    source.write_text(f'{("BILI" + "POD")}_APP_DIR="/tmp/fixture-app"\n')
    assert read_legacy_env(source) == {"BILIBILI_PODCAST_APP_DIR": "/tmp/fixture-app"}
