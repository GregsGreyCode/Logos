"""Logos policy enforcement — single import surface for all approval and security gates.

Centralises the functions previously scattered across tools/approval.py and
tools/tirith_security.py so agent implementations only need one import path.

Policy gate flow:
  1. check_policy_for_tool()  — ActionPolicy dimension check (write/exec/fs)
  2. check_all_command_guards() — dangerous-pattern + tirith scan (exec tools)
  3. create_policy_approval_request() — persists approval requests to auth DB

Usage::

    from logos.policy.enforcement import (
        check_policy_for_tool,
        create_policy_approval_request,
        check_all_command_guards,
    )
"""

# -- Policy-based tool gate (ActionPolicy dimensions) -------------------------
from tools.approval import (
    check_policy_for_tool,
    create_policy_approval_request,
)

# -- Pre-exec command guards (dangerous patterns + tirith) --------------------
from tools.approval import (
    check_dangerous_command,
    check_all_command_guards,
    detect_dangerous_command,
    DANGEROUS_PATTERNS,
)

# -- Tirith binary security scanner -------------------------------------------
from tools.tirith_security import check_command_security, ensure_installed


__all__ = [
    # Policy gate
    "check_policy_for_tool",
    "create_policy_approval_request",
    # Command guards
    "check_dangerous_command",
    "check_all_command_guards",
    "detect_dangerous_command",
    "DANGEROUS_PATTERNS",
    # Tirith
    "check_command_security",
    "ensure_installed",
]
