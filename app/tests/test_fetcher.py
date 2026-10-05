import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import fetcher

STATIC = "<html lang='en'><head><title>Static page title here</title></head><body><main><h1>Hello</h1>" + \
    "<p>" + "word " * 400 + "</p>" + "".join(f"<a href='/p{i}'>Page {i}</a>" for i in range(5)) + "</main></body></html>"
SPA = "<html><head><title>App</title><script src='/static/react-dom.production.min.js'></script></head>" \
      "<body><div id='root'></div></body></html>"
SPA_RENDERED = "<html><head><title>App</title></head><body><h1>Rendered</h1><p>" + "content " * 300 + \
    "</p>" + "".join(f"<a href='/r{i}'>R {i}</a>" for i in range(10)) + "</body></html>"
SSR_REACT = STATIC.replace("</head>", "<script id='__NEXT_DATA__'>{}</script></head>")


@pytest.fixture
def serve(monkeypatch):
    def _serve(html, status=200):
        transport = httpx.MockTransport(lambda req: httpx.Response(status, html=html))
        monkeypatch.setattr(fetcher.httpx, "get",
                            lambda url, **kw: httpx.Client(transport=transport).get(url))
    return _serve


@pytest.fixture
def renders(monkeypatch):
    calls = []

    def _set(result):
        def fake(url, timeout=30):
            calls.append(url)
            if isinstance(result, Exception):
                raise result
            return result, 42
        monkeypatch.setattr(fetcher, "render_html", fake)
    _set.calls = calls
    return _set


def test_static_page_not_rendered(serve, renders):
    serve(STATIC)
    renders(SPA_RENDERED)
    r = fetcher.crawl_page("https://ex.com/")
    assert r.info.render_required is False
    assert r.info.render_status == "not_needed"
    assert r.info.source == "raw"
    assert renders.calls == []


def test_ssr_react_page_not_rendered(serve, renders):
    serve(SSR_REACT)
    renders(SPA_RENDERED)
    assert fetcher.crawl_page("https://ex.com/").info.render_required is False


def test_client_rendered_page_uses_rendered_dom(serve, renders):
    serve(SPA)
    renders(SPA_RENDERED)
    r = fetcher.crawl_page("https://ex.com/")
    assert r.info.render_required and r.info.render_status == "rendered"
    assert any("react" in x for x in r.info.render_reasons)
    assert any("app root" in x for x in r.info.render_reasons)
    assert r.info.source == "rendered"
    assert r.page.h1 == ["Rendered"] and r.raw.h1 == []
    assert r.info.render_duration_ms == 42


def test_render_failure_falls_back_to_raw(serve, renders):
    serve(SPA)
    renders(RuntimeError("chromium crashed"))
    r = fetcher.crawl_page("https://ex.com/")
    assert r.info.render_status == "failed"
    assert "chromium crashed" in r.info.render_error
    assert r.info.source == "raw" and r.rendered is None


def test_render_never_skips(serve, renders):
    serve(SPA)
    renders(SPA_RENDERED)
    r = fetcher.crawl_page("https://ex.com/", "never")
    assert r.info.render_status == "skipped" and renders.calls == []


def test_rendered_not_used_when_no_gain(serve, renders):
    serve(STATIC)
    renders(STATIC)
    r = fetcher.crawl_page("https://ex.com/", "always")
    assert r.info.render_status == "rendered" and r.info.source == "raw"


def test_404_not_rendered(serve, renders):
    serve(SPA, status=404)
    renders(SPA_RENDERED)
    r = fetcher.crawl_page("https://ex.com/x")
    assert r.info.status_code == 404 and renders.calls == []


def test_extract_endpoint_includes_crawl_info(serve, renders):
    serve(SPA)
    renders(SPA_RENDERED)
    body = TestClient(app).post("/api/seo/extract", json={"url": "https://ex.com/"}).json()
    assert body["success"] is True
    assert body["data"]["crawl"]["source"] == "rendered"
    assert body["data"]["page"]["h1"] == ["Rendered"]
    codes = [i["code"] for i in body["data"]["audit"]["issues"]]
    assert "CONTENT_REQUIRES_JS" in codes


def test_extract_endpoint_404(serve):
    serve(STATIC, status=404)
    assert TestClient(app).post("/api/seo/extract", json={"url": "https://ex.com/"}).status_code == 502


def test_static_extract_rendering_passes(serve):
    serve(STATIC)
    body = TestClient(app).post("/api/seo/extract", json={"url": "https://ex.com/"}).json()
    codes = [i["code"] for i in body["data"]["audit"]["issues"]]
    assert "RENDERING_OK" in codes and "CONTENT_REQUIRES_JS" not in codes
