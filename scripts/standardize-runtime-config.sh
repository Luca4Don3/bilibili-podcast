#!/usr/bin/env bash
# Migrate legacy configuration and regenerate canonical single-server units.

set -euo pipefail

APPLY=false
if [ "${1:-}" = "--apply" ]; then
    APPLY=true
elif [ -n "${1:-}" ]; then
    echo "ERROR: usage: scripts/standardize-runtime-config.sh [--apply]" >&2
    exit 2
fi

: "${BILIPOD_CONFIG_ROOT:?BILIPOD_CONFIG_ROOT is required}"
: "${BILIPOD_ENV_FILE:?BILIPOD_ENV_FILE is required as legacy migration input}"
: "${BILIPOD_WEB_ENV_FILE:?BILIPOD_WEB_ENV_FILE is required as legacy migration input}"
: "${BILIPOD_LEGACY_SERIES_DIR:?BILIPOD_LEGACY_SERIES_DIR is required}"
: "${RSS_USERS_CONF:?RSS_USERS_CONF is required as legacy migration input}"

cmd=(bilipod-config migrate
    --legacy-env "$BILIPOD_ENV_FILE"
    --legacy-web-env "$BILIPOD_WEB_ENV_FILE"
    --legacy-series-dir "$BILIPOD_LEGACY_SERIES_DIR"
    --legacy-rss-users "$RSS_USERS_CONF"
    --output-root "$BILIPOD_CONFIG_ROOT")

if [ "$APPLY" != true ]; then
    "${cmd[@]}"
    echo "Dry-run complete; generated configuration validated without writes."
    exit 0
fi

cmd+=(--apply)
"${cmd[@]}"

clean_env=(env -i PATH="$PATH" HOME="${HOME:-/tmp}" BILIPOD_CONFIG_ROOT="$BILIPOD_CONFIG_ROOT")
"${clean_env[@]}" bilipod-config validate
CONFIG_JSON="$("${clean_env[@]}" bilipod-config show --format json)"

config_value() {
    python3 -c '
import json, sys
value = json.loads(sys.argv[1])
for part in sys.argv[2].split("."):
    value = value[part]
print(value)
' "$CONFIG_JSON" "$1"
}

SYSTEMD_DIR="$(config_value scheduler.paths.systemd_dir)"
APP_DIR="$(config_value app.install.app_dir)"
VENV_BIN="$(config_value app.install.venv_bin)"
SYNC_BIN="$(config_value app.executables.sync)"
SERVICE_USER="$(config_value scheduler.runtime.user)"
SERVICE_GROUP="$(config_value scheduler.runtime.group)"
MAX_DOWNLOADS="$(config_value sync.downloads.scheduled_max_per_run)"
BACKUP_DIR="$(mktemp -d "$BILIPOD_CONFIG_ROOT/.backups/standardize-XXXXXXXX")"
chmod 700 "$BACKUP_DIR"

restore_units_on_error() {
    status=$?
    trap - ERR
    for backup in "$BACKUP_DIR"/*.service; do
        [ -f "$backup" ] || continue
        cp "$backup" "$SYSTEMD_DIR/$(basename "$backup")"
    done
    echo "ERROR: unit rewrite failed; original units were restored from $BACKUP_DIR" >&2
    exit "$status"
}
trap restore_units_on_error ERR

write_web_unit() {
    unit="$1"
    if [ ! -s "$unit" ]; then
        echo "ERROR: refusing to replace empty unit: $unit" >&2
        return 1
    fi
    cp "$unit" "$BACKUP_DIR/$(basename "$unit")"
    temp="$(mktemp "$SYSTEMD_DIR/.bilipod-web.XXXXXXXX")"
    {
        echo "[Unit]"
        echo "Description=Bilipod Web Manager"
        echo "After=network.target"
        echo
        echo "[Service]"
        echo "Type=simple"
        echo "User=$SERVICE_USER"
        echo "Group=$SERVICE_GROUP"
        echo "WorkingDirectory=$APP_DIR"
        echo "Environment=\"BILIPOD_CONFIG_ROOT=$BILIPOD_CONFIG_ROOT\""
        echo "ExecStart=$VENV_BIN/bilipod-web"
        echo "Restart=on-failure"
        echo
        echo "[Install]"
        echo "WantedBy=multi-user.target"
    } > "$temp"
    chmod 644 "$temp"
    mv "$temp" "$unit"
}

write_sync_unit() {
    unit="$1"
    if [ ! -s "$unit" ]; then
        echo "ERROR: refusing to replace empty unit: $unit" >&2
        return 1
    fi
    name="$(basename "$unit")"
    series="${name#bilipod-sync@}"
    series="${series%.service}"
    case "$series" in
        ""|*[!a-z0-9_-]*) echo "ERROR: invalid series in unit name: $name" >&2; exit 2 ;;
    esac
    cp "$unit" "$BACKUP_DIR/$name"
    temp="$(mktemp "$SYSTEMD_DIR/.${name}.XXXXXXXX")"
    {
        echo "[Unit]"
        echo "Description=Bilipod Sync — $series"
        echo "After=network.target"
        echo
        echo "[Service]"
        echo "Type=oneshot"
        echo "User=$SERVICE_USER"
        echo "Group=$SERVICE_GROUP"
        echo "WorkingDirectory=$APP_DIR"
        echo "Environment=\"BILIPOD_CONFIG_ROOT=$BILIPOD_CONFIG_ROOT\""
        echo "ExecStart=$SYNC_BIN --series $series --max-downloads-per-run $MAX_DOWNLOADS --token __MEDIA_PLACEHOLDER__ --apply"
        echo "Restart=no"
        echo "TimeoutStartSec=1800"
    } > "$temp"
    chmod 644 "$temp"
    mv "$temp" "$unit"
}

web_unit="$SYSTEMD_DIR/bilipod-web.service"
if [ -f "$web_unit" ]; then
    write_web_unit "$web_unit"
fi
for unit in "$SYSTEMD_DIR"/bilipod-sync@*.service; do
    [ -f "$unit" ] || continue
    write_sync_unit "$unit"
done

find "$BACKUP_DIR" -type f ! -name SHA256SUMS -exec shasum -a 256 {} \; > "$BACKUP_DIR/SHA256SUMS"
if [ -s "$BACKUP_DIR/SHA256SUMS" ]; then
    shasum -a 256 -c "$BACKUP_DIR/SHA256SUMS"
fi
"${clean_env[@]}" bilipod-config validate
trap - ERR

echo "Unified configuration and canonical units written."
echo "No systemd reload, restart, start, or enable action was performed."
echo "Backup: $BACKUP_DIR"
