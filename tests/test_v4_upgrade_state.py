from __future__ import annotations

import json
import getpass
import sqlite3
from pathlib import Path

import pytest

from bilibili_podcast import db
from bilibili_podcast.config import ActiveUpgradeError, ConfigManager
from bilibili_podcast.config.manager import ConfigError
from bilibili_podcast.config.migration import (
    ACTIVE_UPGRADE_SENTINEL,
    LATEST_VERSION,
    VERSION_FILE,
    apply_data_upgrade,
    finalize_upgrade,
    load_plan,
    prepare_upgrade,
    rollback_upgrade,
    update_plan_state,
)
from bilibili_podcast.config.migration.versioning import (
    EARLIEST_UNIFIED_VERSION,
    schema_snapshot_path,
)
from bilibili_podcast.config import cli as config_cli


FIXTURES = Path(__file__).parent / "fixtures" / "version_migration" / "snapshots"


@pytest.fixture(autouse=True)
def _isolate_privileged_final_checks(monkeypatch):
    from bilibili_podcast.config.migration import runtime_permissions, system_upgrade

    monkeypatch.setattr(
        runtime_permissions,
        "verify_permissions_applied",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        system_upgrade,
        "verify_system_applied",
        lambda *args, **kwargs: None,
    )


def _v3_installation(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "config"
    root.mkdir(parents=True)
    for source in (FIXTURES / "v3").glob("*.toml"):
        target = root / source.name
        target.write_text(
            source.read_text(encoding="utf-8").replace("__ROOT__", str(tmp_path)),
            encoding="utf-8",
        )
        target.chmod(0o600)
    database = tmp_path / "runtime" / "state" / "bilibili-podcast.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            (
                Path(__file__).parent
                / "fixtures/version_migration/snapshots/schema-v1-v3.sql"
            ).read_text(encoding="utf-8")
        )
        connection.execute("INSERT INTO schema_version(version) VALUES(3)")
    (root / VERSION_FILE).write_text("3\n", encoding="ascii")
    (root / VERSION_FILE).chmod(0o600)
    return root, database


def _advance_to_system(root: Path, plan_id: str) -> None:
    update_plan_state(
        root,
        plan_id,
        expected="data_applied",
        new_state="permissions_applied",
        permissions_backup_id="test-permissions-backup",
    )
    update_plan_state(
        root,
        plan_id,
        expected="permissions_applied",
        new_state="system_applied",
        system_backup_id="test-system-backup",
    )


def test_prepare_persists_random_opaque_plan_bound_to_inputs(tmp_path):
    root, _ = _v3_installation(tmp_path)
    first = prepare_upgrade(root)
    state = load_plan(root, first.plan_id)
    assert len(first.plan_id) == 48
    assert state["state"] == "prepared"
    assert state["source_version"] == 3
    assert state["target_version"] == LATEST_VERSION
    assert state["config_digest"]
    assert state["database"]["inode"]
    assert state["database"]["schema_digest"]
    assert state["database"]["table_digest"]
    plan_file = root / ".upgrade" / "plans" / f"{first.plan_id}.json"
    assert plan_file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ConfigError, match="active"):
        prepare_upgrade(root)


def test_data_apply_rejects_config_and_database_drift(tmp_path):
    root, database = _v3_installation(tmp_path)
    plan = prepare_upgrade(root)
    app = root / "app.toml"
    app.write_text(app.read_text() + "\n# drift\n")
    with pytest.raises(ConfigError, match="configuration changed"):
        apply_data_upgrade(root, plan.plan_id)

    root2, database2 = _v3_installation(tmp_path / "other")
    plan2 = prepare_upgrade(root2)
    with sqlite3.connect(database2) as connection:
        connection.execute(
            "INSERT INTO series(series,title,author) VALUES('drift','D','A')"
        )
    with pytest.raises(ConfigError, match="database changed"):
        apply_data_upgrade(root2, plan2.plan_id)


def test_data_apply_keeps_inode_sets_sentinel_and_blocks_runtime(tmp_path):
    root, database = _v3_installation(tmp_path)
    inode = database.stat().st_ino
    plan = prepare_upgrade(root)
    result = apply_data_upgrade(root, plan.plan_id)
    assert result.plan.state == "data_applied"
    assert database.stat().st_ino == inode
    assert (root / ACTIVE_UPGRADE_SENTINEL).read_text().strip() == plan.plan_id
    assert "fallback_log_dir" in (root / "app.toml").read_text()
    assert 'ffprobe = "ffprobe"' in (root / "app.toml").read_text()
    with pytest.raises(ActiveUpgradeError, match="active v4 upgrade"):
        ConfigManager(root, environ={}).load()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(3,)]


def test_finalize_commits_markers_only_after_every_step(tmp_path):
    root, database = _v3_installation(tmp_path)
    plan = prepare_upgrade(root)
    apply_data_upgrade(root, plan.plan_id)
    with pytest.raises(ConfigError, match="pending"):
        finalize_upgrade(root, plan.plan_id, apply=True)
    _advance_to_system(root, plan.plan_id)
    finalized = finalize_upgrade(root, plan.plan_id, apply=True)
    assert finalized.state == "finalized"
    assert (root / VERSION_FILE).read_text().strip() == str(LATEST_VERSION)
    assert not (root / ACTIVE_UPGRADE_SENTINEL).exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [
            (LATEST_VERSION,)
        ]
    ConfigManager(root, environ={}).load()
    assert finalize_upgrade(root, plan.plan_id, apply=True).state == "finalized"


def test_finalizing_journal_resumes_after_partial_marker_write(tmp_path):
    root, database = _v3_installation(tmp_path)
    plan = prepare_upgrade(root)
    apply_data_upgrade(root, plan.plan_id)
    _advance_to_system(root, plan.plan_id)
    update_plan_state(
        root,
        plan.plan_id,
        expected="system_applied",
        new_state="finalizing",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version(version) VALUES(?)", (LATEST_VERSION,))
    resumed = finalize_upgrade(root, plan.plan_id, apply=True)
    assert resumed.state == "finalized"


def test_rollback_restores_config_database_and_inode(tmp_path):
    root, database = _v3_installation(tmp_path)
    inode = database.stat().st_ino
    original_app = (root / "app.toml").read_bytes()
    plan = prepare_upgrade(root)
    apply_data_upgrade(root, plan.plan_id)
    rolled_back = rollback_upgrade(root, plan.plan_id, apply=True)
    assert rolled_back.state == "rolled_back"
    assert database.stat().st_ino == inode
    assert (root / "app.toml").read_bytes() == original_app
    assert not (root / ACTIVE_UPGRADE_SENTINEL).exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(3,)]


def test_db_runtime_migrate_never_upgrades_old_schema(tmp_path):
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_version VALUES(3)")
    with pytest.raises(db.DatabaseUpgradeRequired, match="explicit upgrade plan"):
        db.migrate(database)
    assert database.stat().st_ino


def test_every_version_has_an_independent_schema_snapshot():
    paths = [
        schema_snapshot_path(version)
        for version in range(EARLIEST_UNIFIED_VERSION, LATEST_VERSION + 1)
    ]
    assert all(path.is_file() and path.read_text().startswith("-- Immutable") for path in paths)
    assert len({path.name for path in paths}) == LATEST_VERSION


def test_prepare_imports_restricted_system_manifest_once(tmp_path):
    root, _ = _v3_installation(tmp_path)
    manifest = tmp_path / "system-input.toml"
    manifest.write_text(
        "[operator]\n"
        f'user = "{getpass.getuser()}"\n\n'
        "[nginx]\n"
        'user = "nginx-test"\n'
        'group = "nginx-test"\n'
        'config_path = "/etc/nginx/nginx.conf"\n'
        'access_log_path = "/var/log/nginx/access.log"\n'
        'error_log_path = "/var/log/nginx/error.log"\n',
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    plan = prepare_upgrade(root, system_manifest=manifest)
    system = ConfigManager(root, environ={}).load_system()
    assert system.operator.user == getpass.getuser()
    assert (root / "system.toml").stat().st_mode & 0o777 == 0o600
    assert load_plan(root, plan.plan_id)["system_digest"]


def test_upgrade_prepare_cli_json_is_redacted_and_plan_id_is_not_caller_selected(
    tmp_path,
    capsys,
):
    root, _ = _v3_installation(tmp_path)
    assert config_cli.main([
        "--root",
        str(root),
        "upgrade",
        "--prepare",
        "--format",
        "json",
    ]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "prepared"
    assert payload["plan_id"]
    assert str(tmp_path) not in output
    assert config_cli.main([
        "--root",
        str(root),
        "upgrade",
        "--prepare",
        "--plan-id",
        "a" * 48,
    ]) == 2
