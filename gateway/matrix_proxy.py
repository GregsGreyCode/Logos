"""The dashboard passes Matrix client requests through to the Matrix server (story M1).

A person's browser is an encrypted Matrix device of theirs (docs/matrix-chat.md). It reaches the
Matrix server through the dashboard, at the dashboard's own address, so chat works wherever the
dashboard works, Tailscale or not. Only ``/_matrix/client/`` and ``/_matrix/media/`` (and the v1
media API) are passed on, and only for someone signed in to the dashboard. The Matrix access token
travels in the request's own ``Authorization`` header, from the browser to the Matrix server; Logos
neither reads nor keeps it.

A dashboard session that has expired is answered ``503`` (try again), never ``401``: to the Matrix
client a 401 means its own login was rejected, and it would sign the device out. The page renews the
dashboard session by itself within seconds and the Matrix client retries.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import aiohttp
from aiohttp import web

from gateway.auth.middleware import TOKEN_EXPIRED, get_user_from_request

ALLOWED = ("/_matrix/client/", "/_matrix/media/")
PASS_HEADERS = ("Authorization", "Content-Type", "Content-Length", "Accept")
BACK_HEADERS = ("Content-Type", "Content-Disposition", "Cache-Control", "Retry-After")
TIMEOUT = aiohttp.ClientTimeout(total=120)          # /sync waits up to 30 s; uploads can take longer
MAX_BODY = 50 * 1024 * 1024


def _try_again(text: str) -> web.Response:
    return web.json_response({"errcode": "M_UNKNOWN", "error": text}, status=503, headers={"Retry-After": "5"})


def matrix_url(app: web.Application) -> Optional[str]:
    """Where the dashboard reaches the Matrix server (the agents' address), found once."""
    if "matrix_url" not in app:
        from gateway import agent_chat
        from gateway.matrix_host import MatrixHost
        runtime = agent_chat.runtime_of(app.get("sandbox_backend"))
        app["matrix_url"] = MatrixHost(runtime).url() if runtime is not None else None
    return app["matrix_url"]


async def handle(request: web.Request) -> web.StreamResponse:
    user = get_user_from_request(request)
    if user is None or user is TOKEN_EXPIRED:
        return _try_again("Sign in to Logos again; this retries by itself.")
    if not request.path.startswith(ALLOWED):
        return web.json_response({"errcode": "M_NOT_FOUND", "error": "Not passed on by Logos"}, status=404)
    base = await asyncio.to_thread(matrix_url, request.app)
    if not base:
        return _try_again("The Matrix server is not set up here.")
    # Read from the stream ourselves: request.read() stops at the app's 1 MiB limit, and photos and
    # files sent from the browser are bigger than that. MAX_BODY is the limit here.
    body = None
    if request.can_read_body:
        chunks, size = [], 0
        async for chunk in request.content.iter_chunked(1 << 16):
            size += len(chunk)
            if size > MAX_BODY:
                return web.json_response({"errcode": "M_TOO_LARGE", "error": "Too large"}, status=413)
            chunks.append(chunk)
        body = b"".join(chunks)
    session: aiohttp.ClientSession = request.app.setdefault("matrix_session", None) or aiohttp.ClientSession(timeout=TIMEOUT)
    request.app["matrix_session"] = session
    headers = {k: v for k, v in request.headers.items() if k in PASS_HEADERS and k != "Content-Length"}
    try:
        async with session.request(request.method, base + request.path_qs, data=body, headers=headers,
                                   allow_redirects=False) as r:
            data = await r.read()
            return web.Response(status=r.status, body=data,
                                headers={k: v for k, v in r.headers.items() if k in BACK_HEADERS})
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return _try_again("The Matrix server did not answer.")


async def _close(app: web.Application) -> None:
    session: Any = app.get("matrix_session")
    if session is not None:
        await session.close()


def register(app: web.Application) -> None:
    app.router.add_route("*", "/_matrix/{tail:.*}", handle)
    app.on_cleanup.append(_close)
