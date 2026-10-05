"""Correctness regressions modelled on the ai.utcc.ac.th crawl: SPA with HTML fallback for robots.txt and
sitemap.xml, canonicals/hreflang to a private IP, client-rendered content, and percent-encoded image links
that 404. Plus sitemap status classification and crawl-scope classification."""
import asyncio

import httpx
import pytest

from app.models.request import CrawlOptions
from app.services import crawler, fetcher, store
from app.services.sitemap import classify_sitemap, collect_sitemap_urls
from app.services.url_utils import crawl_key, is_asset_url

B = "https://ai.ex.com"
PAGES = ["/en", "/en/news", "/en/news/story", "/en/contact"]
SHELL = ("<!doctype html><html lang='en'><head><title>App</title>{head}"
         "<script src='/react-dom.production.min.js'></script></head><body><div id='root'></div></body></html>")
IMG = "/%77%70%2d%63%6f%6e%74%65%6e%74/uploads/a.jpg"  # "wp-content" percent-encoded


def head(path):
    return (f"<link rel='canonical' href='http://10.7.45.121{path}'>"
            f"<link rel='alternate' hreflang='en' href='http://10.7.45.121{path}'>")


def rendered(url):
    path = url.removeprefix(B)
    links = "".join(f"<a href='{p}'>{p}</a>" for p in PAGES)
    if path == "/en/news/story":
        links += f"<a href='{IMG}'>img</a><a href='/wp-content/uploads/a.jpg'>same img</a>"
    return (f"<html lang='en'><head><title>Story {path} at the example AI site</title>{head(path)}</head>"
            f"<body><main><h1>{path}</h1><p>{'word ' * 400}</p>{links}</main></body></html>")


def handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path.endswith(".jpg"):
        return httpx.Response(404, html="<html><body>not found</body></html>")
    if path in PAGES or path in ("/robots.txt", "/sitemap.xml"):  # SPA serves its shell for everything
        return httpx.Response(200, html=SHELL.format(head=head(path)))
    return httpx.Response(404, html="<html><body>nope</body></html>")


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: (rendered(url), 5))

    def _run(**opts):
        conn = store.connect(":memory:")
        o = CrawlOptions(url=B + "/en", **opts)
        cid = crawler.create_crawl(conn, o)
        asyncio.run(crawler.run_crawl(cid, o, conn, httpx.MockTransport(handler)))
        c = store.row_dict(conn.execute("SELECT * FROM crawls WHERE id=?", (cid,)).fetchone())
        pages = {r["normalized_url"].removeprefix(B): store.row_dict(r)
                 for r in conn.execute("SELECT * FROM crawl_pages WHERE crawl_id=?", (cid,))}
        return c, pages, {i["code"]: i for i in c["site_issues"]}
    return _run


def test_url_normalization_and_assets():
    assert crawl_key(B + IMG) == crawl_key(B + "/wp-content/uploads/a.jpg")
    assert crawl_key(B + "/a%2fb") == B + "/a%2Fb"  # reserved escapes stay encoded
    assert is_asset_url(B + IMG) and is_asset_url(B + "/f.PDF?x=1")
    assert not is_asset_url(B + "/en/news") and not is_asset_url(B + "/v1.2/")


def test_denominators_and_broken_assets(run):
    c, pages, issues = run()
    counts = c["stats"]["counts"]
    assert counts["html_pages_ok"] == 4 and counts["assets_broken"] == 1  # encoded + decoded = one URL
    assert counts["html_error_pages"] == 0 and counts["broken_urls"] == 1
    assert c["stats"]["page_audit_summary"]["total_pages"] == 4
    assert "BROKEN_RESOURCE_LINKS" in issues and issues["BROKEN_RESOURCE_LINKS"]["severity"] == "warning"
    assert "BROKEN_INTERNAL_LINKS" not in issues and "BROKEN_PAGES" not in issues
    img = pages["/wp-content/uploads/a.jpg"]["indexability"]
    assert img["resource"] == "asset" and img["indexable"]["value"] is None
    assert c["stats"]["depth_distribution"] == {"0": 1, "1": 3}  # broken asset not counted as a page


def test_canonical_private_ip_does_not_make_page_non_indexable(run):
    c, pages, issues = run()
    st = pages["/en/news"]["indexability"]
    assert st["indexable"]["value"] is True
    assert st["canonical"]["value"] is False and st["canonical"]["status"] == "private_host"
    assert c["stats"]["indexability"]["canonical_status"] == {"private_host": 4, "not_applicable": 1}
    i = issues["CANONICAL_TO_NON_INDEXABLE"]
    assert i["severity"] == "critical" and i["value"]["pages_still_indexable"] == 4
    by = {x["issue"]: x for x in c["stats"]["page_audit_summary"]["issues"]}
    assert by["CANONICAL_PRIVATE_IP"]["severity"] == "critical" and by["CANONICAL_PRIVATE_IP"]["count"] == 4
    assert by["CANONICAL_HTTP_WHEN_PAGE_HTTPS"]["count"] == 4
    assert by["HREFLANG_PRIVATE_IP"]["count"] == 4


def test_js_rendered_content_is_evidence_based(run):
    c, _, _ = run()
    by = {x["issue"]: x for x in c["stats"]["page_audit_summary"]["issues"]}
    js = by["CONTENT_REQUIRES_JS"]
    assert js["severity"] == "warning" and js["percentage"] == 100.0
    assert "client-rendered" in js["examples"][0]["detail"]
    assert "Search Console" in js["recommendation"] and "not that search engines cannot" in js["recommendation"]


def test_spa_robots_and_sitemap_html_fallback(run):
    c, _, issues = run()
    assert c["stats"]["robots_status"] == "html_response"
    assert "ROBOTS_TXT_INVALID" in issues and "SITEMAP_NOT_IN_ROBOTS" not in issues
    sm = c["stats"]["sitemap"]
    assert sm["status"] == "html_instead_of_xml" and sm["read_complete"] is False
    assert "HTML page instead of an XML sitemap" in issues["SITEMAP_EMPTY_OR_INVALID"]["message"]
    assert "PAGES_MISSING_FROM_SITEMAP" not in issues and "SITEMAP_ANALYSIS_INCOMPLETE" not in issues


def test_scope_classification(run):
    c, _, _ = run()
    cov = c["stats"]["crawl"]
    assert cov["audit_scope"] == "full_known_scope" and cov["complete"] is True
    assert cov["site_coverage_verified"] is False and cov["confidence"] == "medium"  # no sitemap corroboration
    assert cov["crawled_urls"] == 5 and cov["queue_remaining"] == 0 and cov["sitemap_urls"] is None

    cov = run(max_pages=5)[0]["stats"]["crawl"]  # queue emptied exactly at the limit: not proof of size
    assert cov["audit_scope"] == "crawl_limit_reached" and cov["complete"] is False
    assert cov["stop_reason"] == "max_pages_reached"

    cov = run(max_pages=2)[0]["stats"]["crawl"]
    assert cov["audit_scope"] == "partial" and cov["queue_remaining"] > 0 and cov["confidence"] == "low"


# --- sitemap status classification ------------------------------------------------------------------
NS = "xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'"


@pytest.mark.parametrize("body,ctype,expected", [
    (f"<urlset {NS}><url><loc>{B}/a</loc></url></urlset>", "application/xml", "ok"),
    (f"<urlset {NS}></urlset>", "application/xml", "empty"),
    ("<!doctype html><html><body>app</body></html>", "text/html", "html"),
    ("<html><body>app</body></html>", "application/xml", "html"),
    ("<urlset><url><loc>broken", "application/xml", "parse_error"),
    ("<rss><channel><item><link>x</link></item></channel></rss>", "application/rss+xml", "unsupported_format"),
])
def test_classify_sitemap(body, ctype, expected):
    assert classify_sitemap(body.encode(), ctype)[2] == expected


def _collect(files: dict, max_urls=1000, max_files=100):
    def h(req):
        if req.url.path not in files:
            return httpx.Response(404)
        body, ctype = files[req.url.path]
        return httpx.Response(200, text=body, headers={"content-type": ctype})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as client:
            return await collect_sitemap_urls(client, [B + "/sitemap.xml"], max_urls, max_files)
    return asyncio.run(go())[2]


X = "application/xml"


@pytest.mark.parametrize("files,status,complete,reason", [
    ({"/sitemap.xml": (f"<urlset {NS}><url><loc>{B}/a</loc></url></urlset>", X)}, "ok", True, None),
    ({"/sitemap.xml": (f"<urlset {NS}></urlset>", X)}, "empty", True, None),
    ({"/sitemap.xml": ("<html>app</html>", "text/html")}, "html_instead_of_xml", False, "sitemap_html_response"),
    ({"/sitemap.xml": ("<urlset><loc>", X)}, "parse_error", False, "sitemap_parse_error"),
    ({}, "not_found", False, "sitemap_http_error"),
    ({"/sitemap.xml": (f"<sitemapindex {NS}><sitemap><loc>{B}/a.xml</loc></sitemap><sitemap><loc>{B}/b.xml"
                       f"</loc></sitemap></sitemapindex>", X),
      "/a.xml": (f"<urlset {NS}><url><loc>{B}/a</loc></url></urlset>", X),
      "/b.xml": ("<html>oops</html>", "text/html")}, "partial", False, "sitemap_html_response"),
])
def test_sitemap_overall_status(files, status, complete, reason):
    meta = _collect(files)
    assert (meta["status"], meta["read_complete"], meta["stop_reason"]) == (status, complete, reason)
