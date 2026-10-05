"""Static rule catalogue and scoring constants. Changing a value here changes audit output;
nothing is computed at runtime besides lookups."""

# code: (category, severity, recommendation)
RULES: dict[str, tuple[str, str, str]] = {
    "TITLE_MISSING": ("metadata", "critical", "Add a unique, descriptive <title> element."),
    "TITLE_TOO_SHORT": ("metadata", "warning", "Expand the title to describe the page more fully."),
    "TITLE_TOO_LONG": ("metadata", "warning", "Shorten the title so it is not truncated in search results."),
    "META_DESCRIPTION_MISSING": ("metadata", "warning", "Add a concise meta description describing the page content."),
    "META_DESCRIPTION_TOO_SHORT": ("metadata", "info", "Expand the meta description to summarise the page."),
    "META_DESCRIPTION_TOO_LONG": ("metadata", "info", "Shorten the meta description to avoid truncation."),
    "H1_MISSING": ("headings", "warning", "Add one H1 describing the main topic of the page."),
    "MULTIPLE_H1": ("headings", "warning", "Use a single H1; demote the others to H2/H3."),
    "EMPTY_HEADING": ("headings", "warning", "Remove empty headings or give them text."),
    "HEADING_ORDER_SKIPPED": ("headings", "info", "Avoid skipping heading levels (e.g. H1 -> H3)."),
    "CANONICAL_MISSING": ("canonical", "warning", "Add <link rel=\"canonical\"> pointing to the preferred URL."),
    "CANONICAL_INVALID": ("canonical", "critical", "Set the canonical href to a valid absolute http(s) URL."),
    "CANONICAL_PRIVATE_IP": ("canonical", "critical", "The canonical points to a private/internal host; use the public URL."),
    "CANONICAL_DOMAIN_MISMATCH": ("canonical", "warning", "Confirm the cross-domain canonical is intentional."),
    "CANONICAL_HTTP_WHEN_PAGE_HTTPS": ("canonical", "warning", "Use an https:// canonical on https pages."),
    "HREFLANG_INVALID": ("hreflang", "warning", "Use valid ISO 639-1 language (+ optional ISO 3166 region) codes or x-default, with absolute http(s) URLs."),
    "HREFLANG_PRIVATE_IP": ("hreflang", "critical", "Hreflang alternates point to a private/internal host; use public URLs."),
    "HREFLANG_DOMAIN_MISMATCH": ("hreflang", "info", "Hreflang points to another domain. This can be legitimate (ccTLDs); verify return links exist."),
    "HREFLANG_DUPLICATE_LANGUAGE": ("hreflang", "warning", "Each hreflang value should appear once."),
    "IMAGE_ALT_MISSING": ("images", "warning", "Add alt text to informative images (use alt=\"\" for decorative ones)."),
    "EMPTY_LINK": ("links", "warning", "Give every link accessible text (text, aria-label, or image alt)."),
    "JAVASCRIPT_LINK": ("links", "info", "Use real URLs in href so crawlers can follow links."),
    "INVALID_LINK": ("links", "warning", "Fix links with empty or malformed href values."),
    "NOINDEX_PAGE": ("robots", "warning", "Remove noindex if this page should appear in search results."),
    "NOFOLLOW_PAGE": ("robots", "info", "Remove page-level nofollow if links should pass signals."),
    "OG_TITLE_MISSING": ("open_graph", "info", "Add og:title for better social sharing."),
    "OG_DESCRIPTION_MISSING": ("open_graph", "info", "Add og:description for better social sharing."),
    "OG_IMAGE_MISSING": ("open_graph", "info", "Add og:image for better social sharing."),
    "TWITTER_CARD_MISSING": ("twitter", "info", "Optionally add twitter:card meta tags."),
    "EMPTY_MAIN_CONTENT": ("content", "critical", "The page has no extractable text content."),
    "VERY_THIN_CONTENT": ("content", "warning", "Add substantially more useful content."),
    "THIN_CONTENT": ("content", "info", "Consider expanding the content."),
    "STRUCTURED_DATA_NOT_FOUND": ("structured_data", "info", "Consider adding JSON-LD structured data."),
    "CONTENT_REQUIRES_JS": ("rendering", "warning", "Server-render (SSR/SSG/prerender) the main content, headings and links so crawlers see them without executing JavaScript."),
    "STRUCTURED_DATA_INVALID_JSON": ("structured_data", "warning", "Fix the malformed JSON-LD block(s)."),
}

# Site-level rules produced by the indexability engine after a crawl.
SITE_RULES: dict[str, tuple[str, str, str]] = {
    "SITEMAP_NOT_FOUND": ("sitemap", "warning", "Publish an XML sitemap and reference it from robots.txt with a Sitemap: line."),
    "SITEMAP_EMPTY_OR_INVALID": ("sitemap", "critical", "The sitemap URL responds but is not a valid XML sitemap with <loc> entries. Serve a real XML sitemap (not the app's HTML fallback)."),
    "SITEMAP_NOT_IN_ROBOTS": ("sitemap", "info", "Add a 'Sitemap: <url>' line to robots.txt so crawlers find the sitemap."),
    "SITEMAP_URL_NON_200": ("sitemap", "warning", "Remove URLs that return errors from the sitemap, or fix the pages."),
    "SITEMAP_URL_REDIRECT": ("sitemap", "warning", "List final destination URLs in the sitemap, not redirecting URLs."),
    "SITEMAP_URL_NOINDEX": ("sitemap", "warning", "Remove noindex pages from the sitemap, or remove the noindex if they should rank."),
    "SITEMAP_URL_CANONICALIZED": ("sitemap", "warning", "List only canonical URLs in the sitemap."),
    "SITEMAP_URL_BLOCKED": ("sitemap", "warning", "Sitemap URLs are blocked by robots.txt; unblock them or remove them from the sitemap."),
    "SITEMAP_DUPLICATE_URLS": ("sitemap", "info", "List each URL once across all sitemaps."),
    "PAGES_MISSING_FROM_SITEMAP": ("sitemap", "warning", "Add indexable, self-canonical pages to the sitemap."),
    "NOINDEX_PAGES": ("indexability", "info", "Confirm these pages are intentionally excluded from search."),
    "CANONICALIZED_PAGES": ("indexability", "info", "Confirm these canonicals are intentional; they ask search engines to index another URL."),
    "CANONICAL_TO_NON_INDEXABLE": ("indexability", "critical", "Point canonicals at a live (200), indexable URL on the public site."),
    "SOFT_404_CANDIDATES": ("indexability", "warning", "Pages look like error pages but return 200. Return a real 404/410 status or add real content."),
    "ROBOTS_BLOCKED_PAGES": ("indexability", "info", "Confirm robots.txt blocking is intentional; blocked pages cannot be crawled."),
    "BROKEN_PAGES": ("indexability", "warning", "Fix or remove internal links to pages returning 4xx/5xx or failing to load."),
    "RENDER_FAILED_PAGES": ("indexability", "warning", "Pages failed to render in a browser; check for JavaScript errors or blocked resources."),
}

# Check groups; a group with no fired issues yields one "passed" entry (<GROUP>_OK).
GROUPS = ["metadata", "headings", "canonical", "hreflang", "images", "links", "robots",
          "open_graph", "twitter", "content", "structured_data"]

# Scoring: penalty per issue code = min(BASE * occurrences, CAP); score = clamp(100 - sum).
PENALTY_BASE = {"critical": 15, "warning": 5, "info": 0, "passed": 0}
PENALTY_CAP = {"critical": 30, "warning": 10, "info": 0, "passed": 0}

# Grade thresholds (score >= threshold), checked in order.
GRADES = [(90, "excellent"), (75, "good"), (50, "needs_improvement"), (0, "poor")]

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "passed": 3}
