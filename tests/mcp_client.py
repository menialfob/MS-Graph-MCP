"""Drive the server through a real MCP client session over in-memory streams.

Testing `MCPServer.call_tool` directly skips the protocol: it raises exceptions
where the wire contract returns `CallToolResult(isError=true)`, and it never
exercises initialize, capability negotiation or schema validation. These tests
are about specification conformance, so they go through a client session.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams


@asynccontextmanager
async def client_for(server):
    """Yield an initialized ClientSession connected to `server`."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:

            async def run_server() -> None:
                await server._lowlevel_server.run(
                    server_read,
                    server_write,
                    server._lowlevel_server.create_initialization_options(),
                    raise_exceptions=True,
                )

            tg.start_soon(run_server)
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()


def structured(result) -> dict[str, Any]:
    assert not result.is_error, _text(result)
    assert result.structured_content is not None, "tool returned no structured content"
    return result.structured_content


def failure(result) -> str:
    assert result.is_error, f"expected an error result, got: {result.structured_content}"
    return _text(result)


def _text(result) -> str:
    return " ".join(c.text for c in result.content if getattr(c, "type", "") == "text")


@asynccontextmanager
async def client_as(server, subject: str, scopes: tuple[str, ...] = ()):
    """A client session whose requests are attributed to `subject`.

    Simulates what the auth middleware does in a hosted deployment: each
    request arrives carrying a different authenticated user. Patching
    `current_caller` is the smallest faithful stand-in for a verified token.
    """
    import graph_mcp.server as server_module
    from graph_mcp.caller import Caller

    original = server_module.current_caller
    server_module.current_caller = lambda fallback=None: Caller(
        subject=subject, scopes=scopes
    )
    try:
        async with client_for(server) as session:
            yield session
    finally:
        server_module.current_caller = original
