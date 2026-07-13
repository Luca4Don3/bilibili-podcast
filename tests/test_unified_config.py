from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bilibili_podcast.config.cli import main as config_main
from bilibili_podcast.config.manager import (
    ConfigError, ConfigManager, LegacyConfigError, UnsafeConfigError,
)
from bilibili_podcast.config.schema import LEGACY_ENV_MAP, LEGACY_INPUT_ONLY
from bilibili_podcast.config.migration import migrate_legacy
from bilibili_podcast import db


def _actual_config(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "config"
    root = tmp_path / "config"
    root.mkdir(parents=True)
    for example in source.glob("*.toml.example"):
        content = example.read_text(encoding="utf-8")
        content = content.replace("<server_path>", str(tmp_path / "runtime"))
        content = content.replace("<user_token>", "test-user-token")
        target = root / example.name.removesuffix(".example")
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
    return root


def test_loads_one_immutable_snapshot_and_explicit_reload(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    manager = ConfigManager(root, environ={})
    first = manager.load()
    (root / "sync.toml").write_text(
        (root / "sync.toml").read_text().replace('level = "INFO"', 'level = "WARNING"'),
        encoding="utf-8",
    )
    assert manager.load() is first
    assert manager.reload().sync.logging.level == "WARNING"


def test_rejects_unknown_field_and_placeholder(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    with (root / "web.toml").open("a", encoding="utf-8") as handle:
        handle.write("unknown = true\n")
    with pytest.raises(ConfigError, match="unknown configuration field"):
        ConfigManager(root, environ={}).load()
    root = _actual_config(tmp_path / "second")
    path = root / "app.toml"
    path.write_text(path.read_text().replace(str(tmp_path / "second" / "runtime"), "<server_path>"))
    with pytest.raises(ConfigError, match="unreplaced placeholder"):
        ConfigManager(root, environ={}).load()


def test_rejects_control_characters_without_echoing_value(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    web = root / "web.toml"
    web.write_text(web.read_text().replace('host = "127.0.0.1"', 'host = "bad\\nunit"'))
    with pytest.raises(ConfigError, match=r"control character.*web\.toml:server\.host") as exc:
        ConfigManager(root, environ={}).load()
    assert "bad" not in str(exc.value)


def test_rejects_non_finite_numbers_and_unsafe_unit_names(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    sync = root / "sync.toml"
    sync.write_text(sync.read_text().replace("min_free_gb = 5.0", "min_free_gb = nan"))
    with pytest.raises(ConfigError, match="non-finite"):
        ConfigManager(root, environ={}).load()

    root = _actual_config(tmp_path / "unit")
    scheduler = root / "scheduler.toml"
    scheduler.write_text(
        scheduler.read_text().replace(
            'web = "bilipod-web.service"', 'web = "../bilipod-web.service"'
        )
    )
    with pytest.raises(ConfigError, match="invalid scheduler unit name"):
        ConfigManager(root, environ={}).load()

    root = _actual_config(tmp_path / "overlap")
    scheduler = root / "scheduler.toml"
    scheduler.write_text(
        scheduler.read_text().replace(
            'sync_glob = "bilipod-sync@*.service"', 'sync_glob = "bilipod-*.service"'
        )
    )
    with pytest.raises(ConfigError, match="overlaps web unit"):
        ConfigManager(root, environ={}).load()


def test_rejects_unsafe_sensitive_permissions(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    (root / "web.toml").chmod(0o644)
    with pytest.raises(UnsafeConfigError, match="world-readable"):
        ConfigManager(root, environ={}).load()


def test_rejects_legacy_environment_with_target(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    with pytest.raises(LegacyConfigError, match="app.database.path"):
        ConfigManager(root, environ={"BILIPOD_CONFIG_DB": "/tmp/old.db"}).load()


def test_show_json_is_redacted(tmp_path: Path, capsys, monkeypatch) -> None:
    for key in set(LEGACY_ENV_MAP) | LEGACY_INPUT_ONLY:
        monkeypatch.delenv(key, raising=False)
    root = _actual_config(tmp_path)
    web = root / "web.toml"
    web.write_text(web.read_text().replace('password = ""', 'password = "test-password"'))
    assert config_main(["--root", str(root), "show", "--scope", "web", "--format", "json"]) == 0
    output = capsys.readouterr().out
    assert "test-password" not in output
    assert json.loads(output)["web"]["security"]["password"] == "***"


def test_templates_validate_without_runtime_files() -> None:
    root = Path(__file__).parents[1] / "config"
    assert config_main(["--root", str(root), "validate", "--templates"]) == 0


def test_series_aliases_round_trip_through_sqlite(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    yaml_path = series_dir / "demo.yaml"
    yaml_path.write_text(
        """series: demo
title: Demo
author: Author
source:
  uid: 123
sync:
  quality: high
  browser_wait_seconds: 7
  update_period_grace_seconds: 45
  media_mode: manual
""",
        encoding="utf-8",
    )
    from bilibili_podcast.config.models import SeriesConfig

    config = SeriesConfig.from_yaml(yaml_path)
    assert config.sync["quality"] == "192K"
    assert config.sync["browser_wait_min_seconds"] == 7
    db_path = tmp_path / "series.db"
    db.migrate(db_path)
    with db.transaction(db_path) as conn:
        db.upsert_series(conn, config)
        db.upsert_source(conn, config)
        db.upsert_sync_policy(conn, config)
    loaded = db.load_series_configs(db_path)[0]
    assert loaded.sync["quality"] == "192K"
    assert loaded.sync["update_period_grace_seconds"] == 45
    assert loaded.sync["media_mode"] == "manual"


def _legacy_env(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime = tmp_path / "runtime" / "bilipod"
    values = {
        "BILIPOD_APP_DIR": runtime / "app",
        "BILIPOD_VENV_BIN": runtime / "venv/bin",
        "BILIPOD_SYNC_PATH": runtime / "venv/bin/bilibili-podcast",
        "BILIPOD_MEDIA_ROOT": runtime / "media",
        "BILIPOD_JSON_ROOT": runtime / "json",
        "BILIPOD_RSS_ROOT": runtime / "rss",
        "BILIPOD_PUBLISHED_RSS_ROOT": runtime / "published-rss",
        "BILIPOD_STATE_ROOT": runtime / "state",
        "BILIPOD_LOG_DIR": runtime / "logs",
        "BILIPOD_SECRETS_DIR": runtime / "secrets",
        "BILIPOD_COOKIE_FILE": runtime / "secrets/cookie.txt",
        "BILIPOD_LOCK_FILE": runtime / "state/lock",
        "BILIPOD_BROWSER_USER_DATA_ROOT": runtime / "browser/profiles",
        "PLAYWRIGHT_BROWSERS_PATH": runtime / "browser/bin",
        "BILIPOD_SYSTEMD_DIR": runtime / "systemd",
        "BILIPOD_CRON_SCRIPT_DIR": runtime / "cron/wrappers",
    }
    path = tmp_path / "legacy.env"
    path.write_text("\n".join(f"export {key}={value}" for key, value in values.items()) + "\n")
    return path


def test_migration_dry_run_writes_nothing_and_apply_validates(tmp_path: Path) -> None:
    env = _legacy_env(tmp_path)
    web_env = tmp_path / "web.env"
    web_env.write_text("BILIPOD_HTTPS=0\n")
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    rss_users = tmp_path / "users.conf"
    rss_users.write_text("test-token:all\n")
    output = tmp_path / "output"
    dry = migrate_legacy(
        legacy_env=env, legacy_web_env=web_env, legacy_series_dir=series_dir,
        legacy_rss_users=rss_users, output_root=output, apply=False,
    )
    assert dry.applied is False
    assert not output.exists()
    applied = migrate_legacy(
        legacy_env=env, legacy_web_env=web_env, legacy_series_dir=series_dir,
        legacy_rss_users=rss_users, output_root=output, apply=True,
    )
    assert applied.applied is True
    assert ConfigManager(output, environ={}).load().rss_users.users["user_1"].token == "test-token"


def test_migration_apply_rejects_existing_config_symlink(tmp_path: Path) -> None:
    env = _legacy_env(tmp_path)
    empty = tmp_path / "empty"
    empty.write_text("")
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    linked = tmp_path / "linked-config"
    linked.write_text("do not read or replace")
    (output / "web.toml").symlink_to(linked)

    with pytest.raises(UnsafeConfigError, match="unsafe migration target"):
        migrate_legacy(
            legacy_env=env, legacy_web_env=empty, legacy_series_dir=series_dir,
            legacy_rss_users=empty, output_root=output, apply=True,
        )

    assert (output / "web.toml").is_symlink()
    assert linked.read_text() == "do not read or replace"


def test_migration_preserves_primary_and_retry_schedules(tmp_path: Path) -> None:
    env = _legacy_env(tmp_path)
    web_env = tmp_path / "web.env"
    web_env.write_text("")
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "demo.yaml").write_text(
        """series: demo
title: Demo
author: Author
source:
  uid: 123
cron:
  schedules: ["0 0 * * *", "0 12 * * *"]
  retry_schedules: ["0 6 * * *"]
""",
        encoding="utf-8",
    )
    users = tmp_path / "users.conf"
    users.write_text("")
    output = tmp_path / "output"

    result = migrate_legacy(
        legacy_env=env, legacy_web_env=web_env, legacy_series_dir=series_dir,
        legacy_rss_users=users, output_root=output, apply=True,
    )

    assert result.series_count == 1
    db_path = tmp_path / "runtime" / "bilipod" / "state" / "bilipod.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT schedule, kind FROM cron_schedule WHERE series='demo' ORDER BY kind, position"
        ).fetchall()
    assert rows == [
        ("0 0 * * *", "primary"),
        ("0 12 * * *", "primary"),
        ("0 6 * * *", "retry"),
    ]


def test_migration_invalid_number_is_a_config_error(tmp_path: Path) -> None:
    env = _legacy_env(tmp_path)
    with env.open("a", encoding="utf-8") as handle:
        handle.write("BILIPOD_MIN_FREE_GB=not-a-number\n")
    empty = tmp_path / "empty"
    empty.write_text("")
    series_dir = tmp_path / "series"
    series_dir.mkdir()

    with pytest.raises(ConfigError, match="BILIPOD_MIN_FREE_GB"):
        migrate_legacy(
            legacy_env=env, legacy_web_env=empty, legacy_series_dir=series_dir,
            legacy_rss_users=empty, output_root=tmp_path / "output", apply=False,
        )


def test_removed_rsync_environment_is_rejected_and_not_migrated(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    with pytest.raises(LegacyConfigError, match="removed rsync support"):
        ConfigManager(root, environ={"BILIPOD_RSYNC_HOST": "legacy-host"}).load()

    env = _legacy_env(tmp_path / "migration")
    with env.open("a", encoding="utf-8") as handle:
        handle.write("RSYNC_PASSWORD=legacy-password\n")
    empty = tmp_path / "migration" / "empty"
    empty.write_text("")
    series_dir = tmp_path / "migration" / "series"
    series_dir.mkdir()
    output = tmp_path / "migration" / "output"
    result = migrate_legacy(
        legacy_env=env, legacy_web_env=empty, legacy_series_dir=series_dir,
        legacy_rss_users=empty, output_root=output, apply=True,
    )
    assert any("rsync" in item for item in result.normalizations)
    assert "rsync" not in (output / "publish.toml").read_text(encoding="utf-8").lower()


def test_migration_invalid_series_returns_cli_error_without_writes(tmp_path: Path, capsys) -> None:
    env = _legacy_env(tmp_path)
    empty = tmp_path / "empty"
    empty.write_text("")
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "broken.yaml").write_text("cron: [invalid\n", encoding="utf-8")
    output = tmp_path / "output"

    returncode = config_main([
        "migrate", "--legacy-env", str(env), "--legacy-web-env", str(empty),
        "--legacy-series-dir", str(series_dir), "--legacy-rss-users", str(empty),
        "--output-root", str(output),
    ])

    assert returncode == 2
    assert "invalid legacy series configuration" in capsys.readouterr().err
    assert not output.exists()


def test_enabled_publish_requires_valid_url_and_absolute_script(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    publish = root / "publish.toml"
    publish.write_text(
        '[publish]\nenabled = true\nmedia_base_url = "not-a-url"\n'
        'script = "relative.sh"\nmaster_placeholder = "__MEDIA_PLACEHOLDER__"\n'
    )
    with pytest.raises(ConfigError, match="invalid enabled URL"):
        ConfigManager(root, environ={}).load()


def test_manual_media_config_boundaries(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    manual = root / "manual-media.toml"
    manual.write_text(
        '[manual_media]\nenabled = true\nallowed_dirs = ["/data"]\nfollow_symlinks = false\n'
    )
    with pytest.raises(ConfigError, match="too broad"):
        ConfigManager(root, environ={}).load()

    manual.write_text(
        '[manual_media]\nenabled = true\nallowed_dirs = ["/data/manual-media"]\nfollow_symlinks = false\n'
    )
    assert ConfigManager(root, environ={}).load().manual_media.allowed_dirs == (
        Path("/data/manual-media"),
    )

    manual.write_text(
        '[manual_media]\nenabled = true\nallowed_dirs = ["relative"]\nfollow_symlinks = false\n'
    )
    with pytest.raises(ConfigError, match="must be absolute"):
        ConfigManager(root, environ={}).load()


def test_migration_rejects_publish_conflict_without_writes(tmp_path: Path) -> None:
    env = _legacy_env(tmp_path)
    with env.open("a") as handle:
        handle.write("BILIPOD_RSS_PUBLISH=/tmp/one\nBILIPOD_RSS_PUBLISH_SCRIPT=/tmp/two\n")
    web_env = tmp_path / "web.env"
    web_env.write_text("")
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    users = tmp_path / "users"
    users.write_text("")
    output = tmp_path / "output"
    with pytest.raises(ConfigError, match="conflict"):
        migrate_legacy(
            legacy_env=env, legacy_web_env=web_env, legacy_series_dir=series_dir,
            legacy_rss_users=users, output_root=output, apply=True,
        )
    assert not output.exists()


def test_web_config_view_requires_login_and_redacts(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    from unittest.mock import patch

    root = _actual_config(tmp_path)
    web = root / "web.toml"
    web.write_text(
        web.read_text().replace("enabled = false", "enabled = true", 1)
        .replace('password = ""', 'password = "web-test-secret"'),
        encoding="utf-8",
    )
    manager = ConfigManager(root, environ={})
    snapshot = manager.load()
    from bilibili_podcast.web import server

    assert server.create_app(snapshot, manager=manager).state.config is snapshot
    redirect = object()
    with patch.object(server, "_login_required", return_value=redirect):
        assert asyncio.run(server.config_view(object())) is redirect
    rendered = object()
    with patch.object(server, "_login_required", return_value=None), patch.object(
        server.templates, "TemplateResponse", return_value=rendered
    ) as template:
        assert asyncio.run(server.config_view(object())) is rendered
    rows = template.call_args.args[2]["rows"]
    assert all("web-test-secret" not in str(row["value"]) for row in rows)
    assert any(row["field"] == "web.security.password" and row["value"] == "***" for row in rows)


def test_unified_systemd_and_cron_wrappers_only_bootstrap_config_root(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    wrapper_dir = tmp_path / "generated"
    code = f"""
import runpy
from bilibili_podcast.config import ConfigManager
from bilibili_podcast.services import systemd_scheduler
systemd_scheduler.configure(ConfigManager().load())
generate_service = systemd_scheduler.generate_service
print(generate_service('demo'))
module = runpy.run_path('scripts/bilipod-crontab')
path = module['generate_wrapper_script']('demo', None, None, {str(wrapper_dir)!r}, True)
print(path.read_text())
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "src",
        "BILIPOD_CONFIG_ROOT": str(root),
    }
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).parents[1], env=env,
        capture_output=True, text=True, check=True,
    )
    output = result.stdout
    assert f"Environment=BILIPOD_CONFIG_ROOT={root.resolve()}" in output
    assert "--config-db" not in output
    assert "--cookie-file" not in output
    assert "BILIPOD_RSYNC_" not in output
    assert "__MEDIA_PLACEHOLDER__" in output


def test_crontab_explicit_paths_override_unified_scheduler_config(tmp_path: Path) -> None:
    root = _actual_config(tmp_path)
    db_path = ConfigManager(root, environ={}).load().app.database.path
    db_path.parent.mkdir(parents=True)
    db.migrate(db_path)
    override = tmp_path / "one-run-wrappers"
    result = subprocess.run(
        [
            sys.executable, "scripts/bilipod-crontab", "--print",
            "--script-dir", str(override), "--cron-user", "one-run-user",
        ],
        cwd=Path(__file__).parents[1],
        env={
            "PATH": os.environ.get("PATH", ""), "PYTHONPATH": "src",
            "BILIPOD_CONFIG_ROOT": str(root),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert f"wrapper scripts ready in {override}" in result.stderr


def test_sync_cli_override_precedence(tmp_path: Path) -> None:
    from bilibili_podcast.sync import apply_config_defaults, build_parser

    snapshot = ConfigManager(_actual_config(tmp_path), environ={}).load()
    explicit = build_parser().parse_args(["--media-root", "/one-run/media"])
    apply_config_defaults(explicit, snapshot)
    assert explicit.media_root == "/one-run/media"
    inherited = build_parser().parse_args([])
    apply_config_defaults(inherited, snapshot)
    assert inherited.media_root == str(snapshot.app.paths.media_root)
    assert inherited.max_downloads_per_run == 20
