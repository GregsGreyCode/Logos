"""Tests for runs.py — RunRecorder, RunReplayer, and formatting helpers."""

import time
import uuid
from pathlib import Path

import pytest

from hermes_state import SessionDB
from runs import (
    RunRecorder,
    RunReplayer,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    _DESTRUCTIVE_TOOLS,
    fmt_duration,
    fmt_run_id_short,
    fmt_status,
    fmt_ts,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def _run_id():
    return str(uuid.uuid4())


def _make_run(db, run_id=None, session_id=None, source="cli", model="test-model",
              provider="anthropic", message="hello", trigger_type="user_message"):
    run_id = run_id or _run_id()
    session_id = session_id or _run_id()
    db.create_run(
        run_id=run_id,
        session_id=session_id,
        source=source,
        model=model,
        provider=provider,
        user_message_preview=message,
        trigger_type=trigger_type,
    )
    return run_id, session_id


# ── RunRecorder ───────────────────────────────────────────────────────────────

class TestRunRecorder:
    def test_creates_run_in_db(self, db):
        rid = _run_id()
        rec = RunRecorder(
            db=db, run_id=rid, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message="hello",
        )
        assert rec._active
        row = db.get_run(rid)
        assert row is not None
        assert row["status"] == "running"
        assert row["user_message_preview"] == "hello"

    def test_record_tool_call_persists(self, db):
        rid = _run_id()
        rec = RunRecorder(
            db=db, run_id=rid, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message="test",
        )
        rec.record_tool_call("terminal", args_preview="ls -la", success=True, duration_ms=42.5)

        row = db.get_run(rid)
        seq = row["tool_sequence"]
        assert isinstance(seq, list)
        assert len(seq) == 1
        assert seq[0]["tool"] == "terminal"
        assert seq[0]["ok"] is True
        assert seq[0]["ms"] == 42.5

    def test_record_tool_call_with_error(self, db):
        rid = _run_id()
        rec = RunRecorder(
            db=db, run_id=rid, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message="test",
        )
        rec.record_tool_call("bash", success=False, error="permission denied")

        row = db.get_run(rid)
        seq = row["tool_sequence"]
        assert seq[0]["ok"] is False
        assert "permission denied" in seq[0]["err"]

    def test_record_approval(self, db):
        rid = _run_id()
        rec = RunRecorder(
            db=db, run_id=rid, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message="test",
        )
        rec.record_approval("rm -rf /tmp/foo", approved=True)
        rec.record_approval("sudo shutdown", approved=False)

        row = db.get_run(rid)
        events = row["approval_events"]
        assert len(events) == 2
        assert events[0]["approved"] is True
        assert events[1]["approved"] is False

    def test_finish_sets_status_and_tokens(self, db):
        rid = _run_id()
        rec = RunRecorder(
            db=db, run_id=rid, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message="test",
        )
        rec.finish(
            status=STATUS_COMPLETED,
            final_response="Done!",
            api_call_count=3,
            input_tokens=100,
            output_tokens=50,
        )

        row = db.get_run(rid)
        assert row["status"] == STATUS_COMPLETED
        assert row["api_call_count"] == 3
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["final_output_preview"] == "Done!"

    def test_finish_marks_inactive(self, db):
        rid = _run_id()
        rec = RunRecorder(
            db=db, run_id=rid, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message="test",
        )
        rec.finish(status=STATUS_COMPLETED)
        assert not rec._active

    def test_inactive_recorder_is_noop(self, db):
        rid = _run_id()
        rec = RunRecorder(
            db=db, run_id=rid, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message="test",
        )
        rec.finish(status=STATUS_COMPLETED)
        # These should not raise or write anything
        rec.record_tool_call("terminal")
        rec.record_approval("cmd", approved=True)
        rec.finish(status=STATUS_FAILED)

    def test_user_message_truncated_to_200(self, db):
        rid = _run_id()
        long_msg = "x" * 500
        rec = RunRecorder(
            db=db, run_id=rid, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message=long_msg,
        )
        row = db.get_run(rid)
        assert len(row["user_message_preview"]) == 200

    def test_parent_run_id_stored(self, db):
        parent_id = _run_id()
        child_id = _run_id()
        _make_run(db, run_id=parent_id)
        rec = RunRecorder(
            db=db, run_id=child_id, session_id=_run_id(),
            source="cli", model="test", provider="anthropic",
            user_message="child task",
            parent_run_id=parent_id,
        )
        row = db.get_run(child_id)
        assert row["parent_run_id"] == parent_id


# ── RunReplayer ───────────────────────────────────────────────────────────────

class TestRunReplayer:
    def _create_completed_run(self, db, tools=None, message="do something"):
        rid, sid = _make_run(db, message=message)
        tool_seq = [{"tool": t, "ok": True} for t in (tools or [])]
        db.end_run(
            run_id=rid,
            status=STATUS_COMPLETED,
            tool_sequence=tool_seq if tool_seq else None,
        )
        return rid, sid

    def test_get_run_returns_dict(self, db):
        rid, _ = _make_run(db)
        replayer = RunReplayer(db)
        row = replayer.get_run(rid)
        assert row is not None
        assert row["id"] == rid

    def test_get_run_missing_returns_none(self, db):
        replayer = RunReplayer(db)
        assert replayer.get_run("nonexistent-id") is None

    def test_list_runs_returns_all(self, db):
        _make_run(db)
        _make_run(db)
        replayer = RunReplayer(db)
        runs = replayer.list_runs()
        assert len(runs) >= 2

    def test_list_runs_filter_by_status(self, db):
        rid, _ = _make_run(db)
        db.end_run(rid, STATUS_COMPLETED)
        rid2, _ = _make_run(db)
        # rid2 stays 'running'
        replayer = RunReplayer(db)
        completed = replayer.list_runs(status=STATUS_COMPLETED)
        assert all(r["status"] == STATUS_COMPLETED for r in completed)
        running = replayer.list_runs(status=STATUS_RUNNING)
        assert any(r["id"] == rid2 for r in running)

    def test_has_destructive_tools_true(self, db):
        rid, _ = self._create_completed_run(db, tools=["terminal"])
        replayer = RunReplayer(db)
        run = replayer.get_run(rid)
        assert replayer.has_destructive_tools(run) is True

    def test_has_destructive_tools_false(self, db):
        rid, _ = self._create_completed_run(db, tools=["web_search"])
        replayer = RunReplayer(db)
        run = replayer.get_run(rid)
        assert replayer.has_destructive_tools(run) is False

    def test_has_destructive_tools_empty(self, db):
        rid, _ = self._create_completed_run(db, tools=[])
        replayer = RunReplayer(db)
        run = replayer.get_run(rid)
        assert replayer.has_destructive_tools(run) is False

    def test_tool_summary_deduplicates(self, db):
        rid, _ = _make_run(db)
        db.end_run(rid, STATUS_COMPLETED, tool_sequence=[
            {"tool": "web_search", "ok": True},
            {"tool": "web_search", "ok": True},
            {"tool": "file_read", "ok": True},
        ])
        replayer = RunReplayer(db)
        run = replayer.get_run(rid)
        summary = replayer.tool_summary(run)
        assert "web_search" in summary
        assert "file_read" in summary
        # deduped: web_search should appear only once
        assert summary.count("web_search") == 1

    def test_tool_summary_empty(self, db):
        rid, _ = self._create_completed_run(db)
        replayer = RunReplayer(db)
        run = replayer.get_run(rid)
        assert replayer.tool_summary(run) == "(none)"

    def test_clone_user_message(self, db):
        rid, _ = _make_run(db, message="original prompt")
        db.end_run(rid, STATUS_COMPLETED)
        replayer = RunReplayer(db)
        msg = replayer.clone_user_message(rid)
        assert msg == "original prompt"

    def test_clone_user_message_missing_run(self, db):
        replayer = RunReplayer(db)
        assert replayer.clone_user_message("no-such-run") is None

    def test_prepare_replay_safe_run(self, db):
        rid, sid = self._create_completed_run(db, tools=["web_search"], message="search the web")
        replayer = RunReplayer(db)
        info = replayer.prepare_replay(rid)
        assert info["user_message"] == "search the web"
        assert info["session_id"] == sid
        assert info["is_destructive"] is False
        assert "new_run_id" in info
        assert info["parent_run_id"] == rid

    def test_prepare_replay_destructive_raises(self, db):
        rid, _ = self._create_completed_run(db, tools=["terminal"])
        replayer = RunReplayer(db)
        with pytest.raises(RunReplayer.DestructiveRunError):
            replayer.prepare_replay(rid)

    def test_prepare_replay_destructive_force(self, db):
        rid, _ = self._create_completed_run(db, tools=["terminal"])
        replayer = RunReplayer(db)
        info = replayer.prepare_replay(rid, force=True)
        assert info["is_destructive"] is True

    def test_prepare_replay_missing_run_raises(self, db):
        replayer = RunReplayer(db)
        with pytest.raises(ValueError, match="not found"):
            replayer.prepare_replay("no-such-run")


# ── Formatting helpers ────────────────────────────────────────────────────────

class TestFormatting:
    def test_fmt_status_known(self):
        assert "completed" in fmt_status(STATUS_COMPLETED)
        assert "✅" in fmt_status(STATUS_COMPLETED)
        assert "failed" in fmt_status(STATUS_FAILED)
        assert "❌" in fmt_status(STATUS_FAILED)

    def test_fmt_status_unknown(self):
        result = fmt_status("unknown_status")
        assert "unknown_status" in result

    def test_fmt_duration_seconds(self):
        run = {"started_at": time.time() - 10, "ended_at": time.time()}
        result = fmt_duration(run)
        assert result.endswith("s")
        assert "10" in result or "9" in result  # float rounding

    def test_fmt_duration_minutes(self):
        run = {"started_at": time.time() - 90, "ended_at": time.time()}
        result = fmt_duration(run)
        assert "m" in result

    def test_fmt_duration_no_start(self):
        assert fmt_duration({}) == "—"

    def test_fmt_ts_returns_formatted(self):
        ts = 1700000000.0
        result = fmt_ts(ts)
        assert "2023" in result  # Nov 2023

    def test_fmt_ts_none(self):
        assert fmt_ts(None) == "—"
        assert fmt_ts(0) == "—"

    def test_fmt_run_id_short(self):
        rid = "abcdef12-1234-5678-abcd-ef1234567890"
        assert fmt_run_id_short(rid) == "abcdef12"

    def test_fmt_run_id_short_empty(self):
        assert fmt_run_id_short("") == "—"
