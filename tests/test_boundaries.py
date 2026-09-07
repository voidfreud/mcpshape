"""Architectural rules that must hold across the source tree."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "mcpshape"
FASTMCP_ADAPTER = SRC / "adapters" / "fastmcp.py"
FASTMCP_IMPORT = re.compile(r"^\s*(from|import)\s+(fastmcp|mcp)\b", re.MULTILINE)


def test_only_the_fastmcp_adapter_imports_fastmcp() -> None:
    """ADR 0001: every FastMCP touchpoint goes through the adapter."""
    offenders = [
        path.relative_to(SRC)
        for path in SRC.rglob("*.py")
        if path != FASTMCP_ADAPTER and FASTMCP_IMPORT.search(path.read_text())
    ]
    assert offenders == []
