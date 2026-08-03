# bilibili-podcast — Bilibili 视频转播客 RSS

将 B 站 UP 主视频或合集/系列转换为播客 RSS 订阅源，支持音频下载、内容过滤、多用户分发。

应用配置统一存放在 `config/*.toml`，系列配置与同步状态以 SQLite 为唯一写源；YAML 仅用于显式迁移或回滚读取。

## 目录

- [快速开始](#快速开始)
- [统一配置](#统一配置)
- [独立版本迁移模块](#独立版本迁移模块)
- [CLI 参数](#cli-参数)
  - [bilibili-podcast](#bilibili-podcast)
  - [bilibili-podcast-crontab](#bilibili-podcast-crontab)
  - [bilibili-podcast-admin — 系列管理 CLI](#bilibili-podcast-admin--系列管理-cli)
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
- [版本发布与升级](#版本发布与升级)
- [法律声明](#法律声明)

## 快速开始

### 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 单次运行

```bash
export BILIBILI_PODCAST_CONFIG_ROOT=<server_path>/config
bilibili-podcast-config validate
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
| `publish.toml` | master/published RSS、media URL、已删除系列和内建 generation publisher |
| `manual-media.toml` | 手动媒体白名单和 symlink 策略 |
| `rss-users.toml` | 用户 token 与系列授权关系 |
| SQLite | 系列、来源、过滤、同步策略、调度和 `sync_state` |

配置根定位顺序是显式 `ConfigManager(root)`、`BILIBILI_PODCAST_CONFIG_ROOT`、可确认的仓库根 `config/`；找不到时明确失败。除 `BILIBILI_PODCAST_CONFIG_ROOT` 外，旧持久配置环境变量不再生效，检测到时会给出目标字段和迁移命令。

`sync.toml` 的 `timeouts.sync_seconds`、`preview_seconds`、`publish_seconds` 分别控制手动同步、预览和本机发布钩子；`scheduler.toml` 的 `timeouts.command_seconds` 控制 cron/systemd 管理命令。手动同步使用 `downloads.max_per_run`，生成的调度任务使用 `downloads.scheduled_max_per_run`。自定义 systemd 主任务名称时，`units.sync_glob` 必须是恰好含一个 `*` 的 `.service` 文件名模式。

```bash
bilibili-podcast-config validate
bilibili-podcast-config validate --templates
bilibili-podcast-config show --scope web --format json  # 永远脱敏
bilibili-podcast-config migrate \
  --legacy-env <server_path>/legacy.env \
  --legacy-web-env <server_path>/legacy-web.env \
  --legacy-series-dir <server_path>/series.d \
  --legacy-rss-users <server_path>/rss-users.conf \
  --output-root <server_path>/config             # 默认 dry-run
```

`migrate` 只有加 `--apply` 才写入；写入前会 staged 校验，并把被替换的配置和 SQLite 备份到 `config/.backups/`。真实生产值必须由旧配置迁移和人工核对产生，不要把模板占位符当作实际配置。

最早生产布局使用独立 `legacy-v0` profile。历史 env 保持只读，缺失的路径由权限为 `0600` 的 manifest 显式提供；manifest 只允许 `[layout]`，包含数据库、media、JSON、RSS、state、secrets、浏览器、systemd、wrapper 和候选 release 的绝对路径，不得包含 token、Cookie 或密码：

```bash
bilibili-podcast-config migrate \
  --profile legacy-v0 \
  --layout-manifest <server_path>/.temp/legacy-layout.toml \
  --legacy-env <server_path>/legacy.env \
  --legacy-web-env <server_path>/legacy-web.env \
  --legacy-series-dir <server_path>/series.d \
  --legacy-rss-users <server_path>/rss-users.conf \
  --output-root <server_path>/config
```

## 独立版本迁移模块

`bilibili_podcast.config.migration` 是唯一允许修改历史安装格式的独立模块。同步器、Web、Admin、publisher 和部署脚本只能调用该模块，不能各自维护升级分支。

迁移模块的接口契约是“任意已发布版本 → 当前最新版本”，而不是只支持相邻版本：

- 自动检测来源版本，并按已登记步骤连续升级到当前版本。
- 未标记的统一配置安装通过 `legacy-unversioned` 进入版本链；真实 partial-env 的最早生产布局通过 `legacy-v0` 和 layout manifest 进入同一版本链。
- 配置、SQLite schema、文件布局、systemd unit、Cookie 和 RSS 发布格式均属于版本状态。
- 默认 dry-run；`--apply` 前执行在线备份、checksum、staged 验证和回滚准备。
- 当前版本重复执行必须幂等；未知未来版本、损坏状态、缺失步骤或活动同步锁必须显式失败。
- 每次发布改变持久状态时，必须同时登记迁移步骤，并加入从最老 fixture、所有中间版本和跨多个版本直升的测试。
- 测试会动态核对 `EARLIEST_UNIFIED_VERSION..LATEST_VERSION` 的连续 fixture、snapshot 和 step 注册；未来提升版本号但漏掉任一历史升级材料会直接失败。

`legacy-unversioned` 和 `legacy-v0` 是两个独立 source adapter。后续版本不得改写其历史语义来伪装兼容，而应追加不可变迁移步骤和对应 fixture。

已统一配置的历史安装使用独立升级入口；默认只规划和验证，不写入：

```bash
bilibili-podcast-config --root <server_path>/config upgrade
bilibili-podcast-config --root <server_path>/config upgrade --apply
```

输出只包含来源版本、目标版本、步骤名和备份目录，不打印配置值、token 或 Cookie。`--apply` 成功后会写入版本 marker，并使 SQLite `schema_version` 与安装版本一致。`deploy.sh` 使用候选源码中的迁移模块预检，并在候选代码安装后重新计算计划，不能用旧 venv 中的 CLI 判断新版本是否需要升级。后续任何改变持久状态的新功能，都必须在同一个 feature 中同步追加版本步骤和历史 fixture，不能把升级支持留到后续补做。

SQLite schema 在活动数据库上以向后兼容事务原位升级，升级前使用 SQLite online backup 生成校验过的回滚副本；升级 apply 会独占统一配置中的 sync lock，检测到活动同步进程就显式停止。禁止替换活动数据库 inode。systemd unit、timer 和 crontab 属于需要单独授权的系统状态：`standardize-runtime-config.sh --apply` 会保留旧 unit、生成新 unit，并在写入新 crontab 时清除旧自动 block，但不会 reload、restart、enable 或删除旧文件。

---

## CLI 参数

### bilibili-podcast

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--config-db` | (none) | SQLite 数据库路径，替代 `--config-dir` 和 `--state-root` |
| `--config-dir` | (none) | 显式启用 legacy YAML 回滚读取；不会自动回退 |
| `--series` | (all enabled) | 逗号分隔的系列 ID，不指定则处理所有 `enabled: true` 的系列 |
| `--cookie-file` | (none) | Netscape 格式 cookie 文件，用于 B 站 API 鉴权 |
| `--token` | `__MEDIA_PLACEHOLDER__` | 仅接受固定占位符；真实 token 会被拒绝，用户 RSS 由内建 publisher 生成 |
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

### bilibili-podcast-crontab

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--config-dir` | (none) | 显式 legacy YAML 回滚目录 |
| `--config-db` | (none) | SQLite 数据库路径，替代 `--config-dir` |
| `--apply` | off | 写入 crontab 和生成 wrapper 脚本 |
| `--print` | on | 打印生成的 cron 条目（无 `--apply` 时默认） |
| `--script-dir` | `auto` | wrapper 脚本输出目录 |
| `--cron-user` | `bilibili-podcast` | crontab 用户名 |
| `--force` | off | 覆盖已存在的 wrapper 脚本 |

### bilibili-podcast-admin — 系列管理 CLI

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

全局参数放在子命令前，例如 `bilibili-podcast-admin --config-db /path/to/bilibili-podcast.db list`。部分高频确认参数也支持放在子命令后，见下方命令说明。

**系列管理：**

| 命令 | 说明 |
|------|------|
| `bilibili-podcast-admin list` | 列出所有系列及状态 |
| `bilibili-podcast-admin show <series>` | 显示系列完整配置（支持 `--json`） |
| `bilibili-podcast-admin remove-series <series>` | 预览系列移除计划，不执行删除 |
| `bilibili-podcast-admin remove-series <series> --apply` | 永久移除系列、调度、本地 media/JSON/RSS 和数据库记录 |
| `bilibili-podcast-admin remove-up --uid <uid>` | 预览该 UP UID 对应的全部系列移除计划 |
| `bilibili-podcast-admin remove-up --uid <uid> --apply` | 永久移除该 UP UID 对应的全部系列 |
| `bilibili-podcast-admin add` | 交互式新增系列（输入 B 站 URL/UID，按提示配置过滤、同步、cron） |
| `bilibili-podcast-admin edit <series>` | 交互式编辑系列（回车保留当前值） |

**非交互式新增系列：（参数足够时跳过交互）**

```bash
bilibili-podcast-admin add \
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
| `bilibili-podcast-admin filters <series>` | 列出过滤规则（别名: `filters-show`, `fs`） |
| `bilibili-podcast-admin filters-add <series> --exclude-keyword "访谈"` | 追加黑名单关键词（别名: `fa`） |
| `bilibili-podcast-admin filters-add <series> --include-keyword "商业史"` | 追加白名单关键词 |
| `bilibili-podcast-admin filters-add <series> --ad-keyword "恰饭"` | 追加广告关键词 |
| `bilibili-podcast-admin filters-add <series> --exclude-bvid BVxxxx` | 追加排除 BVID |
| `bilibili-podcast-admin filters-add <series> --ad-bvid BVxxxx` | 追加广告 BVID |
| `bilibili-podcast-admin filters-add <series> --exclude-season-id 123456` | 追加排除合集 ID |
| `bilibili-podcast-admin filters-add <series> --exclude-paid` | 启用付费内容排除 |
| `bilibili-podcast-admin filters-remove <series> --exclude-keyword "访谈" [--delete]` | 禁用/删除匹配规则（默认禁用，加 `--delete` 物理删除；别名: `fdel`） |
| `bilibili-podcast-admin filters-disable <series> --rule-id 123` | 按 ID 禁用规则（别名: `fd`） |
| `bilibili-podcast-admin filters-enable <series> --rule-id 123` | 按 ID 启用规则（别名: `fe`） |
| `bilibili-podcast-admin filters-import <series> --type exclude_keyword --file keywords.txt` | 从文件批量导入。`--type` 可选: `exclude_keyword`, `include_keyword`, `ad_keyword`, `exclude_bvid`, `ad_bvid`, `exclude_season_id`（别名: `fi`） |

`filters-add` 额外支持子命令级 `--yes`；`filters-remove` 支持 `--delete` 物理删除，否则默认只禁用匹配规则。

**同步策略管理：**

| 命令 | 说明 |
|------|------|
| `bilibili-podcast-admin sync-policy show <series>` | 显示当前同步策略（支持 `--json`） |
| `bilibili-podcast-admin sync-policy set <series> --keep-last 100 --update-period 12h --quality 64K` | 修改指定字段，未传字段保持不变 |

支持修改的字段：`--page-size`、`--incremental-page-size`、`--max-pages`、`--max-requests-per-series`、`--request-interval`、`--request-jitter`、`--rate-limit-cooldown`、`--update-period`、`--format`、`--quality`、`--fetch-strategy`、`--keep-last`、`--browser-fallback`、`--browser-wait-min`、`--browser-wait-max`、`--browser-fallback-cooldown`、`--require-paid-confirmation`、`--min-duration`、`--max-duration`。

**定时任务管理：**

| 命令 | 说明 |
|------|------|
| `bilibili-podcast-admin cron show <series>` | 显示系列 DB 中的 cron 配置（支持 `--json`） |
| `bilibili-podcast-admin cron set <series> --schedule "15 11 * * *" --schedule "15 23 * * *"` | 修改 cron（仅写 DB，不安装系统 crontab） |
| `bilibili-podcast-admin cron plan` | 预览 cron 计划（使用临时目录，不写系统 crontab） |
| `bilibili-podcast-admin cron plan --cron-script-dir /path/to/auto` | 预览 cron 计划（指定目标目录后输出真实路径） |
| `bilibili-podcast-admin cron apply --cron-script-dir /path/to/auto` | 安装 crontab，wrapper 写入指定目录（生产部署推荐显式指定绝对路径） |
| `bilibili-podcast-admin cron apply --cron-script-dir /path/to/auto --yes` | 跳过确认，直接安装 |

> **注意**：`cron` 命令保留用于兼容和回滚场景。生产环境推荐使用 `scheduler --backend systemd` 管理定时器。DB 模式下，`cron plan/apply` 会跳过当前由 systemd timer 管理的系列，避免双调度。`cron apply` 的 `--cron-script-dir` 和 `--yes` 放在子命令后：`cron apply --cron-script-dir /path --yes`。全局 `--yes`（`bilibili-podcast-admin --yes cron apply`）也兼容。`cron plan` 默认使用临时目录，仅用于结构预览；传 `--cron-script-dir` 后输出真实路径，可与 `cron apply` 核对。

**调度管理：**

| 命令 | 说明 |
|------|------|
| `bilibili-podcast-admin scheduler plan` | 预览调度计划（默认 backend=cron） |
| `bilibili-podcast-admin scheduler plan --backend cron --cron-script-dir /path/to/auto` | 指定 backend + 目录预览 |
| `bilibili-podcast-admin scheduler apply --cron-script-dir /path/to/auto --yes` | 安装调度 |
| `bilibili-podcast-admin scheduler plan --backend systemd --series <series> --cron-script-dir /path/to/auto` | 预览指定 series 的 systemd timer/unit |
| `bilibili-podcast-admin scheduler apply --backend systemd --series <series> --cron-script-dir /path/to/auto --yes` | 指定 series 安装或刷新 systemd timer |
| `bilibili-podcast-admin scheduler status` | 显示所有系列调度状态 |
| `bilibili-podcast-admin scheduler status <series>` | 显示指定系列调度状态 |
| `bilibili-podcast-admin scheduler status --backend systemd` | 显示 systemd timer 状态（enabled/active） |
| `bilibili-podcast-admin scheduler set <series> --schedule "15 3 * * *" --yes` | 设置系列调度（仅写 DB，不安装） |
| `bilibili-podcast-admin scheduler set <series> --schedule "15 11 * * *" --retry-schedule "15 13 * * *" --yes` | 设置主调度和失败后的条件兜底调度 |
| `bilibili-podcast-admin scheduler disable --backend systemd --series <series> --cron-script-dir /path/to/auto --yes` | 禁用指定 series 的 systemd timer，并按需要恢复 cron 调度 |
| `bilibili-podcast-admin scheduler disable --backend systemd --series <series> --delete-units --yes` | 禁用并删除对应 systemd unit 文件 |

> **安全约束**：systemd backend 只应管理 `.timer`，不要手动触发生成的 `.service` 作为测试手段；不要把启用和立即运行合并成一步；timer 应保持 `Persistent=false`，避免补跑错过任务并触发额外 API 请求。需要验证时使用 `scheduler plan/status`、timer 状态、只读日志和 RSS token 扫描。

**付费/手动媒体管理：**

用于需要人工准备媒体文件的系列。命令会复用 SQLite 配置，不会自动下载手动媒体。

| 命令 | 说明 |
|------|------|
| `bilibili-podcast-admin paid refresh-metadata <series> --json-root /path/to/json` | 刷新 metadata JSON，不下载媒体 |
| `bilibili-podcast-admin paid refresh-metadata <series> --cookie-file /path/to/cookies.txt` | 使用指定 cookie 刷新 metadata |
| `bilibili-podcast-admin paid refresh-metadata <series> --bvid BVxxxxxxxxxx` | 仅刷新指定 BVID 的 metadata |
| `bilibili-podcast-admin paid refresh-metadata <series> --url <bilibili-video-url>` | 仅刷新指定视频 URL 的 metadata |
| `bilibili-podcast-admin paid list-missing <series> --json-root /path/to/json --media-root /path/to/media` | 列出已有 metadata 但缺少媒体的条目，只读 |
| `bilibili-podcast-admin paid attach-media <series> --bvid BVxxxxxxxxxx --server-path /path/to/file.mp3 --media-root /path/to/media` | 关联人工上传的 MP3 文件 |
| `bilibili-podcast-admin paid attach-media <series> --bvid BVxxxxxxxxxx --server-path /path/to/file.mp3 --replace` | 覆盖已有媒体文件 |
| `bilibili-podcast-admin paid add-item <series> --url <bilibili-video-url> --media-path /path/to/uploaded-media` | 从用户提供的媒体文件和 B 站视频页面新增一条手动媒体 |
| `bilibili-podcast-admin paid add-item <series> --ffmpeg-bin /path/to/ffmpeg` | 指定转码命令；RSS 重建成功后由内建 publisher 原子发布 |
| `bilibili-podcast-admin paid rebuild-rss <series> --json-root /path/to/json --media-root /path/to/media --rss-root /path/to/rss` | 从现有 metadata + media 重建 master RSS |

`attach-media` 只接受 MP3 文件，并校验 BVID。上传源文件必须位于部署环境配置的白名单目录内。`add-item` 可接受视频或其他 ffmpeg 支持的媒体格式，会调用 `ffmpeg` 转为当前 series 的 MP3 quality，使用视频页面拉取单条 metadata，并重建 master RSS。手动媒体文件名会使用该 series 当前 `sync.quality`，即 `{BVID}_{quality}.mp3`，不要对非 64K series 固定写 `_64K`。`rebuild-rss` 生成 master RSS 时使用 `__MEDIA_PLACEHOLDER__`，由 RSS 发布流程替换为用户专属 token。

```bash
bilibili-podcast-admin paid add-item <series> \
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
| `bilibili-podcast-admin preview <series>` | 执行干跑预览（使用当前 SQLite 配置，不下载、不写 RSS） |
| `bilibili-podcast-admin sync <series>` | 默认干跑模式 |
| `bilibili-podcast-admin sync <series> --apply` | 真正同步（需二次确认） |

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
  --db-path /path/to/bilibili-podcast.db

# 2. 显式回滚读取（不会写回 YAML）
bilibili-podcast --config-dir <server_path>/legacy-series.d ...

# 3. 切换 cron wrapper
python3 scripts/bilibili-podcast-crontab \
  --config-db /path/to/bilibili-podcast.db \
  --script-dir auto --force --apply
```

**回滚**：恢复迁移前的 TOML、SQLite、unit 和 wrapper 备份，再显式用 `--config-dir` 读取旧 YAML；不要让生产命令静默回退。

### 生产部署

零停机生产部署使用 `scripts/deploy-release.sh`。它从已校验的 source artifact 和固定 Git 依赖 wheel 创建 immutable release 与独立 venv；准备和 symlink 激活是两个独立动作，均默认 dry-run，且都不会 reload 或 restart 服务：

```bash
# 校验并准备候选 release；默认不写入
scripts/deploy-release.sh prepare \
  --root <server_path> \
  --commit <commit_sha> \
  --artifact <server_path>/.temp/release.tar.gz \
  --artifact-sha256 <sha256> \
  --bootstrap-wheel <server_path>/.temp/dependency.whl \
  --bootstrap-wheel-sha256 <sha256>

# 分别授权后执行 prepare 和原子 symlink 激活
scripts/deploy-release.sh --apply prepare <same_arguments>
scripts/deploy-release.sh --apply activate \
  --root <server_path> --commit <commit_sha>
```

archive 只允许普通文件和目录，并拒绝 `.git`、`.temp`、`.venv`、`build` 和 `*.egg-info` 等本地/生成目录；路径穿越、symlink、重复 entry、checksum 差异、已有不完整 release 或非 symlink 的 `current` 都会显式失败。venv 移动前会修正 console script 的 staging shebang，release/venv marker 必须交叉一致，最终目录移除写权限；6 个项目命令和 Python 入口缺一不可。服务启动、timer 切换、Nginx/fail2ban reload 仍是后续独立门禁。并行影子 Web 可使用 `bilibili-podcast-web --host 127.0.0.1 --port <shadow_port>`；host 覆盖只接受 loopback IP，且覆盖只作用于当前进程，不改变 TOML。

`scripts/deploy.sh` 仅用于已经采用单一活动代码目录的原地维护，不属于零停机蓝绿工具。它处理以下流程：

| 步骤 | 处理内容 |
|------|----------|
| 环境检查 | 检查 Git 仓库/remote、系统用户 `bilibili-podcast`、secrets、日志/数据目录 |
| 候选预检 | 使用活动 venv 和候选源码规划安装版本升级 |
| 备份 | 在线备份 TOML、SQLite、systemd unit 和 wrapper，并校验 SHA-256 |
| 原地更新 | `git pull --ff-only` 后在活动 venv 执行锁定依赖安装 |
| 版本升级 | 再次使用候选模块规划并应用连续迁移步骤 |
| wrapper 准备 | 使用唯一配置根重新生成 wrapper |
| 验证 | 验证配置并编译 Python；不访问 B 站 API，不操作服务 |

**前置依赖**（脚本会检查并提示设置方式）：

| 依赖 | 说明 |
|------|------|
| Git 仓库 | 代码需已 clone 到服务器，remote origin 已配置 |
| 系统用户 | `bilibili-podcast` 服务用户（`useradd -r -s /sbin/nologin bilibili-podcast`） |
| Cookie | `www.bilibili.com_cookies.txt`（Netscape 格式，可选，无则降级浏览器模式） |
| Web 密码 | 写入权限受限的实际 `web.toml`；真实值不写入 Git |
| 磁盘空间 | media/json/rss/state 所在分区至少 5GB 可用 |

```bash
# 干跑预览（不修改任何文件）
ssh <deploy-host> 'sudo env BILIBILI_PODCAST_CONFIG_ROOT=<server_path>/config bash -s' < scripts/deploy.sh

# 实际执行
ssh <deploy-host> 'sudo env BILIBILI_PODCAST_CONFIG_ROOT=<server_path>/config bash -s -- --apply' < scripts/deploy.sh
```

首次部署先干跑确认步骤，再用 `--apply`。有在线用户时不得用原地模式替代 release 模式。

部署脚本不会执行真实同步请求。生产部署后的进一步验证应以只读检查为主：确认部署版本、timer 状态、日志 warning/error、RSS 中媒体/图片/JSON URL 均包含 token 或占位符。服务器别名、真实路径、访问控制和日志清理等运维动作请放在不提交 git 的运维手册中维护。

#### 运行配置标准化

标准化脚本把旧 env、Web env、系列 YAML 和 RSS 用户文件视为只读迁移输入。它先执行 dry-run/校验；`--apply` 时生成实际 TOML、备份旧配置和 unit，并把 unit 改为只含 `BILIBILI_PODCAST_CONFIG_ROOT`。最早生产布局必须显式传入 `--profile legacy-v0 --layout-manifest <server_path>/.temp/legacy-layout.toml`。需要两个候选 Web 实例时，同时传入不同的 `--web-primary-port` 和 `--web-backup-port`；脚本会生成主 unit 与 `bilibili-podcast-web-backup.service`，但不会启动它们或调用 `systemctl`。脚本不会显示密文。

可单独运行标准化脚本：

```bash
# 干跑
ssh <deploy-host> 'sudo env BILIBILI_PODCAST_CONFIG_ROOT=<server_path>/config BILIBILI_PODCAST_ENV_FILE=<server_path>/legacy.env BILIBILI_PODCAST_WEB_ENV_FILE=<server_path>/legacy-web.env BILIBILI_PODCAST_LEGACY_SERIES_DIR=<server_path>/series.d RSS_USERS_CONF=<server_path>/rss-users.conf bash -s' < scripts/standardize-runtime-config.sh

# 实际修复
ssh <deploy-host> 'sudo env BILIBILI_PODCAST_CONFIG_ROOT=<server_path>/config BILIBILI_PODCAST_ENV_FILE=<server_path>/legacy.env BILIBILI_PODCAST_WEB_ENV_FILE=<server_path>/legacy-web.env BILIBILI_PODCAST_LEGACY_SERIES_DIR=<server_path>/series.d RSS_USERS_CONF=<server_path>/rss-users.conf bash -s -- --apply' < scripts/standardize-runtime-config.sh

# legacy-v0 + 两个独立 localhost 候选端口（仍默认 dry-run）
ssh <deploy-host> 'sudo env BILIBILI_PODCAST_CONFIG_ROOT=<server_path>/config BILIBILI_PODCAST_ENV_FILE=<server_path>/legacy.env BILIBILI_PODCAST_WEB_ENV_FILE=<server_path>/legacy-web.env BILIBILI_PODCAST_LEGACY_SERIES_DIR=<server_path>/series.d RSS_USERS_CONF=<server_path>/rss-users.conf bash -s -- --profile legacy-v0 --layout-manifest <server_path>/.temp/legacy-layout.toml --web-primary-port <primary_port> --web-backup-port <backup_port>' < scripts/standardize-runtime-config.sh
```

生产切换前必须确认所有同步与手动媒体入口都调用内建 publisher，且服务器上不存在仍被 unit、wrapper 或 cron 引用的外部发布脚本。

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
| `api_backend` | string | 否 | 使用的 B 站 API 后端：`bilibili-api`（默认）/ `bilix` / `yutto`，见下方说明 |

### API 后端（api_backend）

| 后端 | 支持范围 | 说明 |
|------|----------|------|
| `bilibili-api` | 空间 / 系列 / 剧集 | 默认后端，随主依赖安装 |
| `bilix` | 空间 / 系列 | 不支持剧集（season）类型 |
| `yutto` | 空间 / 系列 / 剧集 | 覆盖全部类型 |

可选后端（`bilix` / `yutto`）需额外安装：

```bash
pip install "bilibili-podcast[api-backends]"
```

未安装对应后端时，配置该值会在运行时报出明确的中文错误提示。

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
| SQLite 模式 | 同一 `bilibili-podcast.db` 的 `sync_state` 表 |

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

`bilibili-podcast-crontab` 可以从系列配置生成 cron 任务，主要用于兼容、迁移或回滚场景。新部署推荐使用下一节的 systemd 调度。

```bash
# YAML 模式
bilibili-podcast-crontab --config-dir configs/series.d --apply

# SQLite 模式（默认读取 app.database.path）
bilibili-podcast-crontab --force --apply

# 仅预览
bilibili-podcast-crontab --config-dir configs/series.d --print
```

`bilibili-podcast-crontab` 为每个启用了 cron 的系列生成独立的 wrapper 脚本：

- wrapper 脚本仅嵌入 `BILIBILI_PODCAST_CONFIG_ROOT`、series 和一次性控制参数
- `--config-dir` 只用于显式 YAML 回滚
- 支持 `MAX_DOWNLOADS_PER_RUN` / `FORCE` / `DEBUG` 环境变量覆盖
- 自动合并到 `--cron-user` 用户的 crontab（默认 `bilibili-podcast`）
- 已有 crontab 中由 `BEGIN BILIBILI_PODCAST AUTO` / `END BILIBILI_PODCAST AUTO` 标记的自动区域会被替换
- DB 模式会跳过标记为 systemd 后端或当前已有 enabled systemd timer 的系列

### 最佳实践

- 不同系列错开执行时间，避免短时间内大量 API 请求
- 每个系列每天至少 2 次（B 站更新通常每天 1-2 次）
- 周更系列可以设置仅周末运行

---

## Systemd 自动调度

生产环境推荐使用 `bilibili-podcast-admin scheduler --backend systemd` 为每个 series 生成独立的 `.service` 和 `.timer`。

```bash
# 只读预览
bilibili-podcast-admin scheduler plan \
  --backend systemd \
  --series <series> \
  --cron-script-dir /path/to/auto

# 安装或刷新指定 series 的 timer
bilibili-podcast-admin scheduler apply \
  --backend systemd \
  --series <series> \
  --cron-script-dir /path/to/auto \
  --yes

# 查看状态
bilibili-podcast-admin scheduler status --backend systemd
```

systemd 调度的安全约束：

- 主调度之间不得重复；允许 timer 唤醒频率高于该系列的 `update_period`，实际重复同步由 sync 的 update-period gate 跳过。兜底调度必须通过 `--retry-schedule` 显式标记。
- 主调度成功后，兜底 timer 当天触发时只记录 `retry_not_needed`，不会请求 B 站；主调度失败后，兜底可绕过 `update_period` 尝试一次。
- `rate_limit_cooldown` 的优先级始终高于兜底调度，兜底不会绕过限流冷却。
- cron 仅作为 systemd 不可用时的手工兜底链路，默认不启用；cron backend 不支持条件兜底，存在 retry schedule 时 `plan/apply` 会显式失败。
- timer 使用 `Persistent=false`，避免开机或启用时补跑错过任务。
- service 命令必须带 `--token __MEDIA_PLACEHOLDER__`。
- 如果启用 RSS 多用户分发，service 的同步成功后按 `publish.toml` 触发内建 generation publisher。
- 生成的 `.service` 只保留 `Environment=BILIBILI_PODCAST_CONFIG_ROOT=...`，不包含旧 env 文件或敏感值。
- 验证 timer 时只启动/刷新 `.timer`，不要手动启动 `.service`。
- 不要把启用和立即运行合并成一步；启用和启动 timer 应分开执行，并确认 timer active 后再移除旧调度。

---

## RSS 多用户分发

生成的 master RSS 永远使用占位符 token（`__MEDIA_PLACEHOLDER__`）。内建 publisher 在文件锁内构建并校验完整 generation，按 token SHA-256 目录写入用户 RSS，`fsync` 后原子切换 `current`，并保留最近两代。

### 工作流程

```
/path/to/master-rss/{series}.xml          (master, 占位符)
         │
         ▼  publish script
/path/to/published-rss/.generations/{generation}/{token_sha256}/{series}.xml
/path/to/published-rss/current -> .generations/{generation}
         │
         │
         ▼  本机 RSS 服务
https://podcast.example.invalid/rss/<user_token>/{series}.xml
```

Nginx 通过应用 `auth_request` 将 URL 中的 token 映射为 hash 目录。published RSS 权限为 `0640`，Nginx worker 必须通过专用 `bilibili-podcast` 共享组获得只读权限，不能放宽为其他用户可读。鉴权 upstream 的主备实例都必须运行带 `/auth` 接口的新版本；旧 Web 不能作为鉴权 backup。回滚使用已校验的旧 Nginx 配置热 reload。删除系列由内部 `403 + X-RSS-Denial-Status: 410` 映射为公网 `410 Gone`，因为 Nginx `auth_request` 不能直接传播任意状态码。

Nginx 和 Uvicorn 日志不得记录 URI、query、token、Authorization 或 Cookie。存在 fail2ban 时，生产探针只允许携带完整正确参数的单条、低频正向请求；错误 token、缺参和完整负向矩阵只能在进程内或隔离 listener 验证。

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

项目按单服务器部署：同步进程、Web 管理、SQLite、媒体文件、master RSS、用户 RSS 和对外 HTTP 服务位于同一台服务器。发布完全由应用内建实现，不依赖外部 hook 或 rsync。

未来如需向其他服务器分发，应单独设计带身份认证、完整性校验、失败重试和可观测性的发布接口；本项目不保留 rsync 执行链或配置。

### 数据流

```
播客客户端
  → 本机 RSS 服务 (https://podcast.example.invalid/rss/{token}/{series}.xml)
    → 解析 enclosure URL
      → 本机媒体服务 (https://media.example.invalid/media/{series}/{bvid}_{quality}.mp3?token=xxx)
```

### 文件所有权

- media / json / rss / state 目录属主：`bilibili-podcast:bilibili-podcast`
- 目录权限：`755`
- 文件权限：`644`
- Cookie、token 和 Web 密码所在 TOML：`600` 或服务组只读的 `640`

---

## 环境变量

唯一的持久配置 bootstrap 是 `BILIBILI_PODCAST_CONFIG_ROOT`。`FORCE`、`DEBUG`、`SMOKE_SYNC` 仍是一次性运行/部署控制；`PATH` 只参与系统命令发现。cron 的 `MAX_DOWNLOADS_PER_RUN`、`LOG_LEVEL` 会转换成显式 CLI 覆盖并输出弃用提示。

检测到 `BILIBILI_PODCAST_CONFIG_DB`、`BILIBILI_PODCAST_WEB_PASSWORD`、`BILIBILI_PODCAST_MEDIA_ROOT` 等旧持久变量时，配置加载会以退出码 `2` 失败，并指出新字段和 `bilibili-podcast-config migrate`。已移除的 `BILIBILI_PODCAST_RSYNC_*`/`RSYNC_PASSWORD` 同样会被明确拒绝，迁移器只报告其未迁移，不生成替代配置。不允许通过旧环境静默覆盖 TOML。

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

`yt-dlp` 必须是项目专用的 yt-dlp 版本（与 bilibili-podcast 使用同一 Python 环境的 pip 包），不要依赖系统级 `yt-dlp` 命令。

### 开发测试

```bash
pip install -c requirements.lock -e ".[browser,dev]"
python -m pytest tests/
python -m pip check
pip-audit
```

---

## 版本发布与升级

正式版本通过 GitHub Release 提供 source artifact、wheel、sdist、`SHA256SUMS`、CycloneDX SBOM 和构建证明。部署仍使用现有的 immutable release 链，不包含自动在线升级器。

固定升级顺序如下；`<release_sha>`、`<artifact_sha256>`、`<install_root>`、`<config_root>` 和 `<plan_id>` 必须替换为本次发布的实际值：

```bash
# 1. 下载 Release assets 后，先在离线目录校验全部文件
sha256sum --check SHA256SUMS
tar -xzf bilibili-podcast-wheelhouse-v1.0.0.tar.gz
(cd wheelhouse && sha256sum --check ../wheelhouse.SHA256SUMS)

# 2. 准备 immutable source release 与独立 venv，不切换当前版本
scripts/deploy-release.sh --apply prepare \
  --root <install_root> \
  --commit <release_sha> \
  --artifact bilibili-podcast-source-v1.0.0.tar.gz \
  --artifact-sha256 <artifact_sha256> \
  --wheelhouse wheelhouse \
  --wheel-manifest wheelhouse.SHA256SUMS \
  --python python3

# 3. 使用候选 venv 规划、应用并完成配置升级；当前 schema 版本仍为 4
<install_root>/venvs/<release_sha>/bin/bilibili-podcast-config \
  --root <config_root> upgrade --prepare
<install_root>/venvs/<release_sha>/bin/bilibili-podcast-config \
  --root <config_root> upgrade --apply --plan-id <plan_id>
<install_root>/venvs/<release_sha>/bin/bilibili-podcast-config \
  --root <config_root> finalize --apply --plan-id <plan_id>

# 4. 原子切换 release symlink；脚本不会 reload 或 restart 服务
scripts/deploy-release.sh --apply activate \
  --root <install_root> \
  --commit <release_sha> \
  --config-root <config_root> \
  --python python3

# 5. 在独立变更窗口中显式重启服务，并按项目运维门禁验证
systemctl restart <service_unit>
```

若配置升级在 finalize 前失败，使用同一候选 CLI 与 plan 回滚；若激活后的应用验证失败，将 `current` / `current-venv` 原子切回上一套已校验 release/venv，再独立重启服务。任何回滚都必须先确认目标、影响与现有备份：

```bash
<install_root>/venvs/<release_sha>/bin/bilibili-podcast-config \
  --root <config_root> rollback --apply --plan-id <plan_id>
```

只有未来修改 TOML、SQLite schema、文件布局或系统状态契约时，才新增连续迁移步骤并提高 `LATEST_VERSION`；单纯代码、依赖或发布流程变更不得虚增 schema 版本。

---

## 法律声明

Copyright (C) 2026 bilibili-podcast contributors.

本项目是依据 GNU General Public License v3.0 发布的派生作品；相对来源基线的主要修改包括项目重命名、统一配置与连续迁移、SQLite 状态管理、安全加固、内建 RSS 发布和 immutable release 部署。

本项目为社区维护的非官方项目，与哔哩哔哩及其关联公司不存在隶属、授权、认可或赞助关系。“哔哩哔哩”、Bilibili 及相关标识可能是其权利人的商标。本项目不授予任何商标权；用户获取、转换、存储和分发内容时，必须自行确认授权并遵守适用法律、平台条款及内容权利人的版权要求。

---

## License

GNU General Public License v3.0

---

## 致谢

本项目站在许多优秀开源项目的肩膀上，特别感谢：

- [Bilipod](https://github.com/sunrisewestern/bilipod)：感谢原作者及贡献者以 GPLv3 提供基础实现；本仓库来源基线为提交 `d16ce56604d1fbe3b0504ce2db964b0e29ffd9f0`，其后进行了项目重命名、配置迁移、安全、发布与部署方面的派生修改。
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)：可靠的媒体下载与格式处理能力。
- [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)：Bilibili API 的 Python 封装。
- [FeedGenerator](https://github.com/lkiesow/python-feedgen)：RSS/Atom feed 生成能力。
- [FastAPI](https://fastapi.tiangolo.com/)：Web 服务框架。
- [Uvicorn](https://www.uvicorn.org/)：ASGI 服务器。
- [Jinja2](https://jinja.palletsprojects.com/)：Web 模板引擎。
- [itsdangerous](https://itsdangerous.palletsprojects.com/)：会话签名与安全数据序列化。
- [PyYAML](https://pyyaml.org/)：YAML 配置解析。
- [Playwright](https://github.com/microsoft/playwright-python)：浏览器回退抓取能力。
- [FFmpeg](https://ffmpeg.org/)：音视频转码与处理能力。

感谢这些项目的维护者与贡献者。
