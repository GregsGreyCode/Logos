"""
Run record helpers — wraps auth_db agent_runs CRUD with higher-level helpers.

Used by:
  - gateway/run.py  (create/finish records in _run_agent)
  - gateway/http_api.py  (REST handlers)
"""

import json
import logging
import os
import time
from typing import Any, Optional

import gateway.auth.db as auth_db

logger = logging.getLogger(__name__)

_INSTANCE_NAME = os.environ.get("HERMES_INSTANCE_NAME", "Hermes")


def start_run(
    *,
    session_id: str,
    user_id: Optional[str],
    user_message: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    action_policy=None,   # ActionPolicy instance or None
    workflow_run_id: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> str:
    """Create an agent_run record and return its run_id."""
    ap_id = None
    ap_snapshot = None
    if action_policy is not None:
        ap_id = getattr(action_policy, "id", None)
        try:
            ap_snapshot = json.dumps({
                "network_policy": str(getattr(action_policy, "network_policy", "")),
                "filesystem_policy": str(getattr(action_policy, "filesystem_policy", "")),
                "exec_policy": str(getattr(action_policy, "exec_policy", "")),
                "write_policy": str(getattr(action_policy, "write_policy", "")),
                "provider_policy": str(getattr(action_policy, "provider_policy", "")),
            })
        except Exception:
            pass
    try:
        run_id = auth_db.create_agent_run(
            session_id=session_id,
            user_id=user_id,
            instance_name=_INSTANCE_NAME,
            model=model,
            provider=provider,
            action_policy_id=ap_id,
            action_policy_snapshot=ap_snapshot,
            workflow_run_id=workflow_run_id,
            user_message=user_message[:2000] if user_message else None,
            workspace_path=workspace_path,
        )
        return run_id
    except Exception as exc:
        logger.warning("Failed to create run record: %s", exc)
        return ""


def set_workspace(run_id: str, workspace_path: str) -> None:
    """Update the workspace path on an existing run record."""
    if not run_id:
        return
    try:
        auth_db.set_agent_run_workspace(run_id, workspace_path)
    except Exception as exc:
        logger.warning("Failed to set workspace for run %s: %s", run_id, exc)


def finish_run(
    run_id: str,
    *,
    status: str,
    final_response: Optional[str] = None,
    error: Optional[str] = None,
    api_calls: int = 0,
    model: Optional[str] = None,
    tool_calls_log: Optional[list] = None,  # list of {"tool": name, "preview": str}
    approval_ids: Optional[list] = None,
) -> None:
    """Update an agent_run record with final state."""
    if not run_id:
        return
    tool_sequence = [t["tool"] for t in (tool_calls_log or [])]
    try:
        auth_db.finish_agent_run(
            run_id=run_id,
            status=status,
            output_summary=final_response,
            error=error,
            api_calls=api_calls,
            model=model,
            tool_sequence=tool_sequence,
            tool_detail=tool_calls_log or [],
            approval_ids=approval_ids or [],
        )
    except Exception as exc:
        logger.warning("Failed to finish run record %s: %s", run_id, exc)
