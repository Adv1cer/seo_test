import asyncio
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response

from app.models.request import CrawlOptions
from app.services import store
from app.services.crawler import create_crawl, run_crawl
from app.services.report_context import report_context

router = APIRouter(prefix="/api/seo/crawls", tags=["crawls"])

_PAGE_LIST_COLS = ("id, url, normalized_url, crawl_status, status_code, content_type, depth, parent_url, "
                   "discovered_via, in_sitemap, redirect_target, canonical_url, robots_directives, title, "
                   "meta_description, h1, word_count, language, crawl_time_ms, render_required, "
                   "render_status, internal_links_in, internal_links_out, external_links_out, error, "
                   "json_extract(audit, '$.score') AS audit_score, indexability, link_metrics, crawled_at")


def _crawl_or_404(conn, crawl_id: int) -> dict:
    row = conn.execute("SELECT * FROM crawls WHERE id = ?", (crawl_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Crawl {crawl_id} not found")
    d = store.row_dict(row)
    d.pop("sitemap_urls", None)  # can be huge; exposed via the pages endpoint (in_sitemap)
    d.pop("site_issues", None)   # served by /issues
    return d


_RUNNING: set[asyncio.Task] = set()  # strong refs so detached crawls are not garbage-collected
_FINISHED = ("completed", "failed")


def _progress(conn, crawl_id: int) -> dict:
    """Not-finished response: lets a workflow poll GET /{id}/result until done is true."""
    crawl = _crawl_or_404(conn, crawl_id)
    recorded = conn.execute("SELECT COUNT(*) FROM crawl_pages WHERE crawl_id = ?", (crawl_id,)).fetchone()[0]
    return {"success": True, "data": {
        "done": False, "crawl_id": crawl_id, "status": crawl["status"], "root_url": crawl["root_url"],
        "urls_crawled_so_far": recorded, "max_pages": (crawl["options"] or {}).get("max_pages"),
        "poll_url": f"/api/seo/crawls/{crawl_id}/result"}}


@router.post("", status_code=202)
async def start(opts: CrawlOptions, background: BackgroundTasks, response: Response, wait: bool = False,
                max_wait: float = Query(100, gt=0, le=3600)):
    """Queue a site crawl. Without ?wait, returns crawl_id immediately. With ?wait=true, waits up to max_wait
    seconds: if the crawl finishes in time the full result is returned (done=true); otherwise done=false and
    the crawl keeps running. Poll GET /api/seo/crawls/{id}/result until done is true."""
    conn = store.connect()
    crawl_id = create_crawl(conn, opts)
    if not wait:
        conn.close()
        background.add_task(run_crawl, crawl_id, opts)
        return {"success": True, "data": {"done": False, "crawl_id": crawl_id, "status": "queued",
                                          "poll_url": f"/api/seo/crawls/{crawl_id}/result"}}
    task = asyncio.create_task(run_crawl(crawl_id, opts))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    try:
        await asyncio.wait_for(asyncio.shield(task), max_wait)
    except asyncio.TimeoutError:
        return _progress(conn, crawl_id)  # 202: still running
    response.status_code = 200
    return _result(conn, crawl_id)


@router.get("/{crawl_id}/result")
async def result(crawl_id: int, response: Response, wait: float = Query(0, ge=0, le=3600)):
    """Poll endpoint for workflows. done=false (HTTP 202) while running; done=true (HTTP 200) with the same
    full payload as POST ?wait=true once finished. ?wait=N long-polls up to N seconds before answering."""
    conn = store.connect()
    deadline = time.monotonic() + wait
    while True:
        status = _crawl_or_404(conn, crawl_id)["status"]
        if status in _FINISHED:
            return _result(conn, crawl_id)
        if time.monotonic() >= deadline:
            response.status_code = 202
            return _progress(conn, crawl_id)
        await asyncio.sleep(1)


def _result(conn, crawl_id: int) -> dict:
    crawl = _crawl_or_404(conn, crawl_id)
    issues = store.row_dict(conn.execute("SELECT site_issues FROM crawls WHERE id = ?", (crawl_id,)).fetchone())
    stats = crawl.get("stats") or {}
    return {"success": crawl["status"] == "completed", "data": {
        "done": True,
        "crawl_id": crawl_id, "status": crawl["status"], "error": crawl["error"], "root_url": crawl["root_url"],
        # pages_crawled = successfully fetched HTML pages (was: any fetched HTML response, incl. 404s)
        "pages_crawled": (stats.get("counts") or {}).get("html_pages_ok", stats.get("crawled_ok")),
        "duration_ms": stats.get("duration_ms"),
        "indexability": stats.get("indexability"), "architecture": stats.get("architecture"),
        "issues": issues["site_issues"] or [],
        "top_pages": architecture(crawl_id, limit=10)["data"]["top_authority_pages"],
        **report_context(stats, issues["site_issues"] or []),
    }}


@router.get("/{crawl_id}")
def get_crawl(crawl_id: int):
    conn = store.connect()
    return {"success": True, "data": _crawl_or_404(conn, crawl_id)}


@router.get("/{crawl_id}/pages")
def list_pages(crawl_id: int, crawl_status: str | None = None, indexable: bool | None = None, limit: int = Query(100, ge=1, le=1000),
               offset: int = Query(0, ge=0), include_audit: bool = False, issue: str | None = None):
    """issue=CODE drills down from a page_issues rollup to every page whose audit has that issue."""
    conn = store.connect()
    _crawl_or_404(conn, crawl_id)
    cols = _PAGE_LIST_COLS + (", audit, render_info" if include_audit else "")
    where, args = "crawl_id = ?", [crawl_id]
    if issue:
        where += (" AND EXISTS (SELECT 1 FROM json_each(audit, '$.issues') "
                  "WHERE json_extract(value, '$.code') = ?)")
        args.append(issue)
    if crawl_status:
        where += " AND crawl_status = ?"
        args.append(crawl_status)
    if indexable is not None:
        where += " AND json_extract(indexability, '$.indexable.value') = ?"
        args.append(int(indexable))
    total = conn.execute(f"SELECT COUNT(*) FROM crawl_pages WHERE {where}", args).fetchone()[0]
    rows = conn.execute(f"SELECT {cols} FROM crawl_pages WHERE {where} ORDER BY depth IS NULL, depth, id "
                        "LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return {"success": True, "data": {"total": total, "limit": limit, "offset": offset,
                                      "pages": [store.row_dict(r) for r in rows]}}


@router.get("/{crawl_id}/links")
def list_links(crawl_id: int, internal: bool | None = None, limit: int = Query(500, ge=1, le=5000),
               offset: int = Query(0, ge=0)):
    conn = store.connect()
    _crawl_or_404(conn, crawl_id)
    where, args = "crawl_id = ?", [crawl_id]
    if internal is not None:
        where += " AND internal = ?"
        args.append(int(internal))
    total = conn.execute(f"SELECT COUNT(*) FROM page_links WHERE {where}", args).fetchone()[0]
    rows = conn.execute(f"SELECT source_url, target_url, anchor_text, internal, nofollow, location FROM page_links "
                        f"WHERE {where} ORDER BY id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return {"success": True, "data": {"total": total, "links": [dict(r) for r in rows]}}


@router.get("/{crawl_id}/issues")
def list_issues(crawl_id: int):
    """Site-level sitemap and indexability findings, plus the per-state page counts."""
    conn = store.connect()
    crawl = _crawl_or_404(conn, crawl_id)
    row = conn.execute("SELECT site_issues FROM crawls WHERE id = ?", (crawl_id,)).fetchone()
    issues = store.row_dict(row)["site_issues"]
    stats = crawl["stats"] or {}
    return {"success": True, "data": {"status": crawl["status"], "indexability": stats.get("indexability"),
                                      "issues": issues if issues is not None else [],
                                      **report_context(stats, issues or [])}}


_ARCH_FLAGS = ("orphan_candidate", "dead_end", "weak_internal_linking", "deep", "important_but_deep")


@router.get("/{crawl_id}/architecture")
def architecture(crawl_id: int, limit: int = Query(50, ge=1, le=1000)):
    """Link-graph summary, top pages by internal authority, flagged pages and architecture issues."""
    conn = store.connect()
    crawl = _crawl_or_404(conn, crawl_id)
    rows = [store.row_dict(r) for r in conn.execute(
        "SELECT url, depth, in_sitemap, link_metrics FROM crawl_pages WHERE crawl_id = ? AND link_metrics IS NOT NULL",
        (crawl_id,))]
    pages = [{"url": r["url"], "depth": r["depth"], "in_sitemap": bool(r["in_sitemap"]), **r["link_metrics"]}
             for r in rows]
    pages.sort(key=lambda p: -p["authority"])
    issues = store.row_dict(conn.execute("SELECT site_issues FROM crawls WHERE id = ?", (crawl_id,)).fetchone())
    return {"success": True, "data": {
        "status": crawl["status"],
        "summary": (crawl["stats"] or {}).get("architecture"),
        "top_authority_pages": pages[:limit],
        "flagged": {f: [p for p in pages if f in p["flags"]][:limit] for f in _ARCH_FLAGS},
        "issues": [i for i in issues["site_issues"] or [] if i["category"] == "architecture"],
    }}
