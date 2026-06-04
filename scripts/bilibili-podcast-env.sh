# bilibili-podcast环境变量 — 供 bilibili-podcast-crontab 生成 wrapper 脚本时使用
# source 此文件后再运行 bilibili-podcast-crontab 确保新 series 有正确配置

export BILIBILI_PODCAST_SYNC_PATH=<server_path>
export BILIBILI_PODCAST_COOKIE_FILE=<server_path>
export BILIBILI_PODCAST_MEDIA_BASE_URL=<media_base_url>
export BILIBILI_PODCAST_BROWSER_USER_DATA_ROOT=<server_path>
export PLAYWRIGHT_BROWSERS_PATH=<server_path>
export BILIBILI_PODCAST_LOCK_FILE=/var/lib/bilibili-podcast/state/bilibili-podcast.lock
export BILIBILI_PODCAST_LOG_DIR=/var/log/bilibili-podcast
export BILIBILI_PODCAST_RSYNC_HOST=<rsync_host>
export BILIBILI_PODCAST_RSYNC_PORT=<rsync_port>
export BILIBILI_PODCAST_RSYNC_USER=publish
export BILIBILI_PODCAST_RSYNC_SECRET=<server_path>
export BILIBILI_PODCAST_RSYNC_RSS_SRC="/var/lib/bilibili-podcast/published-rss/<token>/*.xml"
