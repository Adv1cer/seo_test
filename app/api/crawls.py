from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from app.models.request import CrawlOptions
from app.services import store
from app.services.crawler import create_crawl, run_crawl

router = APIRouter(prefix="/api/seo/crawls", tags=["crawls"])

_PAGE_LIST_COLS = ("id, url, normalized_url, crawl_status, status_code, content_type, depth, parent_url, "
                   "discovered_via, in_sitemap, redirect_target, canonical_url, robots_directives, title, "
                   "meta_description, h1, word_count, language, crawl_time_ms, render_required, "
                   "render_status, internal_links_in, internal_links_out, external_links_out, error, "
                   "json_extract(audit, '$.score') AS audit_score, indexability, crawled_at")


def _crawl_or_404(conn, crawl_id: int) -> dict:
    row = conn.execute("SELECT * FROM crawls WHERE id = ?", (crawl_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Crawl {crawl_id} not found")
    d = store.row_dict(row)
    d.pop("sitemap_urls", None)  # can be huge; exposed via the pages endpoint (in_sitemap)
    d.pop("site_issues", None)   # served by /issues
    return d


@router.post("", status_code=202)
async def start(opts: CrawlOptions, background: BackgroundTasks):
    """Queue a site crawl. Poll GET /api/seo/crawls/{id} until status is completed or failed."""
    conn = store.connect()
    crawl_id = create_crawl(conn, opts)
    conn.close()
    background.add_task(run_crawl, crawl_id, opts)
    return {"success": True, "data": {"crawl_id": crawl_id, "status": "queued"}}


@router.get("/{crawl_id}")
def get_crawl(crawl_id: int):
    conn = store.connect()
    return {"success": True, "data": _crawl_or_404(conn, crawl_id)}


@router.get("/{crawl_id}/pages")
def list_pages(crawl_id: int, crawl_status: str | None = None, indexable: bool | None = None, limit: int = Query(100, ge=1, le=1000),
               offset: int = Query(0, ge=0), include_audit: bool = False):
    conn = store.connect()
    _crawl_or_404(conn, crawl_id)
    cols = _PAGE_LIST_COLS + (", audit, render_info" if include_audit else "")
    where, args = "crawl_id = ?", [crawl_id]
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
    rows = conn.execute(f"SELECT source_url, target_url, anchor_text, internal, nofollow FROM page_links "
                        f"WHERE {where} ORDER BY id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return {"success": True, "data": {"total": total, "links": [dict(r) for r in rows]}}


@router.get("/{crawl_id}/issues")
def list_issues(crawl_id: int):
    """Site-level sitemap and indexability findings, plus the per-state page counts."""
    conn = store.connect()
    crawl = _crawl_or_404(conn, crawl_id)
    row = conn.execute("SELECT site_issues FROM crawls WHERE id = ?", (crawl_id,)).fetchone()
    issues = store.row_dict(row)["site_issues"]
    return {"success": True, "data": {"status": crawl["status"],
                                      "indexability": (crawl["stats"] or {}).get("indexability"),
                                      "issues": issues if issues is not None else []}}
