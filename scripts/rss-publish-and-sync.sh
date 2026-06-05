#!/usr/bin/env bash
# rss-publish-and-sync.sh — Run local RSS publish + rsync to legacy RSS target.
#
# Called as ExecStartPost by systemd service units.
# Environment variables come from sourcing bilibili-podcast-env.sh below.
# Do not use bilibili-podcast-env.sh as a systemd EnvironmentFile because it uses
# shell-style "export KEY=value" syntax.
#
# Required env (set in bilibili-podcast-env.sh):
#   BILIBILI_PODCAST_RSYNC_HOST   e.g. rss.example.com
#   BILIBILI_PODCAST_RSYNC_PORT   e.g. 51873
#   BILIBILI_PODCAST_RSYNC_USER   e.g. publish
#   BILIBILI_PODCAST_RSYNC_SECRET path to rsync password file
#   BILIBILI_PODCAST_RSYNC_RSS_SRC  source path pattern, e.g. /var/lib/bilibili-podcast/published-rss/<token>/*.xml

set -euo pipefail

# Script directory (same dir as rss-publish.sh and bilibili-podcast-env.sh)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Source environment file (systemd EnvironmentFile= does not understand
# "export KEY=value" lines, so the script must source it explicitly).
ENV_FILE="${SCRIPT_DIR}/bilibili-podcast-env.sh"
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
RSYNC_HOST="${BILIBILI_PODCAST_RSYNC_HOST:-}"
RSYNC_PORT="${BILIBILI_PODCAST_RSYNC_PORT:-51873}"
RSYNC_USER="${BILIBILI_PODCAST_RSYNC_USER:-publish}"
RSYNC_SECRET="${BILIBILI_PODCAST_RSYNC_SECRET:-}"
RSYNC_SRC="${BILIBILI_PODCAST_RSYNC_RSS_SRC:-}"

if [ -z "$RSYNC_HOST" ] || [ -z "$RSYNC_SRC" ]; then
    echo "ERROR: BILIBILI_PODCAST_RSYNC_HOST or BILIBILI_PODCAST_RSYNC_RSS_SRC not set" >&2
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
