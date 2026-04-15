"""Logos policy enforcement — re-exports for approval and command-security gates.

Policy gate flow (after the README-audit cleanup):
  1. check_all_command_guards() — DANGEROUS_PATTERNS regex + Tirith scan
     (the only live pre-exec policy gate)
  2. create_policy_approval_request() — persists approval rows to auth DB
     (used by the MCP access flow)

The former ``check_policy_for_tool`` / ``check_tool`` dispatch-time gate has
been removed; it was never wired into ``tools/registry.py:dispatch()``.
Workspace scoping is enforced inline in ``agents/hermes/agent.py``.

Usage::

    from logos.policy.enforcement import (
        create_policy_approval_request,
        check_all_command_guards,
    )
"""

# -- Approval request persistence --------------------------------------------
from tools.approval import create_policy_approval_request

# -- Pre-exec command guards (dangerous patterns + tirith) -------------------
from tools.approval import (
    check_dangerous_command,
    check_all_command_guards,
    detect_dangerous_command,
    DANGEROUS_PATTERNS,
)

# -- Tirith binary security scanner ------------------------------------------
from tools.tirith_security import check_command_security, ensure_installed


__all__ = [
    "create_policy_approval_request",
    "check_dangerous_command",
    "check_all_command_guards",
    "detect_dangerous_command",
    "DANGEROUS_PATTERNS",
    "check_command_security",
    "ensure_installed",
]
