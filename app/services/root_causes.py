"""Root-cause grouping over per-page audits: when several rule detections on a page stem from one defect,
report the defect once (with the detections attached as effects) instead of as independent problems.
Raw detections are untouched; this only decides which (page, code) pairs a root cause explains."""
from collections import Counter
from urllib.parse import urlsplit

_METADATA_MISSING = ("CANONICAL_MISSING", "META_DESCRIPTION_MISSING", "OG_TITLE_MISSING", "OG_DESCRIPTION_MISSING")

ROOT_CAUSES = {
    "PRIVATE_BASE_URL_CONFIGURATION": {
        "severity": "critical",
        "description": "Head tags (canonical and/or hreflang) are generated with a private/internal base URL "
                       "instead of the public site origin.",
        "related_effects": ["canonical URL is not publicly reachable (CANONICAL_PRIVATE_IP)",
                            "canonical uses HTTP while the page uses HTTPS (CANONICAL_HTTP_WHEN_PAGE_HTTPS)",
                            "canonical target cannot be indexed publicly (CANONICAL_TO_NON_INDEXABLE)",
                            "hreflang alternates point to the private host, so language versions are not "
                            "connected for search engines (HREFLANG_PRIVATE_IP)"],
        "recommendation": "Fix the site/base URL setting used to build head tags so canonical and hreflang URLs use "
                          "the public https:// origin (e.g. https://ai.example.com). One configuration fix resolves "
                          "all related detections.",
    },
    "ROUTE_HEAD_METADATA_MISSING": {
        "severity": "warning",
        "description": "Route renders no page-specific head metadata, even after JavaScript rendering; it inherits "
                       "the generic app shell head.",
        "related_effects": ["no canonical", "no meta description", "no og:title / og:description",
                            "generic shell title, often duplicated across routes"],
        "recommendation": "Add per-route head metadata (title, description, canonical, og:*) for this route, "
                          "preferably rendered on the server.",
    },
}


def _codes(page: dict) -> dict[str, dict]:
    return {i["code"]: i for i in (page.get("audit") or {}).get("issues", []) if i["severity"] != "passed"}


def explain(pages: list[dict]) -> tuple[dict[str, list[dict]], dict[tuple[str, str], str]]:
    """Returns ({root_cause: [per-page evidence]}, {(url, code): root_cause})."""
    hits: dict[str, list[dict]] = {k: [] for k in ROOT_CAUSES}
    explained: dict[tuple[str, str], str] = {}
    for p in pages:
        codes, url = _codes(p), p["url"]
        if "CANONICAL_PRIVATE_IP" in codes or "HREFLANG_PRIVATE_IP" in codes:
            canonical = codes["CANONICAL_PRIVATE_IP"].get("value") if "CANONICAL_PRIVATE_IP" in codes else None
            hreflang = (codes.get("HREFLANG_PRIVATE_IP") or {}).get("value") or []
            hosts = {urlsplit(u).hostname for u in [canonical] + [h.get("href") for h in hreflang
                                                                  if isinstance(h, dict)] if u}
            # HTTP-canonical is only this root cause's effect when the canonical itself is the private one.
            members = [c for c in ("CANONICAL_PRIVATE_IP", "HREFLANG_PRIVATE_IP") if c in codes] +                       [c for c in ("CANONICAL_HTTP_WHEN_PAGE_HTTPS",) if c in codes and canonical]
            hits["PRIVATE_BASE_URL_CONFIGURATION"].append({"url": url, "canonical": canonical, "codes": members,
                                                           "hosts": sorted(h for h in hosts if h)})
            explained.update({(url, c): "PRIVATE_BASE_URL_CONFIGURATION" for c in members})
        missing = [c for c in _METADATA_MISSING if c in codes]
        sources = (p.get("render_info") or {}).get("metadata_source") or {}
        rendered = (p.get("render_info") or {}).get("render_status") == "rendered"
        # Only claim a route/head defect when the final rendered DOM was checked and still lacks the tags.
        if len(missing) >= 2 and rendered:
            members = missing
            hits["ROUTE_HEAD_METADATA_MISSING"].append({"url": url, "codes": members, "title": p.get("title"),
                                                        "metadata_source": sources})
            explained.update({(url, c): "ROUTE_HEAD_METADATA_MISSING" for c in members})
    return {k: v for k, v in hits.items() if v}, explained


def summarize(hits: dict[str, list[dict]], total: int, path) -> list[dict]:
    out = []
    for name, rows in hits.items():
        rc = ROOT_CAUSES[name]
        evidence = {"rule_detections": dict(Counter(c for r in rows for c in r["codes"]))}
        if name == "PRIVATE_BASE_URL_CONFIGURATION":
            evidence["private_hosts"] = dict(Counter(h for r in rows for h in r["hosts"]))
        out.append({
            "root_cause": name, "severity": rc["severity"], "description": rc["description"],
            "affected_pages": len(rows), "total_pages": total,
            "percentage": round(100 * len(rows) / total, 2) if total else 0.0,
            "related_effects": rc["related_effects"], "evidence": evidence,
            "examples": [{"url": path(r["url"]), **{k: v for k, v in r.items() if k != "url"}}
                         for r in rows[:5]],
            "drilldown": f"pages?issue={rows[0]['codes'][0]}&include_audit=true",
            "recommendation": rc["recommendation"],
        })
    return out
