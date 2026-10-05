"""SQLite persistence for site crawls. One connection per call site; WAL lets the API read
while a crawl writes. JSON columns hold nested data that is only ever read back whole."""
import json
import os
import sqlite3
from datetime import datetime, timezone

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_url TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL,              -- queued | running | completed | failed
    options TEXT NOT NULL,             -- JSON CrawlOptions
    stats TEXT,                        -- JSON diagnostics
    robots TEXT,                       -- JSON {status, sitemaps}
    sitemap_urls TEXT,                 -- JSON list as found in sitemaps (duplicates kept)
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS crawl_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_id INTEGER NOT NULL REFERENCES crawls(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    crawl_status TEXT NOT NULL,        -- ok | redirect | duplicate | blocked_robots | non_html | error
    status_code INTEGER,
    content_type TEXT,
    depth INTEGER,                     -- link depth from root; NULL = only reachable via sitemap
    parent_url TEXT,
    discovered_via TEXT NOT NULL,      -- root | link | sitemap
    in_sitemap INTEGER NOT NULL DEFAULT 0,
    redirect_target TEXT,
    redirect_chain TEXT,               -- JSON list
    canonical_url TEXT,
    robots_directives TEXT,            -- JSON {meta, x_robots_tag}
    title TEXT,
    meta_description TEXT,
    h1 TEXT,
    word_count INTEGER,
    language TEXT,
    crawl_time_ms INTEGER,
    render_required INTEGER,
    render_status TEXT,
    render_info TEXT,                  -- JSON CrawlInfo
    internal_links_in INTEGER NOT NULL DEFAULT 0,
    internal_links_out INTEGER NOT NULL DEFAULT 0,
    external_links_out INTEGER NOT NULL DEFAULT 0,
    audit TEXT,                        -- JSON Audit from the on-page linter
    error TEXT,
    crawled_at TEXT NOT NULL,
    UNIQUE (crawl_id, normalized_url)
);
CREATE TABLE IF NOT EXISTS page_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_id INTEGER NOT NULL REFERENCES crawls(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,          -- normalized
    target_url TEXT NOT NULL,          -- normalized for internal, absolute for external
    anchor_text TEXT,
    internal INTEGER NOT NULL,
    nofollow INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_links_crawl_target ON page_links (crawl_id, target_url);
CREATE INDEX IF NOT EXISTS ix_links_crawl_source ON page_links (crawl_id, source_url);
"""

# Columns added after the first release: (table, column, type). Applied idempotently on connect.
MIGRATIONS = [
    ("crawls", "site_issues", "TEXT"),           # JSON list[Issue]: sitemap / indexability findings
    ("crawl_pages", "indexability", "TEXT"),     # JSON per-page states with reasons
]

JSON_COLS = {"options", "stats", "robots", "sitemap_urls", "redirect_chain", "robots_directives",
             "render_info", "audit", "site_issues", "indexability"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn


def _dump(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def row_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in JSON_COLS & d.keys():
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d


def insert(conn: sqlite3.Connection, table: str, values: dict) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" * len(values))
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", [_dump(v) for v in values.values()])
    return cur.lastrowid


def update(conn: sqlite3.Connection, table: str, row_id: int, values: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*(_dump(v) for v in values.values()), row_id])
