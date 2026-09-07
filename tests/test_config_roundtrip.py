"""Any TOML file survives a rewrite of one key with comments and key order intact.

This is the tomlkit contract mcpshape's CLI edits rest on, tested at the config module
because no CLI verb edits a key yet (the first arrives with Overrides). A seam exception
recorded in CLAUDE.md.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import tomlkit
from hypothesis import given
from hypothesis import strategies as st

from mcpshape.config import rewrite

KEY = st.from_regex(r"[a-z][a-z0-9_]{0,11}", fullmatch=True)
COMMENT = st.from_regex(r"[A-Za-z0-9 ,.!?:;()'-]{0,40}", fullmatch=True)
SCALAR = st.one_of(
    st.integers(min_value=-(10**9), max_value=10**9),
    st.booleans(),
    st.from_regex(r"[A-Za-z0-9 _./:-]{0,30}", fullmatch=True),
    st.lists(st.integers(min_value=0, max_value=99), max_size=4),
)


@st.composite
def toml_texts(draw: st.DrawFn) -> tuple[str, list[str]]:
    """A TOML document with hand-written comments between and beside its keys."""
    keys = draw(st.lists(KEY, min_size=1, max_size=6, unique=True))
    lines: list[str] = []
    for key in keys:
        if draw(st.booleans()):
            lines.append(f"# {draw(COMMENT)}")
        value = tomlkit.item(draw(SCALAR)).as_string()
        trailing = f"  # {draw(COMMENT)}" if draw(st.booleans()) else ""
        lines.append(f"{key} = {value}{trailing}")
    if draw(st.booleans()):
        lines.append(f"\n# {draw(COMMENT)}")
        lines.append("[table]")
        lines.append(f"{draw(KEY)} = 1")
    return "\n".join(lines) + "\n", keys


def comments_and_keys(text: str) -> list[str]:
    """Comment lines and top-level keys in the order they appear."""
    found: list[str] = []
    for line in text.splitlines():
        if line.startswith("["):
            break
        if match := re.match(r"^([a-z][a-z0-9_]*) =", line):
            found.append(match.group(1))
        if "#" in line:
            found.append(line[line.index("#") :].strip())
    return found


@given(toml_texts(), KEY)
def test_rewriting_one_key_keeps_comments_and_key_order(
    document: tuple[str, list[str]], edited: str
) -> None:
    text, keys = document
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "file.toml"
        path.write_text(text)
        before = comments_and_keys(text)

        rewrite(path, lambda doc: doc.__setitem__(edited, "changed"))

        after = comments_and_keys(path.read_text())
        rewritten = path.read_text()
    expected = before if edited in keys else [*before, edited]
    assert after == expected
    assert tomlkit.parse(rewritten)[edited] == "changed"
