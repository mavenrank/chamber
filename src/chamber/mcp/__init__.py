"""MCP front-end. Import is deferred so the core works without the `mcp` extra."""

__all__ = ["serve"]


def __getattr__(name: str):  # pragma: no cover - thin import shim
    if name == "serve":
        from chamber.mcp.server import serve

        return serve
    raise AttributeError(name)
