"""Deterministic SEO audit over a ParsedPage. Same input -> identical output; no I/O."""
import re
from collections import Counter
from urllib.parse import urlsplit

from app.config import Settings, settings as default_settings
from app.models.response import Audit, CrawlInfo, Issue, ParsedPage
from app.rules.seo_rules import (GRADES, GROUPS, PENALTY_BASE, PENALTY_CAP, RULES,
                                 SEVERITY_ORDER)
from app.services.url_utils import host_of, is_private_host, is_web_url, same_site

# language (2-3 letters) [-script (4 letters)] [-region (2 letters or 3 digits)] | x-default
_HREFLANG_RE = re.compile(r"^(x-default|[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$", re.I)
_EXAMPLES = 10


class _Collector:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def add(self, code: str, message: str, value=None, count: int = 1) -> None:
        category, severity, rec = RULES[code]
        self.issues.append(Issue(code=code, category=category, severity=severity, message=message,
                                 value=value, count=count, recommendation=rec))


def _check_metadata(p: ParsedPage, c: _Collector, s: Settings) -> None:
    if not p.title:
        c.add("TITLE_MISSING", "Title is missing or empty.")
    elif p.title_length < s.title_min:
        c.add("TITLE_TOO_SHORT", f"Title is {p.title_length} characters (minimum {s.title_min}).", p.title)
    elif p.title_length > s.title_max:
        c.add("TITLE_TOO_LONG", f"Title is {p.title_length} characters (maximum {s.title_max}).", p.title)

    if not p.meta_description:
        c.add("META_DESCRIPTION_MISSING", "Meta description is missing or empty.")
    elif p.meta_description_length < s.meta_desc_min:
        c.add("META_DESCRIPTION_TOO_SHORT",
              f"Meta description is {p.meta_description_length} characters (minimum {s.meta_desc_min}).",
              p.meta_description)
    elif p.meta_description_length > s.meta_desc_max:
        c.add("META_DESCRIPTION_TOO_LONG",
              f"Meta description is {p.meta_description_length} characters (maximum {s.meta_desc_max}).",
              p.meta_description)


def _check_headings(p: ParsedPage, c: _Collector, s: Settings) -> None:
    if p.h1_count == 0:
        c.add("H1_MISSING", "No H1 heading found.")
    elif p.h1_count > 1:
        c.add("MULTIPLE_H1", f"Found {p.h1_count} H1 headings.", p.h1[:_EXAMPLES], p.h1_count)

    empty = [f"h{h.level}" for h in p.heading_order if not h.text]
    if empty:
        c.add("EMPTY_HEADING", f"{len(empty)} empty heading(s).", empty[:_EXAMPLES], len(empty))

    # A skip is a jump deeper by more than one level (H1 -> H3). Going back up is fine.
    skips, prev = [], 0
    for h in p.heading_order:
        if prev and h.level > prev + 1:
            skips.append(f"H{prev} -> H{h.level}")
        prev = h.level
    if skips:
        c.add("HEADING_ORDER_SKIPPED", f"Heading level skipped {len(skips)} time(s).",
              skips[:_EXAMPLES], len(skips))


def _check_canonical(p: ParsedPage, c: _Collector, s: Settings) -> None:
    if p.canonical is None:
        c.add("CANONICAL_MISSING", "No canonical link found.")
        return
    url = p.canonical_absolute_url
    if not is_web_url(url):
        c.add("CANONICAL_INVALID", "Canonical is empty or not a valid http(s) URL.", p.canonical)
        return
    if is_private_host(host_of(url)):
        c.add("CANONICAL_PRIVATE_IP", f"Canonical points to private/internal host {host_of(url)}.", url)
    elif not same_site(url, p.url):
        c.add("CANONICAL_DOMAIN_MISMATCH", f"Canonical host {host_of(url)} differs from page host {p.domain}.", url)
    if p.scheme == "https" and urlsplit(url).scheme.lower() == "http":
        c.add("CANONICAL_HTTP_WHEN_PAGE_HTTPS", "Page is https but canonical is http.", url)


def _check_hreflang(p: ParsedPage, c: _Collector, s: Settings) -> None:
    invalid, private, foreign = [], [], []
    for h in p.hreflang:
        if not _HREFLANG_RE.match(h.lang) or not is_web_url(h.absolute_url):
            invalid.append({"lang": h.lang, "href": h.href})
        elif is_private_host(host_of(h.absolute_url)):
            private.append({"lang": h.lang, "href": h.absolute_url})
        elif not same_site(h.absolute_url, p.url):
            foreign.append({"lang": h.lang, "href": h.absolute_url})
    if invalid:
        c.add("HREFLANG_INVALID", f"{len(invalid)} invalid hreflang entr(ies).", invalid[:_EXAMPLES], len(invalid))
    if private:
        c.add("HREFLANG_PRIVATE_IP", f"{len(private)} hreflang URL(s) point to private/internal hosts.",
              private[:_EXAMPLES], len(private))
    if foreign:
        c.add("HREFLANG_DOMAIN_MISMATCH", f"{len(foreign)} hreflang URL(s) point to another domain.",
              foreign[:_EXAMPLES], len(foreign))
    dupes = sorted(lang for lang, n in Counter(h.lang.lower() for h in p.hreflang).items() if n > 1)
    if dupes:
        c.add("HREFLANG_DUPLICATE_LANGUAGE", "Duplicate hreflang values.", dupes, len(dupes))


def _check_images(p: ParsedPage, c: _Collector, s: Settings) -> None:
    # alt="" is a valid decorative marker and is NOT counted as missing.
    if p.images_without_alt:
        srcs = [i.src for i in p.images if i.alt is None][:_EXAMPLES]
        c.add("IMAGE_ALT_MISSING",
              f"{p.images_without_alt} of {p.images_total} image(s) have no alt attribute "
              f"({p.images_with_empty_alt} have decorative alt=\"\").",
              {"images_without_alt": p.images_without_alt, "images_with_empty_alt": p.images_with_empty_alt,
               "examples": srcs},
              p.images_without_alt)


def _check_links(p: ParsedPage, c: _Collector, s: Settings) -> None:
    unique = [link for link in p.links if not link.is_duplicate]
    empty = [link.href for link in unique if not link.text]
    js = [link.href for link in unique if link.type == "javascript"]
    invalid = [link.href for link in unique if link.type == "invalid"]
    if empty:
        c.add("EMPTY_LINK", f"{len(empty)} link(s) have no accessible text.", empty[:_EXAMPLES], len(empty))
    if js:
        c.add("JAVASCRIPT_LINK", f"{len(js)} javascript: link(s).", js[:_EXAMPLES], len(js))
    if invalid:
        c.add("INVALID_LINK", f"{len(invalid)} link(s) have an empty or malformed href.",
              invalid[:_EXAMPLES], len(invalid))


def _check_robots(p: ParsedPage, c: _Collector, s: Settings) -> None:
    if p.noindex:
        c.add("NOINDEX_PAGE", "Page has meta robots noindex.", p.meta_robots)
    if p.nofollow:
        c.add("NOFOLLOW_PAGE", "Page has meta robots nofollow.", p.meta_robots)


def _check_open_graph(p: ParsedPage, c: _Collector, s: Settings) -> None:
    if not p.og_title:
        c.add("OG_TITLE_MISSING", "og:title is missing.")
    if not p.og_description:
        c.add("OG_DESCRIPTION_MISSING", "og:description is missing.")
    if not p.og_image:
        c.add("OG_IMAGE_MISSING", "og:image is missing.")


def _check_twitter(p: ParsedPage, c: _Collector, s: Settings) -> None:
    if not p.twitter_card:
        c.add("TWITTER_CARD_MISSING", "twitter:card is missing (optional).")


def _check_content(p: ParsedPage, c: _Collector, s: Settings) -> None:
    """Heuristic only: thresholds are project settings, not search-engine rules. When the main-content
    extractor has low confidence (its region misses most visible content), judge the boilerplate-stripped
    content instead, so a small <main> alone never makes a page 'thin'."""
    low = p.main_content_confidence == "low"
    n = p.content_word_count if low else p.main_content_word_count
    basis = "content outside nav/header/footer (main-content extraction confidence low)" if low else "main content"
    approx = " (approximate: Thai word estimate)" if p.word_count_is_approximate else ""
    evidence = {"evaluated_words": n, "basis": basis, "main_content_words": p.main_content_word_count,
                "content_words": p.content_word_count, "body_words": p.word_count,
                "main_content_method": p.main_content_method, "main_content_confidence": p.main_content_confidence,
                "language": p.detected_language, "count_method": p.word_count_method}
    note = " Heuristic threshold, not a search-engine rule."
    if n == 0:
        c.add("EMPTY_MAIN_CONTENT", f"No text content found in {basis}.", evidence)
    elif n < s.very_thin_words:
        c.add("VERY_THIN_CONTENT", f"Potential thin content: {basis} has {n} words{approx} (< {s.very_thin_words}).{note}",
              evidence)
    elif n < s.thin_words:
        c.add("THIN_CONTENT", f"Potential thin content: {basis} has {n} words{approx} (< {s.thin_words}).{note}", evidence)


def _check_structured_data(p: ParsedPage, c: _Collector, s: Settings) -> None:
    if p.structured_data_errors:
        c.add("STRUCTURED_DATA_INVALID_JSON", f"{len(p.structured_data_errors)} malformed JSON-LD block(s).",
              p.structured_data_errors, len(p.structured_data_errors))
    if p.structured_data_count == len(p.structured_data_errors):
        c.add("STRUCTURED_DATA_NOT_FOUND", "No valid JSON-LD structured data found.")


CHECKS = [_check_metadata, _check_headings, _check_canonical, _check_hreflang, _check_images,
          _check_links, _check_robots, _check_open_graph, _check_twitter, _check_content,
          _check_structured_data]


def score_issues(issues: list[Issue]) -> int:
    penalty = sum(min(PENALTY_BASE[i.severity] * i.count, PENALTY_CAP[i.severity]) for i in issues)
    return max(0, min(100, 100 - penalty))


def grade_for(score: int) -> str:
    return next(name for threshold, name in GRADES if score >= threshold)


def _check_rendering(info: CrawlInfo, c: _Collector, s: Settings) -> None:
    js_only = sorted(f for f, src in info.metadata_source.items() if src == "rendered_dom")
    if js_only:
        c.add("METADATA_REQUIRES_JS", f"{len(js_only)} head field(s) only exist after JavaScript rendering: "
                                      f"{', '.join(js_only)}.", {"fields": js_only, "source": info.metadata_source})
    if info.source == "rendered" and info.raw_word_count < s.render_min_words:
        c.add("CONTENT_REQUIRES_JS",
              f"Main content is client-rendered: server HTML has {info.raw_word_count} words, rendered HTML has "
              f"{info.rendered_word_count}. Verify the rendered HTML and consider SSR/SSG for crawlability/performance.",
              {"raw_word_count": info.raw_word_count, "rendered_word_count": info.rendered_word_count})


def audit_page(page: ParsedPage, s: Settings = default_settings, crawl: CrawlInfo | None = None) -> Audit:
    """crawl is only known when the server fetched the page; it enables the rendering group."""
    c = _Collector()
    for check in CHECKS:
        check(page, c, s)
    groups = GROUPS
    if crawl is not None:
        _check_rendering(crawl, c, s)
        groups = GROUPS + ["rendering"]
    fired = {i.category for i in c.issues}
    for group in groups:
        if group not in fired:
            c.issues.append(Issue(code=f"{group.upper()}_OK", category=group, severity="passed",
                                  message=f"All {group.replace('_', ' ')} checks passed.", recommendation=""))
    issues = sorted(c.issues, key=lambda i: (SEVERITY_ORDER[i.severity], i.category, i.code))
    counts = Counter(i.severity for i in issues)
    score = score_issues(issues)
    return Audit(score=score, grade=grade_for(score), critical_count=counts["critical"],
                 warning_count=counts["warning"], info_count=counts["info"],
                 passed_count=counts["passed"], issues=issues)
