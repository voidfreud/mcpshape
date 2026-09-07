"""``python -m mcpshape``: the same command line as the ``mcpshape`` script.

The stdio shim starts the Daemon with ``sys.executable -m mcpshape daemon up``, so this entry
point has to work wherever mcpshape is importable, even when its console script is not on the
Client's PATH.
"""

from __future__ import annotations

from mcpshape.cli import app

if __name__ == "__main__":
    app()
