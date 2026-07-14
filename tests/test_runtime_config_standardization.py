import json
import grp
import os
import pwd
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

    legacy_product = "bili" + "pod"
    legacy_sync_unit = systemd_dir / f"{legacy_product}-sync@demo.service"
    sync_unit = systemd_dir / "bilibili-podcast-sync@demo.service"
    legacy_sync_unit.write_text(
        "[Service]\nEnvironmentFile=/legacy.env\n"
        "Environment=PLAYWRIGHT_BROWSERS_PATH=/legacy/browser\n"
        "ExecStart=/legacy/sync --cookie-file /legacy/cookie\n"
    )
    legacy_timer = systemd_dir / f"{legacy_product}-sync@demo.timer"
    legacy_timer.write_text("[Timer]\nOnCalendar=*-*-* 01:00:00\n")
    legacy_web_unit = systemd_dir / f"{legacy_product}-web.service"
    web_unit = systemd_dir / "bilibili-podcast-web.service"
    legacy_web_unit.write_text(
        "[Service]\nEnvironmentFile=/legacy-web.env\n"
        "Environment=BILIBILI_PODCAST_WEB_PASSWORD=legacy\n"
        "ExecStart=/legacy/uvicorn bilibili_podcast.web.server:app\n"
    )

    config_json = {
        "app": {
            "install": {"app_dir": str(app_dir), "venv_bin": str(venv_bin)},
            "executables": {"sync": str(venv_bin / "bilibili-podcast")},
        },
        "scheduler": {
            "paths": {"systemd_dir": str(systemd_dir)},
            "runtime": {
                "user": pwd.getpwuid(os.getuid()).pw_name,
                "group": grp.getgrgid(os.getgid()).gr_name,
            },
            "units": {
                "web": "bilibili-podcast-web.service",
                "sync_glob": "bilibili-podcast-sync@*.service",
            },
        },
        "sync": {"downloads": {"scheduled_max_per_run": 1}},
    }
    fake_config = fake_bin / "bilibili-podcast-config"
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
        [
            "bash", str(script), "--apply",
            "--web-primary-port", "18001", "--web-backup-port", "18002",
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "BILIBILI_PODCAST_CONFIG_ROOT": str(config_root),
            "BILIBILI_PODCAST_ENV_FILE": str(legacy_env),
            "BILIBILI_PODCAST_WEB_ENV_FILE": str(legacy_web_env),
            "BILIBILI_PODCAST_LEGACY_SERIES_DIR": str(legacy_series),
            "RSS_USERS_CONF": str(legacy_users),
        },
        capture_output=True, text=True, check=True,
    )

    sync_content = sync_unit.read_text()
    web_content = web_unit.read_text()
    backup_web_unit = systemd_dir / "bilibili-podcast-web-backup.service"
    backup_web_content = backup_web_unit.read_text()
    assert "EnvironmentFile=" not in sync_content + web_content
    assert "PLAYWRIGHT_BROWSERS_PATH" not in sync_content
    assert "BILIBILI_PODCAST_WEB_PASSWORD" not in web_content
    assert f'Environment="BILIBILI_PODCAST_CONFIG_ROOT={config_root}"' in sync_content
    assert (
        f"ExecStart={venv_bin}/bilibili-podcast-web --host 127.0.0.1 --port 18001"
        in web_content
    )
    assert (
        f"ExecStart={venv_bin}/bilibili-podcast-web --host 127.0.0.1 --port 18002"
        in backup_web_content
    )
    assert "--cookie-file" not in sync_content
    assert not systemctl_marker.exists()
    assert "No systemd reload" in result.stdout
    assert legacy_sync_unit.exists()
    assert legacy_web_unit.exists()
    assert (systemd_dir / "bilibili-podcast-sync@demo.timer").read_text() == legacy_timer.read_text()
    assert "Prepared Web units" in result.stdout


def test_standardize_dry_run_does_not_require_generated_output(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_config = fake_bin / "bilibili-podcast-config"
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
            "BILIBILI_PODCAST_CONFIG_ROOT": str(missing_root),
            "BILIBILI_PODCAST_ENV_FILE": str(empty),
            "BILIBILI_PODCAST_WEB_ENV_FILE": str(empty),
            "BILIBILI_PODCAST_LEGACY_SERIES_DIR": str(series),
            "RSS_USERS_CONF": str(empty),
        },
        capture_output=True, text=True, check=True,
    )
    assert not missing_root.exists()
    assert "without writes" in result.stdout


def test_standardize_passes_legacy_v0_profile_and_layout_without_printing_values(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    arguments = tmp_path / "arguments"
    fake_config = fake_bin / "bilibili-podcast-config"
    fake_config.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{arguments}'\n"
        "exit 0\n"
    )
    fake_config.chmod(0o755)
    config_root = tmp_path / "config"
    empty = tmp_path / "empty"
    empty.write_text("")
    series = tmp_path / "series"
    series.mkdir()
    layout = tmp_path / "layout.toml"
    layout.write_text('[layout]\napp_dir = "/fixture"\n')
    layout.chmod(0o600)
    script = Path(__file__).resolve().parent.parent / "scripts" / "standardize-runtime-config.sh"

    result = subprocess.run(
        [
            "bash", str(script), "--profile", "legacy-v0",
            "--layout-manifest", str(layout),
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "BILIBILI_PODCAST_CONFIG_ROOT": str(config_root),
            "BILIBILI_PODCAST_ENV_FILE": str(empty),
            "BILIBILI_PODCAST_WEB_ENV_FILE": str(empty),
            "BILIBILI_PODCAST_LEGACY_SERIES_DIR": str(series),
            "RSS_USERS_CONF": str(empty),
        },
        capture_output=True,
        text=True,
        check=True,
    )

    passed = arguments.read_text().splitlines()
    assert passed[passed.index("--profile") + 1] == "legacy-v0"
    assert passed[passed.index("--layout-manifest") + 1] == str(layout)
    assert str(layout) not in result.stdout
    assert not config_root.exists()


def test_standardize_requires_two_distinct_shadow_ports(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "standardize-runtime-config.sh"
    result = subprocess.run(
        ["bash", str(script), "--web-primary-port", "18001"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "must be provided together" in result.stderr

    result = subprocess.run(
        [
            "bash", str(script), "--web-primary-port", "18001",
            "--web-backup-port", "18001",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "must differ" in result.stderr


def test_standardize_restores_units_when_later_rewrite_fails(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    venv_bin = tmp_path / "venv" / "bin"
    systemd_dir = tmp_path / "systemd"
    config_root = tmp_path / "config"
    fake_bin = tmp_path / "bin"
    for directory in (app_dir, venv_bin, systemd_dir, config_root / ".backups", fake_bin):
        directory.mkdir(parents=True)
    web_unit = systemd_dir / "bilibili-podcast-web.service"
    original_web = "[Service]\nExecStart=/legacy/web\n"
    web_unit.write_text(original_web)
    (systemd_dir / "bilibili-podcast-sync@demo.service").write_text("")
    legacy_product = "bili" + "pod"
    legacy_new_service = systemd_dir / f"{legacy_product}-sync@new.service"
    legacy_new_timer = systemd_dir / f"{legacy_product}-sync@new.timer"
    legacy_new_service.write_text("[Service]\nExecStart=/legacy/new\n")
    legacy_new_timer.write_text("[Timer]\nOnCalendar=*-*-* 01:00:00\n")
    new_service = systemd_dir / "bilibili-podcast-sync@new.service"
    new_timer = systemd_dir / "bilibili-podcast-sync@new.timer"
    config_json = {
        "app": {
            "install": {"app_dir": str(app_dir), "venv_bin": str(venv_bin)},
            "executables": {"sync": str(venv_bin / "bilibili-podcast")},
        },
        "scheduler": {
            "paths": {"systemd_dir": str(systemd_dir)},
            "runtime": {
                "user": pwd.getpwuid(os.getuid()).pw_name,
                "group": grp.getgrgid(os.getgid()).gr_name,
            },
            "units": {"web": "bilibili-podcast-web.service", "sync_glob": "bilibili-podcast-sync@*.service"},
        },
        "sync": {"downloads": {"scheduled_max_per_run": 1}},
    }
    fake_config = fake_bin / "bilibili-podcast-config"
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
            "BILIBILI_PODCAST_CONFIG_ROOT": str(config_root),
            "BILIBILI_PODCAST_ENV_FILE": str(empty),
            "BILIBILI_PODCAST_WEB_ENV_FILE": str(empty),
            "BILIBILI_PODCAST_LEGACY_SERIES_DIR": str(series),
            "RSS_USERS_CONF": str(empty),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "original units were restored" in result.stderr
    assert web_unit.read_text() == original_web
    assert not new_service.exists()
    assert not new_timer.exists()
    assert legacy_new_service.exists()
    assert legacy_new_timer.exists()
