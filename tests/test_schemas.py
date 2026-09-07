"""The shipped JSON Schemas match the models and are what the files point at."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mcpshape.schemas import KINDS, generate, shipped_path
from tests.support.seam import run_cli

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


@pytest.mark.parametrize("kind", KINDS)
def test_shipped_schema_is_up_to_date(kind: str) -> None:
    shipped = json.loads(shipped_path(kind).read_text())  # type: ignore[arg-type]

    assert shipped == generate(kind), "run: uv run python -m mcpshape.schemas"  # type: ignore[arg-type]


def test_written_files_point_at_the_schema_they_validate_against(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    upstream_dir = config_dir.path / "upstreams" / "github"

    for name, kind in [("upstream.toml", "upstream"), ("default.toml", "proxy")]:
        first_line = (upstream_dir / name).read_text().splitlines()[0]
        assert first_line.endswith(f"/{shipped_path(kind).name}")  # type: ignore[arg-type]
        assert first_line.startswith("#:schema https://")
