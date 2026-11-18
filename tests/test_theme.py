# The theme, held to numbers: the tokens are read out of the stylesheet and
# every text-on-surface pair the pages use is checked against WCAG AA; a hex
# colour anywhere else, an over-weight page or a request to another origin fails.
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "pharmacli"
CSS = (SRC / "theme.css").read_text(encoding="utf-8")
TOKENS = dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})\b", CSS))
HEX = re.compile(r"#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3,4})\b")

# each page and the most it may weigh with the CSS and JavaScript it loads,
# uncompressed (decision D8)
PAGES = {"chat.html": 30_000, "login.html": 60_000, "pharmacist.html": 60_000,
         "patient.html": 60_000}
FILES = sorted(p for p in SRC.iterdir() if p.suffix in (".html", ".css", ".js"))

TEXT = ("ink", "ink-muted", "green", "green-strong", "danger", "caution")
LIGHT = ("surface", "surface-tint")      # what text sits on
DARK = ("green", "green-strong")         # carries white text only: primary buttons
EDGES = ("line", "green")                # input borders and focus rings


def luminance(colour: str) -> float:
    def linear(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def ratio(text: str, surface: str) -> float:
    return contrast(TOKENS[text], TOKENS[surface])


def tthecontrastformulaisthewcagone():
    assert round(contrast("#FFFFFF", "#000000"), 2) == 21.0
    # the two traps the brief names: both look fine and neither passes
    assert round(contrast("#6B7280", "#DCFCE7"), 2) == 4.40
    assert round(contrast("#22C55E", "#FFFFFF"), 2) == 2.28


def teverytextcolourmeetsaaonthesurfaceitsitson():
    for surface in LIGHT:
        for text in TEXT:
            assert ratio(text, surface) >= 4.5, f"{text} on {surface}: {ratio(text, surface):.2f}"
        for edge in EDGES:
            assert ratio(edge, surface) >= 3.0, f"{edge} on {surface}: {ratio(edge, surface):.2f}"
    for surface in DARK:
        assert ratio("surface", surface) >= 4.5, f"white on {surface}"


def rules(text: str) -> list[str]:
    # the bodies of every style rule in a stylesheet or a page's <style> block
    if "<style" in text:
        text = "".join(re.findall(r"<style>(.*?)</style>", text, re.S))
    return re.findall(r"\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", text, flags=re.S))


def tthepagesuseonlythepairscheckedabove():
    # a colour not in the tables above, or dark text on a dark surface, would be
    # a pair nothing measured
    for path in FILES:
        if path.suffix == ".js":
            continue
        for body in rules(path.read_text(encoding="utf-8")):
            used = set(re.findall(r"var\(--([\w-]+)\)", body))
            assert used <= set(TOKENS), f"{path.name}: unknown token in {body!r}"
            fg = re.findall(r"(?<![\w-])color:\s*var\(--([\w-]+)\)", body)
            bg = re.findall(r"background(?:-color)?:\s*var\(--([\w-]+)\)", body)
            assert set(bg) <= set(LIGHT + DARK), f"{path.name}: {body!r}"
            if set(bg) & set(DARK):
                assert fg == ["surface"], f"{path.name}: a dark surface needs white text: {body!r}"
            else:
                assert set(fg) <= set(TEXT), f"{path.name}: {body!r}"


def thexcoloursappearonlyinthetokenstylesheet():
    assert len(TOKENS) == 9
    for path in FILES:
        found = HEX.findall(path.read_text(encoding="utf-8"))
        if path.name == "theme.css":
            assert sorted(found) == sorted(TOKENS.values())     # the tokens and nothing else
        else:
            assert not found, f"{path.name} writes a colour outside the tokens: {found}"


def loaded(page: str) -> list[str]:
    # the stylesheet and scripts a page pulls in (a download link is not a load)
    return re.findall(r'<(?:link|script)\b[^>]*(?:href|src)="/static/([\w.-]+)"',
                      (SRC / page).read_text(encoding="utf-8"))


@pytest.mark.parametrize("page", PAGES)
def teachpagestayswithinitsweight(page):
    weight = sum((SRC / name).stat().st_size for name in [page, *loaded(page)])
    assert weight <= PAGES[page], f"{page} loads {weight:,} bytes"
    assert "theme.css" in loaded(page)                # one stylesheet, linked by every page


def tnothingisrequestedfromanotherorigin():
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"https?:|@import|url\(", text), path.name
        for target in re.findall(r'(?:href|src|action)="([^"]*)"', text):
            assert target.startswith("/") and not target.startswith("//"), f"{path.name}: {target}"


@pytest.mark.parametrize("page", PAGES)
def teachpagehasalanguagealandmarkandlabelledinputs(page):
    html = (SRC / page).read_text(encoding="utf-8")
    assert '<html lang="en">' in html and "<main" in html and "<h1" in html
    assert 'name="viewport"' in html
    labelled = set(re.findall(r'<label[^>]*\bfor="([\w-]+)"', html))
    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
        ident = re.search(r'\bid="([\w-]+)"', tag)
        assert ident and ident.group(1) in labelled, f"{page}: no label for {tag}"
    # headings never skip a level
    levels = [int(n) for n in re.findall(r"<h([1-6])\b", html)]
    assert all(b - a <= 1 for a, b in zip(levels, levels[1:], strict=False)), f"{page}: {levels}"


def tthechatmarksananswerbusywhileitstreams():
    html = (SRC / "chat.html").read_text(encoding="utf-8")
    assert "setAttribute('aria-busy', 'true')" in html
    assert "setAttribute('aria-busy', 'false')" in html
    assert 'aria-live="polite"' in html


def ttheapiservesthestylesheetandnothingelsefromthefolder(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PHARMACLI_MODEL", str(tmp_path / "absent.joblib"))
    monkeypatch.setenv("PHARMACLI_DB", str(tmp_path / "absent.db"))
    from pharmacli import serve

    with TestClient(serve.app) as c:
        r = c.get("/static/theme.css")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
        assert "--green:" in r.text
        for name in {n for page in PAGES for n in loaded(page)}:
            assert c.get(f"/static/{name}").status_code == 200, name
        assert c.get("/static/serve.py").status_code == 404
        assert c.get("/static/..%2Fserve.py").status_code == 404
