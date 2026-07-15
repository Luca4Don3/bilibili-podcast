import json
import os
import pwd
import stat
import subprocess
from pathlib import Path


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_deploy_backs_up_configured_systemd_unit_names(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    code_dir = tmp_path / "app"
    venv_bin = tmp_path / "venv" / "bin"
    systemd_dir = tmp_path / "systemd"
    wrapper_dir = tmp_path / "wrappers"
    state_root = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    for directory in (
        config_root, code_dir / ".git", venv_bin, systemd_dir,
        wrapper_dir, state_root, fake_bin,
    ):
        directory.mkdir(parents=True)
    (code_dir / "requirements.lock").write_text("# test\n")
    (config_root / "app.toml").write_text("test = true\n")
    (config_root / "rss-users.toml").write_text("")
    for name in (
        "podcast-web.service", "podcast-sync-demo.service", "podcast-sync-demo.timer",
        "bilibili-podcast-retry@demo.service", "bilibili-podcast-retry@demo.timer",
    ):
        (systemd_dir / name).write_text(f"# {name}\n")
    (wrapper_dir / "run_demo_sync.sh").write_text("#!/bin/sh\n")

    config = {
        "app": {
            "database": {"path": str(state_root / "missing.db")},
            "paths": {"state_root": str(state_root)},
            "install": {"app_dir": str(code_dir), "venv_bin": str(venv_bin)},
        },
        "scheduler": {
            "paths": {"wrapper_dir": str(wrapper_dir), "systemd_dir": str(systemd_dir)},
            "runtime": {"user": pwd.getpwuid(os.getuid()).pw_name},
            "units": {
                "web": "podcast-web.service",
                "sync_glob": "podcast-sync-*.service",
            },
        },
    }
    _executable(
        fake_bin / "bilibili-podcast-config",
        "#!/bin/sh\n"
        "if [ \"${3:-}\" = upgrade ]; then\n"
        "  echo '{\"action\":\"dry-run\",\"source_version\":3,\"target_version\":3,\"steps\":[],\"backup_root\":null}'\n"
        "elif [ \"${1:-}\" = show ]; then\n"
        f"  echo '{json.dumps(config)}'\n"
        "fi\n"
        "exit 0\n",
    )
    _executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
    python_calls = tmp_path / "python-calls"
    _executable(
        venv_bin / "python3",
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{python_calls}'\n"
        "case \" $* \" in\n"
        "  *\" upgrade \"*) echo '{\"action\":\"dry-run\",\"source_version\":2,\"target_version\":3,\"steps\":[\"initialize-versioned-installation\"],\"backup_root\":null}' ;;\n"
        "esac\n"
        "exit 0\n",
    )
    _executable(venv_bin / "pip", "#!/bin/sh\nexit 0\n")

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
        check=True,
    )

    backups = list((config_root / ".backups").glob("*/systemd"))
    assert len(backups) == 1
    assert {path.name for path in backups[0].iterdir()} == {
        "podcast-web.service", "podcast-sync-demo.service", "podcast-sync-demo.timer",
        "bilibili-podcast-retry@demo.service", "bilibili-podcast-retry@demo.timer",
    }
    assert (backups[0].parent / "config" / "rss-users.toml").read_bytes() == b""
    assert "without service restart" in result.stdout
    assert any("upgrade --apply" in line for line in python_calls.read_text().splitlines())
    assert not any(
        "permissions --apply" in line for line in python_calls.read_text().splitlines()
    )


def test_deploy_requires_separate_system_permissions_authorization() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"
    result = subprocess.run(
        ["bash", str(script), "--system-permissions"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "requires --apply" in result.stderr
