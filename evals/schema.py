"""
Eval schema — data models for evaluation suites, cases, and results.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AssertionConfig:
    """Configuration for a single assertion within an eval case."""
    # Types: success | contains_keywords | tool_used | tool_not_used |
    #        policy_respected | output_matches
    type: str
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {"type": self.type, "params": self.params}

    @classmethod
    def from_dict(cls, d: Dict) -> "AssertionConfig":
        return cls(type=d["type"], params=d.get("params", {}))


@dataclass
class EvalCase:
    """A single test case within an eval suite."""
    id: str
    name: str
    input_prompt: str
    description: str = ""
    expected_behavior: str = ""
    assertions: List[AssertionConfig] = field(default_factory=list)
    toolsets: Optional[List[str]] = None
    model: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    max_iterations: int = 30

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "input_prompt": self.input_prompt,
            "expected_behavior": self.expected_behavior,
            "assertions": [a.to_dict() for a in self.assertions],
            "toolsets": self.toolsets,
            "model": self.model,
            "tags": self.tags,
            "max_iterations": self.max_iterations,
        }


@dataclass
class EvalSuite:
    """A collection of eval cases."""
    id: str
    name: str
    description: str = ""
    cases: List[EvalCase] = field(default_factory=list)

    @classmethod
    def create(cls, name: str, description: str = "") -> "EvalSuite":
        return cls(id=str(uuid.uuid4()), name=name, description=description)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "cases": [c.to_dict() for c in self.cases],
        }


@dataclass
class AssertionResult:
    """Result of evaluating a single assertion."""
    assertion_type: str
    passed: bool
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "assertion_type": self.assertion_type,
            "passed": self.passed,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass
class EvalCaseResult:
    """Result of running a single eval case."""
    id: str
    suite_id: str
    case_id: str
    case_name: str
    run_id: Optional[str]
    passed: bool
    score: float          # 0.0 to 1.0
    assertion_results: List[AssertionResult]
    duration_seconds: float
    output_preview: str = ""
    error: Optional[str] = None

    @classmethod
    def create(cls, suite_id: str, case_id: str, case_name: str) -> "EvalCaseResult":
        return cls(
            id=str(uuid.uuid4()),
            suite_id=suite_id,
            case_id=case_id,
            case_name=case_name,
            run_id=None,
            passed=False,
            score=0.0,
            assertion_results=[],
            duration_seconds=0.0,
        )

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "suite_id": self.suite_id,
            "case_id": self.case_id,
            "case_name": self.case_name,
            "run_id": self.run_id,
            "passed": self.passed,
            "score": self.score,
            "assertion_results": [a.to_dict() for a in self.assertion_results],
            "duration_seconds": self.duration_seconds,
            "output_preview": self.output_preview,
            "error": self.error,
        }


@dataclass
class EvalSuiteResult:
    """Result of running an entire eval suite."""
    id: str
    suite_id: str
    suite_name: str
    started_at: float
    ended_at: float
    total_cases: int
    passed_cases: int
    failed_cases: int
    pass_rate: float
    case_results: List[EvalCaseResult]

    @property
    def duration_seconds(self) -> float:
        return self.ended_at - self.started_at

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "suite_id": self.suite_id,
            "suite_name": self.suite_name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "pass_rate": self.pass_rate,
            "case_results": [r.to_dict() for r in self.case_results],
        }

    def summary(self) -> str:
        lines = [
            f"Eval Suite: {self.suite_name}",
            f"Result: {self.passed_cases}/{self.total_cases} passed "
            f"({self.pass_rate:.0%})",
            f"Duration: {self.duration_seconds:.1f}s",
            "",
        ]
        for r in self.case_results:
            icon = "PASS" if r.passed else "FAIL"
            lines.append(f"  [{icon}] {r.case_name}  (score: {r.score:.2f})")
            for ar in r.assertion_results:
                a_icon = "  ok " if ar.passed else "  FAIL"
                lines.append(f"    {a_icon} {ar.assertion_type}: {ar.reason}")
            if r.error:
                lines.append(f"    ERROR: {r.error}")
        return "\n".join(lines)
