"""LOG-51.2 — Logos-side monkeypatch that teaches hermes's /v1/runs SSE
stream to interrupt the agent on client disconnect.

Why this exists
───────────────
Upstream hermes-agent 0.7.0 already has the interrupt primitives —
`agent.interrupt()` (run_agent.py:3086) sets a flag the run loop honors
at 20+ checkpoints, including mid-LLM-stream HTTP-close at 5139 — but
it only wires them on `/v1/responses`. The newer `/v1/runs` path (which
LOG-44 Phase 1 dispatches through) has no cancel endpoint and its SSE
handler doesn't interrupt on disconnect, so a Logos Stop button clicked
mid-iteration does nothing.

We don't want to fork hermes-agent (the sandbox image must stay
swappable — see LOG-45). So this patch rides in via upload at spawn
time, rebinds the two methods on ``APIServerAdapter`` in-process, and
delegates to the hermes CLI via ``runpy``. When upstream eventually
ships the capability, the whole file gets deleted and
``hermes_server_mode.py`` reverts to launching ``hermes gateway run``
directly.

How it's delivered
──────────────────
1. ``hermes_server_mode.upload_config_to_sandbox`` uploads this file
   into the sandbox at ``/tmp/hermes-srv-home/hermes_cancel_monkeypatch.py``.
2. ``hermes_server_mode.launch_hermes_gateway`` launches via
   ``python3 /tmp/hermes-srv-home/hermes_cancel_monkeypatch.py gateway run -v``
   instead of ``hermes gateway run -v``.
3. This script applies the patches, then hands off to ``/usr/local/bin/hermes``
   via ``runpy.run_path`` so the rest of hermes boots normally in-process.

Drift guard
───────────
The rebind is wrapped in ``try/except AttributeError/ImportError``. If
hermes renames ``APIServerAdapter`` or the internals we patch, the
patch fails, cancel regresses to "not working" (same as today before
this patch existed), and hermes still boots. We log a loud WARNING so
operators notice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import runpy
import sys
import time
import uuid
from typing import Dict, List, Optional

# Configure a dedicated logger so the patch's successes/failures are
# clearly identifiable in /tmp/hermes-gw.log.
_logger = logging.getLogger("logos.cancel_patch")
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)


def _apply_cancel_patches() -> bool:
    """Rebind `_handle_runs` + `_handle_run_events` on APIServerAdapter.

    Returns True on success, False if any upstream rename made the
    rebind impossible. False path: hermes boots unpatched, cancel does
    not work (documented degradation — same behaviour as pre-patch).
    """
    try:
        from gateway.platforms import api_server as _hermes_api
        from aiohttp import web  # hermes already depends on aiohttp
    except ImportError as exc:
        _logger.warning(
            "logos.cancel_patch: hermes api_server module not importable "
            "(%s) — leaving cancel unpatched", exc,
        )
        return False

    _Adapter = getattr(_hermes_api, "APIServerAdapter", None)
    if _Adapter is None:
        _logger.warning(
            "logos.cancel_patch: APIServerAdapter class not found on "
            "api_server module (upstream rename?) — leaving cancel unpatched",
        )
        return False

    # Sanity check: the internals we rely on exist. If any are gone,
    # fail the whole patch rather than boot a Frankenstein.
    _required_methods = ("_handle_runs", "_handle_run_events",
                         "_check_auth", "_make_run_event_callback",
                         "_create_agent")
    for _m in _required_methods:
        if not hasattr(_Adapter, _m):
            _logger.warning(
                "logos.cancel_patch: APIServerAdapter.%s missing — "
                "leaving cancel unpatched", _m,
            )
            return False

    # Pull module-level helpers the original body uses. Failing any of
    # these means upstream moved them; bail out rather than guess.
    try:
        _openai_error = _hermes_api._openai_error
    except AttributeError:
        _logger.warning(
            "logos.cancel_patch: _openai_error helper missing — "
            "leaving cancel unpatched",
        )
        return False

    _upstream_logger = getattr(_hermes_api, "logger", _logger)

    # ── patched POST /v1/runs ────────────────────────────────────────
    # Verbatim copy of upstream 0.7.0 body except for the two marked
    # LOGOS-PATCH blocks:
    #   (a) init self._logos_run_handles if missing
    #   (b) append the newly-created agent to ``agent_ref`` inside the
    #       _run_and_close closure, and record (task, agent_ref) under
    #       run_id so _handle_run_events can reach them on disconnect.
    async def _patched_handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs — start an agent run, return run_id immediately.

        Logos-patched: tracks (task, agent_ref) in
        ``self._logos_run_handles[run_id]`` so the SSE events handler
        can interrupt the run on client disconnect.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        if len(self._run_streams) >= self._MAX_CONCURRENT_RUNS:
            return web.json_response(
                _openai_error(
                    f"Too many concurrent runs (max {self._MAX_CONCURRENT_RUNS})",
                    code="rate_limit_exceeded",
                ),
                status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = (
            raw_input if isinstance(raw_input, str)
            else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        )
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        run_id = f"run_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = time.time()

        # ── LOGOS-PATCH (a) ────────────────────────────────────────
        # Lazy-init the handles map. Lives alongside _run_streams for
        # the lifetime of the process. Pop in the same finally as
        # _run_streams in _handle_run_events below + in _run_and_close.
        if not hasattr(self, "_logos_run_handles"):
            self._logos_run_handles: Dict[str, Dict] = {}
        agent_ref: List = []
        # ───────────────────────────────────────────────────────────

        event_cb = self._make_run_event_callback(run_id, loop)

        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append(
                    {"role": str(entry["role"]), "content": str(entry["content"])}
                )
            if previous_response_id:
                _upstream_logger.debug(
                    "Both conversation_history and previous_response_id provided; "
                    "using conversation_history"
                )

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored:
                conversation_history = list(stored.get("conversation_history", []))
                stored_session_id = stored.get("session_id")
                if instructions is None:
                    instructions = stored.get("instructions")

        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append(
                        {"role": msg["role"], "content": str(content)}
                    )

        session_id = body.get("session_id") or stored_session_id or run_id
        ephemeral_system_prompt = instructions

        # ── LOGOS-PATCH (d) — LOG-44.4 A.1 ─────────────────────────
        # Auto-load conversation history from hermes's SessionDB when
        # the caller provided an explicit session_id but no
        # conversation_history. Mirrors the /v1/chat/completions
        # behavior at api_server.py:672-700 which already does this
        # for the sibling endpoint. Without this, every Logos turn
        # on /v1/runs starts with an empty conversation and the agent
        # has amnesia across turns in the same chat.
        #
        # Security note: upstream gates this behind API key auth on
        # /v1/chat/completions. /v1/runs already enforced auth above
        # (_check_auth → 401), so we're already authenticated and
        # can reuse the session_id → history lookup freely.
        if body.get("session_id") and not conversation_history:
            try:
                if hasattr(self, "_ensure_session_db"):
                    db = self._ensure_session_db()
                    if db is not None:
                        loaded = db.get_messages_as_conversation(session_id)
                        if loaded:
                            conversation_history = list(loaded)
                            _upstream_logger.info(
                                "logos.cancel_patch: loaded %d messages "
                                "from SessionDB for session %s",
                                len(conversation_history), session_id,
                            )
            except Exception as exc:
                _upstream_logger.warning(
                    "logos.cancel_patch: SessionDB auto-load failed "
                    "for session %s: %s (continuing with empty history)",
                    session_id, exc,
                )
        # ───────────────────────────────────────────────────────────

        async def _run_and_close():
            try:
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                )
                # ── LOGOS-PATCH (b) ────────────────────────────────
                # Expose the agent to _handle_run_events via the
                # agent_ref list captured in the enclosing closure.
                # Doing it here (not before create) so we only publish
                # a usable reference.
                agent_ref.append(agent)
                # ───────────────────────────────────────────────────

                def _run_sync():
                    r = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=conversation_history,
                        task_id="default",
                    )
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                q.put_nowait({
                    "event": "run.completed",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "output": final_response,
                    "usage": usage,
                })
            except asyncio.CancelledError:
                # Client-disconnect path in _handle_run_events cancelled
                # us. Emit a run.failed so any late SSE subscribers see
                # a terminal frame, then re-raise so the asyncio
                # machinery records the cancellation properly.
                try:
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": "cancelled by client",
                    })
                except Exception:
                    pass
                raise
            except Exception as exc:
                _upstream_logger.exception("[api_server] run %s failed", run_id)
                try:
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": str(exc),
                    })
                except Exception:
                    pass
            finally:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
                # LOGOS-PATCH (c): tear down the handle once the run
                # has genuinely finished so a late cancel can't target
                # a now-dead task.
                try:
                    self._logos_run_handles.pop(run_id, None)
                except Exception:
                    pass

        task = asyncio.create_task(_run_and_close())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        # ── LOGOS-PATCH (a cont.) ──────────────────────────────────
        # Stash the handle NOW that we have the task. agent_ref starts
        # empty; _run_and_close appends when the agent is constructed.
        self._logos_run_handles[run_id] = {
            "task": task,
            "agent_ref": agent_ref,
            "created_at": time.time(),
        }
        # ───────────────────────────────────────────────────────────

        return web.json_response({"run_id": run_id, "status": "started"}, status=202)

    # ── patched GET /v1/runs/{run_id}/events ─────────────────────────
    # Verbatim copy except for the new disconnect-catch branch that
    # calls agent.interrupt() + task.cancel(), mirroring upstream's
    # /v1/responses pattern (api_server.py:1375-1389).
    async def _patched_handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events — SSE stream.

        Logos-patched: on client disconnect, calls ``agent.interrupt()``
        and ``task.cancel()`` on the paired run to stop it from iterating
        further. Parity with upstream's /v1/responses behaviour.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]

        for _ in range(20):
            if run_id in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )

        q = self._run_streams[run_id]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        # ── LOGOS-PATCH: split disconnect vs other exceptions ──────
        # Upstream catches "Exception" broadly and logs at debug —
        # which masks the disconnect signal. We catch the disconnect
        # subclasses first, interrupt the agent, then let the finally
        # clean up both the stream and the handle.
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as exc:
            # Client dropped the SSE connection mid-run. Interrupt the
            # agent so it stops making LLM calls (the flag is checked
            # at 20+ points in the run loop, including mid-stream
            # HTTP-close for in-flight token generation), then cancel
            # the asyncio task so the wrapper unwinds.
            handle = getattr(self, "_logos_run_handles", {}).get(run_id)
            if handle:
                agent_list = handle.get("agent_ref") or []
                agent_obj = agent_list[0] if agent_list else None
                if agent_obj is not None:
                    try:
                        agent_obj.interrupt("SSE client disconnected")
                    except Exception as _ierr:
                        _upstream_logger.debug(
                            "logos.cancel_patch: interrupt() on run %s raised: %s",
                            run_id, _ierr,
                        )
                task_obj = handle.get("task")
                if task_obj is not None and not task_obj.done():
                    try:
                        task_obj.cancel()
                    except Exception as _cerr:
                        _upstream_logger.debug(
                            "logos.cancel_patch: task.cancel() on run %s raised: %s",
                            run_id, _cerr,
                        )
                _upstream_logger.info(
                    "logos.cancel_patch: SSE client disconnected, "
                    "interrupted run %s (%s)", run_id, exc.__class__.__name__,
                )
            else:
                _upstream_logger.debug(
                    "logos.cancel_patch: SSE client disconnected for run %s "
                    "but no handle recorded — nothing to interrupt",
                    run_id,
                )
        except Exception as exc:
            _upstream_logger.debug(
                "[api_server] SSE stream error for run %s: %s", run_id, exc,
            )
        finally:
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)
            # Handle usually cleared by _run_and_close's finally once
            # the run actually terminates, but pop defensively in case
            # we're racing with it.
            try:
                getattr(self, "_logos_run_handles", {}).pop(run_id, None)
            except Exception:
                pass

        return response

    # Rebind. Instance-level `_run_handles` init happens lazily in the
    # patched _handle_runs so we don't have to hook __init__.
    _Adapter._handle_runs = _patched_handle_runs
    _Adapter._handle_run_events = _patched_handle_run_events

    _logger.info(
        "logos.cancel_patch: applied /v1/runs SSE-disconnect-interrupt "
        "patch to APIServerAdapter"
    )
    return True


def _main() -> int:
    """Apply the patch (best-effort), then hand off to hermes's CLI.

    Returns the hermes process exit code if reachable via runpy;
    otherwise a small nonzero sentinel.
    """
    _apply_cancel_patches()  # non-fatal on failure — warning is logged

    # The hermes binary is a pip-installed console_scripts entry at
    # /usr/local/bin/hermes. runpy.run_path executes it in this
    # interpreter so the monkeypatches stick. We rewrite argv[0] first
    # so hermes's argparse help lines look right.
    try:
        sys.argv[0] = "hermes"
        runpy.run_path("/usr/local/bin/hermes", run_name="__main__")
    except SystemExit as _se:
        return int(_se.code or 0)
    except Exception as exc:
        _logger.error(
            "logos.cancel_patch: failed to hand off to hermes binary "
            "(/usr/local/bin/hermes): %s", exc,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
