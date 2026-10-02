from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.services.url_utils import is_web_url


class SeoRequest(BaseModel):
    url: str = Field(..., description="Absolute http(s) URL of the scraped page")
    html: str = Field(..., description="Raw rendered HTML")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not is_web_url(v):
            raise ValueError("url must be an absolute http(s) URL")
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
