"""Whether an stdio Upstream's command can be found, in one place the Daemon and the CLI share.

A command that is not there is a failure no PATH can fix and no connect attempt explains well:
a cloned repo moved, an npm cache cleared. ``doctor`` says it against the shell it runs in,
``daemon status`` against the PATH the Daemon runs under, and each names the PATH it looked in
because those two can differ (#48).
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcpshape.model import StdioTransport


def command_missing(transport: StdioTransport, path: str | None) -> str | None:
    """The command, when nothing on ``path`` is it; nothing when it can be run.

    ``path`` is the PATH of whoever will spawn the child. A command holding a directory
    component is a path in itself: it is checked as a file and ``path`` is not consulted.
    """
    command = transport.command
    return None if shutil.which(command, path=path) else command
