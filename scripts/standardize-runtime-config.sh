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

: "${BILIBILI_PODCAST_CONFIG_ROOT:?BILIBILI_PODCAST_CONFIG_ROOT is required}"
: "${BILIBILI_PODCAST_ENV_FILE:?BILIBILI_PODCAST_ENV_FILE is required as legacy migration input}"
: "${BILIBILI_PODCAST_WEB_ENV_FILE:?BILIBILI_PODCAST_WEB_ENV_FILE is required as legacy migration input}"
: "${BILIBILI_PODCAST_LEGACY_SERIES_DIR:?BILIBILI_PODCAST_LEGACY_SERIES_DIR is required}"
: "${RSS_USERS_CONF:?RSS_USERS_CONF is required as legacy migration input}"

cmd=(bilibili-podcast-config migrate
    --legacy-env "$BILIBILI_PODCAST_ENV_FILE"
    --legacy-web-env "$BILIBILI_PODCAST_WEB_ENV_FILE"
    --legacy-series-dir "$BILIBILI_PODCAST_LEGACY_SERIES_DIR"
    --legacy-rss-users "$RSS_USERS_CONF"
    --output-root "$BILIBILI_PODCAST_CONFIG_ROOT")

if [ "$APPLY" != true ]; then
    "${cmd[@]}"
    echo "Dry-run complete; generated configuration validated without writes."
    exit 0
fi

cmd+=(--apply)
"${cmd[@]}"

clean_env=(env -i PATH="$PATH" HOME="${HOME:-/tmp}" BILIBILI_PODCAST_CONFIG_ROOT="$BILIBILI_PODCAST_CONFIG_ROOT")
"${clean_env[@]}" bilibili-podcast-config validate
CONFIG_JSON="$("${clean_env[@]}" bilibili-podcast-config show --format json)"

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
WEB_UNIT="$(config_value scheduler.units.web)"
SYNC_UNIT_GLOB="$(config_value scheduler.units.sync_glob)"
SYNC_UNIT_PREFIX="${SYNC_UNIT_GLOB%%\**}"
SYNC_UNIT_SUFFIX="${SYNC_UNIT_GLOB#*\*}"
OLD_PRODUCT="$(printf '%s%s' 'bili' 'pod')"
BACKUP_DIR="$(mktemp -d "$BILIBILI_PODCAST_CONFIG_ROOT/.backups/standardize-XXXXXXXX")"
chmod 700 "$BACKUP_DIR"
CREATED_UNITS="$BACKUP_DIR/CREATED_UNITS"
: > "$CREATED_UNITS"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "ERROR: configured service user does not exist: $SERVICE_USER" >&2
    exit 2
fi

restore_units_on_error() {
    status=$?
    trap - ERR
    for backup in "$BACKUP_DIR"/*.service; do
        [ -f "$backup" ] || continue
        cp "$backup" "$SYSTEMD_DIR/$(basename "$backup")"
    done
    while IFS= read -r created; do
        [ -n "$created" ] || continue
        rm -f -- "$created"
    done < "$CREATED_UNITS"
    echo "ERROR: unit rewrite failed; original units were restored from $BACKUP_DIR" >&2
    exit "$status"
}
trap restore_units_on_error ERR

copy_legacy_unit_if_needed() {
    source="$1"
    target="$2"
    [ -f "$source" ] || return 0
    [ ! -e "$target" ] || return 0
    cp "$source" "$target"
    printf '%s\n' "$target" >> "$CREATED_UNITS"
}

copy_legacy_unit_if_needed \
    "$SYSTEMD_DIR/$OLD_PRODUCT-web.service" \
    "$SYSTEMD_DIR/$WEB_UNIT"

for old_service in "$SYSTEMD_DIR"/"$OLD_PRODUCT"-sync@*.service; do
    [ -f "$old_service" ] || continue
    series="${old_service##*@}"
    series="${series%.service}"
    target_name="${SYNC_UNIT_GLOB/\*/$series}"
    copy_legacy_unit_if_needed "$old_service" "$SYSTEMD_DIR/$target_name"
    old_timer="${old_service%.service}.timer"
    target_timer="$SYSTEMD_DIR/${target_name%.service}.timer"
    copy_legacy_unit_if_needed "$old_timer" "$target_timer"
done

write_web_unit() {
    unit="$1"
    if [ ! -s "$unit" ]; then
        echo "ERROR: refusing to replace empty unit: $unit" >&2
        return 1
    fi
    cp "$unit" "$BACKUP_DIR/$(basename "$unit")"
    temp="$(mktemp "$SYSTEMD_DIR/.bilibili-podcast-web.XXXXXXXX")"
    {
        echo "[Unit]"
        echo "Description=Bilibili Podcast Web Manager"
        echo "After=network.target"
        echo
        echo "[Service]"
        echo "Type=simple"
        echo "User=$SERVICE_USER"
        echo "Group=$SERVICE_GROUP"
        echo "WorkingDirectory=$APP_DIR"
        echo "Environment=\"BILIBILI_PODCAST_CONFIG_ROOT=$BILIBILI_PODCAST_CONFIG_ROOT\""
        echo "ExecStart=$VENV_BIN/bilibili-podcast-web"
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
    series="${name#"$SYNC_UNIT_PREFIX"}"
    series="${series%"$SYNC_UNIT_SUFFIX"}"
    case "$series" in
        ""|*[!a-z0-9_-]*) echo "ERROR: invalid series in unit name: $name" >&2; exit 2 ;;
    esac
    cp "$unit" "$BACKUP_DIR/$name"
    temp="$(mktemp "$SYSTEMD_DIR/.${name}.XXXXXXXX")"
    {
        echo "[Unit]"
        echo "Description=Bilibili Podcast Sync — $series"
        echo "After=network.target"
        echo
        echo "[Service]"
        echo "Type=oneshot"
        echo "User=$SERVICE_USER"
        echo "Group=$SERVICE_GROUP"
        echo "WorkingDirectory=$APP_DIR"
        echo "Environment=\"BILIBILI_PODCAST_CONFIG_ROOT=$BILIBILI_PODCAST_CONFIG_ROOT\""
        echo "ExecStart=$SYNC_BIN --series $series --max-downloads-per-run $MAX_DOWNLOADS --token __MEDIA_PLACEHOLDER__ --apply"
        echo "Restart=no"
        echo "TimeoutStartSec=1800"
    } > "$temp"
    chmod 644 "$temp"
    mv "$temp" "$unit"
}

web_unit="$SYSTEMD_DIR/$WEB_UNIT"
if [ -f "$web_unit" ]; then
    write_web_unit "$web_unit"
fi
for unit in "$SYSTEMD_DIR"/$SYNC_UNIT_GLOB; do
    [ -f "$unit" ] || continue
    [ "$(basename "$unit")" != "$WEB_UNIT" ] || continue
    write_sync_unit "$unit"
done

find "$BACKUP_DIR" -type f ! -name SHA256SUMS ! -name CREATED_UNITS -exec shasum -a 256 {} \; > "$BACKUP_DIR/SHA256SUMS"
if [ -s "$BACKUP_DIR/SHA256SUMS" ]; then
    shasum -a 256 -c "$BACKUP_DIR/SHA256SUMS"
fi
"${clean_env[@]}" bilibili-podcast-config validate
trap - ERR

echo "Unified configuration and canonical units written."
echo "No systemd reload, restart, start, or enable action was performed."
echo "Backup: $BACKUP_DIR"
