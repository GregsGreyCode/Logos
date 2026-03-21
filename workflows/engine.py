"""Workflow execution engine.

WorkflowEngine executes workflow definitions by:
  1. Creating a WorkflowRun record with per-step WorkflowStepRun rows.
  2. Resolving step dependencies via a wave-based topological traversal.
  3. Running each wave: sequential steps one-at-a-time; steps sharing a
     parallel_group concurrently via asyncio.gather.
  4. Dispatching each step to its type-specific executor, which calls the
     existing GatewayRunner._run_agent() for agent-based step types.
  5. Pausing on approval steps (status=waiting_approval / run=paused) until
     resume_approval() is called.
  6. Handling cancellation and failure propagation cleanly.

Public API:
    engine = WorkflowEngine(runner)
    run_id = await engine.start_run(workflow_id, triggered_by, inputs)
    await engine.cancel_run(run_id)
    await engine.resume_approval(approval_id, approved, decided_by)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import types
import uuid
from typing import Any, Optional

from gateway.auth import db as _db
from workflows.model import (
    RunStatus, StepDefinition, StepStatus, StepType, WorkflowDefinition,
)

logger = logging.getLogger(__name__)

_now_ms = lambda: int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------

def _eval_condition(condition: str, ctx: dict[str, dict]) -> bool:
    """Evaluate a condition expression against the step-result context.

    Context is a dict of step_id → {status, output, ...}.
    The expression can reference:
        step_id.status == 'success'
        step_id.status != 'failed'
        gather.status == 'success' and logs.status == 'success'

    Returns True if evaluation fails (fail-open: prefer running to skipping
    when the condition is malformed).
    """
    ns = {step_id: types.SimpleNamespace(**state) for step_id, state in ctx.items()}
    try:
        return bool(eval(condition, {"__builtins__": {}}, ns))  # noqa: S307
    except Exception as exc:
        logger.warning("Condition eval error (%r): %s — defaulting to True", condition, exc)
        return True


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class WorkflowEngine:
    """Asynchronous workflow execution engine.

    Receives a GatewayRunner reference so it can call _run_agent() for
    agent-backed steps.  The runner is expected to outlive the engine.
    """

    def __init__(self, runner: Any) -> None:
        self._runner = runner
        # run_id → asyncio.Task
        self._active_runs: dict[str, asyncio.Task] = {}
        # approval_request_id → asyncio.Event
        self._approval_events: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def start_run(
        self,
        workflow_id: str,
        triggered_by: Optional[str] = None,
        inputs: Optional[dict] = None,
        action_policy: Any = None,
        auth_user_id: Optional[str] = None,
    ) -> str:
        """Create a run record and schedule execution as an asyncio task.

        Returns the new run_id immediately.
        """
        row = _db.get_workflow_definition(workflow_id)
        if not row:
            raise ValueError(f"Workflow not found: {workflow_id}")
        wf = WorkflowDefinition.from_row(row)

        run_id = _db.create_workflow_run(
            workflow_id=workflow_id,
            triggered_by=triggered_by,
            inputs=inputs or {},
        )

        task = asyncio.create_task(
            self._execute_run(run_id, wf, inputs or {}, action_policy, auth_user_id),
            name=f"wf_{run_id}",
        )
        self._active_runs[run_id] = task
        task.add_done_callback(lambda _: self._active_runs.pop(run_id, None))
        logger.info("Workflow run %s started (workflow=%s)", run_id, workflow_id)
        return run_id

    async def cancel_run(self, run_id: str) -> None:
        """Cancel a running or paused workflow."""
        task = self._active_runs.get(run_id)
        if task and not task.done():
            task.cancel()
        _db.update_workflow_run(run_id, status=RunStatus.CANCELLED, finished_at=_now_ms())

    async def resume_approval(
        self,
        approval_id: str,
        approved: bool,
        decided_by: Optional[str] = None,
    ) -> None:
        """Called after an operator approves or rejects an approval step.

        Updates the approval_requests record then sets the asyncio event so
        the waiting coroutine can continue execution.
        """
        status = "approved" if approved else "rejected"
        _db.resolve_approval_request(approval_id, status=status, decided_by=decided_by)
        event = self._approval_events.get(approval_id)
        if event:
            event.set()

    # ------------------------------------------------------------------ #
    #  Run execution                                                       #
    # ------------------------------------------------------------------ #

    async def _execute_run(
        self,
        run_id: str,
        wf: WorkflowDefinition,
        inputs: dict,
        action_policy: Any,
        auth_user_id: Optional[str],
    ) -> None:
        _db.update_workflow_run(run_id, status=RunStatus.RUNNING, started_at=_now_ms())

        # Pre-create step run rows so the UI has visibility from the start.
        for step_def in wf.steps:
            _db.create_workflow_step_run(run_id, step_def)

        # step_id → {status, output, error} — the shared context for conditions.
        ctx: dict[str, dict] = {}

        try:
            remaining = list(wf.steps)

            while remaining:
                # Find the next wave: all steps whose deps are fully satisfied.
                wave = [s for s in remaining if all(d in ctx for d in s.depends_on)]
                if not wave:
                    # Blocked — no progress possible (circular dep or all blocked).
                    blocked_ids = [s.id for s in remaining]
                    raise RuntimeError(
                        f"Execution blocked: steps {blocked_ids} cannot proceed. "
                        "Check for circular depends_on or missing dependency step IDs."
                    )

                for step in wave:
                    remaining.remove(step)

                # Evaluate conditions; skip steps that don't pass.
                executable: list[StepDefinition] = []
                for step in wave:
                    if step.condition and not _eval_condition(step.condition, ctx):
                        _db.update_step_run(
                            run_id, step.id,
                            status=StepStatus.SKIPPED,
                            started_at=_now_ms(), finished_at=_now_ms(),
                            output_summary="Skipped: condition not met",
                        )
                        ctx[step.id] = {"status": StepStatus.SKIPPED, "output": "", "error": ""}
                        logger.info("Step %s skipped (condition=%r)", step.id, step.condition)
                    else:
                        executable.append(step)

                if not executable:
                    continue

                # Split into sequential (no parallel_group) and grouped.
                groups: dict[str, list[StepDefinition]] = {}
                sequential: list[StepDefinition] = []
                for step in executable:
                    if step.parallel_group:
                        groups.setdefault(step.parallel_group, []).append(step)
                    else:
                        sequential.append(step)

                # Sequential steps — one at a time, fail-fast by default.
                for step in sequential:
                    result = await self._execute_step(
                        step, run_id, ctx, inputs, action_policy, auth_user_id
                    )
                    ctx[step.id] = result
                    if result["status"] == StepStatus.FAILED and not step.continue_on_failure:
                        raise RuntimeError(
                            f"Step '{step.id}' failed: {result.get('error', 'unknown error')}"
                        )

                # Parallel groups — all steps in a group run concurrently.
                for group_name, group_steps in groups.items():
                    logger.info(
                        "Running parallel group '%s' (%d steps)",
                        group_name, len(group_steps),
                    )
                    results = await asyncio.gather(
                        *[
                            self._execute_step(s, run_id, ctx, inputs, action_policy, auth_user_id)
                            for s in group_steps
                        ],
                        return_exceptions=True,
                    )
                    for step, result in zip(group_steps, results):
                        if isinstance(result, BaseException):
                            err_str = str(result)
                            ctx[step.id] = {"status": StepStatus.FAILED, "output": "", "error": err_str}
                            _db.update_step_run(
                                run_id, step.id,
                                status=StepStatus.FAILED, error=err_str, finished_at=_now_ms(),
                            )
                        else:
                            ctx[step.id] = result
                    # Check for fatal failures in the group.
                    for step, result in zip(group_steps, results):
                        if (
                            isinstance(result, BaseException) or
                            (isinstance(result, dict) and result.get("status") == StepStatus.FAILED)
                        ) and not step.continue_on_failure:
                            raise RuntimeError(
                                f"Parallel group '{group_name}': step '{step.id}' failed"
                            )

            # All steps finished.
            output_summary = {
                sid: {"status": v["status"], "output": (v.get("output") or "")[:500]}
                for sid, v in ctx.items()
            }
            _db.update_workflow_run(
                run_id,
                status=RunStatus.SUCCESS,
                finished_at=_now_ms(),
                output_json=json.dumps(output_summary),
            )
            logger.info("Workflow run %s completed successfully", run_id)

        except asyncio.CancelledError:
            _db.update_workflow_run(run_id, status=RunStatus.CANCELLED, finished_at=_now_ms())
            # Mark in-progress steps as cancelled.
            self._cancel_pending_steps(run_id, wf, ctx)
            logger.info("Workflow run %s cancelled", run_id)

        except Exception as exc:
            _db.update_workflow_run(
                run_id, status=RunStatus.FAILED, finished_at=_now_ms(), error=str(exc)
            )
            self._cancel_pending_steps(run_id, wf, ctx)
            logger.error("Workflow run %s failed: %s", run_id, exc)

    def _cancel_pending_steps(
        self, run_id: str, wf: WorkflowDefinition, ctx: dict
    ) -> None:
        for step_def in wf.steps:
            if step_def.id not in ctx:
                _db.update_step_run(
                    run_id, step_def.id,
                    status=StepStatus.CANCELLED, finished_at=_now_ms(),
                )

    # ------------------------------------------------------------------ #
    #  Step dispatch                                                       #
    # ------------------------------------------------------------------ #

    async def _execute_step(
        self,
        step_def: StepDefinition,
        run_id: str,
        ctx: dict,
        inputs: dict,
        action_policy: Any,
        auth_user_id: Optional[str],
    ) -> dict:
        """Dispatch a step to its type handler. Returns {status, output, error}."""
        _db.update_step_run(run_id, step_def.id, status=StepStatus.RUNNING, started_at=_now_ms())
        logger.info("Step %s/%s starting (%s)", run_id[:8], step_def.id, step_def.type.value)

        context_prompt = self._build_context_prompt(step_def, ctx, inputs)

        try:
            if step_def.type == StepType.APPROVAL:
                result = await self._execute_approval(step_def, run_id)
            else:
                result = await self._execute_agent_step(
                    step_def, run_id, context_prompt, action_policy, auth_user_id
                )

            _db.update_step_run(
                run_id, step_def.id,
                status=result["status"],
                output_summary=(result.get("output") or "")[:4000],
                finished_at=_now_ms(),
                error=result.get("error"),
            )
            return result

        except asyncio.CancelledError:
            _db.update_step_run(
                run_id, step_def.id, status=StepStatus.CANCELLED, finished_at=_now_ms()
            )
            raise
        except Exception as exc:
            _db.update_step_run(
                run_id, step_def.id,
                status=StepStatus.FAILED, error=str(exc), finished_at=_now_ms(),
            )
            return {"status": StepStatus.FAILED, "output": "", "error": str(exc)}

    def _build_context_prompt(
        self, step_def: StepDefinition, ctx: dict, inputs: dict
    ) -> str:
        """Build the context_prompt string injected into the agent call."""
        parts: list[str] = []
        if inputs:
            parts.append(f"Workflow inputs:\n{json.dumps(inputs, indent=2)}")

        # Step type guidance prepended to the prompt.
        type_guidance = {
            StepType.INSPECT:  "You are in an INSPECT step. Gather information only. Do not make changes.",
            StepType.REASON:   "You are in a REASON step. Analyse the gathered data and produce conclusions.",
            StepType.PROPOSE:  "You are in a PROPOSE step. Generate a clear action plan or change proposal.",
            StepType.APPLY:    "You are in an APPLY step. Execute the approved change. Follow policy.",
            StepType.VALIDATE: "You are in a VALIDATE step. Verify the outcome. Do not make changes.",
        }
        guidance = type_guidance.get(step_def.type, "")
        if guidance:
            parts.append(guidance)

        # Inject outputs from context_from steps.
        for dep_id in step_def.context_from:
            dep = ctx.get(dep_id, {})
            out = (dep.get("output") or "").strip()
            if out:
                parts.append(f"Output from step '{dep_id}':\n{out}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    #  Agent-backed step types                                             #
    # ------------------------------------------------------------------ #

    async def _execute_agent_step(
        self,
        step_def: StepDefinition,
        run_id: str,
        context_prompt: str,
        action_policy: Any,
        auth_user_id: Optional[str],
    ) -> dict:
        """Run an agent for inspect / reason / propose / apply / validate steps."""
        from gateway.session import SessionSource
        from gateway.config import Platform

        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id=f"wf_{run_id}_{step_def.id}",
            chat_type="dm",
            user_id=auth_user_id or "workflow-engine",
            user_name="Workflow Engine",
        )
        session_id = f"wf_{run_id}_{step_def.id}"

        try:
            result = await asyncio.wait_for(
                self._runner._run_agent(
                    message=step_def.prompt,
                    context_prompt=context_prompt,
                    history=[],
                    source=source,
                    session_id=session_id,
                    action_policy=action_policy,
                    auth_user_id=auth_user_id,
                ),
                timeout=step_def.timeout,
            )
            return {
                "status": StepStatus.SUCCESS,
                "output": result.get("final_response", ""),
                "error": "",
            }
        except asyncio.TimeoutError:
            return {
                "status": StepStatus.FAILED,
                "output": "",
                "error": f"Step timed out after {step_def.timeout}s",
            }

    # ------------------------------------------------------------------ #
    #  Approval step                                                       #
    # ------------------------------------------------------------------ #

    async def _execute_approval(self, step_def: StepDefinition, run_id: str) -> dict:
        """Pause execution and wait for a human decision."""
        note = step_def.prompt or f"Approval required for workflow step: {step_def.name}"

        # Create an approval_request record so the UI can surface it.
        approval_id = _db.create_workflow_approval(
            run_id=run_id,
            step_id=step_def.id,
            note=note,
        )
        _db.update_step_run(
            run_id, step_def.id,
            status=StepStatus.WAITING_APPROVAL,
            approval_id=approval_id,
        )
        _db.update_workflow_run(run_id, status=RunStatus.PAUSED)
        logger.info("Workflow run %s paused — waiting for approval %s", run_id, approval_id)

        # Block until resume_approval() sets this event.
        event = asyncio.Event()
        self._approval_events[approval_id] = event
        try:
            await event.wait()
        finally:
            self._approval_events.pop(approval_id, None)

        # Re-read the decision from DB.
        req = _db.get_approval_request(approval_id)
        if req and req.get("status") == "approved":
            _db.update_workflow_run(run_id, status=RunStatus.RUNNING)
            return {
                "status": StepStatus.SUCCESS,
                "output": f"Approved by {req.get('decided_by') or 'unknown'}",
                "error": "",
            }
        else:
            decision = (req or {}).get("status", "unknown")
            return {
                "status": StepStatus.FAILED,
                "output": "",
                "error": f"Approval {decision} — workflow halted",
            }
