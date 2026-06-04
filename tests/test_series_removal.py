from pathlib import Path

from bilibili_podcast import db
from bilibili_podcast.services.series_removal_service import SeriesRemovalService


def _service(tmp_path: Path) -> tuple[SeriesRemovalService, Path]:
    db_path = tmp_path / "bilibili-podcast.db"
    db.migrate(db_path)
    service = SeriesRemovalService(
        db_path,
        media_root=tmp_path / "media",
        json_root=tmp_path / "json",
        rss_root=tmp_path / "rss",
        published_rss_root=tmp_path / "published",
        cron_script_dir=tmp_path / "auto",
        browser_user_data_root=tmp_path / "browser-profiles",
        users_conf=tmp_path / "rss-publish-users.conf",
    )
    return service, db_path


def _insert_series(db_path: Path, series: str, uid: int) -> None:
    with db.transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO series(series, title, author) VALUES(?, ?, ?)",
            (series, series.title(), "Author"),
        )
        conn.execute(
            "INSERT INTO series_source(series, type, uid) VALUES(?, 'space', ?)",
            (series, uid),
        )
        conn.execute("INSERT INTO sync_policy(series) VALUES(?)", (series,))
        conn.execute(
            "INSERT INTO filter_rule(series, rule_type, value) VALUES(?, 'exclude_keyword', 'x')",
            (series,),
        )
        conn.execute(
            "INSERT INTO cron_schedule(series, schedule) VALUES(?, '0 1 * * *')",
            (series,),
        )
        conn.execute("INSERT INTO sync_state(series) VALUES(?)", (series,))


def test_plan_counts_series_artifacts(tmp_path: Path) -> None:
    service, db_path = _service(tmp_path)
    _insert_series(db_path, "demo", 123)
    (tmp_path / "media" / "demo").mkdir(parents=True)
    (tmp_path / "media" / "demo" / "a.mp3").write_text("x")
    (tmp_path / "json" / "demo").mkdir(parents=True)
    (tmp_path / "json" / "demo" / "a.json").write_text("{}")
    (tmp_path / "rss").mkdir()
    (tmp_path / "rss" / "demo.xml").write_text("<rss/>")
    (tmp_path / "published" / "token").mkdir(parents=True)
    (tmp_path / "published" / "token" / "demo.xml").write_text("<rss/>")
    (tmp_path / "auto").mkdir()
    (tmp_path / "auto" / "run_demo_sync.sh").write_text("#!/bin/sh")
    (tmp_path / "browser-profiles" / "demo").mkdir(parents=True)
    (tmp_path / "browser-profiles" / "demo" / "Cookies").write_text("x")
    (tmp_path / "rss-publish-users.conf").write_text("token:demo,other\n")

    plan = service.plan("demo")

    assert plan.uid == 123
    assert plan.media_files == 1
    assert plan.json_files == 1
    assert plan.master_rss_exists is True
    assert len(plan.published_rss_files) == 1
    assert plan.wrapper_exists is True
    assert plan.browser_profile_files == 1
    assert plan.users_conf_references == 1


def test_remove_deletes_database_and_local_artifacts(tmp_path: Path) -> None:
    service, db_path = _service(tmp_path)
    _insert_series(db_path, "demo", 123)
    for path in (
        tmp_path / "media" / "demo" / "a.mp3",
        tmp_path / "json" / "demo" / "a.json",
        tmp_path / "rss" / "demo.xml",
        tmp_path / "published" / "token" / "demo.xml",
        tmp_path / "auto" / "run_demo_sync.sh",
        tmp_path / "browser-profiles" / "demo" / "Default" / "Cookies",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    users_conf = tmp_path / "rss-publish-users.conf"
    users_conf.write_text("token:demo,other\nonly:demo\nall-token:all\n")

    service.remove("demo")

    assert not (tmp_path / "media" / "demo").exists()
    assert not (tmp_path / "json" / "demo").exists()
    assert not (tmp_path / "rss" / "demo.xml").exists()
    assert not (tmp_path / "published" / "token" / "demo.xml").exists()
    assert not (tmp_path / "auto" / "run_demo_sync.sh").exists()
    assert not (tmp_path / "browser-profiles" / "demo").exists()
    assert users_conf.read_text() == "token:other\nall-token:all\n"
    with db.transaction(db_path) as conn:
        for table in (
            "series", "series_source", "sync_policy", "filter_rule",
            "cron_schedule", "sync_state",
        ):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE series='demo'"
            ).fetchone()[0] == 0


def test_list_series_for_uid_returns_all_matching_series(tmp_path: Path) -> None:
    service, db_path = _service(tmp_path)
    _insert_series(db_path, "one", 123)
    _insert_series(db_path, "two", 123)
    _insert_series(db_path, "other", 456)

    assert service.list_series_for_uid(123) == ["one", "two"]
