import os
import stat
import subprocess
from pathlib import Path


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_deploy_apply_fails_before_any_configuration_read_or_write(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_marker = tmp_path / "config-command-ran"
    _executable(
        fake_bin / "bilibili-podcast-config",
        f"#!/bin/sh\ntouch '{command_marker}'\nexit 0\n",
    )
    config_root = tmp_path / "config-must-not-be-created"
    script = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"

    result = subprocess.run(
        ["bash", str(script), "--apply"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "BILIBILI_PODCAST_CONFIG_ROOT": str(config_root),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 3
    assert "immutable release prepare/activate workflow" in result.stderr
    assert not command_marker.exists()
    assert not config_root.exists()


def test_deploy_requires_separate_system_permissions_authorization() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"
    result = subprocess.run(
        ["bash", str(script), "--system-permissions"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --apply" in result.stderr
