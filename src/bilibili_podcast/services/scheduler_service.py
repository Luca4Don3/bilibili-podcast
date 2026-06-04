from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

@dataclass
class SchedulerCommandResult:
    backend: str
    action: str
    returncode: int
    stdout: str
    stderr: str
    command: list[str] = field(default_factory=list)
    script_dir: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ScheduleEntry:
    id: Optional[int]
    series: str
    schedule: str
    enabled: bool
    position: int


def _find_crontab_script() -> Optional[str]:
    """Locate scripts/bilibili-podcast-crontab relative to this file or via PATH."""
    here = Path(__file__).resolve().parent.parent.parent
    candidate = here / "scripts" / "bilibili-podcast-crontab"
    if candidate.exists():
        return str(candidate)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / "bilibili-podcast-crontab"
        if candidate.exists():
            return str(candidate)
    return None


class SchedulerService:
    """Manage cron and systemd timer scheduling for bilibili-podcast series."""

    def __init__(
        self,
        db_path: str,
        *,
        python_executable: Optional[str] = None,
        crontab_script: Optional[str] = None,
        cron_script_dir: Optional[str] = None,
    ) -> None:
        self.db_path = db_path
        self._python = python_executable or sys.executable
        self._crontab = crontab_script or _find_crontab_script()
        self._cron_script_dir = cron_script_dir

    def _ensure_crontab(self) -> str:
        if not self._crontab:
            raise FileNotFoundError(
                "bilibili-podcast-crontab script not found; "
                "ensure scripts/bilibili-podcast-crontab is present"
            )
        return self._crontab

    # ── Backend check / dispatch ───────────────────────────────────────

    def _require_cron_backend(self, backend: str) -> None:
        if backend != "cron" and backend != "systemd":
            raise NotImplementedError(
                f"{backend} backend is not implemented yet; use --backend cron or --backend systemd"
            )

    # ── plan ───────────────────────────────────────────────────────────

    def plan(
        self,
        *,
        backend: str = "cron",
        cron_script_dir: Optional[str] = None,
        series: Optional[str] = None,
    ) -> SchedulerCommandResult:
        self._require_cron_backend(backend)
        if backend == "systemd":
            return self._plan_systemd(series)

        # ── cron backend ──
        cmd: list[str] = []
        tmp_dir: Optional[str] = None
        try:
            crontab = self._ensure_crontab()
            cmd = [self._python, crontab, "--config-db", self.db_path]

            if cron_script_dir:
                script_dir = cron_script_dir
            else:
                tmp_dir = tempfile.mkdtemp(prefix="bilibili-podcast-scheduler-plan-")
                script_dir = tmp_dir

            cmd.extend(["--script-dir", script_dir, "--print"])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return SchedulerCommandResult(
                backend=backend,
                action="plan",
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                command=cmd,
                script_dir=script_dir,
            )
        except subprocess.TimeoutExpired:
            return SchedulerCommandResult(
                backend=backend,
                action="plan",
                returncode=-1,
                stdout="",
                stderr="",
                command=cmd,
                error=f"执行超时（30 秒）",
            )
        except Exception as e:
            return SchedulerCommandResult(
                backend=backend,
                action="plan",
                returncode=-1,
                stdout="",
                stderr="",
                command=cmd,
                error=f"执行错误: {e}",
            )
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _plan_systemd(self, series: Optional[str]) -> SchedulerCommandResult:
        from . import systemd_scheduler as sysd

        if not series:
            return SchedulerCommandResult(
                backend="systemd", action="plan", returncode=-1,
                stdout="", stderr="",
                error="--series is required for systemd plan",
            )

        schedules = self.list_enabled_schedules(series)
        if not schedules:
            return SchedulerCommandResult(
                backend="systemd", action="plan", returncode=-1,
                stdout="", stderr="",
                error=f"no schedules found for series '{series}'",
            )

        # Convert schedules and build output
        lines: list[str] = []
        converted: list[str] = []
        unconverted: list[str] = []
        for s in schedules:
            oc = sysd.cron_to_oncalendar(s.schedule)
            if oc:
                converted.append((s.schedule, oc))
            else:
                unconverted.append(s.schedule)

        if unconverted:
            lines.append("# ❌ 以下 cron 表达式不支持转换到 systemd OnCalendar:")
            for expr in unconverted:
                lines.append(f"#    {expr}")
            lines.append("# 修复后重试，或继续使用 cron backend。")

        for expr, oc in converted:
            lines.append(f"# cron: {expr}  →  OnCalendar={oc}")
            svc = sysd.generate_service(series)
            # Collect ALL OnCalendar lines
            all_oc = [oc for _, oc in converted]
            timer = sysd.generate_timer(series, all_oc)
            lines.append("")
            lines.append(f"# --- bilibili-podcast-sync@{series}.service ---")
            lines.extend(svc.splitlines())
            lines.append("")
            lines.append(f"# --- bilibili-podcast-sync@{series}.timer ---")
            lines.extend(timer.splitlines())
            lines.append("")
            break  # one service + one timer with all OnCalendar lines

        # Check if unit files already exist
        sysd_dir = sysd.SYSTEMD_DIR
        for suf in ("service", "timer"):
            u = sysd.unit_name(series, suf)
            fp = sysd_dir / u
            if fp.exists():
                lines.append(f"# ⚠ {u} 已存在 ({fp})")
            else:
                lines.append(f"# {u} 尚不存在")

        # Check if series has cron entries
        if schedules:
            lines.append("")
            lines.append("# ⚠ 该 series 当前有 cron 调度。apply systemd 会自动禁用其 cron 条目。")
            lines.append("# apply 后会保留其他 series 的 cron 和手工条目。")

        output = "\n".join(lines) + "\n"
        returncode = -1 if unconverted else 0
        return SchedulerCommandResult(
            backend="systemd", action="plan",
            returncode=returncode, stdout=output, stderr="",
        )

    # ── apply ──────────────────────────────────────────────────────────

    def apply(
        self,
        *,
        backend: str = "cron",
        cron_script_dir: Optional[str] = None,
        series: Optional[str] = None,
    ) -> SchedulerCommandResult:
        self._require_cron_backend(backend)
        if backend == "systemd":
            return self._apply_systemd(series)

        # ── cron backend ──

        cmd: list[str] = []
        try:
            crontab = self._ensure_crontab()
            cmd = [self._python, crontab, "--config-db", self.db_path]
            if cron_script_dir:
                cmd.extend(["--script-dir", cron_script_dir])
            cmd.append("--apply")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return SchedulerCommandResult(
                backend=backend,
                action="apply",
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                command=cmd,
                script_dir=cron_script_dir,
            )
        except subprocess.TimeoutExpired:
            return SchedulerCommandResult(
                backend=backend,
                action="apply",
                returncode=-1,
                stdout="",
                stderr="",
                command=cmd,
                error=f"执行超时（30 秒）",
            )
        except Exception as e:
            return SchedulerCommandResult(
                backend=backend,
                action="apply",
                returncode=-1,
                stdout="",
                stderr="",
                command=cmd,
                error=f"执行错误: {e}",
            )

    def _apply_systemd(self, series: Optional[str]) -> SchedulerCommandResult:
        from . import systemd_scheduler as sysd

        if not series:
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="",
                error="--series is required for systemd apply",
            )

        schedules = self.list_enabled_schedules(series)
        if not schedules:
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="",
                error=f"no schedules found for series '{series}'",
            )

        # Build list of all OnCalendar lines
        oncalendars: list[str] = []
        for s in schedules:
            oc = sysd.cron_to_oncalendar(s.schedule)
            if oc is None:
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="",
                    error=f"unsupported cron expression: {s.schedule}",
                )
            oncalendars.append(oc)

        # 1. Write systemd units
        original_units: dict[str, str | None] = {}
        for suffix in ("service", "timer"):
            path = sysd.SYSTEMD_DIR / sysd.unit_name(series, suffix)
            try:
                original_units[suffix] = path.read_text(encoding="utf-8") if path.exists() else None
            except OSError as exc:
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="",
                    error=f"failed to read existing unit {path}: {exc}",
                )

        modified_units: list[str] = []
        was_enabled = sysd.timer_is_enabled(series)
        was_active = sysd.timer_is_active(series)

        def rollback_units() -> list[str]:
            errors: list[str] = []
            for suffix in modified_units:
                original = original_units[suffix]
                if original is None:
                    result = sysd.remove_unit(series, suffix)
                else:
                    result = sysd.write_unit(series, suffix, original)
                if result.returncode != 0:
                    errors.append(f"restore {suffix}: {result.stderr or result.error or 'failed'}")
            if modified_units:
                dr = sysd.daemon_reload()
                if dr.returncode != 0:
                    errors.append(f"daemon-reload: {dr.stderr.strip()}")
            return errors

        def rollback_timer_and_units() -> list[str]:
            errors: list[str] = []
            disabled = sysd.disable_timer(series)
            if disabled.returncode != 0:
                errors.append(f"disable timer: {disabled.stderr.strip()}")
            errors.extend(rollback_units())
            if was_enabled:
                enabled = sysd.enable_timer(series)
                if enabled.returncode != 0:
                    errors.append(f"restore timer enable: {enabled.stderr.strip()}")
            if was_active:
                started = sysd.start_timer(series)
                if started.returncode != 0:
                    errors.append(f"restore timer active state: {started.stderr.strip()}")
            return errors

        def with_rollback_error(message: str, errors: list[str]) -> str:
            if not errors:
                return message
            return f"{message}; rollback errors: {'; '.join(errors)}"

        for suf in ("service", "timer"):
            if suf == "service":
                content = sysd.generate_service(series)
            else:
                content = sysd.generate_timer(series, oncalendars)
            written = sysd.write_unit(series, suf, content)
            if written.returncode != 0:
                rollback_errors = rollback_units()
                if rollback_errors:
                    written.error = with_rollback_error(written.error or "unit write failed", rollback_errors)
                return written
            modified_units.append(suf)

        # 2. daemon-reload
        dr = sysd.daemon_reload()
        if dr.returncode != 0:
            rollback_errors = rollback_units()
            if rollback_errors:
                dr.error = with_rollback_error(dr.error or "daemon-reload failed", rollback_errors)
            return dr

        # 3. enable timer (symlink for auto-start on boot)
        et = sysd.enable_timer(series)
        if et.returncode != 0:
            rollback_errors = rollback_timer_and_units()
            if rollback_errors:
                et.error = with_rollback_error(et.error or "enable timer failed", rollback_errors)
            return et

        # 3b. Restart timer so updated unit contents take effect. This arms the
        # timer but does not run the service.
        rt = sysd.restart_timer(series)
        if rt.returncode != 0:
            rollback_errors = rollback_timer_and_units()
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="",
                error=with_rollback_error("restart timer failed — rolled back", rollback_errors),
            )

        # 3c. Verify timer is enabled
        if not sysd.timer_is_enabled(series):
            rollback_errors = rollback_timer_and_units()
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="",
                error=with_rollback_error("timer enable verification failed — rolled back", rollback_errors),
            )

        # 3d. Verify timer is active (armed in current boot cycle)
        if not sysd.timer_is_active(series):
            rollback_errors = rollback_timer_and_units()
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="",
                error=with_rollback_error("timer active verification failed — rolled back", rollback_errors),
            )

        # 4. Remove cron for this series (only after timer is confirmed active)
        try:
            self._set_scheduler_backend(series, "systemd")
            self._exclude_series_from_cron(series)
        except Exception as e:
            rollback_errors = rollback_timer_and_units()
            try:
                self._set_scheduler_backend(series, "cron")
            except Exception as backend_exc:
                rollback_errors.append(f"restore scheduler backend: {backend_exc}")
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="",
                error=with_rollback_error(
                    f"failed to disable cron for '{series}': {e} — timer has been rolled back",
                    rollback_errors,
                ),
            )

        return SchedulerCommandResult(
            backend="systemd", action="apply",
            returncode=0,
            stdout=f"systemd timer enabled for {series}, cron disabled",
            stderr="",
        )

    def _exclude_series_from_cron(self, series: str) -> None:
        """Remove *series* from the current crontab while preserving
        all other entries (manual + auto blocks for other series).

        Uses ``crontab -l`` (with proper ``-u`` handling) to read the
        existing crontab, removes only the target series auto block
        via regex, and writes the result back via ``crontab -``.
        """
        existing = self._read_crontab()

        # Remove only the target series auto block
        import re
        cleaned = re.sub(
            rf"^# BEGIN BILIBILI_PODCAST AUTO - {re.escape(series)}(?:\s+\([^\r\n]*\))?\r?\n.*?"
            rf"^# END BILIBILI_PODCAST AUTO\r?\n?",
            "", existing, flags=re.DOTALL | re.MULTILINE,
        ).strip()

        if not cleaned:
            # All entries removed — write empty crontab to prevent dual scheduling
            self._write_crontab("\n")
            return

        # Write back via crontab
        self._write_crontab(cleaned + "\n")

    @staticmethod
    def _crontab_command(*args: str) -> list[str]:
        import getpass
        current_user = getpass.getuser()
        cron_user = "bilibili-podcast"
        if current_user == cron_user:
            return ["crontab", *args]
        return ["crontab", "-u", cron_user, *args]

    def _read_crontab(self) -> str:
        read_result = subprocess.run(
            self._crontab_command("-l"), capture_output=True, text=True, timeout=10,
        )
        if read_result.returncode == 0:
            return read_result.stdout
        if "no crontab" in read_result.stderr.lower():
            return ""
        raise RuntimeError(f"failed to read crontab: {read_result.stderr.strip()}")

    def _write_crontab(self, content: str) -> None:
        subprocess.run(
            self._crontab_command("-"), input=content,
            capture_output=True, text=True, timeout=10, check=True,
        )

    def _set_scheduler_backend(self, series: str, backend: str) -> None:
        from .. import db
        with db.transaction(self.db_path) as conn:
            db.set_scheduler_backend(conn, series, backend)

    @staticmethod
    def _cron_marker_title(title: Any, series: str) -> str:
        normalized = " ".join(str(title or "").splitlines()).strip()
        return normalized or series

    def _restore_cron_for_series(self, series: str) -> None:
        """Restore only *series* to cron after its systemd timer is disabled."""
        schedules = self.list_enabled_schedules(series)
        if not schedules:
            raise RuntimeError(f"no enabled schedules found for series '{series}'")
        if not self._cron_script_dir:
            raise RuntimeError("cron script dir is required to restore cron")

        wrapper = Path(self._cron_script_dir) / f"run_{series}_sync.sh"
        if not wrapper.exists():
            raise RuntimeError(f"cron wrapper not found: {wrapper}")

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT title FROM series WHERE series=?", (series,)).fetchone()
        if row is None:
            raise RuntimeError(f"series not found: {series}")

        title = self._cron_marker_title(row[0], series)
        lines = [f"# BEGIN BILIBILI_PODCAST AUTO - {series} ({title})"]
        lines.extend(f"{entry.schedule} {wrapper}" for entry in schedules)
        lines.append("# END BILIBILI_PODCAST AUTO")
        block = "\n".join(lines)

        existing = self._read_crontab()
        import re
        cleaned = re.sub(
            rf"^# BEGIN BILIBILI_PODCAST AUTO - {re.escape(series)}(?:\s+\([^\r\n]*\))?\r?\n.*?"
            rf"^# END BILIBILI_PODCAST AUTO\r?\n?",
            "", existing, flags=re.DOTALL | re.MULTILINE,
        ).strip()
        content = f"{cleaned}\n\n{block}\n" if cleaned else f"{block}\n"
        self._write_crontab(content)

    # ── list_schedules / replace_schedules (DB only) ──────────────────

    def list_schedules(self, series: str) -> list[ScheduleEntry]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, series, schedule, enabled, position "
                "FROM cron_schedule WHERE series=? ORDER BY position",
                (series,),
            ).fetchall()
        return [
            ScheduleEntry(
                id=r["id"],
                series=r["series"],
                schedule=r["schedule"],
                enabled=bool(r["enabled"]),
                position=r["position"],
            )
            for r in rows
        ]

    def list_enabled_schedules(self, series: str) -> list[ScheduleEntry]:
        """Return only enabled schedules (enabled=1) for installation
        into cron/systemd."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, series, schedule, enabled, position "
                "FROM cron_schedule WHERE series=? AND enabled=1 ORDER BY position",
                (series,),
            ).fetchall()
        return [
            ScheduleEntry(
                id=r["id"],
                series=r["series"],
                schedule=r["schedule"],
                enabled=bool(r["enabled"]),
                position=r["position"],
            )
            for r in rows
        ]

    def replace_schedules(self, series: str, schedules: list[str]) -> int:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cron_schedule WHERE series=?", (series,))
            for pos, sched in enumerate(schedules):
                conn.execute(
                    "INSERT INTO cron_schedule (series, enabled, schedule, position) "
                    "VALUES (?, 1, ?, ?)",
                    (series, sched, pos),
                )
            conn.commit()
        return len(schedules)

    # ── status ─────────────────────────────────────────────────────────

    def status(self, *, backend: str = "cron",
               series: Optional[str] = None) -> list[dict[str, Any]]:
        self._require_cron_backend(backend)
        if backend == "systemd":
            return self._status_systemd(series)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if series:
                rows = conn.execute(
                    "SELECT s.series, s.enabled, s.title, "
                    "COUNT(c.id) AS schedule_count "
                    "FROM series s "
                    "LEFT JOIN cron_schedule c ON c.series = s.series AND c.enabled=1 "
                    "WHERE s.series=? GROUP BY s.series ORDER BY s.series",
                    (series,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT s.series, s.enabled, s.title, "
                    "COUNT(c.id) AS schedule_count "
                    "FROM series s "
                    "LEFT JOIN cron_schedule c ON c.series = s.series AND c.enabled=1 "
                    "GROUP BY s.series ORDER BY s.series",
                ).fetchall()

        return [dict(r) for r in rows]

    def _status_systemd(self, series: Optional[str]) -> list[dict[str, Any]]:
        from . import systemd_scheduler as sysd

        result: list[dict[str, Any]] = []
        series_list = [series] if series else self._list_all_series()

        for s in series_list:
            info: dict[str, Any] = {"series": s}
            u = sysd.unit_name(s, "timer")
            info["timer_unit"] = u
            info["enabled"] = sysd.timer_is_enabled(s)
            info["active"] = sysd.timer_is_active(s)
            info["schedule_count"] = len(self.list_enabled_schedules(s))

            # Query systemctl show for detailed status
            show = sysd.systemctl_show(u)
            info["last_trigger"] = show.get("LastTriggerUSec", "n/a")
            info["next_trigger"] = show.get("NextUSec", "n/a")
            info["active_state"] = show.get("ActiveState", "unknown")
            info["sub_state"] = show.get("SubState", "unknown")
            info["result"] = show.get("Result", "unknown")

            result.append(info)

        return result

    def _list_all_series(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT series FROM series").fetchall()
        return [r[0] for r in rows]

    def disable_systemd(self, series: str, *, delete_units: bool = False) -> SchedulerCommandResult:
        """Disable systemd timer for *series* and restore cron backend."""
        from . import systemd_scheduler as sysd

        was_enabled = sysd.timer_is_enabled(series)
        was_active = sysd.timer_is_active(series)
        dt = sysd.disable_timer(series)
        missing_markers = ("not found", "not loaded", "does not exist")
        if dt.returncode != 0 and not any(marker in dt.stderr.lower() for marker in missing_markers):
            return SchedulerCommandResult(
                backend="systemd", action="disable", returncode=1,
                stdout="", stderr=f"disable timer: {dt.stderr.strip()}",
            )

        try:
            self._set_scheduler_backend(series, "cron")
            self._restore_cron_for_series(series)
        except Exception as exc:
            rollback_errors: list[str] = []
            try:
                self._set_scheduler_backend(series, "systemd")
            except Exception as backend_exc:
                rollback_errors.append(f"restore scheduler backend: {backend_exc}")
            if was_enabled:
                enabled = sysd.enable_timer(series)
                if enabled.returncode != 0:
                    rollback_errors.append(f"restore timer enable: {enabled.stderr.strip()}")
            if was_active:
                restarted = sysd.restart_timer(series)
                if restarted.returncode != 0:
                    rollback_errors.append(f"restore timer active state: {restarted.stderr.strip()}")
            error = f"restore cron: {exc}"
            if rollback_errors:
                error += f"; rollback errors: {'; '.join(rollback_errors)}"
            return SchedulerCommandResult(
                backend="systemd", action="disable", returncode=1,
                stdout="", stderr=error,
            )

        errors: list[str] = []
        if delete_units:
            for suffix in ("service", "timer"):
                result = sysd.remove_unit(series, suffix)
                if result.returncode != 0:
                    errors.append(f"remove {suffix}: {result.stderr or result.error or 'failed'}")
            dr = sysd.daemon_reload()
            if dr.returncode != 0:
                errors.append(f"daemon-reload: {dr.stderr.strip()}")

        if errors:
            return SchedulerCommandResult(
                backend="systemd", action="disable", returncode=1,
                stdout="", stderr="; ".join(errors),
            )
        return SchedulerCommandResult(
            backend="systemd", action="disable", returncode=0,
            stdout=f"systemd timer disabled for {series}, cron restored",
            stderr="",
        )

    def remove_series_schedule(self, series: str, *, delete_units: bool = True) -> SchedulerCommandResult:
        """Remove a series from cron/systemd without restoring another backend."""
        from . import systemd_scheduler as sysd

        errors: list[str] = []
        dt = sysd.disable_timer(series)
        missing_markers = ("not found", "not loaded", "does not exist", "systemctl not found")
        if dt.returncode != 0 and not any(marker in dt.stderr.lower() for marker in missing_markers):
            return SchedulerCommandResult(
                backend="systemd", action="remove-series", returncode=1,
                stdout="", stderr=f"disable timer: {dt.stderr.strip()}",
            )

        try:
            self._exclude_series_from_cron(series)
        except Exception as e:
            errors.append(f"remove cron: {e}")

        if delete_units:
            for suffix in ("service", "timer"):
                result = sysd.remove_unit(series, suffix)
                if result.returncode != 0:
                    errors.append(f"remove {suffix}: {result.stderr.strip()}")

        if "systemctl not found" not in dt.stderr.lower():
            dr = sysd.daemon_reload()
            if dr.returncode != 0:
                errors.append(f"daemon-reload: {dr.stderr.strip()}")

        if errors:
            return SchedulerCommandResult(
                backend="systemd", action="remove-series", returncode=1,
                stdout="", stderr="; ".join(errors),
            )
        return SchedulerCommandResult(
            backend="systemd", action="remove-series", returncode=0,
            stdout=f"schedule removed for {series}", stderr="",
        )
