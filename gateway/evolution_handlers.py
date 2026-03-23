"""Evolution handlers: self-improvement proposals, settings, frontier consultation."""

from __future__ import annotations

import json
import logging
import os
import time

import aiohttp
from aiohttp import web

import gateway.auth.db as auth_db
from gateway.auth.rbac import has_permission

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _user(request: web.Request) -> dict:
    return request["user"]


def _require(request: web.Request, perm: str) -> None:
    u = _user(request)
    if not has_permission(u["role"], perm):
        raise web.HTTPForbidden(reason=f"Requires {perm}")


# ── Proposals ─────────────────────────────────────────────────────────────────

async def handle_list_proposals(request: web.Request) -> web.Response:
    _require(request, "view_evolution")
    status = request.rel_url.query.get("status")
    limit = int(request.rel_url.query.get("limit", 50))
    offset = int(request.rel_url.query.get("offset", 0))
    items, total = auth_db.list_evolution_proposals(status=status, limit=limit, offset=offset)
    return web.json_response({"items": items, "total": total})


async def handle_get_proposal(request: web.Request) -> web.Response:
    _require(request, "view_evolution")
    proposal = auth_db.get_evolution_proposal(request.match_info["id"])
    if not proposal:
        raise web.HTTPNotFound()
    return web.json_response(proposal)


async def handle_create_proposal(request: web.Request) -> web.Response:
    _require(request, "manage_evolution")
    body = await request.json()
    proposal = auth_db.create_evolution_proposal(
        title=body["title"],
        summary=body["summary"],
        diff_text=body.get("diff_text"),
        target_files=body.get("target_files", []),
        proposal_type=body.get("proposal_type", "improvement"),
        agent_id=body.get("agent_id", "hermes"),
    )
    return web.json_response(proposal, status=201)


async def handle_decide_proposal(request: web.Request) -> web.Response:
    """Accept, decline, or question a proposal."""
    _require(request, "decide_evolution")
    proposal_id = request.match_info["id"]
    proposal = auth_db.get_evolution_proposal(proposal_id)
    if not proposal:
        raise web.HTTPNotFound()

    body = await request.json()
    action = body.get("action")  # "accept" | "decline" | "question"
    if action not in ("accept", "decline", "question"):
        raise web.HTTPBadRequest(reason="action must be accept, decline, or question")

    status_map = {"accept": "accepted", "decline": "declined", "question": "questioned"}
    u = _user(request)
    updates: dict = {
        "status": status_map[action],
        "decided_by": u["id"],
        "decided_at": int(time.time() * 1000),
    }
    if action == "question":
        question_text = body.get("question_text", "").strip()
        if not question_text:
            raise web.HTTPBadRequest(reason="question_text required for question action")
        updates["question_text"] = question_text

    updated = auth_db.update_evolution_proposal(proposal_id, **updates)
    return web.json_response(updated)


async def handle_answer_question(request: web.Request) -> web.Response:
    """Record the agent's answer to a question."""
    _require(request, "manage_evolution")
    proposal_id = request.match_info["id"]
    proposal = auth_db.get_evolution_proposal(proposal_id)
    if not proposal:
        raise web.HTTPNotFound()
    body = await request.json()
    answer = body.get("answer_text", "").strip()
    if not answer:
        raise web.HTTPBadRequest(reason="answer_text required")
    updated = auth_db.update_evolution_proposal(
        proposal_id, answer_text=answer, status="pending"
    )
    return web.json_response(updated)


# ── Frontier consultation ──────────────────────────────────────────────────────

_FRONTIER_ENDPOINTS = {
    "claude-opus-4-6": ("https://api.anthropic.com/v1/messages", "anthropic"),
    "claude-sonnet-4-6": ("https://api.anthropic.com/v1/messages", "anthropic"),
    "gpt-4o": ("https://api.openai.com/v1/chat/completions", "openai"),
    "gpt-4o-mini": ("https://api.openai.com/v1/chat/completions", "openai"),
}


async def handle_consult_frontier(request: web.Request) -> web.Response:
    """Ask a frontier model (Claude/GPT) for feedback on a proposal."""
    _require(request, "decide_evolution")
    proposal_id = request.match_info["id"]
    proposal = auth_db.get_evolution_proposal(proposal_id)
    if not proposal:
        raise web.HTTPNotFound()

    settings = auth_db.get_evolution_settings()
    body = await request.json()
    model = body.get("model") or settings.get("frontier_model", "claude-opus-4-6")
    api_key_env = settings.get("frontier_api_key_env", "ANTHROPIC_API_KEY")
    # Allow per-request override of which env var to read
    if body.get("api_key_env"):
        api_key_env = body["api_key_env"]

    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise web.HTTPBadRequest(reason=f"API key env {api_key_env!r} is not set")

    endpoint_info = _FRONTIER_ENDPOINTS.get(model)
    if not endpoint_info:
        raise web.HTTPBadRequest(reason=f"Unknown frontier model: {model}")
    url, provider = endpoint_info

    prompt = (
        f"You are a senior software architect reviewing a self-improvement proposal "
        f"from an AI agent called Logos.\n\n"
        f"## Proposal: {proposal['title']}\n\n"
        f"**Type:** {proposal['proposal_type']}\n\n"
        f"**Summary:**\n{proposal['summary']}\n\n"
    )
    if proposal.get("diff_text"):
        prompt += f"**Proposed diff:**\n```diff\n{proposal['diff_text']}\n```\n\n"
    if proposal.get("target_files"):
        prompt += f"**Target files:** {', '.join(proposal['target_files'])}\n\n"
    prompt += (
        "Please review this proposal and provide:\n"
        "1. Your assessment of the value and risk\n"
        "2. Any concerns or suggested modifications\n"
        "3. A final recommendation: Accept / Decline / Modify\n"
    )

    try:
        async with aiohttp.ClientSession() as session:
            if provider == "anthropic":
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                payload = {
                    "model": model,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                }
                async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    output = data["content"][0]["text"]
            else:  # openai
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                }
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                }
                async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    output = data["choices"][0]["message"]["content"]
    except aiohttp.ClientError as exc:
        raise web.HTTPBadGateway(reason=f"Frontier API error: {exc}")

    auth_db.update_evolution_proposal(
        proposal_id, frontier_model=model, frontier_output=output
    )
    return web.json_response({"model": model, "output": output})


# ── Settings ───────────────────────────────────────────────────────────────────

async def handle_get_settings(request: web.Request) -> web.Response:
    _require(request, "view_evolution")
    settings = auth_db.get_evolution_settings()
    # Never expose the PAT in plaintext
    if settings.get("git_pat"):
        settings["git_pat"] = "••••••••"
    return web.json_response(settings)


async def handle_update_settings(request: web.Request) -> web.Response:
    _require(request, "manage_evolution")
    body = await request.json()
    # Never overwrite PAT with the masked placeholder
    if body.get("git_pat") == "••••••••":
        body.pop("git_pat")
    updated = auth_db.update_evolution_settings(**body)
    if updated.get("git_pat"):
        updated["git_pat"] = "••••••••"
    return web.json_response(updated)
