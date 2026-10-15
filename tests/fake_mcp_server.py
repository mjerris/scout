"""A small MCP server for the external-MCP tests (extmcp.py): stdio, or streamable HTTP on
127.0.0.1 with an optional bearer token.

    python tests/fake_mcp_server.py stdio
    python tests/fake_mcp_server.py http PORT [TOKEN]

Tools: two reads (marked readOnlyHint), one write with no hints, one destructive write.
The writes only append to a list in memory; `calls` says what was called, so a test can
check that a refused call never reached the server."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("fake", instructions="A fake server for Scout's tests.")
CALLED: list[str] = []
# A stdio server's lifetime is one client's, so "calls" can't see other connections;
# FAKE_MCP_LOG (a file) keeps every call for the test to read.
LOG = os.environ.get("FAKE_MCP_LOG", "")


def _note(name: str) -> None:
    CALLED.append(name)
    if LOG:
        with Path(LOG).open("a") as fh:
            fh.write(name + "\n")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def lookup(id: str) -> str:
    """Look up a widget by id."""
    _note("lookup")
    return f"widget {id}: blue, 3 parts"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def count() -> str:
    """How many widgets there are."""
    _note("count")
    return "42 widgets"


@server.tool()
def file_thing(title: str, body: str = "") -> str:
    """File a new widget report."""
    _note("file_thing")
    return f"filed {title!r}"


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
def wipe() -> str:
    """Delete every widget."""
    _note("wipe")
    return "wiped"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def calls() -> str:
    """What was called on this server so far."""
    return ",".join(CALLED) or "none"


def _http(port: int, token: str) -> None:
    import uvicorn
    from starlette.types import ASGIApp, Receive, Scope, Send

    app = server.streamable_http_app(stateless_http=True, json_response=True)

    def guarded(inner: ASGIApp) -> ASGIApp:
        async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
            if token and scope["type"] == "http":
                headers = dict(scope.get("headers") or [])
                if headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                    body = b'{"error": "unauthorized"}'
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [(b"content-type", b"application/json")],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return
            await inner(scope, receive, send)

        return wrapped

    uvicorn.run(guarded(app), host="127.0.0.1", port=port, log_level="warning")


def main(argv: list[str]) -> Any:
    if argv[1:2] == ["http"]:
        _http(int(argv[2]), argv[3] if len(argv) > 3 else "")
    else:
        server.run("stdio")


if __name__ == "__main__":
    main(sys.argv)
