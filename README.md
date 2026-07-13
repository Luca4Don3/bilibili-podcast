# bilibili-podcast — Bilibili 视频转播客 RSS

将 B 站 UP 主视频或合集/系列转换为播客 RSS 订阅源，支持音频下载、内容过滤、多用户分发。

应用配置统一存放在 `config/*.toml`，系列配置与同步状态以 SQLite 为唯一写源；YAML 仅用于显式迁移或回滚读取。

## 目录

- [快速开始](#快速开始)
- [统一配置](#统一配置)
- [CLI 参数](#cli-参数)
  - [bilibili-podcast](#bilibili-podcast)
  - [bilipod-crontab](#bilipod-crontab)
  - [bilipod-admin — 系列管理 CLI](#bilipod-admin--系列管理-cli)
- [配置来源](#配置来源)
- [系列配置文件](#系列配置文件)
- [过滤管线](#过滤管线)
- [抓取模式](#抓取模式)
- [浏览器回退](#浏览器回退)
- [Rate Limit 处理](#rate-limit-处理)
- [付费内容与抢先看](#付费内容与抢先看)
- [时长过滤](#时长过滤)
- [状态管理](#状态管理)
- [进程安全](#进程安全)
- [日志系统](#日志系统)
- [Cron 自动调度](#cron-自动调度)
- [Systemd 自动调度](#systemd-自动调度)
- [RSS 多用户分发](#rss-多用户分发)
- [部署架构](#部署架构)
- [环境变量](#环境变量)
- [依赖安装](#依赖安装)

## 快速开始

### 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 单次运行

```bash
export BILIPOD_CONFIG_ROOT=<server_path>/config
bilipod-config validate
bilibili-podcast --series demo-series --token "__MEDIA_PLACEHOLDER__" --apply
```

不带 `--apply` 时为干跑模式。路径、凭据和行为默认值来自统一配置；显式 CLI 参数仅覆盖当前一次运行。

## 统一配置

仓库只跟踪脱敏模板。复制 `config/*.toml.example` 为同名 `.toml` 后填写实际值，并将敏感文件权限设为 `600` 或服务组只读的 `640`。README 仍位于仓库根目录，配置目录不再放第二份 README。

| 文件 | 职责 |
|------|------|
| `app.toml` | SQLite、共享数据目录、安装目录和公共可执行文件 |
| `sync.toml` | 下载限制、Cookie、浏览器、日志和超时 |
| `web.toml` | Web 登录、HTTPS、Cookie、session 和监听配置 |
| `scheduler.toml` | cron/systemd 用户、目录、wrapper、unit 和超时 |
| `publish.toml` | master/published RSS、media URL 和本机发布脚本 |
| `manual-media.toml` | 手动媒体白名单和 symlink 策略 |
| `rss-users.toml` | 用户 token 与系列授权关系 |
| SQLite | 系列、来源、过滤、同步策略、调度和 `sync_state` |

配置根定位顺序是显式 `ConfigManager(root)`、`BILIPOD_CONFIG_ROOT`、可确认的仓库根 `config/`；找不到时明确失败。除 `BILIPOD_CONFIG_ROOT` 外，旧持久配置环境变量不再生效，检测到时会给出目标字段和迁移命令。

`sync.toml` 的 `timeouts.sync_seconds`、`preview_seconds`、`publish_seconds` 分别控制手动同步、预览和本机发布钩子；`scheduler.toml` 的 `timeouts.command_seconds` 控制 cron/systemd 管理命令。手动同步使用 `downloads.max_per_run`，生成的调度任务使用 `downloads.scheduled_max_per_run`。自定义 systemd 主任务名称时，`units.sync_glob` 必须是恰好含一个 `*` 的 `.service` 文件名模式。

```bash
bilipod-config validate
bilipod-config validate --templates
bilipod-config show --scope web --format json  # 永远脱敏
bilipod-config migrate \
  --legacy-env <server_path>/legacy.env \
  --legacy-web-env <server_path>/legacy-web.env \
  --legacy-series-dir <server_path>/series.d \
  --legacy-rss-users <server_path>/rss-users.conf \
  --output-root <server_path>/config             # 默认 dry-run
```

`migrate` 只有加 `--apply` 才写入；写入前会 staged 校验，并把被替换的配置和 SQLite 备份到 `config/.backups/`。真实生产值必须由旧配置迁移和人工核对产生，不要把模板占位符当作实际配置。

---

## CLI 参数

### bilibili-podcast

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--config-db` | (none) | SQLite 数据库路径，替代 `--config-dir` 和 `--state-root` |
| `--config-dir` | (none) | 显式启用 legacy YAML 回滚读取；不会自动回退 |
| `--series` | (all enabled) | 逗号分隔的系列 ID，不指定则处理所有 `enabled: true` 的系列 |
| `--cookie-file` | (none) | Netscape 格式 cookie 文件，用于 B 站 API 鉴权 |
| `--token` | (none) | RSS enclosure URL 中的 token 占位符，分发时替换为真实用户 token |
| `--media-root` | `app.toml` | MP3 媒体文件存储根目录 |
| `--json-root` | `app.toml` | 剧集元数据 JSON 存储根目录 |
| `--rss-root` | `app.toml` | RSS XML 输出目录 |
| `--media-base-url` | `publish.toml` | RSS enclosure URL 的基础 URL |
| `--lock-file` | `sync.toml` | 进程锁文件路径 |
| `--state-root` | `app.toml` | YAML 回滚模式状态目录 |
| `--max-downloads-per-run` | `sync.toml`（20） | 每次运行最大下载数，`-1` 无限制 |
| `--min-free-gb` | `sync.toml`（5.0） | 磁盘最小剩余空间 (GB)，不足时中止下载 |
| `--browser-fallback` | off | 启用 Playwright 浏览器回退（API 失败时用） |
| `--browser-user-data-root` | `sync.toml` | Playwright 浏览器 profile 目录 |
| `--browser-login-check` | off | 启动时用 Playwright 验证 cookie 登录状态 |
| `--browser-login-wait-seconds` | `sync.toml`（5.0） | 登录检查页面等待时间 |
| `--log-dir` | `app.toml` | 日志输出目录 |
| `--log-level` | `sync.toml`（`INFO`） | 日志级别：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`（大小写不敏感） |
| `--debug` | off | `--log-level DEBUG` 的兼容快捷方式，优先级高于 `--log-level` |
| `--force` | off | 跳过更新周期和 rate-limit cooldown 门控 |
| `--apply` | off | 实际写入文件和下载媒体，不带则为干跑 |
| `--publish-script` | (none) | 发布脚本路径，仅在 `--apply` 且全部系列同步成功后执行 |

### bilipod-crontab

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--config-dir` | (none) | 显式 legacy YAML 回滚目录 |
| `--config-db` | (none) | SQLite 数据库路径，替代 `--config-dir` |
| `--apply` | off | 写入 crontab 和生成 wrapper 脚本 |
| `--print` | on | 打印生成的 cron 条目（无 `--apply` 时默认） |
| `--script-dir` | `auto` | wrapper 脚本输出目录 |
| `--cron-user` | `bilipod` | crontab 用户名 |
| `--force` | off | 覆盖已存在的 wrapper 脚本 |

### bilipod-admin — 系列管理 CLI

用于 SSH 下快速管理系列、过滤规则、同步策略和定时任务。复用 SQLite 配置层，与网页管理后台共享数据。

**全局参数：**

| 参数 | 说明 |
|------|------|
| `--config-db PATH` | SQLite 数据库路径，仅覆盖当前命令；默认读取 `app.database.path` |
| `--yes` | 跳过低风险确认 |
| `--dry-run` | 只预览，不写 DB、不执行同步 |
| `--json` | JSON 格式输出，方便脚本消费 |
| `--quiet` | 预留的安静输出开关，错误仍清楚显示 |
| `--debug` | 预留的诊断输出开关；`preview` 会固定用 `--log-level DEBUG` 调用同步命令 |

全局参数放在子命令前，例如 `bilipod-admin --config-db /path/to/bilipod.db list`。部分高频确认参数也支持放在子命令后，见下方命令说明。

**系列管理：**

| 命令 | 说明 |
|------|------|
| `bilipod-admin list` | 列出所有系列及状态 |
| `bilipod-admin show <series>` | 显示系列完整配置（支持 `--json`） |
| `bilipod-admin remove-series <series>` | 预览系列移除计划，不执行删除 |
| `bilipod-admin remove-series <series> --apply` | 永久移除系列、调度、本地 media/JSON/RSS 和数据库记录 |
| `bilipod-admin remove-up --uid <uid>` | 预览该 UP UID 对应的全部系列移除计划 |
| `bilipod-admin remove-up --uid <uid> --apply` | 永久移除该 UP UID 对应的全部系列 |
| `bilipod-admin add` | 交互式新增系列（输入 B 站 URL/UID，按提示配置过滤、同步、cron） |
| `bilipod-admin edit <series>` | 交互式编辑系列（回车保留当前值） |

**非交互式新增系列：（参数足够时跳过交互）**

```bash
bilipod-admin add \
  --url "https://space.bilibili.com/123456" \
  --series demo-series \
  --title "Demo Series" \
  --author "Demo Author" \
  --exclude-keyword "访谈" \
  --include-keyword "商业史" \
  --keep-last 100 \
  --update-period 12h \
  --quality 64K \
  --cron "15 11 * * *" \
  --exclude-paid \
  --dry-run
```

非交互模式下需要 `--series` 和 `--title`/`--author`（或通过 `--url` 自动解析）。缺少必要参数且非 TTY 时报错退出。默认不覆盖已有系列，使用 `--update-existing` 允许更新。

`add` 支持的非交互参数包括：`--url`、`--series`、`--title`、`--author`、`--description`、`--cover-art`、`--category`、`--lang`、`--exclude-keyword`、`--include-keyword`、`--ad-keyword`、`--exclude-bvid`、`--ad-bvid`、`--keep-last`、`--update-period`、`--quality`、`--fetch-strategy`、`--format`、`--page-size`、`--incremental-page-size`、`--max-pages`、`--max-requests-per-series`、`--request-interval-seconds`、`--request-jitter-seconds`、`--rate-limit-cooldown-seconds`、`--cron`、`--exclude-paid`、`--update-existing`、`--dry-run`、`--yes`。

移除命令默认只预览，必须显式传入 `--apply` 才会执行。执行时会获取与同步进程相同的锁，再逐个系列移除 cron/systemd 调度；调度清理失败时不会继续删除该系列数据。成功后会删除 SQLite 中的系列及关联记录、本地 media/JSON 目录、master RSS、已发布的本地用户 RSS、cron wrapper、浏览器 profile，并从 `rss-users.toml` 的显式系列授权中移除该系列。

远端 RSS 节点不在该命令的控制范围内，远端 XML 不会自动删除。

`remove-series` 和 `remove-up` 还支持：`--apply`、`--yes`、`--media-root`、`--json-root`、`--rss-root`、`--published-rss-root`、`--cron-script-dir`、`--browser-user-data-root`、`--lock-file`、`--users-conf`。这些参数默认读取统一快照，并可作为单次 CLI 覆盖。

**过滤规则管理：**

| 命令 | 说明 |
|------|------|
| `bilipod-admin filters <series>` | 列出过滤规则（别名: `filters-show`, `fs`） |
| `bilipod-admin filters-add <series> --exclude-keyword "访谈"` | 追加黑名单关键词（别名: `fa`） |
| `bilipod-admin filters-add <series> --include-keyword "商业史"` | 追加白名单关键词 |
| `bilipod-admin filters-add <series> --ad-keyword "恰饭"` | 追加广告关键词 |
| `bilipod-admin filters-add <series> --exclude-bvid BVxxxx` | 追加排除 BVID |
| `bilipod-admin filters-add <series> --ad-bvid BVxxxx` | 追加广告 BVID |
| `bilipod-admin filters-add <series> --exclude-season-id 123456` | 追加排除合集 ID |
| `bilipod-admin filters-add <series> --exclude-paid` | 启用付费内容排除 |
| `bilipod-admin filters-remove <series> --exclude-keyword "访谈" [--delete]` | 禁用/删除匹配规则（默认禁用，加 `--delete` 物理删除；别名: `fdel`） |
| `bilipod-admin filters-disable <series> --rule-id 123` | 按 ID 禁用规则（别名: `fd`） |
| `bilipod-admin filters-enable <series> --rule-id 123` | 按 ID 启用规则（别名: `fe`） |
| `bilipod-admin filters-import <series> --type exclude_keyword --file keywords.txt` | 从文件批量导入。`--type` 可选: `exclude_keyword`, `include_keyword`, `ad_keyword`, `exclude_bvid`, `ad_bvid`, `exclude_season_id`（别名: `fi`） |

`filters-add` 额外支持子命令级 `--yes`；`filters-remove` 支持 `--delete` 物理删除，否则默认只禁用匹配规则。

**同步策略管理：**

| 命令 | 说明 |
|------|------|
| `bilipod-admin sync-policy show <series>` | 显示当前同步策略（支持 `--json`） |
| `bilipod-admin sync-policy set <series> --keep-last 100 --update-period 12h --quality 64K` | 修改指定字段，未传字段保持不变 |

支持修改的字段：`--page-size`、`--incremental-page-size`、`--max-pages`、`--max-requests-per-series`、`--request-interval`、`--request-jitter`、`--rate-limit-cooldown`、`--update-period`、`--format`、`--quality`、`--fetch-strategy`、`--keep-last`、`--browser-fallback`、`--browser-wait-min`、`--browser-wait-max`、`--browser-fallback-cooldown`、`--require-paid-confirmation`、`--min-duration`、`--max-duration`。

**定时任务管理：**

| 命令 | 说明 |
|------|------|
| `bilipod-admin cron show <series>` | 显示系列 DB 中的 cron 配置（支持 `--json`） |
| `bilipod-admin cron set <series> --schedule "15 11 * * *" --schedule "15 23 * * *"` | 修改 cron（仅写 DB，不安装系统 crontab） |
| `bilipod-admin cron plan` | 预览 cron 计划（使用临时目录，不写系统 crontab） |
| `bilipod-admin cron plan --cron-script-dir /path/to/auto` | 预览 cron 计划（指定目标目录后输出真实路径） |
| `bilipod-admin cron apply --cron-script-dir /path/to/auto` | 安装 crontab，wrapper 写入指定目录（生产部署推荐显式指定绝对路径） |
| `bilipod-admin cron apply --cron-script-dir /path/to/auto --yes` | 跳过确认，直接安装 |

> **注意**：`cron` 命令保留用于兼容和回滚场景。生产环境推荐使用 `scheduler --backend systemd` 管理定时器。DB 模式下，`cron plan/apply` 会跳过当前由 systemd timer 管理的系列，避免双调度。`cron apply` 的 `--cron-script-dir` 和 `--yes` 放在子命令后：`cron apply --cron-script-dir /path --yes`。全局 `--yes`（`bilipod-admin --yes cron apply`）也兼容。`cron plan` 默认使用临时目录，仅用于结构预览；传 `--cron-script-dir` 后输出真实路径，可与 `cron apply` 核对。

**调度管理：**

| 命令 | 说明 |
|------|------|
| `bilipod-admin scheduler plan` | 预览调度计划（默认 backend=cron） |
| `bilipod-admin scheduler plan --backend cron --cron-script-dir /path/to/auto` | 指定 backend + 目录预览 |
| `bilipod-admin scheduler apply --cron-script-dir /path/to/auto --yes` | 安装调度 |
| `bilipod-admin scheduler plan --backend systemd --series <series> --cron-script-dir /path/to/auto` | 预览指定 series 的 systemd timer/unit |
| `bilipod-admin scheduler apply --backend systemd --series <series> --cron-script-dir /path/to/auto --yes` | 指定 series 安装或刷新 systemd timer |
| `bilipod-admin scheduler status` | 显示所有系列调度状态 |
| `bilipod-admin scheduler status <series>` | 显示指定系列调度状态 |
| `bilipod-admin scheduler status --backend systemd` | 显示 systemd timer 状态（enabled/active） |
| `bilipod-admin scheduler set <series> --schedule "15 3 * * *" --yes` | 设置系列调度（仅写 DB，不安装） |
| `bilipod-admin scheduler set <series> --schedule "15 11 * * *" --retry-schedule "15 13 * * *" --yes` | 设置主调度和失败后的条件兜底调度 |
| `bilipod-admin scheduler disable --backend systemd --series <series> --cron-script-dir /path/to/auto --yes` | 禁用指定 series 的 systemd timer，并按需要恢复 cron 调度 |
| `bilipod-admin scheduler disable --backend systemd --series <series> --delete-units --yes` | 禁用并删除对应 systemd unit 文件 |

> **安全约束**：systemd backend 只应管理 `.timer`，不要手动触发生成的 `.service` 作为测试手段；不要把启用和立即运行合并成一步；timer 应保持 `Persistent=false`，避免补跑错过任务并触发额外 API 请求。需要验证时使用 `scheduler plan/status`、timer 状态、只读日志和 RSS token 扫描。

**付费/手动媒体管理：**

用于需要人工准备媒体文件的系列。命令会复用 SQLite 配置，不会自动下载手动媒体。

| 命令 | 说明 |
|------|------|
| `bilipod-admin paid refresh-metadata <series> --json-root /path/to/json` | 刷新 metadata JSON，不下载媒体 |
| `bilipod-admin paid refresh-metadata <series> --cookie-file /path/to/cookies.txt` | 使用指定 cookie 刷新 metadata |
| `bilipod-admin paid refresh-metadata <series> --bvid BVxxxxxxxxxx` | 仅刷新指定 BVID 的 metadata |
| `bilipod-admin paid refresh-metadata <series> --url <bilibili-video-url>` | 仅刷新指定视频 URL 的 metadata |
| `bilipod-admin paid list-missing <series> --json-root /path/to/json --media-root /path/to/media` | 列出已有 metadata 但缺少媒体的条目，只读 |
| `bilipod-admin paid attach-media <series> --bvid BVxxxxxxxxxx --server-path /path/to/file.mp3 --media-root /path/to/media` | 关联人工上传的 MP3 文件 |
| `bilipod-admin paid attach-media <series> --bvid BVxxxxxxxxxx --server-path /path/to/file.mp3 --replace` | 覆盖已有媒体文件 |
| `bilipod-admin paid add-item <series> --url <bilibili-video-url> --media-path /path/to/uploaded-media` | 从用户提供的媒体文件和 B 站视频页面新增一条手动媒体 |
| `bilipod-admin paid add-item <series> --ffmpeg-bin /path/to/ffmpeg --publish-script /path/to/publish.sh` | 指定转码命令和 RSS 重建后的发布脚本 |
| `bilipod-admin paid rebuild-rss <series> --json-root /path/to/json --media-root /path/to/media --rss-root /path/to/rss` | 从现有 metadata + media 重建 master RSS |

`attach-media` 只接受 MP3 文件，并校验 BVID。上传源文件必须位于部署环境配置的白名单目录内。`add-item` 可接受视频或其他 ffmpeg 支持的媒体格式，会调用 `ffmpeg` 转为当前 series 的 MP3 quality，使用视频页面拉取单条 metadata，并重建 master RSS。手动媒体文件名会使用该 series 当前 `sync.quality`，即 `{BVID}_{quality}.mp3`，不要对非 64K series 固定写 `_64K`。`rebuild-rss` 生成 master RSS 时使用 `__MEDIA_PLACEHOLDER__`，由 RSS 发布流程替换为用户专属 token。

```bash
bilipod-admin paid add-item <series> \
  --url "https://www.bilibili.com/video/BVxxxxxxxxxx/" \
  --media-path "/path/to/manual-media/input.mp4" \
  --media-root "/path/to/media" \
  --json-root "/path/to/json" \
  --rss-root "/path/to/rss" \
  --media-base-url "http://<media-host>:<port>"
```

**预览与同步：**

| 命令 | 说明 |
|------|------|
| `bilipod-admin preview <series>` | 执行干跑预览（使用当前 SQLite 配置，不下载、不写 RSS） |
| `bilipod-admin sync <series>` | 默认干跑模式 |
| `bilipod-admin sync <series> --apply` | 真正同步（需二次确认） |

**退出码规范：**

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 用户取消或校验失败 |
| `2` | 命令参数错误 |
| `4` | DB 读写失败 |
| `5` | 同步/cron apply 失败 |

---

## 配置来源

`ConfigManager(...).load()` 为每个进程生成一次不可变快照。TOML repository 负责应用配置，SQLite repository 负责系列与同步状态；旧 `ConfigStore` 和 `utils.series_config.SeriesConfig` 仅保留兼容 shim。

### SQLite 模式（默认）

- 系列配置与状态：`app.database.path` 指向的 SQLite
- Web、Admin、Sync、cron 和 systemd 共用同一路径
- `--config-db` 仍可作为单次覆盖，不再是持久配置入口

### YAML 回滚模式

- 只有显式传 `--config-dir <legacy-series-dir>` 时读取 YAML
- 不传时绝不扫描旧目录或当前工作目录
- `config/series.d/_template.yaml` 仅是脱敏格式模板

### SQLite 迁移

```bash
# 1. 迁移 YAML 配置和 JSON 状态到 SQLite
python3 scripts/migrate_yaml_to_sqlite \
  --config-dir configs/series.d \
  --state-root /path/to/state \
  --db-path /path/to/bilipod.db

# 2. 显式回滚读取（不会写回 YAML）
bilibili-podcast --config-dir <server_path>/legacy-series.d ...

# 3. 切换 cron wrapper
python3 scripts/bilipod-crontab \
  --config-db /path/to/bilipod.db \
  --script-dir auto --force --apply
```

**回滚**：恢复迁移前的 TOML、SQLite、unit 和 wrapper 备份，再显式用 `--config-dir` 读取旧 YAML；不要让生产命令静默回退。

### 生产部署

`scripts/deploy.sh` 实现一键部署，自动处理以下流程：

| 步骤 | 处理内容 |
|------|----------|
| 环境检查 | 检查 Git 仓库/remote、系统用户 `bilipod`、secrets、日志/数据目录 |
| Python 检测 + `_sqlite3` 编译 | 优先 Python 3.14，允许 3.13 回退；低于 3.13 时中止；缺失 `_sqlite3` 时尝试修复 |
| 拉取最新代码 | `git pull --ff-only` |
| 依赖安装 | `pip install -c requirements.lock -e .`；GitHub 不可达时自动回退 PyPI 安装 |
| 模块验证 | 验证 sqlite3/yaml/aiohttp/curl_cffi/feedgen/lxml/bilibili-api/yt-dlp 已就绪 |
| 运行配置标准化 | 将旧 env/RSS 用户配置迁为 TOML；unit 只保留 config root |
| DB 迁移 | YAML 配置 + JSON 状态 → SQLite（自动备份） |
| wrapper/调度准备 | 生成 auto/run_*.sh；可用于 cron 兼容路径，也可供 scheduler/systemd 使用 |
| 验证 | DB 配置计数 + `exclude_paid` 语义检查 + Web 健康检查；默认不访问 B 站 API |
| 清理 | 删除 `/tmp/Python-*` 编译残留（脚本末尾自动执行） |

**前置依赖**（脚本会检查并提示设置方式）：

| 依赖 | 说明 |
|------|------|
| Git 仓库 | 代码需已 clone 到服务器，remote origin 已配置 |
| 系统用户 | `bilipod` 服务用户（`useradd -r -s /sbin/nologin bilipod`） |
| Cookie | `www.bilibili.com_cookies.txt`（Netscape 格式，可选，无则降级浏览器模式） |
| Web 密码 | 写入权限受限的实际 `web.toml`；真实值不写入 Git |
| 磁盘空间 | media/json/rss/state 所在分区至少 5GB 可用 |

```bash
# 干跑预览（不修改任何文件）
ssh <deploy-host> 'sudo env BILIPOD_CONFIG_ROOT=<server_path>/config bash -s' < scripts/deploy.sh

# 实际执行
ssh <deploy-host> 'sudo env BILIPOD_CONFIG_ROOT=<server_path>/config bash -s -- --apply' < scripts/deploy.sh
```

首次部署先干跑确认步骤，再用 `--apply`。

部署脚本不会执行真实同步请求。生产部署后的进一步验证应以只读检查为主：确认部署版本、timer 状态、日志 warning/error、RSS 中媒体/图片/JSON URL 均包含 token 或占位符。服务器别名、真实路径、访问控制和日志清理等运维动作请放在不提交 git 的运维手册中维护。

#### 运行配置标准化

标准化脚本把旧 env、Web env、系列 YAML 和 RSS 用户文件视为只读迁移输入。它先执行 dry-run/校验；`--apply` 时生成实际 TOML、备份旧配置和 unit，并把 unit 改为只含 `BILIPOD_CONFIG_ROOT`。脚本不会显示密文。

可单独运行标准化脚本：

```bash
# 干跑
ssh <deploy-host> 'sudo env BILIPOD_CONFIG_ROOT=<server_path>/config BILIPOD_ENV_FILE=<server_path>/legacy.env BILIPOD_WEB_ENV_FILE=<server_path>/legacy-web.env BILIPOD_LEGACY_SERIES_DIR=<server_path>/series.d RSS_USERS_CONF=<server_path>/rss-users.conf bash -s' < scripts/standardize-runtime-config.sh

# 实际修复
ssh <deploy-host> 'sudo env BILIPOD_CONFIG_ROOT=<server_path>/config BILIPOD_ENV_FILE=<server_path>/legacy.env BILIPOD_WEB_ENV_FILE=<server_path>/legacy-web.env BILIPOD_LEGACY_SERIES_DIR=<server_path>/series.d RSS_USERS_CONF=<server_path>/rss-users.conf bash -s -- --apply' < scripts/standardize-runtime-config.sh
```

生产切换前必须先取得服务器上未跟踪的 `scripts/rss-publish.sh` 做只读审计，并确认其改为读取 `rss-users.toml`。该文件未审计前，不得宣称发布链已经全局覆盖，也不得执行生产切换。

---
## 系列配置文件

每个系列在 SQLite 中保存一组关联记录。以下 YAML 仅描述迁移/回滚格式；实际写入由 Admin/Web 进入 SQLite。

### 顶层字段

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `series` | string | 是 | 系列标识符，用于目录名和 RSS 文件名，只允许 `[a-z0-9_-]` |
| `enabled` | bool | 否 | 是否启用，默认 `true` |
| `title` | string | 是 | RSS 频道标题，播客客户端中显示的名称 |
| `description` | string | 否 | RSS 频道描述 |
| `author` | string | 是 | 作者名称，**必须是 B 站 UP 主名称** |
| `cover_art` | string | 否 | 封面图片 URL |
| `category` | string | 否 | iTunes 播客分类 |
| `subcategories` | list | 否 | iTunes 子分类列表 |
| `explicit` | bool | 否 | 是否包含 explicit 内容，默认 `false` |
| `lang` | string | 否 | 语言代码，默认 `"zh-CN"` |

### `source` — 数据来源

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `space_url` | string | 二选一 | B 站 UP 主空间链接，可从 URL 自动提取 UID |
| `uid` | int | 二选一 | B 站用户 UID，与 `space_url` 至少提供一个 |
| `type` | string | 否 | 抓取类型：`"space"`（默认，UP 主最新视频）或 `"season"` / `"series"`（合集/系列） |
| `sid` | int | type 为 season/series 时必需 | 合集/系列的 ID |

### `sync` — 同步与下载行为

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `page_size` | int | `20` | 首页每页拉取数量（最大 50） |
| `incremental_page_size` | int | `5` | 后续页每页拉取数量（增量补拉用较小批次） |
| `max_pages` | int | `10` | 最大拉取页数 |
| `max_requests_per_series` | int | `8` | 每个系列每次运行的最大 API 请求数 |
| `request_interval_seconds` | float | `2.0` | API 请求间隔（秒） |
| `request_jitter_seconds` | float | `0.5` | 请求间隔随机抖动范围（秒） |
| `update_period` | string | `"12h"` | 更新周期，支持 `"1h"`, `"30m"`, `"2h30m"`, `"1d"` 等 |
| `rate_limit_cooldown_seconds` | int | `21600` | 被限流后冷却时间（秒），默认 6 小时 |
| `format` | string | `"audio"` | 下载格式，固定 `"audio"` |
| `quality` | string | `"64K"` | 音频质量：`"64K"`, `"132K"`, `"192K"` |
| `keep_last` | int | `100` | RSS 中保留最近 N 条，`0` 不限制 |
| `fetch_strategy` | string | `"api_first"` | 抓取策略：`"api_first"` 或 `"browser_first"` |
| `browser_fallback` | bool | `false` | API 失败时是否回退到 Playwright 浏览器 |
| `browser_wait_min_seconds` | float | `4` | 浏览器页面最小等待秒数 |
| `browser_wait_max_seconds` | float | `8` | 浏览器页面最大等待秒数（实际随机取区间内值） |
| `browser_fallback_cooldown_seconds` | int | `3600` | 浏览器回退冷却时间（秒），避免 IP 被封 |
| `require_paid_state_confirmation` | bool | `false` | 是否要求确认付费状态后才加入 RSS |
| `min_duration_seconds` | int | `0` | 最小时长过滤（秒），`0` 不限 |
| `max_duration_seconds` | int | `0` | 最大时长过滤（秒），`0` 不限 |

### `cron` — 定时调度元数据

| 字段 | 类型 | 说明 |
|------|------|------|
| `enabled` | bool | 是否为此系列生成 cron 任务 |
| `schedules` | list | cron 表达式列表，如 `["15 11 * * *", "15 13 * * *"]` |

不指定 `schedules` 时根据 `sync.update_period` 自动推导。

### `filters` — 内容过滤

详见[过滤管线](#过滤管线)。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `exclude_paid` | bool | `true` | 是否排除付费/充电内容 |
| `exclude_bvids` | list | `[]` | 黑名单 BV 号 |
| `exclude_season_ids` | list[int] | `[]` | 黑名单合集 ID（season_id） |
| `exclude_keywords` | list | `[]` | 标题或简介包含任一关键词则排除 |
| `advertisement_bvids` | list | `[]` | 广告 BV 号黑名单 |
| `advertisement_keywords` | list | `[]` | 广告关键词，命中则排除 |
| `include_keywords` | list | `[]` | 白名单关键词，非空时仅保留命中项 |

### `paid_preview` — 付费抢先看

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 是否启用付费抢先看处理 |
| `retry_after_days` | int | `4` | 几天后重新检查付费状态 |

---

## 过滤管线

过滤器按固定顺序执行，每条视频命中任一过滤即停止后续检查。

### 执行顺序

1. **时长过滤** (`min_duration_seconds` / `max_duration_seconds`) — 超出范围则排除。当 `max <= min` 时自动跳过。
2. **付费确认** (`exclude_paid`) — 自动检测充电专属、付费合集等付费标记。
3. **付费未确认** (`require_paid_state_confirmation`) — 缺少付费状态信息的视频暂不保留。
4. **BV 黑名单** (`exclude_bvids` + `advertisement_bvids`) — 精确 BV 号匹配。
5. **合集黑名单** (`exclude_season_ids`) — 精确匹配空间列表 API 返回的 season_id。
6. **关键词黑名单** (`exclude_keywords`) — 标题或简介包含任一关键词则排除。
7. **广告关键词** (`advertisement_keywords`) — 标题或简介包含任一关键词则排除。
8. **白名单** (`include_keywords`) — 非空时，仅标题或简介匹配至少一个关键词的视频保留。

### 清理机制

被排除的视频自动清理对应的媒体文件和 JSON 元数据：

| 清理阶段 | 处理内容 |
|----------|----------|
| Paid cleanup | 扫描磁盘上已标记为付费的 JSON+media；清理当前 run 付费确认的 BVID |
| Duration cleanup | 扫描磁盘上超出时长范围的 JSON+media；清理当前 run 时长排除的 BVID |
| Excluded cleanup | 清理关键词/BV 排除的 BVID；清理孤立的 JSON 或 media 文件 |

`keep_last` 保留策略在每个系列中独立执行，保留最近 N 条已下载可播放视频，在此范围之外的旧条目按需清理。最终可播放结果不足时补位保留旧条目，不会因下载顺序导致有效条目过早丢失。

---

## 抓取模式

### space（默认）

从 UP 主空间 API 抓取，使用 `uid`。首次用 `page_size` 拉首页，后续页用较小的 `incremental_page_size` 增量补拉。

### season / series

从 B 站合集/系列 API 抓取，使用 `source.sid`。配置 `source.type: season` 或 `source.type: series`。每页固定 100 条。

### Fetch Strategies

| 策略 | 说明 |
|------|------|
| `api_first`（默认） | 优先用 B 站 API，失败时若启用 `browser_fallback` 则回退到 Playwright |
| `browser_first` | 直接使用 Playwright 浏览器抓取，需要 `browser_fallback` 和可用冷却窗口 |

---

## 浏览器回退

当 API 被限流或失败时，如果 `browser_fallback` 开启且冷却窗口已过，自动启动 headless Chromium：

- 使用持久化 browser context 复用登录会话
- 访问 UP 主空间页面，滚动加载视频卡片
- 从渲染 DOM 中解析视频链接（BV ID）
- 将浏览器标题/文本合并回 API 剧集（补全 API 缺失的标题或描述）
- 触发条件：API 被限流且过滤后数量不足，或付费状态信息不完整

**冷却机制**：两次浏览器回退之间有可配置的冷却时间（默认 3600 秒），避免触发 B 站反爬。

---

## Rate Limit 处理

B 站 API 返回 `-799` / "请求过于频繁" 时：

- 立即停止当前系列的后续请求
- 在状态中写入 `rate_limited_until` 时间戳
- 后续运行跳过该系列直到冷却期结束（默认 6 小时，可通过 `sync.rate_limit_cooldown_seconds` 配置）
- 若 `browser_fallback` 启用，可在被限流后回退到 Playwright 继续拉取

`--force` 可跳过所有冷却和周期门控。

---

## 付费内容与抢先看

对于有付费抢先看模式的 UP 主（如亚洲特快），新视频可能先标记为付费抢先看，几天后转为免费。

### `paid_preview` 模式

设置 `paid_preview.enabled: true` 时：

- 当前标记为付费的视频**不会永久排除**，仅本次跳过
- 下次运行时重新检查状态，若转为免费则自动纳入 RSS
- 通常配合 `exclude_paid: false` 使用，让 paid_preview 逻辑接管

### 付费状态确认

`require_paid_state_confirmation` 要求每条视频都必须有明确的付费状态信息。缺少状态信息时视频暂不保留。浏览器回退可以补全 API 缺失的状态字段。

---

## 时长过滤

通过 `min_duration_seconds` 和 `max_duration_seconds` 过滤视频时长。

### 时长解析

支持多种格式自动解析为秒：

| 输入格式 | 示例 | 说明 |
|----------|------|------|
| 整数（秒） | `180` | 直接使用 |
| `mm:ss` | `3:00` | 分:秒 |
| `hh:mm:ss` | `1:30:00` | 时:分:秒 |
| 后缀格式 | `"3m"`, `"1.5h"`, `"30s"` | 支持 s/m/h/d 单位 |

当 `max <= min` 且两者均大于 0 时，时长过滤自动跳过并输出 WARNING 日志。

---

## 状态管理

运行开始时检查每个系列的同步门控：

- 距上次成功同步未达到 `update_period` → 跳过
- 当前时间仍在 `rate_limited_until` 内 → 跳过
- 使用 `--force` 可绕过所有门控

### 存储方式

| 模式 | 存储位置 |
|------|----------|
| YAML 模式 | `{state_root}/{series}.json`，每系列独立文件 |
| SQLite 模式 | 同一 `bilipod.db` 的 `sync_state` 表 |

状态字段：

| 字段 | 说明 |
|------|------|
| `last_success_at` | 上次成功同步的 UTC 时间戳 |
| `last_attempt_at` | 上次尝试同步的 UTC 时间戳 |
| `last_browser_fallback_at` | 上次浏览器回退的 UTC 时间戳 |
| `rate_limited_until` | rate limit 冷却期结束的 UTC 时间戳 |

---

## 进程安全

基于 `fcntl.flock` 的排他锁防止并发运行：

- 如果另一个进程正在运行（如 cron 任务重叠），后续进程立即退出（退出码 2）
- 锁文件写入当前进程 PID，方便排查
- 进程退出时自动释放锁

---

## 日志系统

所有日志输出到 `--log-dir`；未显式覆盖时读取 `app.paths.log_dir`。

| 文件 | Logger | 级别 | 内容 |
|------|--------|------|------|
| `sync.log` | `bilibili_podcast.sync` | INFO/DEBUG | 主同步过程全量日志 |
| `sync.error.log` | `bilibili_podcast.sync` | ERROR | 仅错误级别，快速监控 |
| `playwright.log` | `bilibili_podcast.sync.playwright` | INFO/DEBUG | 浏览器回退日志 |

### 日志轮转

内建 `RotatingFileHandler`，无需系统 logrotate：

- 单文件上限：20 MB
- 轮转备份数：10 个
- 每类日志最大占用：约 220 MB（当前文件加 10 个备份）
- 轮转备份保留时间：30 天；同步进程启动时清理过期备份

### 日志级别

主同步 CLI 支持 `--log-level` 选择日志级别；`--debug` 保留为兼容快捷方式，等价于 `--log-level DEBUG` 且优先级更高。

| 级别 | 触发方式/来源 | 内容 |
|------|----------|------|
| DEBUG | `--log-level DEBUG` 或 `--debug` | 在 INFO 基础上增加：每页 API 请求 URL 和页码、每条剧集元数据写入、JSON 读写路径、磁盘空间检查、浏览器回退状态详情、合并/限制详情 |
| INFO（默认） | `--log-level INFO` 或不传 | 运行开始/完成、系列开始/完成、API 抓取汇总、过滤统计（total/kept/各类排除数）、下载开始/完成/跳过、RSS 写入、清理汇总 |
| WARNING | `--log-level WARNING` | 只记录 warning/error/critical，适合降低常规同步噪声 |
| ERROR | `--log-level ERROR` | 只记录失败级别事件；`sync.error.log` 也会接收这些记录 |
| CRITICAL | `--log-level CRITICAL` | 只记录不可恢复的进程级故障；当前代码路径很少直接使用 |

`sync.error.log` 始终只接收 `ERROR` 及以上级别，方便监控；`sync.log` 和 `playwright.log` 按 `--log-level` 控制最低记录级别。

systemd 使用 `sync.logging.level`。生成的 unit 不再嵌入日志、Cookie、路径或发布配置。

cron wrapper 用户可通过环境变量 `LOG_LEVEL`（默认 `INFO`）和 `DEBUG=1` 控制日志级别，`DEBUG=1` 优先级更高。

---

## Cron 自动调度

`bilipod-crontab` 可以从系列配置生成 cron 任务，主要用于兼容、迁移或回滚场景。新部署推荐使用下一节的 systemd 调度。

```bash
# YAML 模式
bilipod-crontab --config-dir configs/series.d --apply

# SQLite 模式（默认读取 app.database.path）
bilipod-crontab --force --apply

# 仅预览
bilipod-crontab --config-dir configs/series.d --print
```

`bilipod-crontab` 为每个启用了 cron 的系列生成独立的 wrapper 脚本：

- wrapper 脚本仅嵌入 `BILIPOD_CONFIG_ROOT`、series 和一次性控制参数
- `--config-dir` 只用于显式 YAML 回滚
- 支持 `MAX_DOWNLOADS_PER_RUN` / `FORCE` / `DEBUG` 环境变量覆盖
- 自动合并到 `--cron-user` 用户的 crontab（默认 `bilipod`）
- 已有 crontab 中由 `BEGIN BILIPOD AUTO` / `END BILIPOD AUTO` 标记的自动区域会被替换
- DB 模式会跳过标记为 systemd 后端或当前已有 enabled systemd timer 的系列

### 最佳实践

- 不同系列错开执行时间，避免短时间内大量 API 请求
- 每个系列每天至少 2 次（B 站更新通常每天 1-2 次）
- 周更系列可以设置仅周末运行

---

## Systemd 自动调度

生产环境推荐使用 `bilipod-admin scheduler --backend systemd` 为每个 series 生成独立的 `.service` 和 `.timer`。

```bash
# 只读预览
bilipod-admin scheduler plan \
  --backend systemd \
  --series <series> \
  --cron-script-dir /path/to/auto

# 安装或刷新指定 series 的 timer
bilipod-admin scheduler apply \
  --backend systemd \
  --series <series> \
  --cron-script-dir /path/to/auto \
  --yes

# 查看状态
bilipod-admin scheduler status --backend systemd
```

systemd 调度的安全约束：

- 主调度之间不得重复，且最短间隔不得小于该系列的 `update_period`；兜底调度必须通过 `--retry-schedule` 显式标记。
- 主调度成功后，兜底 timer 当天触发时只记录 `retry_not_needed`，不会请求 B 站；主调度失败后，兜底可绕过 `update_period` 尝试一次。
- `rate_limit_cooldown` 的优先级始终高于兜底调度，兜底不会绕过限流冷却。
- cron 仅作为 systemd 不可用时的手工兜底链路，默认不启用；cron backend 不支持条件兜底，存在 retry schedule 时 `plan/apply` 会显式失败。
- timer 使用 `Persistent=false`，避免开机或启用时补跑错过任务。
- service 命令必须带 `--token __MEDIA_PLACEHOLDER__`。
- 如果使用 RSS 多用户分发，service 的同步成功后按 `publish.toml` 触发发布脚本。
- 生成的 `.service` 只保留 `Environment=BILIPOD_CONFIG_ROOT=...`，不包含旧 env 文件或敏感值。
- 验证 timer 时只启动/刷新 `.timer`，不要手动启动 `.service`。
- 不要把启用和立即运行合并成一步；启用和启动 timer 应分开执行，并确认 timer active 后再移除旧调度。

---

## RSS 多用户分发

生成的 master RSS 使用占位符 token（`__MEDIA_PLACEHOLDER__`）。发布脚本会将 master RSS 分发为各用户专属 RSS，并把占位符替换为用户 token。

### 工作流程

```
/path/to/master-rss/{series}.xml          (master, 占位符)
         │
         ▼  publish script
/path/to/published-rss/{token1}/{series}.xml
/path/to/published-rss/{token2}/{series}.xml
         │
         │
         ▼  本机 RSS 服务
https://podcast.example.invalid/rss/<user_token>/{series}.xml
```

### 用户配置文件

`config/rss-users.toml` 使用具名用户表：

```toml
[users.example]
token = "<user_token>"
series = ["series1", "series2"]
```

真实 token 只放在权限受限且被 Git 忽略的实际 TOML。`show`、Web `/config`、日志和错误不会回显 token。

### 路径规范

| 位置 | 路径格式 |
|------|---------|
| 媒体文件 | `{media_root}/{series}/{bvid}_{quality}.mp3` |
| JSON 元数据 | `{json_root}/{series}/{bvid}_{quality}.info.json` |
| RSS 文件 | `{rss_root}/{series}.xml` |
| 状态文件（YAML 模式） | `{state_root}/{series}.json` |

---

## 部署架构

项目按单服务器部署：同步进程、Web 管理、SQLite、媒体文件、master RSS、用户 RSS 和对外 HTTP 服务位于同一台服务器。`scripts/rss-publish-and-sync.sh` 仅作为历史命令名保留，当前只执行本机 `rss-publish.sh`，不包含跨服务器同步。

未来如需向其他服务器分发，应单独设计带身份认证、完整性校验、失败重试和可观测性的发布接口；本项目不保留 rsync 执行链或配置。

### 数据流

```
播客客户端
  → 本机 RSS 服务 (https://podcast.example.invalid/rss/{token}/{series}.xml)
    → 解析 enclosure URL
      → 本机媒体服务 (https://media.example.invalid/media/{series}/{bvid}_{quality}.mp3?token=xxx)
```

### 文件所有权

- media / json / rss / state 目录属主：`bilipod:bilipod`
- 目录权限：`755`
- 文件权限：`644`
- Cookie、token 和 Web 密码所在 TOML：`600` 或服务组只读的 `640`

---

## 环境变量

唯一的持久配置 bootstrap 是 `BILIPOD_CONFIG_ROOT`。`FORCE`、`DEBUG`、`SMOKE_SYNC` 仍是一次性运行/部署控制；`PATH` 只参与系统命令发现。cron 的 `MAX_DOWNLOADS_PER_RUN`、`LOG_LEVEL` 会转换成显式 CLI 覆盖并输出弃用提示。

检测到 `BILIPOD_CONFIG_DB`、`BILIPOD_WEB_PASSWORD`、`BILIPOD_MEDIA_ROOT` 等旧持久变量时，配置加载会以退出码 `2` 失败，并指出新字段和 `bilipod-config migrate`。已移除的 `BILIPOD_RSYNC_*`/`RSYNC_PASSWORD` 同样会被明确拒绝，迁移器只报告其未迁移，不生成替代配置。不允许通过旧环境静默覆盖 TOML。

---

## 依赖安装

### Python 包

```bash
pip install -c requirements.lock -e .             # 核心依赖（含 yt-dlp）
pip install -c requirements.lock -e ".[browser]"  # 含 Playwright 浏览器回退支持
```

项目主运行时为 Python 3.14，Python 3.13 作为回退；3.12 及以下不再支持。`uv.lock` 用于开发侧锁定和审计，生产部署仍使用 pip 与 `requirements.lock`。

### 外部工具

| 工具 | 用途 | 安装方式 |
|------|------|----------|
| `ffmpeg` | 手动媒体转码 | 系统包或显式传 `--ffmpeg-bin` |
| Playwright (Chromium) | 浏览器回退抓取 | `pip install -c requirements.lock -e ".[browser]" && playwright install chromium` |

`yt-dlp` 必须是项目专用的 yt-dlp 版本（与 bilipod 使用同一 Python 环境的 pip 包），不要依赖系统级 `yt-dlp` 命令。

### 开发测试

```bash
pip install -c requirements.lock -e ".[browser,dev]"
python -m pytest tests/
python -m pip check
pip-audit
```

---

## License

GNU General Public License v3.0

---

## 致谢

本项目站在许多优秀开源项目的肩膀上，特别感谢：

- Bilipod：为 Bilibili 内容的播客化工作提供了重要参考与基础。
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)：可靠的媒体下载与格式处理能力。
- [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)：Bilibili API 的 Python 封装。
- [FeedGenerator](https://github.com/lkiesow/python-feedgen)：RSS/Atom feed 生成能力。
- [Playwright](https://github.com/microsoft/playwright-python)：浏览器回退抓取能力。
- [FFmpeg](https://ffmpeg.org/)：音视频转码与处理能力。

感谢这些项目的维护者与贡献者。
