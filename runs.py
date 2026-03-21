"""
Run auditability for Logos — durable audit trail for agent executions.

RunRecorder wraps a single run_conversation() call and persists incremental
state (tool calls, approvals) as the run progresses, then finalises the
record on completion.

RunReplayer handles replay (re-run same user message with same session) and
clone (new session pre-seeded from a prior run) with destructive-action safety
checks.
"""

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Status values ─────────────────────────────────────────────────────────
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_MAX_ITERATIONS = "max_iterations"

# ─── Trigger types ─────────────────────────────────────────────────────────
TRIGGER_USER = "user_message"
TRIGGER_CRON = "cron"
TRIGGER_DELEGATE = "delegate"
TRIGGER_REPLAY = "replay"
TRIGGER_CLONE = "clone"

# ─── Tools that warrant a replay safety prompt ─────────────────────────────
_DESTRUCTIVE_TOOLS = frozenset({
    "terminal", "run_terminal", "bash", "execute_command",
    "write_file", "patch", "delete_file",
    "run_python", "code_execution", "execute_code",
})


def _new_run_id() -> str:
    return str(uuid.uuid4())


# ─── RunRecorder ────────────────────────────────────────────────────────────

class RunRecorder:
    """
    Lightweight per-run recorder.  Created at the start of run_conversation();
    call finish() when the run ends.

    All DB writes are best-effort — a failure never propagates to the agent.
    If the DB is unavailable the recorder is silently inert.
    """

    def __init__(
        self,
        db,                          # SessionDB instance
        run_id: str,
        session_id: str,
        source: str,
        model: str,
        provider: str,
        user_message: str,
        user_id: Optional[str] = None,
        trigger_type: str = TRIGGER_USER,
        parent_run_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ):
        self._db = db
        self.run_id = run_id
        self._tool_sequence: List[Dict] = []
        self._approval_events: List[Dict] = []
        self._active = True

        try:
            db.create_run(
                run_id=run_id,
                session_id=session_id,
                source=source,
                model=model,
                provider=provider,
                user_message_preview=(user_message or "")[:200],
                user_id=user_id,
                trigger_type=trigger_type,
                parent_run_id=parent_run_id,
                agent_id=agent_id,
            )
        except Exception as exc:
            logger.debug("RunRecorder: create_run failed: %s", exc)
            self._active = False

    # ── Incremental updates ─────────────────────────────────────────────────

    def record_tool_call(
        self,
        tool_name: str,
        args_preview: str = "",
        success: Optional[bool] = None,
        duration_ms: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append a tool call event and flush to DB.  Best-effort."""
        if not self._active:
            return
        entry: Dict[str, Any] = {
            "tool": tool_name,
            "args": (args_preview or "")[:200],
            "ts": round(time.time(), 2),
        }
        if success is not None:
            entry["ok"] = success
        if duration_ms is not None:
            entry["ms"] = round(duration_ms, 1)
        if error:
            entry["err"] = (error or "")[:200]
        self._tool_sequence.append(entry)
        try:
            self._db.update_run_field(
                self.run_id, tool_sequence=self._tool_sequence
            )
        except Exception as exc:
            logger.debug("RunRecorder: update tool_sequence failed: %s", exc)

    def record_approval(
        self,
        command: str,
        approved: bool,
        approval_type: str = "session",
    ) -> None:
        """Record a dangerous-command approval event.  Best-effort."""
        if not self._active:
            return
        self._approval_events.append({
            "cmd": (command or "")[:200],
            "approved": approved,
            "type": approval_type,
            "ts": round(time.time(), 2),
        })
        try:
            self._db.update_run_field(
                self.run_id, approval_events=self._approval_events
            )
        except Exception as exc:
            logger.debug("RunRecorder: update approval_events failed: %s", exc)

    # ── Finalisation ────────────────────────────────────────────────────────

    def finish(
        self,
        status: str,
        final_response: Optional[str] = None,
        api_call_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_details: Optional[str] = None,
    ) -> None:
        """Write the final run record.  Best-effort; never raises."""
        if not self._active:
            return
        self._active = False
        try:
            self._db.end_run(
                run_id=self.run_id,
                status=status,
                api_call_count=api_call_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_sequence=self._tool_sequence,
                approval_events=self._approval_events if self._approval_events else None,
                error_details=error_details,
                final_output_preview=(final_response or "")[:500],
            )
        except Exception as exc:
            logger.debug("RunRecorder: finish failed: %s", exc)


# ─── RunReplayer ────────────────────────────────────────────────────────────

class RunReplayer:
    """
    Provides replay (re-run user message with same session context) and
    clone (new session pre-seeded with prior user message) operations.

    Safety model:
    - Replay of a run that used destructive tools raises DestructiveRunError
      unless the caller passes force=True, in which case the caller is
      responsible for requiring re-approval before running.
    - Clone always returns just the user message — the caller decides what
      to do with it.
    """

    class DestructiveRunError(Exception):
        """Raised when replay is requested for a run with destructive tools."""

    def __init__(self, db):
        self._db = db

    # ── Query ───────────────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> Optional[Dict]:
        return self._db.get_run(run_id)

    def list_runs(
        self,
        source: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        return self._db.list_runs(
            source=source, status=status, limit=limit, offset=offset
        )

    # ── Inspection ──────────────────────────────────────────────────────────

    def has_destructive_tools(self, run: Dict) -> bool:
        """Return True if the run used any destructive tools."""
        seq = run.get("tool_sequence") or []
        if isinstance(seq, str):
            try:
                seq = json.loads(seq)
            except Exception:
                return False
        return any(
            isinstance(e, dict) and e.get("tool") in _DESTRUCTIVE_TOOLS
            for e in seq
        )

    def tool_summary(self, run: Dict) -> str:
        """Return a brief comma-separated list of unique tools used."""
        seq = run.get("tool_sequence") or []
        if isinstance(seq, str):
            try:
                seq = json.loads(seq)
            except Exception:
                return "(none)"
        seen: List[str] = []
        for e in seq:
            if isinstance(e, dict):
                t = e.get("tool", "")
                if t and t not in seen:
                    seen.append(t)
        return ", ".join(seen) if seen else "(none)"

    # ── Clone ───────────────────────────────────────────────────────────────

    def clone_user_message(self, run_id: str) -> Optional[str]:
        """
        Return the user_message_preview from a previous run so the caller
        can start a new session pre-filled with that prompt.
        Returns None if the run is not found.
        """
        run = self.get_run(run_id)
        if run is None:
            return None
        return run.get("user_message_preview") or ""

    # ── Replay ──────────────────────────────────────────────────────────────

    def prepare_replay(
        self,
        run_id: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Validate and prepare a replay of a previous run.

        Returns a dict with:
          - user_message:  the original user prompt
          - session_id:    the original session_id (caller may resume it)
          - source:        original source/platform
          - model:         original model
          - is_destructive: bool
          - new_run_id:    pre-generated ID for the replay run

        Raises DestructiveRunError if the original run touched destructive
        tools and force=False.
        """
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id!r} not found")

        is_destructive = self.has_destructive_tools(run)
        if is_destructive and not force:
            raise RunReplayer.DestructiveRunError(
                f"Run {run_id[:8]}… used destructive tools "
                f"({self.tool_summary(run)}). "
                "Pass force=True to proceed — re-approval will be required "
                "for any dangerous commands."
            )

        return {
            "user_message": run.get("user_message_preview") or "",
            "session_id": run.get("session_id") or "",
            "source": run.get("source") or "cli",
            "model": run.get("model") or "",
            "is_destructive": is_destructive,
            "new_run_id": _new_run_id(),
            "parent_run_id": run_id,
        }


# ─── Formatting helpers (used by CLI display) ───────────────────────────────

_STATUS_ICONS = {
    STATUS_RUNNING:        "⏳",
    STATUS_COMPLETED:      "✅",
    STATUS_FAILED:         "❌",
    STATUS_INTERRUPTED:    "⚡",
    STATUS_MAX_ITERATIONS: "⏸",
}

_STATUS_COLORS = {
    STATUS_RUNNING:        "yellow",
    STATUS_COMPLETED:      "green",
    STATUS_FAILED:         "red",
    STATUS_INTERRUPTED:    "yellow",
    STATUS_MAX_ITERATIONS: "dim",
}


def fmt_status(status: str) -> str:
    icon = _STATUS_ICONS.get(status, "?")
    return f"{icon} {status}"


def fmt_duration(run: Dict) -> str:
    started = run.get("started_at")
    ended = run.get("ended_at")
    if not started:
        return "—"
    elapsed = (ended or time.time()) - started
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    return f"{mins}m{secs:02d}s"


def fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_run_id_short(run_id: str) -> str:
    return run_id[:8] if run_id else "—"
