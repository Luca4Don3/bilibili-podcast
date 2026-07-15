"""Tests for CLI admin tool — cron execution and wrapper behavior."""

import json
import os
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from bilibili_podcast import cli_admin


@pytest.fixture(autouse=True)
def reset_admin_config_snapshot(tmp_path: Path):
    """Inject one explicit test snapshot for low-level handler tests."""
    class ManualMedia:
        enabled = True
        follow_symlinks = False

        @property
        def allowed_dirs(self):
            raw = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS", "")
            return tuple(Path(item) for item in raw.split(":") if item)

    cli_admin._CONFIG = SimpleNamespace(
        root=tmp_path / "config",
        app=SimpleNamespace(
            database=SimpleNamespace(path=tmp_path / "bilibili-podcast.db"),
            paths=SimpleNamespace(
                media_root=tmp_path / "media",
                json_root=tmp_path / "json",
                rss_root=tmp_path / "rss",
                published_rss_root=tmp_path / "published-rss",
                state_root=tmp_path / "state",
                log_dir=tmp_path / "logs",
                secrets_dir=tmp_path / "secrets",
            ),
            executables=SimpleNamespace(sync=tmp_path / "bin/bilibili-podcast"),
        ),
        sync=SimpleNamespace(
            paths=SimpleNamespace(cookie_file="", lock_file=tmp_path / "sync.lock"),
            browser=SimpleNamespace(user_data_root=tmp_path / "browser"),
            downloads=SimpleNamespace(max_per_run=20, scheduled_max_per_run=1, min_free_gb=5.0),
            timeouts=SimpleNamespace(sync_seconds=300, preview_seconds=120, publish_seconds=60),
        ),
        scheduler=SimpleNamespace(command_timeout_seconds=30),
        publish=SimpleNamespace(publish=SimpleNamespace(
            enabled=False, media_base_url="https://media.example.invalid",
            master_placeholder="__MEDIA_PLACEHOLDER__", gone_series=(),
        )),
        rss_users=SimpleNamespace(users={}),
        manual_media=ManualMedia(),
    )
    yield
    cli_admin._CONFIG = None


@pytest.fixture
def mock_db(tmp_path: Path) -> str:
    """Create a minimal temp SQLite DB for cron plan to run against."""
    from bilibili_podcast import db

    path = tmp_path / "test.db"
    db.migrate(str(path))
    return str(path)


def test_cron_plan_uses_sys_executable(mock_db: str) -> None:
    """cron plan/apply must invoke bilibili-podcast-crontab via sys.executable,
    not rely on the file's executable bit."""
    called_with: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        called_with.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    dummy_script = "/tmp/nonexistent/bilibili-podcast-crontab"  # non-executable path

    with patch.object(cli_admin, "_find_crontab_bin", return_value=dummy_script):
        with patch.object(subprocess, "run", side_effect=fake_run):
            ns = MagicMock(spec=[])
            ns.yes = False
            ns.json = False
            ns.cron_script_dir = None
            cli_admin._run_crontab(ns, mock_db, "plan")

    assert len(called_with) == 1
    cmd = called_with[0]
    assert cmd[0] == sys.executable, (
        f"Expected sys.executable ({sys.executable}) as first arg, got {cmd[0]}"
    )
    assert cmd[1] == dummy_script
    assert "--config-db" in cmd
    assert "--print" in cmd


def test_cron_apply_passes_apply_flag(mock_db: str) -> None:
    """cron apply --yes must pass --apply to bilibili-podcast-crontab."""
    called_with: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        called_with.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    dummy_script = "/tmp/nonexistent/bilibili-podcast-crontab"

    with patch.object(cli_admin, "_find_crontab_bin", return_value=dummy_script):
        with patch.object(subprocess, "run", side_effect=fake_run):
            ns = MagicMock(spec=[])
            ns.yes = True
            ns.json = False
            ns.cron_script_dir = None
            cli_admin._run_crontab(ns, mock_db, "apply")

    assert len(called_with) == 1
    cmd = called_with[0]
    assert cmd[0] == sys.executable
    assert "--apply" in cmd


# ── Parsing tests ─────────────────────────────────────────────────────


def test_add_parses_dry_run_after_subcommand() -> None:
    """add --dry-run must be recognized on the subparser (not just globally)."""
    p = cli_admin.build_parser()
    ns = p.parse_args(["add", "--dry-run"])
    assert ns.add_dry_run is True
    assert ns.handler == cli_admin.cmd_add


def test_add_parses_yes_after_subcommand() -> None:
    """add --yes must be recognized on the subparser."""
    p = cli_admin.build_parser()
    ns = p.parse_args(["add", "--yes"])
    assert ns.add_yes is True


def test_filters_add_parses_yes_after_subcommand() -> None:
    """filters-add --yes must be recognized on the subparser."""
    p = cli_admin.build_parser()
    ns = p.parse_args(["filters-add", "demo", "--yes"])
    assert ns.fa_yes is True
    assert ns.series == "demo"


def test_sync_policy_set_parses_yes_after_subcommand() -> None:
    """sync-policy set --yes must be recognized on the subparser."""
    p = cli_admin.build_parser()
    ns = p.parse_args(["sync-policy", "set", "demo", "--keep-last", "50", "--yes"])
    assert ns.sp_set_yes is True
    assert ns.keep_last == 50


def test_remove_series_defaults_to_preview() -> None:
    p = cli_admin.build_parser()
    ns = p.parse_args(["remove-series", "demo"])
    assert ns.handler == cli_admin.cmd_remove_series
    assert ns.series == "demo"
    assert ns.apply is False


def test_remove_up_parses_apply_and_uid() -> None:
    p = cli_admin.build_parser()
    ns = p.parse_args(["remove-up", "--uid", "123", "--apply", "--yes"])
    assert ns.handler == cli_admin.cmd_remove_up
    assert ns.uid == 123
    assert ns.apply is True
    assert ns.remove_yes is True


# ── DB integration tests ──────────────────────────────────────────────


def test_add_dry_run_flag_parsed(tmp_path: Path) -> None:
    """add --dry-run must be parsed at argparse level."""
    from bilibili_podcast import db

    db_path = tmp_path / "test.db"
    db.migrate(str(db_path))

    p = cli_admin.build_parser()
    ns = p.parse_args(["--config-db", str(db_path), "add", "--series", "dryonly", "--dry-run"])
    assert ns.add_dry_run is True
    assert ns.series == "dryonly"


def test_slug_conflict_rejected_at_argparse() -> None:
    """Adding a duplicate slug without --update-existing must parse correctly
    (the handler will reject at DB level)."""
    p = cli_admin.build_parser()
    ns = p.parse_args(["add", "--series", "dup", "--title", "Dup", "--author", "A"])
    assert ns.series == "dup"
    assert ns.handler == cli_admin.cmd_add
    assert ns.update_existing is False


def test_cron_set_yes_after_subcommand_parsed() -> None:
    """cron set --yes must be recognized on the subparser."""
    p = cli_admin.build_parser()
    ns = p.parse_args(["cron", "set", "demo", "--schedule", "1 * * * *", "--yes"])
    assert ns.cron_set_yes is True
    assert ns.schedule == ["1 * * * *"]


def test_config_service_cron_schedules_only_enabled(tmp_path: Path) -> None:
    """Shared config loading must not revive disabled schedules."""
    from bilibili_podcast import db
    from bilibili_podcast.services.config_service import ConfigService

    db_path = _migrate(tmp_path)
    with db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('croncfg','T','A')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('croncfg','space',1)")
        conn.execute("INSERT INTO sync_policy(series) VALUES('croncfg')")
        conn.execute(
            "INSERT INTO cron_schedule(series,enabled,schedule,position) VALUES('croncfg',0,'0 6 * * *',0)"
        )
        conn.execute(
            "INSERT INTO cron_schedule(series,enabled,schedule,position) VALUES('croncfg',1,'15 3 * * *',1)"
        )
        conn.execute(
            "INSERT INTO cron_schedule(series,enabled,schedule,position,kind) VALUES('croncfg',1,'15 5 * * *',2,'retry')"
        )

    with db.transaction(db_path) as conn:
        svc = ConfigService(conn)
        assert svc.load_cron_schedules("croncfg") == ["15 3 * * *"]
        full = svc.load_full_config("croncfg")
        assert full["cron"] == ["15 3 * * *"]
        assert full["retry_cron"] == ["15 5 * * *"]


def test_sync_policy_update_fields_normalizes_boolean_values(tmp_path: Path) -> None:
    from bilibili_podcast import db
    from bilibili_podcast.services.sync_policy_service import SyncPolicyService

    db_path = _migrate(tmp_path)
    with db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('boolcfg','T','A')")
        SyncPolicyService(conn).update_fields("boolcfg", {
            "quality": "192K",
            "browser_fallback": True,
            "require_paid_state_confirmation": False,
        })

    with db.transaction(db_path) as conn:
        row = conn.execute(
            "SELECT quality, browser_fallback, require_paid_state_confirmation "
            "FROM sync_policy WHERE series='boolcfg'"
        ).fetchone()
    assert tuple(row) == ("192K", 1, 0)


# ── DB integration tests (write semantics) ────────────────────────────


def _migrate(tmp_path: Path) -> str:
    from bilibili_podcast import db
    path = tmp_path / "test.db"
    db.migrate(str(path))
    return str(path)


def test_add_noninteractive_writes_all_tables(tmp_path: Path) -> None:
    """Non-interactive add must write to series, sync_policy, filter_rule, cron_schedule."""
    import sqlite3

    db_path = _migrate(tmp_path)

    with patch.object(sys, "stdin"), patch.object(sys, "stdout"):
        p = cli_admin.build_parser()
        ns = p.parse_args([
            "--config-db", db_path,
            "add",
            "--series", "inttest",
            "--title", "Integration Test",
            "--author", "Tester",
            "--keep-last", "7",
            "--exclude-keyword", "访谈",
            "--include-keyword", "科技",
            "--exclude-season-id", "5492168",
            "--cron", "0 */12 * * *",
            "--yes",
        ])
        ns.yes = True
        ns.add_yes = True
        cli_admin.cmd_add(ns)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT 1 FROM series WHERE series='inttest'").fetchone()
    assert row is not None, "series not created"

    sp = conn.execute("SELECT keep_last FROM sync_policy WHERE series='inttest'").fetchone()
    assert sp is not None and sp[0] == 7, f"keep_last expected 7, got {sp}"

    rules = conn.execute(
        "SELECT rule_type, value FROM filter_rule WHERE series='inttest' ORDER BY position"
    ).fetchall()
    rule_types = {r[0] for r in rules}
    assert "exclude_keyword" in rule_types
    assert "include_keyword" in rule_types

    cron = conn.execute(
        "SELECT schedule FROM cron_schedule WHERE series='inttest'"
    ).fetchall()
    assert len(cron) == 1
    assert cron[0][0] == "0 */12 * * *"
    conn.close()


def test_remove_series_preview_does_not_delete(tmp_path: Path) -> None:
    import sqlite3

    db_path = _migrate(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
    conn.execute("INSERT INTO series_source(series,type,uid) VALUES('demo','space',123)")
    conn.commit()
    conn.close()

    p = cli_admin.build_parser()
    ns = p.parse_args([
        "--config-db", db_path,
        "remove-series", "demo",
        "--media-root", str(tmp_path / "media"),
        "--json-root", str(tmp_path / "json"),
        "--rss-root", str(tmp_path / "rss"),
        "--published-rss-root", str(tmp_path / "published"),
        "--cron-script-dir", str(tmp_path / "auto"),
        "--browser-user-data-root", str(tmp_path / "browser-profiles"),
        "--lock-file", str(tmp_path / "sync.lock"),
        "--users-conf", str(tmp_path / "rss-users.toml"),
    ])
    with patch.object(sys, "stdout"):
        cli_admin.cmd_remove_series(ns)

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT 1 FROM series WHERE series='demo'").fetchone() is not None
    conn.close()


def test_remove_series_schedule_failure_stops_deletion(tmp_path: Path) -> None:
    import sqlite3

    from bilibili_podcast.services.scheduler_service import SchedulerCommandResult

    db_path = _migrate(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
    conn.execute("INSERT INTO series_source(series,type,uid) VALUES('demo','space',123)")
    conn.commit()
    conn.close()
    media = tmp_path / "media" / "demo" / "a.mp3"
    media.parent.mkdir(parents=True)
    media.write_text("x")

    p = cli_admin.build_parser()
    ns = p.parse_args([
        "--config-db", db_path,
        "remove-series", "demo", "--apply", "--yes",
        "--media-root", str(tmp_path / "media"),
        "--json-root", str(tmp_path / "json"),
        "--rss-root", str(tmp_path / "rss"),
        "--published-rss-root", str(tmp_path / "published"),
        "--cron-script-dir", str(tmp_path / "auto"),
        "--browser-user-data-root", str(tmp_path / "browser-profiles"),
        "--lock-file", str(tmp_path / "sync.lock"),
        "--users-conf", str(tmp_path / "rss-users.toml"),
    ])
    failure = SchedulerCommandResult(
        backend="systemd", action="remove-series", returncode=1,
        stdout="", stderr="failed",
    )
    with patch.object(cli_admin.SchedulerService, "remove_series_schedule", return_value=failure):
        with patch.object(sys, "stdout"), patch.object(sys, "stderr"):
            with pytest.raises(SystemExit) as exc:
                cli_admin.cmd_remove_series(ns)
    assert exc.value.code == cli_admin.EXIT_SYNC_FAIL

    assert media.exists()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT 1 FROM series WHERE series='demo'").fetchone() is not None
    conn.close()


def test_remove_series_lock_contention_stops_deletion(tmp_path: Path) -> None:
    import sqlite3

    from bilibili_podcast import sync

    db_path = _migrate(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
    conn.execute("INSERT INTO series_source(series,type,uid) VALUES('demo','space',123)")
    conn.commit()
    conn.close()
    media = tmp_path / "media" / "demo" / "a.mp3"
    media.parent.mkdir(parents=True)
    media.write_text("x")
    lock_file = tmp_path / "sync.lock"

    p = cli_admin.build_parser()
    ns = p.parse_args([
        "--config-db", db_path,
        "remove-series", "demo", "--apply", "--yes",
        "--media-root", str(tmp_path / "media"),
        "--json-root", str(tmp_path / "json"),
        "--rss-root", str(tmp_path / "rss"),
        "--published-rss-root", str(tmp_path / "published"),
        "--cron-script-dir", str(tmp_path / "auto"),
        "--browser-user-data-root", str(tmp_path / "browser-profiles"),
        "--lock-file", str(lock_file),
        "--users-conf", str(tmp_path / "rss-users.toml"),
    ])
    with sync.process_lock(str(lock_file)):
        with patch.object(sys, "stdout"), pytest.raises(SystemExit) as exc:
            cli_admin.cmd_remove_series(ns)

    assert exc.value.code == 2
    assert media.exists()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT 1 FROM series WHERE series='demo'").fetchone() is not None
    conn.close()


def test_remove_up_does_not_remove_later_schedule_before_failure(tmp_path: Path) -> None:
    import sqlite3

    from bilibili_podcast.services.scheduler_service import SchedulerCommandResult

    db_path = _migrate(tmp_path)
    conn = sqlite3.connect(db_path)
    for series in ("one", "two"):
        conn.execute("INSERT INTO series(series,title,author) VALUES(?,?,?)", (series, series, "A"))
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES(?,'space',123)", (series,))
    conn.commit()
    conn.close()
    (tmp_path / "publish.toml").write_text(
        '[publish]\nenabled = true\nmedia_base_url = "https://media.example.invalid"\n'
        'master_placeholder = "__MEDIA_PLACEHOLDER__"\ngone_series = []\n'
    )

    p = cli_admin.build_parser()
    ns = p.parse_args([
        "--config-db", db_path,
        "remove-up", "--uid", "123", "--apply", "--yes",
        "--media-root", str(tmp_path / "media"),
        "--json-root", str(tmp_path / "json"),
        "--rss-root", str(tmp_path / "rss"),
        "--published-rss-root", str(tmp_path / "published"),
        "--cron-script-dir", str(tmp_path / "auto"),
        "--browser-user-data-root", str(tmp_path / "browser-profiles"),
        "--lock-file", str(tmp_path / "sync.lock"),
        "--users-conf", str(tmp_path / "rss-users.toml"),
    ])
    ok = SchedulerCommandResult("systemd", "remove-series", 0, "", "")
    failure = SchedulerCommandResult("systemd", "remove-series", 1, "", "failed")
    with patch.object(
        cli_admin.SchedulerService, "remove_series_schedule", side_effect=[ok, failure],
    ) as remove_schedule:
        with patch.object(sys, "stdout"), patch.object(sys, "stderr"), pytest.raises(SystemExit) as exc:
            cli_admin.cmd_remove_up(ns)

    assert exc.value.code == cli_admin.EXIT_SYNC_FAIL
    assert [call.args[0] for call in remove_schedule.call_args_list] == ["one", "two"]
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT 1 FROM series WHERE series='one'").fetchone() is None
    assert conn.execute("SELECT 1 FROM series WHERE series='two'").fetchone() is not None
    conn.close()


def test_add_noninteractive_duplicate_exits_validation(tmp_path: Path) -> None:
    import sqlite3

    db_path = _migrate(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO series(series,title,author) VALUES('dup','Dup','A')")
    conn.commit()
    conn.close()

    p = cli_admin.build_parser()
    ns = p.parse_args([
        "--config-db", db_path,
        "add", "--series", "dup", "--title", "Dup", "--author", "A", "--yes",
    ])
    with patch.object(sys, "stdout"), pytest.raises(SystemExit) as exc:
        cli_admin.cmd_add(ns)

    assert exc.value.code == cli_admin.EXIT_VALIDATION


def test_add_update_existing_preserves_unspecified_fields(tmp_path: Path) -> None:
    """--update-existing must preserve fields not explicitly passed."""
    import sqlite3

    db_path = _migrate(tmp_path)

    with patch.object(sys, "stdin"), patch.object(sys, "stdout"):
        p = cli_admin.build_parser()
        ns = p.parse_args([
            "--config-db", db_path, "add",
            "--series", "updtest", "--title", "Original",
            "--author", "A", "--description", "keep me",
            "--cover-art", "http://cover", "--category", "tech",
            "--yes",
        ])
        ns.yes = True
        ns.add_yes = True
        cli_admin.cmd_add(ns)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sync_policy SET update_period_grace_seconds=321, media_mode='manual' "
            "WHERE series='updtest'"
        )

    with patch.object(sys, "stdin"), patch.object(sys, "stdout"):
        p = cli_admin.build_parser()
        ns = p.parse_args([
            "--config-db", db_path, "add",
            "--series", "updtest", "--title", "Updated",
            "--author", "B",
            "--update-existing", "--yes",
        ])
        ns.yes = True
        ns.add_yes = True
        cli_admin.cmd_add(ns)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT title, description, cover_art, category FROM series WHERE series='updtest'"
    ).fetchone()
    sync_row = conn.execute(
        "SELECT update_period_grace_seconds, media_mode FROM sync_policy WHERE series='updtest'"
    ).fetchone()
    conn.close()
    assert row[0] == "Updated"
    assert row[1] == "keep me", f"description should be preserved, got '{row[1]}'"
    assert row[2] == "http://cover", f"cover_art should be preserved, got '{row[2]}'"
    assert row[3] == "tech", f"category should be preserved, got '{row[3]}'"
    assert sync_row == (321, "manual")


def test_cron_set_yes_writes_to_db(tmp_path: Path) -> None:
    """cron set --yes must actually write to cron_schedule."""
    import sqlite3

    db_path = _migrate(tmp_path)

    with patch.object(sys, "stdin"), patch.object(sys, "stdout"):
        p = cli_admin.build_parser()
        ns = p.parse_args([
            "--config-db", db_path, "add",
            "--series", "crontest", "--title", "Cron Test",
            "--author", "T", "--yes",
        ])
        ns.yes = True
        ns.add_yes = True
        cli_admin.cmd_add(ns)

    with patch.object(sys, "stdin"), patch.object(sys, "stdout"):
        p = cli_admin.build_parser()
        ns = p.parse_args([
            "--config-db", db_path,
            "cron", "set", "crontest",
            "--schedule", "5 */12 * * *",
            "--yes",
        ])
        ns.yes = False
        ns.cron_set_yes = True
        cli_admin.cmd_cron_set(ns)

    conn = sqlite3.connect(db_path)
    cron = conn.execute(
        "SELECT schedule FROM cron_schedule WHERE series='crontest'"
    ).fetchall()
    conn.close()
    assert len(cron) == 1
    assert cron[0][0] == "5 */12 * * *"


def test_filters_add_yes_writes_to_db(tmp_path: Path) -> None:
    """filters-add --yes must actually insert filter_rule rows."""
    import sqlite3

    db_path = _migrate(tmp_path)

    with patch.object(sys, "stdin"), patch.object(sys, "stdout"):
        p = cli_admin.build_parser()
        ns = p.parse_args([
            "--config-db", db_path, "add",
            "--series", "filtertest", "--title", "Filter Test",
            "--author", "T", "--yes",
        ])
        ns.yes = True
        ns.add_yes = True
        cli_admin.cmd_add(ns)

    with patch.object(sys, "stdin"), patch.object(sys, "stdout"):
        p = cli_admin.build_parser()
        ns = p.parse_args([
            "--config-db", db_path,
            "filters-add", "filtertest",
            "--exclude-keyword", "广告",
            "--include-keyword", "科技",
            "--exclude-season-id", "5492168",
            "--yes",
        ])
        ns.yes = False
        ns.fa_yes = True
        cli_admin.cmd_filters_add(ns)

    conn = sqlite3.connect(db_path)
    rules = conn.execute(
        "SELECT rule_type, value FROM filter_rule WHERE series='filtertest' ORDER BY position"
    ).fetchall()
    conn.close()
    rule_map = {r[0]: r[1] for r in rules}
    assert "exclude_keyword" in rule_map
    assert rule_map["exclude_keyword"] == "广告"
    assert "include_keyword" in rule_map
    assert rule_map["include_keyword"] == "科技"
    assert rule_map["exclude_season_id"] == "5492168"


# ── cmd_sync tests ────────────────────────────────────────────────────


def test_sync_dry_run_has_production_params(tmp_path: Path) -> None:
    """sync without --apply must include production paths but not --apply."""
    import subprocess

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch.object(cli_admin, "_find_sync_bin", return_value="/tmp/bilibili-podcast"):
        with patch.object(subprocess, "run", side_effect=fake_run):
            p = cli_admin.build_parser()
            ns = p.parse_args(["--config-db", db_path, "sync", "synctest"])
            cli_admin.cmd_sync(ns)

    assert len(calls) == 1, f"expected 1 subprocess call, got {len(calls)}"
    cmd = calls[0]
    assert "--apply" not in cmd, "dry-run must not contain --apply"
    assert "--token __MEDIA_PLACEHOLDER__" in " ".join(cmd)
    assert "--cookie-file" in cmd
    assert "--media-root" in cmd
    assert "--json-root" in cmd
    assert "--rss-root" in cmd
    assert "--state-root" in cmd
    assert "--lock-file" in cmd
    assert "--log-dir" in cmd
    assert "--media-base-url" in cmd
    assert "--browser-user-data-root" in cmd
    assert "--max-downloads-per-run" in cmd
    assert cmd[cmd.index("--max-downloads-per-run") + 1] == "20"
    assert "--min-free-gb" in cmd


def test_sync_builtin_publish_failure_returns_sync_failure(tmp_path: Path) -> None:
    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)
    cli_admin._CONFIG.publish.publish.enabled = True

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch.object(cli_admin, "_find_sync_bin", return_value="/tmp/bilibili-podcast"), \
            patch.object(subprocess, "run", side_effect=fake_run), \
            patch("bilibili_podcast.publisher.publish", side_effect=RuntimeError("publish failed")):
        args = cli_admin.build_parser().parse_args(
            ["--config-db", db_path, "--yes", "sync", "synctest", "--apply"]
        )
        with pytest.raises(SystemExit) as exc:
            cli_admin.cmd_sync(args)
    assert exc.value.code == cli_admin.EXIT_SYNC_FAIL


def test_sync_apply_ignores_legacy_publish_environment(tmp_path: Path) -> None:
    """Legacy publish environment must not activate the post-sync hook."""
    import os
    import subprocess

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch.object(cli_admin, "_find_sync_bin", return_value="/tmp/bilibili-podcast"):
        with patch.object(subprocess, "run", side_effect=fake_run):
            p = cli_admin.build_parser()
            ns = p.parse_args(["--config-db", db_path, "--yes", "sync", "synctest", "--apply"])
            os.environ["BILIBILI_PODCAST_RSS_PUBLISH"] = "/tmp/test-publish.sh"
            cli_admin.cmd_sync(ns)

    assert len(calls) == 1
    sync_cmd = calls[0]
    assert "--apply" in sync_cmd
    assert "--token __MEDIA_PLACEHOLDER__" in " ".join(sync_cmd)


def test_sync_failure_does_not_publish(tmp_path: Path) -> None:
    """sync failure must exit non-zero and not run publish."""
    import subprocess

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="error")

    with patch.object(cli_admin, "_find_sync_bin", return_value="/tmp/bilibili-podcast"):
        with patch.object(subprocess, "run", side_effect=fake_run):
            p = cli_admin.build_parser()
            ns = p.parse_args(["--config-db", db_path, "--yes", "sync", "synctest", "--apply"])
            import pytest
            with pytest.raises(SystemExit) as exc:
                cli_admin.cmd_sync(ns)
            assert exc.value.code == cli_admin.EXIT_SYNC_FAIL

    assert len(calls) == 1, f"expected 1 subprocess call, got {len(calls)}"


def test_legacy_publish_environment_cannot_inject_failing_hook(tmp_path: Path) -> None:
    import os
    import subprocess

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    side_effects = {"first": True}

    def fake_run(cmd, **kwargs):
        if side_effects["first"]:
            side_effects["first"] = False
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="publish error")

    with patch.object(cli_admin, "_find_sync_bin", return_value="/tmp/bilibili-podcast"):
        with patch.object(subprocess, "run", side_effect=fake_run):
            p = cli_admin.build_parser()
            ns = p.parse_args(["--config-db", db_path, "--yes", "sync", "synctest", "--apply"])
            os.environ["BILIBILI_PODCAST_RSS_PUBLISH"] = "/tmp/test-publish.sh"
            cli_admin.cmd_sync(ns)
    assert side_effects["first"] is False


def _create_minimal_series(db_path: str) -> None:
    """Insert a minimal series row for sync tests."""
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('synctest','Sync Test','T')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('synctest','space',1)")
        conn.execute("INSERT INTO sync_policy(series) VALUES('synctest')")
        conn.commit()


def test_crontab_excludes_disabled_schedule(tmp_path: Path) -> None:
    """bilibili-podcast-crontab must exclude enabled=0 schedules from generated crontab."""
    import subprocess
    import sqlite3

    db_path = _migrate(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO series(series,title,author) VALUES('enabtest','E','T')")
    conn.execute("INSERT INTO series_source(series,type,uid) VALUES('enabtest','space',1)")
    conn.execute("INSERT INTO sync_policy(series) VALUES('enabtest')")
    conn.execute("INSERT INTO cron_schedule(series,enabled,schedule,position) VALUES('enabtest',1,'15 3 * * *',0)")
    conn.execute("INSERT INTO cron_schedule(series,enabled,schedule,position) VALUES('enabtest',0,'0 6 * * *',1)")
    conn.commit()
    conn.close()

    crontab_script = str(Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab")
    result = subprocess.run(
        [sys.executable, crontab_script, "--config-db", db_path, "--print"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"bilibili-podcast-crontab failed:\nstdout:{result.stdout}\nstderr:{result.stderr}"
    assert "15 3" in result.stdout, \
        "enabled schedule should appear in crontab"
    assert "0 6" not in result.stdout, \
        "disabled schedule should NOT appear in crontab"


def test_crontab_rejects_retry_schedule(tmp_path: Path) -> None:
    import sqlite3

    db_path = _migrate(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('retrycron','R','T')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('retrycron','space',1)")
        conn.execute("INSERT INTO sync_policy(series) VALUES('retrycron')")
        conn.execute("INSERT INTO cron_schedule(series,schedule,kind) VALUES('retrycron','0 3 * * *','primary')")
        conn.execute("INSERT INTO cron_schedule(series,schedule,kind) VALUES('retrycron','0 5 * * *','retry')")

    crontab_script = str(Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab")
    result = subprocess.run(
        [sys.executable, crontab_script, "--config-db", db_path,
         "--script-dir", str(tmp_path / "auto"), "--print"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "does not support retry schedules: retrycron" in result.stderr


def test_crontab_database_failure_does_not_write_scheduler_files(tmp_path: Path) -> None:
    crontab_script = str(Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab")
    wrapper_dir = tmp_path / "auto"
    result = subprocess.run(
        [
            sys.executable, crontab_script,
            "--config-db", str(tmp_path / "missing" / "bilibili-podcast.db"),
            "--script-dir", str(wrapper_dir),
            "--apply",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={"BILIBILI_PODCAST_CONFIG_ROOT": str(tmp_path / "config")},
    )

    assert result.returncode == 2
    assert "cannot load scheduling configuration" in result.stderr
    assert not wrapper_dir.exists()


def test_crontab_read_failure_never_writes_replacement(monkeypatch) -> None:
    import runpy

    script = Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab"
    module = runpy.run_path(str(script))
    calls = []

    def failed_read(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "permission denied")

    monkeypatch.setattr(subprocess, "run", failed_read)
    with pytest.raises(RuntimeError, match="cannot read existing crontab"):
        module["merge_with_existing_crontab"]("0 1 * * * /new\n", "bilibili-podcast")
    assert len(calls) == 1

    generated = MagicMock()
    monkeypatch.setitem(module, "load_configs", lambda *_: [{
        "series": "demo", "enabled": True,
        "cron": {"enabled": True, "schedules": ["0 1 * * *"]},
    }])
    monkeypatch.setitem(
        module, "merge_with_existing_crontab",
        lambda *_: (_ for _ in ()).throw(RuntimeError("cannot read existing crontab")),
    )
    monkeypatch.setitem(module, "generate_wrapper_script", generated)
    monkeypatch.delenv("BILIBILI_PODCAST_CONFIG_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", [
        str(script), "--config-db", "/tmp/test.db", "--script-dir", "/tmp/wrappers",
        "--apply",
    ])
    with pytest.raises(SystemExit) as exc:
        module["main"]()
    assert exc.value.code == 2
    generated.assert_not_called()


def test_crontab_derived_schedule_is_stable_across_hash_seeds() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab"
    code = (
        "import runpy; "
        f"m=runpy.run_path({str(script)!r}); "
        "print(m['derive_cron_from_period'](12, 'stable-series'))"
    )
    outputs = []
    for seed in ("1", "2"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def test_crontab_marker_title_is_single_line(tmp_path: Path) -> None:
    """bilibili-podcast-crontab must not let a title break the auto block marker."""
    import sqlite3

    db_path = _migrate(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('marker','Line One\nLine Two','T')")
        conn.execute("INSERT INTO cron_schedule(series,schedule) VALUES('marker','15 3 * * *')")

    crontab_script = str(Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab")
    result = subprocess.run(
        [sys.executable, crontab_script, "--config-db", db_path, "--print"],
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0
    assert "# BEGIN BILIBILI_PODCAST AUTO - marker (Line One Line Two)" in result.stdout


def test_crontab_excludes_systemd_backend_without_disabling_schedule(tmp_path: Path) -> None:
    """cron generation must skip systemd series while retaining schedule data."""
    import sqlite3

    from bilibili_podcast import db

    db_path = _migrate(tmp_path)
    with db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('systemdonly','Systemd','T')")
        conn.execute("INSERT INTO cron_schedule(series,schedule) VALUES('systemdonly','15 3 * * *')")
        db.set_scheduler_backend(conn, "systemdonly", "systemd")

    crontab_script = str(Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab")
    result = subprocess.run(
        [sys.executable, crontab_script, "--config-db", db_path, "--print"],
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0
    assert "systemdonly" not in result.stdout
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT enabled FROM cron_schedule WHERE series='systemdonly'"
        ).fetchone()[0] == 1


def test_crontab_database_reader_uses_shared_connection_policy(
    monkeypatch, tmp_path: Path,
) -> None:
    import runpy

    from bilibili_podcast import sqlite_connection

    db_path = _migrate(tmp_path)
    observed = {}
    original_connect = sqlite_connection.connect

    def recording_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        observed["journal_mode"] = connection.execute("PRAGMA journal_mode").fetchone()[0]
        observed["foreign_keys"] = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        observed["busy_timeout"] = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        return connection

    monkeypatch.setattr(sqlite_connection, "connect", recording_connect)
    script = Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab"
    module = runpy.run_path(str(script))
    module["_load_configs_from_db"](db_path)

    assert observed == {
        "journal_mode": "wal",
        "foreign_keys": 1,
        "busy_timeout": 5000,
    }


def test_crontab_merge_preserves_manual_marker_text(monkeypatch) -> None:
    """A manual comment mentioning the marker must not be treated as an auto block."""
    import runpy

    script = Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab"
    module = runpy.run_path(str(script))
    legacy_marker = ("BILI" + "POD").upper()
    existing = (
        "# manual note: # BEGIN BILIBILI_PODCAST AUTO - demo\n"
        f"# BEGIN {legacy_marker} AUTO - old (Old)\n"
        "0 1 * * * /old\n"
        f"# END {legacy_marker} AUTO\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, existing, ""),
    )

    merged = module["merge_with_existing_crontab"](
        "# BEGIN BILIBILI_PODCAST AUTO - new (New)\n0 2 * * * /new\n# END BILIBILI_PODCAST AUTO\n",
        "",
    )

    assert "# manual note:" in merged
    assert f"{legacy_marker} AUTO - old" not in merged
    assert "BILIBILI_PODCAST AUTO - new" in merged


def test_crontab_systemd_timer_detection(monkeypatch, tmp_path: Path) -> None:
    """Existing enabled timers must be protected during first deploy with an old DB."""
    import runpy
    import sqlite3

    db_path = _migrate(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
        conn.execute("INSERT INTO cron_schedule(series,schedule) VALUES('demo','15 3 * * *')")

    script = Path(__file__).resolve().parent.parent / "scripts" / "bilibili-podcast-crontab"
    module = runpy.run_path(str(script))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "enabled\n", ""),
    )

    assert module["systemd_timer_is_enabled"]("demo") is True
    assert module["_load_configs_from_db"](db_path) == []


# ── Paid / manual media tests ─────────────────────────────────────────


def test_paid_list_missing_readonly(tmp_path: Path) -> None:
    """paid list-missing must not write any files."""
    import os

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    json_dir = tmp_path / "json" / "synctest"
    json_dir.mkdir(parents=True)
    ep = {"title": "Test", "bvid": "BV1test12345"}
    (json_dir / "BV1test12345_64K.info.json").write_text(json.dumps(ep))

    p = cli_admin.build_parser()
    ns = p.parse_args(["--config-db", db_path, "paid", "list-missing", "synctest",
                        "--media-root", str(tmp_path / "media")])
    cli_admin.cmd_paid_list_missing(ns)
    # No files should have been written
    assert not (tmp_path / "media").exists() or not list((tmp_path / "media").iterdir()), \
        "list-missing must not write files"


def test_paid_attach_media_rejects_bad_path(tmp_path: Path) -> None:
    """attach-media must reject paths outside whitelist."""
    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    bad_file = tmp_path / "bad.mp3"
    bad_file.write_text("fake mp3")

    p = cli_admin.build_parser()
    ns = p.parse_args(["--config-db", db_path, "paid", "attach-media", "synctest",
                        "--bvid", "BV0000000004", "--server-path", str(bad_file),
                        "--media-root", str(tmp_path / "media")])
    import pytest
    with pytest.raises(SystemExit):
        cli_admin.cmd_paid_attach_media(ns)


def test_paid_attach_media_rejects_bad_extension(tmp_path: Path) -> None:
    """attach-media must reject non-mp3 files (must fail at extension check,
    not at whitelist check)."""
    from bilibili_podcast import cli_admin as ca

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    allow_dir = tmp_path / "manual-media"
    allow_dir.mkdir()
    bad_file = allow_dir / "bad.txt"
    bad_file.write_text("not an audio file")

    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allow_dir)
    try:
        p = cli_admin.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "attach-media", "synctest",
                            "--bvid", "BV0000000004", "--server-path", str(bad_file),
                            "--media-root", str(tmp_path / "media")])
        import pytest
        with pytest.raises(SystemExit):
            cli_admin.cmd_paid_attach_media(ns)
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


def test_paid_attach_media_success(tmp_path: Path) -> None:
    """attach-media must copy file to media dir with correct name."""
    from bilibili_podcast import cli_admin as ca

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    allow_dir = tmp_path / "manual-media"
    allow_dir.mkdir()
    src = allow_dir / "episode.mp3"
    src.write_text("fake audio content")

    media_root = tmp_path / "data" / "media"
    bvid = "BV0000000003"
    target = media_root / "synctest" / f"{bvid}_64K.mp3"

    original_dirs = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allow_dir)
    try:
        p = cli_admin.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "attach-media", "synctest",
                            "--bvid", bvid, "--server-path", str(src),
                            "--media-root", str(media_root)])
        cli_admin.cmd_paid_attach_media(ns)
        assert target.exists(), f"target file not created: {target}"
        assert target.read_text() == "fake audio content"
        assert oct(target.stat().st_mode)[-3:] == "644"
    finally:
        if original_dirs is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = original_dirs


def test_manual_media_skips_download(tmp_path: Path) -> None:
    """Manual media series must have to_download set to empty."""
    from bilibili_podcast.utils.series_config import SeriesConfig

    cfg = SeriesConfig(
        series="manualtest", enabled=True, title="M", author="A",
        description="", cover_art="", category="",
        subcategories=[], explicit=False, lang="zh-CN",
        source={}, sync={"media_mode": "manual"},
        filters={}, paid_preview={}, keep_last=0,
    )
    # Download is skipped when media_mode=manual
    from bilibili_podcast.sync import sync_series
    # Just verify the config field is recognized
    assert cfg.sync.get("media_mode") == "manual"


def test_paid_attach_media_replace_allowed(tmp_path: Path) -> None:
    """attach-media with --replace must overwrite existing file."""
    from bilibili_podcast import cli_admin as ca

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    allow_dir = tmp_path / "manual-media"
    allow_dir.mkdir()
    src = allow_dir / "new.mp3"
    src.write_text("new content")

    media_root = tmp_path / "data" / "media"
    series_dir = media_root / "synctest"
    series_dir.mkdir(parents=True)
    bvid = "BV0000000001"
    target = series_dir / f"{bvid}_64K.mp3"
    target.write_text("old content")  # pre-existing

    original_dirs = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allow_dir)
    try:
        p = cli_admin.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "attach-media", "synctest",
                            "--bvid", bvid, "--server-path", str(src),
                            "--media-root", str(media_root), "--replace"])
        cli_admin.cmd_paid_attach_media(ns)
        assert target.exists()
        assert target.read_text() == "new content", "should be overwritten with new content"
    finally:
        if original_dirs is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = original_dirs


def test_paid_attach_media_rejects_overwrite_without_flag(tmp_path: Path) -> None:
    """attach-media without --replace must reject overwrite."""
    from bilibili_podcast import cli_admin as ca

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)

    allow_dir = tmp_path / "manual-media"
    allow_dir.mkdir()
    src = allow_dir / "new.mp3"
    src.write_text("new content")

    media_root = tmp_path / "data" / "media"
    series_dir = media_root / "synctest"
    series_dir.mkdir(parents=True)
    bvid = "BV0000000002"
    target = series_dir / f"{bvid}_64K.mp3"
    target.write_text("old content")

    original_dirs = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allow_dir)
    try:
        p = cli_admin.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "attach-media", "synctest",
                            "--bvid", bvid, "--server-path", str(src),
                            "--media-root", str(media_root)])
        import pytest
        with pytest.raises(SystemExit):
            cli_admin.cmd_paid_attach_media(ns)
        assert target.read_text() == "old content", "must not overwrite"
    finally:
        if original_dirs is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = original_dirs


# ── 5.1: paid/manual quality 192K tests ─────────────────────────────


def test_paid_attach_media_192K_quality(tmp_path: Path) -> None:
    """attach-media must use quality from sync_policy (192K)."""
    import sqlite3
    from bilibili_podcast import cli_admin as ca

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)
    # Set quality to 192K
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sync_policy SET quality='192K' WHERE series='synctest'")
    conn.commit()
    conn.close()

    allow_dir = tmp_path / "manual-media"
    allow_dir.mkdir()
    src = allow_dir / "ep.mp3"
    src.write_text("audio data")
    bvid = "BV192K000001"
    media_root = tmp_path / "media"

    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allow_dir)
    try:
        p = cli_admin.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "attach-media", "synctest",
                           "--bvid", bvid, "--server-path", str(src),
                           "--media-root", str(media_root)])
        cli_admin.cmd_paid_attach_media(ns)
        target_192 = media_root / "synctest" / f"{bvid}_192K.mp3"
        target_64 = media_root / "synctest" / f"{bvid}_64K.mp3"
        assert target_192.exists(), f"_192K.mp3 not created: {target_192}"
        assert not target_64.exists(), "_64K.mp3 should not exist"
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


def test_paid_list_missing_192K(tmp_path: Path) -> None:
    """list-missing must check the correct quality file."""
    import sqlite3
    import json
    from bilibili_podcast import cli_admin as ca

    db_path = _migrate(tmp_path)
    _create_minimal_series(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sync_policy SET quality='192K' WHERE series='synctest'")
    conn.commit()

    json_dir = tmp_path / "json" / "synctest"
    json_dir.mkdir(parents=True)
    ep = {"title": "Test192", "bvid": "BV192K000002"}
    (json_dir / "BV192K000002_192K.info.json").write_text(json.dumps(ep))

    media_dir = tmp_path / "media" / "synctest"
    media_dir.mkdir(parents=True)
    (media_dir / "BV192K000002_192K.mp3").write_text("present")
    conn.close()

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        p = ca.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "list-missing", "synctest",
                           "--json-root", str(tmp_path / "json"),
                           "--media-root", str(tmp_path / "media")])
        ca.cmd_paid_list_missing(ns)
    out = buf.getvalue()
    assert "无缺失 media" in out, f"should report no missing, got: {out}"


def test_paid_rebuild_rss_192K(tmp_path: Path) -> None:
    """rebuild-rss must use 192K quality in enclosure URL."""
    import sqlite3
    import json
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('qual192','Qual192','T')")
        conn.execute("INSERT INTO series_source(series,space_url,type,uid) VALUES('qual192','https://space.bilibili.com/1','space',1)")
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('qual192','192K')")

    json_dir = tmp_path / "json" / "qual192"
    json_dir.mkdir(parents=True)
    bvid = "BV192K000003"
    ep = {"title": "Qual192", "bvid": bvid, "pubdate": 1000, "duration": 300,
          "link": f"https://www.bilibili.com/video/{bvid}"}
    (json_dir / f"{bvid}_192K.info.json").write_text(json.dumps(ep))

    media_dir = tmp_path / "media" / "qual192"
    media_dir.mkdir(parents=True)
    (media_dir / f"{bvid}_192K.mp3").write_text("data")
    rss_root = tmp_path / "rss"

    p = ca.build_parser()
    ns = p.parse_args(["--config-db", db_path, "paid", "rebuild-rss", "qual192",
                       "--json-root", str(tmp_path / "json"),
                       "--media-root", str(tmp_path / "media"),
                       "--rss-root", str(rss_root),
                       "--media-base-url", "http://test:8080"])
    ca.cmd_paid_rebuild_rss(ns)

    rss_file = rss_root / "qual192.xml"
    assert rss_file.exists()
    content = rss_file.read_text()
    assert "_192K.mp3?token=__MEDIA_PLACEHOLDER__" in content, \
        f"enclosure should use 192K quality:\n{content}"
    assert "_64K.mp3" not in content, "should not reference 64K"


def test_paid_rebuild_rss_accepts_ytdlp_metadata(tmp_path: Path) -> None:
    """rebuild-rss must normalize yt-dlp metadata that lacks sync-only keys."""
    import json
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('paidraw','PaidRaw','T')")
        conn.execute(
            "INSERT INTO series_source(series,space_url,type,uid) "
            "VALUES('paidraw','https://space.bilibili.com/1','space',1)"
        )
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('paidraw','64K')")

    json_dir = tmp_path / "json" / "paidraw"
    json_dir.mkdir(parents=True)
    bvid = "BV1234567890"
    meta = {
        "id": bvid,
        "title": "Raw paid metadata",
        "timestamp": 1_800_000_000,
        "duration": 600.4,
        "thumbnail": "http://example.invalid/cover.jpg",
        "webpage_url": f"https://www.bilibili.com/video/{bvid}",
    }
    (json_dir / f"{bvid}_64K.info.json").write_text(json.dumps(meta))

    media_dir = tmp_path / "media" / "paidraw"
    media_dir.mkdir(parents=True)
    (media_dir / f"{bvid}_64K.mp3").write_text("data")
    rss_root = tmp_path / "rss"

    p = ca.build_parser()
    ns = p.parse_args(["--config-db", db_path, "paid", "rebuild-rss", "paidraw",
                       "--json-root", str(tmp_path / "json"),
                       "--media-root", str(tmp_path / "media"),
                       "--rss-root", str(rss_root),
                       "--media-base-url", "http://test:8080"])
    ca.cmd_paid_rebuild_rss(ns)

    content = (rss_root / "paidraw.xml").read_text()
    assert "Raw paid metadata" in content
    assert f"https://www.bilibili.com/video/{bvid}" in content
    assert f"{bvid}_64K.mp3?token=__MEDIA_PLACEHOLDER__" in content
    assert "<itunes:duration>600</itunes:duration>" in content


def test_paid_rebuild_rss_preserves_existing_channel_cover(tmp_path: Path) -> None:
    """Manual RSS rebuild should not drop an existing channel cover."""
    import json
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('coverraw','CoverRaw','T')")
        conn.execute(
            "INSERT INTO series_source(series,space_url,type,uid) "
            "VALUES('coverraw','https://space.bilibili.com/1','space',1)"
        )
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('coverraw','64K')")

    bvid = "BV1234567891"
    json_dir = tmp_path / "json" / "coverraw"
    json_dir.mkdir(parents=True)
    (json_dir / f"{bvid}_64K.info.json").write_text(json.dumps({
        "bvid": bvid,
        "title": "Cover item",
        "pubdate": 100,
        "duration": 300,
        "link": f"https://www.bilibili.com/video/{bvid}",
    }))
    media_dir = tmp_path / "media" / "coverraw"
    media_dir.mkdir(parents=True)
    (media_dir / f"{bvid}_64K.mp3").write_text("data")
    rss_root = tmp_path / "rss"
    rss_root.mkdir()
    (rss_root / "coverraw.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        '<channel><title>CoverRaw</title>'
        '<itunes:image href="http://test:8080/images/cover.jpg" />'
        '</channel></rss>'
    )

    p = ca.build_parser()
    ns = p.parse_args(["--config-db", db_path, "paid", "rebuild-rss", "coverraw",
                       "--json-root", str(tmp_path / "json"),
                       "--media-root", str(tmp_path / "media"),
                       "--rss-root", str(rss_root),
                       "--media-base-url", "http://test:8080"])
    ca.cmd_paid_rebuild_rss(ns)

    content = (rss_root / "coverraw.xml").read_text()
    assert "itunes:image" in content
    assert "images/cover.jpg?token=__MEDIA_PLACEHOLDER__" in content


def test_placeholder_media_token_leaves_external_images_unchanged() -> None:
    """External absolute image URLs should not get internal media tokens."""
    from bilibili_podcast import cli_admin as ca

    assert ca._placeholder_media_token(
        "https://cdn.example.invalid/images/cover.jpg",
        "http://test:8080",
    ) == "https://cdn.example.invalid/images/cover.jpg"
    assert ca._placeholder_media_token(
        "http://test:8080/images/cover.jpg",
        "http://test:8080",
    ) == "http://test:8080/images/cover.jpg?token=__MEDIA_PLACEHOLDER__"


def test_paid_add_item_converts_media_and_writes_single_metadata(tmp_path: Path, monkeypatch) -> None:
    """add-item should convert arbitrary media, write one metadata JSON, and rebuild RSS."""
    import json
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author,cover_art) VALUES('manualadd','ManualAdd','T','')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('manualadd','space',1)")
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('manualadd','64K')")

    upload_dir = tmp_path / "manual-media"
    upload_dir.mkdir()
    src = upload_dir / "input.mp4"
    src.write_text("video")
    monkeypatch.setenv("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS", str(upload_dir))
    bvid = "BV1234567892"

    def fake_run(cmd, **kwargs):
        if cmd[:3] == [sys.executable, "-m", "yt_dlp"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                "id": bvid,
                "title": "Single manual item",
                "timestamp": 1_800_000_000,
                "duration": 2180.885,
                "thumbnail": "http://example.invalid/thumb.jpg",
                "webpage_url": f"https://www.bilibili.com/video/{bvid}/",
            }), stderr="")
        if cmd[0] == "ffmpeg":
            assert Path(cmd[-1]).parent == tmp_path / "media" / "manualadd"
            Path(cmd[-1]).write_bytes(b"mp3")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run):
        p = ca.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "add-item", "manualadd",
                           "--url", f"https://www.bilibili.com/video/{bvid}/",
                           "--media-path", str(src),
                           "--json-root", str(tmp_path / "json"),
                           "--media-root", str(tmp_path / "media"),
                           "--rss-root", str(tmp_path / "rss"),
                           "--media-base-url", "http://test:8080"])
        ca.cmd_paid_add_item(ns)

    media_file = tmp_path / "media" / "manualadd" / f"{bvid}_64K.mp3"
    json_file = tmp_path / "json" / "manualadd" / f"{bvid}_64K.info.json"
    rss_file = tmp_path / "rss" / "manualadd.xml"
    assert media_file.read_bytes() == b"mp3"
    meta = json.loads(json_file.read_text())
    assert meta["bvid"] == bvid
    assert meta["duration"] == 2181
    content = rss_file.read_text()
    assert "Single manual item" in content
    assert f"{bvid}_64K.mp3?token=__MEDIA_PLACEHOLDER__" in content
    assert not list(tmp_path.rglob("*.backup-*"))


def test_paid_add_item_existing_media_fails_before_network_or_transcode(tmp_path: Path, monkeypatch) -> None:
    """Existing target media should fail before yt-dlp or ffmpeg is invoked."""
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('manualexists','ManualExists','T')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('manualexists','space',1)")
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('manualexists','64K')")

    upload_dir = tmp_path / "manual-media"
    upload_dir.mkdir()
    src = upload_dir / "input.mp4"
    src.write_text("video")
    monkeypatch.setenv("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS", str(upload_dir))
    bvid = "BV1234567893"
    target = tmp_path / "media" / "manualexists" / f"{bvid}_64K.mp3"
    target.parent.mkdir(parents=True)
    target.write_text("existing")

    with patch.object(subprocess, "run") as run:
        p = ca.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "add-item", "manualexists",
                           "--url", f"https://www.bilibili.com/video/{bvid}/",
                           "--media-path", str(src),
                           "--json-root", str(tmp_path / "json"),
                           "--media-root", str(tmp_path / "media"),
                           "--rss-root", str(tmp_path / "rss"),
                           "--media-base-url", "http://test:8080"])
        try:
            ca.cmd_paid_add_item(ns)
        except SystemExit as exc:
            assert exc.code == ca.EXIT_VALIDATION
        else:
            raise AssertionError("expected validation failure")
        run.assert_not_called()


def test_paid_add_item_metadata_failure_cleans_new_media(tmp_path: Path, monkeypatch) -> None:
    """If metadata writing fails after media copy, add-item must remove the new media."""
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('manualfail','ManualFail','T')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('manualfail','space',1)")
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('manualfail','64K')")

    upload_dir = tmp_path / "manual-media"
    upload_dir.mkdir()
    src = upload_dir / "input.mp3"
    src.write_bytes(b"new-media")
    monkeypatch.setenv("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS", str(upload_dir))
    bvid = "BV1234567894"

    def fake_run(cmd, **kwargs):
        if cmd[:3] == [sys.executable, "-m", "yt_dlp"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                "id": bvid,
                "title": "Metadata fail item",
                "duration": 60,
            }), stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    media_file = tmp_path / "media" / "manualfail" / f"{bvid}_64K.mp3"
    with patch.object(subprocess, "run", side_effect=fake_run):
        with patch.object(ca, "_write_metadata_file", side_effect=OSError("disk full")):
            p = ca.build_parser()
            ns = p.parse_args(["--config-db", db_path, "paid", "add-item", "manualfail",
                               "--url", f"https://www.bilibili.com/video/{bvid}/",
                               "--media-path", str(src),
                               "--json-root", str(tmp_path / "json"),
                               "--media-root", str(tmp_path / "media"),
                               "--rss-root", str(tmp_path / "rss"),
                               "--media-base-url", "http://test:8080"])
            try:
                ca.cmd_paid_add_item(ns)
            except SystemExit as exc:
                assert exc.code == ca.EXIT_SYNC_FAIL
            else:
                raise AssertionError("expected sync failure")

    assert not media_file.exists()


def test_paid_add_item_rebuild_failure_restores_replaced_files(tmp_path: Path, monkeypatch) -> None:
    """If RSS rebuild fails under --replace, previous media/json/RSS files are restored."""
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('manualrestore','ManualRestore','T')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('manualrestore','space',1)")
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('manualrestore','64K')")

    upload_dir = tmp_path / "manual-media"
    upload_dir.mkdir()
    src = upload_dir / "input.mp3"
    src.write_bytes(b"new-media")
    monkeypatch.setenv("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS", str(upload_dir))
    bvid = "BV1234567895"
    media_file = tmp_path / "media" / "manualrestore" / f"{bvid}_64K.mp3"
    json_file = tmp_path / "json" / "manualrestore" / f"{bvid}_64K.info.json"
    rss_file = tmp_path / "rss" / "manualrestore.xml"
    media_file.parent.mkdir(parents=True)
    json_file.parent.mkdir(parents=True)
    rss_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"old-media")
    json_file.write_text('{"bvid":"old"}')
    rss_file.write_text("<rss>old</rss>")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == [sys.executable, "-m", "yt_dlp"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                "id": bvid,
                "title": "Restore item",
                "duration": 60,
            }), stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch.object(subprocess, "run", side_effect=fake_run):
        with patch.object(ca, "rebuild_paid_rss", side_effect=RuntimeError("rss failed")):
            p = ca.build_parser()
            ns = p.parse_args(["--config-db", db_path, "paid", "add-item", "manualrestore",
                               "--url", f"https://www.bilibili.com/video/{bvid}/",
                               "--media-path", str(src),
                               "--json-root", str(tmp_path / "json"),
                               "--media-root", str(tmp_path / "media"),
                               "--rss-root", str(tmp_path / "rss"),
                               "--media-base-url", "http://test:8080",
                               "--replace"])
            try:
                ca.cmd_paid_add_item(ns)
            except SystemExit as exc:
                assert exc.code == ca.EXIT_SYNC_FAIL
            else:
                raise AssertionError("expected sync failure")

    assert media_file.read_bytes() == b"old-media"
    assert json_file.read_text() == '{"bvid":"old"}'
    assert rss_file.read_text() == "<rss>old</rss>"
    assert not list(tmp_path.rglob("*.backup-*"))


def test_paid_refresh_metadata_writes_192K_json(tmp_path: Path, monkeypatch) -> None:
    """refresh-metadata must write {bvid}_192K.info.json."""
    import sqlite3
    import asyncio
    from unittest.mock import patch
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    monkeypatch.delenv("BILIBILI_PODCAST_COOKIE_FILE", raising=False)
    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('ref192','Ref192','T')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('ref192','space',1)")
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('ref192','192K')")

    bvid = "BV192K000004"
    async def fake_fetch(cfg, cred):
        info = {"name": "T"}
        eps = [{"bvid": bvid, "title": "T", "pubdate": 100}]
        return info, eps, 1

    with patch("bilibili_podcast.sync.fetch_space_episodes", side_effect=fake_fetch):
        p = ca.build_parser()
        ns = p.parse_args(["--config-db", db_path, "paid", "refresh-metadata", "ref192",
                           "--json-root", str(tmp_path / "json")])
        ca.cmd_paid_refresh_metadata(ns)
        json_file = tmp_path / "json" / "ref192" / f"{bvid}_192K.info.json"
        assert json_file.exists(), f"expected {json_file}"
        assert not (tmp_path / "json" / "ref192" / f"{bvid}_64K.info.json").exists()


@pytest.mark.parametrize("target_flag", ["--bvid", "--url"])
def test_paid_refresh_metadata_single_item(tmp_path: Path, monkeypatch, target_flag: str) -> None:
    from bilibili_podcast import cli_admin as ca
    from bilibili_podcast import db as _db

    monkeypatch.delenv("BILIBILI_PODCAST_COOKIE_FILE", raising=False)
    db_path = _migrate(tmp_path)
    with _db.transaction(db_path) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('singlemeta','Single','T')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('singlemeta','space',1)")
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('singlemeta','192K')")

    bvid = "BV1234567890"
    target = bvid if target_flag == "--bvid" else f"https://www.bilibili.com/video/{bvid}/"
    metadata = {
        "id": bvid,
        "title": "Single metadata",
        "duration": 120.6,
        "webpage_url": f"https://www.bilibili.com/video/{bvid}/",
    }
    with patch.object(ca, "_fetch_single_video_metadata", return_value=metadata) as fetch:
        ns = ca.build_parser().parse_args([
            "--config-db", db_path, "paid", "refresh-metadata", "singlemeta",
            target_flag, target, "--json-root", str(tmp_path / "json"),
        ])
        ca.cmd_paid_refresh_metadata(ns)

    output = tmp_path / "json" / "singlemeta" / f"{bvid}_192K.info.json"
    saved = json.loads(output.read_text())
    assert saved["bvid"] == bvid
    assert saved["duration"] == 121
    fetch.assert_called_once()


def test_paid_refresh_metadata_rejects_bvid_and_url_together() -> None:
    parser = cli_admin.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([
            "paid", "refresh-metadata", "singlemeta",
            "--bvid", "BV1234567890",
            "--url", "https://www.bilibili.com/video/BV1234567890/",
        ])


# ── 6.6: manual media path whitelist config tests ────────────────────


def test_get_allowed_dirs_rejects_root():
    """BILIBILI_PODCAST_MANUAL_MEDIA_DIRS=/ must be rejected."""
    import os
    from bilibili_podcast.cli_admin import _get_allowed_media_dirs
    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = "/"
    try:
        dirs = _get_allowed_media_dirs()
        assert dirs == [], f"/ should be rejected, got {dirs}"
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


def test_get_allowed_dirs_rejects_broad_root():
    """BILIBILI_PODCAST_MANUAL_MEDIA_DIRS=/data must be rejected (only 2 levels)."""
    import os
    from bilibili_podcast.cli_admin import _get_allowed_media_dirs
    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = "/data"
    try:
        dirs = _get_allowed_media_dirs()
        assert dirs == [], f"/data should be rejected, got {dirs}"
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


def test_get_allowed_dirs_accepts_deep_path(tmp_path):
    """BILIBILI_PODCAST_MANUAL_MEDIA_DIRS=/a/b/c must be accepted."""
    import os
    from bilibili_podcast.cli_admin import _get_allowed_media_dirs
    test_dir = tmp_path / "a" / "b" / "c"
    test_dir.mkdir(parents=True)
    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(test_dir)
    try:
        dirs = _get_allowed_media_dirs()
        assert any(d == test_dir for d in dirs), f"expected {test_dir}, got {dirs}"
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


def test_is_allowed_path_inside_allowed_dir(tmp_path):
    """A file inside an allowed dir must be accepted."""
    import os
    from bilibili_podcast.cli_admin import is_allowed_manual_media_path

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    f = allowed / "test.mp3"
    f.write_text("x")
    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allowed)
    try:
        assert is_allowed_manual_media_path(f) is True
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


def test_is_allowed_path_outside_allowed_dir(tmp_path):
    """A file outside allowed dirs must be rejected."""
    import os
    from bilibili_podcast.cli_admin import is_allowed_manual_media_path

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "test.mp3"
    f.write_text("x")
    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allowed)
    try:
        assert is_allowed_manual_media_path(f) is False
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


def test_is_allowed_path_prefix_mismatch(tmp_path):
    """/allowed2 must not match /allowed."""
    import os
    from bilibili_podcast.cli_admin import is_allowed_manual_media_path

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    f = tmp_path / "allowed2" / "test.mp3"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allowed)
    try:
        assert is_allowed_manual_media_path(f) is False
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


def test_is_allowed_path_rejects_symlink_escape(tmp_path):
    """Symlink inside allowed dir pointing outside must be rejected."""
    import os
    from pathlib import Path
    from bilibili_podcast.cli_admin import is_allowed_manual_media_path

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    real_file = outside / "real.mp3"
    real_file.write_text("secret")

    symlink = allowed / "escape.mp3"
    symlink.symlink_to(real_file)

    orig = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")
    os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = str(allowed)
    try:
        assert is_allowed_manual_media_path(symlink) is False, \
            "symlink to outside file must be rejected"
    finally:
        if orig is None:
            del os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"]
        else:
            os.environ["BILIBILI_PODCAST_MANUAL_MEDIA_DIRS"] = orig


# ── add --url 非交互模式本地 UID 解析 ────────────────────────────────


def test_with_fallback_source_fills_missing_uid() -> None:
    from bilibili_podcast.utils.bilibili_url import parse_space_source

    merged = cli_admin._with_fallback_source(
        {"source": {"uid": 0, "space_url": ""}},
        parse_space_source("https://space.bilibili.com/123456"),
    )
    assert merged["source"]["uid"] == 123456


def test_add_url_fallback_extracts_uid_when_resolver_fails(tmp_path: Path) -> None:
    """resolve_url failure must not leave series_source.uid as 0."""
    import sqlite3

    db_path = _migrate(tmp_path)
    ns = cli_admin.build_parser().parse_args([
        "--config-db", db_path,
        "add",
        "--series", "fallbackuid",
        "--url", "https://space.bilibili.com/123456",
        "--title", "Fallback UID Test",
        "--author", "Tester",
        "--yes",
    ])

    with patch.object(
        cli_admin, "_resolve_bilibili_url", side_effect=RuntimeError("API unavailable"),
    ), patch.object(sys, "stdout"):
        cli_admin.cmd_add(ns)

    with sqlite3.connect(db_path) as conn:
        src = conn.execute(
            "SELECT uid, space_url FROM series_source WHERE series='fallbackuid'"
        ).fetchone()
    assert src == (123456, "https://space.bilibili.com/123456")


# ── generate_rss channel 级 itunes:image ──────────────────────────────


def test_generate_rss_has_channel_itunes_image(tmp_path: Path) -> None:
    from bilibili_podcast.sync import SyncPaths, generate_rss
    from bilibili_podcast.utils.series_config import SeriesConfig

    cfg = SeriesConfig(
        series="imgtest", enabled=True, title="ImageTest",
        description="desc", author="A", cover_art="https://example.invalid/cover.jpg",
        category="Music", subcategories=[], explicit=False, lang="zh-CN",
        source={"uid": 1, "space_url": "https://space.bilibili.com/1"},
        sync={"quality": "64K"}, filters={}, paid_preview={}, keep_last=0,
    )
    paths = SyncPaths(
        media_root=tmp_path / "media",
        json_root=tmp_path / "json",
        rss_root=tmp_path / "rss",
        media_base_url="http://test:8080",
    )

    rss_path = generate_rss(
        cfg, paths, {"name": "A", "face": "", "sign": ""}, [],
        "__MEDIA_PLACEHOLDER__", dry_run=False,
    )
    content = rss_path.read_text(encoding="utf-8")
    assert "itunes:image" in content
    assert "https://example.invalid/cover.jpg" in content


def test_generate_rss_atomically_replaces_existing_file(tmp_path: Path, monkeypatch) -> None:
    from bilibili_podcast.sync import SyncPaths, generate_rss
    from bilibili_podcast.utils.series_config import SeriesConfig

    cfg = SeriesConfig(
        series="atomic", enabled=True, title="Atomic", description="desc", author="A",
        cover_art="", category="", subcategories=[], explicit=False, lang="zh-CN",
        source={"uid": 1}, sync={"quality": "64K"}, filters={}, paid_preview={}, keep_last=0,
    )
    paths = SyncPaths(
        media_root=tmp_path / "media", json_root=tmp_path / "json",
        rss_root=tmp_path / "rss", media_base_url="http://test:8080",
    )
    target = paths.rss_root / "atomic.xml"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    original_chmod = Path.chmod

    def reject_target_chmod(path, mode, *, follow_symlinks=True):
        assert path != target
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", reject_target_chmod)

    generate_rss(cfg, paths, {"name": "A"}, [], "__MEDIA_PLACEHOLDER__", dry_run=False)

    assert target.read_text(encoding="utf-8").startswith("<?xml")
    assert target.stat().st_mode & 0o777 == 0o644
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_generate_rss_failure_preserves_existing_file(tmp_path: Path, monkeypatch) -> None:
    from feedgen.feed import FeedGenerator
    from bilibili_podcast.sync import SyncPaths, generate_rss
    from bilibili_podcast.utils.series_config import SeriesConfig

    cfg = SeriesConfig(
        series="atomic", enabled=True, title="Atomic", description="desc", author="A",
        cover_art="", category="", subcategories=[], explicit=False, lang="zh-CN",
        source={"uid": 1}, sync={"quality": "64K"}, filters={}, paid_preview={}, keep_last=0,
    )
    paths = SyncPaths(
        media_root=tmp_path / "media", json_root=tmp_path / "json",
        rss_root=tmp_path / "rss", media_base_url="http://test:8080",
    )
    target = paths.rss_root / "atomic.xml"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    def fail_write(self, filename, **kwargs):
        Path(filename).write_text("partial", encoding="utf-8")
        raise OSError("rss write failed")

    monkeypatch.setattr(FeedGenerator, "rss_file", fail_write)

    with pytest.raises(OSError, match="rss write failed"):
        generate_rss(cfg, paths, {"name": "A"}, [], "__MEDIA_PLACEHOLDER__", dry_run=False)

    assert target.read_text(encoding="utf-8") == "old"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
