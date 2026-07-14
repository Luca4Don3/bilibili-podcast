from __future__ import annotations

import threading
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from bilibili_podcast.publisher import PublishError, publish, token_digest


MASTER = b"""<?xml version="1.0"?><rss><channel><item>
<enclosure url="https://media.test/media/demo/item.mp3?token=__MEDIA_PLACEHOLDER__" />
</item></channel></rss>"""


def _snapshot(tmp_path, *, token="secret-token"):
    rss_root = tmp_path / "rss"
    rss_root.mkdir(exist_ok=True)
    (rss_root / "demo.xml").write_bytes(MASTER)
    return SimpleNamespace(
        app=SimpleNamespace(paths=SimpleNamespace(
            rss_root=rss_root,
            published_rss_root=tmp_path / "published",
        )),
        publish=SimpleNamespace(publish=SimpleNamespace(
            enabled=True,
            master_placeholder="__MEDIA_PLACEHOLDER__",
            gone_series=(),
        )),
        rss_users=SimpleNamespace(users={
            "user": SimpleNamespace(token=token, series=("demo",)),
        }),
    )


def test_publish_uses_token_hash_directory_and_preserves_master(tmp_path):
    snapshot = _snapshot(tmp_path)

    generation = publish(snapshot)

    published = snapshot.app.paths.published_rss_root / "current" / token_digest(
        "secret-token"
    ) / "demo.xml"
    assert generation
    assert published.is_file()
    assert b"secret-token" in published.read_bytes()
    assert b"secret-token" not in (snapshot.app.paths.rss_root / "demo.xml").read_bytes()
    assert b"__MEDIA_PLACEHOLDER__" in (snapshot.app.paths.rss_root / "demo.xml").read_bytes()
    assert stat.S_IMODE(published.stat().st_mode) == 0o640


def test_failed_generation_does_not_switch_current(tmp_path):
    snapshot = _snapshot(tmp_path)
    first = publish(snapshot)
    (snapshot.app.paths.rss_root / "demo.xml").write_text("<broken>")

    with pytest.raises(PublishError):
        publish(snapshot)

    assert snapshot.app.paths.published_rss_root.joinpath("current").resolve().name == first


def test_only_two_complete_generations_are_retained(tmp_path):
    snapshot = _snapshot(tmp_path)
    for _ in range(3):
        publish(snapshot)

    generations = snapshot.app.paths.published_rss_root / ".generations"
    assert len([item for item in generations.iterdir() if not item.name.startswith(".")]) == 2


def test_concurrent_publishers_serialize(tmp_path):
    snapshot = _snapshot(tmp_path)
    errors = []

    def run():
        try:
            publish(snapshot)
        except Exception as exc:  # pragma: no cover - assertion reports the value
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert snapshot.app.paths.published_rss_root.joinpath("current").is_dir()


def test_master_with_configured_token_is_rejected(tmp_path):
    snapshot = _snapshot(tmp_path)
    path = snapshot.app.paths.rss_root / "demo.xml"
    path.write_bytes(MASTER.replace(b"__MEDIA_PLACEHOLDER__", b"secret-token"))

    with pytest.raises(PublishError, match="configured user token"):
        publish(snapshot)


def test_empty_master_set_is_rejected_without_switching(tmp_path):
    snapshot = _snapshot(tmp_path)
    (snapshot.app.paths.rss_root / "demo.xml").rename(
        snapshot.app.paths.rss_root / "demo.xml.disabled"
    )

    with pytest.raises(PublishError, match="no active master"):
        publish(snapshot)
    assert not (snapshot.app.paths.published_rss_root / "current").exists()


def test_gone_series_is_not_published_for_all_user(tmp_path):
    snapshot = _snapshot(tmp_path)
    snapshot.publish.publish.gone_series = ("demo",)
    snapshot.rss_users.users["user"].series = ("all",)

    with pytest.raises(PublishError, match="no active master"):
        publish(snapshot)


def test_missing_authorized_series_fails_before_switch(tmp_path):
    snapshot = _snapshot(tmp_path)
    snapshot.rss_users.users["user"].series = ("missing-series",)

    with pytest.raises(PublishError, match="missing series"):
        publish(snapshot)
    assert not (snapshot.app.paths.published_rss_root / "current").exists()


def test_cleanup_failure_after_activation_does_not_report_publish_failure(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path)
    first = publish(snapshot)
    original_rmtree = __import__("shutil").rmtree

    def fail_obsolete(path, *args, **kwargs):
        if Path(path).name == first:
            raise OSError("injected cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    import bilibili_podcast.publisher as publisher_module

    monkeypatch.setattr(publisher_module.shutil, "rmtree", fail_obsolete)
    second = publish(snapshot)
    assert snapshot.app.paths.published_rss_root.joinpath("current").resolve().name == second
