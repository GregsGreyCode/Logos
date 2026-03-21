"""Logos audit — run record helpers.

Platform-owned interface for agent run lifecycle recording.
Implementation delegates to gateway.runs; future phases may relocate
the auth_db writes here.

Usage::

    from logos.audit.runs import start_run, finish_run, set_workspace
"""

from gateway.runs import start_run, finish_run, set_workspace

__all__ = ["start_run", "finish_run", "set_workspace"]
