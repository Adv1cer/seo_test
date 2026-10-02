from fastapi.testclient import TestClient

from app.main import app
from app.services.html_parser import parse_html
from app.services.seo_auditor import audit_page, score_issues
from app.models.response import Issue
from app.tests.conftest import page

URL = "https://example.com/page"
client = TestClient(app)


def codes(html: str, url: str = URL) -> dict[str, Issue]:
    return {i.code: i for i in audit_page(parse_html(url, html)).issues}


def test_good_page_scores_high(good_html):
    a = audit_page(parse_html("https://example.com/widgets", good_html))
    assert a.critical_count == 0 and a.warning_count == 0
    assert a.score == 100 and a.grade == "excellent"
    assert "HREFLANG_DOMAIN_MISMATCH" not in {i.code for i in a.issues}


def test_title_and_description_rules():
    c = codes(page(body="<h1>x</h1>"))
    assert c["TITLE_MISSING"].severity == "critical"
    assert c["META_DESCRIPTION_MISSING"].severity == "warning"
    assert "TITLE_TOO_SHORT" in codes(page(head="<title>Short</title>"))
    assert "TITLE_TOO_LONG" in codes(page(head=f"<title>{'x' * 61}</title>"))
    assert "META_DESCRIPTION_TOO_SHORT" in codes(page(head="<meta name='description' content='short'>"))
    assert "META_DESCRIPTION_TOO_LONG" in codes(page(head=f"<meta name='description' content='{'x' * 161}'>"))


def test_heading_rules():
    assert "H1_MISSING" in codes(page(body="<h2>x</h2>"))
    c = codes(page(body="<h1>a</h1><h1>b</h1><h3>c</h3><h2></h2><h4>d</h4>"))
    assert c["MULTIPLE_H1"].count == 2
    assert c["EMPTY_HEADING"].value == ["h2"]
    assert c["HEADING_ORDER_SKIPPED"].value == ["H1 -> H3", "H2 -> H4"]
    assert c["HEADING_ORDER_SKIPPED"].severity == "info"


def test_canonical_rules():
    assert "CANONICAL_MISSING" in codes(page())
    ok = codes(page(head="<link rel='canonical' href='https://example.com/page'>"))
    assert not any(k.startswith("CANONICAL_") and k != "CANONICAL_OK" for k in ok)
    assert "CANONICAL_PRIVATE_IP" in codes(page(head="<link rel='canonical' href='http://10.1.2.3/page'>"))
    c = codes(page(head="<link rel='canonical' href='https://other.com/page'>"))
    assert c["CANONICAL_DOMAIN_MISMATCH"].severity == "warning"
    assert "CANONICAL_HTTP_WHEN_PAGE_HTTPS" in codes(page(head="<link rel='canonical' href='http://example.com/page'>"))
    assert "CANONICAL_INVALID" in codes(page(head="<link rel='canonical' href=''>"))
    assert "CANONICAL_INVALID" in codes(page(head="<link rel='canonical' href='ftp://example.com/x'>"))


def test_hreflang_rules():
    c = codes(page(head="""
        <link rel='alternate' hreflang='en' href='https://example.com/en'>
        <link rel='alternate' hreflang='EN' href='https://example.com/en2'>
        <link rel='alternate' hreflang='english' href='https://example.com/x'>
        <link rel='alternate' hreflang='de-DE' href='https://example.de/'>
        <link rel='alternate' hreflang='th' href='http://192.168.0.5/th'>"""))
    assert c["HREFLANG_DUPLICATE_LANGUAGE"].value == ["en"]
    assert c["HREFLANG_INVALID"].count == 1
    assert c["HREFLANG_DOMAIN_MISMATCH"].severity == "info"  # cross-domain can be legitimate
    assert c["HREFLANG_PRIVATE_IP"].severity == "critical"


def test_image_alt_rule_ignores_decorative():
    assert "IMAGE_ALT_MISSING" not in codes(page(body="<img src='a' alt=''>"))
    c = codes(page(body="<img src='a'><img src='b'><img src='c' alt=''>"))
    assert c["IMAGE_ALT_MISSING"].count == 2
    assert c["IMAGE_ALT_MISSING"].value["images_with_empty_alt"] == 1


def test_link_rules_never_claim_broken():
    c = codes(page(body="<a href='/x'></a><a href='javascript:void(0)'>j</a><a href=''>e</a>"))
    assert {"EMPTY_LINK", "JAVASCRIPT_LINK", "INVALID_LINK"} <= set(c)
    assert not any("BROKEN" in k for k in c)


def test_robots_rules_not_critical():
    c = codes(page(head="<meta name='robots' content='noindex,nofollow'>"))
    assert c["NOINDEX_PAGE"].severity == "warning"
    assert c["NOFOLLOW_PAGE"].severity == "info"


def test_social_and_structured_data_rules():
    c = codes(page())
    for k in ("OG_TITLE_MISSING", "OG_DESCRIPTION_MISSING", "OG_IMAGE_MISSING",
              "TWITTER_CARD_MISSING", "STRUCTURED_DATA_NOT_FOUND"):
        assert c[k].severity == "info"
    c = codes(page(head="<script type='application/ld+json'>{oops</script>"))
    assert "STRUCTURED_DATA_INVALID_JSON" in c and "STRUCTURED_DATA_NOT_FOUND" in c


def test_content_rules():
    assert "EMPTY_MAIN_CONTENT" in codes(page())
    assert "VERY_THIN_CONTENT" in codes(page(body="<p>" + "w " * 50 + "</p>"))
    assert "THIN_CONTENT" in codes(page(body="<p>" + "w " * 200 + "</p>"))
    assert "CONTENT_OK" in codes(page(body="<p>" + "w " * 400 + "</p>"))


def test_ai_utcc_style_fixture(utcc_html):
    p = parse_html("https://ai.utcc.ac.th/th", utcc_html)
    assert p.title == "AI UTCC" and p.language == "th"
    assert p.canonical == "http://10.7.45.121/th"
    assert p.links_raw_total == 8 and p.links_total == 4  # desktop+mobile nav collapsed
    a = audit_page(p)
    c = {i.code: i for i in a.issues}
    assert c["CANONICAL_PRIVATE_IP"].severity == "critical"
    assert "CANONICAL_HTTP_WHEN_PAGE_HTTPS" in c
    assert c["HREFLANG_PRIVATE_IP"].count == 2
    assert "TITLE_TOO_SHORT" in c
    assert c["IMAGE_ALT_MISSING"].count == 1
    assert a.critical_count >= 2 and 0 <= a.score < 75


def test_score_is_capped_and_bounded():
    many = page(body="".join(f"<img src='{i}'>" for i in range(50)))
    c = codes(many)
    assert c["IMAGE_ALT_MISSING"].count == 50
    worst = [Issue(code=f"X{i}", category="c", severity="critical", message="", count=99, recommendation="")
             for i in range(20)]
    assert score_issues(worst) == 0
    assert score_issues([]) == 100


def test_deterministic_scoring(utcc_html, good_html):
    for url, html in (("https://ai.utcc.ac.th/th", utcc_html), ("https://example.com/widgets", good_html)):
        results = {audit_page(parse_html(url, html)).model_dump_json() for _ in range(20)}
        assert len(results) == 1
        api = {client.post("/api/seo/audit", json={"url": url, "html": html}).text for _ in range(5)}
        assert len(api) == 1


# ---------- API ----------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_parse_and_audit_endpoints(utcc_html):
    body = {"url": "https://ai.utcc.ac.th/th", "html": utcc_html}
    r = client.post("/api/seo/parse", json=body)
    assert r.status_code == 200 and r.json()["success"] is True
    assert r.json()["data"]["canonical"] == "http://10.7.45.121/th"
    r = client.post("/api/seo/audit", json=body)
    data = r.json()["data"]
    assert set(data) == {"page", "audit"}
    assert data["page"]["title"] == "AI UTCC"
    assert any(i["code"] == "CANONICAL_PRIVATE_IP" for i in data["audit"]["issues"])


def test_api_validation_errors():
    for body in ({"html": "<p>x</p>"}, {"url": URL}, {"url": "not a url", "html": "<p>"},
                 {"url": URL, "html": "   "}, {"url": URL, "html": 123}):
        r = client.post("/api/seo/audit", json=body)
        assert r.status_code == 422, body
        assert r.json()["success"] is False and r.json()["error"]["code"] == "VALIDATION_ERROR"
        assert "Traceback" not in r.text
    r = client.post("/api/seo/parse", content="not json", headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_api_payload_too_large(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "max_html_bytes", 100)
    r = client.post("/api/seo/parse", json={"url": URL, "html": "x" * 200})
    assert r.status_code == 413 and r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_api_malformed_html_does_not_crash():
    r = client.post("/api/seo/audit", json={"url": URL, "html": "<<<>>><html><p<div></table>\x00"})
    assert r.status_code == 200


def test_report_endpoint(utcc_html):
    body = {"url": "https://ai.utcc.ac.th/th", "html": utcc_html}
    r = client.post("/api/seo/report", json=body)
    assert r.status_code == 200
    data = r.json()["data"]
    md = data["report_markdown"]
    assert md.startswith("# SEO Audit Report: https://ai.utcc.ac.th/th")
    assert "CANONICAL_PRIVATE_IP" in md and "10.7.45.121" in md and "นักศึกษา" not in md
    assert data["audit"]["score"] == client.post("/api/seo/audit", json=body).json()["data"]["audit"]["score"]
    raw = client.post("/api/seo/report?format=markdown", json=body)
    assert raw.headers["content-type"].startswith("text/markdown") and raw.text == md


def test_lenient_body_formats():
    import json
    from urllib.parse import urlencode
    payload = {"url": URL, "html": "<title>x</title><h1>x</h1>"}
    as_string = client.post("/api/seo/audit", content=json.dumps(json.dumps(payload)),
                            headers={"content-type": "application/json"})
    no_ctype = client.post("/api/seo/audit", content=json.dumps(payload), headers={"content-type": "text/plain"})
    form = client.post("/api/seo/audit", content=urlencode(payload),
                       headers={"content-type": "application/x-www-form-urlencoded"})
    assert as_string.status_code == no_ctype.status_code == form.status_code == 200
    bad = client.post("/api/seo/audit", content='{"url": "x", "html": "<a href="x">"}',
                      headers={"content-type": "application/json"})
    assert bad.status_code == 422 and "not valid JSON" in bad.text
    arr = client.post("/api/seo/audit", json=[1])
    assert arr.status_code == 422 and "got list" in arr.text
