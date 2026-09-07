from __future__ import annotations

import os

from hypothesis import settings

from tests.support.seam import config_dir

settings.register_profile("ci", max_examples=200, deadline=None)
settings.register_profile("dev", max_examples=50)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

__all__ = ["config_dir"]
