# bilibili-podcast — Bilibili 视频转播客 RSS

将 B 站 UP 主视频或合集/系列转换为播客 RSS 订阅源，支持音频下载、内容过滤、多用户分发。

配置文件支持 **YAML 文件**和 **SQLite 数据库**两种方式，任选其一。

## 目录

- [快速开始](#快速开始)
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
bilibili-podcast \
  --config-dir configs/series.d \
  --series demo-series \
  --cookie-file /path/to/cookies.txt \
  --token "__MEDIA_PLACEHOLDER__" \
  --media-root /var/lib/bilipod/media \
  --json-root /var/lib/bilipod/json \
  --rss-root /var/lib/bilipod/rss \
  --state-root /var/lib/bilipod/state \
  --media-base-url http://your-server:58743 \
  --apply
```

不带 `--apply` 时为干跑模式，仅获取和过滤数据，不写入任何文件。

### 配置来源选择

项目支持两种配置方式：

```bash
# YAML 模式（默认）
bilibili-podcast --config-dir configs/series.d ...

# SQLite 模式（单一文件，推荐生产使用）
bilibili-podcast --config-db /path/to/bilipod.db ...
```

SQLite 模式合并了配置和同步状态，简化管理。迁移工具见[下方](#sqlite-迁移)。

---

## CLI 参数

### bilibili-podcast

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--config-db` | (none) | SQLite 数据库路径，替代 `--config-dir` 和 `--state-root` |
| `--config-dir` | `configs/series.d` | 系列 YAML 配置目录 |
| `--series` | (all enabled) | 逗号分隔的系列 ID，不指定则处理所有 `enabled: true` 的系列 |
| `--cookie-file` | (none) | Netscape 格式 cookie 文件，用于 B 站 API 鉴权 |
| `--token` | (none) | RSS enclosure URL 中的 token 占位符，分发时替换为真实用户 token |
| `--media-root` | `/var/lib/bilipod/media` | MP3 媒体文件存储根目录 |
| `--json-root` | `/var/lib/bilipod/json` | 剧集元数据 JSON 存储根目录 |
| `--rss-root` | `/var/lib/bilipod/rss` | RSS XML 输出目录 |
| `--media-base-url` | `http://localhost:8080` | RSS enclosure URL 的基础 URL |
| `--lock-file` | `/tmp/bilibili-podcast.lock` | 进程锁文件路径 |
| `--state-root` | `/tmp/bilibili-podcast-state` | 系列状态 JSON 目录（YAML 模式使用） |
| `--max-downloads-per-run` | `20` | 每次运行最大下载数，`-1` 无限制 |
| `--min-free-gb` | `5.0` | 磁盘最小剩余空间 (GB)，不足时中止下载 |
| `--browser-fallback` | off | 启用 Playwright 浏览器回退（API 失败时用） |
| `--browser-user-data-root` | `/tmp/bilipod-browser-profiles` | Playwright 浏览器 profile 目录 |
| `--browser-login-check` | off | 启动时用 Playwright 验证 cookie 登录状态 |
| `--browser-login-wait-seconds` | `5.0` | 登录检查页面等待时间 |
| `--log-dir` | `/var/log/bilipod` | 日志输出目录 |
| `--debug` | off | 开启 DEBUG 级别日志 |
| `--force` | off | 跳过更新周期和 rate-limit cooldown 门控 |
| `--apply` | off | 实际写入文件和下载媒体，不带则为干跑 |

### bilipod-crontab

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--config-dir` | `configs/series.d` | 系列 YAML 配置目录 |
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
| `--config-db PATH` | SQLite 数据库路径（也支持 `BILIPOD_CONFIG_DB` 环境变量；生产默认路径见运维手册） |
| `--yes` | 跳过低风险确认 |
| `--dry-run` | 只预览，不写 DB、不执行同步 |
| `--json` | JSON 格式输出，方便脚本消费 |
| `--quiet` | 减少输出，但错误仍清楚显示 |
| `--debug` | 输出诊断信息 |

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

移除命令默认只预览，必须显式传入 `--apply` 才会执行。执行时会获取与同步进程相同的锁，再逐个系列移除 cron/systemd 调度；调度清理失败时不会继续删除该系列数据。成功后会删除 SQLite 中的系列及关联记录、本地 media/JSON 目录、master RSS、已发布的本地用户 RSS、cron wrapper、浏览器 profile，并从 `rss-publish-users.conf` 的显式系列列表中移除该系列。

远端 RSS 节点不在该命令的控制范围内，远端 XML 不会自动删除。

**过滤规则管理：**

| 命令 | 说明 |
|------|------|
| `bilipod-admin filters <series>` | 列出过滤规则（别名: `filters-show`, `fs`） |
| `bilipod-admin filters-add <series> --exclude-keyword "访谈"` | 追加黑名单关键词（别名: `fa`） |
| `bilipod-admin filters-add <series> --include-keyword "商业史"` | 追加白名单关键词 |
| `bilipod-admin filters-add <series> --ad-keyword "恰饭"` | 追加广告关键词 |
| `bilipod-admin filters-add <series> --exclude-bvid BVxxxx` | 追加排除 BVID |
| `bilipod-admin filters-add <series> --ad-bvid BVxxxx` | 追加广告 BVID |
| `bilipod-admin filters-add <series> --exclude-paid` | 启用付费内容排除 |
| `bilipod-admin filters-remove <series> --exclude-keyword "访谈" [--delete]` | 禁用/删除匹配规则（默认禁用，加 `--delete` 物理删除；别名: `fdel`） |
| `bilipod-admin filters-disable <series> --rule-id 123` | 按 ID 禁用规则（别名: `fd`） |
| `bilipod-admin filters-enable <series> --rule-id 123` | 按 ID 启用规则（别名: `fe`） |
| `bilipod-admin filters-import <series> --type exclude_keyword --file keywords.txt` | 从文件批量导入。`--type` 可选: `exclude_keyword`, `include_keyword`, `ad_keyword`, `exclude_bvid`, `ad_bvid`（别名: `fi`） |

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
| `bilipod-admin scheduler disable --backend systemd --series <series> --cron-script-dir /path/to/auto --yes` | 禁用指定 series 的 systemd timer，并按需要恢复 cron 调度 |

> **安全约束**：systemd backend 只应管理 `.timer`，不要手动触发生成的 `.service` 作为测试手段；不要把启用和立即运行合并成一步；timer 应保持 `Persistent=false`，避免补跑错过任务并触发额外 API 请求。需要验证时使用 `scheduler plan/status`、timer 状态、只读日志和 RSS token 扫描。

**付费/手动媒体管理：**

用于需要人工准备媒体文件的系列。命令会复用 SQLite 配置，不会自动下载手动媒体。

| 命令 | 说明 |
|------|------|
| `bilipod-admin paid refresh-metadata <series> --json-root /path/to/json` | 刷新 metadata JSON，不下载媒体 |
| `bilipod-admin paid list-missing <series> --json-root /path/to/json --media-root /path/to/media` | 列出已有 metadata 但缺少媒体的条目，只读 |
| `bilipod-admin paid attach-media <series> --bvid BVxxxxxxxxxx --server-path /path/to/file.mp3 --media-root /path/to/media` | 关联人工上传的 MP3 文件 |
| `bilipod-admin paid attach-media <series> --bvid BVxxxxxxxxxx --server-path /path/to/file.mp3 --replace` | 覆盖已有媒体文件 |
| `bilipod-admin paid add-item <series> --url <bilibili-video-url> --media-path /path/to/uploaded-media` | 从用户提供的媒体文件和 B 站视频页面新增一条手动媒体 |
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

项目使用 `ConfigStore` 抽象层，统一读写配置和同步状态。

### YAML 模式（默认）

- 配置：`configs/series.d/*.yaml`
- 状态：`{state_root}/{series}.json`
- 无需额外设置，开箱即用

### SQLite 模式

- 配置 + 状态：单一 SQLite 文件（`bilipod.db`）
- 8 表 schema，WAL 模式，外键约束
- 使用 `--config-db` 启用，不指定时自动退回到 YAML 模式

### SQLite 迁移

```bash
# 1. 迁移 YAML 配置和 JSON 状态到 SQLite
python3 scripts/migrate_yaml_to_sqlite \
  --config-dir configs/series.d \
  --state-root /path/to/state \
  --db-path /path/to/bilipod.db

# 2. 切换运行
bilibili-podcast --config-db /path/to/bilipod.db ... --apply

# 3. 切换 cron wrapper
python3 scripts/bilipod-crontab \
  --config-db /path/to/bilipod.db \
  --script-dir auto --force --apply
```

**回滚**：迁移脚本自动备份 `bilipod.db.bak.<timestamp>`；wrapper 用 `--config-dir` 重新生成。

### 生产部署

`scripts/deploy.sh` 实现一键部署，自动处理以下流程：

| 步骤 | 处理内容 |
|------|----------|
| 环境检查 | 检查 Git 仓库/remote、系统用户 `bilipod`、secrets、日志/数据目录 |
| Python 检测 + `_sqlite3` 编译 | 检测系统 Python 3.13/3.12/3.11/3.10/3.9；3.13 缺失 `_sqlite3` 时自动编译 |
| 拉取最新代码 | `git pull --ff-only` |
| 依赖安装 | `pip install -e .`，GitHub 不可达时自动回退 PyPI 安装 |
| 模块验证 | 验证 sqlite3/yaml/aiohttp/feedgen/lxml/bilibili-api/yt-dlp 已就绪 |
| 运行配置标准化 | 清理旧 systemd unit 中无效的 shell env 引用；Web 密码迁入受限 secrets env 文件 |
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
| Web 密码 | 预先创建受限 env 文件，内容为 `BILIPOD_WEB_PASSWORD=<web_password>`；真实值不写入 Git |
| 磁盘空间 | media/json/rss/state 所在分区至少 5GB 可用 |

```bash
# 干跑预览（不修改任何文件）
ssh <deploy-host> 'sudo bash -s' < scripts/deploy.sh

# 实际执行
ssh <deploy-host> 'sudo bash -s -- --apply' < scripts/deploy.sh
```

首次部署先干跑确认步骤，再用 `--apply`。

默认部署验证不会执行真实同步请求。确实需要做 API smoke test 时，显式设置：

```bash
ssh <deploy-host> 'sudo SMOKE_SYNC=1 bash -s -- --apply' < scripts/deploy.sh
```

生产部署后的进一步验证应以只读检查为主：确认部署版本、timer 状态、日志 warning/error、RSS 中媒体/图片/JSON URL 均包含 token 或占位符。服务器别名、真实路径、访问控制和日志清理等运维动作请放在不提交 git 的运维手册中维护。

#### 运行配置标准化

部署配置遵循以下边界：

- `bilipod-env.sh` 是 shell 脚本环境文件，可以使用 `export KEY=value`，由 wrapper 和 RSS 发布脚本 `source`。
- systemd unit 不应把 `bilipod-env.sh` 当作 `EnvironmentFile`，否则 systemd 会忽略 `export ...` 行并产生 warning。
- Web 密码不写入 `.service` 文件；应放在受限 env 文件中，例如 `<app_dir>/secrets/bilipod-web.env`，内容格式为 `BILIPOD_WEB_PASSWORD=<web_password>`。
- RSS/rsync 目标仍属于后续待清理的 legacy 兼容路径；相关主机、端口、token 必须只放在服务器私有配置里，不提交 Git。

可单独运行标准化脚本：

```bash
# 干跑
ssh <deploy-host> 'sudo bash -s' < scripts/standardize-runtime-config.sh

# 实际修复
ssh <deploy-host> 'sudo bash -s -- --apply' < scripts/standardize-runtime-config.sh
```

首次执行前，先在服务器私有 secrets 目录创建 `<app_dir>/secrets/bilipod-web.env`：

```bash
sudo install -m 640 -o root -g bilipod /dev/null <app_dir>/secrets/bilipod-web.env
sudoedit <app_dir>/secrets/bilipod-web.env
```

文件内容为：

```text
BILIPOD_WEB_PASSWORD=<web_password>
```

如果是迁移旧 unit，标准化脚本会从现有 `.service` 中提取旧值并迁入 secrets env 文件。不要把真实密码放进命令行、README、handoff 或 Git。

---
## 系列配置文件

每个系列一个 YAML 文件（YAML 模式）或一条数据库记录（SQLite 模式）。以下以 YAML 格式说明所有字段。

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
5. **关键词黑名单** (`exclude_keywords`) — 标题或简介包含任一关键词则排除。
6. **广告关键词** (`advertisement_keywords`) — 标题或简介包含任一关键词则排除。
7. **白名单** (`include_keywords`) — 非空时，仅标题或简介匹配至少一个关键词的视频保留。

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

所有日志输出到 `--log-dir`（默认 `/var/log/bilipod`）。

| 文件 | Logger | 级别 | 内容 |
|------|--------|------|------|
| `sync.log` | `bilibili_podcast.sync` | INFO/DEBUG | 主同步过程全量日志 |
| `sync.error.log` | `bilibili_podcast.sync` | ERROR | 仅错误级别，快速监控 |
| `playwright.log` | `bilibili_podcast.sync.playwright` | INFO/DEBUG | 浏览器回退日志 |

### 日志轮转

内建 `RotatingFileHandler`，无需系统 logrotate：

- 单文件上限：20 MB
- 轮转备份数：10 个
- 每类日志最大占用：200 MB

### 日志级别

| 级别 | 触发方式 | 内容 |
|------|----------|------|
| INFO（默认） | 正常启动 | 运行开始/完成、系列开始/完成、API 抓取汇总、过滤统计（total/kept/各类排除数）、下载开始/完成/跳过、RSS 写入、清理汇总、错误/警告 |
| DEBUG | `--debug` | 在此基础上增加：每页 API 请求 URL 和页码、每条剧集元数据写入、JSON 读写路径、磁盘空间检查、浏览器回退状态详情、合并/限制详情 |

---

## Cron 自动调度

`bilipod-crontab` 可以从系列配置生成 cron 任务，主要用于兼容、迁移或回滚场景。新部署推荐使用下一节的 systemd 调度。

```bash
# YAML 模式
bilipod-crontab --config-dir configs/series.d --apply

# SQLite 模式（wrapper 自动嵌入 DB 路径）
bilipod-crontab --config-db /path/to/bilipod.db --force --apply

# 仅预览
bilipod-crontab --config-dir configs/series.d --print
```

`bilipod-crontab` 为每个启用了 cron 的系列生成独立的 wrapper 脚本：

- wrapper 脚本嵌入 `--config-db` 路径（DB 模式）或 `--config-dir` 路径（YAML 模式）
- 运行时支持 `BILIPOD_CONFIG_DB` 环境变量覆盖
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

- timer 使用 `Persistent=false`，避免开机或启用时补跑错过任务。
- service 命令必须带 `--token __MEDIA_PLACEHOLDER__`。
- 如果使用 RSS 多用户分发，service 的同步成功后应触发发布脚本。
- 生成的 `.service` 不应包含 `EnvironmentFile=<app_dir>/bilipod-env.sh`；该文件是 shell env 文件，不是 systemd env 文件。
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
         ▼  rsync
RSS 服务节点
         │
         ▼  RSS 服务
http://rss-host:58743/rss/<user_token>/{series}.xml
```

### 用户配置文件

`rss-publish-users.conf` 每行格式：

```
<user_token>:series1,series2,series3
<user_token>:all
```

以 `#` 开头的行为注释。用户 token 和系列列表不允许有空格；真实 token 只放在服务器私有配置里，不提交 Git。

### 路径规范

| 位置 | 路径格式 |
|------|---------|
| 媒体文件 | `{media_root}/{series}/{bvid}_{quality}.mp3` |
| JSON 元数据 | `{json_root}/{series}/{bvid}_{quality}.info.json` |
| RSS 文件 | `{rss_root}/{series}.xml` |
| 状态文件（YAML 模式） | `{state_root}/{series}.json` |

---

## 部署架构

典型部署为双服务器结构：

| 服务器 | 角色 | 职责 |
|--------|------|------|
| 媒体节点 | 下载节点 | yt-dlp 下载 B 站视频，存储 MP3/JSON，Nginx 提供媒体文件 |
| RSS 节点 | 订阅节点 | 维护 RSS XML 文件，Nginx 提供 RSS 订阅（带 token 访问控制） |

### 数据流

```
播客客户端
  → RSS 节点 (http://rss-host:58743/rss/{token}/{series}.xml)
    → 解析 enclosure URL
      → 媒体节点 (http://media-host:58743/media/{series}/{bvid}_{quality}.mp3?token=xxx)
```

### 文件所有权

- media / json / rss / state 目录属主：`bilipod:bilipod`
- 目录权限：`755`
- 文件权限：`644`
- Cookie、token 和 Web 密码 env 文件：`600` 或 `640`，属主限制为部署用户/服务用户可读

---

## 环境变量

以下环境变量可覆盖 wrapper 脚本中的默认路径：

| 变量 | 说明 |
|------|------|
| `BILIPOD_CONFIG_DB` | SQLite 数据库路径，wrapper 脚本据此选择 `--config-db` 或 `--config-dir` |
| `BILIPOD_SYNC_PATH` | `bilibili-podcast` 可执行文件路径 |
| `BILIPOD_COOKIE_FILE` | Netscape cookie 文件路径 |
| `BILIPOD_MEDIA_ROOT` | 媒体文件存储根目录 |
| `BILIPOD_JSON_ROOT` | 元数据 JSON 根目录 |
| `BILIPOD_RSS_ROOT` | RSS XML 输出根目录 |
| `BILIPOD_STATE_ROOT` | 状态 JSON 根目录 |
| `BILIPOD_LOCK_FILE` | 进程锁文件路径 |
| `BILIPOD_LOG_DIR` | 日志输出目录 |
| `BILIPOD_BROWSER_USER_DATA_ROOT` | Playwright 浏览器 profile 目录 |
| `BILIPOD_MEDIA_BASE_URL` | RSS enclosure 的 base URL |
| `BILIPOD_MIN_FREE_GB` | 最小剩余磁盘空间 |
| `BILIPOD_RSYNC_SECRET` | Rsync 密码文件路径 |
| `BILIPOD_RSYNC_PORT` | Rsync 端口 |
| `BILIPOD_RSYNC_USER` | Rsync 用户名 |
| `BILIPOD_RSYNC_HOST` | Rsync 目标主机 |
| `BILIPOD_RSS_PUBLISH_SCRIPT` | 多用户 RSS 分发脚本路径 |
| `BILIPOD_MANUAL_MEDIA_DIRS` | 手动 media attach 允许目录（冒号分隔，如 `/path/a:/path/b`） |

`bilipod-env.sh` 文件（与脚本同目录）可自动加载上述变量，运行时环境变量优先级更高。

---

## 依赖安装

### Python 包

```bash
pip install -e .                    # 核心依赖
pip install -e ".[browser]"         # 含 Playwright 浏览器回退支持
```

### 外部工具

| 工具 | 用途 | 安装方式 |
|------|------|----------|
| `yt-dlp` | B 站音频下载 | `pip install yt-dlp`（建议装在同一 venv） |
| Playwright (Chromium) | 浏览器回退抓取 | `pip install playwright && playwright install chromium` |

`yt-dlp` 必须是项目专用的 yt-dlp 版本（与 bilipod 使用同一 Python 环境的 pip 包），不要依赖系统级 `yt-dlp` 命令。

### 开发测试

```bash
pip install pytest
python -m pytest tests/
```

---

## License

GNU General Public License v3.0
