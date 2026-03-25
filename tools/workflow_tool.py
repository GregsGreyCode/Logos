#!/usr/bin/env python3
"""
Workflow Tool — create, trigger, and monitor workflow runs via the gateway.

Workflows are DAG-structured task graphs where each step is an agent call.
Steps can depend on each other, share a parallel_group to run concurrently,
have conditional skip expressions, and include human approval gates.

The tool talks to the local gateway over HTTP using HERMES_INTERNAL_TOKEN
so it works without a user session cookie.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

_GATEWAY_BASE = os.environ.get("HERMES_GATEWAY_URL", "http://localhost:8080")
_INTERNAL_TOKEN = os.environ.get("HERMES_INTERNAL_TOKEN", "")

VALID_ACTIONS = {
    "list", "get", "create", "update", "delete",
    "trigger", "run_status", "list_runs",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if _INTERNAL_TOKEN:
        h["Authorization"] = f"Bearer {_INTERNAL_TOKEN}"
    return h


def _request(method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    url = f"{_GATEWAY_BASE}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items() if v is not None)
        if qs:
            url = f"{url}?{qs}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
        except Exception:
            detail = {"error": exc.reason}
        return {"_http_error": exc.code, **detail}
    except Exception as exc:
        return {"error": f"Gateway unreachable: {exc}"}


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _list_workflows() -> str:
    result = _request("GET", "/workflows")
    if "error" in result:
        return json.dumps(result)
    workflows = result.get("workflows", [])
    # Return a compact summary — full step definitions are large
    summary = [
        {
            "id": w["id"],
            "name": w["name"],
            "description": w.get("description", ""),
            "step_count": w.get("step_count", len(w.get("steps", []))),
            "tags": w.get("tags", []),
            "version": w.get("version", "1.0"),
        }
        for w in workflows
    ]
    return json.dumps({"ok": True, "workflows": summary, "total": len(summary)})


def _get_workflow(workflow_id: str) -> str:
    result = _request("GET", f"/workflows/{workflow_id}")
    if "error" in result:
        return json.dumps(result)
    return json.dumps({"ok": True, **result})


def _create_workflow(name: str, steps: list, description: str = "", tags: list = None) -> str:
    if not name:
        return json.dumps({"error": "name is required"})
    if not steps or not isinstance(steps, list):
        return json.dumps({"error": "steps must be a non-empty array"})
    body = {
        "name": name,
        "description": description,
        "steps": steps,
        "tags": tags or [],
    }
    result = _request("POST", "/workflows", body=body)
    if result.get("_http_error"):
        return json.dumps({"error": result.get("error", "create failed"), "detail": result})
    return json.dumps({"ok": True, **result})


def _update_workflow(workflow_id: str, updates: dict) -> str:
    if not workflow_id:
        return json.dumps({"error": "workflow_id is required"})
    result = _request("PATCH", f"/workflows/{workflow_id}", body=updates)
    if result.get("_http_error"):
        return json.dumps({"error": result.get("error", "update failed"), "detail": result})
    return json.dumps({"ok": True, **result})


def _delete_workflow(workflow_id: str) -> str:
    if not workflow_id:
        return json.dumps({"error": "workflow_id is required"})
    result = _request("DELETE", f"/workflows/{workflow_id}")
    if result.get("_http_error"):
        return json.dumps({"error": result.get("error", "delete failed")})
    return json.dumps({"ok": True, "deleted": workflow_id})


def _trigger_workflow(workflow_id: str, inputs: Optional[dict] = None) -> str:
    if not workflow_id:
        return json.dumps({"error": "workflow_id is required"})
    body = {"inputs": inputs or {}}
    result = _request("POST", f"/workflows/{workflow_id}/trigger", body=body)
    if result.get("_http_error"):
        return json.dumps({"error": result.get("error", "trigger failed"), "detail": result})
    return json.dumps({
        "ok": True,
        "run_id": result.get("run_id"),
        "workflow_id": result.get("workflow_id"),
        "message": "Workflow started. Use action=run_status with run_id to poll progress.",
    })


def _run_status(run_id: str) -> str:
    if not run_id:
        return json.dumps({"error": "run_id is required"})
    result = _request("GET", f"/workflow-runs/{run_id}")
    if result.get("_http_error"):
        return json.dumps({"error": result.get("error", "run not found")})
    run = result.get("run", result)
    steps = result.get("steps", [])
    # Compact step summary
    step_summary = [
        {
            "id": s.get("step_id"),
            "status": s.get("status"),
            "output_summary": (s.get("output_summary") or "")[:300],
            "error": s.get("error"),
        }
        for s in steps
    ]
    return json.dumps({
        "ok": True,
        "run_id": run.get("id") or run_id,
        "status": run.get("status"),
        "workflow_id": run.get("workflow_id"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "error": run.get("error"),
        "steps": step_summary,
    })


def _list_runs(workflow_id: Optional[str] = None, status: Optional[str] = None, limit: int = 20) -> str:
    params = {"limit": limit}
    if workflow_id:
        params["workflow_id"] = workflow_id
    if status:
        params["status"] = status
    result = _request("GET", "/workflow-runs", params=params)
    if "error" in result:
        return json.dumps(result)
    runs = result.get("runs", [])
    return json.dumps({
        "ok": True,
        "runs": runs,
        "total": result.get("total", len(runs)),
    })


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def workflow_tool(
    action: str,
    workflow_id: Optional[str] = None,
    run_id: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    steps: Optional[list] = None,
    tags: Optional[list] = None,
    inputs: Optional[dict] = None,
    updates: Optional[dict] = None,
    status_filter: Optional[str] = None,
    limit: int = 20,
) -> str:
    action = (action or "").strip().lower()
    if action not in VALID_ACTIONS:
        return json.dumps({"error": f"Unknown action '{action}'. Use: {', '.join(sorted(VALID_ACTIONS))}"})

    if action == "list":
        return _list_workflows()
    if action == "get":
        return _get_workflow(workflow_id or "")
    if action == "create":
        return _create_workflow(name or "", steps or [], description or "", tags)
    if action == "update":
        return _update_workflow(workflow_id or "", updates or {})
    if action == "delete":
        return _delete_workflow(workflow_id or "")
    if action == "trigger":
        return _trigger_workflow(workflow_id or "", inputs)
    if action == "run_status":
        return _run_status(run_id or "")
    if action == "list_runs":
        return _list_runs(workflow_id, status_filter, limit)

    return json.dumps({"error": "Unexpected state."})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_STEP_EXAMPLE = {
    "id": "gather",
    "type": "inspect",
    "name": "Gather information",
    "prompt": "Search the web for X and summarise findings.",
    "tools": ["web_search", "web_extract"],
    "model_alias": "balanced",
    "timeout": 120,
    "depends_on": [],
    "parallel_group": None,
    "condition": None,
    "context_from": [],
    "continue_on_failure": False,
}

WORKFLOW_TOOL_SCHEMA = {
    "name": "workflow",
    "description": (
        "Create, trigger, and monitor multi-step agent workflows.\n\n"
        "Workflows are DAG-structured task graphs where each step runs as an agent call. "
        "Use them when a task can be broken into discrete, potentially parallel phases — "
        "e.g. gather data in parallel, then reason, then propose a change, then wait for approval.\n\n"
        "**Step types:** inspect (read-only), reason (analyse), propose (plan), apply (execute), "
        "validate (verify), approval (human gate).\n\n"
        "**Parallel execution:** steps sharing the same `parallel_group` string run concurrently "
        "via asyncio.gather — ideal for independent data-gathering steps.\n\n"
        "**Actions:**\n"
        "- `list` — see all defined workflows\n"
        "- `get` — full definition including all steps (requires workflow_id)\n"
        "- `create` — define a new workflow (requires name + steps array)\n"
        "- `update` — modify an existing workflow (requires workflow_id + updates dict)\n"
        "- `delete` — remove a workflow definition (requires workflow_id)\n"
        "- `trigger` — start a run (requires workflow_id; optional inputs dict)\n"
        "- `run_status` — poll a run's live state and per-step output (requires run_id)\n"
        "- `list_runs` — recent runs, filterable by workflow_id and status\n\n"
        "**Workflow for creating a new workflow:**\n"
        "1. Design steps with depends_on wiring and parallel_groups\n"
        "2. Call action=create\n"
        "3. Call action=trigger with the returned workflow_id\n"
        "4. Poll action=run_status until status is success/failed\n\n"
        f"**Step object shape:** {json.dumps(_STEP_EXAMPLE)}"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(VALID_ACTIONS),
                "description": "Operation to perform.",
            },
            "workflow_id": {
                "type": "string",
                "description": "Workflow ID. Required for get, update, delete, trigger, list_runs.",
            },
            "run_id": {
                "type": "string",
                "description": "Run ID returned by trigger. Required for run_status.",
            },
            "name": {
                "type": "string",
                "description": "Human-readable workflow name. Required for create.",
            },
            "description": {
                "type": "string",
                "description": "Optional description of what this workflow does.",
            },
            "steps": {
                "type": "array",
                "description": (
                    "Array of step definition objects. Required for create. "
                    "Each step needs: id, type, name, prompt. "
                    "Optional: tools, model_alias, timeout, depends_on, parallel_group, "
                    "condition, context_from, continue_on_failure."
                ),
                "items": {"type": "object"},
            },
            "tags": {
                "type": "array",
                "description": "Optional string tags for categorising workflows.",
                "items": {"type": "string"},
            },
            "inputs": {
                "type": "object",
                "description": (
                    "Key-value inputs passed to all steps as context. "
                    "Available in step prompts via the context_prompt injection."
                ),
            },
            "updates": {
                "type": "object",
                "description": (
                    "Fields to update on an existing workflow. "
                    "Accepted keys: name, description, version, tags, steps."
                ),
            },
            "status_filter": {
                "type": "string",
                "enum": ["pending", "running", "paused", "success", "failed", "cancelled"],
                "description": "Filter list_runs by run status.",
            },
            "limit": {
                "type": "integer",
                "description": "Max runs to return for list_runs. Default 20.",
                "default": 20,
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry

registry.register(
    name="workflow",
    toolset="workflow",
    schema=WORKFLOW_TOOL_SCHEMA,
    handler=lambda args, **kw: workflow_tool(
        action=args.get("action", ""),
        workflow_id=args.get("workflow_id"),
        run_id=args.get("run_id"),
        name=args.get("name"),
        description=args.get("description"),
        steps=args.get("steps"),
        tags=args.get("tags"),
        inputs=args.get("inputs"),
        updates=args.get("updates"),
        status_filter=args.get("status_filter"),
        limit=int(args.get("limit", 20)),
    ),
    check_fn=lambda: True,
    description="Create, trigger, and monitor multi-step agent workflows with DAG execution.",
)
