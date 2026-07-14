#!/usr/bin/env bash
# Deploy Bilibili Podcast from one validated BILIBILI_PODCAST_CONFIG_ROOT.
# Dry-run is the default; --apply performs backups, code/dependency update,
# config validation, and scheduler regeneration. It never restarts services.

set -euo pipefail

APPLY=false
if [ "${1:-}" = "--apply" ]; then
    APPLY=true
elif [ -n "${1:-}" ]; then
    echo "ERROR: usage: scripts/deploy.sh [--apply]" >&2
    exit 2
fi

if [ -z "${BILIBILI_PODCAST_CONFIG_ROOT:-}" ]; then
    echo "ERROR: BILIBILI_PODCAST_CONFIG_ROOT is required" >&2
    exit 2
fi
if ! command -v bilibili-podcast-config >/dev/null 2>&1; then
    echo "ERROR: bilibili-podcast-config is required for deployment bootstrap" >&2
    exit 2
fi
if [ ! -d "$BILIBILI_PODCAST_CONFIG_ROOT" ]; then
    echo "ERROR: BILIBILI_PODCAST_CONFIG_ROOT does not exist" >&2
    exit 2
fi

CONFIG_ROOT="$(cd "$BILIBILI_PODCAST_CONFIG_ROOT" && pwd)"
CONFIG_JSON="$(bilibili-podcast-config show --format json)"

config_value() {
    python3 -c '
import json, sys
value = json.loads(sys.argv[1])
for part in sys.argv[2].split("."):
    value = value[part]
if value in (None, "", "***"):
    raise SystemExit(f"missing deploy configuration: {sys.argv[2]}")
print(value)
' "$CONFIG_JSON" "$1"
}

CODE_DIR="$(config_value app.install.app_dir)"
VENV_BIN="$(config_value app.install.venv_bin)"
DB_PATH="$(config_value app.database.path)"
STATE_ROOT="$(config_value app.paths.state_root)"
WRAPPER_DIR="$(config_value scheduler.paths.wrapper_dir)"
SYSTEMD_DIR="$(config_value scheduler.paths.systemd_dir)"
CRON_USER="$(config_value scheduler.runtime.user)"
WEB_UNIT="$(config_value scheduler.units.web)"
SYNC_UNIT_GLOB="$(config_value scheduler.units.sync_glob)"
SYNC_TIMER_GLOB="${SYNC_UNIT_GLOB%.service}.timer"
PYTHON_BIN="$VENV_BIN/python3"
BACKUP_PARENT="$CONFIG_ROOT/.backups"
BACKUP_TEMPLATE="$BACKUP_PARENT/deploy-$(date +%Y%m%d_%H%M%S)-XXXXXXXX"
BACKUP_DIR="$BACKUP_TEMPLATE"

echo "Bilibili Podcast unified deployment"
echo "  mode: $([ "$APPLY" = true ] && echo apply || echo dry-run)"
echo "  config root: $CONFIG_ROOT"
echo "  code dir: $CODE_DIR"
echo "  database: $DB_PATH"

bilibili-podcast-config validate

if [ ! -d "$CODE_DIR/.git" ]; then
    echo "ERROR: configured app.install.app_dir is not a Git repository" >&2
    exit 2
fi
if ! id "$CRON_USER" >/dev/null 2>&1; then
    echo "ERROR: configured scheduler.runtime.user does not exist" >&2
    exit 2
fi
if [ ! -x "$PYTHON_BIN" ]; then
    echo "ERROR: configured Python executable does not exist" >&2
    exit 2
fi

echo "Plan:"
echo "  1. back up TOML, SQLite, systemd units, and wrappers to $BACKUP_DIR"
echo "  2. git pull --ff-only in $CODE_DIR"
echo "  3. install the project using the configured virtualenv"
echo "  4. validate config and compile Python sources"
echo "  5. regenerate cron wrappers using only BILIBILI_PODCAST_CONFIG_ROOT"
echo "  6. leave all services stopped/running exactly as they are"

if [ "$APPLY" != true ]; then
    echo "Dry-run complete; no files or services were changed."
    exit 0
fi

umask 077
mkdir -p "$BACKUP_PARENT"
BACKUP_DIR="$(mktemp -d "$BACKUP_TEMPLATE")"
mkdir -p "$BACKUP_DIR/config" "$BACKUP_DIR/systemd" "$BACKUP_DIR/wrappers"
cp "$CONFIG_ROOT"/*.toml "$BACKUP_DIR/config/"
if [ -f "$DB_PATH" ]; then
    "$PYTHON_BIN" -c '
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.backup(target)
' "$DB_PATH" "$BACKUP_DIR/$(basename "$DB_PATH")"
fi
for unit in \
    "$SYSTEMD_DIR/$WEB_UNIT" \
    "$SYSTEMD_DIR"/$SYNC_UNIT_GLOB \
    "$SYSTEMD_DIR"/$SYNC_TIMER_GLOB \
    "$SYSTEMD_DIR"/bilibili-podcast-retry@*.service \
    "$SYSTEMD_DIR"/bilibili-podcast-retry@*.timer; do
    [ -f "$unit" ] || continue
    cp "$unit" "$BACKUP_DIR/systemd/"
done

for wrapper in "$WRAPPER_DIR"/run_*.sh; do
    [ -f "$wrapper" ] || continue
    cp "$wrapper" "$BACKUP_DIR/wrappers/"
done

find "$BACKUP_DIR" -type f ! -name SHA256SUMS -exec shasum -a 256 {} \; > "$BACKUP_DIR/SHA256SUMS"
test -s "$BACKUP_DIR/SHA256SUMS"
shasum -a 256 -c "$BACKUP_DIR/SHA256SUMS"

git -C "$CODE_DIR" pull --ff-only
"$VENV_BIN/pip" install -c "$CODE_DIR/requirements.lock" -e "$CODE_DIR"

bilibili-podcast-config validate
PYTHONPATH="$CODE_DIR/src" "$PYTHON_BIN" -m compileall -q "$CODE_DIR/src"

mkdir -p "$STATE_ROOT" "$WRAPPER_DIR"
PYTHONPATH="$CODE_DIR/src" "$PYTHON_BIN" "$CODE_DIR/scripts/bilibili-podcast-crontab" \
    --config-db "$DB_PATH" \
    --script-dir "$WRAPPER_DIR" \
    --cron-user "$CRON_USER" \
    --apply \
    --force

bilibili-podcast-config validate
echo "Deployment completed without service restart."
echo "Backup: $BACKUP_DIR"
echo "Next gate: review unit diffs and health checks before separately authorizing restart or production sync."
