"""JSON Schemas for the config files, generated from the models and shipped for editors.

Regenerate with ``python -m mcpshape.schemas`` after changing a file model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

from mcpshape.config import FILE_MODELS, FileKind

SCHEMAS_DIR = Path(__file__).parent
KINDS: tuple[FileKind, ...] = get_args(FileKind)


def generate(kind: FileKind) -> dict[str, Any]:
    schema = FILE_MODELS[kind].json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://raw.githubusercontent.com/voidfreud/mcpshape/main/src/mcpshape/schemas/{kind}.schema.json",
        "title": f"mcpshape {kind} file",
        **schema,
    }


def shipped_path(kind: FileKind) -> Path:
    return SCHEMAS_DIR / f"{kind}.schema.json"


def render(kind: FileKind) -> str:
    return json.dumps(generate(kind), indent=2) + "\n"


def write_all() -> None:
    for kind in KINDS:
        shipped_path(kind).write_text(render(kind))
