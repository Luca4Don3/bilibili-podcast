"""Tests for SQLite database layer and config_store dual-read."""

import sqlite3
from pathlib import Path

import pytest

from bilibili_podcast import db
from bilibili_podcast.config_store import from_args, YamlStore, DbStore
from bilibili_podcast.utils.series_config import SeriesConfig


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    db.migrate(str(path))
    return path


@pytest.fixture
def sample_config() -> SeriesConfig:
    return SeriesConfig(
        series="testseries",
        enabled=True,
        title="测试系列",
        description="A test series",
        author="测试作者",
        cover_art="https://example.com/cover.jpg",
        category="Technology",
        subcategories=["Tech"],
        explicit=False,
        lang="zh-CN",
        source={
            "space_url": "https://space.bilibili.com/12345",
            "uid": 12345,
            "type": "space",
            "sid": None,
        },
        sync={
            "page_size": 20,
            "incremental_page_size": 5,
            "max_pages": 10,
            "max_requests_per_series": 8,
            "request_interval_seconds": 2.0,
            "request_jitter_seconds": 0.5,
            "rate_limit_cooldown_seconds": 21600,
            "update_period": "12h",
            "format": "audio",
            "quality": "64K",
            "fetch_strategy": "api_first",
            "browser_fallback": False,
            "browser_wait_min_seconds": 4.0,
            "browser_wait_max_seconds": 8.0,
            "browser_fallback_cooldown_seconds": 3600,
            "require_paid_state_confirmation": False,
            "min_duration_seconds": 0,
            "max_duration_seconds": 0,
        },
        filters={
            "exclude_paid": True,
            "exclude_bvids": ["BV1xx", "BV2yy"],
            "advertisement_bvids": ["BV3zz"],
            "exclude_season_ids": [5492168],
            "exclude_keywords": ["spam"],
            "advertisement_keywords": ["ad"],
            "include_keywords": [],
        },
        paid_preview={"enabled": False, "retry_after_days": 4},
        keep_last=100,
    )


@pytest.fixture
def state_data() -> dict:
    return {
        "last_attempt_at": 1700000000,
        "last_success_at": 1700000100,
        "last_browser_fallback_at": 0,
        "rate_limited_until": 0,
    }


# ── Schema / Migrate ────────────────────────────────────────────────


class TestMigrate:
    def test_migrate_creates_tables(self, db_path: Path):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "series" in tables
        assert "series_source" in tables
        assert "sync_policy" in tables
        assert "filter_rule" in tables
        assert "paid_preview_policy" in tables
        assert "cron_schedule" in tables
        assert "access_rule" in tables
        assert "sync_state" in tables
        assert "schema_version" in tables

    def test_migrate_is_idempotent(self, db_path: Path):
        db.migrate(str(db_path))
        db.migrate(str(db_path))

    def test_migrate_creates_wal_mode(self, db_path: Path):
        conn = sqlite3.connect(str(db_path))
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert journal in ("wal", "delete")

    def test_existing_client_can_read_and_write_after_online_migration(self, db_path: Path):
        legacy = sqlite3.connect(str(db_path))
        legacy.execute("PRAGMA journal_mode=WAL")
        db.migrate(str(db_path))
        legacy.execute(
            "INSERT INTO series(series,title,author) VALUES('legacy-client','L','A')"
        )
        legacy.commit()
        with db.transaction(str(db_path)) as current:
            assert current.execute(
                "SELECT title FROM series WHERE series='legacy-client'"
            ).fetchone()[0] == "L"
        legacy.close()

    def test_existing_old_schema_requires_explicit_upgrade_plan(self, tmp_path: Path):
        path = tmp_path / "old-schema.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE cron_schedule(id INTEGER PRIMARY KEY, series TEXT, schedule TEXT)"
            )

        with pytest.raises(db.DatabaseUpgradeRequired, match="explicit upgrade plan"):
            db.migrate(path)

        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cron_schedule)")}
        assert "kind" not in columns


# ── transaction context manager ─────────────────────────────────────


class TestTransaction:
    def test_shared_connection_policy(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    def test_commits_on_success(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO series (series, title, author) VALUES (?, ?, ?)",
                ("tx-test", "TX", "Author"),
            )
        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        row = conn2.execute("SELECT series FROM series WHERE series='tx-test'").fetchone()
        conn2.close()
        assert row is not None

    def test_rolls_back_on_error(self, db_path: Path):
        try:
            with db.transaction(str(db_path)) as conn:
                conn.execute(
                    "INSERT INTO series (series, title, author) VALUES (?, ?, ?)",
                    ("rollback-test", "RB", "Author"),
                )
                raise ValueError("rollback")
        except ValueError:
            pass
        conn2 = sqlite3.connect(str(db_path))
        rows = conn2.execute("SELECT series FROM series WHERE series='rollback-test'").fetchall()
        conn2.close()
        assert len(rows) == 0


# ── CRUD: series ────────────────────────────────────────────────────


class TestUpsertSeries:
    def test_upsert_inserts(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT series, title, author FROM series").fetchone()
        conn.close()
        assert row["series"] == "testseries"
        assert row["title"] == "测试系列"
        assert row["author"] == "测试作者"

    def test_upsert_updates(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            sample_config.title = "Updated Title"
            db.upsert_series(conn, sample_config)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT title FROM series").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["title"] == "Updated Title"


# ── CRUD: source / sync_policy / filters / paid_preview / cron ─────


class TestUpsertSource:
    def test_upsert_source(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            db.upsert_source(conn, sample_config)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM series_source").fetchone()
        conn.close()
        assert row["uid"] == 12345
        assert row["type"] == "space"


class TestUpsertSyncPolicy:
    def test_upsert_sync_policy(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            db.upsert_sync_policy(conn, sample_config)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT quality, page_size FROM sync_policy").fetchone()
        conn.close()
        assert row["quality"] == "64K"
        assert row["page_size"] == 20


class TestUpsertFilters:
    def test_upsert_filters(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            db.upsert_filters(conn, sample_config)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT rule_type, value FROM filter_rule ORDER BY position"
        ).fetchall()
        conn.close()
        types = [r["rule_type"] for r in rows]
        assert "exclude_paid" in types
        assert "exclude_bvid" in types
        assert "advertisement_bvid" in types
        assert "exclude_keyword" in types
        assert "advertisement_keyword" in types
        assert "exclude_season_id" in types

    def test_upsert_filters_replaces(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            db.upsert_filters(conn, sample_config)
            sample_config.filters = {"exclude_paid": True}
            db.upsert_filters(conn, sample_config)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT rule_type FROM filter_rule WHERE series='testseries'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1


class TestUpsertPaidPreview:
    def test_upsert_paid_preview(self, db_path: Path, sample_config: SeriesConfig):
        sample_config.paid_preview = {"enabled": True, "retry_after_days": 7}
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            db.upsert_paid_preview(conn, sample_config)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM paid_preview_policy").fetchone()
        conn.close()
        assert row["enabled"] == 1
        assert row["retry_after_days"] == 7


class TestUpsertCron:
    def test_upsert_cron(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO series (series, title, author) VALUES ('cron-test', 'Cron', 'Author')"
            )
            db.upsert_cron(conn, "cron-test", ["0 9 * * *", "30 18 * * *"])
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT schedule FROM cron_schedule ORDER BY position"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0]["schedule"] == "0 9 * * *"
        assert rows[1]["schedule"] == "30 18 * * *"

    def test_upsert_cron_replaces(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO series (series, title, author) VALUES ('cron-replace', 'Cron', 'Author')"
            )
            db.upsert_cron(conn, "cron-replace", ["0 9 * * *"])
            db.upsert_cron(conn, "cron-replace", ["30 18 * * *"])
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT schedule FROM cron_schedule WHERE series='cron-replace'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["schedule"] == "30 18 * * *"

    def test_upsert_cron_preserves_retry_schedules(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO series (series, title, author) VALUES ('cron-retry', 'Cron', 'Author')"
            )
            conn.execute(
                "INSERT INTO cron_schedule(series,schedule,kind) VALUES('cron-retry','0 11 * * *','retry')"
            )
            db.upsert_cron(conn, "cron-retry", ["0 9 * * *"])
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT schedule, kind FROM cron_schedule WHERE series='cron-retry' ORDER BY kind"
            ).fetchall()
        assert rows == [("0 9 * * *", "primary"), ("0 11 * * *", "retry")]


def test_sync_policy_update_allows_denser_timer_wakeups(db_path: Path):
    from bilibili_podcast.services.sync_policy_service import SyncPolicyService

    with db.transaction(str(db_path)) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('policy','P','A')")
        conn.execute("INSERT INTO sync_policy(series,update_period) VALUES('policy','12h')")
        conn.execute("INSERT INTO cron_schedule(series,schedule,position) VALUES('policy','0 0 * * *',0)")
        conn.execute("INSERT INTO cron_schedule(series,schedule,position) VALUES('policy','0 12 * * *',1)")

    with db.transaction(str(db_path)) as conn:
        SyncPolicyService(conn).update_fields("policy", {"update_period": "24h"})

    with sqlite3.connect(str(db_path)) as conn:
        value = conn.execute(
            "SELECT update_period FROM sync_policy WHERE series='policy'"
        ).fetchone()[0]
    assert value == "24h"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"unknown_column": 1}, "unknown sync policy field"),
        ({"update_period": "0h"}, "invalid update_period"),
        ({"request_interval_seconds": float("nan")}, "invalid sync policy value"),
        ({"quality": "lossless"}, "invalid sync policy value"),
        ({"page_size": 0}, "invalid sync policy value"),
        ({"browser_wait_min_seconds": 9.0, "browser_wait_max_seconds": 8.0}, "exceeds"),
        ({"min_duration_seconds": 20, "max_duration_seconds": 10}, "exceeds"),
    ],
)
def test_sync_policy_rejects_invalid_updates_without_inserting_row(
    db_path: Path, updates: dict, message: str,
) -> None:
    from bilibili_podcast.services.sync_policy_service import SyncPolicyService

    with db.transaction(str(db_path)) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('invalid-policy','P','A')")
        with pytest.raises(ValueError, match=message):
            SyncPolicyService(conn).update_fields("invalid-policy", updates)
        assert conn.execute(
            "SELECT 1 FROM sync_policy WHERE series='invalid-policy'"
        ).fetchone() is None


# ── READ: load_series_configs ───────────────────────────────────────


class TestLoadSeriesConfigs:
    def test_loads_enabled_series_only(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            for name in ("enabled-1", "enabled-2", "disabled-1"):
                enabled = 0 if "disabled" in name else 1
                conn.execute(
                    "INSERT INTO series (series, enabled, title, author) VALUES (?, ?, ?, ?)",
                    (name, enabled, name, "Author"),
                )
                conn.execute(
                    "INSERT INTO series_source (series, uid, type) VALUES (?, 999, 'space')",
                    (name,),
                )
                conn.execute(
                    "INSERT INTO sync_policy (series) VALUES (?)", (name,),
                )
        configs = db.load_series_configs(str(db_path))
        assert len(configs) == 2
        assert {c.series for c in configs} == {"enabled-1", "enabled-2"}

    def test_loads_preserves_all_fields(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            db.upsert_source(conn, sample_config)
            db.upsert_sync_policy(conn, sample_config)
            db.upsert_filters(conn, sample_config)
            db.upsert_paid_preview(conn, sample_config)
        configs = db.load_series_configs(str(db_path))
        assert len(configs) == 1
        c = configs[0]
        assert c.series == "testseries"
        assert c.title == "测试系列"
        assert c.author == "测试作者"
        assert c.source["uid"] == 12345
        assert c.sync["quality"] == "64K"
        assert c.sync["page_size"] == 20
        assert c.filters["exclude_paid"] is True
        assert "BV1xx" in c.filters["exclude_bvids"]
        assert "BV2yy" in c.filters["exclude_bvids"]
        assert "BV3zz" in c.filters["advertisement_bvids"]
        assert c.filters["exclude_season_ids"] == [5492168]
        assert "spam" in c.filters["exclude_keywords"]
        assert "ad" in c.filters["advertisement_keywords"]
        assert c.paid_preview["enabled"] is False
        assert c.keep_last == 100

    def test_loads_empty_db(self, db_path: Path):
        configs = db.load_series_configs(str(db_path))
        assert configs == []

    def test_loads_all_configs_in_one_transaction(self, db_path: Path):
        from unittest.mock import patch

        with db.transaction(str(db_path)) as conn:
            for name in ("one", "two"):
                conn.execute(
                    "INSERT INTO series (series, enabled, title, author) VALUES (?, 1, ?, 'Author')",
                    (name, name),
                )

        with patch.object(db, "transaction", wraps=db.transaction) as transaction:
            configs = db.load_series_configs(str(db_path))

        assert {config.series for config in configs} == {"one", "two"}
        assert transaction.call_count == 1

    def test_roundtrip_via_config_store(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            db.upsert_source(conn, sample_config)
            db.upsert_sync_policy(conn, sample_config)
            db.upsert_filters(conn, sample_config)
            db.upsert_paid_preview(conn, sample_config)
        store = DbStore(db_path)
        configs = store.load_configs("testseries")
        assert len(configs) == 1
        assert configs[0].series == "testseries"


class TestSchedulerBackend:
    def test_defaults_to_cron_and_can_switch(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
            assert db.get_scheduler_backend(conn, "demo") == "cron"
            db.set_scheduler_backend(conn, "demo", "systemd")
            assert db.get_scheduler_backend(conn, "demo") == "systemd"
            db.set_scheduler_backend(conn, "demo", "cron")
            assert db.get_scheduler_backend(conn, "demo") == "cron"

    def test_rejects_unknown_backend(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
            with pytest.raises(ValueError, match="unsupported scheduler backend"):
                db.set_scheduler_backend(conn, "demo", "other")

    def test_set_backend_upgrades_database_missing_backend_table(self, db_path: Path):
        with db.transaction(str(db_path)) as conn:
            conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
            conn.execute("DROP TABLE scheduler_backend")
            db.set_scheduler_backend(conn, "demo", "systemd")
            assert db.get_scheduler_backend(conn, "demo") == "systemd"

# ── Sync State ──────────────────────────────────────────────────────


class TestSyncState:
    def test_read_write_state(self, db_path: Path, sample_config: SeriesConfig, state_data: dict):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
        db.write_state_file(str(db_path), "testseries", state_data)
        loaded = db.read_state_file(str(db_path), "testseries")
        assert loaded["last_attempt_at"] == 1700000000
        assert loaded["last_success_at"] == 1700000100

    def test_read_missing_series(self, db_path: Path):
        loaded = db.read_state_file(str(db_path), "nonexistent")
        assert loaded == {}

    def test_write_state_updates(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
        db.write_state_file(str(db_path), "testseries", {"last_attempt_at": 1})
        db.write_state_file(str(db_path), "testseries", {"last_attempt_at": 2, "last_success_at": 3})
        loaded = db.read_state_file(str(db_path), "testseries")
        assert loaded["last_attempt_at"] == 2
        assert loaded["last_success_at"] == 3


# ── ConfigStore ─────────────────────────────────────────────────────


class TestYamlStore:
    @staticmethod
    def _write_yaml_config(config_dir: Path, series: str) -> None:
        (config_dir / f"{series}.yaml").write_text(
            f"""
series: {series}
title: {series}
author: Demo Author
source:
  uid: 123456
""",
            encoding="utf-8",
        )

    def test_load_configs_filters_by_series(self, tmp_path: Path):
        config_dir = tmp_path / "series.d"
        config_dir.mkdir()
        self._write_yaml_config(config_dir, "demo-series")
        self._write_yaml_config(config_dir, "other-series")

        store = YamlStore(config_dir, tmp_path / "state")
        configs = store.load_configs("demo-series")
        assert len(configs) == 1
        assert configs[0].series == "demo-series"

    def test_load_configs_empty_filter_returns_all(self, tmp_path: Path):
        config_dir = tmp_path / "series.d"
        config_dir.mkdir()
        for series in ("demo-series", "other-series", "third-series"):
            self._write_yaml_config(config_dir, series)

        store = YamlStore(config_dir, tmp_path / "state")
        configs = store.load_configs(None)
        assert len(configs) == 3


class TestDbStore:
    def test_load_configs(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
            db.upsert_source(conn, sample_config)
            db.upsert_sync_policy(conn, sample_config)
        store = DbStore(db_path)
        configs = store.load_configs("testseries")
        assert len(configs) == 1
        assert configs[0].title == "测试系列"

    def test_empty_db_returns_empty_list(self, db_path: Path):
        store = DbStore(db_path)
        assert store.load_configs(None) == []

    def test_state_read_write(self, db_path: Path, sample_config: SeriesConfig):
        with db.transaction(str(db_path)) as conn:
            db.upsert_series(conn, sample_config)
        store = DbStore(db_path)
        assert store.read_state("testseries") == {}
        store.write_state("testseries", {"last_attempt_at": 42})
        assert store.read_state("testseries")["last_attempt_at"] == 42


class TestFromArgs:
    def test_db_when_config_db_provided(self, db_path: Path):
        store = from_args("configs/series.d", "/tmp/state", str(db_path))
        assert isinstance(store, DbStore)

    def test_yaml_when_no_config_db(self):
        store = from_args("configs/series.d", "/tmp/state", None)
        assert isinstance(store, YamlStore)


# ── filtered entries helper ────────────────────────────────────────


class TestListFilterEntries:
    def test_lists_all_filter_types(self):
        filters = {
            "exclude_paid": True,
            "exclude_bvids": ["BV1"],
            "advertisement_bvids": ["BV2"],
            "exclude_season_ids": [123456],
            "exclude_keywords": ["kw1"],
            "advertisement_keywords": ["kw2"],
            "include_keywords": ["kw3"],
        }
        entries = db.list_filter_entries(filters)
        assert ("exclude_paid", "true") in entries
        assert ("exclude_bvid", "BV1") in entries
        assert ("advertisement_bvid", "BV2") in entries
        assert ("exclude_season_id", "123456") in entries
        assert ("exclude_keyword", "kw1") in entries
        assert ("advertisement_keyword", "kw2") in entries
        assert ("include_keyword", "kw3") in entries

    def test_exclude_paid_false_writes_entry(self):
        entries = db.list_filter_entries({"exclude_paid": False})
        assert entries == [("exclude_paid", "false")]

    def test_empty_lists_omitted(self):
        entries = db.list_filter_entries({
            "exclude_paid": True,
            "exclude_bvids": [],
            "advertisement_bvids": [],
            "exclude_keywords": [],
            "advertisement_keywords": [],
            "include_keywords": [],
        })
        assert entries == [("exclude_paid", "true")]
