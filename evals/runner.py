"""
Eval runner — executes eval suites against live agent runs and scores results.

The runner reuses the delegation infrastructure (_run_single_child) to spawn
isolated child agents for each eval case, then applies assertions against the
output and any available run record data.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, List, Optional

from evals.assertions import check_assertion
from evals.schema import (
    AssertionResult,
    EvalCase,
    EvalCaseResult,
    EvalSuite,
    EvalSuiteResult,
)

if TYPE_CHECKING:
    from run_agent import AIAgent

logger = logging.getLogger(__name__)


def run_eval_case(
    case: EvalCase,
    suite_id: str,
    parent_agent: "AIAgent",
) -> EvalCaseResult:
    """Run a single eval case using a delegated child agent."""
    from tools.delegate_tool import (
        _load_config,
        _resolve_delegation_credentials,
        _run_single_child,
    )

    result = EvalCaseResult.create(suite_id, case.id, case.name)
    start = time.monotonic()

    try:
        cfg = _load_config()
        try:
            creds = _resolve_delegation_credentials(cfg, parent_agent)
        except ValueError:
            creds = {
                "model": None,
                "provider": None,
                "base_url": None,
                "api_key": None,
                "api_mode": None,
            }

        child_result = _run_single_child(
            task_index=0,
            goal=case.input_prompt,
            context=(
                f"Eval case: {case.name}\n"
                f"Expected behavior: {case.expected_behavior}"
                if case.expected_behavior
                else f"Eval case: {case.name}"
            ),
            toolsets=case.toolsets,
            model=case.model or creds.get("model"),
            max_iterations=case.max_iterations,
            parent_agent=parent_agent,
            task_count=1,
            override_provider=creds.get("provider"),
            override_base_url=creds.get("base_url"),
            override_api_key=creds.get("api_key"),
            override_api_mode=creds.get("api_mode"),
        )

        result.duration_seconds = round(time.monotonic() - start, 2)
        result.output_preview = (child_result.get("summary") or "")[:500]

        if child_result.get("status") == "error":
            result.error = child_result.get("error", "Unknown error")
            result.assertion_results = [
                AssertionResult(
                    "success", False,
                    f"Agent error: {result.error}",
                )
            ]
            result.passed = False
            result.score = 0.0
            return result

        # Build a synthetic run record from the child's tool trace
        # (real run_id tracking requires parent_run_id wiring — see runs.py)
        synthetic_run = {
            "status": (
                "completed" if child_result.get("status") == "completed"
                else "failed"
            ),
            "tool_sequence": [
                {
                    "tool": t.get("tool", "unknown"),
                    "ok": t.get("status") != "error",
                }
                for t in (child_result.get("tool_trace") or [])
            ],
            "approval_events": [],
        }

        output_text = child_result.get("summary") or ""
        assertion_results = [
            check_assertion(ac, synthetic_run, output_text)
            for ac in case.assertions
        ]

        result.assertion_results = assertion_results

        if assertion_results:
            passed_count = sum(1 for ar in assertion_results if ar.passed)
            result.score = passed_count / len(assertion_results)
            result.passed = result.score == 1.0
        else:
            result.passed = child_result.get("status") == "completed"
            result.score = 1.0 if result.passed else 0.0

    except Exception as exc:
        logger.exception("Eval case %r failed: %s", case.name, exc)
        result.error = str(exc)
        result.passed = False
        result.score = 0.0
        result.duration_seconds = round(time.monotonic() - start, 2)

    return result


def run_eval_suite(
    suite: EvalSuite,
    parent_agent: "AIAgent",
    db=None,
) -> EvalSuiteResult:
    """Run all cases in an eval suite and return aggregated results.

    Args:
        suite:        The eval suite to run.
        parent_agent: Agent whose credentials and session_db are inherited.
        db:           Optional SessionDB for persisting results (best-effort).
    """
    started_at = time.time()
    case_results: List[EvalCaseResult] = []

    for case in suite.cases:
        logger.info("[eval] Running case: %s", case.name)
        case_result = run_eval_case(case, suite.id, parent_agent)
        case_results.append(case_result)

        if db:
            try:
                db.save_eval_case_result(case_result)
            except Exception as exc:
                logger.debug("Failed to persist eval case result: %s", exc)

    ended_at = time.time()
    passed_cases = sum(1 for r in case_results if r.passed)
    total_cases = len(case_results)
    pass_rate = passed_cases / total_cases if total_cases > 0 else 0.0

    suite_result = EvalSuiteResult(
        id=str(uuid.uuid4()),
        suite_id=suite.id,
        suite_name=suite.name,
        started_at=started_at,
        ended_at=ended_at,
        total_cases=total_cases,
        passed_cases=passed_cases,
        failed_cases=total_cases - passed_cases,
        pass_rate=pass_rate,
        case_results=case_results,
    )

    if db:
        try:
            db.save_eval_suite_result(suite_result)
        except Exception as exc:
            logger.debug("Failed to persist eval suite result: %s", exc)

    return suite_result
