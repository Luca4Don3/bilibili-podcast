#!/usr/bin/env bash
# Normalize Bilipod runtime configuration after deploy.
#
# This script keeps shell-sourced env files and systemd EnvironmentFile files
# separate:
#   - bilipod-env.sh may contain "export KEY=value" because shell scripts source it.
#   - systemd units must not use bilipod-env.sh as EnvironmentFile.
#   - web password must live in a restricted secrets env file, not in unit text.

set -euo pipefail

APPLY=false
if [ "${1:-}" = "--apply" ]; then
    APPLY=true
fi

APP_DIR="${BILIPOD_APP_DIR:-/opt/bilipod}"
SYSTEMD_DIR="${BILIPOD_SYSTEMD_DIR:-/etc/systemd/system}"
SECRETS_DIR="${BILIPOD_SECRETS_DIR:-$APP_DIR/secrets}"
WEB_UNIT="${BILIPOD_WEB_UNIT:-$SYSTEMD_DIR/bilipod-web.service}"
SYNC_UNIT_GLOB="${BILIPOD_SYNC_UNIT_GLOB:-$SYSTEMD_DIR/bilipod-sync@*.service}"
SHELL_ENV_FILE="${BILIPOD_ENV_FILE:-$APP_DIR/bilipod-env.sh}"
WEB_ENV_FILE="${BILIPOD_WEB_ENV_FILE:-$SECRETS_DIR/bilipod-web.env}"

log() {
    printf '%s\n' "$*"
}

run_or_print() {
    if [ "$APPLY" = true ]; then
        "$@"
    else
        printf '  (dry-run) '
        printf '%q ' "$@"
        printf '\n'
    fi
}

remove_sync_shell_envfile() {
    log "▶ Normalize sync systemd units"
    local found=false
    for unit in $SYNC_UNIT_GLOB; do
        [ -f "$unit" ] || continue
        found=true
        if grep -q "^EnvironmentFile=$SHELL_ENV_FILE$" "$unit"; then
            log "  remove invalid EnvironmentFile from $unit"
            if [ "$APPLY" = true ]; then
                cp "$unit" "$unit.bak-standardize"
                local tmp
                tmp="$(mktemp)"
                awk -v env_line="EnvironmentFile=$SHELL_ENV_FILE" '$0 != env_line { print }' "$unit" > "$tmp"
                cat "$tmp" > "$unit"
                rm -f "$tmp"
            else
                log "  (dry-run) sed -i '/^EnvironmentFile=<shell-env-file>$/d' $unit"
            fi
        fi
    done
    if [ "$found" != true ]; then
        log "  no sync unit files matched"
    fi
}

ensure_web_secret_file() {
    log "▶ Normalize web password secret"
    if [ "$APPLY" != true ]; then
        log "  (dry-run) create/update restricted env file: $WEB_ENV_FILE"
        return
    fi

    mkdir -p "$SECRETS_DIR"
    local password="${BILIPOD_WEB_PASSWORD:-}"
    if [ -z "$password" ] && [ -f "$WEB_UNIT" ]; then
        password="$(sed -n 's/^Environment=BILIPOD_WEB_PASSWORD=//p' "$WEB_UNIT" | tail -n 1)"
    fi
    if [ -z "$password" ] && [ -f "$WEB_ENV_FILE" ]; then
        password="$(sed -n 's/^BILIPOD_WEB_PASSWORD=//p' "$WEB_ENV_FILE" | tail -n 1)"
    fi
    if [ -z "$password" ]; then
        log "  skip: set BILIPOD_WEB_PASSWORD=<web_password> or provide $WEB_ENV_FILE to manage web password"
        return
    fi

    umask 077
    printf 'BILIPOD_WEB_PASSWORD=%s\n' "$password" > "$WEB_ENV_FILE"
    chown root:bilipod "$WEB_ENV_FILE" 2>/dev/null || true
    chmod 640 "$WEB_ENV_FILE"
    log "  web password stored in restricted EnvironmentFile"
}

normalize_web_unit() {
    log "▶ Normalize web systemd unit"
    if [ ! -f "$WEB_UNIT" ]; then
        log "  skip: $WEB_UNIT not found"
        return
    fi
    if [ "$APPLY" != true ]; then
        log "  (dry-run) replace direct BILIPOD_WEB_PASSWORD with EnvironmentFile=$WEB_ENV_FILE"
        log "  (dry-run) dedupe Environment=BILIPOD_HTTPS=1"
        return
    fi

    cp "$WEB_UNIT" "$WEB_UNIT.bak-standardize"
    awk -v env_file="$WEB_ENV_FILE" '
        /^Environment=BILIPOD_WEB_PASSWORD=/ {
            if (!web_env_written && have_env_file) {
                print "EnvironmentFile=" env_file
                web_env_written=1
            }
            next
        }
        /^EnvironmentFile=.*bilipod-web\.env$/ {
            if (!web_env_written) {
                print "EnvironmentFile=" env_file
                web_env_written=1
            }
            next
        }
        /^Environment=BILIPOD_HTTPS=1$/ {
            if (!https_written) {
                print
                https_written=1
            }
            next
        }
        { print }
    ' have_env_file="$([ -f "$WEB_ENV_FILE" ] && echo 1 || echo 0)" "$WEB_UNIT.bak-standardize" > "$WEB_UNIT"
}

verify_config() {
    log "▶ Verify runtime config"
    if grep -R "^EnvironmentFile=$SHELL_ENV_FILE$" $SYNC_UNIT_GLOB >/dev/null 2>&1; then
        log "  ERROR: sync units still reference shell env file as systemd EnvironmentFile" >&2
        exit 1
    fi
    if [ -f "$WEB_UNIT" ] && grep -q "^Environment=BILIPOD_WEB_PASSWORD=" "$WEB_UNIT"; then
        log "  ERROR: web password is still present in $WEB_UNIT" >&2
        exit 1
    fi
    log "  runtime config checks passed"
}

remove_sync_shell_envfile
ensure_web_secret_file
normalize_web_unit

if [ "$APPLY" = true ]; then
    systemctl daemon-reload
    if systemctl is-active -q bilipod-web.service 2>/dev/null; then
        systemctl restart bilipod-web.service
    fi
fi

verify_config
