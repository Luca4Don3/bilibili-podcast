from __future__ import annotations

import math
import os
import re
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
    kind: str = "primary"


def _period_seconds(value: object) -> int:
    text = str(value or "12h").strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text)
    if not match:
        raise ValueError(f"invalid update_period: {value}")
    amount = float(match.group(1))
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError(f"invalid update_period: {value}")
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    seconds = int(amount * multiplier)
    if seconds < 1:
        raise ValueError(f"invalid update_period: {value}")
    return seconds


def _cron_values(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        base, separator, step_text = part.partition("/")
        try:
            step = int(step_text) if separator else 1
        except ValueError as exc:
            raise ValueError(f"invalid cron field: {field}") from exc
        if step <= 0:
            raise ValueError(f"invalid cron field: {field}")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"invalid cron field: {field}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise ValueError(f"invalid cron field: {field}") from exc
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"invalid cron field: {field}")
        values.update(range(start, end + 1, step))
    return values


def _weekly_occurrences(schedule: str) -> set[int]:
    fields = schedule.split()
    if len(fields) != 5 or fields[2:4] != ["*", "*"]:
        raise ValueError(f"unsupported schedule expression: {schedule}")
    minutes = _cron_values(fields[0], 0, 59)
    hours = _cron_values(fields[1], 0, 23)
    days = _cron_values(fields[4], 0, 7)
    days = {0 if day == 7 else day for day in days}
    return {
        day * 86400 + hour * 3600 + minute * 60
        for day in days for hour in hours for minute in minutes
    }


def validate_schedules(entries: list[ScheduleEntry], update_period: object) -> None:
    period = _period_seconds(update_period)
    seen: dict[int, ScheduleEntry] = {}
    primary_points: set[int] = set()
    if any(entry.kind == "retry" for entry in entries) and not any(
        entry.kind == "primary" for entry in entries
    ):
        raise ValueError("at least one primary schedule is required when retry schedules are configured")
    for entry in entries:
        for point in _weekly_occurrences(entry.schedule):
            if point in seen:
                other = seen[point]
                raise ValueError(
                    f"duplicate schedule: {other.schedule} ({other.kind}) and "
                    f"{entry.schedule} ({entry.kind})"
                )
            seen[point] = entry
            if entry.kind == "primary":
                primary_points.add(point)
    if len(primary_points) < 2:
        return
    ordered = sorted(primary_points)
    gaps = [right - left for left, right in zip(ordered, ordered[1:])]
    gaps.append(7 * 86400 + ordered[0] - ordered[-1])
    shortest = min(gaps)
    if shortest < period:
        raise ValueError(
            f"primary schedules conflict: shortest interval {shortest}s "
            f"is less than update_period {period}s"
        )


def replace_schedules_in_connection(
    conn: sqlite3.Connection,
    series: str,
    schedules: list[str],
    retry_schedules: list[str] | None = None,
) -> int:
    """Validate and replace primary/retry schedules in the caller's transaction."""
    entries = [(schedule, "primary") for schedule in schedules]
    entries.extend((schedule, "retry") for schedule in (retry_schedules or []))
    period_row = conn.execute(
        "SELECT update_period FROM sync_policy WHERE series=?", (series,),
    ).fetchone()
    update_period = period_row[0] if period_row else "12h"
    validate_schedules([
        ScheduleEntry(None, series, schedule, True, pos, kind)
        for pos, (schedule, kind) in enumerate(entries)
    ], update_period)
    conn.execute("DELETE FROM cron_schedule WHERE series=?", (series,))
    for pos, (schedule, kind) in enumerate(entries):
        conn.execute(
            "INSERT INTO cron_schedule (series, enabled, schedule, position, kind) "
            "VALUES (?, 1, ?, ?, ?)",
            (series, schedule, pos, kind),
        )
    return len(entries)


def _find_crontab_script() -> Optional[str]:
    """Locate scripts/bilipod-crontab relative to this file or via PATH."""
    here = Path(__file__).resolve().parent.parent.parent
    candidate = here / "scripts" / "bilipod-crontab"
    if candidate.exists():
        return str(candidate)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / "bilipod-crontab"
        if candidate.exists():
            return str(candidate)
    return None


class SchedulerService:
    """Manage cron and systemd timer scheduling for Bilipod series."""

    def __init__(
        self,
        db_path: str,
        *,
        python_executable: Optional[str] = None,
        crontab_script: Optional[str] = None,
        cron_script_dir: Optional[str] = None,
        command_timeout_seconds: int = 30,
    ) -> None:
        self.db_path = db_path
        self._python = python_executable or sys.executable
        self._crontab = crontab_script or _find_crontab_script()
        self._cron_script_dir = cron_script_dir
        self._command_timeout_seconds = command_timeout_seconds

    def _ensure_crontab(self) -> str:
        if not self._crontab:
            raise FileNotFoundError(
                "bilipod-crontab script not found; "
                "ensure scripts/bilipod-crontab is present"
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
        retry_series = self._cron_retry_series()
        if retry_series:
            return SchedulerCommandResult(
                backend="cron", action="plan", returncode=-1,
                stdout="", stderr="",
                error="cron backend does not support retry schedules: " + ", ".join(retry_series),
            )

        # ── cron backend ──
        cmd: list[str] = []
        tmp_dir: Optional[str] = None
        try:
            crontab = self._ensure_crontab()
            cmd = [self._python, crontab, "--config-db", self.db_path]

            if cron_script_dir:
                script_dir = cron_script_dir
            else:
                tmp_dir = tempfile.mkdtemp(prefix="bilipod-scheduler-plan-")
                script_dir = tmp_dir

            cmd.extend(["--script-dir", script_dir, "--print"])
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self._command_timeout_seconds,
            )
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
                error=f"执行超时（{self._command_timeout_seconds} 秒）",
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
        converted: list[tuple[ScheduleEntry, str, str]] = []
        unconverted: list[str] = []
        for s in schedules:
            oc = sysd.cron_to_oncalendar(s.schedule)
            if oc:
                converted.append((s, s.schedule, oc))
            else:
                unconverted.append(s.schedule)

        if unconverted:
            lines.append("# ❌ 以下 cron 表达式不支持转换到 systemd OnCalendar:")
            for expr in unconverted:
                lines.append(f"#    {expr}")
            lines.append("# 修复后重试，或继续使用 cron backend。")
        else:
            try:
                validate_schedules(schedules, self._update_period(series))
            except ValueError as exc:
                return SchedulerCommandResult(
                    backend="systemd", action="plan", returncode=-1,
                    stdout="", stderr="", error=str(exc),
                )

        primary_converted = [(expr, oc) for entry, expr, oc in converted if entry.kind == "primary"]
        retry_converted = [(expr, oc) for entry, expr, oc in converted if entry.kind == "retry"]
        for expr, oc in primary_converted:
            lines.append(f"# cron: {expr}  →  OnCalendar={oc}")
            svc = sysd.generate_service(series)
            # Collect ALL OnCalendar lines
            all_oc = [value for _, value in primary_converted]
            timer = sysd.generate_timer(series, all_oc)
            lines.append("")
            lines.append(f"# --- {sysd.unit_name(series, 'service')} ---")
            lines.extend(svc.splitlines())
            lines.append("")
            lines.append(f"# --- {sysd.unit_name(series, 'timer')} ---")
            lines.extend(timer.splitlines())
            lines.append("")
            break  # one service + one timer with all OnCalendar lines

        if retry_converted:
            retry_oc = [value for _, value in retry_converted]
            lines.append("")
            lines.append(f"# --- {sysd.unit_name(series, 'service', scheduled_retry=True)} ---")
            lines.extend(sysd.generate_service(series, scheduled_retry=True).splitlines())
            lines.append("")
            lines.append(f"# --- {sysd.unit_name(series, 'timer', scheduled_retry=True)} ---")
            lines.extend(sysd.generate_timer(series, retry_oc).splitlines())

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
        retry_series = self._cron_retry_series()
        if retry_series:
            return SchedulerCommandResult(
                backend="cron", action="apply", returncode=-1,
                stdout="", stderr="",
                error="cron backend does not support retry schedules: " + ", ".join(retry_series),
            )

        # ── cron backend ──

        cmd: list[str] = []
        try:
            crontab = self._ensure_crontab()
            cmd = [self._python, crontab, "--config-db", self.db_path]
            if cron_script_dir:
                cmd.extend(["--script-dir", cron_script_dir])
            cmd.append("--apply")
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self._command_timeout_seconds,
            )
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
                error=f"执行超时（{self._command_timeout_seconds} 秒）",
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
        try:
            validate_schedules(schedules, self._update_period(series))
        except ValueError as exc:
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="", error=str(exc),
            )

        # Build list of all OnCalendar lines
        oncalendars: list[str] = []
        retry_oncalendars: list[str] = []
        for s in schedules:
            oc = sysd.cron_to_oncalendar(s.schedule)
            if oc is None:
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="",
                    error=f"unsupported cron expression: {s.schedule}",
                )
            if s.kind == "retry":
                retry_oncalendars.append(oc)
            else:
                oncalendars.append(oc)
        if not oncalendars:
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="", error="at least one primary schedule is required",
            )

        unit_keys = [
            (scheduled_retry, suffix)
            for scheduled_retry in (False, True)
            for suffix in ("service", "timer")
        ]
        original_units: dict[tuple[bool, str], str | None] = {}
        for scheduled_retry, suffix in unit_keys:
            path = sysd.SYSTEMD_DIR / sysd.unit_name(
                series, suffix, scheduled_retry=scheduled_retry,
            )
            try:
                original_units[(scheduled_retry, suffix)] = (
                    path.read_text(encoding="utf-8") if path.exists() else None
                )
            except OSError as exc:
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="", error=f"failed to read existing unit {path}: {exc}",
                )

        original_timer_state = {
            scheduled_retry: (
                sysd.timer_is_enabled(series, scheduled_retry=scheduled_retry),
                sysd.timer_is_active(series, scheduled_retry=scheduled_retry),
            )
            for scheduled_retry in (False, True)
        }
        original_backend = self._scheduler_backend(series)
        modified_units: list[tuple[bool, str]] = []

        def with_rollback_error(message: str, errors: list[str]) -> str:
            return message if not errors else f"{message}; rollback errors: {'; '.join(errors)}"

        def rollback_all() -> list[str]:
            errors: list[str] = []
            for scheduled_retry in (False, True):
                result = sysd.disable_timer(series, scheduled_retry=scheduled_retry)
                if result.returncode != 0:
                    errors.append(f"disable {'retry ' if scheduled_retry else ''}timer: {result.stderr.strip()}")
            for scheduled_retry, suffix in reversed(modified_units):
                original = original_units[(scheduled_retry, suffix)]
                if original is None:
                    result = sysd.remove_unit(
                        series, suffix, scheduled_retry=scheduled_retry,
                    )
                else:
                    result = sysd.write_unit(
                        series, suffix, original, scheduled_retry=scheduled_retry,
                    )
                if result.returncode != 0:
                    errors.append(f"restore {'retry ' if scheduled_retry else ''}{suffix}: {result.stderr or result.error or 'failed'}")
            if modified_units:
                result = sysd.daemon_reload()
                if result.returncode != 0:
                    errors.append(f"daemon-reload: {result.stderr.strip()}")
            for scheduled_retry, (was_enabled, was_active) in original_timer_state.items():
                if was_enabled:
                    result = sysd.enable_timer(series, scheduled_retry=scheduled_retry)
                    if result.returncode != 0:
                        errors.append(f"restore {'retry ' if scheduled_retry else ''}timer enable: {result.stderr.strip()}")
                if was_active:
                    result = sysd.start_timer(series, scheduled_retry=scheduled_retry)
                    if result.returncode != 0:
                        errors.append(f"restore {'retry ' if scheduled_retry else ''}timer active: {result.stderr.strip()}")
            try:
                self._set_scheduler_backend(series, original_backend)
            except Exception as exc:
                errors.append(f"restore scheduler backend: {exc}")
            return errors

        desired_units = [
            (False, "service", sysd.generate_service(series)),
            (False, "timer", sysd.generate_timer(series, oncalendars)),
        ]
        if retry_oncalendars:
            desired_units.extend([
                (True, "service", sysd.generate_service(series, scheduled_retry=True)),
                (True, "timer", sysd.generate_timer(series, retry_oncalendars)),
            ])

        for scheduled_retry, suffix, content in desired_units:
            modified_units.append((scheduled_retry, suffix))
            result = sysd.write_unit(
                series, suffix, content, scheduled_retry=scheduled_retry,
            )
            if result.returncode != 0:
                rollback_errors = rollback_all()
                result.error = with_rollback_error(result.error or "unit write failed", rollback_errors)
                return result

        result = sysd.daemon_reload()
        if result.returncode != 0:
            result.error = with_rollback_error(result.error or "daemon-reload failed", rollback_all())
            return result

        for scheduled_retry in ([False, True] if retry_oncalendars else [False]):
            result = sysd.enable_timer(series, scheduled_retry=scheduled_retry)
            if result.returncode != 0:
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="", error=with_rollback_error(
                        f"{'retry ' if scheduled_retry else ''}timer enable failed — rolled back",
                        rollback_all(),
                    ),
                )
            result = sysd.restart_timer(series, scheduled_retry=scheduled_retry)
            if result.returncode != 0:
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="", error=with_rollback_error(
                        f"{'retry ' if scheduled_retry else ''}restart timer failed — rolled back",
                        rollback_all(),
                    ),
                )
            if not sysd.timer_is_enabled(series, scheduled_retry=scheduled_retry):
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="", error=with_rollback_error(
                        f"{'retry ' if scheduled_retry else ''}timer enable verification failed — rolled back",
                        rollback_all(),
                    ),
                )
            if not sysd.timer_is_active(series, scheduled_retry=scheduled_retry):
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="", error=with_rollback_error(
                        f"{'retry ' if scheduled_retry else ''}timer active verification failed — rolled back",
                        rollback_all(),
                    ),
                )

        if not retry_oncalendars and any(original_timer_state[True]):
            result = sysd.disable_timer(series, scheduled_retry=True)
            if result.returncode != 0:
                return SchedulerCommandResult(
                    backend="systemd", action="apply", returncode=-1,
                    stdout="", stderr="", error=with_rollback_error(
                        "failed to disable stale retry timer", rollback_all(),
                    ),
                )

        try:
            self._set_scheduler_backend(series, "systemd")
            self._exclude_series_from_cron(series)
        except Exception as exc:
            return SchedulerCommandResult(
                backend="systemd", action="apply", returncode=-1,
                stdout="", stderr="", error=with_rollback_error(
                    f"failed to disable cron for '{series}': {exc}", rollback_all(),
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
            rf"^# BEGIN BILIPOD AUTO - {re.escape(series)}(?:\s+\([^\r\n]*\))?\r?\n.*?"
            rf"^# END BILIPOD AUTO\r?\n?",
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
        cron_user = "bilipod"
        if current_user == cron_user:
            return ["crontab", *args]
        return ["crontab", "-u", cron_user, *args]

    def _read_crontab(self) -> str:
        read_result = subprocess.run(
            self._crontab_command("-l"), capture_output=True, text=True,
            timeout=self._command_timeout_seconds,
        )
        if read_result.returncode == 0:
            return read_result.stdout
        if "no crontab" in read_result.stderr.lower():
            return ""
        raise RuntimeError(f"failed to read crontab: {read_result.stderr.strip()}")

    def _write_crontab(self, content: str) -> None:
        subprocess.run(
            self._crontab_command("-"), input=content,
            capture_output=True, text=True,
            timeout=self._command_timeout_seconds, check=True,
        )

    def _set_scheduler_backend(self, series: str, backend: str) -> None:
        from .. import db
        with db.transaction(self.db_path) as conn:
            db.set_scheduler_backend(conn, series, backend)

    def _scheduler_backend(self, series: str) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT backend FROM scheduler_backend WHERE series=?", (series,),
            ).fetchone()
        return str(row[0] if row else "cron")

    def _cron_retry_series(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT c.series FROM cron_schedule c "
                "LEFT JOIN scheduler_backend b ON b.series=c.series "
                "WHERE c.enabled=1 AND c.kind='retry' "
                "AND COALESCE(b.backend, 'cron')='cron' ORDER BY c.series"
            ).fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    def _cron_marker_title(title: Any, series: str) -> str:
        normalized = " ".join(str(title or "").splitlines()).strip()
        return normalized or series

    def _restore_cron_for_series(self, series: str) -> None:
        """Restore only *series* to cron after its systemd timer is disabled."""
        schedules = [
            entry for entry in self.list_enabled_schedules(series)
            if entry.kind == "primary"
        ]
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
        lines = [f"# BEGIN BILIPOD AUTO - {series} ({title})"]
        lines.extend(f"{entry.schedule} {wrapper}" for entry in schedules)
        lines.append("# END BILIPOD AUTO")
        block = "\n".join(lines)

        existing = self._read_crontab()
        import re
        cleaned = re.sub(
            rf"^# BEGIN BILIPOD AUTO - {re.escape(series)}(?:\s+\([^\r\n]*\))?\r?\n.*?"
            rf"^# END BILIPOD AUTO\r?\n?",
            "", existing, flags=re.DOTALL | re.MULTILINE,
        ).strip()
        content = f"{cleaned}\n\n{block}\n" if cleaned else f"{block}\n"
        self._write_crontab(content)

    # ── list_schedules / replace_schedules (DB only) ──────────────────

    def _update_period(self, series: str) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT update_period FROM sync_policy WHERE series=?", (series,),
            ).fetchone()
        return str(row[0] if row else "12h")

    def list_schedules(self, series: str) -> list[ScheduleEntry]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, series, schedule, enabled, position, kind "
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
                kind=r["kind"],
            )
            for r in rows
        ]

    def list_enabled_schedules(self, series: str) -> list[ScheduleEntry]:
        """Return only enabled schedules (enabled=1) for installation
        into cron/systemd."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, series, schedule, enabled, position, kind "
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
                kind=r["kind"],
            )
            for r in rows
        ]

    def replace_schedules(
        self,
        series: str,
        schedules: list[str],
        retry_schedules: list[str] | None = None,
    ) -> int:
        retry_schedules = retry_schedules or []
        with sqlite3.connect(self.db_path) as conn:
            count = replace_schedules_in_connection(conn, series, schedules, retry_schedules)
            conn.commit()
        return count

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
            info["retry_timer_unit"] = sysd.unit_name(s, "timer", scheduled_retry=True)
            info["retry_enabled"] = sysd.timer_is_enabled(s, scheduled_retry=True)
            info["retry_active"] = sysd.timer_is_active(s, scheduled_retry=True)
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

        if any(entry.kind == "retry" for entry in self.list_enabled_schedules(series)):
            return SchedulerCommandResult(
                backend="systemd", action="disable", returncode=1,
                stdout="", stderr="retry schedules are not supported by cron; remove them before restoring cron",
            )

        was_enabled = sysd.timer_is_enabled(series)
        was_active = sysd.timer_is_active(series)
        dt = sysd.disable_timer(series)
        missing_markers = ("not found", "not loaded", "does not exist")
        if dt.returncode != 0 and not any(marker in dt.stderr.lower() for marker in missing_markers):
            return SchedulerCommandResult(
                backend="systemd", action="disable", returncode=1,
                stdout="", stderr=f"disable timer: {dt.stderr.strip()}",
            )
        retry_dt = sysd.disable_timer(series, scheduled_retry=True)
        if retry_dt.returncode != 0 and not any(
            marker in retry_dt.stderr.lower() for marker in missing_markers
        ):
            if was_enabled:
                sysd.enable_timer(series)
            if was_active:
                sysd.start_timer(series)
            return SchedulerCommandResult(
                backend="systemd", action="disable", returncode=1,
                stdout="", stderr=f"disable retry timer: {retry_dt.stderr.strip()}",
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
            for scheduled_retry in (False, True):
                for suffix in ("service", "timer"):
                    result = sysd.remove_unit(
                        series, suffix, scheduled_retry=scheduled_retry,
                    )
                    if result.returncode != 0:
                        errors.append(f"remove {'retry ' if scheduled_retry else ''}{suffix}: {result.stderr or result.error or 'failed'}")
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
        was_enabled = sysd.timer_is_enabled(series)
        was_active = sysd.timer_is_active(series)
        dt = sysd.disable_timer(series)
        missing_markers = ("not found", "not loaded", "does not exist", "systemctl not found")
        if dt.returncode != 0 and not any(marker in dt.stderr.lower() for marker in missing_markers):
            return SchedulerCommandResult(
                backend="systemd", action="remove-series", returncode=1,
                stdout="", stderr=f"disable timer: {dt.stderr.strip()}",
            )
        retry_dt = sysd.disable_timer(series, scheduled_retry=True)
        if retry_dt.returncode != 0 and not any(
            marker in retry_dt.stderr.lower() for marker in missing_markers
        ):
            if was_enabled:
                sysd.enable_timer(series)
            if was_active:
                sysd.start_timer(series)
            return SchedulerCommandResult(
                backend="systemd", action="remove-series", returncode=1,
                stdout="", stderr=f"disable retry timer: {retry_dt.stderr.strip()}",
            )

        try:
            self._exclude_series_from_cron(series)
        except Exception as e:
            errors.append(f"remove cron: {e}")

        if delete_units:
            for scheduled_retry in (False, True):
                for suffix in ("service", "timer"):
                    result = sysd.remove_unit(
                        series, suffix, scheduled_retry=scheduled_retry,
                    )
                    if result.returncode != 0:
                        errors.append(f"remove {'retry ' if scheduled_retry else ''}{suffix}: {result.stderr.strip()}")

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
