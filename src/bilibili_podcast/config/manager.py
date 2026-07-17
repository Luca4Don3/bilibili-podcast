"""Locate, validate, cache, and redact unified configuration snapshots."""

from __future__ import annotations

import math
import os
import re
import stat
import threading
import fcntl
from contextlib import contextmanager
from urllib.parse import urlparse
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .models import (
    AppConfig, AppPathsConfig, BrowserConfig, ConfigSnapshot, DatabaseConfig,
    DownloadConfig, ExecutablesConfig, InstallConfig, LoggingConfig,
    ManualMediaConfig, PublishConfig, PublishSettings, RssUser, RssUsersConfig,
    SchedulerConfig, SchedulerPathsConfig, SchedulerRuntimeConfig,
    SchedulerUnitsConfig, SyncConfig, SyncPathsConfig, TimeoutConfig, WebConfig,
    WebSecurityConfig, WebServerConfig,
)
from .repositories import TomlRepository
from ..secure_files import atomic_write_bytes
from .schema import (
    FILE_SCHEMAS, LEGACY_ENV_MAP, LEGACY_INPUT_ONLY, MISSING,
    REMOVED_LEGACY_ENV, FieldSpec,
)


class ConfigError(ValueError):
    exit_code = 2


class UnsafeConfigError(ConfigError):
    exit_code = 3


class LegacyConfigError(ConfigError):
    pass


class ActiveUpgradeError(ConfigError):
    exit_code = 6


MIGRATION_LOCK_NAME = ".migration.lock"


@contextmanager
def _shared_migration_lock(root: Path):
    path = root / MIGRATION_LOCK_NAME
    if path.is_symlink():
        raise UnsafeConfigError(f"unsafe migration lock {path}: symlink")
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _nested_get(data: Mapping[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return MISSING
        current = current[part]
    return current


def _leaf_paths(data: Mapping[str, Any], prefix: str = "") -> set[str]:
    result: set[str] = set()
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and value:
            result.update(_leaf_paths(value, path))
        else:
            result.add(path)
    return result


def _is_expected_type(value: Any, expected: type | tuple[type, ...]) -> bool:
    types = expected if isinstance(expected, tuple) else (expected,)
    if bool not in types and isinstance(value, bool):
        return False
    return isinstance(value, types)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _to_plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    return value


def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class ConfigManager:
    """Load one immutable snapshot. ``reload`` is intentionally explicit."""

    filenames = tuple(FILE_SCHEMAS)

    def __init__(self, root: str | Path | None = None, *, environ: Mapping[str, str] | None = None) -> None:
        self._explicit_root = Path(root).expanduser() if root is not None else None
        self._environ = os.environ if environ is None else environ
        self._snapshot: ConfigSnapshot | None = None
        self._repository = TomlRepository()
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._locate_root()

    def _locate_root(self) -> Path:
        if self._explicit_root is not None:
            root = self._explicit_root
        elif self._environ.get("BILIBILI_PODCAST_CONFIG_ROOT"):
            root = Path(self._environ["BILIBILI_PODCAST_CONFIG_ROOT"]).expanduser()
        else:
            root = self._repository_root_config()
        if not root.is_dir():
            raise ConfigError(f"configuration root does not exist: {root}")
        return root.resolve()

    @staticmethod
    def _repository_root_config() -> Path:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "config"
            if (parent / "pyproject.toml").is_file() and candidate.is_dir():
                return candidate
        raise ConfigError(
            "configuration root is not set; set BILIBILI_PODCAST_CONFIG_ROOT or pass root to ConfigManager"
        )

    def load(
        self,
        *,
        templates: bool = False,
        allow_active_upgrade: bool = False,
    ) -> ConfigSnapshot:
        with self._lock:
            if self._snapshot is not None and not templates:
                return self._snapshot
            if not templates:
                self._reject_legacy_environment()
            root = self.root
            if (
                not templates
                and not allow_active_upgrade
                and (root / ".active-upgrade").exists()
            ):
                raise ActiveUpgradeError(
                    "installation has an active v4 upgrade plan; use status, finalize, or rollback"
                )
            if templates:
                return self._load_files(root, templates=True)
            with _shared_migration_lock(root):
                return self._load_files(root, templates=False)

    def _load_files(self, root: Path, *, templates: bool) -> ConfigSnapshot:
        raw: dict[str, dict[str, Any]] = {}
        sources: dict[str, Path] = {}
        for scope, specs in FILE_SCHEMAS.items():
            suffix = ".toml.example" if templates else ".toml"
            path = root / f"{scope}{suffix}"
            self._check_file_safety(path, specs, templates=templates)
            try:
                values = self._repository.read(path)
            except ValueError as exc:
                raise ConfigError(str(exc)) from None
            raw[scope] = self._validate_scope(path, values, specs, templates=templates)
            for spec in specs:
                sources[f"{scope}.{spec.path}"] = path
        snapshot = self._build_snapshot(root, raw, sources)
        self._validate_dependencies(snapshot, templates=templates)
        if not templates:
            self._snapshot = snapshot
        return snapshot

    def reload(self) -> ConfigSnapshot:
        with self._lock:
            self._snapshot = None
            return self.load()

    def load_for_migration(self) -> ConfigSnapshot:
        """Load while the caller already holds the exclusive migration lock."""
        with self._lock:
            self._reject_legacy_environment()
            snapshot = self._load_files(self.root, templates=False)
            self._snapshot = snapshot
            return snapshot

    def load_system(self):
        """Load the privileged system scope; runtime services never call this."""
        from .system import load_system_config

        return load_system_config(self.root)

    def import_system_manifest(self, path: str | Path):
        """Normalize one restricted import source into ``system.toml``."""
        from .system import import_system_manifest

        return import_system_manifest(self.root, path)

    @staticmethod
    def read_rss_users_file(path: str | Path) -> dict[str, dict[str, Any]]:
        """Read the legacy-compatible users scope through the config layer."""
        value = TomlRepository().read(Path(path))
        users = value.get("users") or {}
        if not isinstance(users, dict):
            raise ConfigError("invalid rss-users.toml users table")
        result: dict[str, dict[str, Any]] = {}
        for name, user in users.items():
            if not isinstance(name, str) or not name or not isinstance(user, dict):
                raise ConfigError("invalid rss-users.toml user entry")
            if set(user) - {"token", "series"}:
                raise ConfigError("unknown rss-users.toml user field")
            token, series = user.get("token"), user.get("series")
            if not isinstance(token, str) or not token or any(ord(c) < 32 for c in token):
                raise ConfigError("invalid rss-users.toml token")
            if not isinstance(series, list) or not all(isinstance(item, str) for item in series):
                raise ConfigError("invalid rss-users.toml series list")
            result[name] = {"token": token, "series": list(series)}
        return result

    @staticmethod
    def read_publish_gone_series(path: str | Path) -> list[str]:
        value = TomlRepository().read(Path(path))
        gone = (value.get("publish") or {}).get("gone_series")
        if not isinstance(gone, list) or not all(isinstance(item, str) for item in gone):
            raise ConfigError("invalid publish.toml gone_series")
        return list(gone)

    @staticmethod
    def write_rss_users_file(path: str | Path, users: Mapping[str, Mapping[str, Any]], mode: int) -> None:
        lines: list[str] = []
        for name, user in users.items():
            token = user.get("token")
            series = user.get("series")
            if not isinstance(token, str) or not isinstance(series, list):
                raise ConfigError("invalid rss-users.toml user entry")
            lines.extend((f"[users.{_toml_quote(str(name))}]", f"token = {_toml_quote(token)}"))
            lines.append("series = [" + ", ".join(_toml_quote(item) for item in series) + "]")
            lines.append("")
        atomic_write_bytes(Path(path), "\n".join(lines).encode("utf-8"), mode=mode)

    @staticmethod
    def write_publish_gone_series(path: str | Path, gone_series: list[str], mode: int) -> None:
        target = Path(path)
        content = target.read_text(encoding="utf-8")
        replacement = "gone_series = " + "[" + ", ".join(_toml_quote(item) for item in gone_series) + "]"
        updated, count = re.subn(r"(?m)^gone_series\s*=\s*\[[^\r\n]*\]\s*$", replacement, content, count=1)
        if count != 1:
            raise ConfigError("invalid publish.toml gone_series declaration")
        atomic_write_bytes(target, updated.encode("utf-8"), mode=mode)

    @staticmethod
    def write_file_bytes(path: str | Path, content: bytes, mode: int) -> None:
        atomic_write_bytes(Path(path), content, mode=mode)

    def _reject_legacy_environment(self) -> None:
        removed = sorted(REMOVED_LEGACY_ENV & set(self._environ))
        if removed:
            raise LegacyConfigError(
                f"legacy environment variable {removed[0]} belongs to removed rsync support; unset it"
            )
        found = sorted((set(LEGACY_ENV_MAP) | LEGACY_INPUT_ONLY) & set(self._environ))
        if self._environ.get("BILIBILI_PODCAST_INTERNAL_CONFIG_EXEC") == "1":
            found = [key for key in found if key not in {"PLAYWRIGHT_BROWSERS_PATH"}]
        if not found:
            return
        key = found[0]
        target = LEGACY_ENV_MAP.get(key, "migration input only")
        raise LegacyConfigError(
            f"legacy environment variable {key} is no longer a configuration source; "
            f"use {target}; run bilibili-podcast-config migrate"
        )

    def _check_file_safety(self, path: Path, specs: tuple[FieldSpec, ...], *, templates: bool) -> None:
        if not path.exists():
            return
        if path.is_symlink() and not templates:
            raise UnsafeConfigError(f"unsafe configuration file {path}: symlink")
        if templates or not any(spec.sensitive for spec in specs):
            return
        mode = stat.S_IMODE(path.stat().st_mode)
        risks = []
        if mode & stat.S_IROTH:
            risks.append("world-readable")
        if mode & stat.S_IWOTH:
            risks.append("world-writable")
        if mode & stat.S_IWGRP:
            risks.append("group-writable")
        if risks:
            raise UnsafeConfigError(f"unsafe configuration file {path}: {', '.join(risks)}")

    def _validate_scope(
        self,
        path: Path,
        data: dict[str, Any],
        specs: tuple[FieldSpec, ...],
        *,
        templates: bool,
    ) -> dict[str, Any]:
        expected = {spec.path for spec in specs}
        # ``users`` owns the complete subtree in rss-users.toml.
        actual = _leaf_paths(data)
        unknown = sorted(p for p in actual if p not in expected and not p.startswith("users."))
        if unknown:
            raise ConfigError(f"unknown configuration field {path}:{unknown[0]}")
        result: dict[str, Any] = {}
        for spec in specs:
            value = _nested_get(data, spec.path)
            if value is MISSING:
                if spec.required:
                    raise ConfigError(f"missing configuration field {path}:{spec.path}")
                value = spec.default
            if spec.required and isinstance(value, str) and not value.strip():
                raise ConfigError(f"missing configuration field {path}:{spec.path}")
            if spec.path == "users":
                value = data.get("users", {})
            if not _is_expected_type(value, spec.value_type):
                raise ConfigError(f"invalid configuration type {path}:{spec.path}")
            if spec.path == "manual_media.allowed_dirs" and not all(isinstance(item, str) for item in value):
                raise ConfigError(f"invalid configuration type {path}:{spec.path}")
            if spec.path in {"security.previous_cookie_names", "publish.gone_series"} and not all(
                isinstance(item, str) and item for item in value
            ):
                raise ConfigError(f"invalid configuration type {path}:{spec.path}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if isinstance(value, float) and not math.isfinite(value):
                    raise ConfigError(f"non-finite configuration value {path}:{spec.path}")
                if spec.minimum is not None and value < spec.minimum:
                    raise ConfigError(f"configuration value below minimum {path}:{spec.path}")
                if spec.maximum is not None and value > spec.maximum:
                    raise ConfigError(f"configuration value above maximum {path}:{spec.path}")
            if not templates and self._contains_placeholder(value):
                raise ConfigError(f"unreplaced placeholder {path}:{spec.path}")
            if self._contains_control_character(value):
                raise ConfigError(f"control character in configuration field {path}:{spec.path}")
            result[spec.path] = value
        return result

    @staticmethod
    def _contains_placeholder(value: Any) -> bool:
        if isinstance(value, str):
            return "<" in value and ">" in value
        if isinstance(value, Mapping):
            return any(ConfigManager._contains_placeholder(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(ConfigManager._contains_placeholder(item) for item in value)
        return False

    @staticmethod
    def _contains_control_character(value: Any) -> bool:
        if isinstance(value, str):
            return any(ord(char) < 32 for char in value)
        if isinstance(value, Mapping):
            return any(ConfigManager._contains_control_character(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(ConfigManager._contains_control_character(item) for item in value)
        return False

    @staticmethod
    def _validate_dependencies(snapshot: ConfigSnapshot, *, templates: bool) -> None:
        if templates:
            return
        if snapshot.web.server.enabled and not snapshot.web.security.password:
            raise ConfigError("missing enabled dependency web.toml:security.password")
        cookie_names = (
            snapshot.web.security.cookie_name,
            *snapshot.web.security.previous_cookie_names,
        )
        if len(cookie_names) != len(set(cookie_names)):
            raise ConfigError("duplicate web cookie name web.toml:security")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in cookie_names):
            raise ConfigError("invalid web cookie name web.toml:security")
        if any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", series)
            for series in snapshot.publish.publish.gone_series
        ):
            raise ConfigError("invalid gone series publish.toml:publish.gone_series")
        if snapshot.sync.logging.level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("invalid logging level sync.toml:logging.level")
        publish = snapshot.publish.publish
        if publish.enabled and not publish.media_base_url:
            raise ConfigError("missing enabled dependency publish.toml:publish")
        if publish.enabled:
            parsed = urlparse(publish.media_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ConfigError("invalid enabled URL publish.toml:publish.media_base_url")
        if publish.master_placeholder != "__MEDIA_PLACEHOLDER__":
            raise ConfigError("invalid master RSS placeholder publish.toml:publish.master_placeholder")
        required_paths = (
            snapshot.app.database.path,
            *(getattr(snapshot.app.paths, item.name) for item in fields(snapshot.app.paths)),
            snapshot.app.install.app_dir,
            snapshot.app.install.venv_bin,
            snapshot.app.executables.sync,
            snapshot.sync.paths.cookie_file,
            snapshot.sync.paths.lock_file,
            snapshot.sync.browser.user_data_root,
            snapshot.sync.browser.playwright_browsers_path,
            snapshot.scheduler.paths.systemd_dir,
            snapshot.scheduler.paths.cron_script_dir,
            snapshot.scheduler.paths.wrapper_dir,
        )
        if any(not configured_path.is_absolute() for configured_path in required_paths):
            raise ConfigError("runtime paths must be absolute in unified configuration")
        web_unit = snapshot.scheduler.units.web
        sync_glob = snapshot.scheduler.units.sync_glob
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", web_unit):
            raise ConfigError("invalid scheduler unit name scheduler.toml:units.web")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.@*-]+\.service", sync_glob)
            or sync_glob.count("*") != 1
        ):
            raise ConfigError("invalid scheduler unit glob scheduler.toml:units.sync_glob")
        sync_pattern = re.escape(sync_glob).replace(r"\*", ".*")
        if re.fullmatch(sync_pattern, web_unit):
            raise ConfigError("scheduler unit glob overlaps web unit scheduler.toml:units")
        unit_values = (
            str(snapshot.root), str(snapshot.app.install.app_dir),
            str(snapshot.app.install.venv_bin), str(snapshot.app.executables.sync),
            str(snapshot.scheduler.paths.systemd_dir),
        )
        if any(re.search(r'[\s"\\]', value) for value in unit_values):
            raise ConfigError("systemd configuration paths contain unsupported characters")
        if snapshot.manual_media.enabled:
            if not snapshot.manual_media.allowed_dirs:
                raise ConfigError("missing enabled dependency manual-media.toml:manual_media.allowed_dirs")
            for allowed in snapshot.manual_media.allowed_dirs:
                if not allowed.is_absolute():
                    raise ConfigError("manual media directory must be absolute manual-media.toml:manual_media.allowed_dirs")
                resolved = allowed.resolve(strict=False)
                if len(resolved.parts) < 3:
                    raise ConfigError("manual media directory is too broad manual-media.toml:manual_media.allowed_dirs")
                if _has_symlink_component(allowed) and not snapshot.manual_media.follow_symlinks:
                    raise ConfigError("manual media directory is a symlink manual-media.toml:manual_media.allowed_dirs")

    @staticmethod
    def _build_snapshot(root: Path, raw: dict[str, dict[str, Any]], sources: dict[str, Path]) -> ConfigSnapshot:
        a, s, w = raw["app"], raw["sync"], raw["web"]
        sch, pub, mm, ru = raw["scheduler"], raw["publish"], raw["manual-media"], raw["rss-users"]
        users: dict[str, RssUser] = {}
        rss_tokens: set[str] = set()
        for name, value in ru["users"].items():
            if not isinstance(name, str) or not name or not isinstance(value, Mapping):
                raise ConfigError("invalid RSS user entry rss-users.toml:users")
            if set(value) - {"token", "series"}:
                raise ConfigError(f"unknown RSS user field rss-users.toml:users.{name}")
            token = value.get("token")
            series = value.get("series")
            if not isinstance(token, str) or not isinstance(series, list) or not all(isinstance(x, str) for x in series):
                raise ConfigError(f"invalid RSS user entry rss-users.toml:users.{name}")
            if not token or any(ord(char) < 32 for char in token):
                raise ConfigError(f"invalid RSS user token rss-users.toml:users.{name}")
            if token in rss_tokens:
                raise ConfigError(f"duplicate RSS user token rss-users.toml:users.{name}")
            rss_tokens.add(token)
            if any(item != "all" and not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", item) for item in series):
                raise ConfigError(f"invalid RSS user series rss-users.toml:users.{name}")
            users[name] = RssUser(token=token, series=tuple(series))
        return ConfigSnapshot(
            root=root,
            app=AppConfig(
                database=DatabaseConfig(_path(a["database.path"])),
                paths=AppPathsConfig(*(_path(a[f"paths.{key}"]) for key in (
                    "media_root", "json_root", "rss_root", "published_rss_root",
                    "state_root", "log_dir", "fallback_log_dir", "secrets_dir",
                ))),
                install=InstallConfig(_path(a["install.app_dir"]), _path(a["install.venv_bin"])),
                executables=ExecutablesConfig(
                    _path(a["executables.sync"]),
                    a["executables.ffmpeg"],
                    a["executables.ffprobe"],
                    a["executables.bilibili_podcast_config"],
                ),
            ),
            sync=SyncConfig(
                downloads=DownloadConfig(s["downloads.max_per_run"], s["downloads.scheduled_max_per_run"], float(s["downloads.min_free_gb"])),
                paths=SyncPathsConfig(_path(s["paths.cookie_file"]), _path(s["paths.lock_file"])),
                browser=BrowserConfig(_path(s["browser.user_data_root"]), _path(s["browser.playwright_browsers_path"]), float(s["browser.login_wait_seconds"])),
                timeouts=TimeoutConfig(
                    s["timeouts.sync_seconds"], s["timeouts.preview_seconds"],
                    s["timeouts.publish_seconds"],
                ),
                logging=LoggingConfig(s["logging.level"].upper(), s["logging.retention_days"], s["logging.max_bytes"], s["logging.backup_count"]),
            ),
            web=WebConfig(
                server=WebServerConfig(w["server.enabled"], w["server.host"], w["server.port"]),
                security=WebSecurityConfig(
                    w["security.password"], w["security.https"], w["security.cookie_name"],
                    tuple(w["security.previous_cookie_names"]),
                    w["security.session_max_age_seconds"],
                ),
            ),
            scheduler=SchedulerConfig(
                runtime=SchedulerRuntimeConfig(sch["runtime.user"], sch["runtime.group"]),
                paths=SchedulerPathsConfig(_path(sch["paths.systemd_dir"]), _path(sch["paths.cron_script_dir"]), _path(sch["paths.wrapper_dir"])),
                units=SchedulerUnitsConfig(sch["units.web"], sch["units.sync_glob"]),
                command_timeout_seconds=sch["timeouts.command_seconds"],
            ),
            publish=PublishConfig(
                publish=PublishSettings(
                    pub["publish.enabled"], pub["publish.media_base_url"],
                    pub["publish.master_placeholder"], tuple(pub["publish.gone_series"]),
                ),
            ),
            manual_media=ManualMediaConfig(mm["manual_media.enabled"], tuple(_path(item) for item in mm["manual_media.allowed_dirs"]), mm["manual_media.follow_symlinks"]),
            rss_users=RssUsersConfig(MappingProxyType(users)),
            sources=MappingProxyType(sources),
        )

    def redacted(self, snapshot: ConfigSnapshot | None = None, *, scope: str | None = None) -> dict[str, Any]:
        snapshot = snapshot or self.load()
        result = _to_plain(snapshot)
        result.pop("root", None)
        result.pop("sources", None)
        sensitive = {f"{spec.owner}.{spec.path}" for specs in FILE_SCHEMAS.values() for spec in specs if spec.sensitive}
        for path in sensitive:
            parts = path.replace("manual-media", "manual_media").replace("rss-users", "rss_users").split(".")
            current: Any = result
            for part in parts[:-1]:
                if not isinstance(current, dict) or part not in current:
                    break
                current = current[part]
            else:
                if isinstance(current, dict) and parts[-1] in current:
                    current[parts[-1]] = "***"
        if scope:
            normalized = scope.replace("-", "_")
            if normalized not in result:
                raise ConfigError(f"unknown configuration scope: {scope}")
            return {normalized: result[normalized]}
        return result


_default_manager: ConfigManager | None = None
_default_manager_lock = threading.Lock()


def get_config(*, reload: bool = False) -> ConfigSnapshot:
    global _default_manager
    with _default_manager_lock:
        if _default_manager is None:
            _default_manager = ConfigManager()
    return _default_manager.reload() if reload else _default_manager.load()
