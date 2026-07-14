"""Web configuration regression tests."""

from __future__ import annotations

import importlib
import inspect
import sys
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_web_server(monkeypatch, db_path: Path):
    name = "bilibili_podcast.web.server"
    if name in sys.modules:
        server = importlib.reload(sys.modules[name])
    else:
        server = importlib.import_module(name)
    server.DB_PATH = str(db_path)
    server.PASSWORD = "test-password"
    server._SECRET_KEY = server.hashlib.sha256(server.PASSWORD.encode()).hexdigest()
    server._serializer = server.URLSafeTimedSerializer(server._SECRET_KEY)
    root = db_path.parent
    server._CONFIG_SNAPSHOT = SimpleNamespace(
        root=root / "config",
        app=SimpleNamespace(paths=SimpleNamespace(
            media_root=root / "media", json_root=root / "json", rss_root=root / "rss",
            published_rss_root=root / "published-rss", log_dir=root / "logs",
        )),
        sync=SimpleNamespace(
            paths=SimpleNamespace(cookie_file=root / "cookie.txt", lock_file=root / "sync.lock"),
            browser=SimpleNamespace(user_data_root=root / "browser"),
            timeouts=SimpleNamespace(publish_seconds=60),
        ),
        scheduler=SimpleNamespace(command_timeout_seconds=30),
        publish=SimpleNamespace(publish=SimpleNamespace(
            enabled=False, media_base_url="https://media.example.invalid",
            master_placeholder="__MEDIA_PLACEHOLDER__", gone_series=("removed-series",),
        )),
        rss_users=SimpleNamespace(users={}),
    )
    return server


def test_rss_authorization_matrix_and_gone_series_are_local_only(monkeypatch, tmp_path: Path) -> None:
    import asyncio

    server = _load_web_server(monkeypatch, tmp_path / "web.db")
    matrix = {
        "user-a": tuple(f"series-{index:02d}" for index in range(1, 10)),
        "user-b": ("series-01", "series-02"),
        "user-c": ("series-01",),
        "user-d": ("series-01", "series-08", "series-04"),
    }
    users = {
        name: SimpleNamespace(token=f"test-token-{name}", series=series)
        for name, series in matrix.items()
    }
    server._CONFIG_SNAPSHOT.rss_users = SimpleNamespace(users=users)
    published = server._CONFIG_SNAPSHOT.app.paths.published_rss_root
    for user in users.values():
        root = published / "current" / hashlib.sha256(user.token.encode()).hexdigest()
        root.mkdir(parents=True, exist_ok=True)
        for series in user.series:
            (root / f"{series}.xml").write_text("<rss/>")

    all_series = set().union(*matrix.values())
    for name, user in users.items():
        for series in all_series:
            response = asyncio.run(server.authorize_rss(user.token, series))
            assert response.status_code == (204 if series in matrix[name] else 403)
    gone = asyncio.run(server.authorize_rss("test-token-user-a", "removed-series"))
    assert gone.status_code == 403
    assert gone.headers["X-RSS-Denial-Status"] == "410"
    assert asyncio.run(server.authorize_rss("invalid-token", "series-06")).status_code == 403

    server._CONFIG_SNAPSHOT.rss_users = SimpleNamespace(users={
        "all-user": SimpleNamespace(token="all-user-token", series=("all",)),
    })
    missing = asyncio.run(server.authorize_rss("all-user-token", "not-published"))
    assert missing.status_code == 204
    assert missing.headers["X-RSS-Token-Hash"] == hashlib.sha256(
        b"all-user-token"
    ).hexdigest()


def test_media_authorization_and_previous_cookie_refresh(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    from starlette.requests import Request
    from starlette.responses import Response

    server = _load_web_server(monkeypatch, tmp_path / "web.db")
    server._CONFIG_SNAPSHOT.rss_users = SimpleNamespace(users={
        "user": SimpleNamespace(token="test-token", series=("demo",)),
    })
    assert asyncio.run(server.authorize_media("test-token", "demo", "item.mp3")).status_code == 204
    assert asyncio.run(server.authorize_media("invalid", "demo", "item.mp3")).status_code == 403
    assert asyncio.run(server.authorize_media("test-token", "demo", "../item.mp3")).status_code == 404

    server._COOKIE_NAME = "bilibili_podcast_session"
    server._PREVIOUS_COOKIE_NAMES = ("previous_session",)
    old_token = server._session_token()
    request = Request({
        "type": "http", "method": "GET", "path": "/series",
        "headers": [(b"cookie", f"previous_session={old_token}".encode())],
    })
    assert server._get_session(request) == "auth"
    response = server._refresh_session_cookie(request, Response())
    cookie = response.headers["set-cookie"]
    assert "bilibili_podcast_session=" in cookie
    assert "HttpOnly" in cookie


def test_filters_form_can_disable_exclude_paid(monkeypatch, tmp_path: Path) -> None:
    from bilibili_podcast import db

    db_path = tmp_path / "web.db"
    db.migrate(str(db_path))
    with db.transaction(str(db_path)) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('webpaid','T','A')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('webpaid','space',1)")
        conn.execute("INSERT INTO sync_policy(series) VALUES('webpaid')")
        conn.execute(
            "INSERT INTO filter_rule(series,rule_type,value,enabled,position) "
            "VALUES('webpaid','exclude_paid','true',1,0)"
        )

    server = _load_web_server(monkeypatch, db_path)
    sig = inspect.signature(server.filters_update)
    assert sig.parameters["exclude_paid"].default.default is False

    import asyncio

    with patch.object(server, "_csrf_guard", return_value=None):
        response = asyncio.run(
            server.filters_update(
                request=object(),
                series="webpaid",
                csrf_token="csrf",
                exclude_paid=False,
                exclude_bvids="",
                advertisement_bvids="",
                exclude_keywords="",
                advertisement_keywords="",
                include_keywords="",
                exclude_season_ids="5492168",
                paid_preview_enabled=False,
                retry_after_days=4,
                min_duration_seconds=0,
                max_duration_seconds=0,
            )
        )
    assert response.status_code == 302

    with db.transaction(str(db_path)) as conn:
        row = conn.execute(
            "SELECT value, enabled FROM filter_rule "
            "WHERE series='webpaid' AND rule_type='exclude_paid'"
        ).fetchone()
    assert row is not None
    assert row["value"] == "false"
    assert row["enabled"] == 1
    with db.transaction(str(db_path)) as conn:
        season = conn.execute(
            "SELECT value FROM filter_rule "
            "WHERE series='webpaid' AND rule_type='exclude_season_id'"
        ).fetchone()
    assert season["value"] == "5492168"


def test_preview_template_preserves_zero_keep_last() -> None:
    template = Path("src/bilibili_podcast/web/templates/preview.html").read_text()
    assert "sync.keep_last or 100" not in template
    assert "sync.keep_last if sync.keep_last is not none else 100" in template


def test_series_list_renders_after_login(monkeypatch, tmp_path: Path) -> None:
    import asyncio

    from bilibili_podcast import db

    db_path = tmp_path / "web.db"
    db.migrate(str(db_path))
    with db.transaction(str(db_path)) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")

    server = _load_web_server(monkeypatch, db_path)
    rendered = object()
    with patch.object(server, "_login_required", return_value=None), \
            patch.object(server.templates, "TemplateResponse", return_value=rendered) as template:
        response = asyncio.run(server.series_list(request=object()))

    assert response is rendered
    context = template.call_args.args[2]
    assert context["series_list"][0]["series"] == "demo"


def test_series_new_create_rejects_duplicate_without_overwrite(monkeypatch, tmp_path: Path) -> None:
    import asyncio

    from bilibili_podcast import db

    db_path = tmp_path / "web.db"
    db.migrate(str(db_path))
    with db.transaction(str(db_path)) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Original','A')")

    server = _load_web_server(monkeypatch, db_path)
    with patch.object(server, "_csrf_guard", return_value=None):
        response = asyncio.run(server.series_new_create(
            request=object(),
            series="demo",
            title="Replacement",
            description="",
            author="B",
            cover_art="",
            category="",
            subcategories="",
            lang="zh-CN",
            explicit=False,
            space_url="",
            uid=1,
            source_type="space",
            sid=0,
            csrf_value="csrf",
        ))

    assert response.status_code == 400
    with db.transaction(str(db_path)) as conn:
        row = conn.execute("SELECT title, author FROM series WHERE series='demo'").fetchone()
    assert row["title"] == "Original"
    assert row["author"] == "A"


def test_manual_media_attach_rebuilds_without_legacy_publish_env(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    import json
    from unittest.mock import patch
    from bilibili_podcast import db

    db_path = tmp_path / "web.db"
    media_root = tmp_path / "media"
    json_root = tmp_path / "json"
    rss_root = tmp_path / "rss"
    allow_dir = tmp_path / "manual"
    allow_dir.mkdir()
    src = allow_dir / "source.mp3"
    src.write_text("audio")
    publish_script = tmp_path / "publish.sh"

    config = SimpleNamespace(
        app=SimpleNamespace(paths=SimpleNamespace(
            media_root=media_root, json_root=json_root, rss_root=rss_root,
        )),
        publish=SimpleNamespace(publish=SimpleNamespace(
            enabled=False, media_base_url="http://media.test", script=None,
        )),
        manual_media=SimpleNamespace(
            enabled=True, allowed_dirs=(allow_dir,), follow_symlinks=False,
        ),
        sync=SimpleNamespace(timeouts=SimpleNamespace(publish_seconds=60)),
    )
    monkeypatch.setenv("BILIBILI_PODCAST_RSS_PUBLISH", str(publish_script))

    db.migrate(str(db_path))
    with db.transaction(str(db_path)) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('paidweb','PaidWeb','A')")
        conn.execute(
            "INSERT INTO series_source(series,space_url,type,uid) "
            "VALUES('paidweb','https://space.bilibili.com/1','space',1)"
        )
        conn.execute("INSERT INTO sync_policy(series,quality) VALUES('paidweb','64K')")

    bvid = "BV1234567890"
    (json_root / "paidweb").mkdir(parents=True)
    (json_root / "paidweb" / f"{bvid}_64K.info.json").write_text(json.dumps({
        "id": bvid,
        "title": "Manual paid item",
        "timestamp": 1_800_000_000,
        "duration": 300,
        "webpage_url": f"https://www.bilibili.com/video/{bvid}",
    }))

    server = _load_web_server(monkeypatch, db_path)
    server._CONFIG_SNAPSHOT = config
    server._cli_admin._CONFIG = config
    with patch.object(server, "_csrf_guard", return_value=None), \
            patch.object(server.subprocess, "run") as run:
        response = asyncio.run(
            server.manual_media_attach(
                request=object(),
                series="paidweb",
                csrf_token="csrf",
                bvid=bvid,
                server_path=str(src),
                replace="",
            )
        )

    assert response.status_code == 302
    assert "success=" in response.headers["location"]
    assert (media_root / "paidweb" / f"{bvid}_64K.mp3").exists()
    rss = (rss_root / "paidweb.xml").read_text()
    assert "Manual paid item" in rss
    assert f"{bvid}_64K.mp3?token=__MEDIA_PLACEHOLDER__" in rss
    run.assert_not_called()
    assert not list(tmp_path.rglob("*.backup-*"))

    src.write_text("replacement")
    original_rss = (rss_root / "paidweb.xml").read_text()
    with patch.object(server, "_csrf_guard", return_value=None), patch.object(
        server, "rebuild_paid_rss", side_effect=ValueError("rebuild failed")
    ):
        failed = asyncio.run(
            server.manual_media_attach(
                request=object(), series="paidweb", csrf_token="csrf", bvid=bvid,
                server_path=str(src), replace="1",
            )
        )
    assert "error=" in failed.headers["location"]
    assert (media_root / "paidweb" / f"{bvid}_64K.mp3").read_text() == "audio"
    assert (rss_root / "paidweb.xml").read_text() == original_rss
    assert not list(tmp_path.rglob("*.backup-*"))


def test_manual_media_attach_rejects_unknown_series(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    import pytest
    from unittest.mock import patch
    from fastapi import HTTPException
    from bilibili_podcast import db

    db_path = tmp_path / "web.db"
    db.migrate(str(db_path))
    server = _load_web_server(monkeypatch, db_path)

    with patch.object(server, "_csrf_guard", return_value=None):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                server.manual_media_attach(
                    request=object(),
                    series="missing",
                    csrf_token="csrf",
                    bvid="BV1234567890",
                    server_path=str(tmp_path / "source.mp3"),
                    replace="",
                )
            )
    assert exc.value.status_code == 404


def test_resolve_empty_url_renders_error_without_csrf_shadow(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    from bilibili_podcast import db

    db_path = tmp_path / "web.db"
    db.migrate(str(db_path))
    server = _load_web_server(monkeypatch, db_path)
    rendered = object()

    with patch.object(server, "_csrf_guard", return_value=None), \
            patch.object(server.templates, "TemplateResponse", return_value=rendered) as template:
        response = asyncio.run(server.resolve_url(
            request=object(),
            url="",
            csrf_value="csrf",
        ))

    assert response is rendered
    context = template.call_args.args[2]
    assert context["error"]
    assert context["csrf_token"]


def test_resolve_success_renders_result_without_csrf_shadow(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    from bilibili_podcast import db

    db_path = tmp_path / "web.db"
    db.migrate(str(db_path))
    server = _load_web_server(monkeypatch, db_path)
    rendered = object()

    async def fake_resolve(url: str) -> dict:
        return {"series": "demo", "title": "Demo"}

    with patch.object(server, "_csrf_guard", return_value=None), \
            patch("bilibili_podcast.web.resolver.resolve_url", side_effect=fake_resolve), \
            patch.object(server.templates, "TemplateResponse", return_value=rendered) as template:
        response = asyncio.run(server.resolve_url(
            request=object(),
            url="https://space.bilibili.com/1",
            csrf_value="csrf",
        ))

    assert response is rendered
    context = template.call_args.args[2]
    assert context["result"]["series"] == "demo"
    assert context["csrf_token"]


def test_series_update_empty_title_renders_error_without_csrf_shadow(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    from bilibili_podcast import db

    db_path = tmp_path / "web.db"
    db.migrate(str(db_path))
    with db.transaction(str(db_path)) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")

    server = _load_web_server(monkeypatch, db_path)
    rendered = object()
    with patch.object(server, "_csrf_guard", return_value=None), \
            patch.object(server.templates, "TemplateResponse", return_value=rendered) as template:
        response = asyncio.run(server.series_update(
            request=object(),
            series="demo",
            title="",
            description="",
            author="",
            cover_art="",
            category="",
            subcategories="",
            uid=0,
            source_type="space",
            sid=0,
            csrf_value="csrf",
        ))

    assert response is rendered
    context = template.call_args.args[2]
    assert "标题不能为空" in context["errors"]
    assert context["csrf_token"]


def test_preview_run_renders_output_without_csrf_shadow(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    from bilibili_podcast import db
    from bilibili_podcast.services.scheduler_service import SchedulerCommandResult

    db_path = tmp_path / "web.db"
    db.migrate(str(db_path))
    with db.transaction(str(db_path)) as conn:
        conn.execute("INSERT INTO series(series,title,author) VALUES('demo','Demo','A')")
        conn.execute("INSERT INTO series_source(series,type,uid) VALUES('demo','space',1)")
        conn.execute("INSERT INTO sync_policy(series) VALUES('demo')")

    server = _load_web_server(monkeypatch, db_path)
    rendered = object()
    preview_result = SchedulerCommandResult("preview", "dry-run", 0, "ok", "")

    with patch.object(server, "_csrf_guard", return_value=None), \
            patch.object(server.PreviewService, "run_preview", return_value=preview_result), \
            patch.object(server.templates, "TemplateResponse", return_value=rendered) as template:
        response = asyncio.run(server.preview_run(
            request=object(),
            series="demo",
            csrf_value="csrf",
        ))

    assert response is rendered
    context = template.call_args.args[2]
    assert "ok" in context["dry_run_output"]
    assert context["csrf_token"]
