"""Workflow data model — definitions, runs, and step state.

A WorkflowDefinition is a reusable template composed of ordered StepDefinitions.
A WorkflowRun is a single execution instance of a definition.
A WorkflowStepRun tracks the live state of one step within a run.

Execution semantics:
  - Steps run in dependency order (depends_on=[]).
  - Steps sharing a parallel_group run concurrently within the same wave.
  - Steps whose condition evaluates to false are skipped (not failed).
  - approval steps pause the run until a human decides.
"""

from __future__ import annotations

import dataclasses
import json
import time
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class StepType(str, Enum):
    INSPECT  = "inspect"    # Read-only information gathering
    REASON   = "reason"     # Synthesise / analyse gathered data
    PROPOSE  = "propose"    # Generate a plan, diff, or action proposal
    APPLY    = "apply"      # Execute a change (policy-gated, may need approval)
    VALIDATE = "validate"   # Verify outcome / assert success criteria
    APPROVAL = "approval"   # Human gate — pauses the run until decided


class RunStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    PAUSED    = "paused"       # waiting on an approval step
    SUCCESS   = "success"
    FAILED    = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING          = "pending"
    RUNNING          = "running"
    SUCCESS          = "success"
    FAILED           = "failed"
    SKIPPED          = "skipped"           # condition not met → step bypassed
    WAITING_APPROVAL = "waiting_approval"  # approval type, awaiting human
    CANCELLED        = "cancelled"


# ---------------------------------------------------------------------------
# Step definition (part of a workflow template)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class StepDefinition:
    """One step in a workflow definition."""

    id:             str                              # unique within workflow
    type:           StepType
    name:           str
    prompt:         str        = ""                 # instruction for the agent
    tools:          list[str]  = dataclasses.field(default_factory=list)
    model_alias:    str        = "balanced"
    timeout:        int        = 180                # seconds
    depends_on:     list[str]  = dataclasses.field(default_factory=list)
    parallel_group: Optional[str] = None            # same value → run concurrently
    condition:      Optional[str] = None            # e.g. "gather.status == 'success'"
    context_from:   list[str]  = dataclasses.field(default_factory=list)
    continue_on_failure: bool  = False              # don't halt run if this step fails
    metadata:       dict       = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "StepDefinition":
        return cls(
            id=d["id"],
            type=StepType(d["type"]),
            name=d.get("name", d["id"]),
            prompt=d.get("prompt", ""),
            tools=d.get("tools", []),
            model_alias=d.get("model_alias", "balanced"),
            timeout=d.get("timeout", 180),
            depends_on=d.get("depends_on", []),
            parallel_group=d.get("parallel_group"),
            condition=d.get("condition"),
            context_from=d.get("context_from", []),
            continue_on_failure=d.get("continue_on_failure", False),
            metadata=d.get("metadata", {}),
        )

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["type"] = self.type.value
        return d


# ---------------------------------------------------------------------------
# Workflow definition (reusable template)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class WorkflowDefinition:
    """A named, versioned workflow template."""

    id:          str
    name:        str
    description: str        = ""
    version:     str        = "1.0"
    steps:       list[StepDefinition] = dataclasses.field(default_factory=list)
    tags:        list[str]  = dataclasses.field(default_factory=list)
    created_by:  Optional[str] = None
    created_at:  int        = dataclasses.field(default_factory=lambda: int(time.time() * 1000))
    updated_at:  int        = dataclasses.field(default_factory=lambda: int(time.time() * 1000))

    @classmethod
    def from_row(cls, row: dict) -> "WorkflowDefinition":
        steps_raw = json.loads(row.get("steps_json") or "[]")
        return cls(
            id=row["id"],
            name=row["name"],
            description=row.get("description") or "",
            version=row.get("version") or "1.0",
            steps=[StepDefinition.from_dict(s) for s in steps_raw],
            tags=json.loads(row.get("tags") or "[]"),
            created_by=row.get("created_by"),
            created_at=row.get("created_at", 0),
            updated_at=row.get("updated_at", 0),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "steps": [s.to_dict() for s in self.steps],
            "tags": self.tags,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "step_count": len(self.steps),
        }

    def to_db_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "steps_json": json.dumps([s.to_dict() for s in self.steps]),
            "tags": json.dumps(self.tags),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
