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
