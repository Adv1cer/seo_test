from typing import Any, Literal

from pydantic import BaseModel

LinkType = Literal["internal", "external", "anchor", "mailto", "tel", "javascript", "invalid"]
Severity = Literal["critical", "warning", "info", "passed"]
RenderMode = Literal["auto", "never", "always"]


class Link(BaseModel):
    text: str
    href: str
    absolute_url: str | None
    type: LinkType
    nofollow: bool
    is_duplicate: bool  # True if an earlier link had the same (type, normalized url, text)
    location: Literal["nav", "header", "footer", "aside", "main", "body"] = "body"


class Image(BaseModel):
    src: str
    absolute_url: str | None
    alt: str | None  # None = attribute missing, "" = explicitly empty (decorative)
    title: str
    loading: str


class Hreflang(BaseModel):
    lang: str
    href: str
    absolute_url: str | None


class Heading(BaseModel):
    level: int
    text: str


class ParsedPage(BaseModel):
    url: str
    domain: str
    scheme: str
    language: str
    charset: str
    title: str | None
    title_length: int
    meta_description: str | None
    meta_description_length: int
    meta_robots: str | None
    noindex: bool
    nofollow: bool
    canonical: str | None
    canonical_absolute_url: str | None
    viewport: str | None
    og_title: str | None
    og_description: str | None
    og_url: str | None
    og_type: str | None
    og_image: str | None
    twitter_card: str | None
    twitter_title: str | None
    twitter_description: str | None
    twitter_image: str | None
    headings: dict[str, list[str]]
    h1: list[str]
    h2: list[str]
    h3: list[str]
    h1_count: int
    h2_count: int
    heading_order: list[Heading]
    links: list[Link]
    links_total: int  # unique (non-duplicate) links
    links_raw_total: int
    duplicate_links_count: int
    internal_links_count: int
    external_links_count: int
    anchor_links_count: int
    nofollow_links_count: int
    images: list[Image]
    images_total: int
    images_without_alt: int
    images_with_empty_alt: int
    images_with_alt: int
    hreflang: list[Hreflang]
    structured_data_count: int
    structured_data_types: list[str]
    structured_data_errors: list[str]
    body_text: str
    main_content: str
    word_count: int
    main_content_word_count: int
    word_count_is_approximate: bool


class Issue(BaseModel):
    code: str
    category: str
    severity: Severity
    message: str
    value: Any = None
    count: int = 1
    recommendation: str


class Audit(BaseModel):
    score: int
    grade: str
    critical_count: int
    warning_count: int
    info_count: int
    passed_count: int
    issues: list[Issue]


class PageSummary(BaseModel):
    url: str
    domain: str
    title: str | None
    title_length: int
    meta_description: str | None
    canonical: str | None
    language: str
    h1: list[str]
    h2: list[str]
    h3: list[str]
    images_total: int
    images_without_alt: int
    internal_links_count: int
    external_links_count: int
    word_count: int
    main_content_word_count: int


class CrawlInfo(BaseModel):
    status_code: int
    final_url: str
    content_type: str
    x_robots_tag: str | None
    http_duration_ms: int
    render_mode: RenderMode
    render_required: bool
    render_reasons: list[str]
    render_status: Literal["not_needed", "rendered", "failed", "skipped"]
    render_duration_ms: int | None
    render_error: str | None
    source: Literal["raw", "rendered"]
    raw_word_count: int
    rendered_word_count: int | None


class AuditData(BaseModel):
    page: PageSummary
    audit: Audit
    crawl: CrawlInfo | None = None  # only set when the server fetched the page itself


class ParseResponse(BaseModel):
    success: bool = True
    data: ParsedPage


class AuditResponse(BaseModel):
    success: bool = True
    data: AuditData
