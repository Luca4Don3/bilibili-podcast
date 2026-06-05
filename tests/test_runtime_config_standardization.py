import os
import stat
import subprocess
from pathlib import Path


def test_standardize_runtime_config_migrates_web_secret_and_cleans_sync_units(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    secrets_dir = app_dir / "secrets"
    systemd_dir = tmp_path / "systemd"
    fake_bin = tmp_path / "bin"
    app_dir.mkdir()
    secrets_dir.mkdir()
    systemd_dir.mkdir()
    fake_bin.mkdir()

    env_file = app_dir / "bilipod-env.sh"
    env_file.write_text("export BILIPOD_RSYNC_HOST=<rsync_host>\n")

    sync_unit = systemd_dir / "bilipod-sync@demo.service"
    sync_unit.write_text(
        "[Service]\n"
        f"EnvironmentFile={env_file}\n"
        "Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/bilipod/playwright-browsers\n"
        "ExecStart=/opt/bilipod/venv/bin/bilibili-podcast --series demo\n"
    )

    web_unit = systemd_dir / "bilipod-web.service"
    web_unit.write_text(
        "[Service]\n"
        "Environment=BILIPOD_CONFIG_DB=/var/lib/bilipod/state/bilipod.db\n"
        "Environment=BILIPOD_WEB_PASSWORD=<web_password>\n"
        "Environment=BILIPOD_HTTPS=1\n"
        "Environment=BILIPOD_HTTPS=1\n"
        "ExecStart=/opt/bilipod/venv/bin/uvicorn bilibili_podcast.web.server:app\n"
    )

    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IXUSR)

    script = Path(__file__).resolve().parent.parent / "scripts" / "standardize-runtime-config.sh"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "BILIPOD_APP_DIR": str(app_dir),
        "BILIPOD_SYSTEMD_DIR": str(systemd_dir),
        "BILIPOD_SECRETS_DIR": str(secrets_dir),
        "BILIPOD_ENV_FILE": str(env_file),
        "BILIPOD_WEB_UNIT": str(web_unit),
        "BILIPOD_WEB_ENV_FILE": str(secrets_dir / "bilipod-web.env"),
        "BILIPOD_SYNC_UNIT_GLOB": str(systemd_dir / "bilipod-sync@*.service"),
    }

    result = subprocess.run(
        ["bash", str(script), "--apply"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    sync_content = sync_unit.read_text()
    web_content = web_unit.read_text()
    secret_content = (secrets_dir / "bilipod-web.env").read_text()

    assert f"EnvironmentFile={env_file}" not in sync_content
    assert "Environment=BILIPOD_WEB_PASSWORD=" not in web_content
    assert f"EnvironmentFile={secrets_dir / 'bilipod-web.env'}" in web_content
    assert web_content.count("Environment=BILIPOD_HTTPS=1") == 1
    assert secret_content == "BILIPOD_WEB_PASSWORD=<web_password>\n"
    assert "<web_password>" not in result.stdout
