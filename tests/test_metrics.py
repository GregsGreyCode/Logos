"""Tests for metrics.py — MetricsEngine calculations and Prometheus export."""

import json
import time
import uuid
from pathlib import Path

import pytest

from hermes_state import SessionDB
from metrics import MetricsEngine, _pct, _percentile


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


@pytest.fixture()
def engine(db):
    return MetricsEngine(db)


def _run_id():
    return str(uuid.uuid4())


def _insert_run(db, status="completed", source="cli", model="claude-3-5-sonnet",
                provider="anthropic", duration_s=5.0, tool_calls=None,
                approval_events=None, input_tokens=100, output_tokens=50,
                trigger_type="user_message", age_s=None):
    """Insert a complete run record at a given age."""
    rid = _run_id()
    sid = _run_id()
    started = time.time() - (age_s or 1) - duration_s
    db._conn.execute(
        """INSERT INTO runs
           (id, session_id, source, model, provider, trigger_type,
            started_at, ended_at, status,
            input_tokens, output_tokens, tool_sequence, approval_events,
            user_message_preview)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rid, sid, source, model, provider, trigger_type,
            started, started + duration_s, status,
            input_tokens, output_tokens,
            json.dumps(tool_calls) if tool_calls else None,
            json.dumps(approval_events) if approval_events else None,
            "test prompt",
        ),
    )
    db._conn.commit()
    return rid


# ── Pure helpers ──────────────────────────────────────────────────────────────

class TestPureHelpers:
    def test_pct_basic(self):
        assert _pct(1, 4) == 25.0
        assert _pct(0, 10) == 0.0

    def test_pct_zero_denominator(self):
        assert _pct(5, 0) == 0.0

    def test_percentile_single(self):
        assert _percentile([10.0], 50) == 10.0

    def test_percentile_p50(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(values, 50) == 3.0

    def test_percentile_p100(self):
        values = [1.0, 2.0, 3.0]
        assert _percentile(values, 100) == 3.0

    def test_percentile_empty(self):
        assert _percentile([], 50) == 0.0


# ── run_stats ─────────────────────────────────────────────────────────────────

class TestRunStats:
    def test_empty_db(self, engine):
        stats = engine.run_stats(days=7)
        assert stats["total"] == 0
        assert stats["success_rate"] == 0.0

    def test_counts_by_status(self, db, engine):
        _insert_run(db, status="completed")
        _insert_run(db, status="completed")
        _insert_run(db, status="failed")
        _insert_run(db, status="interrupted")

        stats = engine.run_stats(days=7)
        assert stats["total"] == 4
        assert stats["completed"] == 2
        assert stats["failed"] == 1
        assert stats["interrupted"] == 1

    def test_success_rate(self, db, engine):
        _insert_run(db, status="completed")
        _insert_run(db, status="failed")
        stats = engine.run_stats(days=7)
        assert stats["success_rate"] == 50.0

    def test_duration_stats(self, db, engine):
        _insert_run(db, status="completed", duration_s=10.0)
        _insert_run(db, status="completed", duration_s=20.0)
        stats = engine.run_stats(days=7)
        assert stats["duration"]["avg_s"] == 15.0
        assert stats["duration"]["min_s"] == 10.0
        assert stats["duration"]["max_s"] == 20.0

    def test_excludes_runs_outside_window(self, db, engine):
        _insert_run(db, status="completed", age_s=10)       # within 7d
        _insert_run(db, status="completed", age_s=8 * 86400)  # 8 days ago, outside
        stats = engine.run_stats(days=7)
        assert stats["total"] == 1

    def test_by_source(self, db, engine):
        _insert_run(db, status="completed", source="cli")
        _insert_run(db, status="completed", source="telegram")
        stats = engine.run_stats(days=7)
        assert stats["by_source"]["cli"] == 1
        assert stats["by_source"]["telegram"] == 1

    def test_avg_tools_per_run(self, db, engine):
        _insert_run(db, status="completed", tool_calls=[
            {"tool": "web_search", "ok": True},
            {"tool": "file_read", "ok": True},
        ])
        _insert_run(db, status="completed", tool_calls=[])
        stats = engine.run_stats(days=7)
        assert stats["avg_tools_per_run"] == 1.0


# ── tool_stats ────────────────────────────────────────────────────────────────

class TestToolStats:
    def test_empty(self, engine):
        stats = engine.tool_stats(days=7)
        assert stats["total_calls"] == 0
        assert stats["unique_tools"] == 0

    def test_counts_tools(self, db, engine):
        _insert_run(db, status="completed", tool_calls=[
            {"tool": "web_search", "ok": True},
            {"tool": "web_search", "ok": True},
            {"tool": "file_read", "ok": False},
        ])
        stats = engine.tool_stats(days=7)
        assert stats["total_calls"] == 3
        assert stats["unique_tools"] == 2

        tools = {t["tool"]: t for t in stats["tools"]}
        assert tools["web_search"]["calls"] == 2
        assert tools["web_search"]["errors"] == 0
        assert tools["file_read"]["errors"] == 1
        assert tools["file_read"]["error_rate"] == 100.0

    def test_most_used_first(self, db, engine):
        _insert_run(db, status="completed", tool_calls=[
            {"tool": "rare_tool", "ok": True},
            {"tool": "common_tool", "ok": True},
            {"tool": "common_tool", "ok": True},
        ])
        stats = engine.tool_stats(days=7)
        assert stats["tools"][0]["tool"] == "common_tool"


# ── provider_stats ────────────────────────────────────────────────────────────

class TestProviderStats:
    def test_empty(self, engine):
        stats = engine.provider_stats(days=7)
        assert stats["by_model"] == []
        assert stats["by_provider"] == []

    def test_groups_by_model(self, db, engine):
        _insert_run(db, status="completed", model="anthropic/claude-3-5-sonnet",
                    provider="anthropic", input_tokens=100, output_tokens=50)
        _insert_run(db, status="completed", model="anthropic/claude-3-5-sonnet",
                    provider="anthropic", input_tokens=200, output_tokens=75)
        stats = engine.provider_stats(days=7)
        models = {m["model"]: m for m in stats["by_model"]}
        # Model name strips provider prefix
        assert "claude-3-5-sonnet" in models
        m = models["claude-3-5-sonnet"]
        assert m["runs"] == 2
        assert m["input_tokens"] == 300
        assert m["output_tokens"] == 125

    def test_groups_by_provider(self, db, engine):
        _insert_run(db, status="completed", provider="anthropic")
        _insert_run(db, status="completed", provider="openai")
        stats = engine.provider_stats(days=7)
        providers = {p["provider"]: p for p in stats["by_provider"]}
        assert providers["anthropic"]["runs"] == 1
        assert providers["openai"]["runs"] == 1


# ── approval_stats ────────────────────────────────────────────────────────────

class TestApprovalStats:
    def test_empty(self, engine):
        stats = engine.approval_stats(days=7)
        assert stats["total_approval_events"] == 0
        assert stats["denial_rate"] == 0.0

    def test_counts_approvals_and_denials(self, db, engine):
        _insert_run(db, status="completed", approval_events=[
            {"cmd": "ls", "approved": True, "type": "session"},
            {"cmd": "rm -rf /tmp", "approved": False, "type": "session"},
            {"cmd": "cat file", "approved": True, "type": "session"},
        ])
        stats = engine.approval_stats(days=7)
        assert stats["total_approval_events"] == 3
        assert stats["approvals"] == 2
        assert stats["denials"] == 1
        assert abs(stats["denial_rate"] - 33.3) < 0.2

    def test_top_denied_commands(self, db, engine):
        _insert_run(db, status="completed", approval_events=[
            {"cmd": "sudo rm -rf /important", "approved": False},
            {"cmd": "sudo rm -rf /important", "approved": False},
        ])
        stats = engine.approval_stats(days=7)
        assert len(stats["top_denied_commands"]) >= 1
        assert stats["top_denied_commands"][0]["count"] == 2


# ── get_stuck_runs ────────────────────────────────────────────────────────────

class TestStuckRuns:
    def test_no_stuck_runs(self, engine):
        assert engine.get_stuck_runs() == []

    def test_detects_stuck_run(self, db, engine):
        rid = _run_id()
        sid = _run_id()
        # Insert a run that started 2 hours ago and is still 'running'
        old_start = time.time() - 7200
        db._conn.execute(
            """INSERT INTO runs
               (id, session_id, source, model, provider, started_at, status, user_message_preview)
               VALUES (?,?,?,?,?,?,?,?)""",
            (rid, sid, "cli", "test", "anthropic", old_start, "running", "stuck prompt"),
        )
        db._conn.commit()
        stuck = engine.get_stuck_runs()
        assert len(stuck) == 1
        assert stuck[0]["run_id_full"] == rid
        assert stuck[0]["age_hours"] >= 2.0

    def test_recent_running_not_stuck(self, db, engine):
        rid = _run_id()
        sid = _run_id()
        # Started 5 minutes ago — should not be stuck
        db._conn.execute(
            """INSERT INTO runs
               (id, session_id, source, model, provider, started_at, status, user_message_preview)
               VALUES (?,?,?,?,?,?,?,?)""",
            (rid, sid, "cli", "test", "anthropic", time.time() - 300, "running", "recent"),
        )
        db._conn.commit()
        assert engine.get_stuck_runs() == []


# ── export_prometheus ─────────────────────────────────────────────────────────

class TestPrometheusExport:
    def test_basic_format(self, engine):
        output = engine.export_prometheus(days=7)
        assert "logos_runs_total" in output
        assert "logos_runs_completed" in output
        assert "logos_runs_failed" in output
        assert "logos_runs_stuck" in output
        assert "logos_tool_calls_total" in output
        assert "logos_approval_events_total" in output

    def test_has_help_and_type_lines(self, engine):
        output = engine.export_prometheus()
        assert "# HELP" in output
        assert "# TYPE" in output

    def test_includes_tool_entries(self, db, engine):
        _insert_run(db, status="completed", tool_calls=[
            {"tool": "web_search", "ok": True},
        ])
        output = engine.export_prometheus(days=7)
        assert 'tool="web_search"' in output

    def test_includes_model_tokens(self, db, engine):
        _insert_run(db, status="completed", model="claude-opus",
                    input_tokens=500, output_tokens=200)
        output = engine.export_prometheus(days=7)
        assert "logos_model_tokens_total" in output
        assert 'type="input"' in output
        assert 'type="output"' in output
