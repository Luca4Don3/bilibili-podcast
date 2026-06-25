from bilibili_podcast.utils.bilibili_url import parse_space_source


def test_parse_space_source_accepts_supported_inputs() -> None:
    assert parse_space_source("https://space.bilibili.com/123456?spm=x") == {
        "type": "space",
        "uid": 123456,
        "sid": None,
        "space_url": "https://space.bilibili.com/123456",
    }
    assert parse_space_source(
        "https://www.bilibili.com/space/654321/"
    )["uid"] == 654321
    assert parse_space_source("123456")["space_url"] == (
        "https://space.bilibili.com/123456"
    )


def test_parse_space_source_rejects_untrusted_or_unrelated_urls() -> None:
    assert parse_space_source(
        "https://www.bilibili.com/video/BV1xx411c7mD"
    ) is None
    assert parse_space_source(
        "https://evil.invalid/https://space.bilibili.com/123456"
    ) is None
    assert parse_space_source("ftp://space.bilibili.com/123456") is None
    assert parse_space_source("https://user@space.bilibili.com/123456") is None
    assert parse_space_source("https://space.bilibili.com/123456/extra") is None
