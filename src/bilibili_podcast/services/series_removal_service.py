from __future__ import annotations

import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import db
from ..config.manager import ConfigError, ConfigManager


@dataclass
class SeriesRemovalPlan:
    series: str
    title: str
    uid: int | None
    media_dir: str
    media_files: int
    json_dir: str
    json_files: int
    master_rss: str
    master_rss_exists: bool
    published_rss_files: list[str]
    wrapper_script: str
    wrapper_exists: bool
    browser_profile_dir: str
    browser_profile_files: int
    users_conf: str
    users_conf_references: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SeriesRemovalService:
    """Plan and remove one series' database and local filesystem artifacts."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        media_root: str | Path,
        json_root: str | Path,
        rss_root: str | Path,
        published_rss_root: str | Path,
        cron_script_dir: str | Path,
        browser_user_data_root: str | Path,
        users_conf: str | Path,
    ) -> None:
        self.db_path = str(db_path)
        self.media_root = Path(media_root)
        self.json_root = Path(json_root)
        self.rss_root = Path(rss_root)
        self.published_rss_root = Path(published_rss_root)
        self.cron_script_dir = Path(cron_script_dir)
        self.browser_user_data_root = Path(browser_user_data_root)
        self.users_conf = Path(users_conf)
        if self.users_conf.name != "rss-users.toml":
            raise ValueError("users_conf must reference rss-users.toml")
        self.publish_conf = self.users_conf.parent / "publish.toml"

    def list_series_for_uid(self, uid: int) -> list[str]:
        with db.transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT s.series FROM series s "
                "JOIN series_source ss ON ss.series=s.series "
                "WHERE ss.uid=? ORDER BY s.series",
                (uid,),
            ).fetchall()
        return [row["series"] for row in rows]

    def plan(self, series: str) -> SeriesRemovalPlan:
        with db.transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT s.series, s.title, ss.uid FROM series s "
                "LEFT JOIN series_source ss ON ss.series=s.series "
                "WHERE s.series=?",
                (series,),
            ).fetchone()
        if row is None:
            raise ValueError(f"series not found: {series}")

        media_dir = self.media_root / series
        json_dir = self.json_root / series
        master_rss = self.rss_root / f"{series}.xml"
        wrapper = self.cron_script_dir / f"run_{series}_sync.sh"
        browser_profile = self.browser_user_data_root / series
        published = sorted(
            str(path)
            for path in self.published_rss_root.glob(f".generations/*/*/{series}.xml")
        )
        return SeriesRemovalPlan(
            series=series,
            title=row["title"],
            uid=row["uid"],
            media_dir=str(media_dir),
            media_files=self._file_count(media_dir),
            json_dir=str(json_dir),
            json_files=self._file_count(json_dir),
            master_rss=str(master_rss),
            master_rss_exists=master_rss.exists(),
            published_rss_files=published,
            wrapper_script=str(wrapper),
            wrapper_exists=wrapper.exists(),
            browser_profile_dir=str(browser_profile),
            browser_profile_files=self._file_count(browser_profile),
            users_conf=str(self.users_conf),
            users_conf_references=self._users_conf_reference_count(series),
        )

    def remove(self, series: str) -> SeriesRemovalPlan:
        plan = self.plan(series)
        gone_series = self._read_gone_series()

        paths = [
            Path(plan.media_dir), Path(plan.json_dir), Path(plan.master_rss),
            *(Path(path) for path in plan.published_rss_files),
            Path(plan.wrapper_script), Path(plan.browser_profile_dir),
        ]
        staged: list[tuple[Path, Path]] = []
        users_backup = self._backup_users_conf()
        publish_backup = self._backup_file(self.publish_conf)
        try:
            for path in paths:
                if not path.exists() and not path.is_symlink():
                    continue
                quarantine = path.with_name(
                    f".{path.name}.bilibili-podcast-remove-{uuid.uuid4().hex}"
                )
                path.replace(quarantine)
                staged.append((path, quarantine))
            self._remove_users_conf_reference(series)
            self._mark_series_gone(series, gone_series)
            with db.transaction(self.db_path) as conn:
                conn.execute("DELETE FROM series WHERE series=?", (series,))
        except Exception as exc:
            rollback_errors: list[str] = []
            try:
                self._restore_users_conf(users_backup)
            except OSError as rollback_exc:
                rollback_errors.append(f"rss users: {type(rollback_exc).__name__}")
            try:
                self._restore_file_backup(self.publish_conf, publish_backup)
            except OSError as rollback_exc:
                rollback_errors.append(f"publish config: {type(rollback_exc).__name__}")
            for original, quarantine in reversed(staged):
                try:
                    if quarantine.exists() or quarantine.is_symlink():
                        quarantine.replace(original)
                except OSError as rollback_exc:
                    rollback_errors.append(
                        f"{original.name}: {type(rollback_exc).__name__}"
                    )
            if rollback_errors:
                raise RuntimeError(
                    "series removal failed and rollback was incomplete: "
                    + ", ".join(rollback_errors)
                ) from exc
            raise
        for _, quarantine in staged:
            self._remove_path(quarantine)
        return plan

    def _backup_users_conf(self) -> tuple[bool, bytes, int]:
        return self._backup_file(self.users_conf)

    @staticmethod
    def _backup_file(path: Path) -> tuple[bool, bytes, int]:
        if not path.exists():
            return False, b"", 0
        return True, path.read_bytes(), path.stat().st_mode & 0o777

    def _restore_users_conf(self, backup: tuple[bool, bytes, int]) -> None:
        self._restore_file_backup(self.users_conf, backup)

    def _restore_file_backup(self, path: Path, backup: tuple[bool, bytes, int]) -> None:
        existed, content, mode = backup
        if not existed:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        ConfigManager.write_file_bytes(path, content, mode)

    @staticmethod
    def _file_count(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(1 for item in path.rglob("*") if item.is_file())

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)

    def _users_conf_reference_count(self, series: str) -> int:
        if not self.users_conf.exists():
            return 0
        users = self._read_toml_users()
        return sum(1 for user in users.values() if series in user["series"])

    def _remove_users_conf_reference(self, series: str) -> None:
        if not self.users_conf.exists():
            return
        users = self._read_toml_users()
        changed = False
        for name, user in users.items():
            names = [item for item in user.get("series", []) if item != series]
            changed = changed or len(names) != len(user.get("series", []))
            if not names:
                user["series"] = []
                continue
            user["series"] = names
        if changed:
            ConfigManager.write_rss_users_file(
                self.users_conf,
                {name: user for name, user in users.items()
                 if user.get("series")},
                self.users_conf.stat().st_mode & 0o777,
            )

    def _read_gone_series(self) -> list[str]:
        if not self.publish_conf.is_file():
            raise ValueError("publish.toml is required for series removal")
        try:
            return ConfigManager.read_publish_gone_series(self.publish_conf)
        except ConfigError as exc:
            raise ValueError(str(exc)) from None

    def _mark_series_gone(self, series: str, gone_series: list[str]) -> None:
        if series in gone_series:
            return
        try:
            ConfigManager.write_publish_gone_series(
                self.publish_conf,
                [*gone_series, series],
                self.publish_conf.stat().st_mode & 0o777,
            )
        except ConfigError as exc:
            raise ValueError(str(exc)) from None

    def _read_toml_users(self) -> dict[str, dict[str, Any]]:
        try:
            return ConfigManager.read_rss_users_file(self.users_conf)
        except ConfigError as exc:
            raise ValueError(str(exc)) from None
