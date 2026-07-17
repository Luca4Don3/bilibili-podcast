-- Immutable SQLite schema snapshot for unified installation v3.
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE series (
  series TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL, cover_art TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '', subcategories TEXT NOT NULL DEFAULT '[]',
  explicit INTEGER NOT NULL DEFAULT 0, lang TEXT NOT NULL DEFAULT 'zh-CN'
);
CREATE TABLE series_source (
  series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
  space_url TEXT NOT NULL DEFAULT '', uid INTEGER,
  type TEXT NOT NULL DEFAULT 'space', sid INTEGER
);
CREATE TABLE sync_policy (
  series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
  page_size INTEGER NOT NULL DEFAULT 20, incremental_page_size INTEGER NOT NULL DEFAULT 5,
  max_pages INTEGER NOT NULL DEFAULT 10, max_requests_per_series INTEGER NOT NULL DEFAULT 8,
  request_interval_seconds REAL NOT NULL DEFAULT 2.0,
  request_jitter_seconds REAL NOT NULL DEFAULT 0.5,
  rate_limit_cooldown_seconds INTEGER NOT NULL DEFAULT 21600,
  update_period TEXT NOT NULL DEFAULT '12h',
  update_period_grace_seconds INTEGER NOT NULL DEFAULT 120,
  format TEXT NOT NULL DEFAULT 'audio', media_mode TEXT NOT NULL DEFAULT 'auto',
  quality TEXT NOT NULL DEFAULT '64K', fetch_strategy TEXT NOT NULL DEFAULT 'api_first',
  keep_last INTEGER NOT NULL DEFAULT 100, browser_fallback INTEGER NOT NULL DEFAULT 0,
  browser_wait_min_seconds REAL NOT NULL DEFAULT 4.0,
  browser_wait_max_seconds REAL NOT NULL DEFAULT 8.0,
  browser_fallback_cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
  require_paid_state_confirmation INTEGER NOT NULL DEFAULT 0,
  min_duration_seconds INTEGER NOT NULL DEFAULT 0, max_duration_seconds INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE filter_rule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series TEXT NOT NULL REFERENCES series(series) ON DELETE CASCADE,
  rule_type TEXT NOT NULL, value TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE paid_preview_policy (
  series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 0, retry_after_days INTEGER NOT NULL DEFAULT 4
);
CREATE TABLE cron_schedule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series TEXT NOT NULL REFERENCES series(series) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 1, schedule TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'primary'
);
CREATE TABLE access_rule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series TEXT NOT NULL REFERENCES series(series) ON DELETE CASCADE,
  allowed_user TEXT NOT NULL
);
CREATE TABLE sync_state (
  series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
  last_attempt_at INTEGER NOT NULL DEFAULT 0, last_success_at INTEGER NOT NULL DEFAULT 0,
  last_browser_fallback_at INTEGER NOT NULL DEFAULT 0, rate_limited_until INTEGER NOT NULL DEFAULT 0,
  retry_pending INTEGER NOT NULL DEFAULT 0
);
