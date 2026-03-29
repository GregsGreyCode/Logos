"""Logos models — provider and credential resolution.

Re-exports provider resolution from logos_cli.runtime_provider so agents
import from the logos namespace without depending on the CLI layer directly.

Future phases may move the provider registry and credential logic here,
decoupling it from logos_cli entirely.

Usage::

    from logos.models.provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested="anthropic")
    # {"provider": "anthropic", "api_mode": "anthropic_messages", ...}
"""

from logos_cli.runtime_provider import (
    resolve_runtime_provider,
    resolve_requested_provider,
    format_runtime_provider_error,
)

__all__ = [
    "resolve_runtime_provider",
    "resolve_requested_provider",
    "format_runtime_provider_error",
]
