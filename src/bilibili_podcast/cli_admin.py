"""bilibili-podcast-admin CLI — manage series, filters, sync, and cron from the terminal."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import db
from .services import validation
from .services.config_service import ConfigService
from .services.filter_service import FilterRuleService
from .services.sync_policy_service import SyncPolicyService, SYNC_POLICY_DEFAULTS
from .services.preview_service import PreviewService
from .services.scheduler_service import SchedulerService
from .services.series_removal_service import SeriesRemovalPlan, SeriesRemovalService
from .utils.bilibili_url import parse_space_source

DB_ENV_VAR = "BILIBILI_PODCAST_CONFIG_DB"

# Exit codes per HANDOFF spec
EXIT_SUCCESS = 0
EXIT_USER_CANCEL = 1
EXIT_VALIDATION = 1
EXIT_ARGS_ERROR = 2
EXIT_DB_ERROR = 4
EXIT_SYNC_FAIL = 5


def _sanitize(text: str) -> str:
    """Remove or mask sensitive patterns from output text."""
    # Mask common sensitive patterns
    text = re.sub(r'(token|secret|password)\s*[=:]\s*\S+', r'\1=***', text, flags=re.IGNORECASE)
    return text


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a simple aligned table."""
    if not rows:
        print("  （无数据）")
        return
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    print(fmt.format(*headers))
    print("  " + "  ".join("-" * w for w in col_widths))
    for row in rows:
        print(fmt.format(*row))


def _bool_str(val: Any) -> str:
    return "✅ 启用" if val else "❌ 禁用"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _run_json(args: argparse.Namespace, data: Any) -> None:
    """Print data as JSON and exit."""
    print(json.dumps(data, ensure_ascii=False, default=str))
    sys.exit(EXIT_SUCCESS)


def _should_json(args: argparse.Namespace) -> bool:
    """Check if --json output is requested."""
    return getattr(args, "json", False)


def _ts_str(ts: int) -> str:
    if not ts or ts <= 0:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _load_full_config(conn, series: str) -> dict[str, Any]:
    cs = ConfigService(conn)
    return cs.load_full_config(series) or {}


def _resolve_bilibili_url(url: str) -> dict[str, Any]:
    import asyncio
    from .web.resolver import resolve_url

    return asyncio.run(resolve_url(url))


def _with_fallback_source(result: dict[str, Any], fallback_source: dict[str, Any] | None) -> dict[str, Any]:
    if not fallback_source:
        return result
    source = dict(result.get("source") or {})
    source["type"] = source.get("type") or fallback_source["type"]
    source["uid"] = source.get("uid") or fallback_source["uid"]
    source["sid"] = source.get("sid")
    source["space_url"] = source.get("space_url") or fallback_source["space_url"]
    return {**result, "source": source}


# ── Subcommand handlers ────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> None:
    """List all series with status."""
    db_path = _get_db(args)
    with db.transaction(db_path) as conn:
        rows = conn.execute("""
            SELECT s.series, s.enabled, s.title, s.author,
                   ss.type AS source_type,
                   sy.last_success_at, sy.rate_limited_until
            FROM series s
            LEFT JOIN series_source ss ON ss.series = s.series
            LEFT JOIN sync_state sy ON sy.series = s.series
            ORDER BY s.series
        """).fetchall()

    json_rows = []
    for r in rows:
        json_rows.append({
            "series": r["series"],
            "enabled": bool(r["enabled"]),
            "title": r["title"],
            "author": r["author"],
            "source_type": r["source_type"],
            "last_success_at": r["last_success_at"] or None,
            "rate_limited_until": r["rate_limited_until"] or None,
        })
    if _should_json(args):
        _run_json(args, json_rows)

    headers = ["系列", "标题", "作者", "来源", "状态", "上次同步", "限流至"]
    data = []
    for r in json_rows:
        data.append([
            r["series"],
            r["title"] or "—",
            r["author"] or "—",
            r["source_type"] or "—",
            _bool_str(r["enabled"]),
            _ts_str(r["last_success_at"] or 0),
            _ts_str(r["rate_limited_until"] or 0),
        ])
    _print_table(headers, data)


def cmd_show(args: argparse.Namespace) -> None:
    """Show full config for a series."""
    db_path = _get_db(args)
    with db.transaction(db_path) as conn:
        cfg = _load_full_config(conn, args.series)

    if not cfg:
        err = f"❌ 系列不存在: {args.series}"
        if _should_json(args):
            _run_json(args, {"error": err})
        print(err)
        sys.exit(EXIT_VALIDATION)

    # JSON mode
    if _should_json(args):
        _run_json(args, cfg)

    s = cfg["series"]
    src = cfg["source"]
    sp = cfg["sync"]
    pp = cfg["paid_preview"]
    st = cfg["state"]

    print(f"\n=== {args.series} ===")
    print(f"  标题: {s.get('title', '—')}")
    print(f"  作者: {s.get('author', '—')}")
    print(f"  状态: {_bool_str(s.get('enabled', 0))}")
    print(f"  封面: {s.get('cover_art') or '（默认 B 站头像）'}")
    print(f"  分类: {s.get('category', '—')}")
    print(f"  语言: {s.get('lang', 'zh-CN')}")

    print(f"\n  📡 来源")
    print(f"    类型: {src.get('type', 'space')}")
    print(f"    UID: {src.get('uid', '—')}")
    if src.get("sid"):
        print(f"    SID: {src['sid']}")
    if src.get("space_url"):
        print(f"    URL: {src['space_url']}")

    print(f"\n  ⚙️  同步策略")
    if sp:
        print(f"    分页: {sp.get('page_size', 20)} | 增量: {sp.get('incremental_page_size', 5)} | 最大: {sp.get('max_pages', 10)}")
        print(f"    质量: {sp.get('quality', '64K')} | 保留: {sp.get('keep_last', 100)} | 周期: {sp.get('update_period', '12h')}")
        print(f"    策略: {sp.get('fetch_strategy', 'api_first')} | 回退: {_bool_str(sp.get('browser_fallback', 0))}")
        print(f"    时长: {sp.get('min_duration_seconds', 0)}s - {sp.get('max_duration_seconds', 0)}s")

    print(f"\n  🔍 过滤规则 ({len(cfg['filters'])} 条)")
    if cfg["filters"]:
        fr_headers = ["ID", "类型", "值", "启用"]
        fr_rows = []
        for fr in cfg["filters"]:
            fr_rows.append([str(fr["id"]), fr["rule_type"], fr["value"], _bool_str(fr["enabled"])])
        _print_table(fr_headers, fr_rows)
    if pp:
        print(f"    付费预览: {_bool_str(pp.get('enabled', 0))} | 重试: {pp.get('retry_after_days', 4)}天")

    print(f"\n  ⏰ Cron ({len(cfg['cron'])} 条)")
    for sched in cfg["cron"]:
        print(f"    {sched}")

    print(f"\n  📊 同步状态")
    print(f"    上次尝试: {_ts_str(st.get('last_attempt_at', 0))}")
    print(f"    上次成功: {_ts_str(st.get('last_success_at', 0))}")
    if st.get("rate_limited_until", 0):
        print(f"    限流至: {_ts_str(st['rate_limited_until'])}")
    print()


def _series_removal_service(args: argparse.Namespace, db_path: str) -> SeriesRemovalService:
    return SeriesRemovalService(
        db_path,
        media_root=args.media_root,
        json_root=args.json_root,
        rss_root=args.rss_root,
        published_rss_root=args.published_rss_root,
        cron_script_dir=args.cron_script_dir,
        browser_user_data_root=args.browser_user_data_root,
        users_conf=args.users_conf,
    )


def _print_removal_plan(plan: SeriesRemovalPlan) -> None:
    print(f"\n=== 移除计划: {plan.series} ===")
    print(f"  标题: {plan.title}")
    print(f"  UID: {plan.uid if plan.uid is not None else '—'}")
    print(f"  Media: {plan.media_dir} ({plan.media_files} files)")
    print(f"  JSON: {plan.json_dir} ({plan.json_files} files)")
    print(f"  Master RSS: {plan.master_rss} ({'存在' if plan.master_rss_exists else '不存在'})")
    print(f"  用户 RSS: {len(plan.published_rss_files)} files")
    print(f"  Cron wrapper: {plan.wrapper_script} ({'存在' if plan.wrapper_exists else '不存在'})")
    print(f"  Browser profile: {plan.browser_profile_dir} ({plan.browser_profile_files} files)")
    print(f"  用户配置显式引用: {plan.users_conf_references}")


def _remove_series_list(args: argparse.Namespace, series_list: list[str]) -> None:
    db_path = _get_db(args)
    service = _series_removal_service(args, db_path)
    plans = [service.plan(series) for series in series_list]

    if _should_json(args) and not args.apply:
        _run_json(args, {"apply": False, "series": [plan.to_dict() for plan in plans]})
    for plan in plans:
        _print_removal_plan(plan)

    if not args.apply:
        print("\n只预览，未执行删除。使用 --apply 真正移除。")
        return

    yes = args.yes or getattr(args, "remove_yes", False)
    if not yes:
        names = ", ".join(series_list)
        confirm = input(f"\n⚠️  确认永久移除 {names}？输入 remove 确认: ").strip().lower()
        if confirm != "remove":
            print("已取消")
            return

    removed: list[dict[str, Any]] = []
    from .sync import process_lock

    with process_lock(args.lock_file):
        scheduler = SchedulerService(
            db_path,
            crontab_script=_find_crontab_bin(),
            cron_script_dir=args.cron_script_dir,
        )
        for plan in plans:
            result = scheduler.remove_series_schedule(plan.series, delete_units=True)
            if result.returncode != 0:
                print(
                    f"❌ 移除 {plan.series} 调度失败: "
                    f"{result.stderr or result.error or 'unknown error'}",
                    file=sys.stderr,
                )
                sys.exit(EXIT_SYNC_FAIL)
            removed.append(service.remove(plan.series).to_dict())
            print(f"✅ 已移除系列: {plan.series}")

    print("⚠️  远端 RSS 节点不在本命令控制范围内；请确认发布同步端已删除对应 XML。")
    if _should_json(args):
        _run_json(args, {"apply": True, "removed": removed})


def cmd_remove_series(args: argparse.Namespace) -> None:
    """Preview or permanently remove one series and its local artifacts."""
    _remove_series_list(args, [args.series])


def cmd_remove_up(args: argparse.Namespace) -> None:
    """Preview or permanently remove all series belonging to one UP UID."""
    db_path = _get_db(args)
    service = _series_removal_service(args, db_path)
    series_list = service.list_series_for_uid(args.uid)
    if not series_list:
        print(f"❌ 未找到 UID={args.uid} 对应的系列")
        sys.exit(EXIT_VALIDATION)
    _remove_series_list(args, series_list)


def cmd_add(args: argparse.Namespace) -> None:
    """Add a new series. Interactive unless non-interactive params provided."""
    db_path = _get_db(args)

    # Non-interactive mode if --series or --url is provided
    if args.series or args.url:
        _cmd_add_noninteractive(args, db_path)
        return

    # ── Interactive wizard ─────────────────────────────────────────
    print("=== 新增系列 ===")
    print("输入 B 站 URL 或 UID，或者直接回车进入手动模式。\n")

    url = input("B 站 URL / UID: ").strip()

    draft = None
    fallback_source = parse_space_source(url)
    if url:
        try:
            result = _resolve_bilibili_url(url)
            if result.get("error"):
                print(f"⚠️ 解析失败: {result['error']}")
                print("将进入手动模式。\n")
            else:
                draft = _with_fallback_source(result, fallback_source)
                print(f"\n✅ 解析成功！发现: {draft.get('title', '?')}")
                if draft.get("videos"):
                    print(f"   最近 {len(draft['videos'])} 个视频:")
                    for v in draft["videos"][:5]:
                        print(f"   • {v.get('title', '?')} ({v.get('bvid', '?')})")
                print()
        except Exception as e:
            print(f"⚠️ 解析异常: {e}，进入手动模式\n")
        if draft is None and fallback_source:
            print(f"⚠️ 使用本地 URL 解析结果: UID={fallback_source['uid']}\n")
            draft = {"source": fallback_source}

    # Collect series data
    data = _interactive_collect_series(draft)
    if data is None:
        print("已取消")
        return

    source = _interactive_collect_source(data.get("source", {}))

    sync = _interactive_collect_sync({})

    print("\n=== 过滤规则设置 ===")
    filters = _interactive_collect_filters()

    print("\n=== 付费预览设置 ===")
    paid_preview_enabled = _prompt_bool("启用付费预览", False)
    retry_days = _prompt_int("重试天数", 4, 1)

    print("\n=== Cron 设置 ===")
    cron_schedules = _interactive_collect_cron()

    # Summary
    print("\n" + "=" * 50)
    print("配置摘要")
    print("=" * 50)
    print(f"  系列标识: {data['series']}")
    print(f"  标题: {data['title']}")
    print(f"  作者: {data['author']}")
    print(f"  来源: {source['type']} (UID={source['uid']})")
    print(f"  同步策略: 质量={sync.get('quality', '64K')}, 保留={sync.get('keep_last', 100)}")
    print(f"  过滤规则: {sum(len(values) for values in filters)} 条")
    print(f"  Cron: {len(cron_schedules)} 条")
    print()

    add_yes = args.yes or getattr(args, "add_yes", False)
    add_dry_run = args.dry_run or getattr(args, "add_dry_run", False)

    if add_dry_run:
        print("⚠️  --dry-run 模式，不写入数据库\n")
        return

    if not add_yes:
        confirm = input("确认保存? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return

    # Save to DB
    with db.transaction(db_path) as conn:
        inserted = conn.execute(
            """INSERT INTO series (series, enabled, title, description,
                                  author, cover_art, category,
                                  subcategories, explicit, lang)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(series) DO NOTHING""",
            (data["series"], data["title"], data.get("description", ""),
             data["author"], data.get("cover_art", ""),
             data.get("category", ""), data.get("subcategories", "[]"),
             data.get("lang", "zh-CN")),
        )
        if inserted.rowcount == 0:
            print(f"❌ 系列标识 '{data['series']}' 已存在")
            sys.exit(EXIT_VALIDATION)
        conn.execute(
            """INSERT INTO series_source (series, space_url, uid, type, sid)
               VALUES (?, ?, ?, ?, ?)""",
            (data["series"], source.get("space_url", ""),
             source.get("uid"), source["type"], source.get("sid")),
        )
        conn.execute(
            """INSERT INTO sync_policy (
                   series, page_size, incremental_page_size, max_pages,
                   max_requests_per_series, request_interval_seconds,
                   request_jitter_seconds, rate_limit_cooldown_seconds,
                   update_period, format, quality, fetch_strategy, keep_last,
                   browser_fallback, browser_wait_min_seconds,
                   browser_wait_max_seconds, browser_fallback_cooldown_seconds,
                   require_paid_state_confirmation,
                   min_duration_seconds, max_duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data["series"],
             sync.get("page_size", 20), sync.get("incremental_page_size", 5),
             sync.get("max_pages", 10), sync.get("max_requests_per_series", 8),
             sync.get("request_interval_seconds", 2.0),
             sync.get("request_jitter_seconds", 0.5),
             sync.get("rate_limit_cooldown_seconds", 21600),
             sync.get("update_period", "12h"), sync.get("format", "audio"),
             sync.get("quality", "64K"), sync.get("fetch_strategy", "api_first"),
             sync.get("keep_last", 100), int(sync.get("browser_fallback", False)),
             sync.get("browser_wait_min_seconds", 4.0),
             sync.get("browser_wait_max_seconds", 8.0),
             sync.get("browser_fallback_cooldown_seconds", 3600),
             int(sync.get("require_paid_state_confirmation", False)),
             sync.get("min_duration_seconds", 0),
             sync.get("max_duration_seconds", 0)),
        )

        # Filter rules
        pos = 0
        for bvid in filters[0]:  # exclude_bvids
            conn.execute("INSERT INTO filter_rule (series, rule_type, value, position) VALUES (?, ?, ?, ?)",
                         (data["series"], "exclude_bvid", bvid, pos)); pos += 1
        for bvid in filters[1]:  # advertisement_bvids
            conn.execute("INSERT INTO filter_rule (series, rule_type, value, position) VALUES (?, ?, ?, ?)",
                         (data["series"], "advertisement_bvid", bvid, pos)); pos += 1
        for kw in filters[2]:  # exclude_keywords
            conn.execute("INSERT INTO filter_rule (series, rule_type, value, position) VALUES (?, ?, ?, ?)",
                         (data["series"], "exclude_keyword", kw, pos)); pos += 1
        for kw in filters[3]:  # advertisement_keywords
            conn.execute("INSERT INTO filter_rule (series, rule_type, value, position) VALUES (?, ?, ?, ?)",
                         (data["series"], "advertisement_keyword", kw, pos)); pos += 1
        for kw in filters[4]:  # include_keywords
            conn.execute("INSERT INTO filter_rule (series, rule_type, value, position) VALUES (?, ?, ?, ?)",
                         (data["series"], "include_keyword", kw, pos)); pos += 1
        for season_id in filters[5]:  # exclude_season_ids
            conn.execute("INSERT INTO filter_rule (series, rule_type, value, position) VALUES (?, ?, ?, ?)",
                         (data["series"], "exclude_season_id", str(season_id), pos)); pos += 1

        conn.execute(
            """INSERT INTO paid_preview_policy (series, enabled, retry_after_days)
               VALUES (?, ?, ?)""",
            (data["series"], int(paid_preview_enabled), retry_days),
        )

        # Cron
        for pos, sched in enumerate(cron_schedules):
            conn.execute(
                "INSERT INTO cron_schedule (series, enabled, schedule, position) VALUES (?, 1, ?, ?)",
                (data["series"], sched, pos),
            )

    print(f"\n✅ 系列 {data['series']} 已保存！")
    print(f"   执行 dry-run: bilibili-podcast-admin preview {data['series']}")
    print(f"   启动同步: bilibili-podcast-admin sync {data['series']} --apply")
    print()


def _cmd_add_noninteractive(args: argparse.Namespace, db_path: str) -> None:
    """Non-interactive series add using CLI params."""
    series = args.series
    if not series:
        print("❌ 非交互模式需要 --series 参数")
        sys.exit(EXIT_ARGS_ERROR)

    add_dry_run = args.dry_run or getattr(args, "add_dry_run", False)
    add_yes = args.yes or getattr(args, "add_yes", False)

    # Validate slug
    if not validation.validate_slug(series):
        print(f"❌ 系列标识 '{series}' 格式无效（只允许小写字母、数字、横线和下划线）")
        sys.exit(EXIT_VALIDATION)

    if not add_yes and not add_dry_run:
        # Non-TTY: require --yes
        if not sys.stdin.isatty():
            print("❌ 非交互模式下需要 --yes 参数以确认保存")
            sys.exit(EXIT_ARGS_ERROR)

    # Resolve URL if provided. UID/source extraction is local and must not
    # depend on Bilibili API availability.
    draft = None
    fallback_source = parse_space_source(args.url)
    if args.url:
        try:
            result = _resolve_bilibili_url(args.url)
            if result.get("error"):
                print(f"⚠️ 解析失败: {result['error']}")
            else:
                draft = _with_fallback_source(result, fallback_source)
        except Exception as e:
            print(f"⚠️ 解析异常: {e}")
        if draft is None and fallback_source:
            print(f"⚠️ 使用本地 URL 解析结果: UID={fallback_source['uid']}")
            draft = {"source": fallback_source}

    # Check slug uniqueness / load existing config for --update-existing
    existing_series: dict[str, Any] = {}
    existing_sync: dict[str, Any] = {}
    existing_source: dict[str, Any] = {}
    existing_pp: dict[str, Any] = {}
    with db.transaction(db_path) as conn:
        existing_row = conn.execute("SELECT 1 FROM series WHERE series=?", (series,)).fetchone()
        if existing_row:
            if args.update_existing:
                print(f"⚠️ 系列 '{series}' 已存在，--update-existing 仅更新显式传入字段")
                cfg = _load_full_config(conn, series)
                existing_series = cfg.get("series", {})
                existing_sync = cfg.get("sync", {})
                existing_source = cfg.get("source", {})
                existing_pp = cfg.get("paid_preview", {})
            else:
                print(f"❌ 系列标识 '{series}' 已存在（使用 --update-existing 覆盖更新）")
                sys.exit(EXIT_VALIDATION)

    def _esv(key: str, default: str = "") -> str:
        return str(existing_series.get(key, default)) if existing_series else default

    # Build data: explicit args → draft → existing (--update-existing) → defaults
    data = {
        "series": series,
        "title": args.title or (draft.get("title", "") if draft else "") or _esv("title", ""),
        "author": args.author or (draft.get("author", "") if draft else "") or _esv("author", ""),
        "description": args.description if args.description is not None else _esv("description", ""),
        "cover_art": args.cover_art if args.cover_art is not None else _esv("cover_art", ""),
        "category": args.category if args.category is not None else _esv("category", ""),
        "lang": args.lang if args.lang is not None else _esv("lang", "zh-CN"),
        "subcategories": _esv("subcategories", "[]"),
    }
    if not data["title"]:
        print("❌ 非交互模式需要 --title 参数（或通过 --url 自动解析）")
        sys.exit(EXIT_ARGS_ERROR)
    if not data["author"]:
        print("❌ 非交互模式需要 --author 参数（或通过 --url 自动解析）")
        sys.exit(EXIT_ARGS_ERROR)

    # Filter rules from args
    filters = (
        args.exclude_bvid if args.exclude_bvid else [],
        args.ad_bvid if args.ad_bvid else [],
        args.exclude_keyword if args.exclude_keyword else [],
        args.ad_keyword if args.ad_keyword else [],
        args.include_keyword if args.include_keyword else [],
        args.exclude_season_id if args.exclude_season_id else [],
    )

    paid_preview_enabled = False
    retry_days = 4
    cron_schedules = args.cron or []

    # Build sync: existing values → explicit CLI args → hardcoded defaults
    _get_sv = lambda k, d: existing_sync.get(k, d) if existing_sync else d
    sync = {
        "page_size": args.page_size if args.page_size is not None else _get_sv("page_size", 20),
        "incremental_page_size": args.incremental_page_size if args.incremental_page_size is not None else _get_sv("incremental_page_size", 5),
        "max_pages": args.max_pages if args.max_pages is not None else _get_sv("max_pages", 10),
        "max_requests_per_series": args.max_requests_per_series if args.max_requests_per_series is not None else _get_sv("max_requests_per_series", 8),
        "request_interval_seconds": args.request_interval_seconds if args.request_interval_seconds is not None else _get_sv("request_interval_seconds", 2.0),
        "request_jitter_seconds": args.request_jitter_seconds if args.request_jitter_seconds is not None else _get_sv("request_jitter_seconds", 0.5),
        "rate_limit_cooldown_seconds": args.rate_limit_cooldown_seconds if args.rate_limit_cooldown_seconds is not None else _get_sv("rate_limit_cooldown_seconds", 21600),
        "update_period": args.update_period or _get_sv("update_period", "12h"),
        "format": args.format or _get_sv("format", "audio"),
        "quality": args.quality or _get_sv("quality", "64K"),
        "fetch_strategy": args.fetch_strategy or _get_sv("fetch_strategy", "api_first"),
        "keep_last": args.keep_last if args.keep_last is not None else _get_sv("keep_last", 100),
        "browser_fallback": _get_sv("browser_fallback", False),
        "browser_wait_min_seconds": _get_sv("browser_wait_min_seconds", 4.0),
        "browser_wait_max_seconds": _get_sv("browser_wait_max_seconds", 8.0),
        "browser_fallback_cooldown_seconds": _get_sv("browser_fallback_cooldown_seconds", 3600),
        "require_paid_state_confirmation": _get_sv("require_paid_state_confirmation", False),
        "min_duration_seconds": _get_sv("min_duration_seconds", 0),
        "max_duration_seconds": _get_sv("max_duration_seconds", 0),
    }

    # Source: existing values → draft → defaults
    if existing_source:
        source = {
            "type": existing_source.get("type", "space"),
            "uid": existing_source.get("uid", 0),
            "sid": existing_source.get("sid"),
            "space_url": existing_source.get("space_url", ""),
        }
    elif draft and draft.get("source"):
        s = draft["source"]
        source = {
            "type": s.get("type", "space"),
            "uid": s.get("uid", 0),
            "sid": s.get("sid"),
            "space_url": s.get("space_url", ""),
        }
    else:
        source = {"type": "space", "uid": 0, "sid": None, "space_url": ""}

    # Paid preview from existing if --update-existing
    if existing_pp:
        paid_preview_enabled = bool(existing_pp.get("enabled", False))
        retry_days = existing_pp.get("retry_after_days", 4)

    # Summary
    print(f"\n系列标识: {series}")
    print(f"标题: {data['title']}")
    print(f"作者: {data['author']}")
    print(f"URL: {args.url or '—'}")
    print(f"同步策略: 质量={sync['quality']}, 保留={sync['keep_last']}, 周期={sync['update_period']}")
    total_filters = sum(len(f) for f in filters)
    print(f"过滤规则: {total_filters} 条")
    print(f"Cron: {len(cron_schedules)} 条")
    if existing_sync:
        print(f"模式: --update-existing，未显式传入的同步/来源字段保持原值")

    if add_dry_run:
        print("\n⚠️  --dry-run 模式，不写入数据库")
        return

    if not add_yes:
        confirm = input("确认保存? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return

    # Save
    with db.transaction(db_path) as conn:
        series_sql = """INSERT INTO series (
                   series, enabled, title, description, author, cover_art,
                   category, subcategories, explicit, lang)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?, 0, ?)"""
        if args.update_existing:
            series_sql += """
               ON CONFLICT(series) DO UPDATE SET
                   title=excluded.title, description=excluded.description,
                   author=excluded.author, cover_art=excluded.cover_art,
                   category=excluded.category, lang=excluded.lang"""
        else:
            series_sql += " ON CONFLICT(series) DO NOTHING"
        inserted = conn.execute(
            series_sql,
            (series, data["title"], data["description"],
             data["author"], data["cover_art"],
             data["category"], data["subcategories"],
             data["lang"]),
        )
        if not args.update_existing and inserted.rowcount == 0:
            print(f"❌ 系列标识 '{series}' 已存在（使用 --update-existing 覆盖更新）")
            sys.exit(EXIT_VALIDATION)
        conn.execute(
            """INSERT INTO series_source (series, space_url, uid, type, sid)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(series) DO UPDATE SET
                   space_url=excluded.space_url, uid=excluded.uid,
                   type=excluded.type, sid=excluded.sid""",
            (series, source.get("space_url", ""),
             source.get("uid"), source["type"], source.get("sid")),
        )
        conn.execute(
            """INSERT INTO sync_policy (series, page_size, incremental_page_size,
                   max_pages, max_requests_per_series, request_interval_seconds,
                   request_jitter_seconds, rate_limit_cooldown_seconds,
                   update_period, format, quality, fetch_strategy, keep_last,
                   browser_fallback, browser_wait_min_seconds,
                   browser_wait_max_seconds, browser_fallback_cooldown_seconds,
                   require_paid_state_confirmation,
                   min_duration_seconds, max_duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(series) DO UPDATE SET
                   page_size=excluded.page_size,
                   incremental_page_size=excluded.incremental_page_size,
                   max_pages=excluded.max_pages,
                   max_requests_per_series=excluded.max_requests_per_series,
                   request_interval_seconds=excluded.request_interval_seconds,
                   request_jitter_seconds=excluded.request_jitter_seconds,
                   rate_limit_cooldown_seconds=excluded.rate_limit_cooldown_seconds,
                   update_period=excluded.update_period,
                   format=excluded.format, quality=excluded.quality,
                   fetch_strategy=excluded.fetch_strategy,
                   keep_last=excluded.keep_last,
                   browser_fallback=excluded.browser_fallback,
                   browser_wait_min_seconds=excluded.browser_wait_min_seconds,
                   browser_wait_max_seconds=excluded.browser_wait_max_seconds,
                   browser_fallback_cooldown_seconds=excluded.browser_fallback_cooldown_seconds,
                   require_paid_state_confirmation=excluded.require_paid_state_confirmation,
                   min_duration_seconds=excluded.min_duration_seconds,
                   max_duration_seconds=excluded.max_duration_seconds""",
            (series,
             sync["page_size"], sync["incremental_page_size"],
             sync["max_pages"], sync["max_requests_per_series"],
             sync["request_interval_seconds"],
             sync["request_jitter_seconds"],
             sync["rate_limit_cooldown_seconds"],
             sync["update_period"], sync["format"],
             sync["quality"], sync["fetch_strategy"],
             sync["keep_last"], int(sync["browser_fallback"]),
             sync["browser_wait_min_seconds"],
             sync["browser_wait_max_seconds"],
             sync["browser_fallback_cooldown_seconds"],
             int(sync["require_paid_state_confirmation"]),
             sync["min_duration_seconds"],
             sync["max_duration_seconds"]),
        )

        # Filters
        pos = 0
        filter_type_map = [
            ("exclude_bvid", filters[0]),
            ("advertisement_bvid", filters[1]),
            ("exclude_keyword", filters[2]),
            ("advertisement_keyword", filters[3]),
            ("include_keyword", filters[4]),
            ("exclude_season_id", filters[5]),
        ]
        for rtype, values in filter_type_map:
            for val in values:
                conn.execute(
                    "INSERT INTO filter_rule (series, rule_type, value, position) VALUES (?, ?, ?, ?)",
                    (series, rtype, val, pos),
                )
                pos += 1

        if args.exclude_paid:
            _set_exclude_paid(conn, series, True)

        conn.execute(
            "INSERT INTO paid_preview_policy (series, enabled, retry_after_days) VALUES (?, ?, ?) "
            "ON CONFLICT(series) DO UPDATE SET enabled=excluded.enabled, retry_after_days=excluded.retry_after_days",
            (series, int(paid_preview_enabled), retry_days),
        )

        if cron_schedules:
            conn.execute("DELETE FROM cron_schedule WHERE series=?", (series,))
            for pos, sched in enumerate(cron_schedules):
                conn.execute(
                    "INSERT INTO cron_schedule (series, enabled, schedule, position) VALUES (?, 1, ?, ?)",
                    (series, sched, pos),
                )

    print(f"\n✅ 系列 '{series}' 已保存！")
    print(f"   执行 dry-run: bilibili-podcast-admin preview {series}")
    print(f"   启动同步: bilibili-podcast-admin sync {series} --apply")
    print()


def cmd_edit(args: argparse.Namespace) -> None:
    """Interactive editor for an existing series."""
    db_path = _get_db(args)
    with db.transaction(db_path) as conn:
        cfg = _load_full_config(conn, args.series)

    if not cfg:
        print(f"❌ 系列不存在: {args.series}")
        sys.exit(EXIT_VALIDATION)

    s = cfg["series"]
    src = cfg["source"]
    sp = cfg["sync"]
    print(f"编辑系列: {args.series}（回车保持当前值）\n")

    title = input(f"  标题 [{s.get('title', '')}]: ").strip() or s.get("title", "")
    author = input(f"  作者 [{s.get('author', '')}]: ").strip() or s.get("author", "")
    desc = input(f"  描述 [{s.get('description', '')}]: ").strip() or s.get("description", "")
    cover = input(f"  封面 [{s.get('cover_art', '')}]: ").strip() or s.get("cover_art", "")
    category = input(f"  分类 [{s.get('category', '')}]: ").strip() or s.get("category", "")
    lang = input(f"  语言 [{s.get('lang', 'zh-CN')}]: ").strip() or s.get("lang", "zh-CN")
    enabled = _prompt_bool("启用", bool(s.get("enabled", True)))
    explicit = _prompt_bool("Explicit 内容", bool(s.get("explicit", False)))
    _cur_sub = s.get("subcategories", "[]")
    try:
        _sub_list = json.loads(_cur_sub) if _cur_sub else []
    except (json.JSONDecodeError, TypeError):
        _sub_list = []
    subcategories = input(f"  子分类 (逗号分隔) [{', '.join(_sub_list)}]: ").strip()
    if not subcategories and not _sub_list:
        subcategories = "[]"
    elif subcategories:
        subcategories = json.dumps([x.strip() for x in subcategories.split(",") if x.strip()], ensure_ascii=False)
    else:
        subcategories = json.dumps(_sub_list, ensure_ascii=False)

    print("\n=== 数据来源 ===")
    new_source = _interactive_collect_source(src)

    print("\n=== 同步策略 ===")
    new_sync = _interactive_collect_sync(sp)

    print("\n=== 付费预览 ===")
    pp_cfg = cfg.get("paid_preview", {})
    paid_preview_enabled = _prompt_bool("启用付费预览", bool(pp_cfg.get("enabled", False)))
    retry_days = _prompt_int("重试天数", pp_cfg.get("retry_after_days", 4), 1)

    print("\n=== Cron 设置 ===")
    cron_schedules = _interactive_collect_cron(cfg.get("cron"))

    if args.dry_run:
        print("\n⚠️  --dry-run 模式，不写入数据库")
        print("   将保存: 标题/作者/描述/封面/分类/语言/启用/Explicit/子分类")
        print("          来源/同步策略/付费预览/Cron")
        return

    with db.transaction(db_path) as conn:
        conn.execute(
            "UPDATE series SET title=?, description=?, author=?, cover_art=?, category=?, lang=?, enabled=?, explicit=?, subcategories=? WHERE series=?",
            (title, desc, author, cover, category, lang, int(enabled), int(explicit), subcategories, args.series),
        )
        conn.execute(
            """INSERT INTO series_source (series, space_url, uid, type, sid)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(series) DO UPDATE SET
                   space_url=excluded.space_url, uid=excluded.uid,
                   type=excluded.type, sid=excluded.sid""",
            (args.series, new_source.get("space_url", ""),
             new_source.get("uid"), new_source["type"], new_source.get("sid")),
        )
        conn.execute(
            """INSERT INTO sync_policy (series, page_size, incremental_page_size,
                   max_pages, max_requests_per_series, request_interval_seconds,
                   request_jitter_seconds, rate_limit_cooldown_seconds,
                   update_period, format, quality, fetch_strategy, keep_last,
                   browser_fallback, browser_wait_min_seconds,
                   browser_wait_max_seconds, browser_fallback_cooldown_seconds,
                   require_paid_state_confirmation,
                   min_duration_seconds, max_duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(series) DO UPDATE SET
                   page_size=excluded.page_size,
                   incremental_page_size=excluded.incremental_page_size,
                   max_pages=excluded.max_pages,
                   max_requests_per_series=excluded.max_requests_per_series,
                   request_interval_seconds=excluded.request_interval_seconds,
                   request_jitter_seconds=excluded.request_jitter_seconds,
                   rate_limit_cooldown_seconds=excluded.rate_limit_cooldown_seconds,
                   update_period=excluded.update_period,
                   format=excluded.format, quality=excluded.quality,
                   fetch_strategy=excluded.fetch_strategy,
                   keep_last=excluded.keep_last,
                   browser_fallback=excluded.browser_fallback,
                   browser_wait_min_seconds=excluded.browser_wait_min_seconds,
                   browser_wait_max_seconds=excluded.browser_wait_max_seconds,
                   browser_fallback_cooldown_seconds=excluded.browser_fallback_cooldown_seconds,
                   require_paid_state_confirmation=excluded.require_paid_state_confirmation,
                   min_duration_seconds=excluded.min_duration_seconds,
                   max_duration_seconds=excluded.max_duration_seconds""",
            (args.series, new_sync.get("page_size", 20),
             new_sync.get("incremental_page_size", 5),
             new_sync.get("max_pages", 10), new_sync.get("max_requests_per_series", 8),
             new_sync.get("request_interval_seconds", 2.0),
             new_sync.get("request_jitter_seconds", 0.5),
             new_sync.get("rate_limit_cooldown_seconds", 21600),
             new_sync.get("update_period", "12h"), new_sync.get("format", "audio"),
             new_sync.get("quality", "64K"), new_sync.get("fetch_strategy", "api_first"),
             new_sync.get("keep_last", 100), int(new_sync.get("browser_fallback", False)),
             new_sync.get("browser_wait_min_seconds", 4.0),
             new_sync.get("browser_wait_max_seconds", 8.0),
             new_sync.get("browser_fallback_cooldown_seconds", 3600),
             int(new_sync.get("require_paid_state_confirmation", False)),
             new_sync.get("min_duration_seconds", 0),
             new_sync.get("max_duration_seconds", 0)),
        )

        # Paid preview
        conn.execute(
            """INSERT INTO paid_preview_policy (series, enabled, retry_after_days)
               VALUES (?, ?, ?)
               ON CONFLICT(series) DO UPDATE SET
                   enabled=excluded.enabled, retry_after_days=excluded.retry_after_days""",
            (args.series, int(paid_preview_enabled), retry_days),
        )

        # Cron — only update if user provided new schedules (not just Enter)
        if cron_schedules is not None:
            conn.execute("DELETE FROM cron_schedule WHERE series=?", (args.series,))
            for pos, sched in enumerate(cron_schedules):
                conn.execute(
                    "INSERT INTO cron_schedule (series, enabled, schedule, position) VALUES (?, 1, ?, ?)",
                    (args.series, sched, pos),
                )

    print(f"\n✅ 系列 {args.series} 已更新")
    print(f"   执行 dry-run: bilibili-podcast-admin preview {args.series}")
    print(f"   如需更改过滤规则: bilibili-podcast-admin filters {args.series}")
    print()


def cmd_filters(args: argparse.Namespace) -> None:
    """List filters for a series."""
    db_path = _get_db(args)
    with db.transaction(db_path) as conn:
        row = conn.execute("SELECT 1 FROM series WHERE series=?", (args.series,)).fetchone()
        if not row:
            print(f"❌ 系列不存在: {args.series}")
            sys.exit(EXIT_VALIDATION)
        rules = conn.execute(
            "SELECT id, rule_type, value, enabled, position FROM filter_rule WHERE series=? ORDER BY position",
            (args.series,),
        ).fetchall()

    if not rules:
        print(f"  系列 {args.series} 无过滤规则")
        return

    pp_headers = ["ID", "类型", "值", "启用"]
    pp_rows = []
    for r in rules:
        pp_rows.append([str(r["id"]), r["rule_type"], r["value"], _bool_str(r["enabled"])])
    _print_table(pp_headers, pp_rows)


def _set_exclude_paid(conn: sqlite3.Connection, series: str, enabled: bool) -> None:
    FilterRuleService(conn).set_exclude_paid(series, enabled)


def cmd_filters_add(args: argparse.Namespace) -> None:
    """Add filter rules to a series."""
    db_path = _get_db(args)

    pairs: list[tuple[str, str]] = []
    if args.exclude_keyword:
        for kw in args.exclude_keyword:
            pairs.append(("exclude_keyword", kw))
    if args.include_keyword:
        for kw in args.include_keyword:
            pairs.append(("include_keyword", kw))
    if args.ad_keyword:
        for kw in args.ad_keyword:
            pairs.append(("advertisement_keyword", kw))
    if args.exclude_bvid:
        for bvid in args.exclude_bvid:
            pairs.append(("exclude_bvid", bvid))
    if args.ad_bvid:
        for bvid in args.ad_bvid:
            pairs.append(("advertisement_bvid", bvid))
    if args.exclude_season_id:
        for season_id in args.exclude_season_id:
            pairs.append(("exclude_season_id", str(season_id)))

    if not pairs and not args.exclude_paid:
        print("⚠️ 未提供任何规则参数。使用 --exclude-keyword, --include-keyword, --ad-keyword, --exclude-bvid, --ad-bvid")
        return

    with db.transaction(db_path) as conn:
        if not ConfigService(conn).series_exists(args.series):
            print(f"❌ 系列不存在: {args.series}")
            sys.exit(EXIT_VALIDATION)

        fs = FilterRuleService(conn)
        if args.exclude_paid:
            fs.set_exclude_paid(args.series, True)

        added = fs.add_rules(args.series, pairs) if pairs else 0
    fa_yes = args.yes or getattr(args, "fa_yes", False)

    msgs = []
    if added:
        msgs.append(f"{added} 条过滤规则")
    if args.exclude_paid:
        msgs.append("付费内容排除已开启")
    print(f"✅ 已更新 {args.series}：{'，'.join(msgs)}")
    if not fa_yes:
        print(f"   查看: bilibili-podcast-admin filters {args.series}")
        print()


def cmd_filters_remove(args: argparse.Namespace) -> None:
    """Remove filter rules from a series.

    exclude_paid is special: "remove" means set to false (don't exclude paid),
    not delete the rule. Otherwise _row_to_config() defaults to True.
    """
    db_path = _get_db(args)

    conditions: list[tuple[str, str]] = []
    for kw in args.exclude_keyword:
        conditions.append(("exclude_keyword", kw))
    for kw in args.include_keyword:
        conditions.append(("include_keyword", kw))
    for kw in args.ad_keyword:
        conditions.append(("advertisement_keyword", kw))
    for bvid in args.exclude_bvid:
        conditions.append(("exclude_bvid", bvid))
    for bvid in args.ad_bvid:
        conditions.append(("advertisement_bvid", bvid))
    for season_id in args.exclude_season_id:
        conditions.append(("exclude_season_id", str(season_id)))

    if not conditions and not args.exclude_paid:
        print("⚠️ 未提供规则参数")
        return

    with db.transaction(db_path) as conn:
        if not ConfigService(conn).series_exists(args.series):
            print(f"❌ 系列不存在: {args.series}")
            sys.exit(EXIT_VALIDATION)

        fs = FilterRuleService(conn)

        if args.exclude_paid:
            fs.set_exclude_paid(args.series, False)

        if conditions:
            count = fs.remove_rules(args.series, conditions, delete=args.delete)
            if args.delete:
                print(f"✅ 已从 {args.series} 物理删除 {count} 条过滤规则")
            else:
                print(f"✅ 已禁用 {args.series} 的 {count} 条过滤规则（用 --delete 物理删除）")

    if args.exclude_paid:
        print(f"✅ 已关闭 {args.series} 的付费内容排除")


def cmd_filters_disable(args: argparse.Namespace) -> None:
    """Disable a filter rule by ID."""
    db_path = _get_db(args)
    with db.transaction(db_path) as conn:
        if not FilterRuleService(conn).toggle_rule(args.rule_id, args.series, enabled=False):
            print(f"❌ 未找到 ID={args.rule_id} 的规则")
            sys.exit(EXIT_VALIDATION)
    print(f"✅ 已禁用规则 ID={args.rule_id}")


def cmd_filters_enable(args: argparse.Namespace) -> None:
    """Enable a filter rule by ID."""
    db_path = _get_db(args)
    with db.transaction(db_path) as conn:
        if not FilterRuleService(conn).toggle_rule(args.rule_id, args.series, enabled=True):
            print(f"❌ 未找到 ID={args.rule_id} 的规则")
            sys.exit(EXIT_VALIDATION)
    print(f"✅ 已启用规则 ID={args.rule_id}")


def cmd_filters_import(args: argparse.Namespace) -> None:
    """Import filter rules from a file (one per line)."""
    db_path = _get_db(args)
    rule_type_map = {
        "exclude_keyword": "exclude_keyword",
        "include_keyword": "include_keyword",
        "ad_keyword": "advertisement_keyword",
        "advertisement_keyword": "advertisement_keyword",
        "exclude_bvid": "exclude_bvid",
        "ad_bvid": "advertisement_bvid",
        "advertisement_bvid": "advertisement_bvid",
        "exclude_season_id": "exclude_season_id",
    }
    rtype = rule_type_map.get(args.type)
    if not rtype:
        print(f"❌ 无效规则类型: {args.type}。支持: {', '.join(rule_type_map)}")
        sys.exit(EXIT_VALIDATION)

    try:
        with open(args.file) as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError as e:
        print(f"❌ 无法读取文件: {e}")
        sys.exit(EXIT_VALIDATION)

    if not lines:
        print("⚠️ 文件为空，无规则可导入")
        return

    if rtype == "exclude_season_id":
        try:
            lines = [str(_positive_int(line)) for line in lines]
        except (TypeError, ValueError, argparse.ArgumentTypeError) as exc:
            print(f"❌ 合集 ID 必须是正整数: {exc}")
            sys.exit(EXIT_VALIDATION)

    pairs = [(rtype, val) for val in lines]

    with db.transaction(db_path) as conn:
        if not ConfigService(conn).series_exists(args.series):
            print(f"❌ 系列不存在: {args.series}")
            sys.exit(EXIT_VALIDATION)
        count = FilterRuleService(conn).add_rules(args.series, pairs)

    print(f"✅ 已从 {args.file} 导入 {count} 条 {args.type} 规则到 {args.series}")


def cmd_preview(args: argparse.Namespace) -> None:
    """Run dry-run preview for a series."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)

    # Find the bilibili-podcast binary
    sync_bin = _find_sync_bin()
    if not sync_bin:
        print("❌ 找不到 bilibili-podcast 命令")
        sys.exit(EXIT_SYNC_FAIL)

    cmd = [sync_bin, "--config-db", db_path, "--series", args.series, "--log-level", "DEBUG"]

    print(f"🔍 执行干跑: {args.series} ...\n")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                env={**os.environ, "BILIBILI_PODCAST_CONFIG_DB": db_path})
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        print(_sanitize(output))
        if result.returncode != 0:
            print(f"\n⚠️ 干跑返回码 {result.returncode}")
    except subprocess.TimeoutExpired:
        print("❌ 执行超时（120秒）")
        sys.exit(EXIT_SYNC_FAIL)
    except FileNotFoundError:
        print(f"❌ 找不到可执行文件: {sync_bin}")
        sys.exit(EXIT_SYNC_FAIL)
    except Exception as e:
        print(f"❌ 执行错误: {e}")
        sys.exit(EXIT_SYNC_FAIL)


def cmd_sync(args: argparse.Namespace) -> None:
    """Run sync for a series (dry-run unless --apply)."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)

    sync_bin = _find_sync_bin()
    if not sync_bin:
        print("❌ 找不到 bilibili-podcast 命令")
        sys.exit(EXIT_SYNC_FAIL)

    # Build production-safe command (matches systemd template params)
    cookie_file = os.environ.get("BILIBILI_PODCAST_COOKIE_FILE",
        "<server_path>")
    media_root = os.environ.get("BILIBILI_PODCAST_MEDIA_ROOT", "/var/lib/bilibili-podcast/media")
    json_root = os.environ.get("BILIBILI_PODCAST_JSON_ROOT", "/var/lib/bilibili-podcast/json")
    rss_root = os.environ.get("BILIBILI_PODCAST_RSS_ROOT", "/var/lib/bilibili-podcast/rss")
    state_root = os.environ.get("BILIBILI_PODCAST_STATE_ROOT", "/var/lib/bilibili-podcast/state")
    lock_file = os.environ.get("BILIBILI_PODCAST_LOCK_FILE",
        "/var/lib/bilibili-podcast/state/bilibili-podcast.lock")
    log_dir = os.environ.get("BILIBILI_PODCAST_LOG_DIR", "/var/log/bilibili-podcast")
    media_base_url = os.environ.get("BILIBILI_PODCAST_MEDIA_BASE_URL",
        "http://localhost:58743")
    browser_root = os.environ.get("BILIBILI_PODCAST_BROWSER_USER_DATA_ROOT",
        "<server_path>")

    cmd = [
        sync_bin, "--config-db", db_path, "--series", args.series,
        "--cookie-file", cookie_file,
        "--media-root", media_root,
        "--json-root", json_root,
        "--rss-root", rss_root,
        "--state-root", state_root,
        "--lock-file", lock_file,
        "--log-dir", log_dir,
        "--media-base-url", media_base_url,
        "--browser-user-data-root", browser_root,
        "--max-downloads-per-run", "1",
        "--min-free-gb", "5",
        "--token", "__MEDIA_PLACEHOLDER__",
    ]
    if args.apply:
        cmd.append("--apply")

    # Confirm for --apply
    if args.apply and not args.yes:
        confirm = input(f"⚠️  确认同步 {args.series}? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return

    print(f"{'🔄 同步' if args.apply else '🔍 干跑'}: {args.series} ...\n")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                                env={**os.environ, "BILIBILI_PODCAST_CONFIG_DB": db_path})
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        print(_sanitize(output))
        if result.returncode != 0:
            print(f"\n⚠️ 返回码 {result.returncode}")
            sys.exit(EXIT_SYNC_FAIL)
    except subprocess.TimeoutExpired:
        print("❌ 执行超时（300秒）")
        sys.exit(EXIT_SYNC_FAIL)
    except Exception as e:
        print(f"❌ 执行错误: {e}")
        sys.exit(EXIT_SYNC_FAIL)

    # After successful apply: publish RSS
    if args.apply:
        publish = os.environ.get("BILIBILI_PODCAST_RSS_PUBLISH",
            "<server_path>")
        pub_result = subprocess.run(
            [publish], capture_output=True, text=True, timeout=60,
        )
        pub_out = (pub_result.stdout or "") + "\n" + (pub_result.stderr or "")
        print(_sanitize(pub_out))
        if pub_result.returncode != 0:
            print(f"\n⚠️ RSS 发布失败（返回码 {pub_result.returncode}）")
            sys.exit(EXIT_SYNC_FAIL)


# ── Sync-policy handlers ──────────────────────────────────────────────────

def cmd_sync_policy_show(args: argparse.Namespace) -> None:
    """Show sync policy for a series."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)
    with db.transaction(db_path) as conn:
        sp = conn.execute("SELECT * FROM sync_policy WHERE series=?", (args.series,)).fetchone()
    if not sp:
        msg = f"系列 {args.series} 无同步策略（将使用默认值）"
        if _should_json(args):
            _run_json(args, {"series": args.series, "sync_policy": None})
        print(msg)
        return
    spd = dict(sp)
    if _should_json(args):
        _run_json(args, {"series": args.series, "sync_policy": spd})
    print(f"\n=== 同步策略: {args.series} ===")
    print(f"  分页: page_size={spd['page_size']}, incremental={spd['incremental_page_size']}, max_pages={spd['max_pages']}")
    print(f"  请求: max_requests={spd['max_requests_per_series']}, interval={spd['request_interval_seconds']}s, jitter={spd['request_jitter_seconds']}s")
    print(f"  限流冷却: {spd['rate_limit_cooldown_seconds']}s")
    print(f"  更新周期: {spd['update_period']}")
    print(f"  输出: {spd['format']} / {spd['quality']}")
    print(f"  保留: keep_last={spd['keep_last']}")
    print(f"  策略: {spd['fetch_strategy']}, browser_fallback={bool(spd['browser_fallback'])}")
    print(f"  浏览器等待: {spd['browser_wait_min_seconds']}s ~ {spd['browser_wait_max_seconds']}s")
    print(f"  回退冷却: {spd['browser_fallback_cooldown_seconds']}s")
    print(f"  付费确认: {bool(spd['require_paid_state_confirmation'])}")
    print(f"  时长过滤: {spd['min_duration_seconds']}s ~ {spd['max_duration_seconds']}s")
    print()


def cmd_sync_policy_set(args: argparse.Namespace) -> None:
    """Set specific sync policy fields for a series."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)

    updates: dict[str, Any] = {}
    field_map = {
        "page_size": args.page_size,
        "incremental_page_size": args.incremental_page_size,
        "max_pages": args.max_pages,
        "max_requests_per_series": args.max_requests_per_series,
        "request_interval_seconds": args.request_interval_seconds,
        "request_jitter_seconds": args.request_jitter_seconds,
        "rate_limit_cooldown_seconds": args.rate_limit_cooldown_seconds,
        "update_period": args.update_period,
        "format": args.format,
        "quality": args.quality,
        "fetch_strategy": args.fetch_strategy,
        "keep_last": args.keep_last,
        "browser_fallback": int(args.browser_fallback) if args.browser_fallback is not None else None,
        "browser_wait_min_seconds": args.browser_wait_min_seconds,
        "browser_wait_max_seconds": args.browser_wait_max_seconds,
        "browser_fallback_cooldown_seconds": args.browser_fallback_cooldown_seconds,
        "require_paid_state_confirmation": int(args.require_paid_confirmation) if args.require_paid_confirmation is not None else None,
        "min_duration_seconds": args.min_duration_seconds,
        "max_duration_seconds": args.max_duration_seconds,
    }
    for field, value in field_map.items():
        if value is not None:
            updates[field] = value

    if not updates:
        print("⚠️ 未提供任何要设置的字段")
        print("   可用: --page-size, --incremental-page-size, --max-pages, --max-requests-per-series,")
        print("         --request-interval, --request-jitter, --rate-limit-cooldown, --update-period,")
        print("         --format, --quality, --fetch-strategy, --keep-last, --browser-fallback,")
        print("         --browser-wait-min, --browser-wait-max, --browser-fallback-cooldown,")
        print("         --require-paid-confirmation, --min-duration, --max-duration")
        return

    sp_set_yes = args.yes or getattr(args, "sp_set_yes", False)
    if not sp_set_yes:
        confirm = input(f"确认更新 {args.series} 的 {len(updates)} 个同步策略字段? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return

    with db.transaction(db_path) as conn:
        SyncPolicyService(conn).update_fields(args.series, updates)

    print(f"✅ 已更新 {args.series} 的 {len(updates)} 个同步策略字段")
    if not sp_set_yes:
        print(f"   查看: bilibili-podcast-admin sync-policy show {args.series}")
    print()


# ── Cron / Scheduler handlers ─────────────────────────────────────────────

def _run_crontab(args: argparse.Namespace, db_path: str, action: str) -> None:
    """Run bilibili-podcast-crontab via SchedulerService."""
    crontab_bin = _find_crontab_bin()
    if not crontab_bin:
        print("❌ 找不到 bilibili-podcast-crontab 脚本")
        sys.exit(EXIT_SYNC_FAIL)
    svc = SchedulerService(db_path, crontab_script=crontab_bin)
    if action == "apply":
        yes = args.yes or getattr(args, "cron_yes", False)
        if not yes:
            confirm = input("⚠️  确认安装 crontab? (y/N): ").strip().lower()
            if confirm != "y":
                print("已取消")
                return
        result = svc.apply(cron_script_dir=args.cron_script_dir)
        print("🔄 安装 crontab ...\n")
    else:
        result = svc.plan(cron_script_dir=args.cron_script_dir)
        if args.cron_script_dir:
            print("📋 Cron 计划（目标目录: {}）:\n".format(args.cron_script_dir))
        else:
            print("📋 Cron 计划（临时目录，仅供结构预览；传 --cron-script-dir 显示真实路径）:\n")

    if result.error:
        print(result.error)
        sys.exit(EXIT_SYNC_FAIL)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    print(_sanitize(output))
    if result.returncode != 0:
        print(f"\n⚠️ 返回码 {result.returncode}")
        sys.exit(EXIT_SYNC_FAIL)


def cmd_cron_plan(args: argparse.Namespace) -> None:
    """Preview cron plan."""
    db_path = _get_db(args)
    _run_crontab(args, db_path, "plan")


def cmd_cron_apply(args: argparse.Namespace) -> None:
    """Install crontab."""
    db_path = _get_db(args)
    _run_crontab(args, db_path, "apply")


def cmd_cron_show(args: argparse.Namespace) -> None:
    """Show cron schedules for a series using SchedulerService."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)
    schedules = SchedulerService(db_path).list_schedules(args.series)

    if _should_json(args):
        _run_json(args, {
            "series": args.series,
            "schedules": [
                {"id": s.id, "schedule": s.schedule, "enabled": s.enabled, "position": s.position}
                for s in schedules
            ],
        })

    if not schedules:
        print(f"  系列 {args.series} 无 cron 配置")
        return
    print(f"\n=== Cron: {args.series} ===")
    for s in schedules:
        print(f"  {_bool_str(s.enabled)}  {s.schedule}  (id={s.id}, pos={s.position})")
    print()


def cmd_cron_set(args: argparse.Namespace) -> None:
    """Set cron schedules for a series using SchedulerService."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)
    if not args.schedule:
        print("⚠️ 使用 --schedule 指定 cron 表达式（可多次使用）")
        return

    cron_set_yes = args.yes or getattr(args, "cron_set_yes", False)
    if not cron_set_yes:
        confirm = input(f"确认更新 {args.series} 的 {len(args.schedule)} 条 cron 配置? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return

    count = SchedulerService(db_path).replace_schedules(args.series, args.schedule)
    print(f"✅ 已更新 {args.series} 的 {count} 条 cron 配置")
    print(f"   注意: 修改仅保存到数据库，执行 'bilibili-podcast-admin cron apply' 才安装到系统 crontab")
    print()


# ── Scheduler commands (new, wraps SchedulerService) ────────────────────

def cmd_scheduler_plan(args: argparse.Namespace) -> None:
    db_path = _get_db(args)
    crontab_bin = _find_crontab_bin()
    if not crontab_bin:
        print("❌ 找不到 bilibili-podcast-crontab 脚本")
        sys.exit(EXIT_SYNC_FAIL)
    try:
        svc = SchedulerService(db_path, crontab_script=crontab_bin,
                                cron_script_dir=args.cron_script_dir)
        result = svc.plan(backend=args.scheduler_backend, cron_script_dir=args.cron_script_dir,
                          series=getattr(args, "series", None))
        if args.cron_script_dir:
            print("📋 调度计划（目标目录: {}）:\n".format(args.cron_script_dir))
        else:
            print("📋 调度计划（临时目录）:\n")
        if result.error:
            print(result.error)
            sys.exit(EXIT_SYNC_FAIL)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        print(_sanitize(output))
        if result.returncode != 0:
            print(f"\n⚠️ 返回码 {result.returncode}")
            sys.exit(EXIT_SYNC_FAIL)
    except NotImplementedError as e:
        print(f"❌ {e}")
        sys.exit(EXIT_SYNC_FAIL)


def cmd_scheduler_apply(args: argparse.Namespace) -> None:
    db_path = _get_db(args)
    crontab_bin = _find_crontab_bin()
    if not crontab_bin:
        print("❌ 找不到 bilibili-podcast-crontab 脚本")
        sys.exit(EXIT_SYNC_FAIL)
    yes = args.yes or getattr(args, "scheduler_yes", False)
    if not yes:
        confirm = input("⚠️  确认安装调度? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return
    try:
        svc = SchedulerService(db_path, crontab_script=crontab_bin,
                                cron_script_dir=args.cron_script_dir)
        result = svc.apply(backend=args.scheduler_backend, cron_script_dir=args.cron_script_dir,
                           series=getattr(args, "series", None))
        print("🔄 安装调度 ...\n")
        if result.error:
            print(result.error)
            sys.exit(EXIT_SYNC_FAIL)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        print(_sanitize(output))
        if result.returncode != 0:
            print(f"\n⚠️ 返回码 {result.returncode}")
            sys.exit(EXIT_SYNC_FAIL)
    except NotImplementedError as e:
        print(f"❌ {e}")
        sys.exit(EXIT_SYNC_FAIL)


def cmd_scheduler_status(args: argparse.Namespace) -> None:
    db_path = _get_db(args)
    try:
        svc = SchedulerService(db_path)
        status_list = svc.status(backend=args.scheduler_backend,
                                 series=getattr(args, "series", None))
    except NotImplementedError as e:
        print(f"❌ {e}")
        sys.exit(EXIT_SYNC_FAIL)

    if _should_json(args):
        _run_json(args, {"status": status_list})

    if not status_list:
        print("  （无系列配置）")
        return

    headers = ["系列", "启用", "调度数"]
    data = []
    for s in status_list:
        data.append([
            s["series"],
            _bool_str(s["enabled"]),
            str(s["schedule_count"] or 0),
        ])
    _print_table(headers, data)


def cmd_scheduler_set(args: argparse.Namespace) -> None:
    db_path = _get_db(args)
    _require_series(db_path, args.series)
    if not args.schedule:
        print("⚠️ 使用 --schedule 指定 cron 表达式（可多次使用）")
        return

    yes = args.yes or getattr(args, "scheduler_set_yes", False)
    if not yes:
        confirm = input(f"确认更新 {args.series} 的 {len(args.schedule)} 条调度? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return

    count = SchedulerService(db_path).replace_schedules(args.series, args.schedule)
    print(f"✅ 已更新 {args.series} 的 {count} 条调度")
    print(f"   注意: 修改仅保存到数据库，执行 'scheduler apply' 才安装调度")
    print()


def cmd_scheduler_disable(args: argparse.Namespace) -> None:
    """Disable systemd timer for a pilot series and restore cron."""
    db_path = _get_db(args)
    yes = args.yes or getattr(args, "scheduler_disable_yes", False)
    if not yes:
        confirm = input(f"⚠️  确认禁用 {args.series} 的 systemd timer 并恢复 cron? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return
    crontab_bin = _find_crontab_bin()
    svc = SchedulerService(db_path, crontab_script=crontab_bin,
                            cron_script_dir=args.cron_script_dir)
    result = svc.disable_systemd(args.series, delete_units=args.delete_units)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    print(_sanitize(output))
    if result.returncode != 0:
        sys.exit(EXIT_SYNC_FAIL)

def _prompt_str(prompt_text: str, default: str = "") -> str:
    val = input(f"  {prompt_text} [{default}]: ").strip()
    return val if val else default


def _prompt_int(prompt_text: str, default: int, min_val: int = 0, max_val: int | None = None) -> int:
    while True:
        raw = input(f"  {prompt_text} [{default}]: ").strip()
        if not raw:
            return default
        try:
            val = int(raw)
            if val < min_val:
                print(f"    最小值 {min_val}")
                continue
            if max_val is not None and val > max_val:
                print(f"    最大值 {max_val}")
                continue
            return val
        except ValueError:
            print("    请输入数字")


def _prompt_float(prompt_text: str, default: float, min_val: float = 0.0) -> float:
    while True:
        raw = input(f"  {prompt_text} [{default}]: ").strip()
        if not raw:
            return default
        try:
            val = float(raw)
            if val >= min_val:
                return val
            print(f"    最小值 {min_val}")
        except ValueError:
            print("    请输入数字")


def _prompt_bool(prompt_text: str, default: bool = False) -> bool:
    hint = "y/N" if not default else "Y/n"
    raw = input(f"  {prompt_text} ({hint}): ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "1", "true")


def _interactive_collect_series(draft: dict | None) -> dict | None:
    """Collect basic series fields interactively."""
    print("=== 基本信息 ===")
    series = ""
    while not series:
        default_slug = draft.get("series", "") if draft else ""
        raw = input(f"  系列标识 [{default_slug}]: ").strip()
        series = raw or default_slug
        if not series:
            print("    系列标识不能为空")
        elif not re.match(r"^[a-z0-9][a-z0-9_-]*$", series):
            print("    只允许小写字母、数字、横线和下划线")
            series = ""
        else:
            break

    title = _prompt_str("标题", draft.get("title", "") if draft else "")
    if not title:
        print("  标题不能为空")
        title = input("  标题: ").strip()
        if not title:
            return None

    author = _prompt_str("作者", draft.get("author", "") if draft else "")
    if not author:
        author = input("  作者: ").strip()

    description = _prompt_str("描述", draft.get("description", "") if draft else "")
    cover_art = _prompt_str("封面图 URL", draft.get("cover_art", "") if draft else "")
    category = _prompt_str("分类", draft.get("category", "") if draft else "")
    lang = _prompt_str("语言", "zh-CN")
    sub_input = _prompt_str("子分类(逗号分隔)", "")
    if sub_input:
        subcategories = json.dumps([x.strip() for x in sub_input.split(",") if x.strip()], ensure_ascii=False)
    else:
        subcategories = "[]"

    return {
        "series": series,
        "title": title,
        "author": author,
        "description": description,
        "cover_art": cover_art,
        "category": category,
        "subcategories": subcategories,
        "lang": lang,
        "source": draft.get("source", {}) if draft else {},
    }


def _interactive_collect_source(existing: dict) -> dict:
    """Collect source configuration."""
    print("\n=== 数据来源 ===")
    default_type = existing.get("type", "space")
    stype = input(f"  来源类型 (space/season/series) [{default_type}]: ").strip() or default_type
    if stype not in ("space", "season", "series"):
        print("  无效类型，使用 space")
        stype = "space"

    uid = existing.get("uid") or 0
    uid_raw = input(f"  UID [{uid}]: ").strip()
    if uid_raw:
        try:
            uid = int(uid_raw)
        except ValueError:
            print(f"  ⚠️ 无效 UID: '{uid_raw}'，保留 {uid}")

    sid = None
    if stype in ("season", "series"):
        sid_raw = input(f"  SID [{existing.get('sid', '')}]: ").strip() or existing.get("sid", "")
        if sid_raw:
            try:
                sid = int(sid_raw)
            except ValueError:
                print(f"  ⚠️ 无效 SID: '{sid_raw}'，使用空值")
                sid = None

    space_url = input(f"  Space URL [{existing.get('space_url', '')}]: ").strip() or existing.get("space_url", "")

    return {"type": stype, "uid": uid, "sid": sid, "space_url": space_url}


def _interactive_collect_sync(existing: dict) -> dict:
    """Collect sync policy."""
    print("=== 同步策略（回车接受默认值）===")
    quality = _prompt_str("音频质量(64K/132K/192K)", existing.get("quality", "64K"))
    if quality not in ("64K", "132K", "192K"):
        print("  无效值，使用 64K")
        quality = "64K"
    fetch_strategy = _prompt_str("抓取策略(api_first/browser_first)", existing.get("fetch_strategy", "api_first"))
    if fetch_strategy not in ("api_first", "browser_first"):
        print("  无效值，使用 api_first")
        fetch_strategy = "api_first"
    fmt = _prompt_str("输出格式(audio/video)", existing.get("format", "audio"))
    if fmt not in ("audio", "video"):
        print("  无效值，使用 audio")
        fmt = "audio"
    return {
        "page_size": _prompt_int("首页每页条数", existing.get("page_size", 20), 1, 50),
        "incremental_page_size": _prompt_int("增量页条数", existing.get("incremental_page_size", 5), 1),
        "max_pages": _prompt_int("最大页数", existing.get("max_pages", 10), 1),
        "max_requests_per_series": _prompt_int("每系列最大请求数", existing.get("max_requests_per_series", 8), 1),
        "request_interval_seconds": _prompt_float("请求间隔(秒)", float(existing.get("request_interval_seconds", 2.0)), 0.1),
        "request_jitter_seconds": _prompt_float("抖动(秒)", float(existing.get("request_jitter_seconds", 0.5)), 0.0),
        "rate_limit_cooldown_seconds": _prompt_int("限流冷却(秒)", existing.get("rate_limit_cooldown_seconds", 21600), 0),
        "update_period": _prompt_str("更新周期(如 12h)", existing.get("update_period", "12h")),
        "format": fmt,
        "quality": quality,
        "fetch_strategy": fetch_strategy,
        "keep_last": _prompt_int("RSS 保留条数(0=不限)", existing.get("keep_last", 100), 0),
        "browser_fallback": _prompt_bool("启用浏览器回退", bool(existing.get("browser_fallback", False))),
        "browser_wait_min_seconds": _prompt_float("浏览器最小等待(秒)", float(existing.get("browser_wait_min_seconds", 4.0)), 1.0),
        "browser_wait_max_seconds": _prompt_float("浏览器最大等待(秒)", float(existing.get("browser_wait_max_seconds", 8.0)), 1.0),
        "browser_fallback_cooldown_seconds": _prompt_int("回退冷却(秒)", existing.get("browser_fallback_cooldown_seconds", 3600), 0),
        "require_paid_state_confirmation": _prompt_bool("要求付费状态确认", bool(existing.get("require_paid_state_confirmation", False))),
        "min_duration_seconds": _prompt_int("最短时长(秒, 0=不限)", existing.get("min_duration_seconds", 0), 0),
        "max_duration_seconds": _prompt_int("最长时长(秒, 0=不限)", existing.get("max_duration_seconds", 0), 0),
    }


def _interactive_collect_filters() -> tuple:
    """Collect filter rules."""
    exclude_bvids: list[str] = []
    ad_bvids: list[str] = []
    exclude_keywords: list[str] = []
    ad_keywords: list[str] = []
    include_keywords: list[str] = []
    exclude_season_ids: list[int] = []

    print("  输入过滤规则，每行一条，空行结束。")
    print("  黑名单关键词:")
    while True:
        line = input("    > ").strip()
        if not line:
            break
        exclude_keywords.append(line)

    print("  白名单关键词（非空时仅保留命中项）:")
    while True:
        line = input("    > ").strip()
        if not line:
            break
        include_keywords.append(line)

    print("  广告关键词:")
    while True:
        line = input("    > ").strip()
        if not line:
            break
        ad_keywords.append(line)

    print("  排除 BVID:")
    while True:
        line = input("    > ").strip()
        if not line:
            break
        if re.match(r"^BV[0-9A-Za-z]{10}$", line):
            exclude_bvids.append(line)
        else:
            print(f"    ⚠️ 不是有效的 BVID: {line}（应以 BV 开头，共 12 位）")

    print("  广告 BVID:")
    while True:
        line = input("    > ").strip()
        if not line:
            break
        if re.match(r"^BV[0-9A-Za-z]{10}$", line):
            ad_bvids.append(line)
        else:
            print(f"    ⚠️ 不是有效的 BVID: {line}（应以 BV 开头，共 12 位）")

    print("  排除合集 ID:")
    while True:
        line = input("    > ").strip()
        if not line:
            break
        try:
            exclude_season_ids.append(_positive_int(line))
        except (ValueError, argparse.ArgumentTypeError):
            print(f"    ⚠️ 不是有效的合集 ID: {line}（应为正整数）")

    return (exclude_bvids, ad_bvids, exclude_keywords, ad_keywords, include_keywords, exclude_season_ids)


def _interactive_collect_cron(existing: list[str] | None = None) -> list[str] | None:
    """Collect cron expressions. Returns None if all existing kept."""
    if existing:
        print(f"  当前 Cron (共 {len(existing)} 条):")
        for s in existing:
            print(f"    {s}")
    print("  Cron 表达式，每行一个，空行结束。例: 15 11 * * *")
    print("  直接回车 = 保持现有" if existing else "  直接回车 = 跳过")
    schedules: list[str] = []
    while True:
        line = input("    > ").strip()
        if not line:
            break
        schedules.append(line)
    if not schedules and existing:
        return None  # signal: keep existing
    return schedules


# ── CLI utilities ───────────────────────────────────────────────────────

DEFAULT_CONFIG_DB = "/var/lib/bilibili-podcast/state/bilibili-podcast.db"


def _get_db(args: argparse.Namespace) -> str:
    """Resolve DB path from args, env var, or default."""
    db_path = args.config_db or os.environ.get(DB_ENV_VAR) or DEFAULT_CONFIG_DB
    return db_path


def _find_sync_bin() -> str | None:
    """Find the bilibili-podcast binary."""
    # Look in venv bin first
    python_bin = Path(sys.executable).parent
    candidate = python_bin / "bilibili-podcast"
    if candidate.exists():
        return str(candidate)
    # Fall back to PATH
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / "bilibili-podcast"
        if candidate.exists():
            return str(candidate)
    return None


def _require_series(db_path: str, series: str) -> None:
    """Exit with error if series does not exist in DB."""
    with db.transaction(db_path) as conn:
        row = conn.execute("SELECT 1 FROM series WHERE series=?", (series,)).fetchone()
    if not row:
        print(f"❌ 系列不存在: {series}")
        sys.exit(EXIT_VALIDATION)


def _find_crontab_bin() -> str | None:
    """Find bilibili-podcast-crontab script."""
    # Look relative to project root
    here = Path(__file__).resolve().parent.parent.parent  # src/bilibili_podcast -> project root
    candidate = here / "scripts" / "bilibili-podcast-crontab"
    if candidate.exists():
        return str(candidate)
    # Fall back to PATH
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / "bilibili-podcast-crontab"
        if candidate.exists():
            return str(candidate)
    return None


# ── Paid / manual media commands ────────────────────────────────────

def _get_allowed_media_dirs() -> list[Path]:
    """Return allowed directories for manual media attach, configured via
    ``BILIBILI_PODCAST_MANUAL_MEDIA_DIRS`` (colon-separated).

    Falls back to safe defaults when the env var is not set.
    Rejects broad root directories like ``/`` or ``/data``.
    """
    raw = os.environ.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS", "")
    if raw.strip():
        dirs: list[Path] = []
        for p_str in raw.split(":"):
            p_str = p_str.strip()
            if not p_str:
                continue
            p = Path(p_str)
            if not p.is_absolute():
                continue
            parts = p.resolve().parts
            # parts[0] is "/", parts[1] is top-level dir
            # Allow at least 2 levels deep (e.g. <server_path>)
            if len(parts) < 3:
                continue
            dirs.append(p)
        return dirs
    return [
        Path("<server_path>"),
        Path("/data/manual-media"),
    ]


def is_allowed_manual_media_path(path: Path) -> bool:
    """Check if a resolved file path is inside an allowed manual media directory.

    Reads allowed dirs from ``_get_allowed_media_dirs()`` each call,
    ensuring env changes are picked up and the check is not bypassed.
    """
    resolved = path.resolve()
    for d in _get_allowed_media_dirs():
        try:
            resolved.relative_to(d.resolve())
            return True
        except ValueError:
            continue
    return False


def _get_series_quality(db_path: str, series: str) -> str:
    """Read the configured audio quality from sync_policy for *series*.

    Falls back to ``"64K"`` if the series has no explicit quality set.
    """
    from . import db as _db
    with _db.transaction(db_path) as conn:
        row = conn.execute(
            "SELECT quality FROM sync_policy WHERE series=?", (series,),
        ).fetchone()
        quality = row["quality"] if row and row["quality"] else "64K"
    return quality if quality in ("64K", "132K", "192K") else "64K"


def _bvid_from_text(text: str) -> str:
    match = re.search(r"(BV[0-9A-Za-z]+)", text or "")
    return match.group(1) if match else ""


def _normalize_duration(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _placeholder_media_token(url: str, media_base_url: str = "") -> str:
    if not url:
        return ""
    normalized = re.sub(r"([?&]token=)[^&]+", r"\1__MEDIA_PLACEHOLDER__", url)
    if "token=" in normalized:
        return normalized
    parsed = urlparse(normalized)
    base = urlparse(media_base_url) if media_base_url else None
    internal_url = not parsed.netloc or bool(base and parsed.netloc == base.netloc)
    if internal_url and ("/media/" in parsed.path or "/images/" in parsed.path):
        sep = "&" if "?" in normalized else "?"
        return f"{normalized}{sep}token=__MEDIA_PLACEHOLDER__"
    return normalized


def _read_existing_channel_image(rss_path: Path, media_base_url: str = "") -> str:
    if not rss_path.exists():
        return ""
    try:
        import xml.etree.ElementTree as ET
        ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
        root = ET.parse(rss_path).getroot()
        channel = root.find("channel")
        if channel is None:
            return ""
        itunes_image = channel.find("itunes:image", ns)
        if itunes_image is not None and itunes_image.get("href"):
            return _placeholder_media_token(itunes_image.get("href", ""), media_base_url)
        image = channel.find("image")
        if image is not None and image.findtext("url"):
            return _placeholder_media_token(image.findtext("url") or "", media_base_url)
    except Exception:
        return ""
    return ""


def _ensure_channel_itunes_image(rss_path: Path, image_url: str) -> None:
    if not image_url:
        return
    import xml.etree.ElementTree as ET
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    tree = ET.parse(rss_path)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        return
    tag = "{http://www.itunes.com/dtds/podcast-1.0.dtd}image"
    for node in list(channel.findall(tag)):
        channel.remove(node)
    node = ET.Element(tag)
    node.set("href", image_url)
    channel.insert(4, node)
    tree.write(rss_path, encoding="utf-8", xml_declaration=True)
    rss_path.chmod(0o644)


def _write_metadata_file(json_root: Path, series: str, bvid: str, quality: str, metadata: dict[str, Any]) -> Path:
    dst = json_root / series / f"{bvid}_{quality}.info.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    dst.chmod(0o644)
    return dst


def _file_backup(path: Path) -> tuple[bool, bytes]:
    if not path.exists():
        return False, b""
    return True, path.read_bytes()


def _restore_file(path: Path, backup: tuple[bool, bytes]) -> None:
    existed, data = backup
    if existed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
    else:
        path.unlink(missing_ok=True)


def cmd_paid_refresh_metadata(args: argparse.Namespace) -> None:
    """Refresh metadata for a manual media series (no download)."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)
    import asyncio
    from bilibili_podcast.sync import fetch_space_episodes, fetch_series_episodes, load_cookie_file
    from bilibili_podcast.db import transaction
    from bilibili_podcast.utils.series_config import SeriesConfig
    from bilibili_podcast.services.config_service import ConfigService

    # Load config for both enabled and disabled series
    with transaction(db_path) as conn:
        cfg_dict = ConfigService(conn).load_full_config(args.series)
    if not cfg_dict:
        print(f"❌ 未找到系列配置: {args.series}")
        sys.exit(EXIT_VALIDATION)
    s = cfg_dict["series"]
    src = cfg_dict["source"]
    sp = cfg_dict["sync"] or {}

    from bilibili_podcast.db import _parse_subcategories as _parse_subcats

    config = SeriesConfig(
        series=args.series, enabled=bool(s.get("enabled", True)),
        title=s.get("title", ""), description=s.get("description", ""),
        author=s.get("author", ""), cover_art=s.get("cover_art", ""),
        category=s.get("category", ""),
        subcategories=_parse_subcats(s.get("subcategories", "[]")),
        explicit=bool(s.get("explicit", False)), lang=s.get("lang", "zh-CN"),
        source=src, sync=sp, filters={}, paid_preview={}, keep_last=sp.get("keep_last", 0),
    )
    credential = None
    cookie_file = args.cookie_file or os.environ.get("BILIBILI_PODCAST_COOKIE_FILE", "")
    if cookie_file:
        credential = load_cookie_file(cookie_file)

    print(f"🔄 刷新 metadata: {args.series} ...")
    requested_bvid = args.bvid or (_bvid_from_text(args.url) if args.url else "")
    if args.bvid or args.url:
        if not requested_bvid or not validation.validate_bvid(requested_bvid):
            print(f"❌ 无效 BVID 或视频 URL: {args.bvid or args.url}")
            sys.exit(EXIT_VALIDATION)
        video_url = args.url or f"https://www.bilibili.com/video/{requested_bvid}/"
        try:
            metadata = _fetch_single_video_metadata(video_url, cookie_file or None)
        except RuntimeError as exc:
            print(f"❌ API 获取失败: {exc}")
            sys.exit(EXIT_SYNC_FAIL)
        metadata_bvid = metadata.get("bvid") or metadata.get("id") or metadata.get("display_id")
        if metadata_bvid and metadata_bvid != requested_bvid:
            print(f"❌ metadata BVID 与请求不一致: {metadata_bvid} != {requested_bvid}")
            sys.exit(EXIT_VALIDATION)
        quality = _get_series_quality(db_path, args.series)
        normalized = _episode_from_metadata(metadata, requested_bvid)
        path = _write_metadata_file(Path(args.json_root), args.series, requested_bvid, quality, normalized)
        result = {
            "series": args.series,
            "bvid": requested_bvid,
            "metadata_written": 1,
            "json_path": str(path),
        }
        if _should_json(args):
            _run_json(args, result)
        else:
            print(f"  ✅ metadata 已刷新: {path.name}")
        return

    try:
        if config.source.get("sid"):
            info, episodes, count = asyncio.run(fetch_series_episodes(config, credential))
        else:
            info, episodes, count = asyncio.run(fetch_space_episodes(config, credential))
    except Exception as e:
        print(f"❌ API 获取失败: {e}")
        sys.exit(EXIT_SYNC_FAIL)

    json_root = Path(args.json_root) if hasattr(args, "json_root") and args.json_root else Path("/var/lib/bilibili-podcast/json")
    ep_dir = json_root / args.series
    ep_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    quality = _get_series_quality(db_path, args.series)
    for ep in episodes:
        bvid = ep["bvid"]
        path = ep_dir / f"{bvid}_{quality}.info.json"
        path.write_text(json.dumps(ep, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o644)
        written += 1
    if _should_json(args):
        _run_json(args, {"series": args.series, "metadata_written": written})
    else:
        print(f"  ✅ metadata 已刷新: {written} 条 JSON")


def cmd_paid_list_missing(args: argparse.Namespace) -> None:
    """List episodes that are missing media files."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)

    json_dir = Path(args.json_root) / args.series
    media_dir = Path(args.media_root) / args.series

    if not json_dir.exists():
        print(f"  无 metadata 目录: {json_dir}")
        return

    missing = []
    quality = _get_series_quality(db_path, args.series)
    for f in sorted(json_dir.glob("*.info.json")):
        bvid = f.stem.split("_")[0]
        media_file = media_dir / f"{bvid}_{quality}.mp3"
        if not media_file.exists():
            import json
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                title = meta.get("title", bvid)
                missing.append((bvid, title))
            except (json.JSONDecodeError, OSError):
                missing.append((bvid, bvid))

    if not missing:
        print(f"  ✅ {args.series} 无缺失 media")
        return

    print(f"  {len(missing)} 个缺失 media:")
    for bvid, title in missing:
        print(f"    {bvid}  {title}")


def cmd_paid_attach_media(args: argparse.Namespace) -> None:
    """Attach a manually uploaded media file to a series."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)

    # BVID validation
    if not validation.validate_bvid(args.bvid):
        print(f"❌ 无效 BVID: {args.bvid}")
        sys.exit(EXIT_VALIDATION)

    src = Path(args.server_path)
    # Path safety checks
    if not is_allowed_manual_media_path(src):
        print(f"❌ 路径不在白名单内: {src}")
        print(f"   设置 BILIBILI_PODCAST_MANUAL_MEDIA_DIRS 环境变量配置允许目录")
        sys.exit(EXIT_VALIDATION)
    resolved = src.resolve()
    if not resolved.exists():
        print(f"❌ 文件不存在: {src}")
        sys.exit(EXIT_VALIDATION)
    if resolved.suffix.lower() != ".mp3":
        print(f"❌ 只支持 .mp3 格式，收到: {resolved.suffix}")
        print(f"   请先将音频转码为 MP3 后再 attach")
        sys.exit(EXIT_VALIDATION)

    quality = _get_series_quality(db_path, args.series)
    dst_name = f"{args.bvid}_{quality}.mp3"
    media_root = Path(args.media_root) if args.media_root else Path("/var/lib/bilibili-podcast/media")
    dst = media_root / args.series / dst_name
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() and not args.replace:
        print(f"❌ 目标文件已存在: {dst}")
        print(f"   使用 --replace 覆盖")
        sys.exit(EXIT_VALIDATION)

    import shutil
    shutil.copy2(str(resolved), str(dst))
    dst.chmod(0o644)
    print(f"  ✅ media 已关联: {dst.name} ({dst.stat().st_size} bytes)")


def _fetch_single_video_metadata(video_url: str, cookie_file: str | None) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "yt_dlp", "--dump-json", "--skip-download"]
    if cookie_file:
        cmd.extend(["--cookies", cookie_file])
    cmd.append(video_url)
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"metadata 获取失败: {_sanitize(err)}") from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("metadata 输出不是有效 JSON") from exc


def _convert_media_to_mp3(src: Path, dst_dir: Path, bvid: str, quality: str, ffmpeg_bin: str) -> tuple[Path, bool]:
    if src.suffix.lower() == ".mp3":
        return src, False
    bitrate = quality.lower()
    dst_dir.mkdir(parents=True, exist_ok=True)
    tmp = dst_dir / f".{bvid}_{quality}.{os.getpid()}.mp3"
    cmd = [
        ffmpeg_bin, "-y",
        "-i", str(src),
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", bitrate,
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        err = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg 转码失败: {_sanitize(err)}") from exc
    tmp.chmod(0o644)
    return tmp, True


def _copy_attached_media(src: Path, dst: Path, replace: bool) -> None:
    if dst.exists() and not replace:
        raise ValueError(f"目标文件已存在: {dst}")
    import shutil
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    dst.chmod(0o644)


def cmd_paid_add_item(args: argparse.Namespace) -> None:
    """Add one manual media item from a user-provided media file and video URL."""
    db_path = _get_db(args)
    _require_series(db_path, args.series)

    src = Path(args.media_path)
    if not is_allowed_manual_media_path(src):
        print(f"❌ 路径不在白名单内: {src}")
        print("   设置 BILIBILI_PODCAST_MANUAL_MEDIA_DIRS 环境变量配置允许目录")
        sys.exit(EXIT_VALIDATION)
    src = src.resolve()
    if not src.exists() or not src.is_file():
        print(f"❌ 文件不存在: {src}")
        sys.exit(EXIT_VALIDATION)

    bvid = _bvid_from_text(args.url)
    if not bvid or not validation.validate_bvid(bvid):
        print(f"❌ URL 中未找到有效 BVID: {args.url}")
        sys.exit(EXIT_VALIDATION)

    quality = _get_series_quality(db_path, args.series)
    media_root = Path(args.media_root) if args.media_root else Path("/var/lib/bilibili-podcast/media")
    json_root = Path(args.json_root) if args.json_root else Path("/var/lib/bilibili-podcast/json")
    rss_root = Path(args.rss_root) if args.rss_root else Path("/var/lib/bilibili-podcast/rss")
    media_dst = media_root / args.series / f"{bvid}_{quality}.mp3"
    json_dst = json_root / args.series / f"{bvid}_{quality}.info.json"
    rss_dst = rss_root / f"{args.series}.xml"
    if media_dst.exists() and not args.replace:
        print(f"❌ 目标文件已存在: {media_dst}")
        print("   使用 --replace 覆盖")
        sys.exit(EXIT_VALIDATION)

    media_backup = _file_backup(media_dst)
    json_backup = _file_backup(json_dst)
    rss_backup = _file_backup(rss_dst)
    try:
        metadata = _fetch_single_video_metadata(args.url, args.cookie_file or os.environ.get("BILIBILI_PODCAST_COOKIE_FILE"))
        meta_bvid = metadata.get("bvid") or metadata.get("id") or metadata.get("display_id") or bvid
        if meta_bvid != bvid:
            raise ValueError(f"metadata BVID 与 URL 不一致: {meta_bvid} != {bvid}")
        converted, is_temp = _convert_media_to_mp3(src, media_dst.parent, bvid, quality, args.ffmpeg_bin)
        try:
            _copy_attached_media(converted, media_dst, args.replace)
        finally:
            if is_temp:
                converted.unlink(missing_ok=True)

        metadata["bvid"] = bvid
        metadata["duration"] = _normalize_duration(metadata.get("duration"))
        metadata.setdefault("webpage_url", args.url)
        metadata.setdefault("link", args.url)
        _write_metadata_file(json_root, args.series, bvid, quality, metadata)

        rss_path, item_count = rebuild_paid_rss(
            db_path,
            args.series,
            json_root=json_root,
            media_root=media_root,
            rss_root=rss_root,
            media_base_url=args.media_base_url or "http://localhost:58743",
        )
    except (ValueError, RuntimeError, OSError) as exc:
        _restore_file(media_dst, media_backup)
        _restore_file(json_dst, json_backup)
        _restore_file(rss_dst, rss_backup)
        print(f"❌ {exc}")
        sys.exit(EXIT_VALIDATION if isinstance(exc, ValueError) else EXIT_SYNC_FAIL)

    try:
        if args.publish_script:
            subprocess.run([args.publish_script], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"❌ 发布脚本执行失败: {_sanitize(str(exc))}")
        sys.exit(EXIT_SYNC_FAIL)

    result = {
        "series": args.series,
        "bvid": bvid,
        "media_path": str(media_dst),
        "json_path": str(json_dst),
        "rss_path": str(rss_path),
        "rss_items": item_count,
    }
    if _should_json(args):
        _run_json(args, result)
    print(f"  ✅ 手动条目已新增: {bvid}")
    print(f"  media: {media_dst}")
    print(f"  metadata: {json_dst}")
    print(f"  RSS 已生成: {rss_path} ({item_count} 条)")


def _episode_from_metadata(meta: dict[str, Any], bvid: str) -> dict[str, Any]:
    """Normalize sync or yt-dlp metadata into the RSS episode shape."""
    episode = dict(meta)
    episode["bvid"] = episode.get("bvid") or episode.get("id") or episode.get("display_id") or bvid
    episode["title"] = episode.get("title") or episode.get("fulltitle") or bvid
    episode["description"] = episode.get("description") or ""
    episode["pubdate"] = int(episode.get("pubdate") or episode.get("timestamp") or 0)
    episode["duration"] = _normalize_duration(episode.get("duration"))
    episode["image"] = episode.get("image") or episode.get("thumbnail") or ""
    episode["link"] = episode.get("link") or episode.get("webpage_url") or f"https://www.bilibili.com/video/{bvid}"
    return episode


def rebuild_paid_rss(
    db_path: str,
    series: str,
    *,
    json_root: Path,
    media_root: Path,
    rss_root: Path,
    media_base_url: str,
) -> tuple[Path, int]:
    """Rebuild RSS for a manual media series from existing metadata + media."""
    _require_series(db_path, series)

    json_dir = json_root / series
    media_dir = media_root / series

    if not json_dir.exists():
        raise ValueError(f"metadata 目录不存在: {json_dir}")

    episodes = []
    quality = _get_series_quality(db_path, series)
    for f in sorted(json_dir.glob("*.info.json")):
        bvid = f.stem.split("_")[0]
        media_file = media_dir / f"{bvid}_{quality}.mp3"
        if not media_file.exists():
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            episodes.append(_episode_from_metadata(meta, bvid))
        except (json.JSONDecodeError, OSError, ValueError):
            continue

    if not episodes:
        raise ValueError(f"{series} 没有可写入 RSS 的 media 文件")

    from bilibili_podcast.sync import generate_rss
    from bilibili_podcast.sync import SyncPaths
    from bilibili_podcast.utils.series_config import SeriesConfig
    from bilibili_podcast.db import transaction

    paths = SyncPaths(
        media_root=media_root, json_root=json_root,
        rss_root=rss_root, media_base_url=media_base_url,
    )

    existing_cover = _read_existing_channel_image(rss_root / f"{series}.xml", media_base_url)
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM series WHERE series=?", (series,)).fetchone()
        sp_row = conn.execute("SELECT * FROM sync_policy WHERE series=?", (series,)).fetchone()
        src_row = conn.execute("SELECT * FROM series_source WHERE series=?", (series,)).fetchone()
    if not row:
        raise ValueError(f"系列不存在: {series}")

    quality = sp_row["quality"] if sp_row and sp_row["quality"] else "64K"
    source_dict = dict(src_row) if src_row else {"uid": 1}
    if not source_dict.get("space_url") and source_dict.get("uid"):
        source_dict["space_url"] = f"https://space.bilibili.com/{source_dict['uid']}"
    cover_art = _placeholder_media_token(row["cover_art"] or "", media_base_url) or existing_cover
    cfg = SeriesConfig(
        series=series, enabled=True, title=row["title"],
        description=row["description"] or "",
        author=row["author"], cover_art=cover_art,
        category=row["category"] or "", subcategories=[],
        explicit=bool(row["explicit"]), lang=row["lang"] or "zh-CN",
        source=source_dict, sync={"quality": quality},
        filters={}, paid_preview={}, keep_last=0,
    )

    up_info = {"name": row["author"], "face": cover_art, "sign": row["description"]}
    rss_path = generate_rss(cfg, paths, up_info, episodes, "__MEDIA_PLACEHOLDER__", dry_run=False)
    _ensure_channel_itunes_image(rss_path, cover_art)
    return rss_path, len(episodes)


def cmd_paid_rebuild_rss(args: argparse.Namespace) -> None:
    """Rebuild RSS for a manual media series from existing metadata + media."""
    db_path = _get_db(args)
    media_root = Path(args.media_root) if args.media_root else Path("/var/lib/bilibili-podcast/media")
    json_root = Path(args.json_root) if args.json_root else Path("/var/lib/bilibili-podcast/json")
    rss_root = Path(args.rss_root) if args.rss_root else Path("/var/lib/bilibili-podcast/rss")

    try:
        rss_path, item_count = rebuild_paid_rss(
            db_path,
            args.series,
            json_root=json_root,
            media_root=media_root,
            rss_root=rss_root,
            media_base_url=args.media_base_url or "http://localhost:58743",
        )
    except ValueError as exc:
        message = str(exc)
        if "没有可写入 RSS" in message:
            print(f"  ⚠️ {message}")
            return
        print(f"❌ {message}")
        sys.exit(EXIT_VALIDATION)

    print(f"  ✅ RSS 已生成: {rss_path} ({item_count} 条, 含 __MEDIA_PLACEHOLDER__)")
    if _should_json(args):
        _run_json(args, {"series": args.series, "rss_path": str(rss_path), "items": item_count})


# ── Argument parser ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bilibili-podcast-admin",
        description="bilibili-podcast 系列管理 CLI",
    )
    parser.add_argument("--config-db", help=f"SQLite 数据库路径（默认: {DEFAULT_CONFIG_DB}）")
    parser.add_argument("--yes", action="store_true", help="跳过低风险确认")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写 DB、不执行同步")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--quiet", action="store_true", help="减少输出")
    parser.add_argument("--debug", action="store_true", help="输出诊断信息")

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="列出所有系列")
    p_list.set_defaults(handler=cmd_list)

    # show
    p_show = sub.add_parser("show", help="显示系列完整配置")
    p_show.set_defaults(handler=cmd_show)
    p_show.add_argument("series")

    def add_removal_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--apply", action="store_true", help="真正执行移除（默认只预览）")
        p.add_argument("--yes", dest="remove_yes", action="store_true", help="跳过 remove 确认")
        p.add_argument("--media-root", default=os.environ.get("BILIBILI_PODCAST_MEDIA_ROOT", "/var/lib/bilibili-podcast/media"))
        p.add_argument("--json-root", default=os.environ.get("BILIBILI_PODCAST_JSON_ROOT", "/var/lib/bilibili-podcast/json"))
        p.add_argument("--rss-root", default=os.environ.get("BILIBILI_PODCAST_RSS_ROOT", "/var/lib/bilibili-podcast/rss"))
        p.add_argument("--published-rss-root", default=os.environ.get("BILIBILI_PODCAST_PUBLISHED_RSS_ROOT", "/var/lib/bilibili-podcast/published-rss"))
        p.add_argument("--cron-script-dir", default=os.environ.get("BILIBILI_PODCAST_CRON_SCRIPT_DIR", "<server_path>"))
        p.add_argument("--browser-user-data-root", default=os.environ.get(
            "BILIBILI_PODCAST_BROWSER_USER_DATA_ROOT", "<server_path>",
        ))
        p.add_argument("--lock-file", default=os.environ.get(
            "BILIBILI_PODCAST_LOCK_FILE", "/var/lib/bilibili-podcast/state/bilibili-podcast.lock",
        ))
        p.add_argument("--users-conf", default=os.environ.get(
            "RSS_USERS_CONF", "<server_path>",
        ))

    # remove-series
    p_remove = sub.add_parser("remove-series", help="预览或永久移除系列及其本地产物")
    p_remove.set_defaults(handler=cmd_remove_series)
    p_remove.add_argument("series")
    add_removal_args(p_remove)

    # remove-up
    p_remove_up = sub.add_parser("remove-up", help="按 UID 预览或永久移除该 UP 的全部系列")
    p_remove_up.set_defaults(handler=cmd_remove_up)
    p_remove_up.add_argument("--uid", type=int, required=True, help="B 站 UP 主 UID")
    add_removal_args(p_remove_up)

    # add (interactive + non-interactive)
    p_add = sub.add_parser("add", help="新增系列（交互式，或 --url --series 等非交互参数）")
    p_add.set_defaults(handler=cmd_add)
    p_add.add_argument("--url", help="B 站 URL 或 UID（非交互模式）")
    p_add.add_argument("--series", help="系列标识（非交互模式）")
    p_add.add_argument("--title", help="标题")
    p_add.add_argument("--author", help="作者")
    p_add.add_argument("--description", help="描述")
    p_add.add_argument("--cover-art", help="封面图 URL")
    p_add.add_argument("--category", help="分类")
    p_add.add_argument("--lang", help="语言")
    p_add.add_argument("--exclude-keyword", action="append", default=[], help="黑名单关键词")
    p_add.add_argument("--include-keyword", action="append", default=[], help="白名单关键词")
    p_add.add_argument("--ad-keyword", action="append", default=[], help="广告关键词")
    p_add.add_argument("--exclude-bvid", action="append", default=[], help="排除 BVID")
    p_add.add_argument("--ad-bvid", action="append", default=[], help="广告 BVID")
    p_add.add_argument("--exclude-season-id", action="append", type=_positive_int, default=[], help="排除合集 ID")
    p_add.add_argument("--keep-last", type=int, help="RSS 保留条数")
    p_add.add_argument("--update-period", help="更新周期（如 12h）")
    p_add.add_argument("--quality", choices=["64K", "132K", "192K"], help="音频质量")
    p_add.add_argument("--fetch-strategy", choices=["api_first", "browser_first"], help="抓取策略")
    p_add.add_argument("--format", choices=["audio", "video"], help="输出格式")
    p_add.add_argument("--page-size", type=int, help="首页每页条数")
    p_add.add_argument("--incremental-page-size", type=int, help="增量页条数")
    p_add.add_argument("--max-pages", type=int, help="最大页数")
    p_add.add_argument("--max-requests-per-series", type=int, help="每系列最大请求数")
    p_add.add_argument("--request-interval-seconds", type=float, help="请求间隔")
    p_add.add_argument("--request-jitter-seconds", type=float, help="抖动")
    p_add.add_argument("--rate-limit-cooldown-seconds", type=int, help="限流冷却")
    p_add.add_argument("--cron", action="append", default=[], help="Cron 表达式（可多次）")
    p_add.add_argument("--exclude-paid", action="store_true", help="排除付费内容")
    p_add.add_argument("--update-existing", action="store_true", help="允许覆盖已有系列")
    p_add.add_argument("--dry-run", dest="add_dry_run", action="store_true", help="只预览，不写入 DB（与全局 --dry-run 效果相同，支持放在子命令后）")
    p_add.add_argument("--yes", dest="add_yes", action="store_true", help="跳过确认（与全局 --yes 效果相同，支持放在子命令后）")

    # edit
    p_edit = sub.add_parser("edit", help="交互式编辑系列")
    p_edit.set_defaults(handler=cmd_edit)
    p_edit.add_argument("series")

    # filters
    p_filters = sub.add_parser("filters", help="管理过滤规则", aliases=["filters-show", "fs"])
    p_filters.set_defaults(handler=cmd_filters)
    p_filters.add_argument("series")

    # filters add
    p_fa = sub.add_parser("filters-add", help="追加过滤规则", aliases=["fa"])
    p_fa.set_defaults(handler=cmd_filters_add)
    p_fa.add_argument("series")
    p_fa.add_argument("--exclude-keyword", action="append", default=[], help="黑名单关键词")
    p_fa.add_argument("--include-keyword", action="append", default=[], help="白名单关键词")
    p_fa.add_argument("--ad-keyword", action="append", default=[], help="广告关键词")
    p_fa.add_argument("--exclude-bvid", action="append", default=[], help="排除 BVID")
    p_fa.add_argument("--ad-bvid", action="append", default=[], help="广告 BVID")
    p_fa.add_argument("--exclude-season-id", action="append", type=_positive_int, default=[], help="排除合集 ID")
    p_fa.add_argument("--exclude-paid", action="store_true", help="排除付费内容")
    p_fa.add_argument("--yes", dest="fa_yes", action="store_true", help="跳过确认")

    # filters remove
    p_fr = sub.add_parser("filters-remove", help="删除过滤规则（默认禁用，--delete 物理删除）", aliases=["fdel"])
    p_fr.set_defaults(handler=cmd_filters_remove)
    p_fr.add_argument("series")
    p_fr.add_argument("--exclude-keyword", action="append", default=[], help="黑名单关键词")
    p_fr.add_argument("--include-keyword", action="append", default=[], help="白名单关键词")
    p_fr.add_argument("--ad-keyword", action="append", default=[], help="广告关键词")
    p_fr.add_argument("--exclude-bvid", action="append", default=[], help="排除 BVID")
    p_fr.add_argument("--ad-bvid", action="append", default=[], help="广告 BVID")
    p_fr.add_argument("--exclude-season-id", action="append", type=_positive_int, default=[], help="排除合集 ID")
    p_fr.add_argument("--exclude-paid", action="store_true", help="排除付费内容")
    p_fr.add_argument("--delete", action="store_true", help="物理删除（默认仅禁用）")

    # filters disable
    p_fd = sub.add_parser("filters-disable", help="禁用过滤规则", aliases=["fd"])
    p_fd.set_defaults(handler=cmd_filters_disable)
    p_fd.add_argument("series")
    p_fd.add_argument("--rule-id", type=int, required=True, help="规则 ID")

    # filters enable
    p_fe = sub.add_parser("filters-enable", help="启用过滤规则", aliases=["fe"])
    p_fe.set_defaults(handler=cmd_filters_enable)
    p_fe.add_argument("series")
    p_fe.add_argument("--rule-id", type=int, required=True, help="规则 ID")

    # filters import
    p_fi = sub.add_parser("filters-import", help="从文件导入过滤规则", aliases=["fi"])
    p_fi.set_defaults(handler=cmd_filters_import)
    p_fi.add_argument("series")
    p_fi.add_argument("--type", required=True, choices=[
        "exclude_keyword", "include_keyword", "ad_keyword",
        "exclude_bvid", "ad_bvid",
        "exclude_season_id",
    ], help="规则类型")
    p_fi.add_argument("--file", required=True, help="规则文件，每行一条")

    # preview
    p_preview = sub.add_parser("preview", help="执行干跑预览")
    p_preview.set_defaults(handler=cmd_preview)
    p_preview.add_argument("series")

    # sync
    p_sync = sub.add_parser("sync", help="触发同步（默认干跑）")
    p_sync.set_defaults(handler=cmd_sync)
    p_sync.add_argument("series")
    p_sync.add_argument("--apply", action="store_true", help="真正同步（非干跑）")

    # sync-policy show/set
    p_sp = sub.add_parser("sync-policy", help="管理同步策略", aliases=["sp"])
    sp_sub = p_sp.add_subparsers(dest="sync_policy_sub", required=True)
    p_sp_show = sp_sub.add_parser("show", help="显示同步策略")
    p_sp_show.set_defaults(handler=cmd_sync_policy_show)
    p_sp_show.add_argument("series")
    p_sp_set = sp_sub.add_parser("set", help="设置同步策略字段")
    p_sp_set.set_defaults(handler=cmd_sync_policy_set)
    p_sp_set.add_argument("series")
    p_sp_set.add_argument("--page-size", type=int)
    p_sp_set.add_argument("--incremental-page-size", type=int)
    p_sp_set.add_argument("--max-pages", type=int)
    p_sp_set.add_argument("--max-requests-per-series", type=int)
    p_sp_set.add_argument("--request-interval", dest="request_interval_seconds", type=float)
    p_sp_set.add_argument("--request-jitter", dest="request_jitter_seconds", type=float)
    p_sp_set.add_argument("--rate-limit-cooldown", dest="rate_limit_cooldown_seconds", type=int)
    p_sp_set.add_argument("--update-period")
    p_sp_set.add_argument("--format", choices=["audio", "video"])
    p_sp_set.add_argument("--quality", choices=["64K", "132K", "192K"])
    p_sp_set.add_argument("--fetch-strategy", choices=["api_first", "browser_first"])
    p_sp_set.add_argument("--keep-last", type=int)
    p_sp_set.add_argument("--browser-fallback", type=int, choices=[0, 1])
    p_sp_set.add_argument("--browser-wait-min", dest="browser_wait_min_seconds", type=float)
    p_sp_set.add_argument("--browser-wait-max", dest="browser_wait_max_seconds", type=float)
    p_sp_set.add_argument("--browser-fallback-cooldown", dest="browser_fallback_cooldown_seconds", type=int)
    p_sp_set.add_argument("--require-paid-confirmation", type=int, choices=[0, 1])
    p_sp_set.add_argument("--min-duration", dest="min_duration_seconds", type=int)
    p_sp_set.add_argument("--max-duration", dest="max_duration_seconds", type=int)
    p_sp_set.add_argument("--yes", dest="sp_set_yes", action="store_true", help="跳过确认")

    # cron
    p_cron = sub.add_parser("cron", help="管理定时任务")
    cron_sub = p_cron.add_subparsers(dest="cron_sub", required=True)

    p_cron_plan = cron_sub.add_parser("plan", help="预览 cron 计划")
    p_cron_plan.add_argument("--cron-script-dir", help="目标 wrapper 目录（指定后输出真实路径，与 cron apply 一致）")
    p_cron_plan.set_defaults(handler=cmd_cron_plan)

    p_cron_apply = cron_sub.add_parser("apply", help="安装 crontab（需二次确认）")
    p_cron_apply.add_argument("--cron-script-dir", help="wrapper 脚本输出目录（默认自动/auto，生产环境请指定绝对路径如 <server_path>）")
    p_cron_apply.add_argument("--yes", dest="cron_yes", action="store_true", help="跳过确认（与全局 --yes 效果相同，支持放在子命令后）")
    p_cron_apply.set_defaults(handler=cmd_cron_apply)

    p_cron_show = cron_sub.add_parser("show", help="显示系列 cron 配置")
    p_cron_show.set_defaults(handler=cmd_cron_show)
    p_cron_show.add_argument("series")

    p_cron_set = cron_sub.add_parser("set", help="设置系列 cron（仅写 DB，不安装）")
    p_cron_set.set_defaults(handler=cmd_cron_set)
    p_cron_set.add_argument("series")
    p_cron_set.add_argument("--schedule", action="append", default=[], help="Cron 表达式（可多次）")
    p_cron_set.add_argument("--yes", dest="cron_set_yes", action="store_true", help="跳过确认（与全局 --yes 效果相同，支持放在子命令后）")

    # scheduler (new, wraps SchedulerService)
    p_sched = sub.add_parser("scheduler", help="管理调度（当前 backend=cron）")
    sched_sub = p_sched.add_subparsers(dest="scheduler_sub", required=True)

    p_sched_plan = sched_sub.add_parser("plan", help="预览调度计划")
    p_sched_plan.add_argument("--cron-script-dir", help="目标 wrapper 目录（指定后输出真实路径）")
    p_sched_plan.add_argument("--backend", dest="scheduler_backend", default="cron",
                              help="调度后端（当前仅支持 cron）")
    p_sched_plan.add_argument("--series", help="系列标识（systemd backend 必填）")
    p_sched_plan.set_defaults(handler=cmd_scheduler_plan)

    p_sched_apply = sched_sub.add_parser("apply", help="安装调度（需二次确认）")
    p_sched_apply.add_argument("--cron-script-dir", help="wrapper 输出目录")
    p_sched_apply.add_argument("--yes", dest="scheduler_yes", action="store_true",
                               help="跳过确认（与全局 --yes 效果相同，支持放在子命令后）")
    p_sched_apply.add_argument("--backend", dest="scheduler_backend", default="cron",
                               help="调度后端（当前仅支持 cron）")
    p_sched_apply.add_argument("--series", help="系列标识（systemd backend 必填）")
    p_sched_apply.set_defaults(handler=cmd_scheduler_apply)

    p_sched_status = sched_sub.add_parser("status", help="显示调度状态")
    p_sched_status.add_argument("series", nargs="?", help="系列标识（可选，默认全部）")
    p_sched_status.add_argument("--backend", dest="scheduler_backend", default="cron",
                                help="调度后端（当前仅支持 cron）")
    p_sched_status.set_defaults(handler=cmd_scheduler_status)

    p_sched_set = sched_sub.add_parser("set", help="设置系列调度（仅写 DB，不安装）")
    p_sched_set.add_argument("series")
    p_sched_set.add_argument("--schedule", action="append", default=[], help="Cron 表达式（可多次）")
    p_sched_set.add_argument("--yes", dest="scheduler_set_yes", action="store_true",
                             help="跳过确认（与全局 --yes 效果相同，支持放在子命令后）")
    p_sched_set.add_argument("--backend", dest="scheduler_backend", default="cron",
                             help="调度后端（当前仅支持 cron）")
    p_sched_set.set_defaults(handler=cmd_scheduler_set)

    p_sched_disable = sched_sub.add_parser("disable", help="禁用试点 systemd timer，恢复 cron 调度")
    p_sched_disable.add_argument("--backend", dest="scheduler_backend", default="systemd",
                                 choices=["systemd"], help="调度后端（disable 仅支持 systemd）")
    p_sched_disable.add_argument("--series", required=True, help="系列标识")
    p_sched_disable.add_argument("--cron-script-dir", help="wrapper 输出目录（生产环境必填如 <server_path>）")
    p_sched_disable.add_argument("--delete-units", action="store_true", help="同时删除 unit 文件")
    p_sched_disable.add_argument("--yes", dest="scheduler_disable_yes", action="store_true",
                                 help="跳过确认")
    p_sched_disable.set_defaults(handler=cmd_scheduler_disable)

    # paid — manual media management
    p_paid = sub.add_parser("paid", help="管理付费/手动 media 系列（如 paid-sample）")
    paid_sub = p_paid.add_subparsers(dest="paid_sub", required=True)

    p_refresh = paid_sub.add_parser("refresh-metadata", help="刷新 metadata（不下载 media）")
    p_refresh.add_argument("series")
    p_refresh.add_argument("--json-root", default="/var/lib/bilibili-podcast/json", help="JSON metadata 目录")
    p_refresh.add_argument("--cookie-file", help="Netscape cookie 文件路径")
    refresh_target = p_refresh.add_mutually_exclusive_group()
    refresh_target.add_argument("--bvid", help="仅刷新指定 BVID")
    refresh_target.add_argument("--url", help="仅刷新指定 B 站视频 URL")
    p_refresh.set_defaults(handler=cmd_paid_refresh_metadata)

    p_list = paid_sub.add_parser("list-missing", help="列出缺失 media 的条目")
    p_list.add_argument("series")
    p_list.add_argument("--json-root", default="/var/lib/bilibili-podcast/json", help="JSON metadata 目录")
    p_list.add_argument("--media-root", default="/var/lib/bilibili-podcast/media")
    p_list.set_defaults(handler=cmd_paid_list_missing)

    p_attach = paid_sub.add_parser("attach-media", help="关联用户上传的 media 文件")
    p_attach.add_argument("series")
    p_attach.add_argument("--bvid", required=True)
    p_attach.add_argument("--server-path", required=True)
    p_attach.add_argument("--media-root", default="/var/lib/bilibili-podcast/media")
    p_attach.add_argument("--replace", action="store_true", help="覆盖已有 media")
    p_attach.set_defaults(handler=cmd_paid_attach_media)

    p_add_item = paid_sub.add_parser("add-item", help="从用户提供 media + B 站视频页面新增手动条目")
    p_add_item.add_argument("series")
    p_add_item.add_argument("--url", required=True, help="B 站视频页面 URL")
    p_add_item.add_argument("--media-path", required=True, help="服务器上的用户 media 文件路径")
    p_add_item.add_argument("--media-root", default="/var/lib/bilibili-podcast/media")
    p_add_item.add_argument("--json-root", default="/var/lib/bilibili-podcast/json")
    p_add_item.add_argument("--rss-root", default="/var/lib/bilibili-podcast/rss")
    p_add_item.add_argument("--media-base-url", default="http://localhost:58743")
    p_add_item.add_argument("--cookie-file", help="Netscape cookie 文件路径")
    p_add_item.add_argument("--ffmpeg-bin", default="ffmpeg", help="ffmpeg 可执行文件路径")
    p_add_item.add_argument("--replace", action="store_true", help="覆盖已有 media")
    p_add_item.add_argument("--publish-script", help="重建 RSS 后执行的发布脚本")
    p_add_item.set_defaults(handler=cmd_paid_add_item)

    p_rebuild = paid_sub.add_parser("rebuild-rss", help="从已有 metadata + media 重建 RSS")
    p_rebuild.add_argument("series")
    p_rebuild.add_argument("--media-root", default="/var/lib/bilibili-podcast/media")
    p_rebuild.add_argument("--json-root", default="/var/lib/bilibili-podcast/json")
    p_rebuild.add_argument("--rss-root", default="/var/lib/bilibili-podcast/rss")
    p_rebuild.add_argument("--media-base-url", default="http://localhost:58743")
    p_rebuild.set_defaults(handler=cmd_paid_rebuild_rss)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        args.handler(args)
    except KeyboardInterrupt:
        print("\n已取消")
        return EXIT_USER_CANCEL
    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        return EXIT_DB_ERROR
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
