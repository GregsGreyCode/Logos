"""
Telegram / Discord / Slack turn mirror — sandbox-side hook POSTs each
completed agent turn to the gateway so the Chats sidebar's platform
pills (`📱 TG`, `💬 DC`, `⚡ SL`) can show the conversation.

Without this, inbound platform chats happen entirely inside the
sandbox's hermes process — the gateway's `dispatches` + `sessions`
tables never see them, and `/api/platform-sessions?platform=telegram`
returns empty even though conversations are live.

Wire:
  1. Hermes fires `agent:end` on every turn (run.py:4562). Our hook
     bundle (gateway/sandbox_hooks/logos-mirror/) gets uploaded to
     ~/.hermes/hooks/logos-mirror/ at spawn, discovered by hermes'
     HookRegistry, and calls the gateway on each turn end.
  2. The hook POSTs here with {platform, agent_name, chat_id, user_id,
     user_name, user_msg, agent_reply, session_id, ts_*}.
  3. We look up agent_id by name, upsert a session row with
     source=<platform>, record a dispatch row with
     origin=platform_<platform>. `/api/platform-sessions` is already
     wired to surface these.

Auth: shared-secret `X-Mirror-Token` header, matched against
LOGOS_MIRROR_TOKEN env var. The gateway generates the token once at
startup and pushes it to each sandbox as an env var; unauthenticated
calls from anywhere else on the host network bounce 401.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from gateway.auth import db as auth_db

logger = logging.getLogger(__name__)


def _expected_token() -> str:
    """Token the sandbox hook must present in `X-Mirror-Token`."""
    return (os.environ.get("LOGOS_MIRROR_TOKEN") or "").strip()


async def _handle_mirror_turn(request: web.Request) -> web.Response:
    """POST /api/internal/mirror-turn — record a sandbox-side platform turn.

    Never raises on bad input — logs and returns 4xx so a misconfigured
    hook can never break the sandbox's turn loop. A missing mirror is
    strictly better than a crashed agent.
    """
    expected = _expected_token()
    if not expected:
        # Token not provisioned yet — return 503 so the hook doesn't
        # retry forever. Startup should set this; if it hasn't, the
        # gateway has bigger problems.
        return web.json_response(
            {"error": "mirror token not configured on gateway"},
            status=503,
        )
    supplied = (request.headers.get("X-Mirror-Token") or "").strip()
    if supplied != expected:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    platform    = (body.get("platform")    or "").strip().lower()
    agent_name  = (body.get("agent_name")  or "").strip()
    chat_id     = str(body.get("chat_id")  or "").strip()
    user_id     = str(body.get("user_id")  or "").strip() or None
    user_name   = (body.get("user_name")   or "").strip() or None
    user_msg    = (body.get("user_msg")    or "") or ""
    agent_reply = (body.get("agent_reply") or "") or ""
    session_id_hint = (body.get("session_id") or "").strip() or None
    started_at  = float(body.get("started_at") or time.time())
    ended_at    = float(body.get("ended_at") or time.time())

    # Accept even if some fields are blank, but skip the dispatch record
    # for a fully-empty turn. The heartbeat-style "agent:end emitted
    # with no user message" case happens occasionally during hermes
    # startup flushes and would otherwise pollute the Runs tab.
    if not platform or not agent_name:
        return web.json_response(
            {"error": "platform and agent_name are required"},
            status=400,
        )
    if not user_msg and not agent_reply:
        return web.json_response({"ok": True, "skipped": "empty turn"})

    # Resolve agent_id by name — the sandbox's hostname maps to
    # hermes-<agent_name>. If the agent was deleted between spawn and
    # this call, we still record the dispatch under the name we got.
    agent_id = ""
    try:
        row = auth_db.get_agent_by_name(agent_name)
        if row:
            agent_id = row.get("id") or ""
    except Exception as _exc:
        logger.warning("mirror_turn: get_agent_by_name(%r) failed: %s", agent_name, _exc)

    # Stable session id per (agent_id, platform, chat_id). The chats
    # sidebar groups by session, so using a stable id means subsequent
    # turns in the same Telegram chat land in one thread rather than
    # producing a fresh row per message.
    session_key_seed = f"{agent_id or agent_name}|{platform}|{chat_id}"
    session_id = session_id_hint or f"mir_{uuid.uuid5(uuid.NAMESPACE_URL, session_key_seed).hex[:20]}"

    # ── Session upsert into core.state's sessions table ────────────
    runner: Any = request.app.get("runner")
    db = getattr(getattr(runner, "session_store", None), "_db", None) if runner else None
    if db is not None:
        try:
            # Check if the session already exists so we know whether to
            # INSERT (first turn) or UPDATE (follow-up turn in same chat).
            cur = db._conn.execute(
                "SELECT id, message_count FROM sessions WHERE id = ?",
                (session_id,),
            )
            existing = cur.fetchone()
            if existing is None:
                title = user_name or chat_id or "(anon)"
                db._conn.execute(
                    """INSERT INTO sessions
                       (id, source, title, user_id, message_count,
                        started_at, ended_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, platform, title, user_id, 2,
                     started_at, ended_at),
                )
            else:
                db._conn.execute(
                    """UPDATE sessions
                       SET ended_at = ?,
                           message_count = COALESCE(message_count, 0) + 2
                       WHERE id = ?""",
                    (ended_at, session_id),
                )
            db._conn.commit()
        except Exception as _sx:
            logger.warning("mirror_turn: session upsert failed: %s", _sx)

    # ── Append turn to the session's JSONL transcript so the
    # messages endpoint can replay the conversation. Two records per
    # turn: inbound user message + outbound agent reply. ────────────
    try:
        from logos_cli.config import get_hermes_home
        sessions_dir = get_hermes_home() / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        transcript = sessions_dir / f"{session_id}.jsonl"
        with open(transcript, "a", encoding="utf-8") as fh:
            if user_msg:
                fh.write(json.dumps({
                    "role": "user",
                    "content": user_msg,
                    "timestamp": started_at,
                    "mirror_source": f"sandbox/{platform}",
                }, ensure_ascii=False) + "\n")
            if agent_reply:
                fh.write(json.dumps({
                    "role": "assistant",
                    "content": agent_reply,
                    "timestamp": ended_at,
                    "mirror_source": f"sandbox/{platform}",
                }, ensure_ascii=False) + "\n")
    except Exception as _tx:
        logger.warning("mirror_turn: transcript append failed: %s", _tx)

    # ── Dispatch row (origin=platform_<platform>) so the Runs tab
    # AND the sidebar pill query both see it. The pill query
    # (`/api/platform-sessions?platform=X&agent_id=Y`) JOINs through
    # this table — no dispatch row, no pill entry. ────────────────
    try:
        dispatch_id = f"dsp_{uuid.uuid4().hex[:12]}"
        now_ms = int(time.time() * 1000)
        started_ms = int(started_at * 1000)
        auth_db.record_dispatch(
            task_id=dispatch_id,  # sandbox doesn't forward task_id; use our own
            agent_id=agent_id or None,
            sandbox_name=f"hermes-{agent_name.lower()}",
            model=None,
            origin=f"platform_{platform}",
            origin_detail=chat_id,
            session_id=session_id,
            user_id=user_id,
            user_message=user_msg[:2000] if user_msg else "",
        )
        # record_dispatch uses its own started_at = now_ms; fix it so
        # the Runs timeline reflects when the turn actually started.
        with auth_db._conn() as conn:
            conn.execute(
                "UPDATE dispatches SET started_at=?, ended_at=?, status=?, elapsed_s=? WHERE task_id=?",
                (started_ms, now_ms, "ok", max(0.0, ended_at - started_at), dispatch_id),
            )
    except Exception as _dx:
        logger.warning("mirror_turn: dispatch record failed: %s", _dx)

    return web.json_response({
        "ok": True,
        "session_id": session_id,
        "agent_id": agent_id or None,
    })
