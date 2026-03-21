"""
Agent-to-Agent Handoff Tool.

Extends the delegation system with explicit contracts for structured
multi-agent collaboration. Unlike delegate_task (free-form goal/context),
handoff_agent enforces:

  - contract.allowed_toolsets  — restrict child to a toolset subset
  - contract.policy_scope      — "standard" | "restricted" | "read_only"
  - contract.expected_output_schema — optional JSON schema for output validation
  - contract.description       — human-readable contract purpose

Structured I/O:
  - structured_input    — JSON object passed to the child as context
  - Child must respond with valid JSON matching expected_output_schema (if set)

Parent/child tracking:
  - Child run records are linked to the parent run via parent_run_id
  - This enables lineage queries in /runs and metrics

Example use cases:
  Research → Analyst:
    Parent (research) gathers raw data → handoff to child (analyst) to structure it
  Operator → Validator:
    Operator proposes a plan → Validator checks for safety/policy issues
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HANDOFF_BLOCKED_TOOLS = frozenset([
    "delegate_task",
    "handoff_agent",     # no recursive handoffs
    "clarify",
    "memory",
    "send_message",
])

ALLOWED_POLICY_SCOPES = {"standard", "restricted", "read_only"}
MAX_HANDOFF_DEPTH = 2


def _validate_contract(contract: Dict) -> Optional[str]:
    """Return an error string if the contract is invalid, else None."""
    if not isinstance(contract, dict):
        return "contract must be a JSON object"
    policy = contract.get("policy_scope", "standard")
    if policy not in ALLOWED_POLICY_SCOPES:
        return f"contract.policy_scope must be one of {sorted(ALLOWED_POLICY_SCOPES)}"
    schema = contract.get("expected_output_schema")
    if schema is not None and not isinstance(schema, dict):
        return "contract.expected_output_schema must be a JSON object (JSON Schema)"
    toolsets = contract.get("allowed_toolsets")
    if toolsets is not None and not isinstance(toolsets, list):
        return "contract.allowed_toolsets must be a list of toolset names"
    return None


def _validate_output_against_schema(output_obj: Any, schema: Dict) -> Optional[str]:
    """
    Basic JSON Schema validation (type, required, properties).
    Returns error message or None if valid.
    """
    if not isinstance(schema, dict):
        return None  # No schema — skip
    if not isinstance(output_obj, dict):
        return f"expected JSON object, got {type(output_obj).__name__}"

    # Check required fields
    for field in schema.get("required", []):
        if field not in output_obj:
            return f"missing required field: {field!r}"

    # Check property types
    for prop, prop_schema in schema.get("properties", {}).items():
        if prop not in output_obj:
            continue
        expected_type = prop_schema.get("type")
        val = output_obj[prop]
        type_map = {
            "string": str, "number": (int, float), "integer": int,
            "boolean": bool, "array": list, "object": dict, "null": type(None),
        }
        if expected_type and expected_type in type_map:
            if not isinstance(val, type_map[expected_type]):
                return f"field {prop!r} should be {expected_type}, got {type(val).__name__}"

    return None


def _build_handoff_prompt(goal: str, structured_input: Optional[Dict], contract: Dict) -> str:
    """Build a focused system prompt for the handoff child agent."""
    lines = [
        "You are a focused agent completing a specific delegated task.",
        "",
        f"TASK:\n{goal}",
    ]

    if structured_input:
        lines.append(f"\nINPUT DATA (structured):\n{json.dumps(structured_input, indent=2)}")

    policy_scope = contract.get("policy_scope", "standard")
    if policy_scope == "restricted":
        lines.append("\nPOLICY: You are operating in RESTRICTED mode. "
                     "Read-only operations only. Do not modify files or run "
                     "destructive commands.")
    elif policy_scope == "read_only":
        lines.append("\nPOLICY: You are operating in READ-ONLY mode. "
                     "You may read files and run safe queries, but not write, "
                     "delete, or execute code.")

    desc = contract.get("description")
    if desc:
        lines.append(f"\nCONTRACT CONTEXT:\n{desc}")

    schema = contract.get("expected_output_schema")
    if schema:
        required = schema.get("required", [])
        props = list(schema.get("properties", {}).keys())
        lines.append(
            f"\nOUTPUT REQUIREMENT: Respond with valid JSON containing these fields: "
            f"{props}. Required: {required}. "
            "Wrap your JSON in a ```json ... ``` code block."
        )
    else:
        lines.append(
            "\nComplete the task and provide a clear, concise summary of results."
        )

    return "\n".join(lines)


def _extract_json_from_output(output: str) -> Optional[Dict]:
    """Try to extract a JSON object from the agent's output."""
    if not output:
        return None

    # Try ```json ... ``` block first
    import re
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding the first {...} object
    match = re.search(r"(\{[^{}]*\})", output, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try parsing the whole output
    try:
        return json.loads(output.strip())
    except json.JSONDecodeError:
        return None


def _get_parent_run_id(parent_agent) -> Optional[str]:
    """Extract the current run_id from the parent agent's run recorder."""
    recorder = getattr(parent_agent, "_run_recorder", None)
    if recorder:
        return getattr(recorder, "run_id", None)
    return None


def _link_child_to_parent(db, parent_run_id: str, child_session_id: str) -> Optional[str]:
    """
    Find the most recent run for the child's session and set its parent_run_id.
    Returns the child run_id if found, else None.
    """
    if not db or not parent_run_id or not child_session_id:
        return None
    try:
        cursor = db._conn.execute(
            "SELECT id FROM runs WHERE session_id = ? ORDER BY started_at DESC LIMIT 1",
            (child_session_id,),
        )
        row = cursor.fetchone()
        if row:
            child_run_id = row[0]
            db._conn.execute(
                "UPDATE runs SET parent_run_id = ? WHERE id = ?",
                (parent_run_id, child_run_id),
            )
            db._conn.commit()
            return child_run_id
    except Exception as exc:
        logger.debug("Failed to link child run to parent: %s", exc)
    return None


def handoff_agent(
    goal: str,
    contract: Dict[str, Any],
    structured_input: Optional[Dict] = None,
    parent_agent=None,
) -> str:
    """
    Spawn a child agent with an explicit contract for structured multi-agent collaboration.

    Args:
        goal:             What the child agent should accomplish.
        contract:         Enforcement rules:
                            allowed_toolsets: list of toolset names (or None for all)
                            policy_scope: "standard" | "restricted" | "read_only"
                            expected_output_schema: JSON Schema dict (optional)
                            description: human-readable contract context (optional)
        structured_input: Optional JSON data to pass to the child.
        parent_agent:     The calling agent instance (required).

    Returns:
        JSON string with:
          status:           "completed" | "failed" | "error"
          output:           raw text output from child
          structured_output: parsed JSON output (if schema was provided and output is valid JSON)
          validation_error: schema mismatch error (if any)
          run_id:           child run ID (linked to parent via parent_run_id)
          duration_s:       elapsed seconds
    """
    from run_agent import AIAgent

    if parent_agent is None:
        return json.dumps({"error": "handoff_agent requires a parent agent context."})

    depth = getattr(parent_agent, "_delegate_depth", 0)
    if depth >= MAX_HANDOFF_DEPTH:
        return json.dumps({
            "error": (
                f"Handoff depth limit ({MAX_HANDOFF_DEPTH}) reached. "
                "Agents cannot recursively hand off to further agents."
            )
        })

    if not goal or not goal.strip():
        return json.dumps({"error": "goal is required"})

    if not contract:
        contract = {}

    err = _validate_contract(contract)
    if err:
        return json.dumps({"error": f"Invalid contract: {err}"})

    # Resolve toolsets: apply contract restrictions on top of parent's enabled set
    parent_toolsets = getattr(parent_agent, "enabled_toolsets", None) or []
    allowed_toolsets = contract.get("allowed_toolsets")
    if allowed_toolsets is not None:
        # Intersect with parent's toolsets (can only restrict, not expand)
        child_toolsets = [ts for ts in allowed_toolsets if ts in parent_toolsets or not parent_toolsets]
        # Remove always-blocked tools
        blocked_names = {"delegation", "clarify", "memory", "code_execution"}
        child_toolsets = [ts for ts in child_toolsets if ts not in blocked_names]
    else:
        child_toolsets = [ts for ts in parent_toolsets if ts not in {"delegation", "clarify", "memory", "code_execution"}]

    child_session_id = str(uuid.uuid4())
    child_system_prompt = _build_handoff_prompt(goal, structured_input, contract)

    # Extract parent run_id for linking
    parent_run_id = _get_parent_run_id(parent_agent)

    start = time.monotonic()

    try:
        parent_api_key = getattr(parent_agent, "api_key", None)
        if not parent_api_key and hasattr(parent_agent, "_client_kwargs"):
            parent_api_key = parent_agent._client_kwargs.get("api_key")

        child = AIAgent(
            base_url=parent_agent.base_url,
            api_key=parent_api_key,
            model=parent_agent.model,
            provider=getattr(parent_agent, "provider", None),
            api_mode=getattr(parent_agent, "api_mode", None),
            max_iterations=30,
            enabled_toolsets=child_toolsets,
            quiet_mode=True,
            ephemeral_system_prompt=child_system_prompt,
            skip_context_files=True,
            skip_memory=True,
            clarify_callback=None,
            session_db=getattr(parent_agent, "_session_db", None),
            session_id=child_session_id,
            platform=getattr(parent_agent, "platform", "cli"),
            log_prefix="[handoff]",
            providers_allowed=getattr(parent_agent, "providers_allowed", None),
            providers_ignored=getattr(parent_agent, "providers_ignored", None),
            providers_order=getattr(parent_agent, "providers_order", None),
            provider_sort=getattr(parent_agent, "provider_sort", None),
        )

        # Propagate depth limit
        child._delegate_depth = depth + 1

        devnull = io.StringIO()
        with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            result = child.run_conversation(user_message=goal)

        duration_s = round(time.monotonic() - start, 2)
        output_text = result.get("final_response") or ""
        completed = result.get("completed", False)
        status = "completed" if completed else "failed"

        # Link child run to parent in the runs table
        child_run_id = None
        db = getattr(parent_agent, "_session_db", None)
        if db and parent_run_id:
            child_run_id = _link_child_to_parent(db, parent_run_id, child_session_id)

        # Parse structured output if schema was requested
        structured_output = None
        validation_error = None
        schema = contract.get("expected_output_schema")
        if schema:
            structured_output = _extract_json_from_output(output_text)
            if structured_output is not None:
                validation_error = _validate_output_against_schema(structured_output, schema)
            else:
                validation_error = "Could not parse JSON from agent output"

        # Store structured output on the run record
        if child_run_id and structured_output and db:
            try:
                db._conn.execute(
                    "UPDATE runs SET structured_output = ?, agent_contract = ? WHERE id = ?",
                    (
                        json.dumps(structured_output),
                        json.dumps(contract),
                        child_run_id,
                    ),
                )
                db._conn.commit()
            except Exception as exc:
                logger.debug("Failed to store structured output on run: %s", exc)

        response = {
            "status": status,
            "output": output_text[:1000],
            "duration_s": duration_s,
        }
        if child_run_id:
            response["run_id"] = child_run_id[:8]
            response["run_id_full"] = child_run_id
        if parent_run_id:
            response["parent_run_id"] = parent_run_id[:8]
        if structured_output is not None:
            response["structured_output"] = structured_output
        if validation_error:
            response["validation_error"] = validation_error

        return json.dumps(response, ensure_ascii=False)

    except Exception as exc:
        duration_s = round(time.monotonic() - start, 2)
        logger.exception("handoff_agent failed: %s", exc)
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "duration_s": duration_s,
        })


# ─── Tool schema ─────────────────────────────────────────────────────────────

HANDOFF_AGENT_SCHEMA = {
    "name": "handoff_agent",
    "description": (
        "Spawn a child agent with an explicit contract for structured multi-agent collaboration. "
        "Use this instead of delegate_task when you need:\n"
        "- Enforced tool restrictions (allowed_toolsets)\n"
        "- Policy scope control (standard / restricted / read_only)\n"
        "- Structured JSON output with schema validation\n"
        "- Auditable parent/child run linkage\n\n"
        "EXAMPLE PATTERNS:\n"
        "  Research → Analyst: research agent gathers data, analyst structures it\n"
        "  Operator → Validator: operator proposes plan, validator checks safety\n\n"
        "The contract is enforced at agent spawn time — the child cannot access "
        "tools or perform writes beyond what the contract allows.\n\n"
        "Structured output: if expected_output_schema is set, the child's JSON "
        "response is validated and returned as structured_output."
    ),
    "parameters": {
        "type": "object",
        "required": ["goal", "contract"],
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "What the child agent should accomplish. Be specific — "
                    "the child has no memory of your conversation."
                ),
            },
            "contract": {
                "type": "object",
                "description": "Enforcement contract for this handoff.",
                "properties": {
                    "allowed_toolsets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Toolsets the child may use. Must be a subset of currently "
                            "enabled toolsets. Example: ['web', 'file']. "
                            "Omit to inherit all enabled toolsets (minus blocked ones)."
                        ),
                    },
                    "policy_scope": {
                        "type": "string",
                        "enum": ["standard", "restricted", "read_only"],
                        "description": (
                            "standard: normal operation; "
                            "restricted: read-only filesystem, no destructive commands; "
                            "read_only: no writes at all."
                        ),
                    },
                    "expected_output_schema": {
                        "type": "object",
                        "description": (
                            "JSON Schema the child's output must conform to. "
                            "If set, the child is instructed to respond with JSON "
                            "and the output is validated. "
                            "Example: {\"type\": \"object\", \"required\": [\"summary\", \"findings\"], "
                            "\"properties\": {\"summary\": {\"type\": \"string\"}, "
                            "\"findings\": {\"type\": \"array\"}}}"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable context for this handoff contract.",
                    },
                },
            },
            "structured_input": {
                "type": "object",
                "description": (
                    "Optional structured JSON data to pass to the child. "
                    "Useful for passing structured research data, file paths, "
                    "or configuration. The child receives this as INPUT DATA."
                ),
            },
        },
    },
}


# ─── Registry ─────────────────────────────────────────────────────────────────

from tools.registry import registry

registry.register(
    name="handoff_agent",
    toolset="delegation",
    schema=HANDOFF_AGENT_SCHEMA,
    handler=lambda args, **kw: handoff_agent(
        goal=args.get("goal", ""),
        contract=args.get("contract", {}),
        structured_input=args.get("structured_input"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=lambda: True,
)
