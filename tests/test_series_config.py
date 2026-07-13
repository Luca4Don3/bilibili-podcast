from pathlib import Path

import pytest

from bilibili_podcast.utils.series_config import SeriesConfig


def _write_config(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_series_config_loads_unlimited_keep_last_for_archive_series(tmp_path):
    config_path = _write_config(
        tmp_path,
        "archive-sample",
        """
series: archive-sample
title: Archive Sample
author: Demo Author
source:
  uid: 123456
sync:
  keep_last: 0
""",
    )
    config = SeriesConfig.from_yaml(config_path)

    assert config.series == "archive-sample"
    assert config.title == "Archive Sample"
    assert config.author == "Demo Author"
    assert config.keep_last == 0


def test_series_config_loads_zero_keep_last_for_unlimited_series(tmp_path):
    config_path = _write_config(
        tmp_path,
        "unlimited-sample",
        """
series: unlimited-sample
title: Unlimited Sample
author: Demo Author
source:
  uid: 123456
sync:
  keep_last: 0
""",
    )
    config = SeriesConfig.from_yaml(config_path)

    assert config.keep_last == 0


def test_series_config_loads_paid_preview_policy(tmp_path):
    config_path = _write_config(
        tmp_path,
        "paid-sample",
        """
series: paid-sample
title: Paid Sample
author: Demo Author
source:
  uid: 123456
filters:
  exclude_paid: false
paid_preview:
  enabled: true
""",
    )
    config = SeriesConfig.from_yaml(config_path)

    assert config.title == "Paid Sample"
    assert config.author == "Demo Author"
    assert config.filters["exclude_paid"] is False
    assert config.paid_preview["enabled"] is True


def test_series_config_normalizes_exclude_season_ids(tmp_path):
    config_path = _write_config(
        tmp_path,
        "season-filter",
        """
series: season-filter
title: Season Filter
author: Demo Author
source:
  uid: 123456
filters:
  exclude_season_ids: [123, "456"]
""",
    )

    config = SeriesConfig.from_yaml(config_path)

    assert config.filters["exclude_season_ids"] == [123, 456]


def test_series_config_rejects_invalid_exclude_season_ids(tmp_path):
    config_path = _write_config(
        tmp_path,
        "bad-season-filter",
        """
series: bad-season-filter
title: Bad Season Filter
author: Demo Author
source:
  uid: 123456
filters:
  exclude_season_ids: [0]
""",
    )

    with pytest.raises(ValueError, match="positive integers"):
        SeriesConfig.from_yaml(config_path)


def test_series_config_rejects_unsafe_series_slug(tmp_path):
    config_path = tmp_path / "bad slug.yaml"
    config_path.write_text(
        """
series: bad slug
title: Bad
author: Tester
source:
  uid: 123
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="series must use"):
        SeriesConfig.from_yaml(config_path)


def test_series_config_requires_file_name_to_match_series(tmp_path):
    config_path = tmp_path / "wrong.yaml"
    config_path.write_text(
        """
series: right
title: Right
author: Tester
source:
  uid: 123
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="series must match"):
        SeriesConfig.from_yaml(config_path)


def test_series_config_extracts_uid_from_space_url_with_query(tmp_path):
    config_path = tmp_path / "spaceurl.yaml"
    config_path.write_text(
        """
series: spaceurl
title: Space URL
author: Tester
source:
  space_url: "https://space.bilibili.com/123456?spm_id_from=333.337.0.0"
""",
        encoding="utf-8",
    )

    assert SeriesConfig.from_yaml(config_path).uid == 123456
