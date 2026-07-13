"""Systemd backend for SchedulerService — generate, plan, apply, status."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .scheduler_service import SchedulerCommandResult

# ── well-known paths (overridable via env for testing) ───────────────────

SYSTEMD_DIR = Path(os.environ.get("BILIPOD_SYSTEMD_DIR", "/etc/systemd/system"))
APP_DIR = Path(os.environ.get("BILIPOD_APP_DIR", "/opt/bilipod/app"))
ENV_FILE = Path(os.environ.get("BILIPOD_ENV_FILE", "/opt/bilipod/bilipod-env.sh"))
VENV_BIN = Path(os.environ.get("BILIPOD_VENV_BIN", "/opt/bilipod/venv/bin"))
STATE_DIR = Path(os.environ.get("BILIPOD_STATE_DIR", "/var/lib/bilipod/state"))
SECRETS_DIR = Path(os.environ.get("BILIPOD_SECRETS_DIR", "/opt/bilipod/secrets"))
MEDIA_ROOT = Path(os.environ.get("BILIPOD_MEDIA_ROOT", "/var/lib/bilipod/media"))
JSON_ROOT = Path(os.environ.get("BILIPOD_JSON_ROOT", "/var/lib/bilipod/json"))
RSS_ROOT = Path(os.environ.get("BILIPOD_RSS_ROOT", "/var/lib/bilipod/rss"))
LOG_DIR = Path(os.environ.get("BILIPOD_LOG_DIR", "/var/log/bilipod"))
BROWSER_DATA_ROOT = Path(os.environ.get("BILIPOD_BROWSER_USER_DATA_ROOT",
                                         "/opt/bilipod/browser-profiles"))
PLAYWRIGHT_BWS = os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                                "/opt/bilipod/playwright-browsers")
DB_PATH = STATE_DIR / "bilipod.db"
COOKIE_FILE = SECRETS_DIR / "www.bilibili.com_cookies.txt"


def _env_file_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    prefix = f"{key}="
    export_prefix = f"export {key}="
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(export_prefix):
            value = line[len(export_prefix):]
        elif line.startswith(prefix):
            value = line[len(prefix):]
        else:
            continue
        return value.strip().strip('"').strip("'")
    return None


MEDIA_BASE_URL = (
    os.environ.get("BILIPOD_MEDIA_BASE_URL")
    or _env_file_value(ENV_FILE, "BILIPOD_MEDIA_BASE_URL")
    or "http://localhost:58743"
)
SYNC_LOG_LEVEL = os.environ.get("BILIPOD_SYNC_LOG_LEVEL", "INFO")

# ── service unit template ──────────────────────────────────────────────

SERVICE_TEMPLATE = """\
[Unit]
Description=Bilipod Sync — {series}
After=network.target

[Service]
Type=oneshot
User=bilipod
Group=bilipod
WorkingDirectory={app_dir}
Environment=PLAYWRIGHT_BROWSERS_PATH={pw_browsers}
ExecStart={sync_bin} --config-db {db_path} --series {series} --cookie-file {cookie_file} --media-root {media_root} --json-root {json_root} --rss-root {rss_root} --state-root {state_dir} --lock-file {lock_file} --log-dir {log_dir} --media-base-url {media_base_url} --browser-user-data-root {browser_data_root} --max-downloads-per-run 1 --min-free-gb 5 --token __MEDIA_PLACEHOLDER__ --apply --publish-script {rss_publish}{retry_args}{log_level_args}
Restart=no
TimeoutStartSec=1800
"""

# ── timer unit template ────────────────────────────────────────────────

TIMER_TEMPLATE = """\
[Unit]
Description=Bilipod Sync Timer — {series}

[Timer]
{oncalendar_lines}
Persistent=false

[Install]
WantedBy=timers.target
"""


# ── cron → OnCalendar conversion ─────────────────────────────────────

# Weekday name → systemd weekday mapping
_WEEKDAYS = {
    0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat",
}

_SIMPLE_CRON_RE = re.compile(
    r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+(\*|[0-6])\s*$"
)


def cron_to_oncalendar(expr: str) -> Optional[str]:
    """Convert a simple cron expression to systemd OnCalendar format.

    Supports:
      - ``M H * * *``  (daily)        → ``*-*-* H:M:00``
      - ``M H * * N``  (weekly)       → ``N.. H:M:00``

    Returns ``None`` for unconvertable expressions.
    """
    expr = expr.strip()
    m = _SIMPLE_CRON_RE.match(expr)
    if not m:
        return None
    minute, hour, weekday = m.group(1), m.group(2), m.group(3)
    mm = minute.zfill(2)
    hh = hour.zfill(2)
    if weekday == "*":
        return f"*-*-* {hh}:{mm}:00"
    wd = _WEEKDAYS.get(int(weekday))
    if wd is None:
        return None
    return f"{wd} *-*-* {hh}:{mm}:00"


# ── unit content generators ──────────────────────────────────────────

_ENV_FILE = ENV_FILE
_LOCK_FILE = STATE_DIR / "bilibili-podcast.lock"
_SYNC_BIN = VENV_BIN / "bilibili-podcast"
_RSS_PUBLISH = Path(os.environ.get("BILIPOD_RSS_PUBLISH",
                                     "/opt/bilipod/rss-publish-and-sync.sh"))


def _log_level_args() -> str:
    """Return the log-level CLI args for the sync ExecStart line."""
    level = (os.environ.get("BILIPOD_SYNC_LOG_LEVEL", SYNC_LOG_LEVEL) or "INFO").upper()
    if level == "DEBUG":
        # Keep the historical --debug flag so existing tooling continues to work.
        return " --debug"
    if level in ("INFO", "WARNING", "ERROR", "CRITICAL"):
        return f" --log-level {level}"
    # Unknown level: fall back to INFO to avoid breaking the unit.
    return " --log-level INFO"


def generate_service(series: str, *, scheduled_retry: bool = False) -> str:
    """Return the service unit content for *series*."""
    return SERVICE_TEMPLATE.format(
        series=series,
        app_dir=APP_DIR,
        pw_browsers=PLAYWRIGHT_BWS,
        sync_bin=_SYNC_BIN,
        db_path=DB_PATH,
        cookie_file=COOKIE_FILE,
        media_root=MEDIA_ROOT,
        json_root=JSON_ROOT,
        rss_root=RSS_ROOT,
        state_dir=STATE_DIR,
        lock_file=_LOCK_FILE,
        log_dir=LOG_DIR,
        media_base_url=MEDIA_BASE_URL,
        browser_data_root=BROWSER_DATA_ROOT,
        rss_publish=_RSS_PUBLISH,
        retry_args=" --scheduled-retry" if scheduled_retry else "",
        log_level_args=_log_level_args(),
    )


def generate_timer(series: str, oncalendars: list[str]) -> str:
    """Return the timer unit content for *series* with one or more
    pre-converted OnCalendar expressions.  Each entry becomes an
    ``OnCalendar=...`` line in the ``[Timer]`` section."""
    lines = "\n".join(f"OnCalendar={oc}" for oc in oncalendars)
    return TIMER_TEMPLATE.format(series=series, oncalendar_lines=lines)


def unit_name(series: str, suffix: str = "service", *, scheduled_retry: bool = False) -> str:
    """Return the systemd unit file name, e.g. ``bilipod-sync@myseries.service``."""
    prefix = "bilipod-retry" if scheduled_retry else "bilipod-sync"
    return f"{prefix}@{series}.{suffix}"


# ── systemd interaction helpers ───────────────────────────────────────

def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run ``systemctl`` (with ``sudo`` fallback) and return the result.

    Returns a fake failure result if ``systemctl`` is not available.
    """
    try:
        r = subprocess.run(
            ["systemctl", *args],
            capture_output=True, text=True, timeout=30,
        )
        # Retry with sudo if permission denied
        if r.returncode != 0 and ("denied" in r.stderr.lower() or "not authorized" in r.stderr.lower()):
            r = subprocess.run(
                ["sudo", "systemctl", *args],
                capture_output=True, text=True, timeout=30,
            )
        return r
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=["systemctl", *args], returncode=255,
            stdout="", stderr="systemctl not found",
        )


def daemon_reload() -> SchedulerCommandResult:
    """Execute ``systemctl daemon-reload``."""
    result = _systemctl("daemon-reload")
    return SchedulerCommandResult(
        backend="systemd",
        action="daemon-reload",
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command=["systemctl", "daemon-reload"],
    )


def enable_timer(series: str, *, scheduled_retry: bool = False) -> SchedulerCommandResult:
    """Enable (but not start) the timer for *series*.

    Using ``enable --now`` with ``Persistent=true`` would cause an
    immediate catch-up sync, triggering real B站 API calls.  We
    deliberately only ``enable`` without ``--now``.
    """
    u = unit_name(series, "timer", scheduled_retry=scheduled_retry)
    result = _systemctl("enable", u)
    return SchedulerCommandResult(
        backend="systemd",
        action="enable-timer",
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command=["systemctl", "enable", u],
    )


def start_timer(series: str, *, scheduled_retry: bool = False) -> SchedulerCommandResult:
    """Start the timer (arm it in the current boot cycle) without
    triggering the service.

    ``systemctl start <timer>`` just activates the timer scheduling;
    it does NOT invoke the service.  The service only runs when the
    OnCalendar event fires.  With ``Persistent=false``, no catch-up
    sync occurs either.
    """
    u = unit_name(series, "timer", scheduled_retry=scheduled_retry)
    result = _systemctl("start", u)
    return SchedulerCommandResult(
        backend="systemd",
        action="start-timer",
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command=["systemctl", "start", u],
    )


def restart_timer(series: str, *, scheduled_retry: bool = False) -> SchedulerCommandResult:
    """Restart the timer so updated unit contents take effect."""
    u = unit_name(series, "timer", scheduled_retry=scheduled_retry)
    result = _systemctl("restart", u)
    return SchedulerCommandResult(
        backend="systemd",
        action="restart-timer",
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command=["systemctl", "restart", u],
    )


def disable_timer(series: str, *, scheduled_retry: bool = False) -> SchedulerCommandResult:
    """Disable and stop the timer for *series*."""
    u = unit_name(series, "timer", scheduled_retry=scheduled_retry)
    result = _systemctl("disable", "--now", u)
    return SchedulerCommandResult(
        backend="systemd",
        action="disable-timer",
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command=["systemctl", "disable", "--now", u],
    )


def write_unit(series: str, suffix: str, content: str, *, scheduled_retry: bool = False) -> SchedulerCommandResult:
    """Write a unit file, using an atomic local replace and sudo fallback."""
    path = SYSTEMD_DIR / unit_name(series, suffix, scheduled_retry=scheduled_retry)
    tmp_path: Path | None = None
    sudo_tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.chmod(0o644)
        tmp_path.replace(path)
        return SchedulerCommandResult(
            backend="systemd", action="write-unit", returncode=0,
            stdout="", stderr="", command=["replace", str(tmp_path), str(path)],
        )
    except OSError:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    try:
        fd, tmp_name = tempfile.mkstemp(suffix=f".{suffix}")
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_text(content, encoding="utf-8")
        sudo_tmp_path = path.parent / f".{path.name}.{tmp_path.name}.tmp"
        subprocess.run(
            ["sudo", "cp", str(tmp_path), str(sudo_tmp_path)],
            capture_output=True, text=True, timeout=10, check=True,
        )
        subprocess.run(
            ["sudo", "chmod", "644", str(sudo_tmp_path)],
            capture_output=True, text=True, timeout=10, check=True,
        )
        subprocess.run(
            ["sudo", "mv", str(sudo_tmp_path), str(path)],
            capture_output=True, text=True, timeout=10, check=True,
        )
        command = ["sudo", "mv", str(sudo_tmp_path), str(path)]
        sudo_tmp_path = None
        return SchedulerCommandResult(
            backend="systemd", action="write-unit", returncode=0,
            stdout="", stderr="", command=command,
        )
    except Exception as exc:
        return SchedulerCommandResult(
            backend="systemd", action="write-unit", returncode=1,
            stdout="", stderr=str(exc),
            command=["sudo", "mv", str(sudo_tmp_path) if sudo_tmp_path else "<unknown>", str(path)],
            error=f"failed to write {path}: {exc}",
        )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        if sudo_tmp_path is not None:
            try:
                subprocess.run(
                    ["sudo", "rm", "-f", str(sudo_tmp_path)],
                    capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass


def remove_unit(series: str, suffix: str, *, scheduled_retry: bool = False) -> SchedulerCommandResult:
    """Remove a generated unit file, using sudo when direct unlink is denied."""
    path = SYSTEMD_DIR / unit_name(series, suffix, scheduled_retry=scheduled_retry)
    if not path.exists():
        return SchedulerCommandResult(
            backend="systemd",
            action="remove-unit",
            returncode=0,
            stdout="",
            stderr="",
            command=[],
        )
    try:
        path.unlink()
        return SchedulerCommandResult(
            backend="systemd",
            action="remove-unit",
            returncode=0,
            stdout="",
            stderr="",
            command=["unlink", str(path)],
        )
    except OSError:
        command = ["sudo", "rm", "-f", str(path)]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SchedulerCommandResult(
                backend="systemd",
                action="remove-unit",
                returncode=1,
                stdout="",
                stderr=str(exc),
                command=command,
                error=f"failed to remove {path}: {exc}",
            )
        return SchedulerCommandResult(
            backend="systemd",
            action="remove-unit",
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            command=command,
        )


def timer_is_enabled(series: str, *, scheduled_retry: bool = False) -> bool:
    """Check if the timer for *series* is enabled (not necessarily active)."""
    u = unit_name(series, "timer", scheduled_retry=scheduled_retry)
    r = _systemctl("is-enabled", u)
    return r.returncode == 0 and r.stdout.strip() == "enabled"


def timer_is_active(series: str, *, scheduled_retry: bool = False) -> bool:
    """Check if the timer for *series* is enabled and active."""
    u = unit_name(series, "timer", scheduled_retry=scheduled_retry)
    r = _systemctl("is-enabled", u)
    enabled = r.returncode == 0 and r.stdout.strip() == "enabled"
    r2 = _systemctl("is-active", u)
    active = r2.returncode == 0 and r2.stdout.strip() == "active"
    return enabled and active


def systemctl_show(unit: str) -> dict[str, str]:
    """Run ``systemctl show <unit>`` and return key-value pairs.

    Returns an empty dict if ``systemctl`` is not available.
    """
    try:
        r = subprocess.run(
            ["systemctl", "show", unit],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return {}
    if r.returncode != 0:
        return {}
    result: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            result[k] = v
    return result
