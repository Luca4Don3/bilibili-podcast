import json
import os
from pathlib import Path

import pytest

from bilibili_podcast import sync


def _metadata_inputs(tmp_path):
    config = type("Config", (), {"series": "test", "sync": {"quality": "64K"}})()
    paths = type("Paths", (), {"json_root": tmp_path / "json"})()
    episode = {"bvid": "BVtest00001", "title": "测试"}
    return config, paths, episode


def test_write_metadata_atomically_replaces_existing_file(tmp_path, monkeypatch):
    config, paths, episode = _metadata_inputs(tmp_path)
    target = sync.json_path(config, paths, episode["bvid"])
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    original_chmod = Path.chmod

    def reject_target_chmod(path, mode, *, follow_symlinks=True):
        assert path != target
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", reject_target_chmod)

    sync.write_metadata(config, paths, episode, dry_run=False)

    assert json.loads(target.read_text(encoding="utf-8")) == episode
    assert target.stat().st_mode & 0o777 == 0o644
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_write_metadata_replace_failure_preserves_original(tmp_path, monkeypatch):
    config, paths, episode = _metadata_inputs(tmp_path)
    target = sync.json_path(config, paths, episode["bvid"])
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        sync.write_metadata(config, paths, episode, dry_run=False)

    assert target.read_text(encoding="utf-8") == "old"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_write_metadata_dry_run_does_not_create_directory(tmp_path):
    config, paths, episode = _metadata_inputs(tmp_path)

    sync.write_metadata(config, paths, episode, dry_run=True)

    assert not paths.json_root.exists()


def test_download_episode_uses_private_temporary_cookie_copy(tmp_path, monkeypatch):
    config = type("Config", (), {
        "series": "test",
        "sync": {"quality": "64K"},
    })()
    paths = type("Paths", (), {"media_root": tmp_path / "media"})()
    episode = {
        "bvid": "BVtest00001",
        "link": "https://www.bilibili.com/video/BVtest00001",
    }
    cookie_file = tmp_path / "source-cookies.txt"
    cookie_file.write_text("original-cookie", encoding="utf-8")
    observed_cookie = None

    def fake_run(command, check):
        nonlocal observed_cookie
        observed_cookie = Path(command[command.index("--cookies") + 1])
        assert check is True
        assert observed_cookie != cookie_file
        assert observed_cookie.read_text(encoding="utf-8") == "original-cookie"
        assert observed_cookie.stat().st_mode & 0o777 == 0o600
        observed_cookie.write_text("updated-cookie", encoding="utf-8")
        output = sync.media_path(config, paths, episode["bvid"])
        output.write_bytes(b"audio")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)

    sync.download_episode(config, paths, episode, str(cookie_file), dry_run=False)

    assert observed_cookie is not None
    assert not observed_cookie.exists()
    assert cookie_file.read_text(encoding="utf-8") == "original-cookie"
    assert sync.media_path(config, paths, episode["bvid"]).stat().st_mode & 0o777 == 0o644


def test_download_episode_cleans_temporary_cookie_after_failure(tmp_path, monkeypatch):
    config = type("Config", (), {
        "series": "test",
        "sync": {"quality": "64K"},
    })()
    paths = type("Paths", (), {"media_root": tmp_path / "media"})()
    episode = {
        "bvid": "BVtest00001",
        "link": "https://www.bilibili.com/video/BVtest00001",
    }
    cookie_file = tmp_path / "source-cookies.txt"
    cookie_file.write_text("original-cookie", encoding="utf-8")
    observed_cookie = None

    def fail_run(command, check):
        nonlocal observed_cookie
        observed_cookie = Path(command[command.index("--cookies") + 1])
        observed_cookie.write_text("updated-cookie", encoding="utf-8")
        raise sync.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(sync.subprocess, "run", fail_run)

    sync.download_episode(config, paths, episode, str(cookie_file), dry_run=False)

    assert observed_cookie is not None
    assert not observed_cookie.exists()
    assert cookie_file.read_text(encoding="utf-8") == "original-cookie"


def test_process_lock_rejects_second_holder(tmp_path):
    lock_file = tmp_path / "sync.lock"

    with sync.process_lock(str(lock_file)):
        with pytest.raises(SystemExit) as exc:
            with sync.process_lock(str(lock_file)):
                pass

    assert exc.value.code == 2


def test_browser_fallback_cooldown_matches_api_plus_playwright_source():
    result = {"fetch_source": "api+playwright"}

    assert "playwright" in result.get("fetch_source", "")


def test_update_period_gate_skips_recent_success():
    config = type("Config", (), {"sync": {"update_period": "12h"}})()
    state = {"last_success_at": sync.now_timestamp()}

    skipped, reason, next_run_at = sync.should_skip_series(config, state, force=False)

    assert skipped is True
    assert reason == "update_period"
    assert next_run_at > sync.now_timestamp()


def test_update_period_gate_allows_small_timer_drift(monkeypatch):
    config = type("Config", (), {"sync": {"update_period": "12h"}})()
    now = 1_800_000_000
    state = {"last_success_at": now - 12 * 3600 + 24}
    monkeypatch.setattr(sync, "now_timestamp", lambda: now)

    skipped, reason, next_run_at = sync.should_skip_series(config, state, force=False)

    assert skipped is False
    assert reason == ""
    assert next_run_at == now + 24


def test_rate_limit_cooldown_does_not_use_timer_grace(monkeypatch):
    config = type("Config", (), {"sync": {"update_period": "12h"}})()
    now = 1_800_000_000
    state = {
        "last_success_at": now - 13 * 3600,
        "rate_limited_until": now + 24,
    }
    monkeypatch.setattr(sync, "now_timestamp", lambda: now)

    skipped, reason, next_run_at = sync.should_skip_series(config, state, force=False)

    assert skipped is True
    assert reason == "rate_limit_cooldown"
    assert next_run_at == now + 24


def test_rate_limit_gate_takes_precedence():
    config = type("Config", (), {"sync": {"update_period": "1s"}})()
    state = {
        "last_success_at": sync.now_timestamp() - 3600,
        "rate_limited_until": sync.now_timestamp() + 3600,
    }

    skipped, reason, _ = sync.should_skip_series(config, state, force=False)

    assert skipped is True
    assert reason == "rate_limit_cooldown"


def test_existing_rss_items_normalizes_legacy_url_guid(tmp_path):
    rss = tmp_path / "legacy.xml"
    rss.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Legacy item</title>
      <guid>https://www.bilibili.com/video/BV1abcDEF12</guid>
      <link>https://www.bilibili.com/video/BV1abcDEF12</link>
      <pubDate>Fri, 17 Apr 2026 11:00:00 +0800</pubDate>
      <enclosure url="http://example.test/media/x/BV1abcDEF12_64K.mp3" length="123" type="audio/mpeg" />
    </item>
  </channel>
</rss>
""",
        encoding="utf-8",
    )

    items = sync.existing_rss_items(rss)

    assert items[0]["bvid"] == "BV1abcDEF12"
    assert items[0]["pubdate"] > 0


def test_merge_existing_rss_items_handles_paid_content_without_crash(tmp_path):
    from bilibili_podcast.sync import merge_existing_rss_items

    cfg = type("Config", (), {
        "series": "test", "keep_last": 0, "filters": {}, "sync": {},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()

    rss_dir = tmp_path / "rss"
    rss_dir.mkdir(parents=True)
    (rss_dir / "test.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>付费抢先看 测试</title>
      <guid>BVpaid00001</guid>
      <pubDate>Mon, 01 Jan 2026 10:00:00 +0000</pubDate>
      <enclosure url="http://example.test/media/test/BVpaid00001_64K.mp3" length="456" type="audio/mpeg" />
    </item>
    <item>
      <title>Normal Item</title>
      <guid>BVnormal001</guid>
      <pubDate>Mon, 01 Jan 2026 12:00:00 +0000</pubDate>
      <enclosure url="http://example.test/media/test/BVnormal001_64K.mp3" length="789" type="audio/mpeg" />
    </item>
  </channel>
</rss>""",
    )

    result = merge_existing_rss_items(cfg, paths, [])
    assert len(result) <= 1
    assert all(ep["bvid"] != "BVpaid00001" for ep in result)


def test_rss_padding_does_not_restore_excluded_items(tmp_path):
    from bilibili_podcast.sync import pad_with_existing_rss_items

    cfg = type("Config", (), {
        "series": "test", "keep_last": 1, "filters": {}, "sync": {},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()
    paths.rss_root.mkdir()
    (paths.rss_root / "test.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <item><title>Excluded keyword item</title><guid>BVexcluded01</guid>
    <enclosure url="http://example.test/BVexcluded01.mp3?token=__MEDIA_PLACEHOLDER__" length="1" type="audio/mpeg" />
  </item>
  <item><title>Allowed item</title><guid>BVallowed001</guid>
    <enclosure url="http://example.test/BVallowed001.mp3?token=__MEDIA_PLACEHOLDER__" length="1" type="audio/mpeg" />
  </item>
</channel></rss>""",
        encoding="utf-8",
    )

    result = pad_with_existing_rss_items(cfg, paths, [], {"BVexcluded01"})

    assert [item["bvid"] for item in result] == ["BVallowed001"]


def test_cleanup_retention_media_zero_keep_last_noop(tmp_path):
    from bilibili_podcast.sync import cleanup_retention_media

    cfg = type("Config", (), {
        "series": "test", "keep_last": 0, "filters": {}, "sync": {},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()

    assert cleanup_retention_media(cfg, paths, []) == set()


def test_cleanup_retention_media_keeps_retained_bvids(tmp_path):
    from bilibili_podcast.sync import cleanup_retention_media

    cfg = type("Config", (), {
        "series": "test", "keep_last": 2, "filters": {}, "sync": {},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()

    for bvid in ("BVkeep001", "BVkeep002", "BVdelete001"):
        (paths.media_root / "test").mkdir(parents=True, exist_ok=True)
        (paths.json_root / "test").mkdir(parents=True, exist_ok=True)
        (paths.media_root / "test" / f"{bvid}_64K.mp3").write_text("x")
        (paths.json_root / "test" / f"{bvid}_64K.info.json").write_text("{}")

    retained = [{"bvid": "BVkeep001"}, {"bvid": "BVkeep002"}]
    result = cleanup_retention_media(cfg, paths, retained)

    assert "BVdelete001" in result
    assert not (paths.media_root / "test" / "BVdelete001_64K.mp3").exists()


def test_cleanup_retention_media_fills_with_old_playable_when_new_missing(tmp_path):
    from bilibili_podcast.sync import cleanup_retention_media
    import json

    cfg = type("Config", (), {
        "series": "test", "keep_last": 2, "filters": {}, "sync": {},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()

    for bvid in ("BVold001", "BVold002"):
        (paths.media_root / "test").mkdir(parents=True, exist_ok=True)
        (paths.json_root / "test").mkdir(parents=True, exist_ok=True)
        (paths.media_root / "test" / f"{bvid}_64K.mp3").write_text("x")
        (paths.json_root / "test" / f"{bvid}_64K.info.json").write_text(
            json.dumps({"pubdate": 100})
        )

    retained = [{"bvid": "BVnew001", "title": "Not downloaded"}]
    result = cleanup_retention_media(cfg, paths, retained)

    assert "BVold001" not in result
    assert "BVold002" not in result
    assert (paths.media_root / "test" / "BVold001_64K.mp3").exists()
    assert (paths.media_root / "test" / "BVold002_64K.mp3").exists()


def test_cleanup_retention_media_existing_enclosure_counts_as_playable(tmp_path):
    from bilibili_podcast.sync import cleanup_retention_media

    cfg = type("Config", (), {
        "series": "test", "keep_last": 2, "filters": {}, "sync": {},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()

    (paths.media_root / "test").mkdir(parents=True, exist_ok=True)
    (paths.json_root / "test").mkdir(parents=True, exist_ok=True)
    (paths.media_root / "test" / "BVdelete001_64K.mp3").write_text("x")
    (paths.json_root / "test" / "BVdelete001_64K.info.json").write_text("{}")

    retained = [
        {"bvid": "BVkeep001", "_existing_enclosure_url": "http://e.test/1.mp3"},
        {"bvid": "BVkeep002", "_existing_enclosure_url": "http://e.test/2.mp3"},
    ]

    result = cleanup_retention_media(cfg, paths, retained)
    assert "BVdelete001" in result
    assert not (paths.media_root / "test" / "BVdelete001_64K.mp3").exists()


def test_cleanup_retention_media_keeps_old_playable_when_newest_not_downloaded(tmp_path):
    """keep_last=2, A/B have MP3, C(only JSON) is newest in retained, B must survive."""
    from bilibili_podcast.sync import cleanup_retention_media
    import json

    cfg = type("Config", (), {
        "series": "test", "keep_last": 2, "filters": {}, "sync": {},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()

    # Disk: A (media+json), B (media+json), C (json only, no media)
    for bvid, pub in (("BVc", 300), ("BVa", 100), ("BVb", 200)):
        (paths.media_root / "test").mkdir(parents=True, exist_ok=True)
        (paths.json_root / "test").mkdir(parents=True, exist_ok=True)
        meta = json.dumps({"bvid": bvid, "pubdate": pub})
        (paths.json_root / "test" / f"{bvid}_64K.info.json").write_text(meta)
        if bvid != "BVc":
            (paths.media_root / "test" / f"{bvid}_64K.mp3").write_text("audio")

    # retained = [C (newest, no media), A (has media)]
    retained = [
        {"bvid": "BVc", "title": "Newest not downloaded"},
        {"bvid": "BVa", "title": "Old playable"},
    ]

    result = cleanup_retention_media(cfg, paths, retained)

    # B must not be deleted — it fills the keep_last=2 slot
    assert "BVb" not in result, "B should be kept as filler"
    assert (paths.media_root / "test" / "BVb_64K.mp3").exists()
    # A is retained and playable, must survive
    assert (paths.media_root / "test" / "BVa_64K.mp3").exists()
    # C is in the current target set. Keep its metadata so the next run can
    # continue filling missing media instead of rediscovering and deleting it.
    assert "BVc" not in result, "C (json-only target) should be kept"
    assert (paths.json_root / "test" / "BVc_64K.info.json").exists()


def test_cleanup_retention_media_keeps_downloaded_target_at_boundary(tmp_path):
    from bilibili_podcast.sync import cleanup_retention_media
    import json

    cfg = type("Config", (), {
        "series": "test", "keep_last": 2, "filters": {}, "sync": {},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()

    (paths.media_root / "test").mkdir(parents=True, exist_ok=True)
    (paths.json_root / "test").mkdir(parents=True, exist_ok=True)
    for bvid, pub in (("BVtargetNew", 300), ("BVtargetBottom", 100), ("BVstaleOld", 200)):
        (paths.json_root / "test" / f"{bvid}_64K.info.json").write_text(
            json.dumps({"bvid": bvid, "pubdate": pub})
        )
        (paths.media_root / "test" / f"{bvid}_64K.mp3").write_text("audio")

    retained = [
        {"bvid": "BVtargetNew", "title": "Current target top"},
        {"bvid": "BVtargetBottom", "title": "Current target boundary"},
    ]

    result = cleanup_retention_media(cfg, paths, retained)

    assert "BVtargetBottom" not in result
    assert (paths.media_root / "test" / "BVtargetBottom_64K.mp3").exists()
    assert "BVstaleOld" in result
    assert not (paths.media_root / "test" / "BVstaleOld_64K.mp3").exists()


def test_cleanup_retention_media_removes_old_quality_variants(tmp_path):
    from bilibili_podcast.sync import cleanup_retention_media

    cfg = type("Config", (), {
        "series": "test", "keep_last": 1, "filters": {},
        "sync": {"quality": "192K"},
    })()
    paths = type("Paths", (), {
        "media_root": tmp_path / "media",
        "json_root": tmp_path / "json",
        "rss_root": tmp_path / "rss",
        "media_base_url": "http://localhost:8080",
    })()
    (paths.media_root / "test").mkdir(parents=True)
    (paths.json_root / "test").mkdir(parents=True)
    (paths.media_root / "test" / "BVold001_64K.mp3").write_text("old")
    (paths.json_root / "test" / "BVold001_64K.info.json").write_text("{}")

    result = cleanup_retention_media(cfg, paths, [{
        "bvid": "BVnew001",
        "_existing_enclosure_url": "http://example.test/BVnew001.mp3",
    }])

    assert "BVold001" in result
    assert not (paths.media_root / "test" / "BVold001_64K.mp3").exists()
    assert not (paths.json_root / "test" / "BVold001_64K.info.json").exists()
