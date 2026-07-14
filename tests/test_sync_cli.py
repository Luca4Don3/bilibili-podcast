import logging
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bilibili_podcast import sync as sync_mod
from bilibili_podcast.publisher import PublishError
from bilibili_podcast.sync import (
    EXIT_PUBLISH_ERROR,
    EXIT_SYNC_ERROR,
    LOGGER,
    build_parser,
    cleanup_old_log_backups,
    sanitize_external_output,
    setup_logging,
)
from bilibili_podcast.utils.series_config import SeriesConfig


def test_log_level_accepts_standard_levels_case_insensitive() -> None:
    parser = build_parser()

    assert parser.parse_args(["--log-level", "debug"]).log_level == "DEBUG"
    assert parser.parse_args(["--log-level", "INFO"]).log_level == "INFO"
    assert parser.parse_args(["--log-level", "warning"]).log_level == "WARNING"
    assert parser.parse_args(["--log-level", "error"]).log_level == "ERROR"
    assert parser.parse_args(["--log-level", "critical"]).log_level == "CRITICAL"


def test_debug_flag_remains_available() -> None:
    parser = build_parser()

    args = parser.parse_args(["--log-level", "ERROR", "--debug"])
    assert args.log_level == "ERROR"
    assert args.debug is True


def test_setup_logging_keeps_legacy_debug_positional(tmp_path) -> None:
    setup_logging(str(tmp_path), True)

    assert LOGGER.level == logging.DEBUG


def test_cleanup_old_log_backups_only_removes_expired_recognized_files(tmp_path) -> None:
    now = 2_000_000_000.0
    expired = tmp_path / "sync.log.1"
    recent = tmp_path / "sync.error.log.1"
    active = tmp_path / "playwright.log"
    unrelated = tmp_path / "other.log.1"
    for path in (expired, recent, active, unrelated):
        path.write_text("log")
    os.utime(expired, (now - 31 * 86400, now - 31 * 86400))
    os.utime(recent, (now - 29 * 86400, now - 29 * 86400))
    os.utime(active, (now - 31 * 86400, now - 31 * 86400))
    os.utime(unrelated, (now - 31 * 86400, now - 31 * 86400))
    link = tmp_path / "playwright.log.1"
    link.symlink_to(expired)

    removed = cleanup_old_log_backups(tmp_path, now=now)

    assert removed == 1
    assert not expired.exists()
    assert recent.exists()
    assert active.exists()
    assert unrelated.exists()
    assert link.is_symlink()


def test_external_publish_script_argument_is_rejected() -> None:
    parser = build_parser()
    with __import__("pytest").raises(SystemExit):
        parser.parse_args(["--publish-script", "/tmp/pub.sh"])


def test_real_media_token_is_rejected_by_sync_parser() -> None:
    parser = build_parser()
    with __import__("pytest").raises(SystemExit):
        parser.parse_args(["--token", "real-token-value"])
    assert parser.parse_args([]).token == "__MEDIA_PLACEHOLDER__"


def _sync_args(tmp_path, *, apply=True, publisher_snapshot=None, scheduled_retry=False):
    return SimpleNamespace(
        config_dir="configs/series.d",
        config_db=None,
        series="synctest",
        cookie_file=None,
        token="__MEDIA_PLACEHOLDER__",
        media_root=str(tmp_path / "media"),
        json_root=str(tmp_path / "json"),
        rss_root=str(tmp_path / "rss"),
        media_base_url="http://test:8080",
        lock_file=str(tmp_path / "sync.lock"),
        state_root=str(tmp_path / "state"),
        max_downloads_per_run=1,
        min_free_gb=5,
        browser_fallback=False,
        browser_user_data_root=str(tmp_path / "browser"),
        browser_login_check=False,
        browser_login_wait_seconds=5,
        log_dir=str(tmp_path / "logs"),
        log_level="INFO",
        debug=False,
        force=False,
        scheduled_retry=scheduled_retry,
        apply=apply,
        publisher_snapshot=publisher_snapshot,
    )


def _sync_config() -> SeriesConfig:
    return SeriesConfig(
        series="synctest", enabled=True, title="S", description="",
        author="A", cover_art="", category="", subcategories=[],
        explicit=False, lang="zh-CN", source={"uid": 1, "space_url": ""},
        sync={"quality": "64K"}, filters={}, paid_preview={}, keep_last=0,
    )


def _sync_store() -> MagicMock:
    store = MagicMock()
    store.load_configs.return_value = [_sync_config()]
    store.read_state.return_value = {}
    return store


def test_builtin_publisher_runs_after_apply_success(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        return {"series": "synctest"}

    marker = object()
    with patch.object(sync_mod, "make_store", return_value=_sync_store()), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series), \
            patch("bilibili_podcast.publisher.publish", return_value="generation") as publisher:
        rc = asyncio.run(sync_mod.run(_sync_args(tmp_path, publisher_snapshot=marker)))

    assert rc == 0
    publisher.assert_called_once_with(marker)


def test_builtin_publisher_skipped_after_sync_error(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        raise RuntimeError("sync failed")

    with patch.object(sync_mod, "make_store", return_value=_sync_store()), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series), \
            patch("bilibili_podcast.publisher.publish") as publisher:
        rc = asyncio.run(sync_mod.run(_sync_args(tmp_path, publisher_snapshot=object())))

    assert rc == EXIT_SYNC_ERROR
    publisher.assert_not_called()


def test_scheduled_retry_not_needed_skips_sync_and_publish(tmp_path) -> None:
    import asyncio

    store = _sync_store()
    store.read_state.return_value = {"retry_pending": False}
    with patch.object(sync_mod, "make_store", return_value=store), \
            patch.object(sync_mod, "sync_series") as sync_series, \
            patch.object(sync_mod.subprocess, "run") as run:
        rc = asyncio.run(sync_mod.run(_sync_args(tmp_path, scheduled_retry=True)))

    assert rc == 0
    sync_series.assert_not_called()
    run.assert_not_called()


def test_scheduled_retry_consumes_pending_before_request(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        return {"series": "synctest"}

    store = _sync_store()
    store.read_state.return_value = {
        "retry_pending": True,
        "last_success_at": sync_mod.now_timestamp(),
    }
    with patch.object(sync_mod, "make_store", return_value=store), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series):
        rc = asyncio.run(sync_mod.run(_sync_args(
            tmp_path, scheduled_retry=True, publisher_snapshot=None,
        )))

    assert rc == 0
    first_state = store.write_state.call_args_list[0].args[1]
    assert first_state["retry_pending"] is False


def test_rate_limit_does_not_consume_scheduled_retry(tmp_path) -> None:
    import asyncio

    store = _sync_store()
    store.read_state.return_value = {
        "retry_pending": True,
        "rate_limited_until": sync_mod.now_timestamp() + 3600,
    }
    with patch.object(sync_mod, "make_store", return_value=store), \
            patch.object(sync_mod, "sync_series") as sync_series:
        rc = asyncio.run(sync_mod.run(_sync_args(
            tmp_path, scheduled_retry=True, publisher_snapshot=None,
        )))

    assert rc == 0
    sync_series.assert_not_called()
    store.write_state.assert_not_called()


def test_failed_scheduled_retry_remains_consumed(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        raise RuntimeError("retry failed")

    store = _sync_store()
    store.read_state.return_value = {"retry_pending": True}
    with patch.object(sync_mod, "make_store", return_value=store), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series):
        rc = asyncio.run(sync_mod.run(_sync_args(
            tmp_path, scheduled_retry=True, publisher_snapshot=None,
        )))

    assert rc == EXIT_SYNC_ERROR
    assert all(
        call.args[1]["retry_pending"] is False
        for call in store.write_state.call_args_list
    )


def test_primary_failure_sets_retry_pending(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        raise RuntimeError("primary failed")

    store = _sync_store()
    with patch.object(sync_mod, "make_store", return_value=store), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series):
        rc = asyncio.run(sync_mod.run(_sync_args(tmp_path, publisher_snapshot=None)))

    assert rc == EXIT_SYNC_ERROR
    assert store.write_state.call_args.args[1]["retry_pending"] is True


def test_builtin_publisher_failure_returns_nonzero(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        return {"series": "synctest"}

    with patch.object(sync_mod, "make_store", return_value=_sync_store()), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series), \
            patch("bilibili_podcast.publisher.publish", side_effect=PublishError("publish error token=real-value")), \
            patch.object(sync_mod.LOGGER, "error") as log_error:
        rc = asyncio.run(sync_mod.run(_sync_args(tmp_path, publisher_snapshot=object())))

    assert rc == EXIT_PUBLISH_ERROR
    logged = " ".join(str(arg) for arg in log_error.call_args.args)
    assert "publish error" in logged
    assert "real-value" not in logged


def test_publish_failure_output_is_redacted_and_bounded() -> None:
    output = "x" * 5000 + " token=real-value Authorization:Bearer secret-value"

    sanitized = sanitize_external_output(output)

    assert len(sanitized) <= sync_mod.MAX_PUBLISH_ERROR_CHARS
    assert "real-value" not in sanitized
    assert "secret-value" not in sanitized
