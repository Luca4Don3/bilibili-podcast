#!/usr/bin/env bash
# rss-publish-and-sync.sh — Run local RSS publish + rsync to legacy RSS target.
#
# Called as ExecStartPost by systemd service units.
# Environment variables come from sourcing bilipod-env.sh below.
# Do not use bilipod-env.sh as a systemd EnvironmentFile because it uses
# shell-style "export KEY=value" syntax.
#
# Required env (set in bilipod-env.sh):
#   BILIPOD_RSYNC_HOST   e.g. rss.example.com
#   BILIPOD_RSYNC_PORT   e.g. 51873
#   BILIPOD_RSYNC_USER   e.g. publish
#   BILIPOD_RSYNC_SECRET path to rsync password file
#   BILIPOD_RSYNC_RSS_SRC  source path pattern, e.g. /var/lib/bilipod/published-rss/<token>/*.xml

set -euo pipefail

# Script directory (same dir as rss-publish.sh and bilipod-env.sh)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Source environment file (systemd EnvironmentFile= does not understand
# "export KEY=value" lines, so the script must source it explicitly).
ENV_FILE="${SCRIPT_DIR}/bilipod-env.sh"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# 1. Local RSS publish (user token distribution)
if [ ! -x "${SCRIPT_DIR}/rss-publish.sh" ]; then
    echo "ERROR: ${SCRIPT_DIR}/rss-publish.sh not found or not executable" >&2
    exit 1
fi
"${SCRIPT_DIR}/rss-publish.sh"

# 2. rsync to legacy RSS target
RSYNC_HOST="${BILIPOD_RSYNC_HOST:-}"
RSYNC_PORT="${BILIPOD_RSYNC_PORT:-51873}"
RSYNC_USER="${BILIPOD_RSYNC_USER:-publish}"
RSYNC_SECRET="${BILIPOD_RSYNC_SECRET:-}"
RSYNC_SRC="${BILIPOD_RSYNC_RSS_SRC:-}"

if [ -z "$RSYNC_HOST" ] || [ -z "$RSYNC_SRC" ]; then
    echo "ERROR: BILIPOD_RSYNC_HOST or BILIPOD_RSYNC_RSS_SRC not set" >&2
    exit 1
fi
if [ ! -f "$RSYNC_SECRET" ]; then
    echo "ERROR: rsync password file not found: $RSYNC_SECRET" >&2
    exit 1
fi

RSYNC_PASSWORD="$(cat "$RSYNC_SECRET")"
export RSYNC_PASSWORD
rsync -qt --port="${RSYNC_PORT}" --contimeout=10 \
    ${RSYNC_SRC} \
    "${RSYNC_USER}@${RSYNC_HOST}::rss/"
