"""Text extraction and Unicode-aware (Thai-compatible) approximate word counting."""
import math
import re

from bs4 import BeautifulSoup, Tag

NOISE_TAGS = ["script", "style", "svg", "canvas", "noscript", "template", "iframe", "object"]
BOILERPLATE_TAGS = ["nav", "header", "footer"]

# Thai is written without spaces between words. Without a dictionary segmenter we
# approximate: one word per THAI_CHARS_PER_WORD Thai code points (incl. combining marks).
THAI_CHARS_PER_WORD = 5
_THAI = "฀-๿"
_TOKEN_RE = re.compile(rf"[{_THAI}]+|[^\s{_THAI}]+")
_WS_RE = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()


def text_of(node: Tag | None) -> str:
    return normalize_ws(node.get_text(" ")) if node is not None else ""


_BOILERPLATE_ROLES = {"navigation", "banner", "contentinfo", "complementary"}


def extract_content(soup: BeautifulSoup) -> dict:
    """Deterministic content extraction. Call after strip_noise; mutates the soup (removes boilerplate).
    content_text: body minus nav, and header/footer/aside outside <main>/<article>.
    main_text: all top-level <main>/role=main regions joined (homepages often have several), else a single
    <article>, else content_text. confidence: high only when a semantic region holds >= 50% of the content
    text; otherwise thin-content checks must not rely on main_text alone."""
    body = soup.body or soup
    for tag in body.find_all(True):
        if tag.decomposed:
            continue
        role = (tag.get("role") or "").lower()
        inside = tag.find_parent(["main", "article"]) is not None
        if tag.name == "nav" or role == "navigation" or (
                (tag.name in ("header", "footer", "aside") or role in _BOILERPLATE_ROLES) and not inside):
            tag.decompose()
    content_text = text_of(body)
    mains = [m for m in body.find_all(lambda t: t.name == "main" or (t.get("role") or "").lower() == "main")
             if m.find_parent(lambda p: p.name == "main" or (p.get("role") or "").lower() == "main") is None]
    articles = body.find_all("article")
    if mains:
        method, main_text = f"main_elements({len(mains)})", normalize_ws(" ".join(text_of(m) for m in mains))
    elif len(articles) == 1:
        method, main_text = "single_article", text_of(articles[0])
    else:
        method, main_text = "body_without_boilerplate", content_text
    return {"content_text": content_text, "main_text": main_text, "method": method}


def main_content_node(soup: BeautifulSoup) -> Tag | None:
    """Pick <main>, then a single <article>, else <body>. Mutates: removes nav (always) and
    header/footer only when falling back to body, so article bylines/titles survive."""
    node = soup.find("main") or soup.find(attrs={"role": "main"})
    if node is None:
        articles = soup.find_all("article")
        node = articles[0] if len(articles) == 1 else None
    remove = ["nav"]
    if node is None:
        node = soup.body or soup
        remove = BOILERPLATE_TAGS
    for tag in node.find_all(remove):
        tag.decompose()
    for tag in node.find_all(attrs={"role": "navigation"}):
        tag.decompose()
    return node


def contains_thai(text: str) -> bool:
    return re.search(f"[{_THAI}]", text) is not None


_LANG_SEGMENT = re.compile(r"^/([a-z]{2})(?:[-_][a-z]{2})?(?:/|$)", re.I)


def detect_language(html_lang: str, url_path: str, text: str) -> tuple[str, str]:
    """(primary language subtag, signal used). Order: <html lang>, URL language segment (/en/, /th-th/),
    then script dominance of the text. '' when nothing is reliable."""
    if html_lang:
        return html_lang.split("-")[0].split("_")[0].lower(), "html_lang"
    m = _LANG_SEGMENT.match(url_path or "")
    if m:
        return m.group(1).lower(), "url_segment"
    letters = [c for c in text if c.isalpha()]
    if letters:
        thai = sum("฀" <= c <= "๿" for c in letters) / len(letters)
        return ("th", "script_ratio") if thai > 0.5 else ("", "unknown")
    return "", "unknown"


def word_count_method(lang: str, text: str) -> tuple[str, bool]:
    """(method description, approximate?). Thai has no spaces between words: its runs are estimated at
    THAI_CHARS_PER_WORD chars/word. The count is 'approximate' only when Thai runs are a material part
    (> 20% of counted words) of the text, e.g. a Thai page, not an English page with a Thai footer."""
    thai_words = sum(math.ceil(len(t) / THAI_CHARS_PER_WORD) for t in re.findall(f"[{_THAI}]+", text))
    total = count_words(text) or 1
    if lang == "th" or thai_words / total > 0.2:
        return f"whitespace tokens + Thai runs estimated at {THAI_CHARS_PER_WORD} chars/word", True
    return "whitespace-delimited tokens containing a letter or digit", False


def count_words(text: str) -> int:
    """Approximate word count. Non-Thai: whitespace tokens containing a letter/digit.
    Thai runs: ceil(len / THAI_CHARS_PER_WORD). Not a linguistic segmentation."""
    total = 0
    for tok in _TOKEN_RE.findall(text):
        if "฀" <= tok[0] <= "๿":
            total += math.ceil(len(tok) / THAI_CHARS_PER_WORD)
        elif any(c.isalnum() for c in tok):
            total += 1
    return total
