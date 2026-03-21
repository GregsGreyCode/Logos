"""REST handlers for server-side chat session persistence."""

import asyncio
import json
import logging
import os

import aiohttp
from aiohttp import web

import gateway.auth.db as auth_db

logger = logging.getLogger(__name__)

_OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
_OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
_EMBED_MODEL     = os.environ.get("EMBED_MODEL", "text-embedding-3-small")


async def _get_embedding(text: str) -> list | None:
    """Call the embedding endpoint. Returns None on failure (graceful degradation)."""
    if not _OPENAI_BASE_URL or not _OPENAI_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_OPENAI_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {_OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": _EMBED_MODEL, "input": text[:8000]},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    return d["data"][0]["embedding"]
    except Exception as e:
        logger.debug("Embedding failed: %s", e)
    return None


async def handle_chats_list(request: web.Request) -> web.Response:
    user = request["current_user"]
    chats = auth_db.list_chats(user["sub"])
    return web.json_response({"chats": chats})


async def handle_chats_post(request: web.Request) -> web.Response:
    user = request["current_user"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name", "New conversation")
    chat = auth_db.create_chat(user["sub"], name)
    return web.json_response({"chat": chat}, status=201)


async def handle_chat_get(request: web.Request) -> web.Response:
    user = request["current_user"]
    cid = request.match_info["id"]
    chat = auth_db.get_chat(cid)
    if not chat or chat["user_id"] != user["sub"]:
        raise web.HTTPNotFound(reason="chat_not_found")
    chat["messages"] = json.loads(chat.get("messages", "[]"))
    return web.json_response({"chat": chat})


async def handle_chat_patch(request: web.Request) -> web.Response:
    user = request["current_user"]
    cid = request.match_info["id"]
    chat = auth_db.get_chat(cid)
    if not chat or chat["user_id"] != user["sub"]:
        raise web.HTTPNotFound(reason="chat_not_found")
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid_json")

    if "name" in body:
        auth_db.update_chat_name(cid, body["name"][:120])
    if "messages" in body:
        messages = body["messages"]
        auth_db.save_chat_messages(cid, messages)
        # Embed the last message in the background if it's new
        if messages:
            last = messages[-1]
            content = last.get("content", "")
            role = last.get("role", "user")
            if content:
                asyncio.ensure_future(_embed_and_store(cid, user["sub"], role, content))

    return web.json_response({"ok": True})


async def handle_chat_delete(request: web.Request) -> web.Response:
    user = request["current_user"]
    cid = request.match_info["id"]
    chat = auth_db.get_chat(cid)
    if not chat or chat["user_id"] != user["sub"]:
        raise web.HTTPNotFound(reason="chat_not_found")
    auth_db.delete_chat(cid)
    return web.Response(status=204)


async def handle_chats_search(request: web.Request) -> web.Response:
    user = request["current_user"]
    q = request.rel_url.query.get("q", "").strip()
    if not q:
        return web.json_response({"results": []})
    embedding = await _get_embedding(q)
    if embedding is None:
        # Fall back to a simple text scan if embeddings are unavailable
        loop = asyncio.get_event_loop()
        chats = await loop.run_in_executor(None, auth_db.list_chats, user["sub"])
        results = [c for c in chats if q.lower() in c.get("name", "").lower()]
        return web.json_response({"results": results[:5], "method": "text"})
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, auth_db.search_embeddings, user["sub"], embedding)
    return web.json_response({"results": results, "method": "embedding"})


async def _embed_and_store(chat_id: str, user_id: str, role: str, content: str) -> None:
    embedding = await _get_embedding(content)
    if embedding:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, auth_db.save_embedding, chat_id, user_id, role, content, embedding
        )
