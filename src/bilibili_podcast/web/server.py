"""Bilipod web management UI — FastAPI + Jinja2 + SQLite server."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .. import db
from ..services.config_service import ConfigService
from ..services.scheduler_service import SchedulerService
from ..services.filter_service import FilterRuleService, build_filters_from_rows
from ..services.sync_policy_service import SyncPolicyService
from ..services.preview_service import PreviewService
from ..services import validation
from ..cli_admin import (
    _get_allowed_media_dirs,
    _get_series_quality,
    is_allowed_manual_media_path,
    rebuild_paid_rss,
)
from ..config import ConfigManager, ConfigSnapshot
from .. import cli_admin as _cli_admin

# ── Config ──────────────────────────────────────────────────────────────
DB_PATH = ""
PASSWORD = ""
_CONFIG_MANAGER: ConfigManager | None = None
_CONFIG_SNAPSHOT: ConfigSnapshot | None = None
_COOKIE_NAME = "bilipod_session"
_SESSION_MAX_AGE = 86400  # 24 hours
_HTTPS = False
_cli_admin._CONFIG = None

_SECRET_KEY = hashlib.sha256(PASSWORD.encode()).hexdigest()

# ── App ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_TEMPLATE_DIR = str(_HERE / "templates")
router = APIRouter()
templates = Jinja2Templates(directory=_TEMPLATE_DIR)


# Workaround Jinja2 3.1.6 LRU cache key bug: disable template cache
class _NoCache:
    """Dummy cache that accepts any key — workaround Jinja2 3.1.6 LRU bug."""
    def get(self, key): return None
    def set(self, key, value): pass
    def __setitem__(self, key, value): pass
    def __contains__(self, key): return False


templates.env.cache = _NoCache()


# ── Template filters ───────────────────────────────────────────────────
def _timestamp_to_str(ts: int) -> str:
    """Convert Unix timestamp to readable datetime string."""
    if not ts or ts <= 0:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _url_enc_draft(data: dict) -> str:
    """JSON-encode and URL-quote draft data for query string."""
    return quote(json.dumps(data, ensure_ascii=False))


templates.env.filters["timestamp_to_str"] = _timestamp_to_str
templates.env.filters["url_enc_draft"] = _url_enc_draft

# ── Auth helpers ────────────────────────────────────────────────────────
_serializer = URLSafeTimedSerializer(_SECRET_KEY)


def _runtime_config() -> ConfigSnapshot:
    if _CONFIG_SNAPSHOT is None:
        raise RuntimeError("web configuration was not injected")
    return _CONFIG_SNAPSHOT


def csrf_token() -> str:
    return _serializer.dumps("csrf", salt="csrf")


def _verify_csrf(token: str) -> bool:
    try:
        _serializer.loads(token, max_age=_SESSION_MAX_AGE, salt="csrf")
        return True
    except (BadSignature, SignatureExpired):
        return False


def _session_token() -> str:
    return _serializer.dumps("auth", salt="session")


def _get_session(request: Request) -> str | None:
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    try:
        return _serializer.loads(cookie, max_age=_SESSION_MAX_AGE, salt="session")
    except (BadSignature, SignatureExpired):
        return None


def _login_required(request: Request) -> RedirectResponse | None:
    if _get_session(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return None


def _csrf_guard(request: Request, token: str | None) -> HTMLResponse | None:
    if _get_session(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    if not token or not _verify_csrf(token):
        return HTMLResponse("Invalid CSRF token", status_code=403)
    return None


# ── Helper: build filters dict from DB rows ────────────────────────────
# (moved to services.filter_service.build_filters_from_rows)


def _parse_subcategories(raw: str) -> str:
    """Parse comma-separated subcategories into JSON array string."""
    parts = [s.strip() for s in raw.split(",") if s.strip()]
    import json
    return json.dumps(parts, ensure_ascii=False)


async def _fetch_up_face_url(uid: int) -> str | None:
    """Try to fetch UP主 avatar URL from B站 API. Returns None on failure."""
    if not uid or uid <= 0:
        return None
    try:
        from bilibili_api import user
        u = user.User(uid)
        info = await u.get_user_info()
        return info.get("face")
    except Exception:
        return None


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/login")
async def login_page(request: Request):
    if _get_session(request) is not None:
        return RedirectResponse(url="/series", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    if password == PASSWORD:
        resp = RedirectResponse(url="/series", status_code=302)
        resp.set_cookie(_COOKIE_NAME, _session_token(),
                        max_age=_SESSION_MAX_AGE, httponly=True, samesite="lax",
                        secure=_HTTPS)
        return resp
    return templates.TemplateResponse(request, "login.html", {
        "error": "密码错误",
    }, status_code=401)


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(_COOKIE_NAME)
    return resp


@router.get("/")
async def root():
    return RedirectResponse(url="/series", status_code=302)


@router.get("/config")
async def config_view(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect
    if _CONFIG_MANAGER is None or _CONFIG_SNAPSHOT is None:
        raise HTTPException(status_code=503, detail="unified configuration is not loaded")
    redacted = _CONFIG_MANAGER.redacted(_CONFIG_SNAPSHOT)
    rows = []
    for scope, values in redacted.items():
        stack = [(scope, values)]
        while stack:
            prefix, value = stack.pop()
            if isinstance(value, dict):
                stack.extend((f"{prefix}.{key}", item) for key, item in reversed(value.items()))
            else:
                source = _CONFIG_SNAPSHOT.sources.get(prefix.replace("manual_media", "manual-media").replace("rss_users", "rss-users"))
                rows.append({"field": prefix, "source": source.name if source else "derived", "value": value})
    return templates.TemplateResponse(request, "config.html", {"rows": rows})


# ── /series — list ─────────────────────────────────────────────────────
@router.get("/series")
async def series_list(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect

    with db.transaction(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT s.series, s.enabled, s.title, s.author,
                   ss.type AS source_type, ss.uid,
                   sy.last_success_at, sy.last_attempt_at,
                   sy.rate_limited_until
            FROM series s
            LEFT JOIN series_source ss ON ss.series = s.series
            LEFT JOIN sync_state sy ON sy.series = s.series
            ORDER BY s.series
        """).fetchall()

    return templates.TemplateResponse(request, "series_list.html", {
        "series_list": [dict(r) for r in rows],
        "csrf_token": csrf_token(),
    })


# ── /series/{series}/toggle ────────────────────────────────────────────
@router.post("/series/{series}/toggle")
async def series_toggle(request: Request, series: str,
                        csrf_token: str = Form("")):
    guard = _csrf_guard(request, csrf_token)
    if guard:
        return guard

    with db.transaction(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM series WHERE series=?",
                           (series,)).fetchone()
        if not row:
            raise HTTPException(404)
        import json
        from ..utils.series_config import SeriesConfig
        cfg = SeriesConfig(
            series=row["series"], enabled=not row["enabled"],
            title=row["title"], description=row["description"] or "",
            author=row["author"], cover_art=row["cover_art"] or "",
            category=row["category"] or "",
            subcategories=json.loads(row["subcategories"]) if row["subcategories"] else [],
            explicit=bool(row["explicit"]), lang=row["lang"] or "zh-CN",
            source={}, sync={}, filters={}, paid_preview={}, keep_last=100,
        )
        from .. import db as _db
        _db.upsert_series(conn, cfg)

    return RedirectResponse(url="/series", status_code=302)


# ── /series/new — form ─────────────────────────────────────────────────
@router.get("/series/new")
async def series_new_form(request: Request, draft: str = ""):
    redirect = _login_required(request)
    if redirect:
        return redirect

    defaults = {"title": "", "description": "", "author": "",
                "cover_art": "", "category": "", "lang": "zh-CN",
                "explicit": False}
    source = {"space_url": "", "uid": None, "type": "space", "sid": None}
    is_resolve = False

    if draft:
        import json
        try:
            data = json.loads(draft)
            if isinstance(data, dict):
                defaults["title"] = data.get("title", "")
                defaults["author"] = data.get("author", "")
                defaults["description"] = data.get("description", "")
                defaults["cover_art"] = data.get("cover_art", "")
                src = data.get("source", {})
                source["space_url"] = src.get("space_url", "")
                source["uid"] = src.get("uid")
                source["type"] = src.get("type", "space")
                source["sid"] = src.get("sid")
                is_resolve = True
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # Try to show B站 default avatar when cover_art is empty
    up_face = (await _fetch_up_face_url(source.get("uid") or 0)
               if not defaults.get("cover_art") else None)

    return templates.TemplateResponse(request, "series_form.html", {
        "is_new": True, "series": None,
        "data": defaults, "source": source, "is_resolve": is_resolve,
        "up_face_url": up_face,
        "csrf_token": csrf_token(),
    })


@router.post("/series/new")
async def series_new_create(
    request: Request,
    series: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
    author: str = Form(""),
    cover_art: str = Form(""),
    category: str = Form(""),
    subcategories: str = Form(""),
    lang: str = Form("zh-CN"),
    explicit: bool = Form(False),
    space_url: str = Form(""),
    uid: int = Form(0),
    source_type: str = Form("space"),
    sid: int = Form(0),
    csrf_value: str = Form("", alias="csrf_token"),
):
    guard = _csrf_guard(request, csrf_value)
    if guard:
        return guard

    # Parse subcategories
    _subcats = _parse_subcategories(subcategories)

    errors = []
    if not validation.validate_slug(series):
        errors.append("系列标识只能包含小写字母、数字、横线和下划线")
    if not title:
        errors.append("标题不能为空")
    if not author:
        errors.append("作者不能为空")
    if source_type == "space" and not space_url and uid <= 0:
        errors.append("Space 模式需要提供 Space URL 或 UID")
    if source_type in ("season", "series") and sid <= 0:
        errors.append("合集/系列模式需要提供 SID")
    if source_type not in ("space", "season", "series"):
        errors.append("来源类型无效")

    if errors:
        return templates.TemplateResponse(request, "series_form.html", {
            "is_new": True, "series": None,
            "data": {"title": title, "description": description,
                     "author": author, "cover_art": cover_art,
                     "category": category, "subcategories": _subcats,
                     "lang": lang, "explicit": explicit},
            "source": {"space_url": space_url,
                       "uid": uid if uid > 0 else None,
                       "type": source_type,
                       "sid": sid if sid > 0 else None},
            "errors": errors, "csrf_token": csrf_token(),
            "up_face_url": None,
        }, status_code=400)

    conflict_context = {
        "is_new": True, "series": None,
        "data": {"title": title, "description": description,
                 "author": author, "cover_art": cover_art,
                 "category": category, "lang": lang,
                 "explicit": explicit},
        "source": {"space_url": space_url,
                   "uid": uid if uid > 0 else None,
                   "type": source_type,
                   "sid": sid if sid > 0 else None},
        "errors": ["系列标识已被占用"],
        "csrf_token": csrf_token(),
        "up_face_url": None,
    }

    subcats_list = json.loads(_subcats) if _subcats else []
    from ..utils.series_config import SeriesConfig
    cfg = SeriesConfig(
        series=series, enabled=True, title=title,
        description=description, author=author,
        cover_art=cover_art, category=category,
        subcategories=subcats_list, explicit=explicit, lang=lang,
        source={"space_url": space_url, "uid": uid if uid > 0 else None,
                "type": source_type, "sid": sid if sid > 0 else None},
        sync={}, filters={}, paid_preview={"enabled": False},
        keep_last=100,
    )

    with db.transaction(DB_PATH) as conn:
        inserted = conn.execute(
            """INSERT INTO series (
                   series, enabled, title, description, author, cover_art,
                   category, subcategories, explicit, lang)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(series) DO NOTHING""",
            (cfg.series, int(cfg.enabled), cfg.title, cfg.description,
             cfg.author, cfg.cover_art, cfg.category, _subcats,
             int(cfg.explicit), cfg.lang),
        )
        if inserted.rowcount == 0:
            return templates.TemplateResponse(
                request, "series_form.html", conflict_context, status_code=400,
            )

        db.upsert_source(conn, cfg)
        db.upsert_sync_policy(conn, cfg)
        db.upsert_paid_preview(conn, cfg)

    return RedirectResponse(url=f"/series/{series}/sync", status_code=302)


# ── /resolve — URL parsing ─────────────────────────────────────────────
@router.get("/resolve")
async def resolve_page(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect

    return templates.TemplateResponse(request, "resolve.html", {
        "csrf_token": csrf_token(),
    })


@router.post("/resolve")
async def resolve_url(request: Request, url: str = Form(""),
                      csrf_value: str = Form("", alias="csrf_token")):
    guard = _csrf_guard(request, csrf_value)
    if guard:
        return guard

    if not url.strip():
        return templates.TemplateResponse(request, "resolve.html", {
            "error": "请输入 B 站 URL、UID 或 SID",
            "csrf_token": csrf_token(),
        })

    from . import resolver as _resolver

    try:
        result = await _resolver.resolve_url(url.strip())
    except Exception as e:
        result = {"error": f"解析出错: {e}"}

    return templates.TemplateResponse(request, "resolve.html", {
        "result": result,
        "input_url": url,
        "csrf_token": csrf_token(),
    })


# ── /series/{series} — detail/edit ─────────────────────────────────────
@router.get("/series/{series}")
async def series_detail(request: Request, series: str):
    redirect = _login_required(request)
    if redirect:
        return redirect

    with db.transaction(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM series WHERE series=?",
                           (series,)).fetchone()
        if not row:
            raise HTTPException(404)
        src = conn.execute("SELECT * FROM series_source WHERE series=?",
                           (series,)).fetchone()

    data = dict(row)
    source = dict(src) if src else {}
    # Fetch B站 default avatar if cover_art is empty
    up_face = (await _fetch_up_face_url(source.get("uid") or 0)
               if not data.get("cover_art") else None)

    return templates.TemplateResponse(request, "series_form.html", {
        "is_new": False, "series": series,
        "data": data, "source": source,
        "up_face_url": up_face,
        "csrf_token": csrf_token(),
    })


@router.post("/series/{series}")
async def series_update(
    request: Request,
    series: str,
    title: str = Form(""),
    description: str = Form(""),
    author: str = Form(""),
    cover_art: str = Form(""),
    category: str = Form(""),
    subcategories: str = Form(""),
    lang: str = Form("zh-CN"),
    explicit: bool = Form(False),
    space_url: str = Form(""),
    uid: int = Form(0),
    source_type: str = Form("space"),
    sid: int = Form(0),
    csrf_value: str = Form("", alias="csrf_token"),
):
    guard = _csrf_guard(request, csrf_value)
    if guard:
        return guard

    _subcats = _parse_subcategories(subcategories)

    if not title:
        return templates.TemplateResponse(request, "series_form.html", {
            "is_new": False, "series": series,
            "data": {"title": title, "description": description,
                     "author": author, "cover_art": cover_art,
                     "category": category, "lang": lang,
                     "explicit": explicit},
            "source": {"space_url": space_url,
                       "uid": uid if uid > 0 else None,
                       "type": source_type,
                       "sid": sid if sid > 0 else None},
            "errors": ["标题不能为空"],
            "csrf_token": csrf_token(),
            "up_face_url": None,
        }, status_code=400)

    with db.transaction(DB_PATH) as conn:
        # Read current series state to preserve enabled
        cur = conn.execute("SELECT enabled FROM series WHERE series=?",
                           (series,)).fetchone()
        current_enabled = bool(cur["enabled"]) if cur else True

        import json
        subcats_list = json.loads(_subcats) if _subcats else []
        from ..utils.series_config import SeriesConfig
        cfg = SeriesConfig(
            series=series, enabled=current_enabled, title=title,
            description=description, author=author,
            cover_art=cover_art, category=category,
            subcategories=subcats_list, explicit=explicit, lang=lang,
            source={"space_url": space_url, "uid": uid if uid > 0 else None,
                    "type": source_type, "sid": sid if sid > 0 else None},
            sync={}, filters={}, paid_preview={}, keep_last=100,
        )
        from .. import db as _db
        _db.upsert_series(conn, cfg)
        conn.execute(
            """INSERT INTO series_source (series, space_url, uid, type, sid)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(series) DO UPDATE SET
                   space_url=excluded.space_url, uid=excluded.uid,
                   type=excluded.type, sid=excluded.sid""",
            (series, space_url, uid if uid > 0 else None,
             source_type, sid if sid > 0 else None),
        )

    return RedirectResponse(url=f"/series/{series}", status_code=302)


# ── /series/{series}/sync — sync policy ────────────────────────────────
@router.get("/series/{series}/sync")
async def sync_policy_page(request: Request, series: str):
    redirect = _login_required(request)
    if redirect:
        return redirect

    with db.transaction(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM sync_policy WHERE series=?",
                           (series,)).fetchone()
        pp = conn.execute(
            "SELECT * FROM paid_preview_policy WHERE series=?",
            (series,),
        ).fetchone()
        if not row:
            raise HTTPException(404)

    return templates.TemplateResponse(request, "sync_policy.html", {
        "series": series,
        "data": dict(row) if row else {},
        "paid_preview": dict(pp) if pp else {},
        "csrf_token": csrf_token(),
    })


@router.post("/series/{series}/sync")
async def sync_policy_update(
    request: Request,
    series: str,
    csrf_token: str = Form(""),
    page_size: int = Form(20),
    incremental_page_size: int = Form(5),
    max_pages: int = Form(10),
    max_requests_per_series: int = Form(8),
    request_interval_seconds: float = Form(2.0),
    request_jitter_seconds: float = Form(0.5),
    rate_limit_cooldown_seconds: int = Form(21600),
    update_period: str = Form("12h"),
    update_period_grace_seconds: int = Form(120),
    format: str = Form("audio"),
    media_mode: str = Form("auto"),
    quality: str = Form("64K"),
    keep_last: int = Form(100),
    fetch_strategy: str = Form("api_first"),
    browser_fallback: bool = Form(False),
    browser_wait_min_seconds: float = Form(4.0),
    browser_wait_max_seconds: float = Form(8.0),
    browser_fallback_cooldown_seconds: int = Form(3600),
    require_paid_state_confirmation: bool = Form(False),
):
    guard = _csrf_guard(request, csrf_token)
    if guard:
        return guard

    try:
        with db.transaction(DB_PATH) as conn:
            SyncPolicyService(conn).upsert(series, {
                "page_size": page_size,
                "incremental_page_size": incremental_page_size,
                "max_pages": max_pages,
                "max_requests_per_series": max_requests_per_series,
                "request_interval_seconds": request_interval_seconds,
                "request_jitter_seconds": request_jitter_seconds,
                "rate_limit_cooldown_seconds": rate_limit_cooldown_seconds,
                "update_period": update_period,
                "update_period_grace_seconds": update_period_grace_seconds,
                "format": format,
                "media_mode": media_mode,
                "quality": quality,
                "keep_last": keep_last,
                "fetch_strategy": fetch_strategy,
                "browser_fallback": browser_fallback,
                "browser_wait_min_seconds": browser_wait_min_seconds,
                "browser_wait_max_seconds": browser_wait_max_seconds,
                "browser_fallback_cooldown_seconds": browser_fallback_cooldown_seconds,
                "require_paid_state_confirmation": require_paid_state_confirmation,
            })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/series/{series}/sync", status_code=302)


# ── /series/{series}/filters ───────────────────────────────────────────
@router.get("/series/{series}/filters")
async def filters_page(request: Request, series: str):
    redirect = _login_required(request)
    if redirect:
        return redirect

    with db.transaction(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM series WHERE series=?",
                           (series,)).fetchone()
        if not row:
            raise HTTPException(404)
        rules = conn.execute(
            "SELECT id, rule_type, value, enabled FROM filter_rule WHERE series=? ORDER BY position",
            (series,),
        ).fetchall()
        sp = conn.execute("SELECT * FROM sync_policy WHERE series=?",
                          (series,)).fetchone()
        pp = conn.execute("SELECT * FROM paid_preview_policy WHERE series=?",
                          (series,)).fetchone()

    # Split rules: enabled-only for textarea, all for rules_detail display
    all_rules = [dict(r) for r in rules]
    enabled_rules = [r for r in all_rules if r["enabled"]]

    return templates.TemplateResponse(request, "filters.html", {
        "series": series,
        "filters": build_filters_from_rows(enabled_rules),
        "rules_detail": all_rules,
        "sync": dict(sp) if sp else {},
        "paid_preview": dict(pp) if pp else {"enabled": False, "retry_after_days": 4},
        "csrf_token": csrf_token(),
    })


@router.post("/series/{series}/filters")
async def filters_update(
    request: Request,
    series: str,
    csrf_token: str = Form(""),
    exclude_paid: bool = Form(False),
    exclude_bvids: str = Form(""),
    advertisement_bvids: str = Form(""),
    exclude_keywords: str = Form(""),
    advertisement_keywords: str = Form(""),
    include_keywords: str = Form(""),
    exclude_season_ids: str = Form(""),
    paid_preview_enabled: bool = Form(False),
    retry_after_days: int = Form(4),
    min_duration_seconds: int = Form(0),
    max_duration_seconds: int = Form(0),
):
    guard = _csrf_guard(request, csrf_token)
    if guard:
        return guard

    def _parse(text: str) -> list[str]:
        return [line.strip() for line in text.split("\n") if line.strip()]

    try:
        parsed_season_ids = [int(value) for value in _parse(exclude_season_ids)]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="exclude_season_ids must contain positive integers") from exc
    if any(value <= 0 for value in parsed_season_ids):
        raise HTTPException(status_code=400, detail="exclude_season_ids must contain positive integers")

    with db.transaction(DB_PATH) as conn:
        # Update filter rules
        filters_dict: dict[str, Any] = {
            "exclude_paid": exclude_paid,
            "exclude_bvids": _parse(exclude_bvids),
            "advertisement_bvids": _parse(advertisement_bvids),
            "exclude_keywords": _parse(exclude_keywords),
            "advertisement_keywords": _parse(advertisement_keywords),
            "include_keywords": _parse(include_keywords),
            "exclude_season_ids": parsed_season_ids,
        }
        FilterRuleService(conn).replace_all(series, filters_dict)

        # Update duration & paid_preview
        SyncPolicyService(conn).update_fields(series, {
            "min_duration_seconds": min_duration_seconds,
            "max_duration_seconds": max_duration_seconds,
        })
        conn.execute(
            """INSERT INTO paid_preview_policy (series, enabled, retry_after_days)
               VALUES (?, ?, ?)
               ON CONFLICT(series) DO UPDATE SET
                   enabled=excluded.enabled, retry_after_days=excluded.retry_after_days""",
            (series, int(paid_preview_enabled), retry_after_days),
        )

    return RedirectResponse(url=f"/series/{series}/filters", status_code=302)


# ── Filter rule single operations ────────────────────────────────────
@router.post("/series/{series}/filters/toggle")
async def filter_rule_toggle(
    request: Request,
    series: str,
    rule_id: int = Form(...),
    csrf_token: str = Form(""),
):
    guard = _csrf_guard(request, csrf_token)
    if guard:
        return guard
    with db.transaction(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, rule_type, enabled FROM filter_rule WHERE id=? AND series=?",
            (rule_id, series),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        new_enabled = 0 if row["enabled"] else 1
        if row["rule_type"] == "exclude_paid":
            FilterRuleService(conn).set_exclude_paid(series, bool(new_enabled))
        else:
            conn.execute("UPDATE filter_rule SET enabled=? WHERE id=?",
                         (new_enabled, rule_id))
    return RedirectResponse(url=f"/series/{series}/filters", status_code=302)


@router.post("/series/{series}/filters/delete")
async def filter_rule_delete(
    request: Request,
    series: str,
    rule_id: int = Form(...),
    csrf_token: str = Form(""),
):
    guard = _csrf_guard(request, csrf_token)
    if guard:
        return guard
    with db.transaction(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM filter_rule WHERE id=? AND series=?",
            (rule_id, series),
        )
    return RedirectResponse(url=f"/series/{series}/filters", status_code=302)


# ── /series/{series}/cron ──────────────────────────────────────────────
@router.get("/series/{series}/cron")
async def cron_page(request: Request, series: str):
    redirect = _login_required(request)
    if redirect:
        return redirect

    svc = SchedulerService(DB_PATH)
    schedules = svc.list_schedules(series)

    # Get systemd timer status for this series
    sysd_status = None
    try:
        status_list = svc.status(backend="systemd", series=series)
        if status_list:
            sysd_status = status_list[0]
    except (NotImplementedError, OSError):
        pass

    return templates.TemplateResponse(request, "cron.html", {
        "series": series,
        "schedules": [s.schedule for s in schedules if s.kind == "primary"],
        "retry_schedules": [s.schedule for s in schedules if s.kind == "retry"],
        "sysd_status": sysd_status,
        "csrf_token": csrf_token(),
    })


@router.post("/series/{series}/cron")
async def cron_update(
    request: Request,
    series: str,
    csrf_token: str = Form(""),
    schedules: str = Form(""),
    retry_schedules: str = Form(""),
):
    guard = _csrf_guard(request, csrf_token)
    if guard:
        return guard

    parsed = [line.strip() for line in schedules.split("\n") if line.strip()]
    parsed_retries = [line.strip() for line in retry_schedules.split("\n") if line.strip()]

    try:
        SchedulerService(DB_PATH).replace_schedules(series, parsed, parsed_retries)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/series/{series}/cron", status_code=302)


# ── /series/{series}/preview — dry-run ─────────────────────────────────
@router.get("/series/{series}/preview")
async def preview_page(request: Request, series: str):
    redirect = _login_required(request)
    if redirect:
        return redirect

    data, source, sync, filters, pp, state, scheds = _load_full_config(series)
    if data is None:
        raise HTTPException(404)

    return templates.TemplateResponse(request, "preview.html", {
        "series": series,
        "data": data, "source": source, "sync": sync,
        "filters": filters, "paid_preview": pp,
        "sync_state": state, "cron_schedules": scheds,
        "csrf_token": csrf_token(), "dry_run_output": None,
    })


@router.post("/series/{series}/preview")
async def preview_run(request: Request, series: str,
                      csrf_value: str = Form("", alias="csrf_token")):
    guard = _csrf_guard(request, csrf_value)
    if guard:
        return guard

    config = _runtime_config()
    svc = PreviewService(DB_PATH, config)
    cookie_file = str(config.sync.paths.cookie_file)
    media_root = str(config.app.paths.media_root)
    json_root = str(config.app.paths.json_root)
    rss_root = str(config.app.paths.rss_root)
    lock_file = str(config.sync.paths.lock_file)
    media_base_url = config.publish.publish.media_base_url
    browser_data_root = str(config.sync.browser.user_data_root)
    log_dir = str(config.app.paths.log_dir)

    result = svc.run_preview(series,
        cookie_file=cookie_file,
        media_root=media_root, json_root=json_root, rss_root=rss_root, lock_file=lock_file,
        media_base_url=media_base_url, browser_user_data_root=browser_data_root, log_dir=log_dir)

    if result.error:
        output = result.error
    else:
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0:
            output = f"⚠️ 干跑返回码 {result.returncode}\n\n" + output

    data, source, sync, filters, pp, state, scheds = _load_full_config(series)
    return templates.TemplateResponse(request, "preview.html", {
        "series": series,
        "data": data, "source": source, "sync": sync,
        "filters": filters, "paid_preview": pp,
        "sync_state": state, "cron_schedules": scheds,
        "csrf_token": csrf_token(), "dry_run_output": output,
    })


def _load_full_config(series: str) -> tuple:
    """Load all config sections for a series using ConfigService."""
    with db.transaction(DB_PATH) as conn:
        return ConfigService(conn).load_full_config_tuple(series)


def _manual_media_redirect(series: str, *, error: str = "", success: str = "") -> RedirectResponse:
    query = ""
    if error:
        query = f"?error={quote(error)}"
    elif success:
        query = f"?success={quote(success)}"
    return RedirectResponse(url=f"/series/{series}/manual-media{query}", status_code=302)


def _require_web_series(series: str) -> None:
    if not validation.validate_slug(series):
        raise HTTPException(status_code=404, detail="series not found")
    data, *_ = _load_full_config(series)
    if data is None:
        raise HTTPException(status_code=404, detail="series not found")


# ── /jobs — sync status ────────────────────────────────────────────────
@router.get("/jobs")
async def jobs_page(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect

    with db.transaction(DB_PATH) as conn:
        states = conn.execute("""
            SELECT s.series, s.title, s.enabled,
                   sy.last_attempt_at, sy.last_success_at,
                   sy.rate_limited_until
            FROM series s
            LEFT JOIN sync_state sy ON sy.series = s.series
            ORDER BY sy.last_attempt_at DESC, s.series
        """).fetchall()

    # Read last lines of error log
    log_dir = str(_runtime_config().app.paths.log_dir)
    error_log = ""
    if log_dir:
        error_path = Path(log_dir) / "sync.error.log"
        if error_path.exists():
            try:
                lines = error_path.read_text(encoding="utf-8").split("\n")
                error_log = "\n".join(lines[-50:])
            except OSError:
                error_log = "(无法读取日志)"

    return templates.TemplateResponse(request, "jobs.html", {
        "states": [dict(s) for s in states],
        "error_log": error_log,
    })


# ── /series/{series}/manual-media — read-only manual media info ─────
@router.get("/series/{series}/manual-media")
async def manual_media_page(request: Request, series: str,
                            error: str = "", success: str = ""):
    redirect = _login_required(request)
    if redirect:
        return redirect

    with db.transaction(DB_PATH) as conn:
        row = conn.execute(
            "SELECT quality FROM sync_policy WHERE series=?", (series,)
        ).fetchone()
    quality = row["quality"] if row and row["quality"] else "64K"

    config = _runtime_config()
    json_dir = config.app.paths.json_root / series
    media_dir = config.app.paths.media_root / series

    json_files = list(json_dir.glob("*.info.json")) if json_dir.exists() else []
    total_meta = len(json_files)

    missing = []
    import json
    for f in sorted(json_files, key=lambda p: p.stem):
        bvid = f.stem.split("_")[0]
        media_file = media_dir / f"{bvid}_{quality}.mp3"
        if not media_file.exists():
            title = bvid
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                title = meta.get("title") or bvid
            except (json.JSONDecodeError, OSError):
                pass
            missing.append({"bvid": bvid, "title": title})

    allowed_dirs = [str(d) for d in _get_allowed_media_dirs()]

    return templates.TemplateResponse(request, "manual_media.html", {
        "series": series,
        "quality": quality,
        "total_meta": total_meta,
        "missing_count": len(missing),
        "missing": missing[:50],
        "error": error,
        "success": success,
        "allowed_dirs": allowed_dirs,
        "csrf_token": csrf_token(),
    })


# ── /series/{series}/manual-media — attach form POST ──────────────────
@router.post("/series/{series}/manual-media")
async def manual_media_attach(
    request: Request,
    series: str,
    csrf_token: str = Form(""),
    bvid: str = Form(""),
    server_path: str = Form(""),
    replace: str = Form(""),
):
    guard = _csrf_guard(request, csrf_token)
    if guard:
        return guard

    _require_web_series(series)

    if not validation.validate_bvid(bvid):
        return _manual_media_redirect(series, error="invalid bvid")

    src = Path(server_path)
    if not is_allowed_manual_media_path(src):
        return _manual_media_redirect(series, error="path not in whitelist")

    resolved = src.resolve()
    if not resolved.exists():
        return _manual_media_redirect(series, error="file not found")

    if resolved.suffix.lower() != ".mp3":
        return _manual_media_redirect(series, error="only .mp3 supported")

    quality = _get_series_quality(DB_PATH, series)
    dst_name = f"{bvid}_{quality}.mp3"
    config = _runtime_config()
    media_root = config.app.paths.media_root
    json_root = config.app.paths.json_root
    rss_root = config.app.paths.rss_root
    media_base_url = config.publish.publish.media_base_url
    dst = media_root / series / dst_name
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() and replace != "1":
        return _manual_media_redirect(series, error="target exists; use replace")

    shutil.copy2(str(resolved), str(dst))
    dst.chmod(0o644)

    try:
        rebuild_paid_rss(
            DB_PATH,
            series,
            json_root=json_root,
            media_root=media_root,
            rss_root=rss_root,
            media_base_url=media_base_url,
        )
    except ValueError as exc:
        return _manual_media_redirect(series, error=f"rss rebuild failed: {exc}")

    publish_script = (
        _CONFIG_SNAPSHOT.publish.publish.script
        if _CONFIG_SNAPSHOT and _CONFIG_SNAPSHOT.publish.publish.enabled
        else None
    )
    if publish_script is not None:
        try:
            subprocess.run([str(publish_script)], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            return _manual_media_redirect(series, error=f"rss publish failed: {exc}")

    return _manual_media_redirect(series, success="media attached and RSS updated")


# ── /scheduler — web scheduler status ───────────────────────────────
@router.get("/scheduler")
async def scheduler_page(request: Request):
    redirect = _login_required(request)
    if redirect:
        return redirect

    svc = SchedulerService(DB_PATH)
    try:
        cron_status = svc.status(backend="cron")
    except NotImplementedError:
        cron_status = []
    try:
        sysd_status = svc.status(backend="systemd")
    except NotImplementedError:
        sysd_status = []

    return templates.TemplateResponse(request, "scheduler.html", {
        "cron_status": cron_status,
        "sysd_status": sysd_status,
        "csrf_token": csrf_token(),
    })


def create_app(
    snapshot: ConfigSnapshot | None = None,
    *,
    manager: ConfigManager | None = None,
) -> FastAPI:
    """Configure and return the ASGI app from one immutable snapshot."""
    global DB_PATH, PASSWORD, _COOKIE_NAME, _SESSION_MAX_AGE, _HTTPS
    global _SECRET_KEY, _serializer, _CONFIG_MANAGER, _CONFIG_SNAPSHOT

    if snapshot is not None:
        selected_manager = manager or ConfigManager(snapshot.root, environ={})
        selected = snapshot
    else:
        selected_manager = manager or ConfigManager()
        selected = selected_manager.load()
    if not selected.web.server.enabled:
        raise RuntimeError("web.server.enabled is false")
    if not selected.web.security.password:
        raise RuntimeError("web.security.password is required")
    DB_PATH = str(selected.app.database.path)
    PASSWORD = selected.web.security.password
    _COOKIE_NAME = selected.web.security.cookie_name
    _SESSION_MAX_AGE = selected.web.security.session_max_age_seconds
    _HTTPS = selected.web.security.https
    _SECRET_KEY = hashlib.sha256(PASSWORD.encode()).hexdigest()
    _serializer = URLSafeTimedSerializer(_SECRET_KEY)
    _CONFIG_MANAGER = selected_manager
    _CONFIG_SNAPSHOT = selected
    _cli_admin._CONFIG = selected
    from ..services import systemd_scheduler
    systemd_scheduler.configure(selected)
    configured_app = FastAPI(title="Bilipod Manager")
    configured_app.include_router(router)
    configured_app.state.config = selected
    return configured_app
