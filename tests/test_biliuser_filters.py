from bilibili_podcast.sync import apply_filters, is_paid_content, load_browser_cookies
from bilibili_podcast.sync import merge_browser_hints, paid_state_incomplete
from bilibili_podcast.utils.series_config import SeriesConfig


def _paid_preview_config() -> SeriesConfig:
    return SeriesConfig(
        series="paid-sample",
        enabled=True,
        title="Paid Sample",
        description="",
        author="Demo Author",
        cover_art="",
        category="",
        subcategories=[],
        explicit=False,
        lang="zh-CN",
        source={"uid": 123456, "type": "space"},
        sync={},
        filters={
            "exclude_paid": False,
            "include_keywords": ["Paid Sample"],
        },
        paid_preview={
            "enabled": True,
            "retry_after_days": 4,
        },
        keep_last=100,
    )


def test_paid_preview_policy_keeps_only_target_series():
    config = _paid_preview_config()
    episodes = [
        {"bvid": "BV_PAID", "title": "Paid Sample 抢先看", "description": "", "pay": 1},
        {"bvid": "BV_OTHER1", "title": "Other show latest", "description": "", "pay": 0},
        {"bvid": "BV_OTHER2", "title": "Another show latest", "description": "", "pay": 0},
        {"bvid": "BV_KEEP", "title": "Paid Sample latest", "description": "", "pay": 0},
    ]

    filtered, excluded = apply_filters(config, episodes)

    assert [episode["bvid"] for episode in filtered] == ["BV_PAID", "BV_KEEP"]


def test_paid_state_can_be_completed_from_browser_hint():
    config = _paid_preview_config()
    api_episodes = [{"bvid": "BV_PREVIEW", "title": "Paid Sample latest", "raw": {}}]
    browser_episodes = [
        {
            "bvid": "BV_PREVIEW",
            "title": "Paid Sample latest",
            "raw": {"browser_text": "Paid Sample latest 抢先看"},
        }
    ]

    assert paid_state_incomplete(config, api_episodes) is True
    merged = merge_browser_hints(api_episodes, browser_episodes)

    assert paid_state_incomplete(config, merged) is False
    assert is_paid_content(merged[0]) is True


def test_paid_preview_unknown_state_is_not_kept():
    config = _paid_preview_config()
    episodes = [{"bvid": "BV_UNKNOWN", "title": "Paid Sample latest", "description": "", "raw": {}}]

    filtered, excluded = apply_filters(config, episodes)
    assert filtered == []


def test_http_only_netscape_cookies_are_parsed(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1999999999\tSESSDATA\tsecret\n",
        encoding="utf-8",
    )

    cookies = load_browser_cookies(str(cookie_file))

    assert cookies[0]["name"] == "SESSDATA"
    assert cookies[0]["domain"] == ".bilibili.com"


def test_exclude_season_ids_filters_matching_raw_season_only():
    config = _paid_preview_config()
    config.filters = {"exclude_paid": False, "exclude_season_ids": [5492168]}
    config.paid_preview = {"enabled": False}
    episodes = [
        {"bvid": "BV_SEASON", "title": "Excluded", "raw": {"season_id": 5492168}},
        {"bvid": "BV_ZERO", "title": "Zero", "raw": {"season_id": 0}},
        {"bvid": "BV_MISSING", "title": "Missing", "raw": {}},
    ]

    filtered, excluded = apply_filters(config, episodes)

    assert [episode["bvid"] for episode in filtered] == ["BV_ZERO", "BV_MISSING"]
    assert excluded["season"] == {"BV_SEASON"}
