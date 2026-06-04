# bilipod环境变量 — 供 bilipod-crontab 生成 wrapper 脚本时使用
# source 此文件后再运行 bilipod-crontab 确保新 series 有正确配置

export BILIPOD_SYNC_PATH=/opt/bilipod/venv/bin/bilibili-podcast
export BILIPOD_COOKIE_FILE=/opt/bilipod/secrets/www.bilibili.com_cookies.txt
export BILIPOD_MEDIA_BASE_URL=<media_base_url>
export BILIPOD_BROWSER_USER_DATA_ROOT=/opt/bilipod/browser-profiles
export PLAYWRIGHT_BROWSERS_PATH=/opt/bilipod/playwright-browsers
export BILIPOD_LOCK_FILE=/var/lib/bilipod/state/bilibili-podcast.lock
export BILIPOD_LOG_DIR=/var/log/bilipod
export BILIPOD_RSYNC_HOST=<rsync_host>
export BILIPOD_RSYNC_PORT=<rsync_port>
export BILIPOD_RSYNC_USER=publish
export BILIPOD_RSYNC_SECRET=/opt/bilipod/secrets/rsync_password
export BILIPOD_RSYNC_RSS_SRC="/var/lib/bilipod/published-rss/<token>/*.xml"
