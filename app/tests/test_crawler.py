import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.models.request import CrawlOptions
from app.services import crawler, fetcher, store
from app.services.sitemap import parse_sitemap
from app.services.url_utils import crawl_key

BASE = "https://ex.com"


def page(title, links=(), body="word " * 200, extra_head=""):
    a = "".join(f"<a href='{h}'>{t}</a>" for h, t in links)
    return (f"<html lang='en'><head><title>{title}</title>{extra_head}</head><body><main><h1>{title}</h1>"
            f"<p>{body}</p>{a}</main></body></html>")


SPA = "<html><head><title>App</title><script src='/react-dom.production.min.js'></script></head>" \
      "<body><div id='root'></div></body></html>"

SITE = {
    "/robots.txt": (200, "User-agent: *\nDisallow: /private\nSitemap: https://ex.com/sitemap_index.xml\n"),
    "/sitemap_index.xml": (200, "<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                                "<sitemap><loc>https://ex.com/sitemap1.xml</loc></sitemap></sitemapindex>"),
    "/sitemap1.xml": (200, "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                           "<url><loc>https://ex.com/about</loc></url>"
                           "<url><loc>https://ex.com/orphan</loc></url></urlset>"),
    "/": (200, page("Home", [("/about", "About"), ("/about?utm_source=x", "About again"), ("/old", "Old"),
                             ("/missing", "Missing"), ("/private/x", "Private"), ("/loop", "Loop"),
                             ("/app", "App"), ("/l1", "Deep"), ("https://other.com/", "External"),
                             ("/file.pdf", "PDF"), ("/nf", "nofollowed")])),
    "/about": (200, page("About", [("/", "Home")])),
    "/orphan": (200, page("Orphan")),
    "/new": (200, page("New", [("/", "Home")])),
    "/app": (200, SPA),
    "/l1": (200, page("L1", [("/l2", "L2")])),
    "/l2": (200, page("L2", [("/l3", "L3")])),
    "/l3": (200, page("L3")),
    "/nf": (200, page("NF")),
    "/file.pdf": (200, "%PDF"),
}


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
    if request.url.host != "ex.com":
        return httpx.Response(200, html=page("External"))
    if path == "/old":
        return httpx.Response(301, headers={"location": "/new"})
    if path in ("/loop", "/loop2"):
        return httpx.Response(302, headers={"location": "/loop2" if path == "/loop" else "/loop"})
    status, body = SITE.get(request.url.path, (404, page("Not found")))
    ctype = "application/pdf" if path.endswith(".pdf") else \
        "application/xml" if path.endswith(".xml") else "text/plain" if path.endswith(".txt") else "text/html"
    return httpx.Response(status, text=body, headers={"content-type": ctype})


SITE["/"] = (200, SITE["/"][1].replace("href='/nf'", "href='/nf' rel='nofollow'"))


@pytest.fixture
def run(monkeypatch):
    rendered = page("Rendered app", [("/", "Home")], body="content " * 300)
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: (rendered, 7))

    def _run(**opts):
        conn = store.connect(":memory:")
        o = CrawlOptions(url=BASE + "/", **opts)
        cid = crawler.create_crawl(conn, o)
        asyncio.run(crawler.run_crawl(cid, o, conn, httpx.MockTransport(handler)))
        crawl = store.row_dict(conn.execute("SELECT * FROM crawls WHERE id=?", (cid,)).fetchone())
        pages = {r["normalized_url"].removeprefix(BASE) or "/": store.row_dict(r)
                 for r in conn.execute("SELECT * FROM crawl_pages WHERE crawl_id=?", (cid,))}
        return crawl, pages, conn
    return _run


def test_full_crawl(run):
    crawl, pages, conn = run()
    assert crawl["status"] == "completed", crawl["error"]
    assert pages["/"]["depth"] == 0 and pages["/"]["crawl_status"] == "ok"
    assert pages["/about"]["depth"] == 1 and pages["/about"]["in_sitemap"] == 1
    # tracking param variant collapsed into /about
    assert not any("utm" in k for k in pages)
    # redirect recorded, target crawled
    assert pages["/old"]["crawl_status"] == "redirect" and pages["/old"]["status_code"] == 301
    assert pages["/old"]["redirect_target"] == BASE + "/new"
    assert pages["/new"]["crawl_status"] == "ok" and pages["/new"]["parent_url"] == BASE + "/old"
    # redirect loop is an error, not a hang
    assert pages["/loop"]["crawl_status"] == "error" and "redirect" in pages["/loop"]["error"]
    assert pages["/missing"]["status_code"] == 404
    assert pages["/private/x"]["crawl_status"] == "blocked_robots"
    # sitemap-only page has no link depth
    assert pages["/orphan"]["discovered_via"] == "sitemap" and pages["/orphan"]["depth"] is None
    assert pages["/orphan"]["internal_links_in"] == 0
    assert pages["/file.pdf"]["crawl_status"] == "non_html"
    # client-rendered page went through the browser
    assert pages["/app"]["render_status"] == "rendered" and pages["/app"]["title"] == "Rendered app"
    # nofollow link not followed; external domain not crawled
    assert "/nf" not in pages
    assert not any("other.com" in k for k in pages)
    assert pages["/"]["external_links_out"] == 1
    assert pages["/"]["internal_links_in"] >= 2  # from /about, /new, /app
    assert pages["/"]["audit"]["score"] >= 0
    stats = crawl["stats"]
    assert stats["blocked_by_robots"] == 1 and stats["rendered_with_browser"] == 1
    assert stats["depth_distribution"]["sitemap_only"] == 1
    links = conn.execute("SELECT COUNT(*) FROM page_links WHERE internal=0").fetchone()[0]
    assert links == 1


def test_max_depth_and_pages(run):
    _, pages, _ = run(max_depth=2, use_sitemaps=False)
    assert "/l2" in pages and "/l3" not in pages
    crawl, pages, _ = run(max_pages=3, use_sitemaps=False)
    assert crawl["stats"]["requested_urls"] == 3


def test_ignore_robots_and_exclude(run):
    _, pages, _ = run(respect_robots_txt=False, exclude_patterns=[r"/l\d"], use_sitemaps=False)
    assert pages["/private/x"]["crawl_status"] != "blocked_robots"
    assert "/l1" not in pages


def test_render_failure_does_not_fail_crawl(run, monkeypatch):
    def boom(url, timeout=30):
        raise RuntimeError("no browser")
    monkeypatch.setattr(fetcher, "render_html", boom)
    crawl, pages, _ = run(use_sitemaps=False)
    assert crawl["status"] == "completed"
    assert pages["/app"]["render_status"] == "failed" and pages["/app"]["crawl_status"] == "ok"
    assert crawl["stats"]["render_errors"][0]["error"].startswith("RuntimeError")


def test_crawl_key():
    assert crawl_key("HTTPS://Ex.com/a/?b=2&utm_source=x&a=1&a=3#frag") == "https://ex.com/a?a=1&b=2"
    assert crawl_key("https://ex.com/a/") == crawl_key("https://ex.com/a")


def test_parse_sitemap_index_and_gzip():
    import gzip
    urls, kids = parse_sitemap(SITE["/sitemap_index.xml"][1].encode())
    assert urls == [] and kids == ["https://ex.com/sitemap1.xml"]
    urls, _ = parse_sitemap(gzip.compress(SITE["/sitemap1.xml"][1].encode()))
    assert urls == ["https://ex.com/about", "https://ex.com/orphan"]
    assert parse_sitemap(b"not xml") == ([], [])


def test_crawl_api(monkeypatch, tmp_path):
    from app.config import settings
    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    real = crawler.run_crawl

    async def fake_run(cid, o):
        await real(cid, o, transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.api.crawls.run_crawl", fake_run)
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: (page("R"), 1))
    client = TestClient(app)
    r = client.post("/api/seo/crawls", json={"url": BASE, "max_pages": 5})
    assert r.status_code == 202
    cid = r.json()["data"]["crawl_id"]
    data = client.get(f"/api/seo/crawls/{cid}").json()["data"]
    assert data["status"] == "completed" and data["stats"]["requested_urls"] == 5
    pages = client.get(f"/api/seo/crawls/{cid}/pages").json()["data"]
    assert pages["total"] >= 5 and pages["pages"][0]["depth"] == 0
    assert client.get(f"/api/seo/crawls/{cid}/links?internal=true").json()["data"]["total"] > 0
    assert client.get("/api/seo/crawls/999").status_code == 404
    assert client.post("/api/seo/crawls", json={"url": BASE, "exclude_patterns": ["("]}).status_code == 422
