"""mcpshape: a local proxy that reshapes what MCP servers expose.

What a Proxy's Python file imports: ``hook``, ``tool``, and ``upstream``, with the types their
functions receive and return. See ``mcpshape.hooks``.
"""

from mcpshape.hooks import (
    Call,
    Content,
    Message,
    PromptResult,
    ResourceResult,
    ToolResult,
    UpstreamError,
    hook,
    tool,
    upstream,
)

__all__ = [
    "Call",
    "Content",
    "Message",
    "PromptResult",
    "ResourceResult",
    "ToolResult",
    "UpstreamError",
    "hook",
    "tool",
    "upstream",
]
