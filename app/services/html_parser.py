"""Deterministic HTML -> structured SEO data. BeautifulSoup + lxml; never executes anything."""
import json
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.models.response import Heading, Hreflang, Image, Link, ParsedPage
from app.services import content_cleaner as cc
from app.services.url_utils import classify_link, normalize, resolve


def _attr(tag, name: str) -> str | None:
    if tag is None:
        return None
    val = tag.get(name)
    if isinstance(val, list):  # multi-valued attrs like rel/class
        val = " ".join(val)
    return val.strip() if val is not None else None


def _rel(tag) -> set[str]:
    rel = tag.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return {r.lower() for r in rel}


def _meta(soup, key: str, value: str) -> str | None:
    """First <meta key=value content=...>, attribute value matched case-insensitively."""
    value = value.lower()
    for tag in soup.find_all("meta"):
        if (_attr(tag, key) or "").lower() == value:
            return _attr(tag, "content")
    return None


def _charset(soup) -> str:
    tag = soup.find("meta", attrs={"charset": True})
    if tag:
        return _attr(tag, "charset").lower()
    content = _meta(soup, "http-equiv", "content-type") or ""
    for part in content.split(";"):
        k, _, v = part.strip().partition("=")
        if k.lower() == "charset":
            return v.strip().lower()
    return ""


def _jsonld_types(data, out: list[str]) -> None:
    if isinstance(data, list):
        for item in data:
            _jsonld_types(item, out)
    elif isinstance(data, dict):
        t = data.get("@type")
        for name in (t if isinstance(t, list) else [t]):
            if isinstance(name, str) and name not in out:
                out.append(name)
        if "@graph" in data:
            _jsonld_types(data["@graph"], out)


def _link_text(a) -> str:
    text = cc.text_of(a)
    if text:
        return text
    for cand in (_attr(a, "aria-label"), _attr(a, "title")):
        if cand:
            return cand
    img = a.find("img", alt=True)
    return (_attr(img, "alt") or "") if img else ""


_LOCATION_TAGS = {"nav": "nav", "header": "header", "footer": "footer", "aside": "aside", "main": "main",
                  "article": "main"}
_LOCATION_ROLES = {"navigation": "nav", "banner": "header", "contentinfo": "footer",
                   "complementary": "aside", "main": "main"}


def _link_location(a) -> str:
    """Nearest semantic container of a link: nav | header | footer | aside | main | body."""
    for parent in a.parents:
        if parent.name in _LOCATION_TAGS:
            return _LOCATION_TAGS[parent.name]
        role = (parent.get("role") or "").lower() if hasattr(parent, "get") else ""
        if role in _LOCATION_ROLES:
            return _LOCATION_ROLES[role]
    return "body"


def parse_html(url: str, html: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    parts = urlsplit(url)
    html_tag = soup.find("html")

    # <base href> affects relative URL resolution
    base_tag = soup.find("base", href=True)
    base_url = (resolve(url, _attr(base_tag, "href")) if base_tag else None) or url

    # Metadata is searched document-wide: real pages often misplace tags into <body>.
    title_tag = soup.find("title")
    title = cc.normalize_ws(title_tag.get_text()) if title_tag else None
    meta_desc = _meta(soup, "name", "description")
    meta_desc = cc.normalize_ws(meta_desc) if meta_desc is not None else None
    robots = _meta(soup, "name", "robots")
    robot_tokens = {t.strip().lower() for t in (robots or "").split(",")}

    canonical_tag = next((t for t in soup.find_all("link", href=True) if "canonical" in _rel(t)), None)
    canonical = _attr(canonical_tag, "href")

    # Headings in document order
    headings: dict[str, list[str]] = {f"h{i}": [] for i in range(1, 7)}
    order: list[Heading] = []
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = cc.text_of(h)
        if not text:
            img = h.find("img", alt=True)
            text = cc.normalize_ws(_attr(img, "alt") or "") if img else ""
        headings[h.name].append(text)
        order.append(Heading(level=int(h.name[1]), text=text))

    # Links: raw list preserved; repeats of (type, normalized url, text) are flagged
    # is_duplicate and excluded from summary counts (desktop + mobile nav duplication).
    links: list[Link] = []
    seen: set[tuple] = set()
    for a in soup.find_all("a", href=True):
        href = _attr(a, "href") or ""
        ltype, absolute = classify_link(base_url, href)
        text = _link_text(a)
        target = normalize(absolute) if absolute and ltype in ("internal", "external") else href
        key = (ltype, target, text.casefold())
        links.append(Link(text=text, href=href, absolute_url=absolute, type=ltype,
                          nofollow="nofollow" in _rel(a), is_duplicate=key in seen,
                          location=_link_location(a)))
        seen.add(key)
    unique = [link for link in links if not link.is_duplicate]

    images = [
        Image(src=_attr(img, "src") or "", absolute_url=resolve(base_url, _attr(img, "src")),
              alt=_attr(img, "alt"), title=_attr(img, "title") or "", loading=_attr(img, "loading") or "")
        for img in soup.find_all("img")
    ]

    hreflang = [
        Hreflang(lang=_attr(t, "hreflang") or "", href=_attr(t, "href") or "",
                 absolute_url=resolve(base_url, _attr(t, "href")))
        for t in soup.find_all("link", hreflang=True)
        if "alternate" in _rel(t)
    ]

    # JSON-LD: malformed blocks are recorded, never fatal
    sd_count, sd_types, sd_errors = 0, [], []
    for script in soup.find_all("script", type=True):
        if (_attr(script, "type") or "").lower() != "application/ld+json":
            continue
        sd_count += 1
        try:
            _jsonld_types(json.loads(script.get_text()), sd_types)
        except (ValueError, RecursionError) as exc:
            sd_errors.append(f"JSON-LD block {sd_count}: {type(exc).__name__}: {str(exc)[:200]}")

    charset = _charset(soup)
    language = (_attr(html_tag, "lang") or "") if html_tag else ""

    # Content extraction mutates the soup, so it runs last.
    cc.strip_noise(soup)
    body_text = cc.text_of(soup.body or soup)
    main_content = cc.text_of(cc.main_content_node(soup))

    return ParsedPage(
        url=url,
        domain=(parts.hostname or "").lower(),
        scheme=parts.scheme.lower(),
        language=language,
        charset=charset,
        title=title,
        title_length=len(title or ""),
        meta_description=meta_desc,
        meta_description_length=len(meta_desc or ""),
        meta_robots=robots,
        noindex=bool(robot_tokens & {"noindex", "none"}),
        nofollow=bool(robot_tokens & {"nofollow", "none"}),
        canonical=canonical,
        canonical_absolute_url=resolve(base_url, canonical),
        viewport=_meta(soup, "name", "viewport"),
        og_title=_meta(soup, "property", "og:title"),
        og_description=_meta(soup, "property", "og:description"),
        og_url=_meta(soup, "property", "og:url"),
        og_type=_meta(soup, "property", "og:type"),
        og_image=_meta(soup, "property", "og:image"),
        twitter_card=_meta(soup, "name", "twitter:card"),
        twitter_title=_meta(soup, "name", "twitter:title"),
        twitter_description=_meta(soup, "name", "twitter:description"),
        twitter_image=_meta(soup, "name", "twitter:image"),
        headings=headings,
        h1=headings["h1"], h2=headings["h2"], h3=headings["h3"],
        h1_count=len(headings["h1"]),
        h2_count=len(headings["h2"]),
        heading_order=order,
        links=links,
        links_total=len(unique),
        links_raw_total=len(links),
        duplicate_links_count=len(links) - len(unique),
        internal_links_count=sum(link.type == "internal" for link in unique),
        external_links_count=sum(link.type == "external" for link in unique),
        anchor_links_count=sum(link.type == "anchor" for link in unique),
        nofollow_links_count=sum(link.nofollow for link in unique),
        images=images,
        images_total=len(images),
        images_without_alt=sum(i.alt is None for i in images),
        images_with_empty_alt=sum(i.alt == "" for i in images),
        images_with_alt=sum(bool(i.alt) for i in images),
        hreflang=hreflang,
        structured_data_count=sd_count,
        structured_data_types=sd_types,
        structured_data_errors=sd_errors,
        body_text=body_text,
        main_content=main_content,
        word_count=cc.count_words(body_text),
        main_content_word_count=cc.count_words(main_content),
        word_count_is_approximate=cc.contains_thai(body_text),
    )
