"""What an LLM report may conclude from a crawl: scope/coverage, explicit denominators, one unified list of
site-level findings (root causes counted once), definitions and reporting rules. Pure; reads stored stats."""

REPORTING_RULES = [
    "NEVER generalize a partial crawl into a site-wide conclusion.",
    "Only when crawl.coverage_confidence is 'sitemap_verified_complete' may you use site-wide wording ('all "
    "pages', 'no issues', 'healthy'). Otherwise scope every statement to the crawled URLs, e.g. 'No broken "
    "links were detected among the N crawled URLs'.",
    "If audit_scope is 'crawl_limit_reached' or crawl.stop_reason is 'max_pages_reached', say: 'The crawler "
    "audited N URLs within the configured crawl limit; this does not establish that the website contains only "
    "N URLs.'",
    "An exhausted crawl queue proves only that the crawler found no more URLs under the configured scope and "
    "rules (links via <a href>, same host). It does not prove the size of the website.",
    "If crawl.languages_not_crawled is non-empty, say which language versions were not crawled and do not "
    "describe the audit as covering the whole website.",
    "crawl.coverage is a percentage of URLs discovered within the requested scope, never of the website.",
    "Call only counts.html_pages_ok 'pages'. Discovered/recorded URLs include redirects, assets, and errors.",
    "Every percentage must name its denominator (each finding states it).",
    "Report each entry of findings once. Raw rule detections attached to a root cause are effects of that "
    "root cause, not separate problems; do not add their counts or severities together.",
    "sitemap.discovered_urls is the number of URLs listed in the sitemaps ('The sitemap contains X URLs'), "
    "not the number of pages on the website. If sitemap.read_complete is false, do not claim pages are "
    "missing from the sitemap.",
    "indexability.indexable describes the page itself; indexability.canonical describes whether the page is its "
    "own valid canonical. An invalid canonical does not make the page non-indexable.",
    "CONTENT_REQUIRES_JS / METADATA_REQUIRES_JS mean content is client-rendered. Do not claim Google cannot see it.",
    "THIN_CONTENT / VERY_THIN_CONTENT are heuristics (project thresholds), not search-engine rules.",
]

DEFINITIONS = {
    "counts.html_pages_ok": "HTML documents fetched with HTTP < 400 (the audited pages)",
    "counts.html_error_pages": "HTML responses with HTTP >= 400",
    "counts.assets_broken": "image/file URLs (by extension) returning HTTP >= 400",
    "counts.assets_and_non_html_ok": "image/file/non-HTML URLs fetched successfully",
    "crawl.requested_scope": "start URL, host and rules that bound what the crawler may discover",
    "crawl.discovered_urls": "discovered scope: unique in-scope URLs found via the start URL, links, redirects "
                             "and sitemaps",
    "crawl.crawled_urls": "crawled scope: HTTP requests made (limited by max_pages)",
    "crawl.queue_remaining": "discovered URLs not crawled",
    "crawl.audit_scope": "full_known_scope = every discovered URL was crawled before any limit; "
                         "crawl_limit_reached = queue emptied exactly at max_pages; partial = URLs left or a limit/"
                         "sitemap gap/error cut the crawl short",
    "crawl.coverage_confidence": "sitemap_verified_complete | scoped_complete | "
                                 "scoped_complete_other_languages_not_crawled | crawl_limit_reached | partial",
    "indexability.indexable": "page itself: fetched OK, not redirect, not noindex. null for assets",
    "indexability.canonical": "true = page is its own valid canonical (self or implicit); false = declares "
                              "another/invalid canonical. See canonical_status for the reason",
}

_PAGES = "counts.html_pages_ok"
# Site-issue code -> denominator ('site' = one site-wide check, 'links' = counted in link occurrences).
_DENOMINATORS = {"sitemap": "site", "robots": "site", "BROKEN_INTERNAL_LINKS": "links",
                 "BROKEN_RESOURCE_LINKS": "links", "REDIRECTED_INTERNAL_LINKS": "links",
                 "BROKEN_PAGES": "counts.recorded_urls", "ROBOTS_BLOCKED_PAGES": "counts.recorded_urls"}
# Site issues that are another view of a page-level root cause (folded in, never counted twice).
_SITE_ROOT_CAUSE = {"CANONICAL_TO_NON_INDEXABLE": ("PRIVATE_BASE_URL_CONFIGURATION", "private_host")}


def scope_statement(cov: dict, counts: dict) -> str:
    n, scope = cov.get("crawled_urls"), cov.get("audit_scope")
    seg = (cov.get("requested_scope") or {}).get("start_language_segment")
    others = cov.get("languages_not_crawled") or []
    lang = (f" This audit covers the /{seg} scope; other language scopes ({', '.join(others)}) declared via "
            "hreflang were not crawled." if others and seg else
            f" Language versions declared via hreflang were not crawled: {', '.join(others)}." if others else "")
    if scope == "full_known_scope":
        base = (f"All {cov.get('discovered_urls')} URLs discovered from {cov.get('requested_scope', {}).get('start_url')}"
                f" under the crawl rules were crawled ({counts.get('html_pages_ok')} successfully fetched HTML "
                "pages); this is 100% of the discovered scope, not of the website.")
        if cov.get("site_coverage_verified"):
            return base + " The fully read sitemap corroborates this scope." + lang
        return (base + f" No complete sitemap corroborates it (sitemap status: {cov.get('sitemap_status')})." + lang)
    limit = (f"The crawler audited {n} URLs within the configured crawl limit; this does not establish that "
             f"the website contains only {n} URLs.")
    if scope == "crawl_limit_reached":
        return limit + lang
    detail = (f"Partial audit ({cov.get('stop_reason')}): {n} URLs crawled, {cov.get('queue_remaining')} of "
              f"{cov.get('discovered_urls')} discovered URLs not crawled. Findings describe crawled URLs only.")
    return ((limit + " " + detail) if cov.get("stop_reason") == "max_pages_reached" else detail) + lang


def _pct(n, total):
    return round(100 * n / total, 2) if total else None


def build_findings(site_issues: list[dict], stats: dict) -> list[dict]:
    counts, summary = stats.get("counts") or {}, stats.get("page_audit_summary") or {}
    total = summary.get("total_pages", counts.get("html_pages_ok", 0))
    out = []
    roots = {r["root_cause"]: dict(r) for r in summary.get("root_causes", [])}
    for i in site_issues:
        rc = _SITE_ROOT_CAUSE.get(i["code"])
        statuses = ((i.get("value") or {}).get("canonical_status") or {}) if isinstance(i.get("value"), dict) else {}
        if rc and rc[0] in roots and set(statuses) == {rc[1]}:
            roots[rc[0]]["evidence"] = {**roots[rc[0]]["evidence"], "site_level_detection": {
                "code": i["code"], "count": i["count"], "message": i["message"]}}
            continue
        denom = _DENOMINATORS.get(i["code"]) or _DENOMINATORS.get(i["category"]) or _PAGES
        base = counts.get(denom.split(".", 1)[1]) if denom.startswith("counts.") else None
        value = i.get("value")
        examples = value.get("urls") if isinstance(value, dict) and "urls" in value else value
        out.append({"code": i["code"], "root_cause": None, "level": "site", "category": i["category"],
                    "severity": i["severity"], "affected": i["count"],
                    "affected_unit": {"site": "site check", "links": "links"}.get(denom, "URLs"),
                    "denominator": None if denom in ("site", "links") else denom,
                    "percentage": _pct(i["count"], base), "evidence": i["message"],
                    "examples": (examples[:5] if isinstance(examples, list) else examples),
                    "drilldown": "issues", "recommendation": i["recommendation"]})
    for name, r in roots.items():
        out.append({"code": name, "root_cause": name, "level": "page_root_cause", "category": "root_cause",
                    "severity": r["severity"], "affected": r["affected_pages"], "affected_unit": "pages",
                    "denominator": _PAGES, "percentage": r["percentage"],
                    "evidence": {"description": r["description"], "related_effects": r["related_effects"],
                                 **r["evidence"]},
                    "examples": r["examples"], "drilldown": r["drilldown"], "recommendation": r["recommendation"]})
    for i in summary.get("issues", []):
        n = i.get("independent_count", i["count"])
        if not n:
            continue  # fully explained by a root cause above
        out.append({"code": i["issue"], "root_cause": None, "level": "page", "category": i["category"],
                    "severity": i["severity"], "affected": n, "affected_unit": "pages", "denominator": _PAGES,
                    "percentage": _pct(n, total),
                    "evidence": {"description": i["description"], "raw_detections": i["count"],
                                 "explained_by_root_cause": i.get("explained_by_root_cause") or {}},
                    "examples": i["examples"], "drilldown": i["drilldown"], "recommendation": i["recommendation"]})
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: (order.get(f["severity"], 3), -(f["affected"] or 0)))


def report_context(stats: dict, site_issues: list[dict] | None = None) -> dict:
    cov = stats.get("crawl") or {}
    counts = stats.get("counts") or {}
    scope = cov.get("audit_scope") or ("full_known_scope" if cov.get("complete") else "partial")
    cov = {**cov, "audit_scope": scope}
    return {"audit_scope": scope, "coverage_confidence": cov.get("coverage_confidence", scope),
            "confidence": cov.get("confidence", "low"),
            "scope_statement": scope_statement(cov, counts), "counts": counts, "crawl": cov,
            "sitemap": stats.get("sitemap"), "robots_status": stats.get("robots_status"),
            "depth_distribution": stats.get("depth_distribution"),
            "findings": build_findings(site_issues or [], stats),
            "page_issues": stats.get("page_audit_summary"), "definitions": DEFINITIONS,
            "reporting_rules": REPORTING_RULES}
