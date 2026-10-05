"""Regression tests: sitemap reading is independent of max_pages, completeness is tracked, missing-from-
sitemap is only concluded on a complete read, crawl coverage, and page-audit aggregation."""
import asyncio

import httpx
import pytest

from app.config import settings
from app.models.request import CrawlOptions
from app.services import crawler, fetcher, store
from app.services.page_audit_summary import summarize
from app.services.sitemap import collect_sitemap_urls

B = "https://ex.com"
NS = "xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'"


def urlset(paths):
    return f"<urlset {NS}>" + "".join(f"<url><loc>{B}{p}</loc></url>" for p in paths) + "</urlset>"


def index(names):
    return f"<sitemapindex {NS}>" + "".join(f"<sitemap><loc>{B}/{n}</loc></sitemap>" for n in names) + "</sitemapindex>"


def page(title, links=(), desc="A perfectly reasonable meta description that is long enough to pass the check ok."):
    a = "".join(f"<a href='{h}'>x</a>" for h in links)
    return (f"<html lang='en'><head><title>{title}</title><meta name='description' content='{desc}'></head>"
            f"<body><main><h1>{title}</h1><p>{'word ' * 400}</p>{a}</main></body></html>")


# WordPress-like: index -> 3 big post sitemaps, then a nested index -> page sitemap with the real pages.
POSTS = [[f"/post-{s}-{i}" for i in range(100)] for s in range(3)]
SITE = {
    "/robots.txt": f"User-agent: *\nSitemap: {B}/sitemap.xml\n",
    "/sitemap.xml": index(["post-sitemap.xml", "post-sitemap2.xml", "post-sitemap3.xml", "nested-index.xml"]),
    "/post-sitemap.xml": urlset(POSTS[0] + ["/post-1-5"]),  # duplicate of a URL in sitemap2
    "/post-sitemap2.xml": urlset(POSTS[1]),
    "/post-sitemap3.xml": urlset(POSTS[2]),
    "/nested-index.xml": index(["page-sitemap.xml"]),
    "/page-sitemap.xml": urlset(["/", "/about", "/admission"]),
    "/": page("Home page of the example university site", ["/about", "/admission", "/faculty"]),
    "/about": page("About", ["/"]),
    "/admission": page("About", ["/"]),  # duplicate (short) title
    "/faculty": page("Faculty of things and stuff at the example university", ["/"]),
}


def handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path not in SITE:
        if path.startswith("/post-"):
            return httpx.Response(200, html=page(f"Post {path} with a long enough descriptive title"))
        return httpx.Response(404, html=page("Not found"))
    ctype = "application/xml" if path.endswith(".xml") else "text/plain" if path.endswith(".txt") else "text/html"
    return httpx.Response(200, text=SITE[path], headers={"content-type": ctype})


def collect(h=handler, max_urls=200_000, max_files=1000):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return await collect_sitemap_urls(c, [B + "/sitemap.xml"], max_urls, max_files)
    return asyncio.run(go())


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: (page("R"), 1))

    def _run(h=handler, **opts):
        conn = store.connect(":memory:")
        o = CrawlOptions(url=B + "/", render_javascript=False, **opts)
        cid = crawler.create_crawl(conn, o)
        asyncio.run(crawler.run_crawl(cid, o, conn, httpx.MockTransport(h)))
        crawl = store.row_dict(conn.execute("SELECT * FROM crawls WHERE id=?", (cid,)).fetchone())
        return crawl, {i["code"]: i for i in crawl["site_issues"]}
    return _run


def test_index_with_children_and_nested_index_fully_read():
    urls, files, meta = collect()
    assert len(files) == 6 and meta["discovered_sitemaps"] == 6
    assert B + "/admission" in urls                   # lives in the last, nested child sitemap
    assert meta["discovered_urls"] == len(urls) == 303  # 300 posts + 3 pages, duplicate removed
    assert len(set(urls)) == len(urls)
    assert meta["read_complete"] is True and meta["stop_reason"] is None
    assert meta["duplicate_urls"] == 1 and meta["duplicate_examples"] == [B + "/post-1-5"]


def test_safety_limit_marks_read_incomplete():
    _, _, meta = collect(max_urls=150)
    assert meta["read_complete"] is False and meta["stop_reason"] == "sitemap_safety_limit"
    assert meta["discovered_urls"] == 150
    _, _, meta = collect(max_files=2)
    assert meta["read_complete"] is False and meta["stop_reason"] == "sitemap_safety_limit"


def test_failed_child_sitemap_marks_read_incomplete():
    def broken(req):
        return httpx.Response(500) if req.url.path == "/page-sitemap.xml" else handler(req)
    _, _, meta = collect(broken)
    assert meta["read_complete"] is False and meta["stop_reason"] == "sitemap_http_error"
    assert meta["status"] == "partial"
    assert meta["failed_sitemaps"] == [{"url": B + "/page-sitemap.xml", "result": "http_500"}]


def test_sitemap_not_limited_by_max_pages(run):
    """Regression: max_pages=30 used to cap sitemap reading at 150 URLs, so page-sitemap.xml was never
    read and every crawled page was falsely reported as missing from the sitemap."""
    crawl, issues = run(max_pages=30)
    sm = crawl["stats"]["sitemap"]
    assert sm["read_complete"] is True and sm["discovered_urls"] == 303
    assert "PAGES_MISSING_FROM_SITEMAP" in issues  # /faculty genuinely is not in any sitemap...
    assert issues["PAGES_MISSING_FROM_SITEMAP"]["value"] == [B + "/faculty"]  # ...and only it
    assert "SITEMAP_ANALYSIS_INCOMPLETE" not in issues


def test_incomplete_sitemap_suppresses_missing_finding(run, monkeypatch):
    monkeypatch.setattr(settings, "sitemap_max_urls", 150)
    crawl, issues = run(max_pages=30)
    assert crawl["stats"]["sitemap"]["read_complete"] is False
    assert "PAGES_MISSING_FROM_SITEMAP" not in issues
    assert "not evaluated" in issues["SITEMAP_ANALYSIS_INCOMPLETE"]["message"]
    assert crawl["stats"]["crawl"]["stop_reason"] == "max_pages_reached"


def test_coverage_partial_and_complete(run):
    crawl, _ = run(max_pages=30)
    cov = crawl["stats"]["crawl"]
    assert cov["requested_limit"] == 30 and cov["crawled_urls"] == 30
    assert cov["discovered_urls"] == 304  # 303 sitemap URLs + /faculty from links
    assert cov["complete"] is False and cov["stop_reason"] == "max_pages_reached"
    assert 0 < cov["coverage_percent"] < 100

    crawl, _ = run(max_pages=1000)
    cov = crawl["stats"]["crawl"]
    assert cov["complete"] is True and cov["stop_reason"] is None and cov["coverage_percent"] == 100


def test_page_audit_aggregation(run):
    crawl, _ = run(max_pages=1000)
    s = crawl["stats"]["page_audit_summary"]
    assert s["total_pages"] == 304
    by = {i["issue"]: i for i in s["issues"]}
    short = by["TITLE_TOO_SHORT"]
    assert short["count"] == 2 and short["total_pages"] == 304 and short["severity"] == "warning"
    assert short["percentage"] == round(200 / 304, 2)
    assert {e["url"] for e in short["examples"]} == {"/about", "/admission"}
    assert by["DUPLICATE_TITLE"]["count"] == 2 and by["DUPLICATE_TITLE"]["examples"][0]["shared_with"] == 1
    assert by["DUPLICATE_META_DESCRIPTION"]["count"] == 304
    assert len(by["DUPLICATE_META_DESCRIPTION"]["examples"]) == 5  # examples are capped
    assert "TITLE_MISSING" not in by and not any(i["severity"] == "passed" for i in s["issues"])


def test_summarize_ignores_passed_and_counts_only_given_pages():
    audit = {"issues": [{"code": "H1_MISSING", "severity": "warning", "category": "headings",
                         "message": "No H1 heading found.", "value": None},
                        {"code": "METADATA_OK", "severity": "passed", "category": "metadata", "message": "ok"}]}
    s = summarize([{"url": B + "/a", "title": "A", "meta_description": None, "audit": audit},
                   {"url": B + "/b", "title": "B", "meta_description": None, "audit": {"issues": []}}])
    assert s["total_pages"] == 2
    assert [(i["issue"], i["count"], i["percentage"]) for i in s["issues"]] == [("H1_MISSING", 1, 50.0)]


def test_wait_response_exposes_scope_and_page_issues(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    real = crawler.run_crawl

    async def fake_run(cid, o):
        await real(cid, o, transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.api.crawls.run_crawl", fake_run)
    client = TestClient(app)
    d = client.post("/api/seo/crawls?wait=true",
                             json={"url": B + "/", "max_pages": 10, "render_javascript": False}).json()["data"]
    assert d["audit_scope"] == "partial" and d["crawl"]["complete"] is False
    assert "audited 10 URLs within the configured crawl limit" in d["scope_statement"]
    assert "294 of 304 discovered URLs not crawled" in d["scope_statement"]
    assert d["counts"]["html_pages_ok"] == d["pages_crawled"] == 10
    assert d["sitemap"]["discovered_urls"] == 303
    assert any("NEVER generalize" in r for r in d["reporting_rules"])
    assert d["page_issues"]["total_pages"] == 10
    short = next(i for i in d["page_issues"]["issues"] if i["issue"] == "TITLE_TOO_SHORT")
    drill = client.get(f"/api/seo/crawls/{d['crawl_id']}/{short['drilldown']}").json()["data"]
    assert drill["total"] == short["count"] == 2  # every affected URL, not just the examples
