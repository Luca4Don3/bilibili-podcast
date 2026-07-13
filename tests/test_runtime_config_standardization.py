import json
import os
import stat
import subprocess
from pathlib import Path


def test_standardize_runtime_config_writes_canonical_units_without_systemctl(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    venv_bin = tmp_path / "venv" / "bin"
    systemd_dir = tmp_path / "systemd"
    config_root = tmp_path / "config"
    fake_bin = tmp_path / "bin"
    for directory in (app_dir, venv_bin, systemd_dir, config_root / ".backups", fake_bin):
        directory.mkdir(parents=True)

    sync_unit = systemd_dir / "bilipod-sync@demo.service"
    sync_unit.write_text(
        "[Service]\nEnvironmentFile=/legacy.env\n"
        "Environment=PLAYWRIGHT_BROWSERS_PATH=/legacy/browser\n"
        "ExecStart=/legacy/sync --cookie-file /legacy/cookie\n"
    )
    web_unit = systemd_dir / "bilipod-web.service"
    web_unit.write_text(
        "[Service]\nEnvironmentFile=/legacy-web.env\n"
        "Environment=BILIPOD_WEB_PASSWORD=legacy\n"
        "ExecStart=/legacy/uvicorn bilibili_podcast.web.server:app\n"
    )

    config_json = {
        "app": {
            "install": {"app_dir": str(app_dir), "venv_bin": str(venv_bin)},
            "executables": {"sync": str(venv_bin / "bilibili-podcast")},
        },
        "scheduler": {
            "paths": {"systemd_dir": str(systemd_dir)},
            "runtime": {"user": "bilipod", "group": "bilipod"},
        },
        "sync": {"downloads": {"scheduled_max_per_run": 1}},
    }
    fake_config = fake_bin / "bilipod-config"
    fake_config.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = show ]; then\n"
        f"  echo '{json.dumps(config_json)}'\n"
        "fi\n"
        "exit 0\n"
    )
    fake_config.chmod(fake_config.stat().st_mode | stat.S_IXUSR)
    systemctl_marker = tmp_path / "systemctl-called"
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(f"#!/bin/sh\ntouch '{systemctl_marker}'\nexit 0\n")
    fake_systemctl.chmod(fake_systemctl.stat().st_mode | stat.S_IXUSR)

    legacy_env = tmp_path / "legacy.env"
    legacy_web_env = tmp_path / "legacy-web.env"
    legacy_series = tmp_path / "series"
    legacy_users = tmp_path / "users.conf"
    legacy_env.write_text("")
    legacy_web_env.write_text("")
    legacy_series.mkdir()
    legacy_users.write_text("")

    script = Path(__file__).resolve().parent.parent / "scripts" / "standardize-runtime-config.sh"
    result = subprocess.run(
        ["bash", str(script), "--apply"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "BILIPOD_CONFIG_ROOT": str(config_root),
            "BILIPOD_ENV_FILE": str(legacy_env),
            "BILIPOD_WEB_ENV_FILE": str(legacy_web_env),
            "BILIPOD_LEGACY_SERIES_DIR": str(legacy_series),
            "RSS_USERS_CONF": str(legacy_users),
        },
        capture_output=True, text=True, check=True,
    )

    sync_content = sync_unit.read_text()
    web_content = web_unit.read_text()
    assert "EnvironmentFile=" not in sync_content + web_content
    assert "PLAYWRIGHT_BROWSERS_PATH" not in sync_content
    assert "BILIPOD_WEB_PASSWORD" not in web_content
    assert f'Environment="BILIPOD_CONFIG_ROOT={config_root}"' in sync_content
    assert f"ExecStart={venv_bin}/bilipod-web" in web_content
    assert "--cookie-file" not in sync_content
    assert not systemctl_marker.exists()
    assert "No systemd reload" in result.stdout


def test_standardize_dry_run_does_not_require_generated_output(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_config = fake_bin / "bilipod-config"
    fake_config.write_text("#!/bin/sh\nexit 0\n")
    fake_config.chmod(0o755)
    missing_root = tmp_path / "not-created"
    empty = tmp_path / "empty"
    empty.write_text("")
    series = tmp_path / "series"
    series.mkdir()
    script = Path(__file__).resolve().parent.parent / "scripts" / "standardize-runtime-config.sh"
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "BILIPOD_CONFIG_ROOT": str(missing_root),
            "BILIPOD_ENV_FILE": str(empty),
            "BILIPOD_WEB_ENV_FILE": str(empty),
            "BILIPOD_LEGACY_SERIES_DIR": str(series),
            "RSS_USERS_CONF": str(empty),
        },
        capture_output=True, text=True, check=True,
    )
    assert not missing_root.exists()
    assert "without writes" in result.stdout


def test_standardize_restores_units_when_later_rewrite_fails(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    venv_bin = tmp_path / "venv" / "bin"
    systemd_dir = tmp_path / "systemd"
    config_root = tmp_path / "config"
    fake_bin = tmp_path / "bin"
    for directory in (app_dir, venv_bin, systemd_dir, config_root / ".backups", fake_bin):
        directory.mkdir(parents=True)
    web_unit = systemd_dir / "bilipod-web.service"
    original_web = "[Service]\nExecStart=/legacy/web\n"
    web_unit.write_text(original_web)
    (systemd_dir / "bilipod-sync@demo.service").write_text("")
    config_json = {
        "app": {
            "install": {"app_dir": str(app_dir), "venv_bin": str(venv_bin)},
            "executables": {"sync": str(venv_bin / "bilibili-podcast")},
        },
        "scheduler": {
            "paths": {"systemd_dir": str(systemd_dir)},
            "runtime": {"user": "bilipod", "group": "bilipod"},
        },
        "sync": {"downloads": {"scheduled_max_per_run": 1}},
    }
    fake_config = fake_bin / "bilipod-config"
    fake_config.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = show ]; then\n"
        f"  echo '{json.dumps(config_json)}'\n"
        "fi\n"
        "exit 0\n"
    )
    fake_config.chmod(0o755)
    empty = tmp_path / "empty"
    empty.write_text("")
    series = tmp_path / "series"
    series.mkdir()
    script = Path(__file__).resolve().parent.parent / "scripts" / "standardize-runtime-config.sh"

    result = subprocess.run(
        ["bash", str(script), "--apply"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "BILIPOD_CONFIG_ROOT": str(config_root),
            "BILIPOD_ENV_FILE": str(empty),
            "BILIPOD_WEB_ENV_FILE": str(empty),
            "BILIPOD_LEGACY_SERIES_DIR": str(series),
            "RSS_USERS_CONF": str(empty),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "original units were restored" in result.stderr
    assert web_unit.read_text() == original_web
