import asyncio
import sqlite3

import httpx

from app.models.request import CrawlOptions
from app.services import crawler, store
from app.services.indexability import _noindex

BASE = "https://ex.com"
NS = "xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'"


def page(title, links=(), head=""):
    a = "".join(f"<a href='{h}'>{h}</a>" for h in links)
    return (f"<html lang='en'><head><title>{title}</title>{head}</head><body><main><h1>{title}</h1>"
            f"<p>{'word ' * 200}</p>{a}</main></body></html>")


def make_site(sitemap_body, robots="User-agent: *\nDisallow: /blocked\n"):
    site = {
        "/robots.txt": robots,
        "/sitemap.xml": sitemap_body,
        "/": page("Home", ["/noindex", "/xrobots", "/canon", "/private-canon", "/gone", "/soft", "/plain"]),
        "/noindex": page("Noindex", head="<meta name='robots' content='noindex, follow'>"),
        "/xrobots": page("X robots"),
        "/canon": page("Canon", head="<link rel='canonical' href='https://ex.com/plain'>"),
        "/private-canon": page("Private", head="<link rel='canonical' href='http://10.0.0.1/x'>"),
        "/soft": page("Page not found"),
        "/plain": page("Plain", head="<link rel='canonical' href='https://ex.com/plain'>"),
        "/blocked": page("Blocked"),
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/moved":
            return httpx.Response(301, headers={"location": "/plain"})
        if path not in site:
            return httpx.Response(404, html=page("404"))
        headers = {"content-type": "application/xml" if path.endswith(".xml") else
                   "text/plain" if path.endswith(".txt") else "text/html"}
        if path == "/xrobots":
            headers["x-robots-tag"] = "googlebot: noindex"
        return httpx.Response(200, text=site[path], headers=headers)
    return handler


def crawl(handler, **opts):
    conn = store.connect(":memory:")
    o = CrawlOptions(url=BASE + "/", **opts)
    cid = crawler.create_crawl(conn, o)
    asyncio.run(crawler.run_crawl(cid, o, conn, httpx.MockTransport(handler)))
    c = store.row_dict(conn.execute("SELECT * FROM crawls WHERE id=?", (cid,)).fetchone())
    pages = {r["normalized_url"].removeprefix(BASE) or "/": store.row_dict(r)
             for r in conn.execute("SELECT * FROM crawl_pages WHERE crawl_id=?", (cid,))}
    return c, {k: v["indexability"] for k, v in pages.items()}, {i["code"]: i for i in c["site_issues"]}


SITEMAP = (f"<urlset {NS}>" + "".join(f"<url><loc>{BASE}{p}</loc></url>" for p in
           ["/", "/noindex", "/canon", "/gone", "/moved", "/blocked", "/plain", "/plain"]) + "</urlset>")


def test_page_states():
    c, st, _ = crawl(make_site(SITEMAP))
    assert c["status"] == "completed"
    assert st["/"]["indexable"]["value"] is True and st["/"]["canonical"]["value"] is True
    assert st["/noindex"]["indexable"] == {"value": False, "reason": "meta robots noindex"}
    assert st["/xrobots"]["indexable"]["reason"] == "X-Robots-Tag noindex"
    assert st["/canon"]["canonical"]["value"] is False
    assert "another URL" in st["/canon"]["canonical"]["reason"]
    assert st["/canon"]["indexable"]["value"] is True  # canonical state is tracked separately
    assert st["/plain"]["canonical"]["reason"] == "self-referencing canonical"
    assert "private" in st["/private-canon"]["canonical"]["reason"]
    assert st["/gone"]["fetchable"] == {"value": False, "reason": "HTTP 404"}
    assert st["/gone"]["indexable"]["value"] is False
    assert st["/blocked"]["crawlable"] == {"value": False, "reason": "blocked by robots.txt"}
    assert st["/moved"]["indexable"]["reason"].startswith("redirects to")
    assert st["/soft"]["soft_404_candidate"] is True and st["/plain"]["soft_404_candidate"] is False
    assert c["stats"]["indexability"]["indexable"]["false"] >= 4


def test_sitemap_issues():
    _, _, issues = crawl(make_site(SITEMAP))
    assert issues["SITEMAP_URL_NON_200"]["value"] == [BASE + "/gone"]
    assert issues["SITEMAP_URL_REDIRECT"]["value"] == [BASE + "/moved"]
    assert issues["SITEMAP_URL_BLOCKED"]["value"] == [BASE + "/blocked"]
    assert issues["SITEMAP_URL_NOINDEX"]["value"] == [BASE + "/noindex"]
    assert issues["SITEMAP_URL_CANONICALIZED"]["value"] == [BASE + "/canon"]
    assert issues["SITEMAP_DUPLICATE_URLS"]["value"] == [BASE + "/plain"]
    missing = issues["PAGES_MISSING_FROM_SITEMAP"]["value"]
    assert BASE + "/soft" in missing and BASE + "/noindex" not in missing
    assert "SITEMAP_NOT_IN_ROBOTS" in issues  # robots has no Sitemap: line
    assert issues["CANONICAL_TO_NON_INDEXABLE"]["severity"] == "critical"
    assert issues["CANONICAL_TO_NON_INDEXABLE"]["value"]["urls"] == [BASE + "/private-canon"]
    assert issues["CANONICALIZED_PAGES"]["value"] == [BASE + "/canon"]
    assert {"NOINDEX_PAGES", "BROKEN_PAGES", "SOFT_404_CANDIDATES", "ROBOTS_BLOCKED_PAGES"} <= issues.keys()


def test_empty_sitemap_is_flagged():
    _, _, issues = crawl(make_site("<html><body>app shell</body></html>"))
    assert "SITEMAP_EMPTY_OR_INVALID" in issues
    assert "PAGES_MISSING_FROM_SITEMAP" not in issues  # no sitemap → covered by the issue above


def test_missing_sitemap_is_flagged():
    handler = make_site("")

    def no_sitemap(req):
        return httpx.Response(404) if req.url.path == "/sitemap.xml" else handler(req)
    _, _, issues = crawl(no_sitemap)
    assert "SITEMAP_NOT_FOUND" in issues and "SITEMAP_EMPTY_OR_INVALID" not in issues


def test_noindex_token_parsing():
    assert _noindex("noindex, follow") and _noindex("NONE") and _noindex("googlebot: noindex")
    assert not _noindex("index, follow") and not _noindex(None) and not _noindex("noimageindex")


def test_migration_adds_columns(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE crawls (id INTEGER PRIMARY KEY, root_url TEXT NOT NULL, domain TEXT NOT NULL, "
                "status TEXT NOT NULL, options TEXT NOT NULL, created_at TEXT NOT NULL)")
    old.commit()
    old.close()
    cols = {r[1] for r in store.connect(path).execute("PRAGMA table_info(crawls)")}
    assert "site_issues" in cols
