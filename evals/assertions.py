"""
Assertion functions for eval cases.

Each assertion takes:
  - cfg: AssertionConfig with type + params
  - run_record: dict from runs table (may be None or synthetic)
  - output_text: the agent's final response text

Returns an AssertionResult.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from evals.schema import AssertionConfig, AssertionResult


def check_assertion(
    cfg: AssertionConfig,
    run_record: Optional[Dict],
    output_text: str,
) -> AssertionResult:
    """Dispatch a single assertion check."""
    dispatch = {
        "success":           _check_success,
        "contains_keywords": _check_contains_keywords,
        "tool_used":         _check_tool_used,
        "tool_not_used":     _check_tool_not_used,
        "policy_respected":  _check_policy_respected,
        "output_matches":    _check_output_matches,
    }
    fn = dispatch.get(cfg.type)
    if fn is None:
        return AssertionResult(
            assertion_type=cfg.type,
            passed=False,
            reason=f"Unknown assertion type: {cfg.type!r}",
        )
    return fn(cfg, run_record, output_text)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_tool_sequence(run_record: Optional[Dict]) -> List[Dict]:
    if not run_record:
        return []
    seq = run_record.get("tool_sequence") or []
    if isinstance(seq, str):
        try:
            seq = json.loads(seq)
        except Exception:
            return []
    return seq if isinstance(seq, list) else []


def _get_approval_events(run_record: Optional[Dict]) -> List[Dict]:
    if not run_record:
        return []
    events = run_record.get("approval_events") or []
    if isinstance(events, str):
        try:
            events = json.loads(events)
        except Exception:
            return []
    return events if isinstance(events, list) else []


# ── Assertion implementations ─────────────────────────────────────────────────

def _check_success(cfg: AssertionConfig, run_record, output_text: str) -> AssertionResult:
    if not run_record:
        return AssertionResult("success", False, "No run record found")
    status = run_record.get("status", "")
    passed = status == "completed"
    reason = "Run completed successfully" if passed else f"Run status: {status!r}"
    return AssertionResult("success", passed, reason)


def _check_contains_keywords(cfg: AssertionConfig, run_record, output_text: str) -> AssertionResult:
    keywords = cfg.params.get("keywords", [])
    require_all = cfg.params.get("require_all", False)
    if not keywords:
        return AssertionResult("contains_keywords", True, "No keywords specified")

    text_lower = output_text.lower()
    found = [kw for kw in keywords if kw.lower() in text_lower]
    missing = [kw for kw in keywords if kw.lower() not in text_lower]

    if require_all:
        passed = len(missing) == 0
        reason = (
            f"All {len(keywords)} keywords found" if passed
            else f"Missing keywords: {missing}"
        )
    else:
        passed = len(found) > 0
        reason = (
            f"Found keywords: {found}" if passed
            else f"None of the expected keywords found: {keywords}"
        )

    return AssertionResult(
        "contains_keywords", passed, reason,
        {"found": found, "missing": missing},
    )


def _check_tool_used(cfg: AssertionConfig, run_record, output_text: str) -> AssertionResult:
    tools = cfg.params.get("tools", [])
    require_all = cfg.params.get("require_all", False)
    seq = _get_tool_sequence(run_record)
    tools_in_seq = {e.get("tool") for e in seq if isinstance(e, dict)}

    found = [t for t in tools if t in tools_in_seq]
    missing = [t for t in tools if t not in tools_in_seq]

    if require_all:
        passed = len(missing) == 0
        reason = (
            f"All required tools used: {tools}" if passed
            else f"Required tools not used: {missing}"
        )
    else:
        passed = len(found) > 0
        reason = (
            f"At least one required tool used: {found}" if passed
            else f"None of the required tools were used: {tools}"
        )

    return AssertionResult(
        "tool_used", passed, reason,
        {"found": found, "missing": missing},
    )


def _check_tool_not_used(cfg: AssertionConfig, run_record, output_text: str) -> AssertionResult:
    disallowed = cfg.params.get("tools", [])
    seq = _get_tool_sequence(run_record)
    tools_in_seq = {e.get("tool") for e in seq if isinstance(e, dict)}

    violations = [t for t in disallowed if t in tools_in_seq]
    passed = len(violations) == 0
    reason = (
        "No disallowed tools used" if passed
        else f"Disallowed tools were used: {violations}"
    )

    return AssertionResult(
        "tool_not_used", passed, reason,
        {"violations": violations},
    )


def _check_policy_respected(cfg: AssertionConfig, run_record, output_text: str) -> AssertionResult:
    """Check approval events.

    By default, asserts no commands were denied (policy was respected).
    With expect_blocked=True, asserts at least one command was blocked.
    """
    events = _get_approval_events(run_record)
    denied = [e for e in events if isinstance(e, dict) and not e.get("approved", True)]

    if cfg.params.get("expect_blocked", False):
        passed = len(denied) > 0
        reason = (
            f"Policy correctly blocked {len(denied)} command(s)" if passed
            else "Expected a command to be blocked but none were"
        )
    else:
        passed = len(denied) == 0
        reason = (
            "No commands were blocked" if passed
            else (
                f"{len(denied)} command(s) blocked by policy: "
                f"{[e.get('cmd', '')[:50] for e in denied[:3]]}"
            )
        )

    return AssertionResult(
        "policy_respected", passed, reason,
        {"denied_count": len(denied)},
    )


def _check_output_matches(cfg: AssertionConfig, run_record, output_text: str) -> AssertionResult:
    pattern = cfg.params.get("pattern", "")
    if not pattern:
        return AssertionResult("output_matches", True, "No pattern specified")

    flags = re.IGNORECASE if cfg.params.get("case_insensitive", True) else 0
    match = re.search(pattern, output_text, flags)
    passed = match is not None
    reason = (
        f"Output matches pattern {pattern!r}" if passed
        else f"Output does not match pattern {pattern!r}"
    )
    return AssertionResult("output_matches", passed, reason)
