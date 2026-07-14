"""Atomic, generation-based RSS publisher."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

from .config.models import ConfigSnapshot


LOGGER = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Publishing failed before the active generation was switched."""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@contextmanager
def _publish_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".publish.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validated_master(path: Path, tokens: tuple[str, ...], placeholder: str) -> bytes:
    try:
        payload = path.read_bytes()
        ET.fromstring(payload)
    except (OSError, ET.ParseError) as exc:
        raise PublishError(f"invalid master RSS {path}: {exc}") from exc
    for token in tokens:
        if token.encode() in payload:
            raise PublishError(f"master RSS contains a configured user token: {path}")
    if placeholder.encode() not in payload:
        raise PublishError(f"master RSS has no media placeholder: {path}")
    return payload


def publish(snapshot: ConfigSnapshot) -> str:
    """Build and atomically activate one complete user RSS generation."""
    settings = snapshot.publish.publish
    if not settings.enabled:
        return ""
    output_root = snapshot.app.paths.published_rss_root
    generations = output_root / ".generations"
    gone_series = set(settings.gone_series)
    masters = sorted(
        path for path in snapshot.app.paths.rss_root.glob("*.xml")
        if path.stem not in gone_series
    )
    if not masters:
        raise PublishError("no active master RSS files were found")
    users = tuple(snapshot.rss_users.users.values())
    if not users:
        raise PublishError("no RSS users are configured")
    tokens = tuple(user.token for user in users)
    generation = f"{time.time_ns()}-{uuid.uuid4().hex[:12]}"

    with _publish_lock(output_root):
        generations.mkdir(parents=True, exist_ok=True)
        staging = generations / f".staging-{generation}"
        final = generations / generation
        staging.mkdir(mode=0o750)
        try:
            master_payloads = {
                path.stem: _validated_master(path, tokens, settings.master_placeholder)
                for path in masters
            }
            for user in users:
                missing = set(user.series) - {"all"} - set(master_payloads) - gone_series
                if missing:
                    raise PublishError(
                        f"RSS authorization references missing series: {sorted(missing)[0]}"
                    )
                allowed = set(master_payloads) if "all" in user.series else set(user.series)
                user_root = staging / token_digest(user.token)
                user_root.mkdir(mode=0o750)
                for series in sorted(allowed & set(master_payloads)):
                    target = user_root / f"{series}.xml"
                    target.write_bytes(
                        master_payloads[series].replace(
                            settings.master_placeholder.encode(), user.token.encode()
                        )
                    )
                    # Nginx reads published RSS through the dedicated service
                    # group; other local users must not be able to read tokens.
                    target.chmod(0o640)
                    ET.parse(target)
                    _fsync_file(target)
                _fsync_dir(user_root)
            _fsync_dir(staging)
            staging.rename(final)
            _fsync_dir(generations)

            link = output_root / ".current-new"
            current = output_root / "current"
            link.unlink(missing_ok=True)
            link.symlink_to(Path(".generations") / generation, target_is_directory=True)
            os.replace(link, current)
            try:
                _fsync_dir(output_root)
                retained = sorted(
                    (item for item in generations.iterdir() if item.is_dir() and not item.name.startswith(".staging-")),
                    key=lambda item: item.name,
                    reverse=True,
                )
                for obsolete in retained[2:]:
                    shutil.rmtree(obsolete)
            except OSError as exc:
                # ``current`` already points at a complete fsynced generation.
                # Retention cleanup is retryable and must not make callers roll
                # back source media after a successful activation.
                LOGGER.warning("RSS generation activated but cleanup failed: %s", type(exc).__name__)
            return generation
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def main() -> int:
    from .config import ConfigError, ConfigManager

    try:
        generation = publish(ConfigManager().load())
    except (ConfigError, OSError, PublishError) as exc:
        print(f"publish error: {exc}", file=__import__("sys").stderr)
        return getattr(exc, "exit_code", 3)
    if generation:
        print(generation)
    return 0
