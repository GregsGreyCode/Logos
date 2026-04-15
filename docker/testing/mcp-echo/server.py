"""Trivial streamable-HTTP MCP server for smoke-testing the deploy pipeline.

Exposes a single tool — ``echo(text: str) -> str`` — that returns its
input verbatim. Zero required config, zero external dependencies at
runtime besides ``aiohttp``. Deployed via the Logos Docker-container
MCP flow to prove the end-to-end path works: ``docker run`` → gateway
auto-wire → agent tool call → container response → back to the agent.

Protocol: implements just enough of the MCP streamable-HTTP transport
to satisfy the gateway's MCP client:
    - initialize
    - notifications/initialized  (no-op 202)
    - tools/list
    - tools/call (echo)

No SSE streaming — the gateway's client is happy with direct
JSON-RPC-in-POST-body responses for tools this simple. Listens on
``$PORT`` (default 8000) at ``/mcp``. ``/health`` returns 200 for
container healthchecks.
"""

import os
from aiohttp import web


PORT = int(os.environ.get("PORT", "8000"))
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "echo-test"
SERVER_VERSION = "0.1"


def _ok(req_id, result):
    return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id, code, message):
    return web.json_response(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


async def handle_mcp(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
            status=400,
        )

    method = body.get("method")
    req_id = body.get("id")

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "notifications/initialized":
        return web.Response(status=202)

    if method == "tools/list":
        return _ok(req_id, {
            "tools": [
                {
                    "name": "echo",
                    "description": (
                        "Return the input text verbatim. Useful for smoke-testing "
                        "the Logos MCP deploy pipeline — if an agent can call this "
                        "and see the string come back, the whole gateway → container "
                        "→ gateway round-trip is working."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "Any string. The tool returns it unchanged.",
                            },
                        },
                        "required": ["text"],
                    },
                },
            ],
        })

    if method == "tools/call":
        params = body.get("params", {}) or {}
        tool = params.get("name")
        args = params.get("arguments", {}) or {}
        if tool == "echo":
            text = str(args.get("text", ""))
            return _ok(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        return _err(req_id, -32601, f"Unknown tool: {tool}")

    return _err(req_id, -32601, f"Method not found: {method}")


async def handle_health(request):
    return web.Response(text="ok", status=200)


def make_app():
    app = web.Application()
    app.router.add_post("/mcp", handle_mcp)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT, print=None)
