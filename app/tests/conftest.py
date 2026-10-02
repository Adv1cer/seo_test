from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return html.replace("{{LOREM}}", " ".join(f"word{i}" for i in range(350)))


def page(head: str = "", body: str = "", lang: str = "en") -> str:
    return f"<!DOCTYPE html><html lang='{lang}'><head>{head}</head><body>{body}</body></html>"


@pytest.fixture
def good_html() -> str:
    return load("good_page.html")


@pytest.fixture
def utcc_html() -> str:
    return load("ai_utcc_style.html")
