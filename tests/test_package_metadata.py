import tomllib
from pathlib import Path


def test_wheel_declares_templates_and_all_console_entry_points():
    metadata = tomllib.loads(Path("pyproject.toml").read_text())

    assert metadata["tool"]["setuptools"]["package-data"]["bilibili_podcast"] == [
        "web/templates/*.html"
    ]
    assert metadata["project"]["scripts"] == {
        "bilibili-podcast": "bilibili_podcast.sync:main",
        "bilibili-podcast-admin": "bilibili_podcast.cli_admin:main",
        "bilibili-podcast-config": "bilibili_podcast.config.cli:main",
        "bilibili-podcast-web": "bilibili_podcast.web.runner:main",
        "bilibili-podcast-publish": "bilibili_podcast.publisher:main",
        "bilibili-podcast-crontab": "bilibili_podcast.crontab:main",
    }
