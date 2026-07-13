#!/usr/bin/env bash
# rss-publish-and-sync.sh — Compatibility wrapper for local RSS publishing.
#
# Configuration is injected only by ``bilibili-podcast-config exec --scope publish``.

set -euo pipefail

# Script directory (same dir as rss-publish.sh)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "${1:-}" != "--configured" ]; then
    if [ -z "${BILIBILI_PODCAST_CONFIG_ROOT:-}" ]; then
        echo "ERROR: BILIBILI_PODCAST_CONFIG_ROOT is required" >&2
        exit 2
    fi
    exec bilibili-podcast-config exec --scope publish -- "$0" --configured
fi

# 1. Local RSS publish (user token distribution)
if [ ! -x "${SCRIPT_DIR}/rss-publish.sh" ]; then
    echo "ERROR: ${SCRIPT_DIR}/rss-publish.sh not found or not executable" >&2
    exit 1
fi
"${SCRIPT_DIR}/rss-publish.sh"
