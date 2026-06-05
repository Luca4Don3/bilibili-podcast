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

    env_file = app_dir / "bilibili-podcast-env.sh"
    env_file.write_text("export BILIBILI_PODCAST_RSYNC_HOST=<rsync_host>\n")

    sync_unit = systemd_dir / "bilibili-podcast-sync@demo.service"
    sync_unit.write_text(
        "[Service]\n"
        f"EnvironmentFile={env_file}\n"
        "Environment=PLAYWRIGHT_BROWSERS_PATH=<server_path>\n"
        "ExecStart=<server_path> --series demo\n"
    )

    web_unit = systemd_dir / "bilibili-podcast-web.service"
    web_unit.write_text(
        "[Service]\n"
        "Environment=BILIBILI_PODCAST_CONFIG_DB=/var/lib/bilibili-podcast/state/bilibili-podcast.db\n"
        "Environment=BILIBILI_PODCAST_WEB_PASSWORD=<web_password>\n"
        "Environment=BILIBILI_PODCAST_HTTPS=1\n"
        "Environment=BILIBILI_PODCAST_HTTPS=1\n"
        "ExecStart=<server_path> bilibili_podcast.web.server:app\n"
    )

    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IXUSR)

    script = Path(__file__).resolve().parent.parent / "scripts" / "standardize-runtime-config.sh"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "BILIBILI_PODCAST_APP_DIR": str(app_dir),
        "BILIBILI_PODCAST_SYSTEMD_DIR": str(systemd_dir),
        "BILIBILI_PODCAST_SECRETS_DIR": str(secrets_dir),
        "BILIBILI_PODCAST_ENV_FILE": str(env_file),
        "BILIBILI_PODCAST_WEB_UNIT": str(web_unit),
        "BILIBILI_PODCAST_WEB_ENV_FILE": str(secrets_dir / "bilibili-podcast-web.env"),
        "BILIBILI_PODCAST_SYNC_UNIT_GLOB": str(systemd_dir / "bilibili-podcast-sync@*.service"),
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
    secret_content = (secrets_dir / "bilibili-podcast-web.env").read_text()

    assert f"EnvironmentFile={env_file}" not in sync_content
    assert "Environment=BILIBILI_PODCAST_WEB_PASSWORD=" not in web_content
    assert f"EnvironmentFile={secrets_dir / 'bilibili-podcast-web.env'}" in web_content
    assert web_content.count("Environment=BILIBILI_PODCAST_HTTPS=1") == 1
    assert secret_content == "BILIBILI_PODCAST_WEB_PASSWORD=<web_password>\n"
    assert "<web_password>" not in result.stdout
