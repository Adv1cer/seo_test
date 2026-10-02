import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    max_html_bytes: int = _int("SEO_MAX_HTML_BYTES", 10 * 1024 * 1024)
    title_min: int = _int("SEO_TITLE_MIN", 30)
    title_max: int = _int("SEO_TITLE_MAX", 60)
    meta_desc_min: int = _int("SEO_META_DESC_MIN", 70)
    meta_desc_max: int = _int("SEO_META_DESC_MAX", 160)
    very_thin_words: int = _int("SEO_VERY_THIN_WORDS", 100)
    thin_words: int = _int("SEO_THIN_WORDS", 300)


settings = Settings()
