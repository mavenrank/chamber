"""The JavaScript chamber injects into every page.

A syntax error here is uniquely nasty: the file is injected as a string, so nothing
fails at import, nothing fails at launch, and the only symptom is that the overlay
silently does not exist. That happened — a backtick inside a CSS comment terminated
the template literal holding the whole stylesheet, and the bar, cursor and highlight
all vanished with one line in the page console as the only clue.

These checks are cheap and catch that class of bug without a browser.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent.parent / "src" / "chamber"
# The React display has its own generated Vite bundle and local npm dependencies.
# They are application assets, not injected Chamber scripts; walking them here
# would make this test lint React/Vite's third-party source as if it were ours.
JS_FILES = sorted(
    path
    for path in JS_DIR.rglob("*.js")
    if "node_modules" not in path.parts and "dist" not in path.parts
)


def test_there_are_injected_scripts_to_check():
    assert JS_FILES, "expected overlay.js, extract.js and ready.js"


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_parses(path: Path):
    """Syntax-check with node when it is available."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run(
        [node, "--check", str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, f"{path.name} does not parse:\n{result.stderr}"


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_template_literals_are_balanced(path: Path):
    """The node-free guard for the bug that actually happened.

    Every CSS block here lives inside a template literal, and a stray backtick —
    in a comment, in prose — closes it early. Deciding whether a given backtick is
    *inside* a literal needs a real parser, which `test_parses` uses when node is
    available. This is the cheap half: an odd count means one is unterminated,
    which is exactly the shape of that failure.
    """
    source = re.sub(r"\\`", "", path.read_text(encoding="utf-8"))
    count = source.count("`")
    assert count % 2 == 0, (
        f"{path.name} has {count} backticks — an odd number means a template "
        "literal is left open, which silently truncates the injected script."
    )


# Not every injected script is a .js file. `detect.py` and `overlay_block.py` hold theirs as Python string constants, and those run in the page
# exactly like the files above do — with the same silent failure when they are
# malformed. Nothing else would catch it: the string imports fine, and a broken probe
# just quietly reports that nothing is covering the page.
PY_EMBEDDED = [
    (JS_DIR / "interrupt" / "detect.py", "_DETECT_JS"),
    (JS_DIR / "interrupt" / "overlay_block.py", "_PROBE_JS"),
]


def _extract(path: Path, name: str) -> str:
    """Pull one `NAME = \"\"\"...\"\"\"` (or r-string) constant out of a module."""
    source = path.read_text(encoding="utf-8")
    match = re.search(
        rf'^{re.escape(name)}\s*=\s*r?"""(.*?)"""', source, re.DOTALL | re.MULTILINE
    )
    assert match, f"{path.name} no longer defines {name} as a triple-quoted string"
    return match.group(1)


@pytest.mark.parametrize(
    "path,name", PY_EMBEDDED, ids=lambda v: v.name if isinstance(v, Path) else v
)
def test_embedded_js_parses(path: Path, name: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    body = _extract(path, name)
    # These are all bare arrow functions, which are not statements on their own.
    # `encoding` is not optional: these probes match on "×" and "✕", and on Windows
    # a text-mode pipe defaults to cp1252, which cannot encode either.
    result = subprocess.run(
        [node, "--check", "-"],
        input=f"void ({body});",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"{path.name}:{name} does not parse:\n{result.stderr}"


def test_overlay_exposes_the_control_api():
    """The Take Control button is the one piece of UI with real consequences —
    it stops the agent. If these names drift, the Python side silently no-ops."""
    source = (JS_DIR / "overlay" / "overlay.js").read_text(encoding="utf-8")
    for name in ("toggleControl", "isControlled", "__chamberOnControl", "say"):
        assert name in source, f"overlay.js no longer defines {name}"
