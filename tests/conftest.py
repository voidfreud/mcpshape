from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import settings

from tests.support.seam import ConfigDir, config_dir

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

settings.register_profile("ci", max_examples=200, deadline=None)
settings.register_profile("dev", max_examples=50)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[ConfigDir]:
    with config_dir(tmp_path) as cfg:
        yield cfg
