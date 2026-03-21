"""Tests for evals/ — schema data models, assertion checks, and DB persistence."""

import uuid
from pathlib import Path

import pytest

from evals.assertions import check_assertion
from evals.schema import (
    AssertionConfig,
    AssertionResult,
    EvalCase,
    EvalCaseResult,
    EvalSuite,
    EvalSuiteResult,
)
from hermes_state import SessionDB


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def _completed_run(tool_names=None, approval_events=None):
    """Build a synthetic run record dict as used by assertions."""
    return {
        "status": "completed",
        "tool_sequence": [{"tool": t, "ok": True} for t in (tool_names or [])],
        "approval_events": approval_events or [],
    }


def _failed_run():
    return {"status": "failed", "tool_sequence": [], "approval_events": []}


# ── AssertionConfig serialisation ─────────────────────────────────────────────

class TestAssertionConfig:
    def test_to_dict_round_trip(self):
        cfg = AssertionConfig(type="success", params={})
        d = cfg.to_dict()
        assert d == {"type": "success", "params": {}}
        cfg2 = AssertionConfig.from_dict(d)
        assert cfg2.type == "success"
        assert cfg2.params == {}

    def test_from_dict_with_params(self):
        d = {"type": "contains_keywords", "params": {"keywords": ["hello"], "require_all": True}}
        cfg = AssertionConfig.from_dict(d)
        assert cfg.type == "contains_keywords"
        assert cfg.params["keywords"] == ["hello"]

    def test_from_dict_missing_params_defaults_empty(self):
        cfg = AssertionConfig.from_dict({"type": "success"})
        assert cfg.params == {}


# ── EvalCase / EvalSuite serialisation ───────────────────────────────────────

class TestSchemaSerialisation:
    def test_eval_case_to_dict(self):
        case = EvalCase(
            id="c1", name="My Case",
            input_prompt="Do something",
            description="A test",
            assertions=[AssertionConfig(type="success")],
            tags=["cpu"],
        )
        d = case.to_dict()
        assert d["id"] == "c1"
        assert d["name"] == "My Case"
        assert len(d["assertions"]) == 1
        assert d["assertions"][0]["type"] == "success"

    def test_eval_suite_to_dict(self):
        suite = EvalSuite.create(name="Suite A", description="Testing suite A")
        suite.cases.append(EvalCase(id="c1", name="C1", input_prompt="test"))
        d = suite.to_dict()
        assert d["name"] == "Suite A"
        assert len(d["cases"]) == 1

    def test_eval_case_result_to_dict(self):
        result = EvalCaseResult.create("suite-1", "case-1", "My Case")
        result.passed = True
        result.score = 0.75
        result.assertion_results = [
            AssertionResult("success", True, "OK"),
        ]
        d = result.to_dict()
        assert d["passed"] is True
        assert d["score"] == 0.75
        assert len(d["assertion_results"]) == 1

    def test_eval_suite_result_duration(self):
        result = EvalSuiteResult(
            id=str(uuid.uuid4()), suite_id="s1", suite_name="S1",
            started_at=1000.0, ended_at=1010.0,
            total_cases=2, passed_cases=1, failed_cases=1,
            pass_rate=0.5, case_results=[],
        )
        assert result.duration_seconds == 10.0

    def test_eval_suite_result_summary_contains_pass_info(self):
        case_result = EvalCaseResult.create("s1", "c1", "Case 1")
        case_result.passed = True
        case_result.score = 1.0
        case_result.assertion_results = [AssertionResult("success", True, "ok")]

        suite_result = EvalSuiteResult(
            id=str(uuid.uuid4()), suite_id="s1", suite_name="Test Suite",
            started_at=1000.0, ended_at=1005.0,
            total_cases=1, passed_cases=1, failed_cases=0,
            pass_rate=1.0, case_results=[case_result],
        )
        summary = suite_result.summary()
        assert "Test Suite" in summary
        assert "1/1" in summary
        assert "PASS" in summary


# ── check_assertion: success ──────────────────────────────────────────────────

class TestAssertionSuccess:
    def test_passes_for_completed(self):
        cfg = AssertionConfig(type="success")
        result = check_assertion(cfg, _completed_run(), "")
        assert result.passed is True

    def test_fails_for_failed_run(self):
        cfg = AssertionConfig(type="success")
        result = check_assertion(cfg, _failed_run(), "")
        assert result.passed is False
        assert "failed" in result.reason

    def test_fails_for_no_run_record(self):
        cfg = AssertionConfig(type="success")
        result = check_assertion(cfg, None, "")
        assert result.passed is False


# ── check_assertion: contains_keywords ───────────────────────────────────────

class TestAssertionContainsKeywords:
    def test_any_keyword_match(self):
        cfg = AssertionConfig(
            type="contains_keywords",
            params={"keywords": ["cpu", "memory"], "require_all": False},
        )
        result = check_assertion(cfg, None, "The CPU usage is high")
        assert result.passed is True
        assert "cpu" in result.details["found"]

    def test_any_keyword_no_match(self):
        cfg = AssertionConfig(
            type="contains_keywords",
            params={"keywords": ["cpu", "memory"], "require_all": False},
        )
        result = check_assertion(cfg, None, "Everything is fine")
        assert result.passed is False

    def test_require_all_passes(self):
        cfg = AssertionConfig(
            type="contains_keywords",
            params={"keywords": ["cpu", "memory"], "require_all": True},
        )
        result = check_assertion(cfg, None, "CPU and memory both look fine")
        assert result.passed is True

    def test_require_all_fails_missing_one(self):
        cfg = AssertionConfig(
            type="contains_keywords",
            params={"keywords": ["cpu", "memory"], "require_all": True},
        )
        result = check_assertion(cfg, None, "Only CPU mentioned here")
        assert result.passed is False
        assert "memory" in result.details["missing"]

    def test_case_insensitive(self):
        cfg = AssertionConfig(
            type="contains_keywords",
            params={"keywords": ["CPU"], "require_all": False},
        )
        result = check_assertion(cfg, None, "the cpu is fine")
        assert result.passed is True

    def test_no_keywords_passes(self):
        cfg = AssertionConfig(type="contains_keywords", params={})
        result = check_assertion(cfg, None, "anything")
        assert result.passed is True


# ── check_assertion: tool_used ────────────────────────────────────────────────

class TestAssertionToolUsed:
    def test_tool_present(self):
        cfg = AssertionConfig(type="tool_used", params={"tools": ["web_search"]})
        result = check_assertion(cfg, _completed_run(["web_search"]), "")
        assert result.passed is True

    def test_tool_absent(self):
        cfg = AssertionConfig(type="tool_used", params={"tools": ["web_search"]})
        result = check_assertion(cfg, _completed_run(["file_read"]), "")
        assert result.passed is False

    def test_require_all_true(self):
        cfg = AssertionConfig(
            type="tool_used",
            params={"tools": ["web_search", "file_read"], "require_all": True},
        )
        result = check_assertion(cfg, _completed_run(["web_search", "file_read"]), "")
        assert result.passed is True

    def test_require_all_fails_missing(self):
        cfg = AssertionConfig(
            type="tool_used",
            params={"tools": ["web_search", "file_read"], "require_all": True},
        )
        result = check_assertion(cfg, _completed_run(["web_search"]), "")
        assert result.passed is False
        assert "file_read" in result.details["missing"]

    def test_no_run_record(self):
        cfg = AssertionConfig(type="tool_used", params={"tools": ["web_search"]})
        result = check_assertion(cfg, None, "")
        assert result.passed is False


# ── check_assertion: tool_not_used ───────────────────────────────────────────

class TestAssertionToolNotUsed:
    def test_disallowed_absent(self):
        cfg = AssertionConfig(type="tool_not_used", params={"tools": ["terminal"]})
        result = check_assertion(cfg, _completed_run(["web_search"]), "")
        assert result.passed is True

    def test_disallowed_present(self):
        cfg = AssertionConfig(type="tool_not_used", params={"tools": ["terminal"]})
        result = check_assertion(cfg, _completed_run(["terminal"]), "")
        assert result.passed is False
        assert "terminal" in result.details["violations"]

    def test_multiple_disallowed(self):
        cfg = AssertionConfig(
            type="tool_not_used",
            params={"tools": ["terminal", "bash"]},
        )
        result = check_assertion(cfg, _completed_run(["bash"]), "")
        assert result.passed is False


# ── check_assertion: policy_respected ────────────────────────────────────────

class TestAssertionPolicyRespected:
    def test_no_denials_passes(self):
        cfg = AssertionConfig(type="policy_respected", params={})
        run = _completed_run()
        run["approval_events"] = [{"cmd": "ls", "approved": True}]
        result = check_assertion(cfg, run, "")
        assert result.passed is True

    def test_denial_fails_default(self):
        cfg = AssertionConfig(type="policy_respected", params={})
        run = _completed_run()
        run["approval_events"] = [{"cmd": "rm -rf /", "approved": False}]
        result = check_assertion(cfg, run, "")
        assert result.passed is False
        assert result.details["denied_count"] == 1

    def test_expect_blocked_true_with_denial(self):
        cfg = AssertionConfig(type="policy_respected", params={"expect_blocked": True})
        run = _completed_run()
        run["approval_events"] = [{"cmd": "dangerous", "approved": False}]
        result = check_assertion(cfg, run, "")
        assert result.passed is True

    def test_expect_blocked_true_no_denial(self):
        cfg = AssertionConfig(type="policy_respected", params={"expect_blocked": True})
        result = check_assertion(cfg, _completed_run(), "")
        assert result.passed is False

    def test_no_run_record(self):
        cfg = AssertionConfig(type="policy_respected", params={})
        result = check_assertion(cfg, None, "")
        assert result.passed is True  # no events = no denials = pass


# ── check_assertion: output_matches ──────────────────────────────────────────

class TestAssertionOutputMatches:
    def test_pattern_matches(self):
        cfg = AssertionConfig(
            type="output_matches",
            params={"pattern": r"\d+ percent"},
        )
        result = check_assertion(cfg, None, "CPU is at 85 percent capacity")
        assert result.passed is True

    def test_pattern_no_match(self):
        cfg = AssertionConfig(
            type="output_matches",
            params={"pattern": r"\d+ percent"},
        )
        result = check_assertion(cfg, None, "CPU looks fine")
        assert result.passed is False

    def test_case_insensitive_default(self):
        cfg = AssertionConfig(
            type="output_matches",
            params={"pattern": "ERROR"},
        )
        result = check_assertion(cfg, None, "An error occurred")
        assert result.passed is True

    def test_empty_pattern_passes(self):
        cfg = AssertionConfig(type="output_matches", params={})
        result = check_assertion(cfg, None, "anything")
        assert result.passed is True


# ── check_assertion: unknown type ────────────────────────────────────────────

class TestAssertionUnknownType:
    def test_unknown_type_fails(self):
        cfg = AssertionConfig(type="nonexistent_assertion")
        result = check_assertion(cfg, None, "")
        assert result.passed is False
        assert "Unknown assertion type" in result.reason


# ── DB persistence ────────────────────────────────────────────────────────────

class TestEvalDBPersistence:
    def _make_case_result(self, suite_id="suite-abc", case_id="case-1", name="Test Case"):
        result = EvalCaseResult.create(suite_id, case_id, name)
        result.passed = True
        result.score = 0.8
        result.duration_seconds = 1.5
        result.output_preview = "Agent said: done"
        result.assertion_results = [
            AssertionResult("success", True, "Run completed"),
            AssertionResult("contains_keywords", True, "Found: ['done']"),
        ]
        # started_at is required by the DB schema (NOT NULL) but not in the dataclass;
        # save_eval_case_result reads it via getattr(..., None)
        result.started_at = 1700000000.0
        result.suite_run_id = "suite-run-1"
        return result

    def test_save_and_retrieve(self, db):
        result = self._make_case_result()
        db.save_eval_case_result(result)

        rows = db.list_eval_results()
        assert len(rows) == 1
        row = rows[0]
        assert row["case_name"] == "Test Case"
        assert row["passed"] == 1
        assert abs(row["score"] - 0.8) < 0.001
        assert isinstance(row["assertion_results"], list)
        assert len(row["assertion_results"]) == 2

    def test_list_filter_by_suite_name(self, db):
        r1 = self._make_case_result(suite_id="suite-A")
        r2 = self._make_case_result(suite_id="suite-B", case_id="case-2", name="Other")
        db.save_eval_case_result(r1)
        db.save_eval_case_result(r2)

        rows = db.list_eval_results(suite_name="suite-A")
        assert len(rows) == 1
        assert rows[0]["suite_name"] == "suite-A"

    def test_save_suite_result_persists_all_cases(self, db):
        suite = EvalSuite.create("Test Suite")
        suite.cases = [
            EvalCase(id="c1", name="Case 1", input_prompt="p1"),
            EvalCase(id="c2", name="Case 2", input_prompt="p2"),
        ]
        suite_result = EvalSuiteResult(
            id=str(uuid.uuid4()),
            suite_id=suite.id,
            suite_name=suite.name,
            started_at=1000.0,
            ended_at=1010.0,
            total_cases=2,
            passed_cases=1,
            failed_cases=1,
            pass_rate=0.5,
            case_results=[
                self._make_case_result(suite_id=suite.id, case_id="c1", name="Case 1"),
                self._make_case_result(suite_id=suite.id, case_id="c2", name="Case 2"),
            ],
        )
        db.save_eval_suite_result(suite_result)

        rows = db.list_eval_results(suite_name=suite.id)
        assert len(rows) == 2
