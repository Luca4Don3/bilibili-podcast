import logging
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bilibili_podcast import sync as sync_mod
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


def test_publish_script_argument_accepted() -> None:
    """--publish-script must be accepted by sync parser."""
    parser = build_parser()
    ns = parser.parse_args(["--publish-script", "/tmp/pub.sh"])
    assert ns.publish_script == "/tmp/pub.sh"


def test_publish_script_absent_by_default() -> None:
    """Without --publish-script, the attribute must be None."""
    parser = build_parser()
    ns = parser.parse_args([])
    assert ns.publish_script is None


def _sync_args(tmp_path, *, apply=True, publish_script="/tmp/publish.sh"):
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
        apply=apply,
        publish_script=publish_script,
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


def test_publish_script_runs_after_apply_success(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        return {"series": "synctest"}

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="published", stderr="")

    with patch.object(sync_mod, "make_store", return_value=_sync_store()), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series), \
            patch.object(sync_mod.subprocess, "run", side_effect=fake_run):
        rc = asyncio.run(sync_mod.run(_sync_args(tmp_path)))

    assert rc == 0
    assert calls == [["/tmp/publish.sh"]]


def test_publish_script_skipped_after_sync_error(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        raise RuntimeError("sync failed")

    with patch.object(sync_mod, "make_store", return_value=_sync_store()), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series), \
            patch.object(sync_mod.subprocess, "run") as run:
        rc = asyncio.run(sync_mod.run(_sync_args(tmp_path)))

    assert rc == EXIT_SYNC_ERROR
    run.assert_not_called()


def test_publish_script_failure_returns_nonzero(tmp_path) -> None:
    import asyncio

    async def fake_sync_series(**kwargs):
        return {"series": "synctest"}

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="publish error token=real-value",
        )

    with patch.object(sync_mod, "make_store", return_value=_sync_store()), \
            patch.object(sync_mod, "sync_series", side_effect=fake_sync_series), \
            patch.object(sync_mod.subprocess, "run", side_effect=fake_run), \
            patch.object(sync_mod.LOGGER, "error") as log_error:
        rc = asyncio.run(sync_mod.run(_sync_args(tmp_path)))

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
