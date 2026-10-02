import time

from app.services.content_cleaner import count_words
from app.services.html_parser import parse_html
from app.services.url_utils import classify_link, is_private_host, normalize
from app.tests.conftest import page

URL = "https://example.com/blog/post"


def parse(html: str, url: str = URL):
    return parse_html(url, html)


def test_valid_normal_html(good_html):
    p = parse(good_html, "https://example.com/widgets")
    assert p.title.startswith("Example Widgets")
    assert p.meta_description.startswith("Browse durable")
    assert p.canonical_absolute_url == "https://example.com/widgets"
    assert p.language == "en" and p.charset == "utf-8"
    assert p.viewport and p.og_image == "https://example.com/og.png"
    assert p.twitter_card == "summary_large_image"
    assert p.h1 == ["Steel Widgets"] and p.h2_count == 1
    assert [h.level for h in p.heading_order] == [1, 2, 3]
    assert p.structured_data_types == ["Organization", "Product"]
    assert p.main_content_word_count > 300


def test_missing_title_and_description():
    p = parse(page(body="<p>hi</p>"))
    assert p.title is None and p.title_length == 0
    assert p.meta_description is None


def test_multiple_and_no_h1():
    assert parse(page(body="<h1>A</h1><h1>B</h1>")).h1_count == 2
    assert parse(page(body="<h2>A</h2>")).h1_count == 0


def test_relative_urls_resolved():
    p = parse(page(body="<a href='../about'>About</a><a href='//cdn.example.com/x'>CDN</a><img src='i.png' alt='x'>"))
    assert p.links[0].absolute_url == "https://example.com/about"
    assert p.links[1].absolute_url == "https://cdn.example.com/x"
    assert p.images[0].absolute_url == "https://example.com/blog/i.png"


def test_base_href_respected():
    p = parse(page(head="<base href='https://example.com/root/'>", body="<a href='a'>A</a>"))
    assert p.links[0].absolute_url == "https://example.com/root/a"


def test_internal_external_classification():
    p = parse(page(body="""
        <a href='/x'>rel</a><a href='https://EXAMPLE.com/y'>abs</a><a href='https://www.example.com/z'>www</a>
        <a href='https://blog.example.com/'>sub</a><a href='https://other.com/'>other</a>
        <a href='/startswith-slash-but-fine'>s</a>"""))
    types = [link.type for link in p.links]
    assert types == ["internal", "internal", "internal", "external", "external", "internal"]
    assert p.internal_links_count == 4 and p.external_links_count == 2


def test_images_alt_states():
    p = parse(page(body="<img src='a' alt='Cat'><img src='b' alt=''><img src='c'><img src='d' alt='  '>"))
    assert p.images_total == 4
    assert p.images_with_alt == 1
    assert p.images_with_empty_alt == 2  # alt="" and whitespace-only alt
    assert p.images_without_alt == 1


def test_relative_canonical():
    p = parse(page(head="<link rel='canonical' href='/blog/post'>"))
    assert p.canonical == "/blog/post"
    assert p.canonical_absolute_url == "https://example.com/blog/post"


def test_hreflang_extraction():
    p = parse(page(head="<link rel='alternate' hreflang='th-TH' href='/th'><link rel='alternate' hreflang='en' href='https://example.com/en'>"))
    assert [(h.lang, h.absolute_url) for h in p.hreflang] == [
        ("th-TH", "https://example.com/th"), ("en", "https://example.com/en")]


def test_robots_noindex_nofollow():
    p = parse(page(head="<meta name='ROBOTS' content='NoIndex, nofollow'>"))
    assert p.noindex and p.nofollow and p.meta_robots == "NoIndex, nofollow"
    p = parse(page(head="<meta name='robots' content='none'>"))
    assert p.noindex and p.nofollow
    assert not parse(page()).noindex


def test_jsonld_valid_and_malformed():
    p = parse(page(head="""
        <script type='application/ld+json'>{"@type": ["WebPage", "Article"]}</script>
        <script type='application/ld+json'>{bad json</script>"""))
    assert p.structured_data_count == 2
    assert p.structured_data_types == ["WebPage", "Article"]
    assert len(p.structured_data_errors) == 1 and "block 2" in p.structured_data_errors[0]


def test_thai_content_preserved():
    thai = "มหาวิทยาลัยหอการค้าไทย ที่นี่มีน้ำใจ"
    p = parse(page(body=f"<main><p>{thai}</p></main>", lang="th"))
    assert p.main_content == thai  # combining marks (ั ่ ้ ำ ใ) intact
    assert p.word_count_is_approximate
    assert p.main_content_word_count > 2


def test_thai_english_mixed():
    p = parse(page(body="<main><p>AI ปัญญาประดิษฐ์ for everyone</p></main>"))
    assert "AI ปัญญาประดิษฐ์ for everyone" == p.main_content
    assert count_words("AI for everyone") == 3
    assert count_words("ปัญญาประดิษฐ์") == 3  # ceil(13 / 5)
    assert p.main_content_word_count == 3 + 3


def test_duplicate_navigation_links():
    nav = "<nav><a href='/a'>A</a><a href='/b'>B</a></nav>"
    p = parse(page(body=nav + nav.replace("/b", "/b/") + "<a href='/a'>Different text</a>"))
    assert p.links_raw_total == 5
    assert [link.is_duplicate for link in p.links] == [False, False, True, True, False]
    assert p.links_total == 3 and p.duplicate_links_count == 2
    assert p.internal_links_count == 3


def test_empty_html():
    p = parse("")
    assert p.title is None and p.word_count == 0 and p.links == []


def test_malformed_html():
    p = parse("<html><head><title>Broken<body><h1>Head<p>text <a href='/x'>link<div></span></html")
    assert p.h1_count == 1
    assert p.links_total == 1


def test_very_large_html():
    body = "".join(f"<section><h2>S{i}</h2><p>para {i} lorem ipsum dolor</p><a href='/p{i}'>l{i}</a></section>"
                   for i in range(20000))
    html = page(head="<title>Big</title>", body=body)
    assert len(html) > 1_500_000
    start = time.perf_counter()
    p = parse(html)
    assert time.perf_counter() - start < 30
    assert p.h2_count == 20000 and p.links_total == 20000


def test_script_style_svg_excluded(utcc_html):
    p = parse(utcc_html, "https://ai.utcc.ac.th/th")
    for junk in ("should not appear", "color:red", "SVG TEXT HIDDEN"):
        assert junk not in p.body_text
    assert "หน้าแรก" in p.body_text            # nav present in body text
    assert "หน้าแรก" not in p.main_content     # but not in main content
    assert "AI Integrated University" in p.main_content


def test_query_and_fragment_urls():
    p = parse(page(body="<a href='/s?q=1#top'>q</a><a href='#sec'>a</a><a href='/blog/post#x'>self</a>"))
    assert p.links[0].type == "internal" and p.links[0].absolute_url == "https://example.com/s?q=1#top"
    assert p.links[1].type == "anchor"
    assert p.links[2].type == "anchor"
    assert normalize("HTTPS://Example.com:443/a/?x=1#f") == "https://example.com/a?x=1"


def test_mailto_tel_javascript_invalid_links():
    p = parse(page(body="<a href='mailto:a@b.c'>m</a><a href='tel:+66'>t</a><a href='javascript:void(0)'>j</a>"
                        "<a href=''>e</a><a href='http://[::1'>bad</a><a>no href</a>"))
    assert [link.type for link in p.links] == ["mailto", "tel", "javascript", "invalid", "invalid"]


def test_url_utils_private_hosts():
    for host in ("10.7.45.121", "192.168.1.1", "172.16.0.1", "127.0.0.1", "localhost", "::1"):
        assert is_private_host(host), host
    assert not is_private_host("8.8.8.8") and not is_private_host("example.com")
    assert classify_link(URL, "  ") == ("invalid", None)
