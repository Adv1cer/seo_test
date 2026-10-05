import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.services.url_utils import is_web_url


class SeoRequest(BaseModel):
    url: str = Field(..., description="Absolute http(s) URL of the scraped page")
    html: str = Field(..., description="Raw rendered HTML")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        received = v
        v = "".join(v.split())  # URLs never contain whitespace; tolerate "https:// www.x"
        if not is_web_url(v):
            raise ValueError(f"url must be an absolute http(s) URL (received: {received[:200]!r})")
        return v

    @field_validator("html")
    @classmethod
    def _html(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("html must not be empty")
        if len(v.encode("utf-8")) > settings.max_html_bytes:
            raise ValueError(f"html exceeds {settings.max_html_bytes} bytes")
        if "<" not in v:  # catches unresolved workflow templates like "{{node.html}}"
            raise ValueError(f"html does not look like HTML (received: {v.strip()[:100]!r}). "
                             "Check that the workflow variable was substituted.")
        return v


class UrlRequest(BaseModel):
    url: str = Field(..., description="Absolute http(s) URL to fetch and analyze")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        received = v
        v = "".join(v.split())
        if not is_web_url(v):
            raise ValueError(f"url must be an absolute http(s) URL (received: {received[:200]!r})")
        return v


class CrawlOptions(UrlRequest):
    max_pages: int = Field(200, ge=1, le=10_000)
    max_depth: int = Field(5, ge=0, le=50)
    concurrency: int = Field(5, ge=1, le=20)
    request_timeout: float = Field(15, gt=0, le=120)
    same_domain_only: bool = Field(True, description="False also crawls subdomains of the root domain")
    respect_robots_txt: bool = True
    follow_redirects: bool = True
    use_sitemaps: bool = True
    render_javascript: bool = True
    render_strategy: Literal["auto", "never", "always"] = "auto"
    include_patterns: list[str] = Field([], description="Regexes; if set, a URL must match one")
    exclude_patterns: list[str] = Field([], description="Regexes; a matching URL is skipped")

    @field_validator("include_patterns", "exclude_patterns")
    @classmethod
    def _regexes(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}")
        return v
