"""Final hardening regressions: language-aware word counts and main-content confidence, accessible link
names, head metadata source (server vs rendered), root-cause grouping, language scope, sitemap severity."""
import asyncio

import httpx
import pytest

from app.models.request import CrawlOptions
from app.rules.seo_rules import SITE_RULES
from app.services import crawler, fetcher, store
from app.services.fetcher import RawFetch, analyze
from app.services.html_parser import parse_html
from app.services.report_context import report_context
from app.services.seo_auditor import audit_page

WORDS = "Our university offers degree programs in data science and engineering. " * 30  # 300 words


def codes(page, crawl=None):
    return {i.code: i for i in audit_page(page, crawl=crawl).issues}


# --- 1. word counting / main content -------------------------------------------------------------------
def test_english_count_is_exact_even_with_thai_footer():
    p = parse_html("https://x.com/en", f"<html lang='en'><body><main><p>{WORDS}</p></main>"
                                       "<footer>ซอยวิภาวดีรังสิต แขวงรัชดาภิเษก</footer></body></html>")
    assert p.detected_language == "en" and p.language_signal == "html_lang"
    assert p.main_content_word_count == 300 and p.word_count_is_approximate is False
    assert "whitespace" in p.word_count_method


def test_thai_count_is_estimated_and_flagged():
    p = parse_html("https://x.com/th", "<html lang='th'><body><main><p>มหาวิทยาลัยหอการค้าไทย</p></main></body></html>")
    assert p.detected_language == "th" and p.word_count_is_approximate is True
    assert p.main_content_word_count == 5  # 22 Thai chars / 5 per word, rounded up


def test_language_from_url_segment_when_no_html_lang():
    p = parse_html("https://x.com/en/news", f"<html><body><main>{WORDS}</main></body></html>")
    assert (p.detected_language, p.language_signal) == ("en", "url_segment")


def test_nav_footer_heavy_page_with_small_article_is_still_thin():
    nav = "<nav>" + "<a href='/x'>Menu item link</a>" * 200 + "</nav>"
    p = parse_html("https://x.com/a", f"<html lang='en'><body>{nav}<main><article><p>Short news item with only "
                                      f"a few words.</p></article></main><footer>{WORDS}</footer></body></html>")
    assert p.main_content_confidence == "high" and p.main_content_word_count == 8
    c = codes(p)
    assert "VERY_THIN_CONTENT" in c and c["VERY_THIN_CONTENT"].value["basis"] == "main content"
    assert "not a search-engine rule" in c["VERY_THIN_CONTENT"].message


def test_homepage_with_distributed_content_is_not_thin():
    """Like ai.utcc.ac.th/en: two <main> regions plus sections outside them."""
    html = (f"<html lang='en'><body><nav>menu</nav><main><h1>Hero</h1><p>{'Welcome to the AI site. ' * 10}</p>"
            f"</main><section><p>{WORDS}</p></section><main><p>{'More highlights here. ' * 10}</p></main>"
            "<footer>contact</footer></body></html>")
    p = parse_html("https://x.com/en", html)
    assert p.main_content_method == "main_elements(2)" and p.main_content_word_count == 81  # both mains joined
    assert p.main_content_confidence == "low" and p.content_word_count > 300
    assert not {"VERY_THIN_CONTENT", "THIN_CONTENT"} & codes(p).keys()


def test_js_rendered_english_content(monkeypatch):
    shell = "<html lang='en'><head><title>App</title></head><body><div id='root'></div></body></html>"
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: (
        f"<html lang='en'><head><title>App page title that is long enough</title></head><body><main>{WORDS}"
        "</main><footer>ที่อยู่ กรุงเทพมหานคร</footer></body></html>", 3))
    cp = analyze(RawFetch(status_code=200, final_url="https://x.com/en", content_type="text/html", headers={},
                          html=shell, duration_ms=1), "auto")
    assert cp.info.source == "rendered" and cp.info.raw_word_count == 0
    assert cp.page.main_content_word_count == 300 and cp.page.word_count_is_approximate is False
    c = codes(cp.page, cp.info)
    assert "CONTENT_REQUIRES_JS" in c and "client-rendered" in c["CONTENT_REQUIRES_JS"].message
    assert not {"VERY_THIN_CONTENT", "THIN_CONTENT"} & c.keys()


# --- 3. accessible link names --------------------------------------------------------------------------
@pytest.mark.parametrize("anchor,empty", [
    ("<a href='/p'><img src='a.jpg' alt='Photo'></a>", False),
    ("<a href='/p' aria-label='Open photo'><img src='a.jpg'></a>", False),
    ("<a href='/p'><img src='a.jpg' alt=''></a>", True),
    ("<a href='/p'>Text</a>", False),
    ("<span id='lbl'>Gallery</span><a href='/p' aria-labelledby='lbl'><img src='a.jpg' alt=''></a>", False),
    ("<a href='/p' aria-labelledby='nope'><img src='a.jpg' alt=''></a>", True),
    ("<a href='/p'><img src='a.jpg' alt=''><img src='b.jpg' alt='Second'></a>", False),
])
def test_empty_link_accessible_name(anchor, empty):
    p = parse_html("https://x.com/", f"<html><body>{anchor}</body></html>")
    assert ("EMPTY_LINK" in codes(p)) is empty


# --- 4. head metadata source ---------------------------------------------------------------------------
HEAD = ("<title>Rendered title for the AI page here</title><meta name='description' content='{d}'>"
        "<link rel='canonical' href='https://x.com/en/a'><meta property='og:title' content='T'>"
        "<meta property='og:description' content='D'>")


def _analyze(monkeypatch, raw_head, rendered_head):
    body = f"<body><main>{WORDS}</main></body>"
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: (
        f"<html lang='en'><head>{rendered_head}</head>{body}</html>", 3))
    raw = (f"<html lang='en'><head>{raw_head}<script src='/react-dom.production.min.js'></script></head>"
           f"{body}</html>")
    return analyze(RawFetch(status_code=200, final_url="https://x.com/en/a", content_type="text/html",
                            headers={}, html=raw, duration_ms=1), "always")


def test_metadata_only_in_rendered_dom_is_not_missing(monkeypatch):
    cp = _analyze(monkeypatch, "<title>Shell</title>", HEAD.format(d="A description " * 6))
    assert cp.info.source == "raw"  # same body content, so the raw DOM is the page source...
    assert cp.info.metadata_source["canonical"] == "rendered_dom"  # ...but head tags come from the rendered DOM
    assert cp.info.metadata_source["title"] == "server_html"
    c = codes(cp.page, cp.info)
    assert "CANONICAL_MISSING" not in c and "META_DESCRIPTION_MISSING" not in c
    assert "canonical" in c["METADATA_REQUIRES_JS"].value["fields"]


def test_metadata_absent_from_rendered_dom_is_missing(monkeypatch):
    cp = _analyze(monkeypatch, "<title>Shell</title>", "<title>Shell</title>")
    assert cp.info.metadata_source["canonical"] == "missing"
    assert {"CANONICAL_MISSING", "META_DESCRIPTION_MISSING", "OG_TITLE_MISSING"} <= codes(cp.page, cp.info).keys()


# --- 2/4/5. crawl-level: root causes, language scope ---------------------------------------------------
B = "https://ai.ex.com"
PAGES = ["/en", "/en/news", "/en/bare"]


def page_html(path):
    head = ("" if path == "/en/bare" else
            f"<link rel='canonical' href='http://10.7.45.121{path}'>"
            f"<meta name='description' content='{'Description text ' * 6}'>"
            f"<meta property='og:title' content='T'><meta property='og:description' content='D'>"
            f"<link rel='alternate' hreflang='th-TH' href='http://10.7.45.121/th{path[3:]}'>"
            f"<link rel='alternate' hreflang='en' href='http://10.7.45.121{path}'>")
    links = "".join(f"<a href='{p}'>{p}</a>" for p in PAGES)
    return (f"<html lang='en'><head><title>AI site page title long enough</title>{head}</head><body><main>"
            f"<p>{WORDS}</p>{links}<button role='switch' aria-label='Switch to Thai'>TH</button></main></body></html>")


@pytest.fixture
def crawl_ai(monkeypatch):
    shell = "<html lang='en'><head><title>App</title></head><body><div id='root'></div></body></html>"
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: (page_html(url.removeprefix(B)), 2))

    def handler(req):
        if req.url.path in PAGES:
            return httpx.Response(200, html=shell)
        return httpx.Response(404, html="<html><body>missing</body></html>")

    conn = store.connect(":memory:")
    o = CrawlOptions(url=B + "/en")
    cid = crawler.create_crawl(conn, o)
    asyncio.run(crawler.run_crawl(cid, o, conn, httpx.MockTransport(handler)))
    c = store.row_dict(conn.execute("SELECT * FROM crawls WHERE id=?", (cid,)).fetchone())
    return c, report_context(c["stats"], c["site_issues"])


def test_private_base_url_root_cause_counted_once(crawl_ai):
    c, ctx = crawl_ai
    roots = {r["root_cause"]: r for r in c["stats"]["page_audit_summary"]["root_causes"]}
    rc = roots["PRIVATE_BASE_URL_CONFIGURATION"]
    assert rc["affected_pages"] == 2 and rc["evidence"]["private_hosts"] == {"10.7.45.121": 2}
    assert rc["evidence"]["rule_detections"] == {"CANONICAL_PRIVATE_IP": 2, "HREFLANG_PRIVATE_IP": 2,
                                                 "CANONICAL_HTTP_WHEN_PAGE_HTTPS": 2}
    by = {i["issue"]: i for i in c["stats"]["page_audit_summary"]["issues"]}
    for code in ("CANONICAL_PRIVATE_IP", "HREFLANG_PRIVATE_IP", "CANONICAL_HTTP_WHEN_PAGE_HTTPS"):
        assert by[code]["count"] == 2 and by[code]["independent_count"] == 0  # raw detections kept
    codes_ = [f["code"] for f in ctx["findings"]]
    assert codes_.count("PRIVATE_BASE_URL_CONFIGURATION") == 1
    assert not {"CANONICAL_PRIVATE_IP", "CANONICAL_HTTP_WHEN_PAGE_HTTPS", "CANONICAL_TO_NON_INDEXABLE",
                "HREFLANG_PRIVATE_IP"} & set(codes_)
    f = next(f for f in ctx["findings"] if f["code"] == "PRIVATE_BASE_URL_CONFIGURATION")
    assert f["affected"] == 2 and f["severity"] == "critical"
    assert f["evidence"]["site_level_detection"]["code"] == "CANONICAL_TO_NON_INDEXABLE"
    for f in ctx["findings"]:  # every finding carries the required fields
        assert {"code", "root_cause", "severity", "affected", "denominator", "percentage", "evidence",
                "examples", "drilldown", "recommendation"} <= f.keys()


def test_hreflang_only_private_host_is_same_root_cause():
    from app.services.root_causes import explain
    audit = {"issues": [{"code": "HREFLANG_PRIVATE_IP", "severity": "critical",
                         "value": [{"lang": "th", "href": "http://10.0.0.5/th"}]}]}
    hits, explained = explain([{"url": "https://x.com/en", "audit": audit}])
    assert hits["PRIVATE_BASE_URL_CONFIGURATION"][0]["hosts"] == ["10.0.0.5"]
    assert explained == {("https://x.com/en", "HREFLANG_PRIVATE_IP"): "PRIVATE_BASE_URL_CONFIGURATION"}


def test_route_without_head_metadata_is_one_root_cause(crawl_ai):
    c, ctx = crawl_ai
    roots = {r["root_cause"]: r for r in c["stats"]["page_audit_summary"]["root_causes"]}
    rc = roots["ROUTE_HEAD_METADATA_MISSING"]
    assert rc["affected_pages"] == 1 and rc["examples"][0]["url"] == "/en/bare"
    assert rc["examples"][0]["metadata_source"]["canonical"] == "missing"
    assert "META_DESCRIPTION_MISSING" not in [f["code"] for f in ctx["findings"]]


def test_language_scope_not_called_whole_website(crawl_ai):
    c, ctx = crawl_ai
    cov = c["stats"]["crawl"]
    assert cov["audit_scope"] == "full_known_scope"
    assert cov["coverage_confidence"] == "scoped_complete_other_languages_not_crawled"
    assert cov["languages_not_crawled"] == ["/th"] and cov["site_coverage_verified"] is False
    assert {a["hreflang"] for a in cov["alternate_languages"]} == {"en", "th"}  # th-TH grouped as th
    assert cov["alternate_language_urls_not_crawled"] == 2
    assert B + "/th" in cov["alternate_language_urls_not_crawled_examples"]  # private host mapped to public
    assert cov["crawled_scope"]["language_segments"] == {"en": 3}
    assert cov["requested_scope"]["start_language_segment"] == "en"
    assert "not a percentage of the website" in cov["coverage"]
    assert "This audit covers the /en scope; other language scopes (/th)" in ctx["scope_statement"]


# --- 7. sitemap severity -------------------------------------------------------------------------------
def test_sitemap_severity_is_warning_not_critical():
    assert SITE_RULES["SITEMAP_EMPTY_OR_INVALID"][1] == "warning"
    assert SITE_RULES["SITEMAP_NOT_FOUND"][1] == "warning"
    assert SITE_RULES["SITEMAP_URL_NON_200"][1] == "warning"  # broken URLs in a valid sitemap: separate issue


# --- resource classification: Content-Type first, URL extension only as fallback ----------------------
from app.services.url_utils import resource_type  # noqa: E402


@pytest.mark.parametrize("url,ctype,status,expected", [
    ("https://x.com/media/12345", "image/webp", 200, "asset"),          # extensionless asset
    ("https://x.com/fonts/main", "font/woff2", 200, "asset"),
    ("https://x.com/v/intro", "video/mp4", 200, "asset"),
    ("https://x.com/a/track", "audio/mpeg", 200, "asset"),
    ("https://x.com/download", "application/pdf", 200, "non_html"),
    ("https://x.com/get?id=1", "application/zip", 200, "non_html"),
    ("https://x.com/about", "text/html; charset=utf-8", 200, "document"),  # extensionless page
    ("https://x.com/gallery/photo.jpg", "text/html", 200, "document"),     # HTML wins over extension
    ("https://x.com/wp-content/a.jpg", "text/html", 404, "asset"),         # HTML error page for an image
    ("https://x.com/missing-page", "text/html", 404, "document"),
    ("https://x.com/file.pdf", None, None, "asset"),                       # no response info: extension
])
def test_resource_type(url, ctype, status, expected):
    assert resource_type(url, ctype, status) == expected


def test_extensionless_asset_and_page_in_crawl(monkeypatch):
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: ("<html></html>", 1))
    home = (f"<html lang='en'><head><title>Home page of the example site ok</title></head><body><main>{WORDS}"
            "<a href='/about'>About</a><a href='/media/42'>Photo</a></main></body></html>")

    def handler(req):
        if req.url.path == "/media/42":
            return httpx.Response(200, content=b"PNG-bytes", headers={"content-type": "image/png"})
        if req.url.path in ("/", "/about"):
            return httpx.Response(200, html=home)
        return httpx.Response(404)
    conn = store.connect(":memory:")
    o = CrawlOptions(url="https://x.com/", use_sitemaps=False)
    cid = crawler.create_crawl(conn, o)
    asyncio.run(crawler.run_crawl(cid, o, conn, httpx.MockTransport(handler)))
    stats = store.row_dict(conn.execute("SELECT stats FROM crawls WHERE id=?", (cid,)).fetchone())["stats"]
    assert stats["counts"]["html_pages_ok"] == 2 and stats["counts"]["assets_and_non_html_ok"] == 1
    assert stats["page_audit_summary"]["total_pages"] == 2
    assert stats["indexability"]["resource_type"] == {"document": 2, "asset": 1}


# --- www.utcc.ac.th regressions ------------------------------------------------------------------------
from app.services.sitemap import classify_sitemap  # noqa: E402
from app.services.url_utils import crawl_key  # noqa: E402


def test_thai_iri_and_percent_encoded_url_are_one_url():
    raw = "https://www.utcc.ac.th/คณะนิติศาสตร์-7/"
    enc = "https://www.utcc.ac.th/%e0%b8%84%e0%b8%93%e0%b8%b0%e0%b8%99%e0%b8%b4%e0%b8%95%e0%b8%b4%e0%b8%a8" \
          "%e0%b8%b2%e0%b8%aa%e0%b8%95%e0%b8%a3%e0%b9%8c-7/"
    assert crawl_key(raw) == crawl_key(enc)
    assert "%E0%B8%84" in crawl_key(raw)  # one canonical encoded form, uppercase escapes


def test_rss_and_atom_sitemaps_are_read():
    rss = ("<?xml version='1.0'?><rss version='2.0'><channel><title>x</title><link>https://x.com/</link>"
           "<item><link>https://x.com/a</link></item><item><link>https://x.com/b</link></item></channel></rss>")
    assert classify_sitemap(rss.encode(), "text/xml") == (["https://x.com/a", "https://x.com/b"], [], "ok")
    atom = ("<feed xmlns='http://www.w3.org/2005/Atom'><entry><link href='https://x.com/c'/></entry>"
            "<entry><link rel='edit' href='https://x.com/edit'/></entry></feed>")
    assert classify_sitemap(atom.encode(), "application/atom+xml") == (["https://x.com/c"], [], "ok")


def test_tracking_pixels_are_not_content_images():
    p = parse_html("https://x.com/", "<html><body><noscript><img src='https://tr.line.me/tag.gif'></noscript>"
                                     "<img src='/px.gif' width='1' height='1'><img src='/flag.png'></body></html>")
    assert p.images_total == 1 and p.images_without_alt == 1  # only the real (flag) image counts


def test_sitemap_as_start_url_crawls_from_homepage(monkeypatch):
    monkeypatch.setattr(fetcher, "render_html", lambda url, timeout=30: ("<html></html>", 1))
    home = f"<html lang='en'><head><title>Home page of the example site ok</title></head><body><main>{WORDS}" \
           "<a href='/about'>About</a></main></body></html>"
    sm = "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><url><loc>https://x.com/about</loc></url>" \
         "<url><loc>https://x.com/orphan</loc></url></urlset>"

    def handler(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sm, headers={"content-type": "application/xml"})
        if req.url.path in ("/", "/about", "/orphan"):
            return httpx.Response(200, html=home)
        return httpx.Response(404)
    conn = store.connect(":memory:")
    o = CrawlOptions(url="https://x.com/sitemap.xml")
    cid = crawler.create_crawl(conn, o)
    asyncio.run(crawler.run_crawl(cid, o, conn, httpx.MockTransport(handler)))
    stats = store.row_dict(conn.execute("SELECT stats FROM crawls WHERE id=?", (cid,)).fetchone())["stats"]
    assert stats["crawl"]["requested_scope"]["start_url"] == "https://x.com/"
    assert stats["crawl"]["requested_scope"]["start_url_was_sitemap"] is True
    assert stats["depth_distribution"] == {"0": 1, "1": 1, "sitemap_only": 1}
    assert stats["sitemap"]["read_complete"] is True and stats["counts"]["html_pages_ok"] == 3


def test_rss_feed_overlap_is_not_a_sitemap_duplicate():
    from app.services.sitemap import collect_sitemap_urls
    ns = "xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'"
    files = {"/sitemap.xml": f"<urlset {ns}><url><loc>https://x.com/a</loc></url><url><loc>https://x.com/b</loc>"
                             "</url><url><loc>https://x.com/b</loc></url></urlset>",
             "/sitemap.rss": "<rss><channel><item><link>https://x.com/a</link></item></channel></rss>"}

    def h(req):
        return httpx.Response(200, text=files[req.url.path], headers={"content-type": "text/xml"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return await collect_sitemap_urls(c, ["https://x.com/sitemap.xml", "https://x.com/sitemap.rss"], 100, 10)
    urls, _, meta = asyncio.run(go())
    assert sorted(urls) == ["https://x.com/a", "https://x.com/b"]
    assert meta["duplicate_examples"] == ["https://x.com/b"]  # XML self-duplicate counted, feed overlap not
