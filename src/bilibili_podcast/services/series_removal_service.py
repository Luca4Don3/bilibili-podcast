from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import db


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
            str(path) for path in self.published_rss_root.glob(f"*/{series}.xml")
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

        self._remove_path(Path(plan.media_dir))
        self._remove_path(Path(plan.json_dir))
        Path(plan.master_rss).unlink(missing_ok=True)
        for path in plan.published_rss_files:
            Path(path).unlink(missing_ok=True)
        Path(plan.wrapper_script).unlink(missing_ok=True)
        self._remove_path(Path(plan.browser_profile_dir))
        self._remove_users_conf_reference(series)

        with db.transaction(self.db_path) as conn:
            conn.execute("DELETE FROM series WHERE series=?", (series,))
        return plan

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
        count = 0
        for raw in self.users_conf.read_text(encoding="utf-8").splitlines():
            content = raw.split("#", 1)[0].strip()
            if ":" not in content:
                continue
            _, series_list = content.split(":", 1)
            names = {item.strip() for item in series_list.split(",")}
            if series in names:
                count += 1
        return count

    def _remove_users_conf_reference(self, series: str) -> None:
        if not self.users_conf.exists():
            return
        output: list[str] = []
        changed = False
        for raw in self.users_conf.read_text(encoding="utf-8").splitlines():
            content, marker, comment = raw.partition("#")
            if ":" not in content:
                output.append(raw)
                continue
            token, series_list = content.split(":", 1)
            names = [item.strip() for item in series_list.split(",") if item.strip()]
            if series not in names:
                output.append(raw)
                continue
            names = [name for name in names if name != series]
            changed = True
            if not names:
                continue
            rebuilt = f"{token.strip()}:{','.join(names)}"
            if marker:
                rebuilt += f" #{comment}"
            output.append(rebuilt)
        if changed:
            self.users_conf.write_text("\n".join(output) + "\n", encoding="utf-8")
