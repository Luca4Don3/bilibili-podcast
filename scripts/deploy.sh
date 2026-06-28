#!/bin/bash
# Phase 2 SQLite 迁移部署脚本
# 用法: ssh <deploy-host> 'sudo bash -s -- --apply' < scripts/deploy.sh
#
# 不带 --apply 时为干跑模式，仅打印执行计划。
# 带 --apply 时执行实际迁移。

set -euo pipefail

APPLY="${1:-}"
if [ "$APPLY" = "--apply" ]; then
    APPLY=true
else
    APPLY=false
fi

# ── 路径（与 bilibili-podcast-env.sh 保持一致） ──────────────
APP_DIR="<server_path>"
CODE_DIR="$APP_DIR/app"
VENV_DIR="$APP_DIR/venv"
STATE_DIR="$APP_DIR/state"
CONFIG_DIR="$CODE_DIR/configs/series.d"
DB_PATH="$STATE_DIR/bilibili-podcast.db"
COOKIE_FILE="$APP_DIR/secrets/www.bilibili.com_cookies.txt"
WEB_ENV_FILE="$APP_DIR/secrets/bilibili-podcast-web.env"
MEDIA_ROOT="/var/lib/bilibili-podcast/media"
JSON_ROOT="/var/lib/bilibili-podcast/json"
RSS_ROOT="$APP_DIR/rss"
LOCK_FILE="$STATE_DIR/bilibili-podcast.lock"
LOG_DIR="/var/log/bilibili-podcast"

SYNC_BIN="$VENV_DIR/bin/bilibili-podcast"
PYTHON_BIN="$VENV_DIR/bin/python3"
CONSTRAINTS_FILE="$CODE_DIR/requirements.lock"
PYTHON_SOURCE_VERSION="3.14.6"
PLAYWRIGHT_PACKAGE_SPEC="playwright>=1.60,<2"
SUPPORTED_PYTHON_CANDIDATES=(
    /usr/local/bin/python3.14
    /usr/bin/python3.14
    /usr/local/bin/python3.13
    /usr/bin/python3.13
    /usr/local/bin/python3
    /usr/bin/python3
)
FALLBACK_RUNTIME_PACKAGES=(
    "aiohttp>=3.14.1,<4"
    PyYAML
    feedgen
    lxml
    pillow
    requests
    pycryptodomex
    "curl_cffi>=0.15.0,<0.16"
    "yt-dlp>=2026.6.9,<2027"
    bilibili-api-python
)
REQUIRED_PYTHON_MODULES=(
    sqlite3:sqlite3
    yaml:PyYAML
    aiohttp:aiohttp
    curl_cffi:curl_cffi
    feedgen:feedgen
    lxml:lxml
    bilibili_api:bilibili-api
)

echo "========================================"
echo " Phase 2: SQLite 迁移部署"
echo "========================================"
echo "  代码目录: $CODE_DIR"
echo "  配置文件: $CONFIG_DIR"
echo "  状态目录: $STATE_DIR"
echo "  目标 DB:  $DB_PATH"
echo "  模式:     $([ "$APPLY" = true ] && echo "实际执行" || echo "干跑模式")"
echo ""

# ── 1. 检查环境依赖 ──────────────────────────────
echo "▶ [1/9] 检查环境依赖 ..."

# 1a. Git 仓库
if [ ! -d "$CODE_DIR/.git" ]; then
    echo "  ✗ 错误: $CODE_DIR 不是 git 仓库"
    echo ""
    echo "  首次部署需先完成:"
    echo "    git clone <repo-url> $CODE_DIR"
    echo "    git -C $CODE_DIR remote add origin <internal-git-url>"
    exit 1
fi

# 1b. Git remote 可达
REMOTE_URL=$(cd "$CODE_DIR" && git remote get-url origin 2>/dev/null || true)
if [ -z "$REMOTE_URL" ]; then
    echo "  ✗ 错误: Git remote origin 未配置"
    echo "  请先添加 remote:"
    echo "    git -C $CODE_DIR remote add origin <git-url>"
    exit 1
fi
echo "  ✓ 仓库: $REMOTE_URL"

# 1c. 配置文件
if [ ! -d "$CONFIG_DIR" ]; then
    echo "  ✗ 错误: 配置目录不存在 $CONFIG_DIR"
    echo "  请确保代码已 clone 且 configs/series.d/ 存在"
    exit 1
fi

# 1d. 系统用户
if ! id bilibili-podcast &>/dev/null; then
    echo "  ✗ 错误: 系统用户 bilibili-podcast 不存在"
    echo "  请先创建:"
    echo "    useradd -r -s /sbin/nologin bilibili-podcast"
    exit 1
fi

# 1e. secrets 目录
if [ ! -f "$COOKIE_FILE" ]; then
    echo "  ⚠ 警告: Cookie 文件不存在 $COOKIE_FILE"
    echo "  同步时将使用浏览器降级模式（如已配置）"
    echo "  正常部署请创建:"
    echo "    mkdir -p $(dirname "$COOKIE_FILE")"
    echo "    # 放入 B 站 cookies（netscape 格式）"
fi

# 1f. 日志目录
if [ ! -d "$LOG_DIR" ]; then
    echo "  创建日志目录 $LOG_DIR ..."
    if [ "$APPLY" = true ]; then
        mkdir -p "$LOG_DIR"
        chown bilibili-podcast:bilibili-podcast "$LOG_DIR"
        echo "  ✓ 日志目录已创建"
    else
        echo "  (干跑，将创建: mkdir -p $LOG_DIR)"
    fi
fi

# 1g. 数据目录
for d in "$MEDIA_ROOT" "$JSON_ROOT" "$RSS_ROOT" "$STATE_DIR"; do
    if [ ! -d "$d" ]; then
        echo "  创建数据目录 $d ..."
        if [ "$APPLY" = true ]; then
            mkdir -p "$d"
            chown bilibili-podcast:bilibili-podcast "$d"
            echo "  ✓ $d 已创建"
        else
            echo "  (干跑，将创建: mkdir -p $d)"
        fi
    fi
done

echo "  ✓ 环境依赖检查通过"

UNCOMMITTED=$(cd "$CODE_DIR" && git status --porcelain 2>/dev/null | wc -l)
if [ "$UNCOMMITTED" -gt 0 ] && [ "$APPLY" = true ]; then
    echo "  ⚠ 代码目录有 $UNCOMMITTED 个未提交文件（通常是 bilibili-podcast-env.sh）"
fi

# 查找系统 Python，优先 3.14，允许 3.13 回退
_python_supported() {
    "$1" -c "import sys; raise SystemExit(0 if (3, 13) <= sys.version_info[:2] < (3, 15) else 1)" \
        >/dev/null 2>&1
}

_supported_python_candidates() {
    printf '%s\n' "${SUPPORTED_PYTHON_CANDIDATES[@]}"
}

_find_system_python() {
    local p
    while IFS= read -r p; do
        if [ -x "$p" ] && _python_supported "$p"; then
            echo "$p"
            return 0
        fi
    done < <(_supported_python_candidates)
    return 1
}

_pip_install() {
    if [ -f "$CONSTRAINTS_FILE" ]; then
        "$VENV_DIR/bin/pip" install -c "$CONSTRAINTS_FILE" "$@"
    else
        "$VENV_DIR/bin/pip" install "$@"
    fi
}

_log_constraints_status() {
    if [ -f "$CONSTRAINTS_FILE" ]; then
        echo "  使用依赖约束: $CONSTRAINTS_FILE"
    else
        echo "  ⚠ 未找到依赖约束文件: $CONSTRAINTS_FILE"
    fi
}

# 下载并编译 Python 3.14.6
_install_python_314() {
    {
        echo "  未找到 Python 3.14/3.13，下载编译 Python ${PYTHON_SOURCE_VERSION} ..."
        local SRC="/tmp/Python-${PYTHON_SOURCE_VERSION}"
        if [ ! -d "$SRC" ]; then
            curl -sL "https://www.python.org/ftp/python/${PYTHON_SOURCE_VERSION}/Python-${PYTHON_SOURCE_VERSION}.tgz" \
                | tar xz -C /tmp 2>&1 | tail -1
        fi
        cd "$SRC"
        echo "  配置 (--enable-shared --prefix=/usr/local) ..."
        ./configure --enable-shared --prefix=/usr/local --quiet 2>&1 | tail -1 | sed 's/^/  /'
        echo "  编译 ..."
        make -j$(nproc) 2>&1 | tail -3 | sed 's/^/  /'
        echo "  安装到 /usr/local ..."
        make install 2>&1 | tail -3 | sed 's/^/  /'
        ldconfig
    } >&2
    echo "/usr/local/bin/python3.14"
}

_verify_required_modules() {
    local entry mod label
    ALL_OK=true
    for entry in "${REQUIRED_PYTHON_MODULES[@]}"; do
        mod="${entry%%:*}"
        label="${entry#*:}"
        _verify_module "$mod" "$label" || ALL_OK=false
    done
}

# 如果 venv 不存在，自动创建
if [ ! -f "$PYTHON_BIN" ]; then
    echo "  ⚠ venv 不存在: $VENV_DIR"
    SYSTEM_PY=$(_find_system_python) || true
    if [ -z "$SYSTEM_PY" ]; then
        if [ "$APPLY" = true ]; then
            SYSTEM_PY=$(_install_python_314)
            PYTHON_BIN="$SYSTEM_PY"
        else
            echo "  (干跑，将下载编译 Python 3.14.6 并创建 venv)"
            SYSTEM_PY="/usr/local/bin/python3.14"
        fi
    else
        echo "  找到系统 Python: $($SYSTEM_PY --version 2>&1)"
    fi
    if [ "$APPLY" = true ]; then
        "$SYSTEM_PY" -m venv "$VENV_DIR"
        echo "  ✓ venv 已创建: $VENV_DIR"
        PYTHON_BIN="$VENV_DIR/bin/python3"
        SYNC_BIN="$VENV_DIR/bin/bilibili-podcast"
    else
        PYTHON_BIN="$SYSTEM_PY"
    fi
fi

if [ -x "$PYTHON_BIN" ]; then
    echo "  ✓ Python: $("$PYTHON_BIN" --version 2>&1)"
elif [ "$APPLY" = true ]; then
    echo "  ✗ Python 不存在或不可执行: $PYTHON_BIN"
    exit 1
else
    echo "  (干跑，目标 Python 尚不存在，将在 --apply 时创建)"
fi

if [ -x "$PYTHON_BIN" ] && ! _python_supported "$PYTHON_BIN"; then
    echo "  ✗ Python 版本不受支持，需 3.13.x 或 3.14.x"
    exit 1
fi

# ── 2. _sqlite3 编译 ──────────────────────────────
echo "▶ [2/9] Python 版本检测 + _sqlite3 编译 ..."
if [ -x "$PYTHON_BIN" ]; then
    PY_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
else
    PY_VER="planned"
fi
echo "  版本: $PY_VER"

SQLITE3_OK=false
if [ ! -x "$PYTHON_BIN" ] && [ "$APPLY" != true ]; then
    echo "  (干跑，将在目标 Python 创建后验证 sqlite3)"
elif "$PYTHON_BIN" -c "import sqlite3" 2>/dev/null; then
    SQLITE3_OK=true
    echo "  ✓ sqlite3"
else
    echo "  ✗ sqlite3 (缺失)"
    if [ "$APPLY" = true ]; then
        if ! rpm -q sqlite-devel &>/dev/null; then
            echo "  安装 sqlite-devel ..."
            dnf install -y sqlite-devel 2>&1 | tail -2 | sed 's/^/  /'
        fi

        PY_FULL=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
        SRC="/tmp/Python-${PY_FULL}"
        if [ ! -d "$SRC" ]; then
            echo "  下载 Python ${PY_FULL} 源码 ..."
            curl -sL "https://www.python.org/ftp/python/${PY_FULL}/Python-${PY_FULL}.tgz" \
                | tar xz -C /tmp 2>&1 | tail -1
        fi

        cd "$SRC"
        echo "  配置 ..."
        ./configure --enable-shared --quiet 2>&1 | tail -1 | sed 's/^/  /'
        make -j$(nproc) 2>&1 | tail -3 | sed 's/^/  /'

        # 查找编译产物
        SO_FILE=$(find "$SRC" -name '_sqlite3*.so' -type f 2>/dev/null | head -1)
        if [ -n "$SO_FILE" ]; then
            DYNLOAD=$("$PYTHON_BIN" -c "import sys; print(next(p for p in sys.path if p.endswith('lib-dynload')))" 2>/dev/null || echo "")
            if [ -z "$DYNLOAD" ]; then
                DYNLOAD=$(dirname "$SO_FILE" 2>/dev/null)
            fi
            if [ -n "$DYNLOAD" ]; then
                cp "$SO_FILE" "${DYNLOAD}/"
                echo "  ✓ _sqlite3.so 安装到 ${DYNLOAD}"
            fi
            SQLITE3_OK=true
        fi

        # 全量编译后重试导入
        if [ "$SQLITE3_OK" != true ]; then
            "$PYTHON_BIN" -c "import sqlite3; print(sqlite3.sqlite_version)" 2>/dev/null && {
                SQLITE3_OK=true
                echo "  ✓ _sqlite3（全量编译后已可用）"
            }
        fi

        # 编译失败 → 尝试用其他兼容 Python 重建 venv
        if [ "$SQLITE3_OK" != true ]; then
            echo "  ⚠ _sqlite3 编译失败，尝试用其他 Python 3.14/3.13 重建 venv"
            ALT_PY=""
            while IFS= read -r p; do
                if [ -x "$p" ] && _python_supported "$p" && "$p" -c "import sqlite3" 2>/dev/null; then
                    ALT_PY="$p"
                    break
                fi
            done < <(_supported_python_candidates)
            if [ -z "$ALT_PY" ]; then
                echo "  ✗ 未找到支持 sqlite3 的 Python 3.14/3.13，中止"
                exit 1
            fi
            TIMESTAMP=$(date +%Y%m%d_%H%M%S)
            mv "$VENV_DIR" "${VENV_DIR}-py${PY_VER}.${TIMESTAMP}"
            "$ALT_PY" -m venv "$VENV_DIR"
            PYTHON_BIN="$VENV_DIR/bin/python3"
            SYNC_BIN="$VENV_DIR/bin/bilibili-podcast"
            SQLITE3_OK=true
            echo "  ✓ venv 已重建 ($("$PYTHON_BIN" --version 2>&1))"
        fi
    else
        echo "  (干跑，将尝试编译 _sqlite3 或回退其他 Python 3.14/3.13)"
    fi
fi

# ── 3. 拉取最新代码 ──────────────────────────────────
echo "▶ [3/9] 拉取最新代码 ..."

CURRENT_BRANCH=$(cd "$CODE_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
echo "  当前分支: $CURRENT_BRANCH"

if [ "$APPLY" = true ]; then
    cd "$CODE_DIR"
    git pull --ff-only 2>&1 | sed 's/^/  /'
    echo "  ✓ 代码已更新"
else
    cd "$CODE_DIR"
    git fetch --dry-run 2>&1 | head -5 | sed 's/^/  /' || true
    LOCAL_REV=$(git rev-parse HEAD 2>/dev/null | cut -c1-8)
    REMOTE_REV=$(git rev-parse @{upstream} 2>/dev/null | cut -c1-8 || echo "unknown")
    echo "  本地: $LOCAL_REV  远端: $REMOTE_REV"
    echo "  (干跑，不执行 pull)"
fi

# ── 4. 安装 Python 包 ────────────────────────────────
echo "▶ [4/9] 安装 Python 包 ..."

if [ "$APPLY" = true ]; then
    _log_constraints_status

    # 先尝试正常安装（含固定 commit 的 bilibili-api-python）
    INSTALL_OK=false
    if PIP_OUTPUT=$(_pip_install -e "$CODE_DIR" 2>&1); then
        INSTALL_OK=true
        echo "$PIP_OUTPUT" | tail -3 | sed 's/^/  /'
    else
        echo "  ⚠ pip install -e 失败（GitHub 网络问题？），回退 PyPI 安装 ..."
        echo "$PIP_OUTPUT" | tail -5 | sed 's/^/  /'
    fi

    # 失败时手动安装所有依赖，bilibili-api-python 走 PyPI
    if [ "$INSTALL_OK" != true ]; then
        _pip_install "${FALLBACK_RUNTIME_PACKAGES[@]}" 2>&1 | tail -3 | sed 's/^/  /'

        # 装回项目本身（不需依赖解析）
        "$VENV_DIR/bin/pip" install -e "$CODE_DIR" --no-deps 2>&1 | tail -3 | sed 's/^/  /'
    fi

    echo "  ✓ 包已更新"
else
    if [ -f "$CONSTRAINTS_FILE" ]; then
        echo "  (干跑，执行命令: pip install -c $CONSTRAINTS_FILE -e $CODE_DIR)"
    else
        echo "  (干跑，执行命令: pip install -e $CODE_DIR)"
    fi
    echo "  (如果 GitHub 不可达，回退: pip install aiohttp PyYAML ... bilibili-api-python)"
fi

# ── 5. 模块验证 ──────────────────────────────────────
echo "▶ [5/9] 模块验证 ..."

_verify_module() {
    local mod="$1"
    local label="${2:-$mod}"
    if "$PYTHON_BIN" -c "import $mod" 2>/dev/null; then
        echo "  ✓ $label"
        return 0
    fi
    echo "  ✗ $label (缺失)"
    return 1
}

_verify_required_modules

# yt-dlp 单独验证
if "$VENV_DIR/bin/yt-dlp" --version >/dev/null 2>&1; then
    echo "  ✓ yt-dlp ($("$VENV_DIR/bin/yt-dlp" --version 2>/dev/null))"
else
    echo "  ✗ yt-dlp (缺失)"
    ALL_OK=false
fi

if [ "$ALL_OK" != true ] && [ "$APPLY" = true ]; then
    echo "  ⚠ 部分模块缺失，尝试安装缺失项 ..."
    _pip_install "${FALLBACK_RUNTIME_PACKAGES[@]}" 2>&1 | tail -3 | sed 's/^/  /'

    # 最终确认
    _verify_required_modules
    if ! "$PYTHON_BIN" -c "import bilibili_api" 2>/dev/null; then
        echo "  ✗ 严重: bilibili-api-python 安装失败，后续步骤可能出错"
    fi
fi

# ── 5b. Playwright/Chromium ──────────────────────────
echo "▶ [5b/9] Playwright 浏览器安装 ..."

_INSTALL_PLAYWRIGHT=false
if "$PYTHON_BIN" -c "import playwright" 2>/dev/null; then
    echo "  ✓ playwright 包已安装"
else
    if [ "$APPLY" = true ]; then
        echo "  安装 playwright 包（优先国内镜像）..."
        PIP_OUT="$( _pip_install -i https://pypi.tuna.tsinghua.edu.cn/simple "$PLAYWRIGHT_PACKAGE_SPEC" 2>&1 )" || \
        PIP_OUT="$( _pip_install "$PLAYWRIGHT_PACKAGE_SPEC" 2>&1 )" || {
            echo "  ✗ playwright 安装失败"
            echo "$PIP_OUT" | tail -5 | sed 's/^/  /'
            exit 1
        }
        echo "$PIP_OUT" | tail -3 | sed 's/^/  /'
        echo "  ✓ playwright 已安装"
        _INSTALL_PLAYWRIGHT=true
    else
        echo "  (干跑，将安装: pip install playwright)"
    fi
fi

BROWSER_DIR="${PLAYWRIGHT_BROWSERS_PATH:-<server_path>}"

_INSTALL_CHROMIUM=false
if [ "$APPLY" = true ]; then
    CHROMIUM_DIR="$(find "$BROWSER_DIR" -maxdepth 1 -type d -name 'chromium-*' 2>/dev/null | head -1 || true)"
    if [ -n "$CHROMIUM_DIR" ]; then
        echo "  ✓ Chromium browser 已存在 ($(basename "$CHROMIUM_DIR"))"
    else
        echo "  安装 Chromium browser ..."
        PW_OUT="$( PLAYWRIGHT_BROWSERS_PATH="$BROWSER_DIR" "$VENV_DIR/bin/playwright" install chromium 2>&1 )" || {
            echo "  ✗ Chromium 安装失败"
            echo "$PW_OUT" | tail -5 | sed 's/^/  /'
            exit 1
        }
        echo "$PW_OUT" | tail -3 | sed 's/^/  /'
        chown -R bilibili-podcast:bilibili-podcast "$BROWSER_DIR" 2>/dev/null || true
        echo "  ✓ Chromium 已安装到 $BROWSER_DIR"
        _INSTALL_CHROMIUM=true
    fi
fi

# 强验证：import + launch
if [ "$APPLY" = true ]; then
    echo "  验证 browser launch ..."
    VERIFY_OUT="$( sudo -u bilibili-podcast PLAYWRIGHT_BROWSERS_PATH="$BROWSER_DIR" "$PYTHON_BIN" -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    v = b.version
    b.close()
    print(v)
" 2>&1 )" || {
        echo "  ✗ Chromium launch 验证失败"
        echo "$VERIFY_OUT" | sed 's/^/  /'
        exit 1
    }
    echo "  ✓ Chromium launch OK, version: $VERIFY_OUT"
fi

# ── 5c. 持久化 Playwright 环境变量 ─────────────────────
echo "▶ [5c/9] 持久化 PLAYWRIGHT_BROWSERS_PATH 环境变量 ..."

if [ "$APPLY" = true ]; then
    # 5c.1 bilibili-podcast-env.sh
    ENV_FILE="$APP_DIR/bilibili-podcast-env.sh"
    if [ -f "$ENV_FILE" ]; then
        if grep -q "PLAYWRIGHT_BROWSERS_PATH" "$ENV_FILE" 2>/dev/null; then
            sed -i "s|^export PLAYWRIGHT_BROWSERS_PATH=.*|export PLAYWRIGHT_BROWSERS_PATH=$BROWSER_DIR|" "$ENV_FILE"
        else
            echo "export PLAYWRIGHT_BROWSERS_PATH=$BROWSER_DIR" >> "$ENV_FILE"
        fi
        echo "  ✓ bilibili-podcast-env.sh: PLAYWRIGHT_BROWSERS_PATH=$BROWSER_DIR"
    else
        echo "  ⚠ bilibili-podcast-env.sh 不存在 ($ENV_FILE)，跳过"
    fi

    # 5c.2 systemd unit
    UNIT_FILE="/etc/systemd/system/bilibili-podcast-web.service"
    if [ -f "$UNIT_FILE" ]; then
        if grep -q "PLAYWRIGHT_BROWSERS_PATH" "$UNIT_FILE" 2>/dev/null; then
            sed -i "s|^Environment=PLAYWRIGHT_BROWSERS_PATH=.*|Environment=PLAYWRIGHT_BROWSERS_PATH=$BROWSER_DIR|" "$UNIT_FILE"
        else
            sed -i "/^\[Service\]/a Environment=PLAYWRIGHT_BROWSERS_PATH=$BROWSER_DIR" "$UNIT_FILE"
        fi
        systemctl daemon-reload 2>/dev/null || true
        if systemctl is-active -q bilibili-podcast-web.service 2>/dev/null; then
            if systemctl restart bilibili-podcast-web.service; then
                echo "  ✓ bilibili-podcast-web.service 已重启（新环境变量生效）"
            else
                echo "  ⚠ bilibili-podcast-web.service 重启失败，请检查:"
                echo "    systemctl status bilibili-podcast-web.service"
                echo "    journalctl -u bilibili-podcast-web.service -n 50 --no-pager"
            fi
        fi
        echo "  ✓ systemd: $UNIT_FILE 已更新"
    else
        echo "  ⚠ systemd unit 不存在 ($UNIT_FILE)，跳过"
        echo "  手动添加:"
        echo "    [Service]"
        echo "    Environment=PLAYWRIGHT_BROWSERS_PATH=$BROWSER_DIR"
    fi

    # 5c.3 标准化 systemd/secrets 运行配置
    STANDARDIZE_SCRIPT="$CODE_DIR/scripts/standardize-runtime-config.sh"
    if [ -x "$STANDARDIZE_SCRIPT" ]; then
        BILIBILI_PODCAST_APP_DIR="$APP_DIR" \
        BILIBILI_PODCAST_ENV_FILE="$APP_DIR/bilibili-podcast-env.sh" \
        BILIBILI_PODCAST_WEB_ENV_FILE="$WEB_ENV_FILE" \
        "$STANDARDIZE_SCRIPT" --apply 2>&1 | sed 's/^/  /'
        echo "  ✓ systemd/secrets 运行配置已标准化"
    else
        echo "  ⚠ 标准化脚本不存在或不可执行: $STANDARDIZE_SCRIPT"
    fi

    # 5c.4 CLI wrapper — source bilibili-podcast-env.sh + 自动切 bilibili-podcast 用户
    CLI_WRAPPER="$APP_DIR/bilibili-podcast-admin"
    cat > "$CLI_WRAPPER" << CLIEOF
#!/bin/bash
# bilibili-podcast-admin CLI wrapper
# 1) 加载 bilibili-podcast-env.sh（PLAYWRIGHT_BROWSERS_PATH 等）
# 2) 非 bilibili-podcast 用户时自动 sudo -u bilibili-podcast（SQLite WAL 权限）
# 3) 已为 bilibili-podcast 时直接 exec 真实命令
SCRIPT_PATH="\$0"
if command -v readlink >/dev/null 2>&1; then
    RESOLVED="\$(readlink -f "\$SCRIPT_PATH" 2>/dev/null || true)"
    if [ -n "\$RESOLVED" ]; then
        SCRIPT_PATH="\$RESOLVED"
    fi
fi
APP_DIR="\$(cd "\$(dirname "\$SCRIPT_PATH")" && pwd)"
source "\$APP_DIR/bilibili-podcast-env.sh" 2>/dev/null || true
if [ "\$(id -un)" != "bilibili-podcast" ]; then
    exec sudo -u bilibili-podcast \
        PLAYWRIGHT_BROWSERS_PATH="\${PLAYWRIGHT_BROWSERS_PATH:-<server_path>}" \
        "\$SCRIPT_PATH" "\$@"
fi
exec "\$APP_DIR/venv/bin/bilibili-podcast-admin" "\$@"
CLIEOF
    chown bilibili-podcast:bilibili-podcast "$CLI_WRAPPER"
    chmod 755 "$CLI_WRAPPER"
    echo "  ✓ CLI wrapper: $CLI_WRAPPER"

    # 可选: 创建 /usr/local/bin/bilibili-podcast-admin symlink（便利性，不覆盖已有命令）
    GLOBAL_SYMLINK="/usr/local/bin/bilibili-podcast-admin"
    if [ ! -e "$GLOBAL_SYMLINK" ]; then
        ln -s "$CLI_WRAPPER" "$GLOBAL_SYMLINK"
        echo "  ✓ $GLOBAL_SYMLINK -> $CLI_WRAPPER"
    elif [ "$(readlink -f "$GLOBAL_SYMLINK" 2>/dev/null || true)" = "$CLI_WRAPPER" ]; then
        echo "  ✓ bilibili-podcast-admin PATH 入口已指向 wrapper"
    else
        echo "  ⚠ bilibili-podcast-admin 已存在: $GLOBAL_SYMLINK"
        echo "    请显式使用: $CLI_WRAPPER"
    fi

    echo "  ✓ 环境变量持久化完成"
else
    echo "  (干跑，将更新 systemd unit + bilibili-podcast-env.sh + CLI wrapper)"
    echo "    Environment=PLAYWRIGHT_BROWSERS_PATH=$BROWSER_DIR"
    echo "    scripts/standardize-runtime-config.sh --apply"
fi

# ── 6. 迁移 YAML → SQLite ────────────────────────────
echo "▶ [6/9] 迁移 YAML 配置到 SQLite ..."

if [ "$APPLY" = true ]; then
    if [ -f "$DB_PATH" ]; then
        echo "  DB 已存在: $DB_PATH"
        BACKUP="${DB_PATH}.bak.$(date +%Y%m%d_%H%M%S)"
        cp "$DB_PATH" "$BACKUP"
        echo "  已备份: $BACKUP"
    fi

    "$VENV_DIR/bin/python3" "$CODE_DIR/scripts/migrate_yaml_to_sqlite" \
        --config-dir "$CONFIG_DIR" \
        --state-root "$STATE_DIR" \
        --db-path "$DB_PATH" 2>&1 | sed 's/^/  /'

    if [ -f "$DB_PATH" ] && [ "$(stat -c '%U' "$DB_PATH" 2>/dev/null || stat -f '%Su' "$DB_PATH" 2>/dev/null)" != "bilibili-podcast" ]; then
        chown bilibili-podcast:bilibili-podcast "$DB_PATH" 2>/dev/null || true
        echo "  ✓ DB 属主修正为 bilibili-podcast"
    fi

    echo "  ✓ 迁移完成"
else
    echo "  (干跑，执行命令:)"
    echo "    python3 scripts/migrate_yaml_to_sqlite \\"
    echo "      --config-dir $CONFIG_DIR \\"
    echo "      --state-root $STATE_DIR \\"
    echo "      --db-path $DB_PATH"
fi

# ── 7. 生成新 wrapper 脚本 ──────────────────────────
echo "▶ [7/9] 生成支持 --config-db 的 wrapper 脚本 + 安装 crontab ..."

# 加载服务器运行环境（补充 BILIBILI_PODCAST_MEDIA_BASE_URL 等变量）
ENV_FILE="$APP_DIR/bilibili-podcast-env.sh"
if [ -f "$ENV_FILE" ]; then
    # 占位符守卫: 避免误 source 含有 <media_base_url> 等未替换模板值的 env 文件
    if grep -qE '<[^>]+>' "$ENV_FILE"; then
        echo "  ✗ $ENV_FILE 仍包含未替换占位符:"
        grep -nE '<[^>]+>' "$ENV_FILE" | sed 's/^/    /'
        exit 1
    fi
    set -a
    source "$ENV_FILE"
    set +a
fi

if [ "$APPLY" = true ]; then
    "$VENV_DIR/bin/python3" "$CODE_DIR/scripts/bilibili-podcast-crontab" \
        --config-db "$DB_PATH" \
        --script-dir "$APP_DIR/auto" \
        --cron-user bilibili-podcast \
        --apply \
        --force 2>&1 | sed 's/^/  /'

    SAMPLE_SCRIPT=$(ls "$APP_DIR/auto/"run_*.sh 2>/dev/null | head -1)
    if [ -n "$SAMPLE_SCRIPT" ] && grep -q "CONFIG_DB" "$SAMPLE_SCRIPT"; then
        echo "  ✓ Wrapper 脚本已更新 (嵌入 CONFIG_DB 路径)"
    fi

    chown -R bilibili-podcast:bilibili-podcast "$APP_DIR/auto" 2>/dev/null || true
else
    echo "  (干跑，加载 $ENV_FILE，执行命令:)"
    echo "    bilibili-podcast-crontab --config-db $DB_PATH --script-dir $APP_DIR/auto --cron-user bilibili-podcast --apply --force"
fi

# ── 8. 验证 ──────────────────────────────────────────
echo "▶ [8/9] 验证 ..."

if [ "$APPLY" = true ] && [ -f "$DB_PATH" ]; then
    COUNT=$("$VENV_DIR/bin/python3" -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
conn.row_factory = sqlite3.Row
count = conn.execute('SELECT COUNT(*) as c FROM series WHERE enabled=1').fetchone()['c']
print(count)
conn.close()
")
    echo "  DB 中已启用配置数: $COUNT"

    PAID_PREVIEW=$("$VENV_DIR/bin/python3" -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
conn.row_factory = sqlite3.Row
row = conn.execute(\"SELECT COUNT(*) AS count FROM filter_rule WHERE rule_type='exclude_paid' AND value='false'\").fetchone()
print(row['count'] if row else 0)
conn.close()
")
    if [ "$PAID_PREVIEW" -gt 0 ]; then
        echo "  ✓ 至少一个系列保留 exclude_paid=false"
    else
        echo "  ⚠ 未发现 exclude_paid=false 规则，如有付费预览系列请确认配置"
    fi

    echo "  执行干跑测试（跳过：export SMOKE_SYNC=1 启用）..."
    if [ "${SMOKE_SYNC:-0}" = "1" ]; then
        FIRST_SERIES=$("$VENV_DIR/bin/python3" -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT series FROM series WHERE enabled=1 ORDER BY series LIMIT 1').fetchall()
conn.close()
for r in rows: print(r['series'])
")
        "$SYNC_BIN" \
            --config-db "$DB_PATH" \
            --series "$FIRST_SERIES" \
            --cookie-file "$COOKIE_FILE" \
            --token "__MEDIA_PLACEHOLDER__" \
            --media-root "$MEDIA_ROOT" \
            --json-root "$JSON_ROOT" \
            --rss-root "$RSS_ROOT" \
            --state-root "$STATE_DIR" \
            --lock-file "$LOCK_FILE" \
            --log-dir "$LOG_DIR" \
            --browser-user-data-root "$APP_DIR/browser-profiles" \
            --log-level DEBUG 2>&1 | tail -15 | sed 's/^/  /'
    else
        echo "  (跳过，设 SMOKE_SYNC=1 执行真实 API 测试)"
    fi

    echo "  ✓ 验证完成"

    # web 健康检查（仅 warning，不阻断）
    if curl -fsS http://127.0.0.1:8743/login >/dev/null 2>&1; then
        echo "  ✓ bilibili-podcast-web /login 可访问"
    else
        echo "  ⚠ bilibili-podcast-web /login 不可访问，请检查:"
        echo "    systemctl status bilibili-podcast-web.service"
        echo "    journalctl -u bilibili-podcast-web.service -n 50 --no-pager"
    fi
elif [ ! "$APPLY" = true ]; then
    echo "  (干跑模式，跳过实际验证)"
else
    echo "  ⚠ DB 文件不存在，跳过验证"
fi

# ── 清理 ─────────────────────────────────────────────
if [ "$APPLY" = true ]; then
    echo ""
    echo "▶ 清理临时文件 ..."
    rm -rf /tmp/Python-3.14.6 /tmp/Python-3.14.* /tmp/Python-3.13.*  2>/dev/null || true
    echo "  ✓ 已清理 /tmp/Python-*"
fi

echo ""
echo "========================================"
echo " 部署 $([ "$APPLY" = true ] && echo "完成" || echo "计划 (使用 --apply 执行)")"
echo "========================================"

if [ "$APPLY" = true ]; then
    echo ""
    echo "后续步骤:"
    echo "  1. 验证 cron: sudo crontab -l -u bilibili-podcast"
    echo "  2. 监控下次 cron 触发是否正常"
    echo ""
    echo "回滚:"
    echo "  - DB: cp $DB_PATH.bak.<timestamp> $DB_PATH"
    echo "  - Wrapper: bilibili-podcast-crontab --config-dir $CONFIG_DIR --force --apply"
    echo "  - 代码: git checkout main"
fi
