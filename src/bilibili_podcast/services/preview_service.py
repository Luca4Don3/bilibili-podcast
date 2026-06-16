from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class DryRunResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    error: Optional[str] = None


def _find_sync_bin() -> str | None:
    """Find the bilibili-podcast sync binary."""
    candidate = Path(sys.executable).parent / "bilibili-podcast"
    if candidate.exists():
        return str(candidate)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / "bilibili-podcast"
        if candidate.exists():
            return str(candidate)
    return None


class PreviewService:
    """Run dry-run/preview of bilibili-podcast sync."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def run_dry_run(
        self,
        series: str,
        extra_args: list[str] | None = None,
        timeout: int = 120,
        env_overrides: dict[str, str] | None = None,
    ) -> DryRunResult:
        sync_bin = _find_sync_bin()
        if not sync_bin:
            return DryRunResult(
                stdout="", stderr="",
                returncode=-1, timed_out=False,
                error="未配置 BILIBILI_PODCAST_SYNC_PATH，无法执行干跑。",
            )

        cmd = [sync_bin, "--config-db", self.db_path, "--series", series, "--log-level", "DEBUG"]
        if extra_args:
            cmd.extend(extra_args)

        env = {**os.environ, **(env_overrides or {})}
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            return DryRunResult(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                returncode=result.returncode,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            return DryRunResult(
                stdout="", stderr="",
                returncode=-1, timed_out=True,
                error=f"执行超时（{timeout} 秒）",
            )
        except FileNotFoundError:
            return DryRunResult(
                stdout="", stderr="",
                returncode=-1, timed_out=False,
                error=f"找不到可执行文件: {sync_bin}",
            )
        except Exception as e:
            return DryRunResult(
                stdout="", stderr="",
                returncode=-1, timed_out=False,
                error=f"执行错误: {e}",
            )

    def run_preview(self, series: str, **overrides) -> DryRunResult:
        """Convenience for CLI-style preview."""
        args = []
        cookie_file = overrides.get("cookie_file") or os.environ.get("BILIBILI_PODCAST_COOKIE_FILE", "")
        if cookie_file:
            args.extend(["--cookie-file", cookie_file])
        for key in ("media_root", "json_root", "rss_root", "lock_file",
                     "media_base_url", "browser_user_data_root", "log_dir"):
            val = overrides.get(key) or os.environ.get(f"BILIBILI_PODCAST_{key.upper()}", "")
            if val:
                args.extend([f"--{key.replace('_', '-')}", val])
        return self.run_dry_run(series, extra_args=args)
