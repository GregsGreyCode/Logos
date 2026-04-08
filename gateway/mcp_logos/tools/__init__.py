"""
Logos MCP tool implementations.

Each tool module exports a ``register(server)`` function that the logos
package's ``register_logos_server()`` entry point calls after creating
the server. Splitting tools into modules keeps each phase of the
capabilities migration small and testable in isolation.

Phases:
    platform  — L.2 (platform_send, home_message)
    session   — L.3 (session_read, session_list)
    memory    — L.3 (memory_recall, memory_write)
    cron      — L.4 (cron_schedule, cron_list, cron_cancel)
    workflow  — L.4 (workflow_start, workflow_status)
    agents    — L.5 (agent_list, agent_message)
"""
