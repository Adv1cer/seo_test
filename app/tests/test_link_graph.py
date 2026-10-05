import asyncio

import httpx
from fastapi.testclient import TestClient

from app.models.request import CrawlOptions
from app.services import crawler, store
from app.services.html_parser import parse_html
from app.services.link_graph import analyze, pagerank

B = "https://ex.com"


def row(path, depth=1, status="ok", code=200, via="link", sitemap=0, redirect=None):
    return {"normalized_url": B + path, "url": B + path, "crawl_status": status, "status_code": code,
            "depth": depth, "discovered_via": via, "in_sitemap": sitemap, "redirect_target": redirect}


def link(src, tgt, nofollow=0, anchor="x"):
    return {"source_url": B + src, "target_url": B + tgt, "anchor_text": anchor, "nofollow": nofollow,
            "location": "main"}


def test_pagerank_hub_wins():
    nodes = ["a", "b", "c", "d"]
    ranks = pagerank(nodes, {"a": {"b"}, "c": {"b"}, "d": {"b"}, "b": {"a"}})
    assert max(ranks, key=ranks.get) == "b"
    assert abs(sum(ranks.values()) - 1) < 1e-6
    assert pagerank([], {}) == {}


def test_graph_metrics_and_issues():
    rows = [row("/", 0, via="root"), row("/hub"), row("/leaf", 2), row("/orphan", None, via="sitemap", sitemap=1),
            row("/deep", 5, sitemap=1), row("/gone", 2, code=404), row("/old", 1, status="redirect", code=301,
                                                                          redirect=B + "/hub"),
            row("/nf-only", 2)]
    links = [link("/", "/hub"), link("/", "/old"), link("/hub", "/"), link("/hub", "/leaf"), link("/hub", "/gone"),
             link("/hub", "/deep"), link("/leaf", "/hub"), link("/deep", "/hub"), link("/hub", "/nf-only", nofollow=1)]
    metrics, issues, summary = analyze(rows, links)
    codes = {i.code: i for i in issues}

    assert metrics[B + "/hub"]["authority"] == 100.0  # most linked page, incl. via the /old redirect
    assert metrics[B + "/hub"]["unique_inlinks"] == 3  # /, /leaf, /deep; the / -> /old -> /hub link counts once
    assert "orphan_candidate" in metrics[B + "/orphan"]["flags"]
    assert "dead_end" in metrics[B + "/orphan"]["flags"]
    assert "orphan_candidate" in metrics[B + "/nf-only"]["flags"]  # only a nofollow link points here
    assert "weak_internal_linking" in metrics[B + "/leaf"]["flags"]
    assert "deep" in metrics[B + "/deep"]["flags"] and "important_but_deep" in metrics[B + "/deep"]["flags"]
    assert "orphan_candidate" not in metrics[B + "/"]["flags"]  # start page is never an orphan
    assert B + "/gone" not in metrics  # error pages hold no authority

    assert codes["BROKEN_INTERNAL_LINKS"].value[0] == {"source": B + "/hub", "target": B + "/gone",
                                                       "anchor": "x", "status": 404}
    assert codes["BROKEN_INTERNAL_LINKS"].severity == "critical"
    assert codes["REDIRECTED_INTERNAL_LINKS"].value[0]["redirects_to"] == B + "/hub"
    assert {"ORPHAN_PAGE_CANDIDATES", "DEAD_END_PAGES", "WEAKLY_LINKED_PAGES", "DEEP_PAGES",
            "IMPORTANT_PAGES_TOO_DEEP"} <= codes.keys()
    assert summary["broken_internal_links"] == 1 and summary["max_depth"] == 5
    assert summary["depth_distribution"]["sitemap_only"] == 1


def test_link_location():
    html = ("<html><body><header><a href='/h'>h</a></header><nav><a href='/n'>n</a></nav>"
            "<div role='navigation'><a href='/r'>r</a></div><main><a href='/m'>m</a></main>"
            "<aside><a href='/a'>a</a></aside><footer><a href='/f'>f</a></footer><a href='/b'>b</a></body></html>")
    locs = {l.href: l.location for l in parse_html(B + "/", html).links}
    assert locs == {"/h": "header", "/n": "nav", "/r": "nav", "/m": "main", "/a": "aside", "/f": "footer", "/b": "body"}


def test_architecture_endpoint(monkeypatch, tmp_path):
    from app.config import settings
    from app.main import app
    from app.tests.test_crawler import handler
    from app.services import fetcher
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "a.db"))
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: ("<html><body>x</body></html>", 1))
    conn = store.connect()
    o = CrawlOptions(url=B + "/")
    cid = crawler.create_crawl(conn, o)
    asyncio.run(crawler.run_crawl(cid, o, conn, httpx.MockTransport(handler)))
    data = TestClient(app).get(f"/api/seo/crawls/{cid}/architecture").json()["data"]
    assert data["status"] == "completed"
    assert data["top_authority_pages"][0]["authority"] == 100.0
    assert data["summary"]["pages"] > 3
    assert any(i["code"] == "BROKEN_INTERNAL_LINKS" for i in data["issues"])  # / links to /missing (404)
    assert any(p["url"].endswith("/orphan") for p in data["flagged"]["orphan_candidate"])
    links = TestClient(app).get(f"/api/seo/crawls/{cid}/links?internal=true").json()["data"]["links"]
    assert all(l["location"] == "main" for l in links)
