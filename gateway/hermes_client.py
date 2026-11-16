"""Async client for a hermes v0.23.1 agent's HTTP API (the ``api_server`` platform).

Logos v2 agents are upstream hermes processes running inside OpenShell sandboxes.
Each one serves an HTTP API on ``127.0.0.1:8642`` *inside* its sandbox
(``api_server.py:155`` ``DEFAULT_PORT``). This module is the only place that knows
that API's paths, payloads and SSE event names.

It is deliberately transport-agnostic: it takes a ``base_url`` and does not know how
that URL reaches the sandbox (see ``docs/architecture/hermes-client.md`` for the open
transport question). It is not wired into ``http_api.py`` yet.

All ``file:line`` references below are to the upstream source shipped in the
``logos-agent:0.23.1`` image under ``/opt/hermes/gateway/platforms/``:

* ``api_server.py``            - adapter, auth, health, sessions API, SSE framing
* ``api_server_runs.py``       - ``/v1/runs`` (start, status, events, stop)
* ``api_server_run_idempotency.py`` - terminal status set

Upstream facts this client relies on:

* Auth is ``Authorization: Bearer <API_SERVER_KEY>``; a bad or missing key answers
  401 ``{"error": {"code": "gateway_auth_failed", ...}}``
  (``api_server.py:1407-1411``, ``api_server.py:1413-1435``).
* Errors use the OpenAI envelope ``{"error": {"message", "type", "param", "code"}}``
  (``api_server.py:740-743``).
* Run events are SSE frames ``id: <seq>\\ndata: <json>\\n\\n`` with NO ``event:`` line;
  the event name is the ``"event"`` key inside the JSON
  (``api_server.py:195-201``, ``api_server_runs.py:224-226``,
  ``api_server_runs.py:1294-1297``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote

import aiohttp

__all__ = [
    "HermesClient",
    "HermesEvent",
    "HermesError",
    "HermesConnectionError",
    "HermesTimeoutError",
    "HermesHTTPError",
    "HermesAuthError",
    "HermesNotFoundError",
    "HermesProtocolError",
    "HermesStreamError",
    "EVENT_TYPES",
    "TERMINAL_EVENT_TYPES",
    "normalise_event",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HermesError(Exception):
    """Base class for every failure talking to a hermes agent."""


class HermesConnectionError(HermesError):
    """The agent could not be reached (connection refused, DNS, reset before a reply)."""


class HermesTimeoutError(HermesError):
    """A request, or a read on the event stream, exceeded its timeout."""


class HermesHTTPError(HermesError):
    """The agent answered with a non-2xx status.

    ``code`` / ``error_type`` come from the upstream OpenAI-style envelope
    (``api_server.py:740-743``) when the body has one.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        method: str = "",
        path: str = "",
        code: Optional[str] = None,
        error_type: Optional[str] = None,
        body: Any = None,
    ) -> None:
        self.status = status
        self.method = method
        self.path = path
        self.code = code
        self.error_type = error_type
        self.body = body
        self.upstream_message = message
        where = f"{method} {path}".strip()
        detail = message or "(no error message)"
        if code:
            detail = f"{detail} [{code}]"
        super().__init__(f"hermes {where} failed with HTTP {status}: {detail}")


class HermesAuthError(HermesHTTPError):
    """401/403 - the API key was rejected (``api_server.py:1407-1411``)."""


class HermesNotFoundError(HermesHTTPError):
    """404 - unknown run or session (``api_server_runs.py:251-252``, ``api_server.py:2498``)."""


class HermesProtocolError(HermesError):
    """A 2xx reply that is not the JSON shape upstream documents."""


class HermesStreamError(HermesError):
    """The event stream ended before a terminal event and without upstream's close marker.

    ``last_seq`` is the last sequence number received; pass it to
    ``stream_events(run_id, last_seq=...)`` to resume from the server's backlog
    (``api_server_runs.py:1277-1281``).
    """

    def __init__(self, message: str, *, run_id: str, last_seq: Optional[int] = None) -> None:
        self.run_id = run_id
        self.last_seq = last_seq
        super().__init__(message)


# ---------------------------------------------------------------------------
# Normalised events
# ---------------------------------------------------------------------------

#: Upstream event name -> normalised type. Sources:
#: ``message.delta``        api_server_runs.py:1073-1077   {"delta"}
#: ``message.interim``      api_server_runs.py:1079-1087   {"text", "already_streamed"}
#: ``reasoning.available``  api_server_runs.py:145         {"text"}
#: ``tool.started``         api_server_runs.py:141         {"tool", "preview", "tool_call_id"?}
#: ``tool.completed``       api_server_runs.py:142-144, 387-388
#:                          {"tool", "duration", "error": bool, "tool_call_id"?, "preview"}
#: ``subagent.start`` / ``subagent.complete``  api_server_runs.py:390-400
#: ``approval.request``     api_server.py:90-102
#: ``run.<status>``         api_server_runs.py:1089-1100 (status in TERMINAL_STATUSES,
#:                          api_server_run_idempotency.py:21)
#: ``run.queued``           api_server_runs.py:1177-1182 (not terminal: another owner runs it)
#:
#: Upstream v0.23.1 never emits ``run.started`` on this stream (the 202 from
#: ``POST /v1/runs`` is the start signal); the mapping is kept so a later version that
#: adds it is rendered correctly, and :meth:`HermesClient.chat` synthesises one.
_EVENT_NAME_TO_TYPE: Dict[str, str] = {
    "message.delta": "token",
    "message.interim": "message",
    "reasoning.available": "reasoning",
    "tool.started": "tool_started",
    "tool.completed": "tool_completed",  # becomes tool_failed when "error" is true
    "subagent.start": "subagent_started",
    "subagent.complete": "subagent_completed",
    "approval.request": "approval_request",
    "run.started": "run_started",
    "run.queued": "run_queued",
    "run.completed": "run_completed",
    "run.failed": "run_failed",
    "run.cancelled": "run_cancelled",
    "run.interrupted": "run_interrupted",
}

EVENT_TYPES = frozenset(_EVENT_NAME_TO_TYPE.values()) | {"tool_failed", "other"}

#: Types after which upstream closes the stream. Mirrors ``TERMINAL_STATUSES``
#: (``api_server_run_idempotency.py:21``): completed, failed, cancelled, interrupted.
TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed", "run_cancelled", "run_interrupted"})


@dataclass(frozen=True)
class HermesEvent:
    """One run event, normalised for rendering, with the upstream payload kept in ``raw``.

    ``type`` is one of :data:`EVENT_TYPES`. Anything upstream sends that this client does
    not know (``replay.truncated``, control prompts, future names) has type ``"other"``.

    Convenience fields (empty/None when the event does not carry them):

    * ``text``  - token delta, interim/reasoning text, or the final ``output`` of
      ``run_completed``
    * ``tool`` / ``tool_call_id`` / ``preview`` / ``duration`` - tool events
    * ``error`` - error message of ``run_failed`` / ``run_interrupted``
    """

    type: str
    name: str
    run_id: Optional[str] = None
    seq: Optional[int] = None
    timestamp: Optional[float] = None
    text: str = ""
    tool: Optional[str] = None
    tool_call_id: Optional[str] = None
    preview: str = ""
    duration: Optional[float] = None
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.type in TERMINAL_EVENT_TYPES


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def normalise_event(raw: Mapping[str, Any], *, sse_event: Optional[str] = None) -> HermesEvent:
    """Map one upstream event payload to a :class:`HermesEvent`. Never raises.

    ``sse_event`` is the SSE ``event:`` line, used only if the JSON has no ``"event"``
    key (upstream v0.23.1 always puts the name in the JSON, ``api_server_runs.py:224-226``).
    """
    payload: Dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {"value": raw}
    name = _as_str(payload.get("event") or sse_event or "")
    etype = _EVENT_NAME_TO_TYPE.get(name, "other")

    text = ""
    error: Optional[str] = None
    if etype == "token":
        text = _as_str(payload.get("delta"))
    elif etype in ("message", "reasoning"):
        text = _as_str(payload.get("text"))
    elif etype == "run_completed":
        text = _as_str(payload.get("output"))
    elif etype == "tool_completed" and payload.get("error"):
        # upstream folds failure into tool.completed as a boolean (api_server_runs.py:142-144)
        etype = "tool_failed"
    if etype in ("run_failed", "run_interrupted") and payload.get("error") is not None:
        error = _as_str(payload.get("error"))

    seq = payload.get("seq")
    timestamp = payload.get("timestamp")
    duration = payload.get("duration")
    is_tool = etype in ("tool_started", "tool_completed", "tool_failed")
    return HermesEvent(
        type=etype,
        name=name,
        run_id=_as_str(payload.get("run_id")) or None,
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        timestamp=float(timestamp) if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) else None,
        text=text,
        tool=(_as_str(payload.get("tool")) or None) if is_tool else None,
        tool_call_id=(_as_str(payload.get("tool_call_id")) or None) if is_tool else None,
        preview=_as_str(payload.get("preview")) if is_tool or etype.startswith("subagent_") else "",
        duration=float(duration) if is_tool and isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
        error=error,
        raw=payload,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_STREAM_CLOSED_COMMENT = "stream closed"


def _seg(value: str) -> str:
    """Quote one path segment (run ids / session ids are opaque strings)."""
    return quote(str(value), safe="")


class HermesClient:
    """Client for one hermes agent's API server.

    ``base_url`` is the server root, e.g. ``http://127.0.0.1:8642`` (a
    ``/p/<profile>`` suffix also works: upstream mirrors every route there,
    ``api_server.py:3973-3975``). ``api_key`` is the agent's ``API_SERVER_KEY``.

    If ``session`` is given the caller owns it; otherwise one is created lazily and
    closed by :meth:`close` / ``async with``.

    ``request_timeout`` bounds each non-streaming call. ``stream_idle_timeout`` bounds
    the gap between bytes on the event stream; upstream writes a ``: keepalive``
    comment every 10 s (``api_server.py:173``, ``api_server_runs.py:1327-1330``), so
    the default of 45 s only fires when the peer has really gone quiet.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        session: Optional[aiohttp.ClientSession] = None,
        *,
        request_timeout: float = 30.0,
        stream_idle_timeout: float = 45.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._session = session
        self._owns_session = session is None
        self.request_timeout = request_timeout
        self.stream_idle_timeout = stream_idle_timeout

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "HermesClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP session if this client created it."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        if self._owns_session:
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or (self._owns_session and self._session.closed):
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    # -- plumbing -----------------------------------------------------------

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if extra:
            headers.update(extra)
        return headers

    def _transport_error(self, exc: BaseException, method: str, path: str) -> HermesError:
        """Translate an aiohttp/asyncio transport failure. Timeouts are checked first:
        aiohttp's ``ServerTimeoutError`` is both a ``ClientError`` and a ``TimeoutError``."""
        where = f"{method} {self.base_url}{path}"
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return HermesTimeoutError(f"hermes {where} timed out")
        if isinstance(exc, aiohttp.ClientConnectorError):
            return HermesConnectionError(f"cannot connect to hermes at {self.base_url} ({method} {path}): {exc}")
        return HermesConnectionError(f"hermes {where} connection failed: {type(exc).__name__}: {exc}")

    @staticmethod
    async def _http_error(resp: aiohttp.ClientResponse, method: str, path: str) -> HermesHTTPError:
        """Build the right :class:`HermesHTTPError` from a non-2xx response."""
        try:
            text = await resp.text()
        except Exception:  # body unreadable: the status alone still identifies the failure
            text = ""
        body: Any = text
        message, code, error_type = "", None, None
        try:
            body = json.loads(text) if text else text
        except ValueError:
            body = text
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                message = _as_str(err.get("message"))
                code = _as_str(err.get("code")) or None
                error_type = _as_str(err.get("type")) or None
            elif err is not None:
                message = _as_str(err)
        if not message:
            message = (text or resp.reason or "").strip()[:300]
        cls = HermesHTTPError
        if resp.status in (401, 403):
            cls = HermesAuthError
        elif resp.status == 404:
            cls = HermesNotFoundError
        return cls(resp.status, message, method=method, path=path, code=code, error_type=error_type, body=body)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        """One non-streaming call returning a JSON object, with errors mapped to HermesError."""
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        try:
            async with self._get_session().request(
                method,
                self.base_url + path,
                params=params,
                json=json_body,
                headers=self._headers(headers),
                timeout=timeout,
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise await self._http_error(resp, method, path)
                text = await resp.text()
        except HermesError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:
            raise self._transport_error(exc, method, path) from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise HermesProtocolError(f"hermes {method} {path} returned non-JSON body: {text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise HermesProtocolError(f"hermes {method} {path} returned {type(data).__name__}, expected an object")
        return data

    # -- health -------------------------------------------------------------

    async def health(self) -> Dict[str, Any]:
        """``GET /health`` -> ``{"status": "ok", "platform": "hermes-agent", "version": ...}``.

        Route ``api_server.py:1555``, handler ``api_server.py:2266-2268``. Upstream does
        not authenticate this route, so a 200 proves reachability, not that the key works
        (use :meth:`list_sessions` with ``limit=1`` for that).
        """
        return await self._request_json("GET", "/health")

    # -- runs ---------------------------------------------------------------

    async def start_run(
        self,
        input: Union[str, Sequence[Mapping[str, Any]], None] = None,  # noqa: A002 - upstream field name
        *,
        messages: Optional[Sequence[Mapping[str, Any]]] = None,
        session_id: Optional[str] = None,
        instructions: Optional[str] = None,
        conversation_history: Optional[Sequence[Mapping[str, Any]]] = None,
        previous_response_id: Optional[str] = None,
        model: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """``POST /v1/runs`` -> run id. Upstream answers 202
        ``{"run_id", "status": "started", "replayed"}`` (``api_server_runs.py:549-555``)
        and executes the turn in the background.

        Route ``api_server_runs.py:286``, handler ``api_server_runs.py:768``.

        Body fields (all verified upstream):

        * ``input`` (required, ``api_server_runs.py:793``, ``718-728``): a string, or a
          list of ``{"role", "content"}`` messages whose LAST item is the user turn.
          ``messages=`` is an alias that is sent as ``input`` - upstream has no
          ``messages`` field on this route.
        * ``session_id`` (``api_server_runs.py:806``): continue a stored session; its
          history is loaded server side (``api_server_runs.py:851-852``). Omitted, the
          run id becomes the session id (``api_server_runs.py:841``).
        * ``instructions`` (``api_server_runs.py:498``): ephemeral system prompt.
        * ``conversation_history`` (``api_server_runs.py:501-511``): explicit
          ``[{"role", "content"}]`` history; overrides stored history.
        * ``previous_response_id`` (``api_server_runs.py:499``), ``model``
          (``api_server_runs.py:807``).

        Note: a multi-message ``input`` makes upstream treat all but the last message
        as the history (``api_server_runs.py:522-529``), which then wins over the stored
        history of ``session_id``. To continue a session, send only the new user text.

        ``idempotency_key`` is sent as the ``Idempotency-Key`` header
        (``api_server_runs.py:786-790``); a retry with the same key and body returns the
        original run id. Too many concurrent runs answer 429
        (``api_server.py:3565-3567``) -> :class:`HermesHTTPError`.
        """
        if input is not None and messages is not None:
            raise ValueError("pass either input or messages, not both")
        payload_input: Any = input if input is not None else (list(messages) if messages is not None else None)
        if isinstance(payload_input, (list, tuple)):
            payload_input = [dict(m) for m in payload_input]
        if not payload_input:
            raise ValueError("start_run needs a non-empty input (string or message list)")

        body: Dict[str, Any] = dict(extra or {})
        body["input"] = payload_input
        if session_id:
            body["session_id"] = session_id
        if instructions is not None:
            body["instructions"] = instructions
        if conversation_history is not None:
            body["conversation_history"] = [dict(m) for m in conversation_history]
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        if model:
            body["model"] = model

        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        data = await self._request_json("POST", "/v1/runs", json_body=body, headers=headers)
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise HermesProtocolError(f"hermes POST /v1/runs returned no run_id: {data!r}")
        return run_id

    async def get_run(self, run_id: str) -> Dict[str, Any]:
        """``GET /v1/runs/{run_id}`` -> pollable status object
        ``{"object": "hermes.run", "run_id", "status", "session_id", "last_event", ...}``.

        Route ``api_server_runs.py:286``, handler ``api_server_runs.py:1249-1253``,
        shape ``api_server_runs.py:311-320``. This is where the run's ``session_id`` can
        be read back (the 202 from :meth:`start_run` does not carry it).
        """
        return await self._request_json("GET", f"/v1/runs/{_seg(run_id)}")

    async def stop_run(self, run_id: str) -> Dict[str, Any]:
        """``POST /v1/runs/{run_id}/stop`` - interrupt a run.

        Route ``api_server_runs.py:292``, handler ``api_server_runs.py:1497-1528``.
        Returns ``{"run_id", "status": "stopping"}`` (``api_server_runs.py:1528``,
        ``api_server_authority_runs.py:165``), or the run's status object if it already
        reached a terminal status (``api_server_runs.py:1514-1515``). The stream then
        ends with ``run.cancelled``. Unknown run -> :class:`HermesNotFoundError`; a run
        this process cannot interrupt answers 409 ``run_not_active``
        (``api_server_runs.py:1516-1519``) -> :class:`HermesHTTPError`.
        """
        return await self._request_json("POST", f"/v1/runs/{_seg(run_id)}/stop")

    async def stream_events(self, run_id: str, *, last_seq: Optional[int] = None) -> AsyncIterator[HermesEvent]:
        """``GET /v1/runs/{run_id}/events`` (SSE) as an async iterator of :class:`HermesEvent`.

        Route ``api_server_runs.py:287``, handler ``api_server_runs.py:1256-1346``.

        Wire format: ``: open`` comment first (``api_server_runs.py:1307``), then frames
        ``id: <seq>\\ndata: <json>\\n\\n`` where the JSON is
        ``{"event", "run_id", "timestamp", ..., "seq"}`` (``api_server.py:195-201``,
        ``api_server_runs.py:224-226``, ``1294-1297``), ``: keepalive`` comments while
        idle (``api_server_runs.py:1330``) and ``: stream closed`` before upstream ends
        the response (``api_server_runs.py:1319``, ``1323``, ``1336``).

        The iterator ends cleanly after a terminal event (:data:`TERMINAL_EVENT_TYPES`),
        or when upstream sends its ``: stream closed`` marker without one (the
        ``run.queued`` hand-off, ``api_server_runs.py:1177-1182``). If the response ends
        any other way - connection cut, or upstream dropping a slow subscriber
        (``api_server_runs.py:1332-1334``) - :class:`HermesStreamError` is raised with
        the last ``seq`` seen. A silent stream raises :class:`HermesTimeoutError`.

        ``last_seq`` resumes after that sequence number via the ``Last-Event-ID`` header
        (``api_server_runs.py:1277-1281``); upstream keeps a 1000-event backlog per run
        (``api_server_runs.py:167``) and sends a ``replay.truncated`` event (type
        ``"other"``) if older events were lost (``api_server_runs.py:1308-1316``).

        Malformed frames and unknown event names are yielded as type ``"other"``; they
        are never dropped and never raise.
        """
        path = f"/v1/runs/{_seg(run_id)}/events"
        headers = {"Accept": "text/event-stream"}
        if last_seq is not None:
            headers["Last-Event-ID"] = str(int(last_seq))
        # No total timeout: a run may last minutes. sock_read bounds silence between bytes.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self.request_timeout, sock_read=self.stream_idle_timeout)
        seen_seq: Optional[int] = last_seq
        closed_by_server = False
        try:
            async with self._get_session().get(
                self.base_url + path, headers=self._headers(headers), timeout=timeout
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise await self._http_error(resp, "GET", path)
                async for kind, value, sse_event, sse_id in _iter_sse(resp.content):
                    if kind == "comment":
                        if value.strip() == _STREAM_CLOSED_COMMENT:
                            closed_by_server = True
                        continue
                    event = _event_from_frame(value, sse_event, sse_id, run_id)
                    if event.seq is not None:
                        seen_seq = event.seq
                    yield event
                    if event.terminal:
                        return
        except HermesError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise HermesTimeoutError(
                f"hermes event stream for run {run_id} was silent for more than "
                f"{self.stream_idle_timeout:g}s (last seq {seen_seq})"
            ) from exc
        except aiohttp.ClientConnectorError as exc:
            raise self._transport_error(exc, "GET", path) from exc
        except aiohttp.ClientError as exc:
            raise HermesStreamError(
                f"hermes event stream for run {run_id} was cut before a terminal event "
                f"(last seq {seen_seq}): {type(exc).__name__}: {exc}",
                run_id=run_id,
                last_seq=seen_seq,
            ) from exc
        if not closed_by_server:
            raise HermesStreamError(
                f"hermes event stream for run {run_id} ended before a terminal event (last seq {seen_seq})",
                run_id=run_id,
                last_seq=seen_seq,
            )

    async def chat(
        self,
        input: Union[str, Sequence[Mapping[str, Any]], None] = None,  # noqa: A002 - upstream field name
        *,
        messages: Optional[Sequence[Mapping[str, Any]]] = None,
        session_id: Optional[str] = None,
        **start_kwargs: Any,
    ) -> AsyncIterator[HermesEvent]:
        """Start a run and yield its events until it finishes.

        The first event is a ``run_started`` synthesised by this client from the 202
        (upstream v0.23.1 emits no ``run.started``); its ``raw`` is
        ``{"event": "run.started", "run_id": ..., "synthetic": True}`` so callers learn
        the run id before the first token and can call :meth:`stop_run`.

        Abandoning the iterator does NOT stop the run upstream - call :meth:`stop_run`.
        Errors are those of :meth:`start_run` and :meth:`stream_events`.
        """
        run_id = await self.start_run(input, messages=messages, session_id=session_id, **start_kwargs)
        yield normalise_event({"event": "run.started", "run_id": run_id, "synthetic": True})
        async for event in self.stream_events(run_id):
            yield event

    # -- sessions -----------------------------------------------------------

    async def list_sessions(
        self,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        source: Optional[str] = None,
        include_children: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """``GET /api/sessions`` -> ``{"object": "list", "data": [session...], "limit",
        "offset", "has_more"}``.

        Route ``api_server.py:1563``, handler ``api_server.py:2520-2571``. Query params
        ``limit`` (default 50, max 200), ``offset``, ``source``, ``include_children``
        (``api_server.py:2525-2528``). Each session has the client-safe keys of
        ``_session_response`` (``api_server.py:2440-2454``): ``id``, ``title``,
        ``source``, ``model``, ``started_at``, ``last_active``, ``message_count``, ...
        Sessions created through ``/v1/runs`` have source ``api_server``
        (``api_server.py:1601``).
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        if offset is not None:
            params["offset"] = int(offset)
        if source:
            params["source"] = source
        if include_children is not None:
            params["include_children"] = "true" if include_children else "false"
        data = await self._request_json("GET", "/api/sessions", params=params or None)
        if not isinstance(data.get("data"), list):
            raise HermesProtocolError(f"hermes GET /api/sessions returned no data list: {data!r}")
        return data

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """``GET /api/sessions/{session_id}`` -> the session dict (unwrapped from
        ``{"object": "hermes.session", "session": {...}}``).

        Route ``api_server.py:1565``, handler ``api_server.py:2645-2650``. Unknown id ->
        :class:`HermesNotFoundError` (code ``session_not_found``, ``api_server.py:2498``).
        """
        path = f"/api/sessions/{_seg(session_id)}"
        data = await self._request_json("GET", path)
        session = data.get("session")
        if not isinstance(session, dict):
            raise HermesProtocolError(f"hermes GET {path} returned no session object: {data!r}")
        return session

    async def get_session_messages(
        self,
        session_id: str,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /api/sessions/{session_id}/messages`` -> ``{"object": "list",
        "session_id", "data": [message...], "pagination": {...}}``.

        Route ``api_server.py:1568``, handler ``api_server.py:2730-2763``. Query params
        ``limit`` (max 500), ``offset``, ``order`` in ``oldest``/``latest``
        (``api_server.py:2738-2751``); with no ``limit`` upstream returns the latest 500.
        Message keys are those of ``_message_response`` (``api_server.py:2475-2481``):
        ``id``, ``role``, ``content``, ``tool_calls``, ``tool_name``, ``timestamp``,
        ``reasoning``, ... The returned ``session_id`` may differ from the one asked for
        when the session was compressed (``api_server.py:2737``).
        """
        if order not in (None, "oldest", "latest"):
            raise ValueError("order must be 'oldest' or 'latest'")
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        if offset is not None:
            params["offset"] = int(offset)
        if order:
            params["order"] = order
        path = f"/api/sessions/{_seg(session_id)}/messages"
        data = await self._request_json("GET", path, params=params or None)
        if not isinstance(data.get("data"), list):
            raise HermesProtocolError(f"hermes GET {path} returned no data list: {data!r}")
        return data


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


def _event_from_frame(data: str, sse_event: Optional[str], sse_id: Optional[str], run_id: str) -> HermesEvent:
    """Decode one SSE ``data`` payload. Undecodable frames become type ``"other"``."""
    try:
        payload = json.loads(data)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {"event": sse_event or "", "run_id": run_id, "malformed": True, "data": data}
    if "seq" not in payload and sse_id is not None:
        try:
            payload["seq"] = int(sse_id)
        except ValueError:
            pass
    return normalise_event(payload, sse_event=sse_event)


async def _iter_sse(content: aiohttp.StreamReader):
    """Yield ``("comment", text, None, None)`` and ``("data", data, event, id)`` tuples.

    Reads raw chunks and splits lines itself rather than using ``readline``: a
    ``run.completed`` frame carries the whole final answer on one line and can exceed
    aiohttp's line-length limit.
    """
    decoder_buffer = b""
    data_lines: List[str] = []
    sse_event: Optional[str] = None
    sse_id: Optional[str] = None
    async for chunk in content.iter_any():
        decoder_buffer += chunk
        while True:
            newline = decoder_buffer.find(b"\n")
            if newline < 0:
                break
            line = decoder_buffer[:newline].rstrip(b"\r").decode("utf-8", errors="replace")
            decoder_buffer = decoder_buffer[newline + 1:]
            if line == "":
                if data_lines:
                    yield "data", "\n".join(data_lines), sse_event, sse_id
                data_lines, sse_event, sse_id = [], None, None
            elif line.startswith(":"):
                yield "comment", line[1:].strip(), None, None
            else:
                name, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if name == "data":
                    data_lines.append(value)
                elif name == "event":
                    sse_event = value
                elif name == "id":
                    sse_id = value
