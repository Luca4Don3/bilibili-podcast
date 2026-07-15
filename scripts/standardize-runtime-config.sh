#!/usr/bin/env bash
# Migrate legacy configuration and regenerate canonical single-server units.

set -euo pipefail

usage() {
    echo "ERROR: usage: scripts/standardize-runtime-config.sh [--apply] [--system-permissions] [--profile legacy-unversioned|legacy-v0] [--layout-manifest PATH] [--web-primary-port PORT --web-backup-port PORT]" >&2
    exit 2
}

APPLY=false
SYSTEM_PERMISSIONS=false
PROFILE="legacy-unversioned"
LAYOUT_MANIFEST=""
WEB_PRIMARY_PORT=""
WEB_BACKUP_PORT=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply)
            APPLY=true
            shift
            ;;
        --system-permissions)
            SYSTEM_PERMISSIONS=true
            shift
            ;;
        --profile)
            [ "$#" -ge 2 ] || usage
            PROFILE="$2"
            shift 2
            ;;
        --layout-manifest)
            [ "$#" -ge 2 ] || usage
            LAYOUT_MANIFEST="$2"
            shift 2
            ;;
        --web-primary-port)
            [ "$#" -ge 2 ] || usage
            WEB_PRIMARY_PORT="$2"
            shift 2
            ;;
        --web-backup-port)
            [ "$#" -ge 2 ] || usage
            WEB_BACKUP_PORT="$2"
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

if [ "$SYSTEM_PERMISSIONS" = true ] && [ "$APPLY" != true ]; then
    echo "ERROR: --system-permissions requires --apply" >&2
    exit 2
fi

case "$PROFILE" in
    legacy-unversioned)
        [ -z "$LAYOUT_MANIFEST" ] || {
            echo "ERROR: --layout-manifest is only valid with --profile legacy-v0" >&2
            exit 2
        }
        ;;
    legacy-v0)
        [ -n "$LAYOUT_MANIFEST" ] || {
            echo "ERROR: --profile legacy-v0 requires --layout-manifest" >&2
            exit 2
        }
        ;;
    *)
        echo "ERROR: unsupported migration profile: $PROFILE" >&2
        exit 2
        ;;
esac

validate_port() {
    label="$1"
    value="$2"
    case "$value" in
        ""|*[!0-9]*)
            echo "ERROR: $label must be an integer between 1 and 65535" >&2
            exit 2
            ;;
    esac
    if [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
        echo "ERROR: $label must be an integer between 1 and 65535" >&2
        exit 2
    fi
}

if { [ -n "$WEB_PRIMARY_PORT" ] && [ -z "$WEB_BACKUP_PORT" ]; } \
    || { [ -z "$WEB_PRIMARY_PORT" ] && [ -n "$WEB_BACKUP_PORT" ]; }; then
    echo "ERROR: --web-primary-port and --web-backup-port must be provided together" >&2
    exit 2
fi
if [ -n "$WEB_PRIMARY_PORT" ]; then
    validate_port "--web-primary-port" "$WEB_PRIMARY_PORT"
    validate_port "--web-backup-port" "$WEB_BACKUP_PORT"
    [ "$WEB_PRIMARY_PORT" != "$WEB_BACKUP_PORT" ] || {
        echo "ERROR: primary and backup Web ports must differ" >&2
        exit 2
    }
fi

: "${BILIBILI_PODCAST_CONFIG_ROOT:?BILIBILI_PODCAST_CONFIG_ROOT is required}"
: "${BILIBILI_PODCAST_ENV_FILE:?BILIBILI_PODCAST_ENV_FILE is required as legacy migration input}"
: "${BILIBILI_PODCAST_WEB_ENV_FILE:?BILIBILI_PODCAST_WEB_ENV_FILE is required as legacy migration input}"
: "${BILIBILI_PODCAST_LEGACY_SERIES_DIR:?BILIBILI_PODCAST_LEGACY_SERIES_DIR is required}"
: "${RSS_USERS_CONF:?RSS_USERS_CONF is required as legacy migration input}"

cmd=(bilibili-podcast-config migrate
    --profile "$PROFILE"
    --legacy-env "$BILIBILI_PODCAST_ENV_FILE"
    --legacy-web-env "$BILIBILI_PODCAST_WEB_ENV_FILE"
    --legacy-series-dir "$BILIBILI_PODCAST_LEGACY_SERIES_DIR"
    --legacy-rss-users "$RSS_USERS_CONF"
    --output-root "$BILIBILI_PODCAST_CONFIG_ROOT")

if [ -n "$LAYOUT_MANIFEST" ]; then
    cmd+=(--layout-manifest "$LAYOUT_MANIFEST")
fi

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
WEB_BACKUP_UNIT="${WEB_UNIT%.service}-backup.service"
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
if ! python3 -c 'import grp, sys; grp.getgrnam(sys.argv[1])' "$SERVICE_GROUP" 2>/dev/null; then
    echo "ERROR: configured service group does not exist: $SERVICE_GROUP" >&2
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
    [ -L "$source" ] && {
        echo "ERROR: refusing to copy symlinked legacy unit: $source" >&2
        return 1
    }
    [ -f "$source" ] || return 0
    [ ! -L "$target" ] || {
        echo "ERROR: refusing to replace symlinked unit: $target" >&2
        return 1
    }
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
    port="${2:-}"
    if [ -L "$unit" ]; then
        echo "ERROR: refusing to replace symlinked unit: $unit" >&2
        return 1
    fi
    if [ -e "$unit" ] && [ ! -s "$unit" ]; then
        echo "ERROR: refusing to replace empty unit: $unit" >&2
        return 1
    fi
    if [ -e "$unit" ]; then
        cp "$unit" "$BACKUP_DIR/$(basename "$unit")"
    else
        printf '%s\n' "$unit" >> "$CREATED_UNITS"
    fi
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
        if [ -n "$port" ]; then
            echo "ExecStart=$VENV_BIN/bilibili-podcast-web --host 127.0.0.1 --port $port"
        else
            echo "ExecStart=$VENV_BIN/bilibili-podcast-web"
        fi
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
    if [ -L "$unit" ]; then
        echo "ERROR: refusing to replace symlinked unit: $unit" >&2
        return 1
    fi
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
    write_web_unit "$web_unit" "$WEB_PRIMARY_PORT"
elif [ -n "$WEB_PRIMARY_PORT" ]; then
    echo "ERROR: canonical Web unit is missing: $web_unit" >&2
    false
fi
if [ -n "$WEB_BACKUP_PORT" ]; then
    write_web_unit "$SYSTEMD_DIR/$WEB_BACKUP_UNIT" "$WEB_BACKUP_PORT"
fi
for unit in "$SYSTEMD_DIR"/$SYNC_UNIT_GLOB; do
    [ -f "$unit" ] || continue
    [ "$(basename "$unit")" != "$WEB_UNIT" ] || continue
    [ "$(basename "$unit")" != "$WEB_BACKUP_UNIT" ] || continue
    write_sync_unit "$unit"
done

find "$BACKUP_DIR" -type f ! -name SHA256SUMS ! -name CREATED_UNITS -exec shasum -a 256 {} \; > "$BACKUP_DIR/SHA256SUMS"
if [ -s "$BACKUP_DIR/SHA256SUMS" ]; then
    shasum -a 256 -c "$BACKUP_DIR/SHA256SUMS"
fi
"${clean_env[@]}" bilibili-podcast-config validate

permission_cmd=("${clean_env[@]}" bilibili-podcast-config --root "$BILIBILI_PODCAST_CONFIG_ROOT" permissions)
if [ "$SYSTEM_PERMISSIONS" = true ]; then
    "${permission_cmd[@]}" --apply
else
    "${permission_cmd[@]}"
fi
trap - ERR

echo "Unified configuration and canonical units written."
if [ -n "$WEB_BACKUP_PORT" ]; then
    echo "Prepared Web units: $WEB_UNIT ($WEB_PRIMARY_PORT), $WEB_BACKUP_UNIT ($WEB_BACKUP_PORT)."
fi
echo "No systemd reload, restart, start, or enable action was performed."
echo "Backup: $BACKUP_DIR"
