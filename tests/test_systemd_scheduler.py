"""Tests for systemd scheduler backend — cron conversion, unit gen, plan."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bilibili_podcast.services import systemd_scheduler as sysd
from bilibili_podcast.services.scheduler_service import ScheduleEntry, validate_schedules


def _entry(schedule: str, kind: str = "primary") -> ScheduleEntry:
    return ScheduleEntry(None, "test", schedule, True, 0, kind)


class TestScheduleValidation:
    def test_duplicate_primary_and_retry_rejected(self):
        with pytest.raises(ValueError, match="duplicate schedule"):
            validate_schedules([_entry("0 10 * * *"), _entry("0 10 * * *", "retry")], "12h")

    def test_primary_interval_shorter_than_period_rejected(self):
        with pytest.raises(ValueError, match="less than update_period"):
            validate_schedules([_entry("0 10 * * *"), _entry("0 20 * * *")], "12h")

    def test_primary_interval_equal_to_period_allowed(self):
        validate_schedules([_entry("0 0 * * *"), _entry("0 12 * * *")], "12h")

    def test_retry_inside_update_period_allowed(self):
        validate_schedules([_entry("0 10 * * *"), _entry("0 12 * * *", "retry")], "12h")

    def test_cross_midnight_interval_rejected(self):
        with pytest.raises(ValueError, match="less than update_period"):
            validate_schedules([_entry("0 23 * * 1"), _entry("0 1 * * 2")], "3h")

    def test_step_expression_is_validated(self):
        validate_schedules([_entry("5 */12 * * *")], "12h")

    def test_retry_without_primary_rejected(self):
        with pytest.raises(ValueError, match="at least one primary"):
            validate_schedules([_entry("0 12 * * *", "retry")], "12h")


class TestRetryLifecycle:
    @staticmethod
    def _database(tmp_path: Path, *, retry: bool = True) -> Path:
        from bilibili_podcast import db

        db_path = tmp_path / "retry.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('rt','R','A')")
            conn.execute("INSERT INTO sync_policy(series,update_period) VALUES('rt','12h')")
            conn.execute("INSERT INTO cron_schedule(series,schedule,kind,position) VALUES('rt','0 10 * * *','primary',0)")
            if retry:
                conn.execute("INSERT INTO cron_schedule(series,schedule,kind,position) VALUES('rt','0 12 * * *','retry',1)")
        return db_path

    def test_apply_installs_separate_retry_units(self, tmp_path: Path):
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = self._database(tmp_path)
        ok = SchedulerCommandResult("systemd", "ok", 0, "", "")
        enabled_calls = {False: 0, True: 0}
        active_calls = {False: 0, True: 0}

        def enabled(series, *, scheduled_retry=False):
            enabled_calls[scheduled_retry] += 1
            return enabled_calls[scheduled_retry] > 1

        def active(series, *, scheduled_retry=False):
            active_calls[scheduled_retry] += 1
            return active_calls[scheduled_retry] > 1

        svc = SchedulerService(str(db_path))
        with patch.object(sysd_mod, "SYSTEMD_DIR", tmp_path), \
                patch.object(sysd_mod, "write_unit", return_value=ok) as write, \
                patch.object(sysd_mod, "daemon_reload", return_value=ok), \
                patch.object(sysd_mod, "enable_timer", return_value=ok), \
                patch.object(sysd_mod, "restart_timer", return_value=ok), \
                patch.object(sysd_mod, "timer_is_enabled", side_effect=enabled), \
                patch.object(sysd_mod, "timer_is_active", side_effect=active), \
                patch.object(svc, "_exclude_series_from_cron"):
            result = svc.apply(backend="systemd", series="rt")

        assert result.returncode == 0
        retry_service = next(
            call.args[2] for call in write.call_args_list
            if call.kwargs.get("scheduled_retry") and call.args[1] == "service"
        )
        assert "--scheduled-retry" in retry_service

    def test_apply_without_retry_disables_stale_retry_timer(self, tmp_path: Path):
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = self._database(tmp_path, retry=False)
        ok = SchedulerCommandResult("systemd", "ok", 0, "", "")
        enabled_calls = {False: 0, True: 0}
        active_calls = {False: 0, True: 0}

        def enabled(series, *, scheduled_retry=False):
            enabled_calls[scheduled_retry] += 1
            return True if scheduled_retry else enabled_calls[False] > 1

        def active(series, *, scheduled_retry=False):
            active_calls[scheduled_retry] += 1
            return True if scheduled_retry else active_calls[False] > 1

        svc = SchedulerService(str(db_path))
        with patch.object(sysd_mod, "SYSTEMD_DIR", tmp_path), \
                patch.object(sysd_mod, "write_unit", return_value=ok), \
                patch.object(sysd_mod, "daemon_reload", return_value=ok), \
                patch.object(sysd_mod, "enable_timer", return_value=ok), \
                patch.object(sysd_mod, "restart_timer", return_value=ok), \
                patch.object(sysd_mod, "disable_timer", return_value=ok) as disable, \
                patch.object(sysd_mod, "timer_is_enabled", side_effect=enabled), \
                patch.object(sysd_mod, "timer_is_active", side_effect=active), \
                patch.object(svc, "_exclude_series_from_cron"):
            result = svc.apply(backend="systemd", series="rt")

        assert result.returncode == 0
        disable.assert_called_once_with("rt", scheduled_retry=True)

    def test_retry_unit_write_failure_rolls_back_primary(self, tmp_path: Path):
        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = self._database(tmp_path)
        ok = SchedulerCommandResult("systemd", "ok", 0, "", "")
        failed = SchedulerCommandResult("systemd", "write", 1, "", "write failed")
        svc = SchedulerService(str(db_path))
        with patch.object(sysd_mod, "SYSTEMD_DIR", tmp_path), \
                patch.object(sysd_mod, "write_unit", side_effect=[ok, ok, failed]), \
                patch.object(sysd_mod, "remove_unit", return_value=ok) as remove, \
                patch.object(sysd_mod, "disable_timer", return_value=ok) as disable, \
                patch.object(sysd_mod, "daemon_reload", return_value=ok), \
                patch.object(sysd_mod, "timer_is_enabled", return_value=False), \
                patch.object(sysd_mod, "timer_is_active", return_value=False), \
                patch.object(svc, "_exclude_series_from_cron") as exclude:
            result = svc.apply(backend="systemd", series="rt")

        assert result.returncode != 0
        assert disable.call_count == 2
        assert remove.call_count == 3
        exclude.assert_not_called()
        with db.transaction(str(db_path)) as conn:
            assert db.get_scheduler_backend(conn, "rt") == "cron"

    def test_cron_plan_rejects_retry_without_running_script(self, tmp_path: Path):
        from bilibili_podcast.services.scheduler_service import SchedulerService

        db_path = self._database(tmp_path)
        with patch("subprocess.run") as run:
            result = SchedulerService(str(db_path), crontab_script="/unused").plan(
                backend="cron",
            )
        assert result.returncode != 0
        assert "does not support retry" in (result.error or "")
        run.assert_not_called()

    def test_disable_rejects_retry_before_touching_timers(self, tmp_path: Path):
        from bilibili_podcast.services.scheduler_service import SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = self._database(tmp_path)
        with patch.object(sysd_mod, "disable_timer") as disable:
            result = SchedulerService(str(db_path)).disable_systemd("rt")
        assert result.returncode != 0
        assert "remove them before restoring cron" in result.stderr
        disable.assert_not_called()


class TestMediaUrlTokenGuard:
    """media_url() must reject empty token to prevent tokenless enclosure URLs."""

    def test_empty_token_raises(self):
        from bilibili_podcast.sync import media_url
        from bilibili_podcast.utils.series_config import SeriesConfig
        from bilibili_podcast.sync import SyncPaths
        from pathlib import Path

        cfg = SeriesConfig(
            series="test", enabled=True, title="T", author="A",
            description="", cover_art="", category="",
            subcategories=[], explicit=False, lang="zh-CN",
            source={}, sync={}, filters={}, paid_preview={}, keep_last=100,
        )
        paths = SyncPaths(
            media_root=Path("/m"), json_root=Path("/j"),
            rss_root=Path("/r"), media_base_url="http://example.com",
        )
        import pytest
        with pytest.raises(ValueError, match="media token is required"):
            media_url(cfg, paths, "BVtest123456", token=None)
    def test_daily(self):
        assert sysd.cron_to_oncalendar("15 3 * * *") == "*-*-* 03:15:00"

    def test_weekly_sunday(self):
        assert sysd.cron_to_oncalendar("5 11 * * 0") == "Sun *-*-* 11:05:00"

    def test_weekly_thursday(self):
        assert sysd.cron_to_oncalendar("30 18 * * 4") == "Thu *-*-* 18:30:00"

    def test_zero_padded(self):
        assert sysd.cron_to_oncalendar("6 8 * * 1") == "Mon *-*-* 08:06:00"

    def test_complex_interval(self):
        assert sysd.cron_to_oncalendar("*/10 * * * *") is None

    def test_range(self):
        assert sysd.cron_to_oncalendar("0 6 * * 1-5") is None

    def test_day_of_month(self):
        assert sysd.cron_to_oncalendar("0 6 1 * *") is None


class TestUnitGeneration:
    def test_service_contains_execstart(self):
        content = sysd.generate_service("testseries")
        assert "ExecStart=" in content
        assert "testseries" in content
        assert "Restart=no" in content

    def test_service_contains_token(self):
        content = sysd.generate_service("testseries")
        assert "__MEDIA_PLACEHOLDER__" in content
        assert "--token" in content

    def test_service_does_not_use_shell_env_file(self):
        content = sysd.generate_service("testseries")
        assert "EnvironmentFile=" not in content
        assert "Environment=PLAYWRIGHT_BROWSERS_PATH=" in content
        assert "ExecStartPost=" not in content
        assert "--publish-script" in content

    def test_env_file_value_reads_export_syntax(self, tmp_path: Path):
        env_file = tmp_path / "bilipod-env.sh"
        env_file.write_text('export BILIPOD_MEDIA_BASE_URL="http://media.example"\n')
        assert sysd._env_file_value(env_file, "BILIPOD_MEDIA_BASE_URL") == "http://media.example"

    def test_generate_service_no_debug_by_default(self, monkeypatch):
        # 清除可能干扰的环境变量，默认 level=INFO 不应产生 --debug
        monkeypatch.delenv("BILIPOD_SYNC_LOG_LEVEL", raising=False)
        content = sysd.generate_service("testseries")
        assert "--debug" not in content

    def test_generate_service_log_level_warning(self, monkeypatch):
        monkeypatch.setenv("BILIPOD_SYNC_LOG_LEVEL", "WARNING")
        content = sysd.generate_service("testseries")
        assert "--log-level WARNING" in content

    def test_timer_contains_oncalendar(self):
        content = sysd.generate_timer("testseries", ["*-*-* 03:15:00"])
        assert "OnCalendar=*-*-* 03:15:00" in content

    def test_timer_persistent_false(self):
        content = sysd.generate_timer("testseries", ["*-*-* 03:15:00"])
        assert "Persistent=false" in content
        assert "Persistent=true" not in content

    def test_timer_multiple_oncalendar(self):
        content = sysd.generate_timer("testseries", ["*-*-* 03:15:00", "*-*-* 15:30:00"])
        assert "OnCalendar=*-*-* 03:15:00" in content
        assert "OnCalendar=*-*-* 15:30:00" in content

    def test_unit_name(self):
        assert sysd.unit_name("demo-series") == "bilipod-sync@demo-series.service"
        assert sysd.unit_name("demo-series", "timer") == "bilipod-sync@demo-series.timer"

    def test_write_unit_reports_sudo_chmod_failure(self, tmp_path: Path):
        import subprocess

        from bilibili_podcast.services import systemd_scheduler as sysd_mod

        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[1] == "chmod":
                raise subprocess.CalledProcessError(1, command, stderr="chmod failed")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(sysd_mod, "SYSTEMD_DIR", tmp_path), \
                patch.object(sysd_mod.tempfile, "NamedTemporaryFile", side_effect=PermissionError("denied")), \
                patch.object(sysd_mod.subprocess, "run", side_effect=fake_run):
            result = sysd_mod.write_unit("demo", "service", "[Unit]\n")

        assert result.returncode == 1
        assert result.error is not None
        assert "failed to write" in result.error
        assert any(command[1] == "chmod" for command in calls)

    def test_plan_output_contains_service_and_timer(self, tmp_path: Path):
        """Plan output must contain service/timer content but not write files."""
        from bilibili_podcast.services.scheduler_service import SchedulerService
        from bilibili_podcast import db

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO series(series,title,author) VALUES('pt', 'P', 'A')"
            )
            conn.execute(
                "INSERT INTO series_source(series,type,uid) VALUES('pt','space',1)"
            )
            conn.execute("INSERT INTO sync_policy(series) VALUES('pt')")
            conn.execute(
                "INSERT INTO cron_schedule(series,schedule,position) VALUES('pt','15 3 * * *',0)"
            )

        svc = SchedulerService(str(db_path), crontab_script="/nonexistent")
        result = svc.plan(backend="systemd", series="pt")
        assert result.returncode == 0
        assert "bilipod-sync@pt.service" in result.stdout
        assert "OnCalendar=" in result.stdout

        # Plan must not write unit files
        assert not (sysd.SYSTEMD_DIR / sysd.unit_name("pt")).exists()

    def test_plan_rejects_unconvertible(self, tmp_path: Path):
        from bilibili_podcast.services.scheduler_service import SchedulerService
        from bilibili_podcast import db

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO series(series,title,author) VALUES('pt', 'P', 'A')"
            )
            conn.execute(
                "INSERT INTO series_source(series,type,uid) VALUES('pt','space',1)"
            )
            conn.execute("INSERT INTO sync_policy(series) VALUES('pt')")
            conn.execute(
                "INSERT INTO cron_schedule(series,schedule,position) VALUES('pt','*/10 * * * *',0)"
            )

        svc = SchedulerService(str(db_path), crontab_script="/nonexistent")
        result = svc.plan(backend="systemd", series="pt")
        assert result.returncode != 0
        assert "不支持转换" in result.stdout or "unsupported" in (result.stderr + (result.error or "")).lower()

    def test_plan_requires_series(self, tmp_path: Path):
        from bilibili_podcast.services.scheduler_service import SchedulerService

        svc = SchedulerService(str(tmp_path / "test.db"))
        result = svc.plan(backend="systemd")
        assert result.returncode != 0
        assert "--series is required" in (result.stdout + result.stderr + (result.error or ""))


class TestApply:
    def test_apply_requires_series(self, tmp_path: Path):
        from bilibili_podcast.services.scheduler_service import SchedulerService

        svc = SchedulerService(str(tmp_path / "test.db"))
        result = svc.apply(backend="systemd")
        assert result.returncode != 0
        assert "--series is required" in (result.stdout + result.stderr + (result.error or ""))

    def test_apply_no_schedules(self, tmp_path: Path):
        from bilibili_podcast.services.scheduler_service import SchedulerService
        from bilibili_podcast import db

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO series(series,title,author) VALUES('pt', 'P', 'A')"
            )
            conn.execute(
                "INSERT INTO series_source(series,type,uid) VALUES('pt','space',1)"
            )
            conn.execute("INSERT INTO sync_policy(series) VALUES('pt')")

        svc = SchedulerService(str(db_path), crontab_script="/nonexistent")
        result = svc.apply(backend="systemd", series="pt")
        assert result.returncode != 0
        assert "no schedules" in (result.stdout + result.stderr + (result.error or ""))

    def test_apply_removes_new_service_when_timer_write_fails(self, tmp_path: Path):
        from unittest.mock import call

        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('pt','P','A')")
            conn.execute("INSERT INTO cron_schedule(series,schedule,position) VALUES('pt','15 3 * * *',0)")

        ok = SchedulerCommandResult(
            backend="systemd", action="write-unit", returncode=0, stdout="", stderr="",
        )
        failed = SchedulerCommandResult(
            backend="systemd", action="write-unit", returncode=1, stdout="", stderr="write failed",
        )
        with patch.object(sysd_mod, "SYSTEMD_DIR", tmp_path), \
                patch.object(sysd_mod, "write_unit", side_effect=[ok, failed]), \
                patch.object(sysd_mod, "remove_unit", return_value=ok) as remove_unit, \
                patch.object(sysd_mod, "daemon_reload", return_value=ok):
            result = SchedulerService(str(db_path)).apply(backend="systemd", series="pt")

        assert result.returncode == 1
        assert call("pt", "service", scheduled_retry=False) in remove_unit.call_args_list

    def test_apply_restores_existing_service_when_timer_write_fails(self, tmp_path: Path):
        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('pt','P','A')")
            conn.execute("INSERT INTO cron_schedule(series,schedule,position) VALUES('pt','15 3 * * *',0)")

        service_path = tmp_path / sysd_mod.unit_name("pt", "service")
        service_path.write_text("old service\n")
        ok = SchedulerCommandResult(
            backend="systemd", action="write-unit", returncode=0, stdout="", stderr="",
        )
        failed = SchedulerCommandResult(
            backend="systemd", action="write-unit", returncode=1, stdout="", stderr="write failed",
        )
        with patch.object(sysd_mod, "SYSTEMD_DIR", tmp_path), \
                patch.object(sysd_mod, "write_unit", side_effect=[ok, failed, ok]) as write_unit, \
                patch.object(sysd_mod, "remove_unit", return_value=ok), \
                patch.object(sysd_mod, "daemon_reload", return_value=ok):
            result = SchedulerService(str(db_path)).apply(backend="systemd", series="pt")

        assert result.returncode == 1
        write_unit.assert_any_call(
            "pt", "service", "old service\n", scheduled_retry=False,
        )

    def test_apply_marks_systemd_backend_without_disabling_schedule(self, tmp_path: Path):
        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('pt','P','A')")
            conn.execute("INSERT INTO cron_schedule(series,schedule,position) VALUES('pt','15 3 * * *',0)")

        ok = SchedulerCommandResult(
            backend="systemd", action="ok", returncode=0, stdout="", stderr="",
        )
        svc = SchedulerService(str(db_path))
        with patch.object(sysd_mod, "SYSTEMD_DIR", tmp_path), \
                patch.object(sysd_mod, "write_unit", return_value=ok), \
                patch.object(sysd_mod, "daemon_reload", return_value=ok), \
                patch.object(sysd_mod, "enable_timer", return_value=ok), \
                patch.object(sysd_mod, "restart_timer", return_value=ok), \
                patch.object(sysd_mod, "timer_is_enabled", side_effect=[False, False, True]), \
                patch.object(sysd_mod, "timer_is_active", side_effect=[False, False, True]), \
                patch.object(svc, "_exclude_series_from_cron"):
            result = svc.apply(backend="systemd", series="pt")

        assert result.returncode == 0
        with db.transaction(str(db_path)) as conn:
            assert db.get_scheduler_backend(conn, "pt") == "systemd"
            assert conn.execute(
                "SELECT enabled FROM cron_schedule WHERE series='pt'"
            ).fetchone()[0] == 1


class TestStatus:
    def test_status_systemd_no_traceback(self, tmp_path: Path):
        """status --backend systemd must not crash even without systemd."""
        from bilibili_podcast.services.scheduler_service import SchedulerService
        from bilibili_podcast import db

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        svc = SchedulerService(str(db_path))
        try:
            result = svc.status(backend="systemd", series="nonexistent")
            # May return empty list or have systemd errors, but no crash
            assert isinstance(result, list)
        except Exception:
            pytest.fail("status(backend='systemd') raised unexpected exception")


class TestCLIParsing:
    def test_scheduler_plan_backend_systemd_series(self):
        from bilibili_podcast.cli_admin import build_parser

        p = build_parser()
        ns = p.parse_args(
            ["scheduler", "plan", "--backend", "systemd", "--series", "demo-series"]
        )
        assert ns.scheduler_backend == "systemd"
        assert ns.series == "demo-series"

    def test_scheduler_apply_backend_systemd_series_yes(self):
        from bilibili_podcast.cli_admin import build_parser

        p = build_parser()
        ns = p.parse_args(
            ["scheduler", "apply", "--backend", "systemd", "--series", "demo-series", "--yes"]
        )
        assert ns.scheduler_backend == "systemd"
        assert ns.series == "demo-series"
        assert ns.scheduler_yes is True

    def test_scheduler_disable_backend_systemd_series_yes(self):
        from bilibili_podcast.cli_admin import build_parser

        p = build_parser()
        ns = p.parse_args(
            ["scheduler", "disable", "--backend", "systemd", "--series", "demo-series", "--yes"]
        )
        assert ns.scheduler_backend == "systemd"
        assert ns.series == "demo-series"
        assert ns.scheduler_disable_yes is True

    def test_cron_compat_still_works(self):
        from bilibili_podcast.cli_admin import build_parser

        p = build_parser()
        ns = p.parse_args(["cron", "plan"])
        assert ns.handler is not None

        ns2 = p.parse_args(["cron", "apply", "--yes"])
        assert ns2.handler is not None


class TestApplyRollback:
    """apply must rollback on restart_timer / timer_is_active failure."""

    def test_restart_timer_failure_rollbacks_and_restores_active_state(self, tmp_path: Path):
        """If restart_timer fails, rollback must restore the previous timer state."""
        import os
        os.environ["BILIPOD_SYSTEMD_DIR"] = str(tmp_path)
        # Re-import to pick up the new env var
        from unittest.mock import patch
        import importlib
        import bilibili_podcast.services.systemd_scheduler as sysd_mod
        importlib.reload(sysd_mod)
        from bilibili_podcast.services.scheduler_service import SchedulerService
        from bilibili_podcast import db

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('pt','P','A')")
            conn.execute("INSERT INTO series_source(series,type,uid) VALUES('pt','space',1)")
            conn.execute("INSERT INTO sync_policy(series) VALUES('pt')")
            conn.execute("INSERT INTO cron_schedule(series,schedule,position) VALUES('pt','15 3 * * *',0)")

        calls = []
        def fake_systemctl(*args):
            calls.append(args)
            from subprocess import CompletedProcess
            if args[0] == "restart":
                return CompletedProcess(args, 1, "", "restart failed")
            return CompletedProcess(args, 0, "enabled\n" if args[0] == "is-enabled" else "active\n", "")

        with patch.object(sysd_mod, "_systemctl", side_effect=fake_systemctl):
            svc = SchedulerService(str(db_path), crontab_script="/nonexistent")
            result = svc.apply(backend="systemd", series="pt")
            assert result.returncode != 0
            assert "rolled back" in (result.error or "").lower()
            assert any(c[0] == "disable" for c in calls), "disable_timer was not called"
            assert any(c[0] == "start" for c in calls), "previous active state was not restored"

    def test_timer_is_active_failure_rollbacks(self, tmp_path: Path):
        """If timer_is_active returns False, disable_timer must be called and
        the error must mention 'active verification' not just 'enabled'."""
        import os
        os.environ["BILIPOD_SYSTEMD_DIR"] = str(tmp_path)
        import importlib
        import bilibili_podcast.services.systemd_scheduler as sysd_mod
        importlib.reload(sysd_mod)
        from unittest.mock import patch
        from bilibili_podcast.services.scheduler_service import SchedulerService
        from bilibili_podcast import db

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('pt','P','A')")
            conn.execute("INSERT INTO series_source(series,type,uid) VALUES('pt','space',1)")
            conn.execute("INSERT INTO sync_policy(series) VALUES('pt')")
            conn.execute("INSERT INTO cron_schedule(series,schedule,position) VALUES('pt','15 3 * * *',0)")

        calls = []
        def fake_systemctl(*args):
            calls.append(args)
            from subprocess import CompletedProcess
            # is-enabled returns "enabled" → passes enabled check
            # is-active  returns "inactive" → fails active check
            if args[0] == "is-active":
                return CompletedProcess(args, 1, "inactive\n", "")
            return CompletedProcess(args, 0, "enabled\n" if args[0] == "is-enabled" else "active\n", "")

        with patch.object(sysd_mod, "_systemctl", side_effect=fake_systemctl):
            svc = SchedulerService(str(db_path), crontab_script="/nonexistent")
            result = svc.apply(backend="systemd", series="pt")
            assert result.returncode != 0
            # Must mention active verification, not just enabled
            assert "active verification" in (result.error or "").lower(), (
                f"expected 'active verification' in error, got: {result.error}"
            )
            assert any(c[0] == "disable" for c in calls), "disable_timer was not called"
            assert any(c[0] == "is-active" for c in calls), "is-active was not called"
            assert "rolled back" in (result.error or "").lower()
            assert any(c[0] == "disable" for c in calls), "disable_timer was not called"

    def test_enable_no_now(self, tmp_path: Path):
        """enable_timer must not call enable --now."""
        from unittest.mock import patch
        with patch("bilibili_podcast.services.systemd_scheduler._systemctl") as mock:
            sysd.enable_timer("ptest")
            call_args = mock.call_args[0]
            assert call_args[0] == "enable"
            assert "--now" not in call_args, "enable --now is forbidden"

    def test_start_only_timer(self, tmp_path: Path):
        """start_timer must call 'start <timer>' not 'start <service>'."""
        from unittest.mock import patch
        with patch("bilibili_podcast.services.systemd_scheduler._systemctl") as mock:
            sysd.start_timer("ptest")
            call_args = mock.call_args[0]
            assert call_args[0] == "start"
            assert call_args[1].endswith(".timer"), f"expected .timer, got {call_args[1]}"
            assert not call_args[1].endswith(".service"), "must not start .service"

    def test_restart_only_timer(self, tmp_path: Path):
        """restart_timer must reload the timer without restarting the service."""
        from unittest.mock import patch
        with patch("bilibili_podcast.services.systemd_scheduler._systemctl") as mock:
            sysd.restart_timer("ptest")
            call_args = mock.call_args[0]
            assert call_args[0] == "restart"
            assert call_args[1].endswith(".timer"), f"expected .timer, got {call_args[1]}"
            assert not call_args[1].endswith(".service"), "must not restart .service"


# ── 5.2: RSS padding token guard tests ────────────────────────────


class TestTokenSafety:
    def test_is_safe_enclosure_url(self):
        from bilibili_podcast.sync import is_safe_enclosure_url
        assert is_safe_enclosure_url("http://host/media.mp3?token=abc") is True
        assert is_safe_enclosure_url("http://host/media.mp3?token=__MEDIA_PLACEHOLDER__") is True
        assert is_safe_enclosure_url("http://host/media.mp3") is False
        assert is_safe_enclosure_url("") is False

    def test_safe_episodes_survive_keep_last(self, tmp_path):
        """With keep_last=1 and one safe + one unsafe old RSS item,
        only the safe item should appear in generated RSS."""
        import json
        import sqlite3
        from pathlib import Path
        from bilibili_podcast import db, sync
        from bilibili_podcast.utils.series_config import SeriesConfig
        from bilibili_podcast.sync import SyncPaths, generate_rss, existing_rss_items

        # Build a minimal DB series
        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO series(series,title,author) VALUES('guard','Guard','T')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('guard','space',1)")
        conn.execute("INSERT INTO sync_policy(series) VALUES('guard')")
        conn.commit()
        conn.close()

        cfg = SeriesConfig(
            series="guard", enabled=True, title="Guard", description="", author="T",
            cover_art="", category="", subcategories=[], explicit=False, lang="zh-CN",
            source={"uid": 1}, sync={}, filters={}, paid_preview={}, keep_last=1,
        )

        paths = SyncPaths(
            media_root=tmp_path / "media", json_root=tmp_path / "json",
            rss_root=tmp_path / "rss", media_base_url="http://test",
        )

        # Build an old RSS with one safe and one unsafe enclosure
        rss_dir = paths.rss_root
        rss_dir.mkdir(parents=True)
        rss_file = rss_dir / "guard.xml"
        rss_file.write_text(f"""<?xml version="1.0"?>
<rss version="2.0">
<channel><title>Guard</title>
<item>
  <title>Unsafe</title>
  <guid isPermaLink="false">BVunsafe00001</guid>
  <enclosure url="http://test/media/guard/BVunsafe00001_64K.mp3" length="100" type="audio/mpeg"/>
</item>
<item>
  <title>Safe</title>
  <guid isPermaLink="false">BVsafe0000001</guid>
  <enclosure url="http://test/media/guard/BVsafe0000001_64K.mp3?token=__MEDIA_PLACEHOLDER__" length="200" type="audio/mpeg"/>
</item>
</channel></rss>""")

        # Parse the old RSS
        old_items = existing_rss_items(rss_file)
        assert len(old_items) == 2

        # Now call generate_rss with NO media files
        result_path = generate_rss(cfg, paths, {"name": "Guard"}, old_items, "__MEDIA_PLACEHOLDER__", dry_run=False)

        # Read generated RSS
        content = result_path.read_text()
        # The unsafe URL has no token — should be filtered out
        assert "BVunsafe00001" not in content, "unsafe item should not appear"
        # The safe URL has __MEDIA_PLACEHOLDER__ — should be kept
        assert "BVsafe0000001" in content, "safe item should appear"


# ── 6.3: scheduler status enabled/active + schedule_count tests ─────


class TestSchedulerStatus:
    def test_enabled_active_separation(self, tmp_path):
        """status must return enabled=True, active=False when timer is
        enabled but not active."""
        from unittest.mock import patch
        from bilibili_podcast.services.scheduler_service import SchedulerService
        from bilibili_podcast import db

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('st','St','T')")
            conn.execute("INSERT INTO series_source(series,type,uid) VALUES('st','space',1)")
            conn.execute("INSERT INTO sync_policy(series) VALUES('st')")

        import bilibili_podcast.services.systemd_scheduler as sysd_mod
        with patch.object(sysd_mod, "_systemctl") as mock:
            def fake_ctl(*args):
                from subprocess import CompletedProcess
                if args[0] == "is-enabled":
                    return CompletedProcess(args, 0, "enabled\n", "")
                if args[0] == "is-active":
                    return CompletedProcess(args, 1, "inactive\n", "")
                if args[0] == "show":
                    return CompletedProcess(args, 0, "ActiveState=inactive\n", "")
                return CompletedProcess(args, 0, "", "")
            mock.side_effect = fake_ctl

            svc = SchedulerService(str(db_path))
            result = svc.status(backend="systemd", series="st")
            assert len(result) == 1
            info = result[0]
            assert info["enabled"] is True, f"enabled should be True, got {info['enabled']}"
            assert info["active"] is False, f"active should be False, got {info['active']}"

    def test_systemd_schedule_count_excludes_disabled(self, tmp_path):
        """systemd status schedule_count must only count enabled=1."""
        from unittest.mock import patch
        from bilibili_podcast.services.scheduler_service import SchedulerService
        from bilibili_podcast import db
        import sqlite3

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('sc','Sc','T')")
            conn.execute("INSERT INTO series_source(series,type,uid) VALUES('sc','space',1)")
            conn.execute("INSERT INTO sync_policy(series) VALUES('sc')")
        # Insert disabled schedule only
        conn2 = sqlite3.connect(str(db_path))
        conn2.execute("INSERT INTO cron_schedule(series,enabled,schedule,position) VALUES('sc',0,'0 6 * * *',0)")
        conn2.commit()
        conn2.close()

        import bilibili_podcast.services.systemd_scheduler as sysd_mod
        with patch.object(sysd_mod, "_systemctl") as mock:
            mock.return_value = __import__("subprocess").CompletedProcess([], 0, "enabled\n", "")
            svc = SchedulerService(str(db_path))
            result = svc.status(backend="systemd", series="sc")
            assert len(result) == 1
            assert result[0]["schedule_count"] == 0, f"expected 0, got {result[0]['schedule_count']}"


class TestSeriesScheduleRemoval:
    def test_exclude_series_from_cron_does_not_match_prefix(self, tmp_path):
        from subprocess import CompletedProcess
        from unittest.mock import patch

        from bilibili_podcast.services.scheduler_service import SchedulerService

        existing = (
            "# manual note: # BEGIN BILIPOD AUTO - demo (not a block)\n"
            "# BEGIN BILIPOD AUTO - demo (Demo (Archive))\n"
            "0 1 * * * /demo\n"
            "# END BILIPOD AUTO\n"
            "# BEGIN BILIPOD AUTO - demo-test (Demo Test)\n"
            "0 2 * * * /demo-test\n"
            "# END BILIPOD AUTO\n"
        )
        written: list[str] = []

        def fake_run(cmd, **kwargs):
            if "-l" in cmd:
                return CompletedProcess(cmd, 0, existing, "")
            written.append(kwargs["input"])
            return CompletedProcess(cmd, 0, "", "")

        svc = SchedulerService(str(tmp_path / "test.db"))
        with patch("getpass.getuser", return_value="bilipod"), \
                patch("subprocess.run", side_effect=fake_run):
            svc._exclude_series_from_cron("demo")

        assert len(written) == 1
        assert "# manual note:" in written[0]
        assert "BILIPOD AUTO - demo (Demo (Archive))\n" not in written[0]
        assert "BILIPOD AUTO - demo-test (Demo Test)\n" in written[0]

    def test_restore_cron_for_series_only_adds_target_block(self, tmp_path):
        from subprocess import CompletedProcess
        from unittest.mock import patch

        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerService

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            for series, title, schedule in (
                ("one", "One\nArchive", "0 1 * * *"),
                ("two", "Two", "0 2 * * *"),
            ):
                conn.execute(
                    "INSERT INTO series(series,title,author) VALUES(?,?,?)",
                    (series, title, "A"),
                )
                conn.execute(
                    "INSERT INTO cron_schedule(series,schedule) VALUES(?,?)",
                    (series, schedule),
                )

        auto = tmp_path / "auto"
        auto.mkdir()
        (auto / "run_one_sync.sh").write_text("#!/bin/sh\n")
        existing = (
            "# manual\n"
            "# BEGIN BILIPOD AUTO - two (Two)\n"
            "0 2 * * * /old/two\n"
            "# END BILIPOD AUTO\n"
        )
        written: list[str] = []

        def fake_run(cmd, **kwargs):
            if "-l" in cmd:
                return CompletedProcess(cmd, 0, existing, "")
            written.append(kwargs["input"])
            return CompletedProcess(cmd, 0, "", "")

        svc = SchedulerService(str(db_path), cron_script_dir=str(auto))
        with patch("getpass.getuser", return_value="bilipod"), \
                patch("subprocess.run", side_effect=fake_run):
            svc._restore_cron_for_series("one")

        assert len(written) == 1
        assert "# manual" in written[0]
        assert "BILIPOD AUTO - one (One Archive)" in written[0]
        assert "0 1 * * *" in written[0]
        assert "BILIPOD AUTO - two (Two)" in written[0]
        assert "/old/two" in written[0]

    def test_disable_systemd_does_not_restore_cron_when_timer_disable_fails(self, tmp_path):
        from unittest.mock import patch

        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        svc = SchedulerService(str(db_path))
        failed = SchedulerCommandResult(
            backend="systemd", action="disable-timer", returncode=1,
            stdout="", stderr="permission denied",
        )
        with patch.object(sysd_mod, "disable_timer", return_value=failed), \
                patch.object(svc, "_restore_cron_for_series") as restore:
            result = svc.disable_systemd("demo")

        assert result.returncode == 1
        assert "permission denied" in result.stderr
        restore.assert_not_called()

    def test_disable_systemd_uses_remove_unit_for_delete_units(self, tmp_path):
        from unittest.mock import call, patch

        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
            db.set_scheduler_backend(conn, "demo", "systemd")
        svc = SchedulerService(str(db_path))
        ok = SchedulerCommandResult(
            backend="systemd", action="ok", returncode=0, stdout="", stderr="",
        )
        with patch.object(sysd_mod, "disable_timer", return_value=ok), \
                patch.object(sysd_mod, "remove_unit", return_value=ok) as remove_unit, \
                patch.object(sysd_mod, "daemon_reload", return_value=ok), \
                patch.object(svc, "_restore_cron_for_series"):
            result = svc.disable_systemd("demo", delete_units=True)

        assert result.returncode == 0
        assert remove_unit.call_args_list == [
            call("demo", "service", scheduled_retry=False),
            call("demo", "timer", scheduled_retry=False),
            call("demo", "service", scheduled_retry=True),
            call("demo", "timer", scheduled_retry=True),
        ]
        with db.transaction(str(db_path)) as conn:
            assert db.get_scheduler_backend(conn, "demo") == "cron"

    def test_disable_systemd_restore_failure_keeps_units_and_restores_timer(self, tmp_path):
        from unittest.mock import patch

        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
            db.set_scheduler_backend(conn, "demo", "systemd")
        svc = SchedulerService(str(db_path))
        ok = SchedulerCommandResult(
            backend="systemd", action="ok", returncode=0, stdout="", stderr="",
        )
        with patch.object(sysd_mod, "timer_is_enabled", return_value=True), \
                patch.object(sysd_mod, "timer_is_active", return_value=True), \
                patch.object(sysd_mod, "disable_timer", return_value=ok), \
                patch.object(sysd_mod, "enable_timer", return_value=ok) as enable_timer, \
                patch.object(sysd_mod, "restart_timer", return_value=ok) as restart_timer, \
                patch.object(sysd_mod, "remove_unit") as remove_unit, \
                patch.object(svc, "_restore_cron_for_series", side_effect=RuntimeError("write failed")):
            result = svc.disable_systemd("demo", delete_units=True)

        assert result.returncode == 1
        assert "restore cron" in result.stderr
        enable_timer.assert_called_once_with("demo")
        restart_timer.assert_called_once_with("demo")
        remove_unit.assert_not_called()
        with db.transaction(str(db_path)) as conn:
            assert db.get_scheduler_backend(conn, "demo") == "systemd"

    def test_remove_series_schedule_does_not_restore_cron(self, tmp_path):
        from unittest.mock import patch
        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        svc = SchedulerService(str(db_path))
        ok = SchedulerCommandResult(
            backend="systemd", action="ok", returncode=0, stdout="", stderr="",
        )
        with patch.object(sysd_mod, "disable_timer", return_value=ok), \
             patch.object(sysd_mod, "remove_unit", return_value=ok), \
             patch.object(sysd_mod, "daemon_reload", return_value=ok), \
             patch.object(svc, "_exclude_series_from_cron"), \
             patch.object(svc, "_restore_cron_for_series") as restore:
            result = svc.remove_series_schedule("demo")

        assert result.returncode == 0
        restore.assert_not_called()

    def test_remove_series_schedule_stops_when_timer_disable_fails(self, tmp_path):
        from unittest.mock import patch

        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        svc = SchedulerService(str(db_path))
        failed = SchedulerCommandResult(
            backend="systemd", action="disable-timer", returncode=1,
            stdout="", stderr="permission denied",
        )
        with patch.object(sysd_mod, "disable_timer", return_value=failed), \
                patch.object(sysd_mod, "remove_unit") as remove_unit, \
                patch.object(svc, "_exclude_series_from_cron") as remove_cron:
            result = svc.remove_series_schedule("demo")

        assert result.returncode == 1
        assert "permission denied" in result.stderr
        remove_cron.assert_not_called()
        remove_unit.assert_not_called()

    def test_remove_series_schedule_reports_unit_delete_failure(self, tmp_path):
        from unittest.mock import patch
        from bilibili_podcast import db
        from bilibili_podcast.services.scheduler_service import SchedulerCommandResult, SchedulerService
        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        db_path = tmp_path / "test.db"
        db.migrate(str(db_path))
        svc = SchedulerService(str(db_path))
        ok = SchedulerCommandResult(
            backend="systemd", action="ok", returncode=0, stdout="", stderr="",
        )
        failed = SchedulerCommandResult(
            backend="systemd", action="remove-unit", returncode=1,
            stdout="", stderr="permission denied",
        )
        with patch.object(sysd_mod, "disable_timer", return_value=ok), \
             patch.object(sysd_mod, "remove_unit", side_effect=[failed, ok, ok, ok]), \
             patch.object(sysd_mod, "daemon_reload", return_value=ok), \
             patch.object(svc, "_exclude_series_from_cron"):
            result = svc.remove_series_schedule("demo")

        assert result.returncode == 1
        assert "permission denied" in result.stderr

    def test_remove_unit_reports_sudo_timeout(self, tmp_path):
        import subprocess
        from unittest.mock import patch

        import bilibili_podcast.services.systemd_scheduler as sysd_mod

        unit = tmp_path / "bilipod-sync@demo.service"
        unit.write_text("x")
        with patch.object(sysd_mod, "SYSTEMD_DIR", tmp_path), \
             patch.object(type(unit), "unlink", side_effect=PermissionError("denied")), \
             patch.object(
                 sysd_mod.subprocess,
                 "run",
                 side_effect=subprocess.TimeoutExpired(["sudo", "rm"], 10),
             ):
            result = sysd_mod.remove_unit("demo", "service")

        assert result.returncode == 1
        assert result.error is not None
        assert "failed to remove" in result.error
