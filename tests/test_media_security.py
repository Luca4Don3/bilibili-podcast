from __future__ import annotations

import os
import subprocess

import pytest

from bilibili_podcast.media_security import (
    MediaIntegrityError,
    MediaDownloadError,
    atomic_media_copy,
    private_cookie_copy,
    run_download,
)


def test_atomic_copy_validates_before_replace(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    target = tmp_path / "target.mp3"

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="12.5\n", stderr="")

    monkeypatch.setattr("bilibili_podcast.media_security.subprocess.run", fake_run)
    atomic_media_copy(source, target)
    assert target.read_bytes() == b"audio"
    assert target.stat().st_mode & 0o777 == 0o440
    assert target.stat().st_nlink == 1


def test_invalid_existing_media_is_not_replaced(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    target = tmp_path / "target.mp3"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    monkeypatch.setattr(
        "bilibili_podcast.media_security.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr="bad"),
    )
    with pytest.raises(MediaIntegrityError):
        atomic_media_copy(source, target, replace=True)
    assert target.read_bytes() == b"old"


def test_cookie_copy_is_private_and_removed(tmp_path):
    source = tmp_path / "cookies.txt"
    source.write_text("cookie")
    with private_cookie_copy(source) as copy:
        assert copy.read_text() == "cookie"
        assert copy.stat().st_mode & 0o777 == 0o600
        copied_path = copy
    assert not copied_path.exists()


def test_download_failure_is_redacted_and_does_not_leave_cookie_path(tmp_path, monkeypatch):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("cookie")

    def fake_run(command, **kwargs):
        assert str(cookie) not in command
        return subprocess.CompletedProcess(command, 1, stdout="secret=bad", stderr="cookie path")

    monkeypatch.setattr("bilibili_podcast.media_security.subprocess.run", fake_run)
    with pytest.raises(MediaDownloadError, match="download command failed"):
        run_download(["yt-dlp", "--cookies", "placeholder", "url"], cookie_file=cookie)


@pytest.mark.parametrize("duration", ("0", "-1", "inf", "nan", "not-a-number"))
def test_media_duration_must_be_positive_and_finite(tmp_path, monkeypatch, duration):
    media = tmp_path / "media.mp3"
    media.write_bytes(b"audio")
    monkeypatch.setattr(
        "bilibili_podcast.media_security.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{duration}\n",
            stderr="",
        ),
    )
    from bilibili_podcast.media_security import validate_media

    with pytest.raises(MediaIntegrityError, match="duration"):
        validate_media(media)


def test_media_and_cookie_reject_hardlinks(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    media_link = tmp_path / "linked.mp3"
    os.link(source, media_link)
    monkeypatch.setattr(
        "bilibili_podcast.media_security.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="10\n",
            stderr="",
        ),
    )
    with pytest.raises(MediaIntegrityError, match="hard link"):
        atomic_media_copy(source, tmp_path / "target.mp3")
    with pytest.raises(PermissionError, match="unsafe"):
        with private_cookie_copy(media_link):
            pass


def test_cookie_rejects_symlink_in_parent_path(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    cookie = real / "cookies.txt"
    cookie.write_text("cookie")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(PermissionError, match="unsafe"):
        with private_cookie_copy(linked / "cookies.txt"):
            pass


def test_atomic_copy_failure_removes_only_its_operation_directory(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    unrelated = tmp_path / ".media-operation-unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("keep")
    monkeypatch.setattr(
        "bilibili_podcast.media_security.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="sensitive",
        ),
    )
    with pytest.raises(MediaIntegrityError, match="rejected"):
        atomic_media_copy(source, tmp_path / "target.mp3")
    assert (unrelated / "keep").read_text() == "keep"
    assert tuple(
        item for item in tmp_path.glob(".media-operation-*")
        if item != unrelated
    ) == ()
