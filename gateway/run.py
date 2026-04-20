"""
Gateway runner - entry point for messaging platform integrations.

This module provides:
- start_gateway(): Start all configured platform adapters
- GatewayRunner: Main class managing the gateway lifecycle

Usage:
    # Start the gateway
    python -m gateway.run
    
    # Or from CLI
    python cli.py --gateway
"""

import asyncio
import contextvars
import json
import logging
import os
import re
import shlex
import sys
import signal
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Any, List, Tuple

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Resolve home directory (respects LOGOS_HOME / HERMES_HOME override; defaults to ~/.logos).
# Migration: if ~/.logos does not yet exist but ~/.hermes does (legacy Linux/macOS
# installations), rename ~/.hermes → ~/.logos so existing data is preserved.
_default_logos_home = Path.home() / ".logos"
_legacy_hermes_home = Path.home() / ".hermes"
if (
    "LOGOS_HOME" not in os.environ
    and "HERMES_HOME" not in os.environ
    and not _default_logos_home.exists()
    and _legacy_hermes_home.exists()
):
    try:
        _legacy_hermes_home.rename(_default_logos_home)
    except OSError:
        pass  # cross-device rename or permissions — leave both in place
_hermes_home = Path(
    os.getenv("LOGOS_HOME")
    or os.getenv("HERMES_HOME")
    or str(_default_logos_home)
)

# Load environment variables from ~/.logos/.env first
from dotenv import load_dotenv
_env_path = _hermes_home / '.env'
if _env_path.exists():
    try:
        load_dotenv(_env_path, encoding="utf-8")
    except UnicodeDecodeError:
        load_dotenv(_env_path, encoding="latin-1")
# Also try project .env as fallback
load_dotenv()

# Openshell-style shell-env file: KEY=VALUE lines loaded with override=False
# so explicit shell exports still win. Covers the `python -m gateway.run`
# direct-launch path (logos_cli also overlays this into its subprocess env).
_shell_env_path = _hermes_home / 'env'
if _shell_env_path.exists():
    try:
        load_dotenv(_shell_env_path, encoding="utf-8", override=False)
    except UnicodeDecodeError:
        load_dotenv(_shell_env_path, encoding="latin-1", override=False)


def _warn_deprecated_hermes_env_vars() -> None:
    """One-time warning for HERMES_* env vars whose canonical name is LOGOS_*.

    Phase 1 of the env-var rename keeps HERMES_* readable for backward compat,
    but emits a single grouped warning at boot so deployments are nudged to
    migrate. Removed entirely in Phase 3 of the migration.
    """
    import logging as _logging
    _renamed = (
        "HERMES_HOME", "HERMES_PORT", "HERMES_INSTANCE_NAME",
        "HERMES_JWT_SECRET", "HERMES_INTERNAL_TOKEN", "HERMES_COOKIE_SECURE",
        "HERMES_ADMIN_EMAIL", "HERMES_ADMIN_PASSWORD", "HERMES_ADMIN_NAME",
        "HERMES_OAUTH_TRACE", "HERMES_CODEX_BASE_URL", "HERMES_PORTAL_BASE_URL",
        "HERMES_CA_BUNDLE", "HERMES_LOG_LEVEL", "HERMES_MCP_PORT",
        "HERMES_IS_CANARY", "HERMES_WIPE_ON_START",
        "HERMES_WORKSPACE_TTL_HOURS", "HERMES_WORKSPACE_DIR",
        "HERMES_WORKSPACE_CLEANUP_INTERVAL_HOURS", "HERMES_REPO_ROOTS",
        "HERMES_GATEWAY_MCP", "HERMES_GATEWAY_URL", "HERMES_QUIET",
        "HERMES_INTERACTIVE", "HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION",
        "HERMES_REDACT_SECRETS", "HERMES_DUMP_REQUESTS", "HERMES_DUMP_REQUEST_STDOUT",
        "HERMES_HUMAN_DELAY_MODE", "HERMES_HUMAN_DELAY_MIN_MS", "HERMES_HUMAN_DELAY_MAX_MS",
        "HERMES_SHARED_HOME", "HERMES_SOUL", "HERMES_TOOLSETS", "HERMES_POLICY_LEVEL",
        "HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM", "HERMES_YOLO_MODE",
        "HERMES_MODEL",
    )
    _present = [n for n in _renamed if n in os.environ and not os.environ.get(n.replace("HERMES_", "LOGOS_"))]
    if _present:
        _logging.getLogger("gateway").warning(
            "DEPRECATED env vars in use — please rename to LOGOS_* (HERMES_* will be "
            "removed in a future release): %s",
            ", ".join(sorted(_present)),
        )


_warn_deprecated_hermes_env_vars()

# Bridge config into the environment so os.getenv() picks them up.
# Two-file strategy:
#   config-base.yaml — infra-managed (k8s ConfigMap), overwritten each restart
#   config.yaml      — runtime-managed (setup wizard writes API keys, model, etc.)
# Base is loaded first, then runtime overlays on top. Runtime wins on conflict.
_config_path = _hermes_home / 'config.yaml'
_config_base_path = _hermes_home / 'config-base.yaml'

# Models confirmed loaded by this process — skip the check+load on subsequent messages.
# LM Studio creates a :2 instance if /load is called while the model is running, so
# we track our own loads and only call /load once per model per gateway lifetime.
_LMS_CONFIRMED_LOADED: set[str] = set()

def _deep_merge(base: dict, overlay: dict) -> dict:
    """Merge overlay into base. Overlay values win; dicts are merged recursively."""
    merged = base.copy()
    for key, val in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged

# Load and merge config files
_cfg: dict = {}
try:
    import yaml as _yaml
    if _config_base_path.exists():
        with open(_config_base_path, encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
    if _config_path.exists():
        with open(_config_path, encoding="utf-8") as _f:
            _runtime_cfg = _yaml.safe_load(_f) or {}
        _cfg = _deep_merge(_cfg, _runtime_cfg)
except Exception:
    pass

# Env var values that are placeholders — config.yaml should override these.
_PLACEHOLDER_ENV_VALUES = frozenset({"local", "not-needed", "dummy", "placeholder", "none", "changeme", ""})

if _cfg:
    try:
        # Top-level simple values — set env if not present or if the current
        # value is a known placeholder (e.g. OPENAI_API_KEY="local" from k8s secret).
        for _key, _val in _cfg.items():
            if isinstance(_val, (str, int, float, bool)):
                _str_val = str(_val).strip()
                _existing = os.environ.get(_key, "")
                if not _existing or _existing.strip().lower() in _PLACEHOLDER_ENV_VALUES:
                    if _str_val and _str_val.lower() not in _PLACEHOLDER_ENV_VALUES:
                        os.environ[_key] = _str_val
        # Terminal config is nested — bridge to TERMINAL_* env vars.
        # config.yaml overrides .env for these since it's the documented config path.
        _terminal_cfg = _cfg.get("terminal", {})
        if _terminal_cfg and isinstance(_terminal_cfg, dict):
            _terminal_env_map = {
                "backend": "TERMINAL_ENV",
                "cwd": "TERMINAL_CWD",
                "timeout": "TERMINAL_TIMEOUT",
                "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
                "docker_image": "TERMINAL_DOCKER_IMAGE",
                "singularity_image": "TERMINAL_SINGULARITY_IMAGE",
                "modal_image": "TERMINAL_MODAL_IMAGE",
                "daytona_image": "TERMINAL_DAYTONA_IMAGE",
                "ssh_host": "TERMINAL_SSH_HOST",
                "ssh_user": "TERMINAL_SSH_USER",
                "ssh_port": "TERMINAL_SSH_PORT",
                "ssh_key": "TERMINAL_SSH_KEY",
                "container_cpu": "TERMINAL_CONTAINER_CPU",
                "container_memory": "TERMINAL_CONTAINER_MEMORY",
                "container_disk": "TERMINAL_CONTAINER_DISK",
                "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
                "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
                "sandbox_dir": "TERMINAL_SANDBOX_DIR",
            }
            for _cfg_key, _env_var in _terminal_env_map.items():
                if _cfg_key in _terminal_cfg:
                    _val = _terminal_cfg[_cfg_key]
                    if isinstance(_val, list):
                        os.environ[_env_var] = json.dumps(_val)
                    else:
                        os.environ[_env_var] = str(_val)
        _compression_cfg = _cfg.get("compression", {})
        if _compression_cfg and isinstance(_compression_cfg, dict):
            _compression_env_map = {
                "enabled": "CONTEXT_COMPRESSION_ENABLED",
                "threshold": "CONTEXT_COMPRESSION_THRESHOLD",
                "summary_model": "CONTEXT_COMPRESSION_MODEL",
                "summary_provider": "CONTEXT_COMPRESSION_PROVIDER",
            }
            for _cfg_key, _env_var in _compression_env_map.items():
                if _cfg_key in _compression_cfg:
                    os.environ[_env_var] = str(_compression_cfg[_cfg_key])
        # Auxiliary model overrides (vision, web_extract).
        # Each task has provider + model; bridge non-default values to env vars.
        _auxiliary_cfg = _cfg.get("auxiliary", {})
        if _auxiliary_cfg and isinstance(_auxiliary_cfg, dict):
            _aux_task_env = {
                "vision":      ("AUXILIARY_VISION_PROVIDER",      "AUXILIARY_VISION_MODEL"),
                "web_extract": ("AUXILIARY_WEB_EXTRACT_PROVIDER",  "AUXILIARY_WEB_EXTRACT_MODEL"),
            }
            for _task_key, (_prov_env, _model_env) in _aux_task_env.items():
                _task_cfg = _auxiliary_cfg.get(_task_key, {})
                if not isinstance(_task_cfg, dict):
                    continue
                _prov = str(_task_cfg.get("provider", "")).strip()
                _model = str(_task_cfg.get("model", "")).strip()
                if _prov and _prov != "auto":
                    os.environ[_prov_env] = _prov
                if _model:
                    os.environ[_model_env] = _model
        _agent_cfg = _cfg.get("agent", {})
        if _agent_cfg and isinstance(_agent_cfg, dict):
            if "max_turns" in _agent_cfg:
                os.environ["HERMES_MAX_ITERATIONS"] = str(_agent_cfg["max_turns"])
        # Timezone: bridge config.yaml → HERMES_TIMEZONE env var.
        # HERMES_TIMEZONE from .env takes precedence (already in os.environ).
        _tz_cfg = _cfg.get("timezone", "")
        if _tz_cfg and isinstance(_tz_cfg, str) and "HERMES_TIMEZONE" not in os.environ:
            os.environ["HERMES_TIMEZONE"] = _tz_cfg.strip()
        # (Runtime mode bridge removed — OpenShell is the only supported
        # runtime and there is nothing to select.)
        # Security settings
        _security_cfg = _cfg.get("security", {})
        if isinstance(_security_cfg, dict):
            _redact = _security_cfg.get("redact_secrets")
            if _redact is not None:
                os.environ["LOGOS_REDACT_SECRETS"] = str(_redact).lower()
                os.environ["HERMES_REDACT_SECRETS"] = str(_redact).lower()
    except Exception:
        pass  # Non-fatal; gateway can still run with .env values

# Gateway runs in quiet mode - suppress debug output and use cwd directly (no temp dirs)
os.environ["LOGOS_QUIET"] = "1"
os.environ["HERMES_QUIET"] = "1"  # deprecated alias — kept for legacy in-flight code

# Enable interactive exec approval for dangerous commands on messaging platforms
os.environ["LOGOS_EXEC_ASK"] = "1"
os.environ["HERMES_EXEC_ASK"] = "1"  # deprecated alias — kept for legacy in-flight code

# Set terminal working directory for messaging platforms.
# If the user set an explicit path in config.yaml (not "." or "auto"),
# respect it. Otherwise use MESSAGING_CWD or default to home directory.
_configured_cwd = os.environ.get("TERMINAL_CWD", "")
if not _configured_cwd or _configured_cwd in (".", "auto", "cwd"):
    messaging_cwd = os.getenv("MESSAGING_CWD") or str(Path.home())
    os.environ["TERMINAL_CWD"] = messaging_cwd

from gateway.config import (
    Platform,
    GatewayConfig,
    load_gateway_config,
)
from gateway.session import (
    SessionStore,
    SessionSource,
    SessionContext,
    build_session_context,
    build_session_context_prompt,
    build_session_key,
)
from gateway.delivery import DeliveryRouter
from gateway.runs import start_run, finish_run
from gateway.channels.base import BasePlatformAdapter, MessageEvent, MessageType

# ── Correlation ID contextvars ──────────────────────────────────────────────
# These propagate through the async request path so every log record in a
# message-handling turn carries a common set of trace identifiers. The
# unified log can then be grepped by task_id / session_id / user_id /
# worker_id to reconstruct the full cross-component story for a single
# chat turn. See docs/MISSING.md M6 for the design rationale.
_session_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_id", default="-"
)
_task_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "task_id", default="-"
)
_user_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "user_id", default="-"
)
_worker_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "worker_id", default="-"
)
_chat_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "chat_id", default="-"
)


_CORRELATION_VARS = {
    "session_id": _session_ctx,
    "task_id": _task_ctx,
    "user_id": _user_ctx,
    "worker_id": _worker_ctx,
    "chat_id": _chat_ctx,
}


def set_log_context(**kwargs: Any) -> None:
    """Set one or more correlation-ID contextvars for the current async task.

    Callers at entry points (HTTP handlers, workflow runs, cron jobs) should
    invoke this once at the top of their handling code. All subsequent log
    records in the same async context will carry the values and they become
    grep-able in ``~/.logos/logs/unified.jsonl`` via::

        logos debug tail --filter task_id=<id>
        logos debug tail --filter session_id=<id>

    Unknown keys are silently ignored. Values are coerced to ``str`` so
    callers don't have to stringify UUIDs / ints themselves. Passing an
    empty string or None for a key leaves that contextvar unchanged —
    use the default ``-`` explicitly if you want to clear it.

    Example::

        set_log_context(
            session_id=session_id,
            task_id=task_id,
            user_id=user.get("sub"),
            worker_id=f"hermes-{agent_name}",
        )
    """
    for key, value in kwargs.items():
        var = _CORRELATION_VARS.get(key)
        if var is None:
            continue  # unknown key — ignore rather than fail loudly
        if value is None or value == "":
            continue
        try:
            var.set(str(value))
        except Exception:
            # Contextvars shouldn't raise, but defensively swallow anything
            # so a bad correlation ID never breaks the request itself.
            pass


class _SessionFilter(logging.Filter):
    """Inject all correlation IDs into every log record.

    The contextvar getters default to "-" so records always have the field
    set, even when no request context is active. Filters are applied per
    handler (not on the root logger) because child loggers propagate records
    directly to the root's handlers, bypassing root-level filters.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _session_ctx.get()
        record.task_id = _task_ctx.get()
        record.user_id = _user_ctx.get()
        record.worker_id = _worker_ctx.get()
        record.chat_id = _chat_ctx.get()
        return True


class JsonRedactingFormatter(logging.Formatter):
    """Structured JSON-lines formatter for the unified log sink.

    Emits one JSON object per log record on a single line. Includes
    correlation IDs (session_id, task_id, user_id, worker_id, chat_id)
    injected by _SessionFilter, plus the standard logging fields.

    The message body is run through ``redact_sensitive_text`` to strip
    API keys, tokens, and other secrets before serialisation — matching
    the behaviour of ``RedactingFormatter`` for the text log.

    Companion to the existing RotatingFileHandler at ~/.logos/logs/gateway.log
    (unstructured text, optimised for tail -f) — this one writes to
    ~/.logos/logs/unified.jsonl, optimised for ``logos debug tail`` /
    ``grep task_id=xyz`` / future log aggregation backends.
    """

    # Standard LogRecord attributes we do NOT want to serialise verbatim
    # (we explicitly pick the ones we want instead, for stable output shape)
    _SKIP = frozenset({
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "msg", "message", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "getMessage",
        "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        # Defensive defaults in case _SessionFilter wasn't applied to this
        # handler for some reason. Every field should always be present.
        for attr, default in (
            ("session_id", "-"),
            ("task_id", "-"),
            ("user_id", "-"),
            ("worker_id", "-"),
            ("chat_id", "-"),
        ):
            if not hasattr(record, attr):
                setattr(record, attr, default)

        try:
            from agent.redact import redact_sensitive_text
            message = redact_sensitive_text(record.getMessage())
        except Exception:
            message = record.getMessage()

        payload: Dict[str, Any] = {
            "ts": record.created,                      # float seconds epoch
            "level": record.levelname,                 # "INFO", "WARNING", ...
            "logger": record.name,                     # "gateway.worker_registry"
            "msg": message,
            "session_id": record.session_id,
            "task_id": record.task_id,
            "user_id": record.user_id,
            "worker_id": record.worker_id,
            "chat_id": record.chat_id,
            "source": "gateway",                       # distinguishes from worker/cluster
            "pid": record.process,
        }

        # Attach exception info if present (pre-formatted, redacted)
        if record.exc_info:
            try:
                from agent.redact import redact_sensitive_text as _r
                payload["exc"] = _r(self.formatException(record.exc_info))
            except Exception:
                payload["exc"] = self.formatException(record.exc_info)

        # Pull in any extra fields a caller attached via logger.info(..., extra={...})
        # without clobbering the structured fields above.
        for k, v in record.__dict__.items():
            if k in self._SKIP or k in payload or k.startswith("_"):
                continue
            # Only serialise JSON-safe types; everything else gets str()'d
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = str(v)

        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:
            # Last-resort fallback: return a minimal dict that cannot fail
            return json.dumps({
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": "<formatter error — see text log>",
                "source": "gateway",
            })


logger = logging.getLogger(__name__)


def _resolve_runtime_agent_kwargs() -> dict:
    """Resolve provider credentials for gateway-created AIAgent instances."""
    from logos_cli.runtime_provider import (
        resolve_runtime_provider,
        format_runtime_provider_error,
    )

    try:
        runtime = resolve_runtime_provider(
            requested=os.getenv("HERMES_INFERENCE_PROVIDER"),
        )
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    # The OpenAI Python SDK requires a non-empty api_key string (raises
    # OpenAIError if None or "").  "not-needed" is the standard placeholder
    # for local inference servers that don't require authentication —
    # widely used across the OpenAI-compatible ecosystem (Ollama, LM Studio
    # with auth disabled, llama.cpp, vLLM, etc.).  When auth IS enabled,
    # the real key from .env/config.yaml is used instead.
    _key = runtime.get("api_key") or ""
    return {
        "api_key": _key if _key else "not-needed",
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
    }


def _resolve_gateway_model() -> str:
    """Read model from env/config — mirrors the resolution in _run_agent_sync.

    Without this, temporary AIAgent instances (memory flush, /compress) fall
    back to the hardcoded default ("anthropic/claude-opus-4.6") which fails
    when the active provider is openai-codex.
    """
    model = os.getenv("HERMES_MODEL") or os.getenv("LLM_MODEL") or "anthropic/claude-opus-4.6"
    try:
        import yaml as _y
        _cfg_path = _hermes_home / "config.yaml"
        if _cfg_path.exists():
            with open(_cfg_path, encoding="utf-8") as _f:
                _cfg = _y.safe_load(_f) or {}
            _model_cfg = _cfg.get("model", {})
            if isinstance(_model_cfg, str):
                model = _model_cfg
            elif isinstance(_model_cfg, dict):
                model = _model_cfg.get("default", model)
    except Exception:
        pass
    return model


# Module-level reference to the running gateway runner so the launcher
# can request a graceful shutdown without abruptly stopping the event loop.
#
# IMPORTANT: the canonical storage for these is ``gateway.runtime_state``.
# We keep the ``_current_runner`` / ``_current_loop`` names here purely as
# legacy thin wrappers for any code in this file that still reads them,
# but **any code outside this module must go through runtime_state**.
# Why: ``python -m gateway.run`` loads this file as ``__main__``, and a
# second ``from gateway import run`` or ``import gateway.run`` from
# anywhere else re-loads it as a separate module object — two copies of
# every global. Anything assigning to ``_current_runner`` here mutates
# the ``__main__`` copy; anything reading ``gateway.run._current_runner``
# from an executor reads the *other* copy which stays ``None`` forever.
# ``gateway.runtime_state`` sits outside that trap because it's never
# imported as ``__main__``.
from gateway import runtime_state as _runtime_state


def _set_current_runner(runner: Optional["GatewayRunner"]) -> None:
    _runtime_state.set_current_runner(runner)


def request_gateway_shutdown() -> None:
    """Thread-safe: schedule runner.stop() from outside the event loop (e.g. launcher)."""
    r = _runtime_state.current_runner
    loop = _runtime_state.current_loop
    if r is None or loop is None:
        return
    try:
        if not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(r.stop()))
    except Exception:
        pass


class GatewayRunner:
    """
    Main gateway controller.
    
    Manages the lifecycle of all platform adapters and routes
    messages to/from the agent.
    """
    
    def __init__(self, config: Optional[GatewayConfig] = None):
        self.config = config or load_gateway_config()
        # Legacy env-token adapters: one per Platform. Populated for any
        # platform that has NO agent_channel_credentials rows (back-compat
        # for single-bot installs).
        self.adapters: Dict[Platform, BasePlatformAdapter] = {}
        # Per-agent credential-row adapters: keyed by
        # (agent_id, platform, label). A platform may have several rows
        # (Hermes's prod + staging Telegram bots, say) or rows across
        # different agents (Hermes's bot + Henry's bot). The startup
        # loop in start() populates this from
        # agent_channel_credentials.
        self.agent_adapters: Dict[Tuple[str, Platform, str], BasePlatformAdapter] = {}

        # WorkerRegistry tracks connected OpenShell sandbox workers over
        # WebSocket (/ws/worker). It lives on the runner, not on the HTTP
        # layer, because the runner has a longer lifecycle and both the
        # HTTP /chat endpoint and the platform dispatcher need to route
        # tasks through it. http_api.start_http_api reads it from the
        # runner at boot rather than creating its own copy. Platforms
        # dispatch inbound messages through this registry via
        # dispatch_platform_message below.
        from gateway.worker_registry import WorkerRegistry
        self.worker_registry = WorkerRegistry()

        # Load ephemeral config from config.yaml / env vars.
        # Both are injected at API-call time only and never persisted.
        self._prefill_messages = self._load_prefill_messages()
        self._ephemeral_system_prompt = self._load_ephemeral_system_prompt()
        self._reasoning_config = self._load_reasoning_config()
        self._show_reasoning = self._load_show_reasoning()
        self._provider_routing = self._load_provider_routing()
        self._fallback_model = self._load_fallback_model()

        # Wire process registry into session store for reset protection
        from tools.process_registry import process_registry
        self.session_store = SessionStore(
            self.config.sessions_dir, self.config,
            has_active_processes_fn=lambda key: process_registry.has_active_for_session(key),
        )
        self.delivery_router = DeliveryRouter(self.config)
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Per-session agent runtime overrides (set via /runtime command)
        self._session_runtime_overrides: Dict[str, str] = {}

        # Track running agents per session for interrupt support
        # Key: session_key, Value: AIAgent instance
        self._running_agents: Dict[str, Any] = {}
        self._pending_messages: Dict[str, str] = {}  # Queued messages during interrupt

        # Per-platform request counters (success / error) for /health reporting.
        # Keys are platform.value strings (e.g. "telegram", "discord").
        self._platform_stats: Dict[str, Dict[str, int]] = {}

        # Bounded set of recently-seen message IDs for idempotency.
        # Prevents double-processing when Telegram retries on timeout.
        import collections
        self._seen_message_ids: collections.deque = collections.deque(maxlen=500)
        
        # Track pending exec approvals per session
        # Key: session_key, Value: {"command": str, "pattern_key": str, ...}
        self._pending_approvals: Dict[str, Dict[str, Any]] = {}

        # Live session tracking — populated per _run_agent call, read by /status.
        # Key: session_key, Value: {platform, current_tool, tool_started_at,
        #   session_started_at, tool_count, error_count, recent_tools, stuck}
        self._session_status: Dict[str, Any] = {}

        # Ring buffer of recently completed sessions (newest last), capped at 20.
        import collections as _col
        self._recent_sessions: _col.deque = _col.deque(maxlen=20)

        # Ensure tirith security scanner is available (downloads if needed)
        try:
            from tools.tirith_security import ensure_installed
            ensure_installed()
        except Exception:
            pass  # Non-fatal — fail-open at scan time if unavailable
        
        # Initialize session database for session_search tool support
        self._session_db = None
        try:
            from core.state import SessionDB
            self._session_db = SessionDB()
        except Exception as e:
            logger.debug("SQLite session store not available: %s", e)
        
        # DM pairing store for code-based user authorization
        from gateway.pairing import PairingStore
        self.pairing_store = PairingStore()
        
        # Event hook system
        from gateway.hooks import HookRegistry
        self.hooks = HookRegistry()

        # Per-chat voice reply mode: "off" | "voice_only" | "all"
        self._voice_mode: Dict[str, str] = self._load_voice_modes()

    def _resolve_runtime(self, session_id: str) -> str:
        """Determine which agent runtime to use for a session.

        Priority: per-session override > config.yaml > default (hermes).
        """
        override = getattr(self, "_session_runtime_overrides", {}).get(session_id)
        if override:
            return override
        try:
            import yaml as _y
            _cfg_path = _hermes_home / "config.yaml"
            if _cfg_path.exists():
                with open(_cfg_path, encoding="utf-8") as _f:
                    _cfg = _y.safe_load(_f) or {}
                return _cfg.get("agent_runtime", "hermes")
        except Exception:
            pass
        return "hermes"

    # -- Voice mode persistence ------------------------------------------

    _VOICE_MODE_PATH = _hermes_home / "gateway_voice_mode.json"

    def _load_voice_modes(self) -> Dict[str, str]:
        try:
            data = json.loads(self._VOICE_MODE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

        if not isinstance(data, dict):
            return {}

        valid_modes = {"off", "voice_only", "all"}
        return {
            str(chat_id): mode
            for chat_id, mode in data.items()
            if mode in valid_modes
        }

    def _save_voice_modes(self) -> None:
        try:
            self._VOICE_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._VOICE_MODE_PATH.write_text(
                json.dumps(self._voice_mode, indent=2)
            )
        except OSError as e:
            logger.warning("Failed to save voice modes: %s", e)

    def _set_adapter_auto_tts_disabled(self, adapter, chat_id: str, disabled: bool) -> None:
        """Update an adapter's in-memory auto-TTS suppression set if present."""
        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        if not isinstance(disabled_chats, set):
            return
        if disabled:
            disabled_chats.add(chat_id)
        else:
            disabled_chats.discard(chat_id)

    def _sync_voice_mode_state_to_adapter(self, adapter) -> None:
        """Restore persisted /voice off state into a live platform adapter."""
        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        if not isinstance(disabled_chats, set):
            return
        disabled_chats.clear()
        disabled_chats.update(
            chat_id for chat_id, mode in self._voice_mode.items() if mode == "off"
        )

    # -----------------------------------------------------------------

    def _auto_ingest_session(self, session_id: str, msgs: list):
        """Optionally ingest the session transcript into the knowledge base.

        Gated by knowledge.auto_ingest_sessions config flag.  Extracts
        assistant responses (the substantive content) and ingests them as a
        single knowledge source named after the session.
        """
        try:
            from logos_cli.config import load_config
            cfg = load_config().get("knowledge", {})
        except Exception:
            return

        if not cfg.get("auto_ingest_sessions", False):
            return

        min_messages = cfg.get("auto_ingest_min_messages", 8)
        if len(msgs) < min_messages:
            return

        # Extract assistant turns — these contain the substantive analysis,
        # explanations, code reviews, research findings etc.
        parts = []
        for m in msgs:
            if m.get("role") == "assistant" and m.get("content"):
                content = m["content"].strip()
                if len(content) > 50:  # skip trivial responses
                    parts.append(content)

        if not parts:
            return

        transcript_text = "\n\n---\n\n".join(parts)
        source_name = f"session-{session_id[:12]}"

        try:
            from tools.knowledge_store import KnowledgeStore
            store = KnowledgeStore(
                knowledge_dir=_hermes_home / "knowledge",
                embedding_model=cfg.get("embedding_model", "nomic-embed-text"),
                embedding_endpoint=cfg.get("embedding_endpoint"),
                embedding_api_key=cfg.get("embedding_api_key"),
                chunk_size=cfg.get("chunk_size", 512),
                chunk_overlap=cfg.get("chunk_overlap", 64),
                max_chunks=cfg.get("max_chunks", 10_000),
            )
            result = store.ingest(transcript_text, source_name=source_name, source_type="session")
            if result.get("success"):
                logger.info(
                    "Auto-ingested session %s into knowledge base (%d chunks)",
                    session_id[:12], result.get("chunk_count", 0),
                )
            else:
                logger.debug("Auto-ingest skipped for session %s: %s", session_id[:12], result.get("error"))
        except Exception as exc:
            logger.debug("Auto-ingest failed for session %s: %s", session_id[:12], exc)

    @staticmethod
    def _load_prefill_messages() -> List[Dict[str, Any]]:
        """Load ephemeral prefill messages from config or env var.
        
        Checks HERMES_PREFILL_MESSAGES_FILE env var first, then falls back to
        the prefill_messages_file key in ~/.logos/config.yaml.
        Relative paths are resolved from ~/.hermes/.
        """
        import json as _json
        file_path = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
        if not file_path:
            try:
                import yaml as _y
                cfg_path = _hermes_home / "config.yaml"
                if cfg_path.exists():
                    with open(cfg_path, encoding="utf-8") as _f:
                        cfg = _y.safe_load(_f) or {}
                    file_path = cfg.get("prefill_messages_file", "")
            except Exception:
                pass
        if not file_path:
            return []
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = _hermes_home / path
        if not path.exists():
            logger.warning("Prefill messages file not found: %s", path)
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if not isinstance(data, list):
                logger.warning("Prefill messages file must contain a JSON array: %s", path)
                return []
            return data
        except Exception as e:
            logger.warning("Failed to load prefill messages from %s: %s", path, e)
            return []

    @staticmethod
    def _load_ephemeral_system_prompt() -> str:
        """Load ephemeral system prompt from config or env var.
        
        Checks HERMES_EPHEMERAL_SYSTEM_PROMPT env var first, then falls back to
        agent.system_prompt in ~/.logos/config.yaml.
        """
        prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "")
        if prompt:
            return prompt
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                return (cfg.get("agent", {}).get("system_prompt", "") or "").strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _load_reasoning_config() -> dict | None:
        """Load reasoning effort from config with env fallback.

        Checks agent.reasoning_effort in config.yaml first, then
        HERMES_REASONING_EFFORT as a fallback. Valid: "xhigh", "high",
        "medium", "low", "minimal", "none". Returns None to use default
        (medium).
        """
        effort = ""
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                effort = str(cfg.get("agent", {}).get("reasoning_effort", "") or "").strip()
        except Exception:
            pass
        if not effort:
            effort = os.getenv("HERMES_REASONING_EFFORT", "")
        if not effort:
            return None
        effort = effort.lower().strip()
        if effort == "none":
            return {"enabled": False}
        valid = ("xhigh", "high", "medium", "low", "minimal")
        if effort in valid:
            return {"enabled": True, "effort": effort}
        logger.warning("Unknown reasoning_effort '%s', using default (medium)", effort)
        return None

    @staticmethod
    def _load_show_reasoning() -> bool:
        """Load show_reasoning toggle from config.yaml display section."""
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                return bool(cfg.get("display", {}).get("show_reasoning", False))
        except Exception:
            pass
        return False

    @staticmethod
    def _load_background_notifications_mode() -> str:
        """Load background process notification mode from config or env var.

        Modes:
          - ``all``    — push running-output updates *and* the final message (default)
          - ``result`` — only the final completion message (regardless of exit code)
          - ``error``  — only the final message when exit code is non-zero
          - ``off``    — no watcher messages at all
        """
        mode = os.getenv("HERMES_BACKGROUND_NOTIFICATIONS", "")
        if not mode:
            try:
                import yaml as _y
                cfg_path = _hermes_home / "config.yaml"
                if cfg_path.exists():
                    with open(cfg_path, encoding="utf-8") as _f:
                        cfg = _y.safe_load(_f) or {}
                    raw = cfg.get("display", {}).get("background_process_notifications")
                    if raw is False:
                        mode = "off"
                    elif raw not in (None, ""):
                        mode = str(raw)
            except Exception:
                pass
        mode = (mode or "all").strip().lower()
        valid = {"all", "result", "error", "off"}
        if mode not in valid:
            logger.warning(
                "Unknown background_process_notifications '%s', defaulting to 'all'",
                mode,
            )
            return "all"
        return mode

    @staticmethod
    def _load_provider_routing() -> dict:
        """Load OpenRouter provider routing preferences from config.yaml."""
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                return cfg.get("provider_routing", {}) or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _load_fallback_model() -> dict | None:
        """Load fallback model config from config.yaml.

        Returns a dict with 'provider' and 'model' keys, or None if
        not configured / both fields empty.
        """
        try:
            import yaml as _y
            cfg_path = _hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                fb = cfg.get("fallback_model", {}) or {}
                if fb.get("provider") and fb.get("model"):
                    return fb
        except Exception:
            pass
        return None

    async def start(self) -> bool:
        """
        Start the gateway and all configured platform adapters.
        
        Returns True if at least one adapter connected successfully.
        """
        logger.info("Starting Hermes Gateway...")
        logger.info("Session storage: %s", self.config.sessions_dir)
        
        # Warn if no user allowlists are configured and open access is not opted in
        _any_allowlist = any(
            os.getenv(v)
            for v in ("TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_USERS",
                       "WHATSAPP_ALLOWED_USERS", "SLACK_ALLOWED_USERS",
                       "GATEWAY_ALLOWED_USERS")
        )
        _allow_all = os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes")
        if not _any_allowlist and not _allow_all:
            logger.warning(
                "No user allowlists configured. All unauthorized users will be denied. "
                "Set GATEWAY_ALLOW_ALL_USERS=true in ~/.logos/.env to allow open access, "
                "or configure platform allowlists (e.g., TELEGRAM_ALLOWED_USERS=your_id)."
            )
        
        # Discover and load event hooks
        self.hooks.discover_and_load()
        
        # Recover background processes from checkpoint (crash recovery)
        try:
            from tools.process_registry import process_registry
            recovered = process_registry.recover_from_checkpoint()
            if recovered:
                logger.info("Recovered %s background process(es) from previous run", recovered)
        except Exception as e:
            logger.warning("Process checkpoint recovery: %s", e)
        
        connected_count = 0

        # Auth DB initialisation — the HTTP layer calls this too during
        # start_http_api, but we need the tables up NOW so the per-agent
        # credential migration + adapter spawn below can read from them.
        # init_db is idempotent (CREATE TABLE IF NOT EXISTS + version
        # flags guard re-runs), so calling it from both places is fine.
        try:
            from gateway.auth import db as _auth_db_init
            from logos_cli.config import get_hermes_home as _get_hh
            _auth_db_init.init_db(_get_hh())
        except Exception as _init_exc:
            logger.warning("auth DB init from start(): %s", _init_exc)

        # ── Migration: seed per-agent credential rows from legacy env ───
        # First boot after the per-agent-credentials feature: if the
        # agent_channel_credentials table is empty but legacy env tokens
        # (TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN, …) are set, convert
        # each to a 'default' row on the first-listed named agent. This
        # means existing single-bot deployments flip onto the new
        # lifecycle with zero user action and a reversible shape in the
        # DB. Idempotent: only runs when the table is empty.
        try:
            self._migrate_env_tokens_to_channel_credentials()
        except Exception as _mig_err:
            logger.warning("env → channel credentials migration: %s", _mig_err)

        # ── Per-agent channel adapters (new path, one per credential row) ──
        # Each enabled row in agent_channel_credentials spawns its own
        # adapter instance, tagged with the owning agent_id. The
        # adapter stamps source.agent_id on every inbound event so
        # dispatch_platform_message skips the platform_routing lookup.
        # Tracks which platforms have at least one credential-driven
        # adapter so we know to SKIP the legacy env-token path below
        # for that platform (avoids two adapters polling the same bot).
        platforms_with_credentials: set = set()
        try:
            credential_count = await self._spawn_agent_channel_adapters(platforms_with_credentials)
            if credential_count:
                logger.info("✓ %d agent-scoped channel adapter(s) connected", credential_count)
                connected_count += credential_count
        except Exception as _ch_err:
            logger.exception("agent-scoped adapter spawn: %s", _ch_err)

        # ── Legacy platform adapters (env-token path, gateway-mediated) ──
        # Inbound messages no longer run the in-process AIAgent loop.
        # set_message_handler is bound to dispatch_platform_message, which
        # routes the event into a sandbox worker via WorkerRegistry. The
        # gateway holds platform credentials but never executes agent
        # code; the sandbox holds no credentials but executes the agent.
        # Skipped per-platform when a credential-driven adapter is already
        # running for that platform (mutex to prevent double polling).
        # See docs/migration/platforms-as-gateway-mediated.md.
        for platform, platform_config in self.config.platforms.items():
            if not platform_config.enabled:
                continue
            if platform in platforms_with_credentials:
                logger.debug(
                    "skipping legacy env-token adapter for %s (per-agent credentials active)",
                    platform.value,
                )
                continue
            adapter = self._create_adapter(platform, platform_config)
            if not adapter:
                logger.warning("No adapter available for %s", platform.value)
                continue
            adapter.set_message_handler(self.dispatch_platform_message)
            logger.info("Connecting to %s...", platform.value)
            try:
                success = await adapter.connect()
                if success:
                    self.adapters[platform] = adapter
                    self._sync_voice_mode_state_to_adapter(adapter)
                    connected_count += 1
                    logger.info("✓ %s connected", platform.value)
                else:
                    logger.warning("✗ %s failed to connect", platform.value)
            except Exception as e:
                logger.error("✗ %s error: %s", platform.value, e)

        if connected_count == 0:
            logger.info("Gateway running without platforms.")

        # ── Bootstrap platform_routing for any newly-enabled platforms ──
        # Each enabled platform needs at least one 'global' rule so
        # dispatch_platform_message has a target. We seed it with the
        # first named agent; the admin can later override via the
        # Platforms dashboard.
        try:
            self._bootstrap_platform_routing()
        except Exception as _bp_err:
            logger.warning("platform routing bootstrap: %s", _bp_err)

        # Update delivery router with adapters
        self.delivery_router.adapters = self.adapters
        
        self._running = True
        
        # Emit gateway:startup hook
        hook_count = len(self.hooks.loaded_hooks)
        if hook_count:
            logger.info("%s hook(s) loaded", hook_count)
        await self.hooks.emit("gateway:startup", {
            "platforms": [p.value for p in self.adapters.keys()],
        })
        
        if connected_count > 0:
            logger.info("Gateway running with %s platform(s)", connected_count)
        
        # Build initial channel directory for send_message name resolution
        try:
            from gateway.channel_directory import build_channel_directory
            directory = build_channel_directory(self.adapters)
            ch_count = sum(len(chs) for chs in directory.get("platforms", {}).values())
            logger.info("Channel directory built: %d target(s)", ch_count)
        except Exception as e:
            logger.warning("Channel directory build failed: %s", e)
        
        # Check if we're restarting after a /update command. If the update is
        # still running, keep watching so we notify once it actually finishes.
        notified = await self._send_update_notification()
        if not notified and any(
            path.exists()
            for path in (
                _hermes_home / ".update_pending.json",
                _hermes_home / ".update_pending.claimed.json",
            )
        ):
            self._schedule_update_notification_watch()

        # Start background session expiry watcher for proactive memory flushing
        asyncio.create_task(self._session_expiry_watcher())

        logger.info("Press Ctrl+C to stop")
        
        return True
    
    async def _session_expiry_watcher(self, interval: int = 300,
                                       max_per_cycle: int = 3):
        """Background task that proactively flushes memories for expired sessions.

        Runs every ``interval`` seconds (default 5 min). Each cycle flushes
        up to ``max_per_cycle`` expired sessions (default 3) — the remaining
        backlog is picked up next cycle. This bound matters on cold boots
        where a day's worth of expired sessions have accumulated: without
        the cap, one cycle would serialise ~N × 30s of aux-LLM retries
        through the default thread pool and starve the HTTP listener,
        which ALSO uses ``run_in_executor`` for static file serving.

        At interval=300 and max_per_cycle=3, the worst-case drain rate
        is 36 sessions / hour — plenty for normal usage, and a 50-session
        cold-boot backlog drains in ~1.4 hours without ever monopolising
        the event loop.
        """
        await asyncio.sleep(60)  # initial delay — let the gateway fully start
        while self._running:
            try:
                self.session_store._ensure_loaded()
                _flushed_this_cycle = 0
                for key, entry in list(self.session_store._entries.items()):
                    if _flushed_this_cycle >= max_per_cycle:
                        # Yield the rest of the backlog to the next cycle.
                        break
                    if entry.session_id in self.session_store._pre_flushed_sessions:
                        continue  # already flushed this session
                    if not self.session_store._is_session_expired(entry):
                        continue  # session still active
                    # Session has expired — mark it flushed so we don't
                    # revisit it every cycle. Memory saving is the
                    # sandboxed agent's own responsibility (it auto-flushes
                    # during context compression inside its sandbox); the
                    # gateway previously fired an in-process AIAgent turn
                    # to do it here, which violated the "no agents in the
                    # gateway process" rule and ran parallel to the
                    # sandbox's own flush.
                    self.session_store._pre_flushed_sessions.add(entry.session_id)
                    _flushed_this_cycle += 1
            except Exception as e:
                logger.debug("Session expiry watcher error: %s", e)
            # Sleep in small increments so we can stop quickly
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the gateway and disconnect all adapters."""
        logger.info("Stopping gateway...")
        self._running = False
        
        # Disconnect per-agent credential adapters (may include entries
        # also present in self.adapters as the shim-promoted default —
        # track which instances we've already closed to avoid double
        # disconnect).
        closed_instances: set = set()
        for key, adapter in list(self.agent_adapters.items()):
            try:
                await adapter.disconnect()
                closed_instances.add(id(adapter))
                logger.info("✓ %s/%s/%s disconnected", key[0], key[1].value, key[2])
            except Exception as e:
                logger.error(
                    "✗ %s/%s/%s disconnect error: %s",
                    key[0], key[1].value, key[2], e,
                )

        for platform, adapter in list(self.adapters.items()):
            if id(adapter) in closed_instances:
                continue  # already closed via agent_adapters
            try:
                await adapter.disconnect()
                logger.info("✓ %s disconnected", platform.value)
            except Exception as e:
                logger.error("✗ %s disconnect error: %s", platform.value, e)

        self.agent_adapters.clear()
        self.adapters.clear()
        self._shutdown_event.set()
        _set_current_runner(None)
        _runtime_state.set_current_loop(None)

        from gateway.status import remove_pid_file
        remove_pid_file()

        logger.info("Gateway stopped")
    
    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()
    
    def _create_adapter(
        self,
        platform: Platform,
        config: Any,
        *,
        agent_id: Optional[str] = None,
        credential_label: Optional[str] = None,
    ) -> Optional[BasePlatformAdapter]:
        """Create the appropriate adapter for a platform.

        ``agent_id`` / ``credential_label`` are forwarded to the adapter
        so events it emits can be stamped with the owning agent (see
        BasePlatformAdapter.set_message_handler). Legacy callers that
        go through the env-token path leave them as None.
        """
        kw = {"agent_id": agent_id, "credential_label": credential_label}
        if platform == Platform.TELEGRAM:
            from gateway.channels.telegram import TelegramAdapter, check_telegram_requirements
            if not check_telegram_requirements():
                logger.warning("Telegram: python-telegram-bot not installed")
                return None
            return TelegramAdapter(config, **kw)

        elif platform == Platform.DISCORD:
            from gateway.channels.discord import DiscordAdapter, check_discord_requirements
            if not check_discord_requirements():
                logger.warning("Discord: discord.py not installed")
                return None
            return DiscordAdapter(config, **kw)

        elif platform == Platform.WHATSAPP:
            from gateway.channels.whatsapp import WhatsAppAdapter, check_whatsapp_requirements
            if not check_whatsapp_requirements():
                logger.warning("WhatsApp: Node.js not installed or bridge not configured")
                return None
            return WhatsAppAdapter(config, **kw)

        elif platform == Platform.SLACK:
            from gateway.channels.slack import SlackAdapter, check_slack_requirements
            if not check_slack_requirements():
                logger.warning("Slack: slack-bolt not installed. Run: pip install 'hermes-agent[slack]'")
                return None
            return SlackAdapter(config, **kw)

        elif platform == Platform.SIGNAL:
            from gateway.channels.signal import SignalAdapter, check_signal_requirements
            if not check_signal_requirements():
                logger.warning("Signal: SIGNAL_HTTP_URL or SIGNAL_ACCOUNT not configured")
                return None
            return SignalAdapter(config, **kw)

        elif platform == Platform.HOMEASSISTANT:
            from gateway.channels.homeassistant import HomeAssistantAdapter, check_ha_requirements
            if not check_ha_requirements():
                logger.warning("HomeAssistant: aiohttp not installed or HASS_TOKEN not set")
                return None
            return HomeAssistantAdapter(config, **kw)

        elif platform == Platform.EMAIL:
            from gateway.channels.email import EmailAdapter, check_email_requirements
            if not check_email_requirements():
                logger.warning("Email: EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_IMAP_HOST, or EMAIL_SMTP_HOST not set")
                return None
            return EmailAdapter(config, **kw)

        return None

    async def connect_platform(self, platform: Platform) -> dict:
        """Connect (or reconnect) a single platform adapter at runtime.

        Called when a messaging token is saved via the Channels UI.
        Returns {ok, message}.
        """
        from gateway.config import PlatformConfig, load_gateway_config
        # Reload config so it picks up the newly-injected env var
        fresh_config = load_gateway_config()
        pconfig = fresh_config.platforms.get(platform)
        if not pconfig or not pconfig.enabled:
            return {"ok": False, "message": f"{platform.value} not enabled in config"}

        # Disconnect existing adapter if running
        existing = self.adapters.pop(platform, None)
        if existing:
            try:
                await existing.disconnect()
                logger.info("Disconnected old %s adapter for reconnect", platform.value)
            except Exception as e:
                logger.warning("Error disconnecting old %s adapter: %s", platform.value, e)

        adapter = self._create_adapter(platform, pconfig)
        if not adapter:
            return {"ok": False, "message": f"No adapter available for {platform.value}"}

        adapter.set_message_handler(self.dispatch_platform_message)
        try:
            success = await adapter.connect()
            if success:
                self.adapters[platform] = adapter
                self._sync_voice_mode_state_to_adapter(adapter)
                self.delivery_router.adapters = self.adapters
                logger.info("Runtime connect: %s connected", platform.value)
                return {"ok": True, "message": f"{platform.value} connected"}
            else:
                return {"ok": False, "message": f"{platform.value} failed to connect"}
        except Exception as e:
            logger.error("Runtime connect: %s error: %s", platform.value, e)
            return {"ok": False, "message": str(e)}

    async def disconnect_platform(self, platform: Platform) -> dict:
        """Disconnect a platform adapter at runtime."""
        existing = self.adapters.pop(platform, None)
        if not existing:
            return {"ok": True, "message": f"{platform.value} was not connected"}
        try:
            await existing.disconnect()
            self.delivery_router.adapters = self.adapters
            logger.info("Runtime disconnect: %s disconnected", platform.value)
            return {"ok": True, "message": f"{platform.value} disconnected"}
        except Exception as e:
            logger.warning("Runtime disconnect error: %s", e)
            return {"ok": True, "message": f"{platform.value} disconnected (with warnings)"}

    def _is_user_authorized(self, source: SessionSource) -> bool:
        """
        Check if a user is authorized to use the bot.
        
        Checks in order:
        1. Per-platform allow-all flag (e.g., DISCORD_ALLOW_ALL_USERS=true)
        2. Environment variable allowlists (TELEGRAM_ALLOWED_USERS, etc.)
        3. DM pairing approved list
        4. Global allow-all (GATEWAY_ALLOW_ALL_USERS=true)
        5. Default: deny
        """
        # Home Assistant events are system-generated (state changes), not
        # user-initiated messages.  The HASS_TOKEN already authenticates the
        # connection, so HA events are always authorized.
        if source.platform == Platform.HOMEASSISTANT:
            return True

        user_id = source.user_id
        if not user_id:
            return False

        platform_env_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOWED_USERS",
            Platform.DISCORD: "DISCORD_ALLOWED_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOWED_USERS",
            Platform.SLACK: "SLACK_ALLOWED_USERS",
            Platform.SIGNAL: "SIGNAL_ALLOWED_USERS",
            Platform.EMAIL: "EMAIL_ALLOWED_USERS",
        }
        platform_allow_all_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOW_ALL_USERS",
            Platform.DISCORD: "DISCORD_ALLOW_ALL_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOW_ALL_USERS",
            Platform.SLACK: "SLACK_ALLOW_ALL_USERS",
            Platform.SIGNAL: "SIGNAL_ALLOW_ALL_USERS",
            Platform.EMAIL: "EMAIL_ALLOW_ALL_USERS",
        }

        # Per-platform allow-all flag (e.g., DISCORD_ALLOW_ALL_USERS=true)
        platform_allow_all_var = platform_allow_all_map.get(source.platform, "")
        if platform_allow_all_var and os.getenv(platform_allow_all_var, "").lower() in ("true", "1", "yes"):
            return True

        # Check pairing store (always checked, regardless of allowlists)
        platform_name = source.platform.value if source.platform else ""
        if self.pairing_store.is_approved(platform_name, user_id):
            return True

        # Check platform-specific and global allowlists
        platform_allowlist = os.getenv(platform_env_map.get(source.platform, ""), "").strip()
        global_allowlist = os.getenv("GATEWAY_ALLOWED_USERS", "").strip()

        if not platform_allowlist and not global_allowlist:
            # No allowlists configured -- check global allow-all flag
            return os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes")

        # Check if user is in any allowlist
        allowed_ids = set()
        if platform_allowlist:
            allowed_ids.update(uid.strip() for uid in platform_allowlist.split(",") if uid.strip())
        if global_allowlist:
            allowed_ids.update(uid.strip() for uid in global_allowlist.split(",") if uid.strip())

        # WhatsApp JIDs have @s.whatsapp.net suffix — strip it for comparison
        check_ids = {user_id}
        if "@" in user_id:
            check_ids.add(user_id.split("@")[0])
        return bool(check_ids & allowed_ids)
    
    # ──────────────────────────────────────────────────────────────────
    # Phase 5.4 — platform routing bootstrap
    # ──────────────────────────────────────────────────────────────────

    def _bootstrap_platform_routing(self) -> None:
        """Ensure every enabled platform has a 'global' routing rule.

        Idempotent: only writes a row if none exists for that platform.
        Picks the first named agent as the default target. Admins can
        override via Admin → Platforms after the fact.
        """
        if not self.config.platforms:
            return
        from gateway.auth import db as _auth_db
        try:
            agents = _auth_db.list_agents()
        except Exception:
            logger.exception("bootstrap routing: list_agents failed")
            return
        if not agents:
            logger.info("bootstrap routing: no agents yet, skipping")
            return
        default_agent_id = agents[0]["id"]
        for platform, pconfig in self.config.platforms.items():
            if not pconfig.enabled:
                continue
            try:
                existing = _auth_db.resolve_platform_routing(platform.value)
                if existing:
                    continue
                _auth_db.upsert_platform_routing(
                    platform=platform.value,
                    scope="global",
                    scope_id="",
                    agent_id=default_agent_id,
                )
                logger.info(
                    "bootstrap routing: %s → %s (global)",
                    platform.value, agents[0].get("name", default_agent_id),
                )
            except Exception:
                logger.exception("bootstrap routing: %s failed", platform.value)

    # ──────────────────────────────────────────────────────────────────
    # Per-agent channel credentials
    # ──────────────────────────────────────────────────────────────────

    _ENV_TOKEN_BY_PLATFORM: Dict[Platform, str] = {
        Platform.TELEGRAM: "TELEGRAM_BOT_TOKEN",
        Platform.DISCORD: "DISCORD_BOT_TOKEN",
        Platform.SLACK: "SLACK_BOT_TOKEN",
        Platform.WHATSAPP: "WHATSAPP_TOKEN",
        Platform.SIGNAL: "SIGNAL_HTTP_URL",
        Platform.HOMEASSISTANT: "HASS_TOKEN",
        Platform.EMAIL: "EMAIL_PASSWORD",
    }

    def _migrate_env_tokens_to_channel_credentials(self) -> None:
        """First-boot seed: env-token → default credential row.

        Runs only when ``agent_channel_credentials`` is totally empty
        (treated as a proxy for "never run on this gateway"). For
        every Platform whose env token is set and whose
        ``PlatformConfig.enabled`` is true, inserts a row on the
        first-listed named agent with ``label='default'``. The env
        token keeps working for this boot (the adapter reads from it)
        but subsequent boots will use the DB row, so rotating the
        token means updating the credentials table, not touching env.

        Explicitly scoped to populate exactly one row per platform —
        if the user already ran the UI add-credential flow, the table
        won't be empty and this migration is a no-op.
        """
        if not self.config.platforms:
            return
        from gateway.auth import db as _auth_db
        try:
            existing = _auth_db.list_agent_channel_credentials()
        except Exception:
            logger.exception("env→credentials migration: list failed")
            return
        if existing:
            return  # already populated — user or previous boot did it
        try:
            agents = _auth_db.list_agents()
        except Exception:
            logger.exception("env→credentials migration: list_agents failed")
            return
        if not agents:
            logger.debug("env→credentials migration: no agents, skipping")
            return
        default_agent = agents[0]
        seeded = 0
        for platform, pconfig in self.config.platforms.items():
            if not pconfig.enabled:
                continue
            env_key = self._ENV_TOKEN_BY_PLATFORM.get(platform)
            if not env_key:
                continue
            token = (os.environ.get(env_key) or pconfig.token or "").strip()
            if not token:
                continue
            try:
                _auth_db.upsert_agent_channel_credential(
                    agent_id=default_agent["id"],
                    platform=platform.value,
                    token=token,
                    label="default",
                    enabled=True,
                )
                seeded += 1
                logger.info(
                    "env→credentials: seeded %s default row for agent %s",
                    platform.value, default_agent.get("name") or default_agent["id"],
                )
                # Give the agent the tool + network policy it needs to
                # actually USE the token it just got. Without this step
                # the migration leaves the agent in the "has a bot but
                # can't send on it" state that the first round of this
                # feature shipped with. Best-effort.
                try:
                    from gateway import policies as _gp
                    _gp.ensure_channel_access(default_agent["id"], platform.value)
                except Exception:
                    logger.exception(
                        "env→credentials: ensure_channel_access(%s, %s) failed",
                        default_agent["id"], platform.value,
                    )
            except Exception:
                logger.exception("env→credentials: upsert %s failed", platform.value)
        if seeded:
            logger.info("env→credentials: migrated %d platform token(s)", seeded)

    async def _spawn_agent_channel_adapters(
        self, platforms_with_credentials: set,
    ) -> int:
        """Spawn one adapter per enabled ``agent_channel_credentials`` row.

        Each adapter is tagged with its owning ``agent_id`` /
        ``credential_label`` so incoming events get
        ``source.agent_id`` stamped automatically (see
        BasePlatformAdapter.set_message_handler). That stamping is
        what lets dispatch_platform_message skip the platform_routing
        lookup — the token IS the routing.

        Populates ``self.agent_adapters`` by (agent_id, platform, label).
        Also sets the FIRST adapter per platform into
        ``self.adapters[platform]`` so the existing delivery_router /
        channel_directory code (which keys by Platform) still finds an
        outbound path. That promotion is a shim until task #4 teaches
        the router to pick the right per-agent adapter for outbound.

        Adds each platform it spawns for to
        ``platforms_with_credentials`` so the legacy env-token startup
        loop can skip them — prevents double-polling the same bot.

        Returns the number of adapters successfully connected.
        """
        from gateway.auth import db as _auth_db
        from gateway.config import Platform as _Platform, PlatformConfig
        try:
            rows = _auth_db.list_agent_channel_credentials(enabled_only=True)
        except Exception:
            logger.exception("_spawn_agent_channel_adapters: list failed")
            return 0
        if not rows:
            return 0

        connected = 0
        for row in rows:
            try:
                platform = _Platform(row["platform"])
            except ValueError:
                logger.warning(
                    "_spawn_agent_channel_adapters: unknown platform %s on row %s",
                    row["platform"], row["id"],
                )
                continue
            # Build a PlatformConfig that carries only this row's token,
            # so the adapter talks to THIS bot and no other.
            pconfig = PlatformConfig(enabled=True, token=row["token"])
            adapter = self._create_adapter(
                platform,
                pconfig,
                agent_id=row["agent_id"],
                credential_label=row["label"],
            )
            if not adapter:
                logger.warning(
                    "_spawn_agent_channel_adapters: no adapter class for %s (row %s)",
                    platform.value, row["id"],
                )
                continue
            adapter.set_message_handler(self.dispatch_platform_message)
            try:
                success = await adapter.connect()
            except Exception:
                logger.exception(
                    "_spawn_agent_channel_adapters: connect failed for %s/%s/%s",
                    row["agent_id"], platform.value, row["label"],
                )
                continue
            if not success:
                logger.warning(
                    "_spawn_agent_channel_adapters: adapter.connect returned False for %s/%s/%s",
                    row["agent_id"], platform.value, row["label"],
                )
                continue
            key = (row["agent_id"], platform, row["label"])
            self.agent_adapters[key] = adapter
            # Shim for outbound: first adapter per platform also lives
            # in self.adapters[platform] so delivery_router finds one.
            # Multi-agent outbound gets proper routing in task #4.
            self.adapters.setdefault(platform, adapter)
            self._sync_voice_mode_state_to_adapter(adapter)
            platforms_with_credentials.add(platform)
            connected += 1
            logger.info(
                "✓ %s/%s/%s connected (agent-scoped)",
                row["agent_id"], platform.value, row["label"],
            )
        return connected

    async def connect_agent_channel(self, cred_id: str) -> dict:
        """Hot-connect an adapter for a newly-saved credential row.

        Called from the POST /admin/agents/{id}/channels and the
        toggle handler so users don't have to restart the gateway
        after adding or re-enabling a bot.

        Idempotent: if an adapter already exists for the row's
        (agent_id, platform, label) key, it's disconnected first so
        a token rotation cleanly replaces the old poller.

        Returns ``{ok, message}``.
        """
        from gateway.auth import db as _auth_db
        from gateway.config import Platform as _Platform, PlatformConfig
        try:
            row = _auth_db.get_agent_channel_credential(cred_id)
        except Exception as exc:
            return {"ok": False, "message": f"credential lookup failed: {exc}"}
        if not row:
            return {"ok": False, "message": "credential not found"}
        if not row.get("enabled"):
            # Explicitly disabled rows shouldn't be connected. Caller
            # used the wrong entry point — tell them.
            return {"ok": False, "message": "credential is disabled"}

        try:
            platform = _Platform(row["platform"])
        except ValueError:
            return {"ok": False, "message": f"unknown platform: {row['platform']}"}

        key = (row["agent_id"], platform, row["label"])

        # Disconnect any existing adapter for this key (rotation path).
        existing = self.agent_adapters.pop(key, None)
        if existing:
            try:
                await existing.disconnect()
                # If this adapter was also the shim-promoted default in
                # self.adapters[platform], drop it there too so the
                # upcoming setdefault can re-promote the new instance.
                if self.adapters.get(platform) is existing:
                    self.adapters.pop(platform, None)
            except Exception:
                logger.exception("connect_agent_channel: old disconnect failed")

        # Mutex with the legacy env-token path: if self.adapters[platform]
        # currently holds an env-token adapter (not from a credential
        # row — the new one will be the first agent_adapter for this
        # platform), swap it out. Otherwise two pollers fight over the
        # same bot and messages get double-delivered.
        # Safe check: if the current self.adapters[platform] isn't any
        # value in self.agent_adapters, it's the legacy one.
        current_default = self.adapters.get(platform)
        if current_default is not None and current_default not in self.agent_adapters.values():
            try:
                await current_default.disconnect()
                logger.info(
                    "connect_agent_channel: disconnected legacy env-token adapter for %s",
                    platform.value,
                )
            except Exception:
                logger.exception("connect_agent_channel: legacy disconnect failed")
            self.adapters.pop(platform, None)

        pconfig = PlatformConfig(enabled=True, token=row["token"])
        adapter = self._create_adapter(
            platform,
            pconfig,
            agent_id=row["agent_id"],
            credential_label=row["label"],
        )
        if not adapter:
            return {"ok": False, "message": f"no adapter class for {platform.value}"}
        adapter.set_message_handler(self.dispatch_platform_message)
        try:
            success = await adapter.connect()
        except Exception as exc:
            logger.exception("connect_agent_channel: connect failed")
            return {"ok": False, "message": str(exc)}
        if not success:
            return {"ok": False, "message": "adapter.connect returned False"}

        self.agent_adapters[key] = adapter
        self.adapters.setdefault(platform, adapter)
        self._sync_voice_mode_state_to_adapter(adapter)
        self.delivery_router.adapters = self.adapters
        logger.info(
            "connect_agent_channel: ✓ %s/%s/%s connected",
            row["agent_id"], platform.value, row["label"],
        )
        return {"ok": True, "message": f"{platform.value} connected"}

    async def disconnect_agent_channel(self, cred_id: str) -> dict:
        """Hot-disconnect the adapter for a deleted/disabled credential.

        Looks the row up either from the DB (if it still exists, e.g.
        for the toggle-to-disabled case) or reconstructs the key from
        the caller's info. Callers should pass the cred_id BEFORE
        deleting the row so we can resolve the (agent, platform,
        label) tuple.
        """
        from gateway.auth import db as _auth_db
        try:
            row = _auth_db.get_agent_channel_credential(cred_id)
        except Exception:
            row = None
        if not row:
            # The caller may have deleted the row already; sweep any
            # adapters whose credential id matches (rare but possible).
            # Nothing to do if we can't resolve the key and no orphans
            # exist — that's the common path when the row is gone.
            return {"ok": True, "message": "no adapter bound"}

        from gateway.config import Platform as _Platform
        try:
            platform = _Platform(row["platform"])
        except ValueError:
            return {"ok": False, "message": f"unknown platform: {row['platform']}"}
        key = (row["agent_id"], platform, row["label"])

        existing = self.agent_adapters.pop(key, None)
        if not existing:
            return {"ok": True, "message": "not connected"}
        try:
            await existing.disconnect()
            if self.adapters.get(platform) is existing:
                self.adapters.pop(platform, None)
            self.delivery_router.adapters = self.adapters
            logger.info(
                "disconnect_agent_channel: ✓ %s/%s/%s disconnected",
                row["agent_id"], platform.value, row["label"],
            )
            return {"ok": True, "message": f"{platform.value} disconnected"}
        except Exception as exc:
            logger.exception("disconnect_agent_channel: disconnect failed")
            return {"ok": False, "message": str(exc)}

    # ──────────────────────────────────────────────────────────────────
    # Phase 5.3 — dispatch_platform_message
    # ──────────────────────────────────────────────────────────────────

    async def dispatch_platform_message(self, event: MessageEvent) -> Optional[str]:
        """Dispatch an inbound platform message to a sandbox worker.

        Phase 5.3 implementation: resolves the target named agent,
        builds a minimal task payload and calls
        ``worker_registry.dispatch_task`` over the worker's WebSocket.

        Agent resolution (Phase 5.4):
          1. ``platform_routing`` table — most-specific match wins
             (chat → user → global). Setup wizard seeds one ``global``
             row per enabled platform pointing at the default agent.
          2. Fallback: first named agent in the DB.

        Failure modes returned as friendly strings (adapter will send
        them to the user verbatim):
          - no named agents in DB → "no agent configured"
          - target worker not connected → "sandbox not ready"
          - worker busy with another task → "agent is thinking"
          - dispatch timeout → "agent took too long to respond"
          - any other error → generic failure string

        Authorisation, slash-command handling (`/new` etc) and approval
        flows still need to be ported into this method — they were dropped
        when the legacy ``_handle_message`` was deleted in Phase 5.6.
        Attachment enrichment (audio→transcription, image→vision,
        document→context-note) is wired in below; the other three remain
        as a follow-up.
        """
        source = event.source
        platform_name = source.platform.value if source and source.platform else "unknown"
        user_id = source.user_id if source else ""
        chat_id = source.chat_id if source else ""

        # ── 1. Resolve target agent ───────────────────────────────────
        try:
            from gateway.auth import db as _auth_db
            agents = _auth_db.list_agents()
        except Exception as exc:
            logger.exception("dispatch_platform_message: failed to load agents")
            return "⚠️ Agent registry is unavailable right now. Please try again."

        if not agents:
            logger.warning(
                "dispatch_platform_message: no named agents in DB (platform=%s user=%s)",
                platform_name, user_id,
            )
            return (
                "⚠️ No agent is configured on this Logos instance. "
                "Open the web dashboard and create an agent to get started."
            )

        # 1a. If the adapter stamped an agent_id on the event, it's a
        # per-agent-credential adapter and already knows exactly whose
        # bot received the update. Skip the routing table entirely —
        # the token IS the routing.
        agent = None
        stamped_agent_id = getattr(source, "agent_id", None) if source else None
        if stamped_agent_id:
            try:
                agent = _auth_db.get_agent(stamped_agent_id)
            except Exception:
                logger.exception(
                    "dispatch_platform_message: get_agent(%s) failed", stamped_agent_id,
                )
                agent = None

        # 1b. Legacy routing: most-specific routing rule wins (chat →
        # user → global). Used only when no agent was stamped on the
        # event (i.e. the adapter was created from a global env token,
        # not a per-agent credential row).
        if agent is None:
            try:
                rule = _auth_db.resolve_platform_routing(
                    platform=platform_name,
                    chat_id=chat_id or "",
                    user_id=user_id or "",
                )
            except Exception:
                logger.exception("dispatch_platform_message: routing lookup failed")
                rule = None
            if rule and rule.get("agent_id"):
                try:
                    agent = _auth_db.get_agent(rule["agent_id"])
                except Exception:
                    logger.exception("dispatch_platform_message: get_agent failed")
                    agent = None

        # 1c. Final fallback: first named agent.
        if agent is None:
            agent = agents[0]
        agent_name = agent.get("name", "")

        # ── 2. Resolve the worker_id for this agent ───────────────────
        try:
            from gateway.executors.openshell import _sanitize_sandbox_name
            worker_id = _sanitize_sandbox_name(f"hermes-{agent_name}")
        except Exception:
            logger.exception("dispatch_platform_message: sanitize_sandbox_name failed")
            return "⚠️ Internal error resolving agent sandbox."

        worker_entry = self.worker_registry.get(worker_id)
        if not worker_entry or not worker_entry.healthy:
            # Reactive auto-respawn: the sandbox is absent (host reboot,
            # admin-deleted, gateway crash). Trigger a fresh spawn using
            # the same executor + InstanceConfig shape as the startup
            # resurrect pass. Platform messages don't stream, so no
            # on_event callback — the user just waits 10-30s for the
            # reply instead of getting "check Admin → Sandboxes."
            try:
                from gateway.sandbox_heal import ensure_sandbox_alive
                from gateway.executors.openshell import OpenShellExecutor
                _heal_ok, worker_entry = await ensure_sandbox_alive(
                    worker_registry=self.worker_registry,
                    executor=OpenShellExecutor(),
                    worker_id=worker_id,
                    agent_record=agent,
                )
            except Exception:
                logger.exception("dispatch_platform_message: auto-respawn failed")
                _heal_ok = False
            if not _heal_ok:
                logger.info(
                    "dispatch_platform_message: worker %s not connected and auto-respawn failed (platform=%s)",
                    worker_id, platform_name,
                )
                return (
                    f"⚠️ {agent_name}'s sandbox isn't running and auto-respawn failed. "
                    "Check Admin → Sandboxes on the dashboard, then try again."
                )

        # ── 3. Build session + history ────────────────────────────────
        session_id = ""
        session_key = ""
        try:
            session_entry = self.session_store.get_or_create_session(source)
            session_id = session_entry.session_id
            session_key = getattr(session_entry, "session_key", "")
        except Exception:
            logger.exception("dispatch_platform_message: session_store failure")
            session_key = f"{platform_name}:{user_id or 'unknown'}"
            session_id = session_key

        # Load recent conversation history from the session store so the
        # agent has context of prior turns. Without this, every Telegram
        # (or other platform) message arrives as a completely fresh
        # exchange — the sandbox is a new `openshell sandbox exec` process
        # each time and carries no memory of what the user just said.
        #
        # The earlier comment here claimed the sandbox maintained context
        # via session_id; it doesn't. Each task is stateless, and the web
        # /chat path works only because its client (the dashboard) sends
        # recent history in the POST body. Platform adapters have no
        # such client, so the gateway has to repopulate from the DB.
        #
        # Cap via LOGOS_PLATFORM_HISTORY_LIMIT (default 50 turns worth).
        # Filter to user/assistant roles only — system messages are
        # rebuilt fresh each turn from the capabilities_prompt + soul,
        # and tool messages reference tool_call_ids from prior runs
        # that won't match this turn's calls.
        history: list[dict] = []
        try:
            _history_limit = int(os.environ.get("LOGOS_PLATFORM_HISTORY_LIMIT", "50"))
        except ValueError:
            _history_limit = 50
        if session_id and _history_limit > 0:
            try:
                _full = self.session_store.load_transcript(session_id) or []
                _filtered = [
                    {"role": m.get("role"), "content": m.get("content") or ""}
                    for m in _full
                    if m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str)
                    and m.get("content").strip()
                ]
                history = _filtered[-_history_limit:]
            except Exception:
                logger.exception(
                    "dispatch_platform_message: history load failed for session=%s",
                    session_id,
                )
                history = []

        # ── 4. Enrich message with attachments ────────────────────────
        # Voice messages, images, and documents arrive on the MessageEvent
        # via media_urls/media_types. The platform adapter has already
        # cached them locally; here we run them through the same
        # enrichment pipeline the HTTP /chat path uses so the agent
        # actually sees the transcript / vision description / doc note
        # instead of just the user's caption (or empty text). Failure
        # is non-fatal — we fall through with the raw message rather
        # than dropping the dispatch.
        message_text = event.text or ""
        if event.media_urls:
            try:
                message_text = await self._enrich_message_with_attachments(
                    message_text,
                    event.media_urls,
                    event.media_types or [],
                )
            except Exception:
                logger.exception(
                    "dispatch_platform_message: attachment enrichment failed (platform=%s, urls=%s)",
                    platform_name, event.media_urls,
                )

        # ── 5. Dispatch to sandbox worker ─────────────────────────────
        import uuid as _uuid
        # Compose a real system prompt: identity + soul + per-platform
        # routing hint. Without the identity preamble the agent answers
        # "I'm an AI assistant" when asked its name (the soul is generic
        # and the sandbox worker forwards context_prompt verbatim).
        from gateway.session import build_agent_system_prompt as _basp
        platform_hint = (
            f"You are being addressed on {platform_name} by user "
            f"{getattr(source, 'user_name', None) or user_id or 'unknown'}. "
            f"Respond naturally — the adapter will deliver your reply to the channel."
        )
        context_prompt = _basp(agent, platform_hint)
        task_payload = {
            "type":           "run_conversation",
            "task_id":        str(_uuid.uuid4()),
            "session_id":     session_id,
            "session_key":    session_key,
            "message":        message_text,
            "history":        history,
            "context_prompt": context_prompt,
            "toolsets":       worker_entry.toolsets or ["hermes-cli"],
            "max_iterations": int(os.environ.get(
                "LOGOS_MAX_ITERATIONS",
                os.environ.get("HERMES_MAX_ITERATIONS", "1000"),
            )),
        }

        # Emit a dispatch row so platform messages (Discord/Telegram/
        # WhatsApp/etc.) show up in the Runs tab. The /chat path does
        # this inline at http_api.py:3930; this mirrors it for the
        # platform-driven code path, with the same STAMP snapshot
        # fields (soul, toolsets_snapshot, policy_snapshot) so the
        # Runs UI can render pills for platform runs the same way it
        # does for web-chat runs. Best-effort — never blocks dispatch.
        import json as _json
        import time as _time
        _dispatch_id = None
        _dispatch_started = _time.time()
        try:
            from gateway.auth import db as _auth_db
            # agent is a dict (from _auth_db.get_agent / list_agents),
            # NOT an object — earlier getattr() calls were silently
            # returning "" because dicts don't expose keys as attrs.
            _agent_id = agent.get("id") or ""
            _agent_soul = agent.get("soul_slug") or ""
            # Model resolution mirrors the /chat path: per-agent setting
            # wins, fall back to gateway-wide env. worker_entry doesn't
            # carry a .model attribute on the health-entry shim.
            _agent_model = (
                agent.get("model")
                or os.environ.get("LOGOS_MODEL")
                or os.environ.get("HERMES_MODEL")
                or ""
            )
            # Toolsets + policy snapshots at dispatch time — captured
            # verbatim from the agent row so the Runs tab's T and P
            # pills survive later config edits.
            _toolsets_raw = agent.get("toolsets") or ""
            try:
                _tl = _json.loads(_toolsets_raw) if _toolsets_raw else []
                _toolsets_json = _json.dumps(_tl) if isinstance(_tl, list) and _tl else ""
            except Exception:
                _toolsets_json = ""
            _presets_raw = agent.get("applied_presets") or ""
            try:
                _pl = _json.loads(_presets_raw) if _presets_raw else []
                _policy_json = _json.dumps(_pl) if isinstance(_pl, list) and _pl else ""
            except Exception:
                _policy_json = ""
            # user_id: resolve the platform chat user to a Logos user
            # via user_platform_links. Falls back to the agent's
            # creator_id when no link exists (single-user installs
            # where nobody has bothered to link their Telegram account
            # to their Logos login). The raw platform uid is always
            # captured in origin_detail for audit.
            _dispatch_user_id = ""
            if user_id:
                try:
                    _dispatch_user_id = _auth_db.resolve_platform_user(
                        platform_name, user_id,
                    ) or ""
                except Exception:
                    pass
            if not _dispatch_user_id:
                _dispatch_user_id = agent.get("creator_id") or ""
            _dispatch_id = _auth_db.create_dispatch(
                task_id=task_payload["task_id"],
                agent_id=_agent_id,
                sandbox_name=worker_id or "",
                model=_agent_model,
                origin=f"platform_{platform_name}" if platform_name else "platform",
                origin_detail=_json.dumps({
                    "platform": platform_name,
                    "platform_user_id": user_id or "",
                    "chat_id": getattr(source, "chat_id", "") or "",
                }),
                session_id=session_id or "",
                user_id=_dispatch_user_id,
                soul=_agent_soul,
                toolsets_snapshot=_toolsets_json,
                user_message=message_text or "",
                policy_snapshot=_policy_json,
            )
        except Exception:
            logger.exception("dispatch_platform_message: create_dispatch failed")

        def _finish_dispatch(status, err=None, usage=None):
            if not _dispatch_id:
                return
            try:
                from gateway.auth import db as _auth_db
                _auth_db.complete_dispatch(
                    _dispatch_id, status=status,
                    elapsed_s=_time.time() - _dispatch_started,
                    prompt_tokens=(usage or {}).get("prompt_tokens"),
                    completion_tokens=(usage or {}).get("completion_tokens"),
                    error=err,
                )
            except Exception:
                pass

        # Seed _session_status so the Live Executions panel shows this
        # platform-initiated run in flight. Without it, Discord/Telegram/
        # WhatsApp-triggered agent turns never appeared in the live
        # panel even though dispatches worked fine.
        _now_fn = _time.time
        _platform_val = event.source.platform.value if event.source and event.source.platform else "unknown"
        if session_key:
            self._session_status[session_key] = {
                "platform": _platform_val,
                "agent_name": agent_name or "",
                "current_tool": "thinking…",
                "tool_started_at": _now_fn(),
                "session_started_at": _now_fn(),
                "tool_count": 0,
                "error_count": 0,
                "recent_tools": [],
                "stuck": False,
            }

        async def _on_platform_stream(msg):
            etype = (msg or {}).get("type")
            if not session_key or session_key not in self._session_status:
                return
            entry = self._session_status[session_key]
            if etype == "tool_start":
                tool_name = msg.get("tool", "") or "unknown"
                entry["current_tool"] = tool_name
                entry["tool_started_at"] = _now_fn()
                entry["tool_count"] = (entry.get("tool_count") or 0) + 1
                recent = entry.setdefault("recent_tools", [])
                if not recent or recent[-1] != tool_name:
                    recent.append(tool_name)
                    if len(recent) > 10:
                        recent.pop(0)
            elif etype == "tool_end":
                if not bool(msg.get("success", True)):
                    entry["error_count"] = (entry.get("error_count") or 0) + 1
                entry["current_tool"] = "thinking…"
                entry["tool_started_at"] = _now_fn()

        def _clear_live_status():
            if session_key and session_key in self._session_status:
                try:
                    del self._session_status[session_key]
                except Exception:
                    pass

        try:
            from gateway.worker_registry_v2 import dispatch_task_v2
            result = await dispatch_task_v2(
                worker_id, task_payload,
                timeout=float(os.environ.get(
                    "LOGOS_AGENT_TIMEOUT",
                    os.environ.get("HERMES_AGENT_TIMEOUT", "300"),
                )),
                on_stream_event=_on_platform_stream,
            )
        except asyncio.TimeoutError:
            _clear_live_status()
            _finish_dispatch("error", err="timeout")
            logger.warning("dispatch_platform_message: worker %s timed out", worker_id)
            return "⚠️ The agent took too long to respond. Please try again."
        except ConnectionError as exc:
            _clear_live_status()
            _finish_dispatch("error", err=f"disconnected: {exc}")
            logger.info("dispatch_platform_message: worker %s disconnected: %s", worker_id, exc)
            return f"⚠️ {agent_name}'s sandbox disconnected mid-task. Please try again."
        except RuntimeError as exc:
            _clear_live_status()
            _finish_dispatch("error", err=f"busy: {exc}")
            logger.info("dispatch_platform_message: worker %s busy: %s", worker_id, exc)
            return f"⚠️ {agent_name} is still working on another message. Please wait."
        except Exception as exc:
            _clear_live_status()
            _finish_dispatch("error", err=str(exc)[:500])
            logger.exception("dispatch_platform_message: dispatch_task failed")
            return "⚠️ The agent hit an unexpected error. Check the gateway logs."

        _clear_live_status()
        _finish_dispatch(
            (result or {}).get("status", "ok"),
            usage=(result or {}).get("usage") or {},
        )

        # ── 5. Persist both turns to the session transcript ─────────
        # The /chat web path writes messages inline as SSE events
        # arrive (http_api.py:4171). The platform path has no SSE
        # stream — it gets a single final_response dict back — so
        # we write both the user turn and the assistant reply here.
        # Without this, Telegram conversations never land in the
        # session DB and the TG pill in /chats shows empty.
        final = (result or {}).get("final_response") or ""
        if session_id:
            try:
                # LOG-26: append_to_transcript now handles embedding on
                # a background thread, so the older explicit embed
                # calls that used to live here are redundant (and were
                # synchronous — adding 100-1000ms of latency per
                # platform reply).
                self.session_store.append_to_transcript(
                    session_id, {"role": "user", "content": message_text},
                )
                if final:
                    self.session_store.append_to_transcript(
                        session_id, {"role": "assistant", "content": final},
                    )
            except Exception:
                logger.exception(
                    "dispatch_platform_message: transcript write failed for session=%s",
                    session_id,
                )

        if not final:
            logger.warning("dispatch_platform_message: worker returned empty final_response")
            return "The agent returned an empty response."
        return final

    async def _handle_reset_command(self, event: MessageEvent) -> str:
        """Handle /new or /reset command."""
        source = event.source
        
        # Get existing session key
        session_key = self.session_store._generate_session_key(source)

        # Reset the session. The previously-fired in-process memory flush
        # was pre-sandbox residue; the sandboxed agent auto-flushes on
        # context compression inside its own sandbox.
        new_entry = self.session_store.reset_session(session_key)
        
        # Emit session:reset hook
        await self.hooks.emit("session:reset", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })
        
        if new_entry:
            return "✨ Session reset! I've started fresh with no memory of our previous conversation."
        else:
            # No existing session, just create one
            self.session_store.get_or_create_session(source, force_new=True)
            return "✨ New session started!"
    
    async def _handle_status_command(self, event: MessageEvent) -> str:
        """Handle /status command."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        
        connected_platforms = [p.value for p in self.adapters.keys()]
        
        # Check if there's an active agent
        session_key = session_entry.session_key
        is_running = session_key in self._running_agents
        
        lines = [
            "📊 **Hermes Gateway Status**",
            "",
            f"**Session ID:** `{session_entry.session_id[:12]}...`",
            f"**Created:** {session_entry.created_at.strftime('%Y-%m-%d %H:%M')}",
            f"**Last Activity:** {session_entry.updated_at.strftime('%Y-%m-%d %H:%M')}",
            f"**Tokens:** {session_entry.total_tokens:,}",
            f"**Agent Running:** {'Yes ⚡' if is_running else 'No'}",
            "",
            f"**Connected Platforms:** {', '.join(connected_platforms)}",
        ]
        
        return "\n".join(lines)
    
    async def _handle_stop_command(self, event: MessageEvent) -> str:
        """Handle /stop command - interrupt a running agent."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        
        if session_key in self._running_agents:
            agent = self._running_agents[session_key]
            agent.interrupt()
            return "⚡ Stopping the current task... The agent will finish its current step and respond."
        else:
            return "No active task to stop."
    
    async def _handle_help_command(self, event: MessageEvent) -> str:
        """Handle /help command - list available commands."""
        lines = [
            "📖 **Hermes Commands**\n",
            "`/new` — Start a new conversation",
            "`/reset` — Reset conversation history",
            "`/status` — Show session info",
            "`/stop` — Interrupt the running agent",
            "`/model [provider:model]` — Show/change model (or switch provider)",
            "`/provider` — Show available providers and auth status",
            "`/personality [name]` — Set a personality",
            "`/retry` — Retry your last message",
            "`/undo` — Remove the last exchange",
            "`/sethome` — Set this chat as the home channel",
            "`/title [name]` — Set or show the session title",
            "`/resume [name]` — Resume a previously-named session",
            "`/usage` — Show token usage for this session",
            "`/insights [days]` — Show usage insights and analytics",
            "`/reasoning [level|show|hide]` — Set reasoning effort or toggle display",
            "`/rollback [number]` — List or restore filesystem checkpoints",
            "`/voice [on|off|tts|status]` — Toggle voice reply mode",
            "`/runtime [name]` — Show or switch agent runtime (hermes, claude-direct)",
            "`/reload-mcp` — Reload MCP servers from config",
            "`/update` — Update Hermes Agent to the latest version",
            "`/help` — Show this message",
        ]
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                lines.append(f"\n⚡ **Skill Commands** ({len(skill_cmds)} installed):")
                for cmd in sorted(skill_cmds):
                    lines.append(f"`{cmd}` — {skill_cmds[cmd]['description']}")
        except Exception:
            pass
        return "\n".join(lines)
    
    async def _handle_runtime_command(self, event: MessageEvent) -> str:
        """Handle /runtime command — show or switch agent runtime for this session."""
        args = event.get_command_args().strip().lower()
        session_id = self._get_session_id(event.source)
        current = self._resolve_runtime(session_id)

        _available = ["hermes", "claude-direct"]

        if not args:
            lines = [f"Current runtime: **{current}**", "", "Available runtimes:"]
            for rt in _available:
                marker = " ← active" if rt == current else ""
                lines.append(f"  `{rt}`{marker}")
            lines.append(f"\nSwitch with: `/runtime <name>`")
            return "\n".join(lines)

        if args not in _available:
            return f"Unknown runtime `{args}`. Available: {', '.join(_available)}"

        if args == "claude-direct":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                return "Claude Direct requires an Anthropic API key. Set `ANTHROPIC_API_KEY` in your environment."

        self._session_runtime_overrides[session_id] = args
        return f"Switched to **{args}** runtime for this session."

    async def _handle_model_command(self, event: MessageEvent) -> str:
        """Handle /model command - show or change the current model."""
        import yaml
        from logos_cli.models import (
            parse_model_input,
            validate_requested_model,
            curated_models_for_provider,
            normalize_provider,
            _PROVIDER_LABELS,
        )

        args = event.get_command_args().strip()
        config_path = _hermes_home / 'config.yaml'

        # Resolve current model and provider from config
        current = os.getenv("HERMES_MODEL") or "anthropic/claude-opus-4.6"
        current_provider = "openrouter"
        try:
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, str):
                    current = model_cfg
                elif isinstance(model_cfg, dict):
                    current = model_cfg.get("default", current)
                    current_provider = model_cfg.get("provider", current_provider)
        except Exception:
            pass

        # Resolve "auto" to the actual provider using credential detection
        current_provider = normalize_provider(current_provider)
        if current_provider == "auto":
            try:
                from logos_cli.auth import resolve_provider as _resolve_provider
                current_provider = _resolve_provider(current_provider)
            except Exception:
                current_provider = "openrouter"

        # Detect custom endpoint: provider resolved to openrouter but a custom
        # base URL is configured — the user set up a custom endpoint.
        if current_provider == "openrouter" and os.getenv("OPENAI_BASE_URL", "").strip():
            current_provider = "custom"

        if not args:
            provider_label = _PROVIDER_LABELS.get(current_provider, current_provider)
            lines = [
                f"🤖 **Current model:** `{current}`",
                f"**Provider:** {provider_label}",
                "",
            ]
            curated = curated_models_for_provider(current_provider)
            if curated:
                lines.append(f"**Available models ({provider_label}):**")
                for mid, desc in curated:
                    marker = " ←" if mid == current else ""
                    label = f"  _{desc}_" if desc else ""
                    lines.append(f"• `{mid}`{label}{marker}")
                lines.append("")
            lines.append("To change: `/model model-name`")
            lines.append("Switch provider: `/model provider:model-name`")
            return "\n".join(lines)

        # Parse provider:model syntax
        target_provider, new_model = parse_model_input(args, current_provider)
        provider_changed = target_provider != current_provider

        # Resolve credentials from gateway DB (cloud_providers + machines tables)
        # This mirrors the PATCH /api/model logic for consistent behavior.
        api_key = ""
        base_url = ""
        resolved_from_db = False
        try:
            from gateway.auth import db as _adb
            # Check cloud providers first
            cloud_provs = _adb.list_cloud_providers()
            for cp in cloud_provs:
                if cp.get("active_model") == new_model:
                    api_key = cp.get("api_key") or ""
                    base_url = cp.get("base_url") or ""
                    target_provider = cp.get("provider", target_provider)
                    resolved_from_db = True
                    break
            if not resolved_from_db:
                # Check local machines
                machines = _adb.list_machines()
                for m in machines:
                    if m.get("enabled") and m.get("endpoint_url"):
                        base_url = m["endpoint_url"].rstrip("/")
                        api_key = m.get("api_key") or "not-needed"
                        target_provider = "custom"
                        resolved_from_db = True
                        break
        except Exception:
            pass
        # Fall back to CLI-level resolution if DB lookup failed
        if not resolved_from_db:
            api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
            base_url = "https://openrouter.ai/api/v1"
            try:
                from logos_cli.runtime_provider import resolve_runtime_provider
                prov_to_resolve = target_provider if provider_changed else current_provider
                runtime = resolve_runtime_provider(requested=prov_to_resolve)
                api_key = runtime.get("api_key", "")
                base_url = runtime.get("base_url", "")
            except Exception as e:
                if provider_changed:
                    provider_label = _PROVIDER_LABELS.get(target_provider, target_provider)
                    return f"⚠️ Could not resolve credentials for provider '{provider_label}': {e}"

        # Validate the model against the live API
        try:
            validation = validate_requested_model(
                new_model,
                target_provider,
                api_key=api_key,
                base_url=base_url,
            )
        except Exception:
            validation = {"accepted": True, "persist": True, "recognized": False, "message": None}

        if not validation.get("accepted"):
            msg = validation.get("message", "Invalid model")
            tip = "\n\nUse `/model` to see available models, `/provider` to see providers" if "Did you mean" not in msg else ""
            return f"⚠️ {msg}{tip}"

        # Persist to config only if validation approves
        if validation.get("persist"):
            try:
                user_config = {}
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                if "model" not in user_config or not isinstance(user_config["model"], dict):
                    user_config["model"] = {}
                user_config["model"]["default"] = new_model
                if provider_changed:
                    user_config["model"]["provider"] = target_provider
                with open(config_path, 'w', encoding="utf-8") as f:
                    yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                return f"⚠️ Failed to save model change: {e}"

        # Set env vars so the next agent run picks up the change
        os.environ["HERMES_MODEL"] = new_model
        if provider_changed or resolved_from_db:
            os.environ["HERMES_INFERENCE_PROVIDER"] = target_provider
            # Also update the endpoint env vars so the OpenAI client connects
            # to the right backend (prevents local models → cloud API mismatch)
            if base_url:
                os.environ["OPENAI_BASE_URL"] = base_url
            if api_key:
                os.environ["OPENAI_API_KEY"] = api_key

        provider_label = _PROVIDER_LABELS.get(target_provider, target_provider)
        provider_note = f"\n**Provider:** {provider_label}" if provider_changed else ""

        warning = ""
        if validation.get("message"):
            warning = f"\n⚠️ {validation['message']}"

        if validation.get("persist"):
            persist_note = "saved to config"
        else:
            persist_note = "this session only — will revert on restart"
        return f"🤖 Model changed to `{new_model}` ({persist_note}){provider_note}{warning}\n_(takes effect on next message)_"

    async def _handle_provider_command(self, event: MessageEvent) -> str:
        """Handle /provider command - show available providers."""
        import yaml
        from logos_cli.models import (
            list_available_providers,
            normalize_provider,
            _PROVIDER_LABELS,
        )

        # Resolve current provider from config
        current_provider = "openrouter"
        config_path = _hermes_home / 'config.yaml'
        try:
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, dict):
                    current_provider = model_cfg.get("provider", current_provider)
        except Exception:
            pass

        current_provider = normalize_provider(current_provider)
        if current_provider == "auto":
            try:
                from logos_cli.auth import resolve_provider as _resolve_provider
                current_provider = _resolve_provider(current_provider)
            except Exception:
                current_provider = "openrouter"

        # Detect custom endpoint
        if current_provider == "openrouter" and os.getenv("OPENAI_BASE_URL", "").strip():
            current_provider = "custom"

        current_label = _PROVIDER_LABELS.get(current_provider, current_provider)

        lines = [
            f"🔌 **Current provider:** {current_label} (`{current_provider}`)",
            "",
            "**Available providers:**",
        ]

        providers = list_available_providers()
        for p in providers:
            marker = " ← active" if p["id"] == current_provider else ""
            auth = "✅" if p["authenticated"] else "❌"
            aliases = f"  _(also: {', '.join(p['aliases'])})_" if p["aliases"] else ""
            lines.append(f"{auth} `{p['id']}` — {p['label']}{aliases}{marker}")

        lines.append("")
        lines.append("Switch: `/model provider:model-name`")
        lines.append("Setup: `hermes setup`")
        return "\n".join(lines)
    
    async def _handle_personality_command(self, event: MessageEvent) -> str:
        """Handle /personality command - list or set a personality."""
        import yaml

        args = event.get_command_args().strip().lower()
        config_path = _hermes_home / 'config.yaml'

        try:
            if config_path.exists():
                with open(config_path, 'r', encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
                personalities = config.get("agent", {}).get("personalities", {})
            else:
                config = {}
                personalities = {}
        except Exception:
            config = {}
            personalities = {}

        if not personalities:
            return "No personalities configured in `~/.logos/config.yaml`"

        if not args:
            lines = ["🎭 **Available Personalities**\n"]
            lines.append("• `none` — (no personality overlay)")
            for name, prompt in personalities.items():
                if isinstance(prompt, dict):
                    preview = prompt.get("description") or prompt.get("system_prompt", "")[:50]
                else:
                    preview = prompt[:50] + "..." if len(prompt) > 50 else prompt
                lines.append(f"• `{name}` — {preview}")
            lines.append(f"\nUsage: `/personality <name>`")
            return "\n".join(lines)

        def _resolve_prompt(value):
            if isinstance(value, dict):
                parts = [value.get("system_prompt", "")]
                if value.get("tone"):
                    parts.append(f'Tone: {value["tone"]}')
                if value.get("style"):
                    parts.append(f'Style: {value["style"]}')
                return "\n".join(p for p in parts if p)
            return str(value)

        if args in ("none", "default", "neutral"):
            try:
                if "agent" not in config or not isinstance(config.get("agent"), dict):
                    config["agent"] = {}
                config["agent"]["system_prompt"] = ""
                with open(config_path, "w") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                return f"⚠️ Failed to save personality change: {e}"
            self._ephemeral_system_prompt = ""
            return "🎭 Personality cleared — using base agent behavior.\n_(takes effect on next message)_"
        elif args in personalities:
            new_prompt = _resolve_prompt(personalities[args])

            # Write to config.yaml, same pattern as CLI save_config_value.
            try:
                if "agent" not in config or not isinstance(config.get("agent"), dict):
                    config["agent"] = {}
                config["agent"]["system_prompt"] = new_prompt
                with open(config_path, 'w', encoding="utf-8") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                return f"⚠️ Failed to save personality change: {e}"

            # Update in-memory so it takes effect on the very next message.
            self._ephemeral_system_prompt = new_prompt

            return f"🎭 Personality set to **{args}**\n_(takes effect on next message)_"

        available = "`none`, " + ", ".join(f"`{n}`" for n in personalities.keys())
        return f"Unknown personality: `{args}`\n\nAvailable: {available}"
    
    async def _handle_retry_command(self, event: MessageEvent) -> str:
        """Handle /retry command - re-send the last user message."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)
        
        # Find the last user message
        last_user_msg = None
        last_user_idx = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                last_user_msg = history[i].get("content", "")
                last_user_idx = i
                break
        
        if not last_user_msg:
            return "No previous message to retry."
        
        # Truncate history to before the last user message and persist
        truncated = history[:last_user_idx]
        self.session_store.rewrite_transcript(session_entry.session_id, truncated)
        # Reset stored token count — transcript was truncated
        session_entry.last_prompt_tokens = 0
        
        # Re-send by creating a fake text event with the old message
        retry_event = MessageEvent(
            text=last_user_msg,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=event.raw_message,
        )

        # Re-dispatch through the sandbox-mediated platform path.
        # _handle_message was deleted in Phase 5.6 — dispatch_platform_message
        # is the only inbound entrypoint now.
        return await self.dispatch_platform_message(retry_event)
    
    async def _handle_undo_command(self, event: MessageEvent) -> str:
        """Handle /undo command - remove the last user/assistant exchange."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)
        
        # Find the last user message and remove everything from it onward
        last_user_idx = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                last_user_idx = i
                break
        
        if last_user_idx is None:
            return "Nothing to undo."
        
        removed_msg = history[last_user_idx].get("content", "")
        removed_count = len(history) - last_user_idx
        self.session_store.rewrite_transcript(session_entry.session_id, history[:last_user_idx])
        # Reset stored token count — transcript was truncated
        session_entry.last_prompt_tokens = 0
        
        preview = removed_msg[:40] + "..." if len(removed_msg) > 40 else removed_msg
        return f"↩️ Undid {removed_count} message(s).\nRemoved: \"{preview}\""
    
    async def _handle_set_home_command(self, event: MessageEvent) -> str:
        """Handle /sethome command -- set the current chat as the platform's home channel."""
        source = event.source
        platform_name = source.platform.value if source.platform else "unknown"
        chat_id = source.chat_id
        chat_name = source.chat_name or chat_id
        
        env_key = f"{platform_name.upper()}_HOME_CHANNEL"
        
        # Save to config.yaml
        try:
            import yaml
            config_path = _hermes_home / 'config.yaml'
            user_config = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
            user_config[env_key] = chat_id
            with open(config_path, 'w', encoding="utf-8") as f:
                yaml.dump(user_config, f, default_flow_style=False)
            # Also set in the current environment so it takes effect immediately
            os.environ[env_key] = str(chat_id)
        except Exception as e:
            return f"Failed to save home channel: {e}"
        
        return (
            f"✅ Home channel set to **{chat_name}** (ID: {chat_id}).\n"
            f"Cron jobs and cross-platform messages will be delivered here."
        )
    
    @staticmethod
    def _get_guild_id(event: MessageEvent) -> Optional[int]:
        """Extract Discord guild_id from the raw message object."""
        raw = getattr(event, "raw_message", None)
        if raw is None:
            return None
        # Slash command interaction
        if hasattr(raw, "guild_id") and raw.guild_id:
            return int(raw.guild_id)
        # Regular message
        if hasattr(raw, "guild") and raw.guild:
            return raw.guild.id
        return None

    async def _handle_voice_command(self, event: MessageEvent) -> str:
        """Handle /voice [on|off|tts|channel|leave|status] command."""
        args = event.get_command_args().strip().lower()
        chat_id = event.source.chat_id

        adapter = self.adapters.get(event.source.platform)

        if args in ("on", "enable"):
            self._voice_mode[chat_id] = "voice_only"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=False)
            return (
                "Voice mode enabled.\n"
                "I'll reply with voice when you send voice messages.\n"
                "Use /voice tts to get voice replies for all messages."
            )
        elif args in ("off", "disable"):
            self._voice_mode[chat_id] = "off"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
            return "Voice mode disabled. Text-only replies."
        elif args == "tts":
            self._voice_mode[chat_id] = "all"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=False)
            return (
                "Auto-TTS enabled.\n"
                "All replies will include a voice message."
            )
        elif args in ("channel", "join"):
            return await self._handle_voice_channel_join(event)
        elif args == "leave":
            return await self._handle_voice_channel_leave(event)
        elif args == "status":
            mode = self._voice_mode.get(chat_id, "off")
            labels = {
                "off": "Off (text only)",
                "voice_only": "On (voice reply to voice messages)",
                "all": "TTS (voice reply to all messages)",
            }
            # Append voice channel info if connected
            adapter = self.adapters.get(event.source.platform)
            guild_id = self._get_guild_id(event)
            if guild_id and hasattr(adapter, "get_voice_channel_info"):
                info = adapter.get_voice_channel_info(guild_id)
                if info:
                    lines = [
                        f"Voice mode: {labels.get(mode, mode)}",
                        f"Voice channel: #{info['channel_name']}",
                        f"Participants: {info['member_count']}",
                    ]
                    for m in info["members"]:
                        status = " (speaking)" if m.get("is_speaking") else ""
                        lines.append(f"  - {m['display_name']}{status}")
                    return "\n".join(lines)
            return f"Voice mode: {labels.get(mode, mode)}"
        else:
            # Toggle: off → on, on/all → off
            current = self._voice_mode.get(chat_id, "off")
            if current == "off":
                self._voice_mode[chat_id] = "voice_only"
                self._save_voice_modes()
                if adapter:
                    self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=False)
                return "Voice mode enabled."
            else:
                self._voice_mode[chat_id] = "off"
                self._save_voice_modes()
                if adapter:
                    self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
                return "Voice mode disabled."

    async def _handle_voice_channel_join(self, event: MessageEvent) -> str:
        """Join the user's current Discord voice channel."""
        adapter = self.adapters.get(event.source.platform)
        if not hasattr(adapter, "join_voice_channel"):
            return "Voice channels are not supported on this platform."

        guild_id = self._get_guild_id(event)
        if not guild_id:
            return "This command only works in a Discord server."

        voice_channel = await adapter.get_user_voice_channel(
            guild_id, event.source.user_id
        )
        if not voice_channel:
            return "You need to be in a voice channel first."

        # Wire callbacks BEFORE join so voice input arriving immediately
        # after connection is not lost.
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = self._handle_voice_channel_input
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = self._handle_voice_timeout_cleanup

        try:
            success = await adapter.join_voice_channel(voice_channel)
        except Exception as e:
            logger.warning("Failed to join voice channel: %s", e)
            adapter._voice_input_callback = None
            return f"Failed to join voice channel: {e}"

        if success:
            adapter._voice_text_channels[guild_id] = int(event.source.chat_id)
            self._voice_mode[event.source.chat_id] = "all"
            self._save_voice_modes()
            self._set_adapter_auto_tts_disabled(adapter, event.source.chat_id, disabled=False)
            return (
                f"Joined voice channel **{voice_channel.name}**.\n"
                f"I'll speak my replies and listen to you. Use /voice leave to disconnect."
            )
        # Join failed — clear callback
        adapter._voice_input_callback = None
        return "Failed to join voice channel. Check bot permissions (Connect + Speak)."

    async def _handle_voice_channel_leave(self, event: MessageEvent) -> str:
        """Leave the Discord voice channel."""
        adapter = self.adapters.get(event.source.platform)
        guild_id = self._get_guild_id(event)

        if not guild_id or not hasattr(adapter, "leave_voice_channel"):
            return "Not in a voice channel."

        if not hasattr(adapter, "is_in_voice_channel") or not adapter.is_in_voice_channel(guild_id):
            return "Not in a voice channel."

        try:
            await adapter.leave_voice_channel(guild_id)
        except Exception as e:
            logger.warning("Error leaving voice channel: %s", e)
        # Always clean up state even if leave raised an exception
        self._voice_mode[event.source.chat_id] = "off"
        self._save_voice_modes()
        self._set_adapter_auto_tts_disabled(adapter, event.source.chat_id, disabled=True)
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = None
        return "Left voice channel."

    def _handle_voice_timeout_cleanup(self, chat_id: str) -> None:
        """Called by the adapter when a voice channel times out.

        Cleans up runner-side voice_mode state that the adapter cannot reach.
        """
        self._voice_mode[chat_id] = "off"
        self._save_voice_modes()
        adapter = self.adapters.get(Platform.DISCORD)
        self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)

    async def _handle_voice_channel_input(
        self, guild_id: int, user_id: int, transcript: str
    ):
        """Handle transcribed voice from a user in a voice channel.

        Creates a synthetic MessageEvent and processes it through the
        adapter's full message pipeline (session, typing, agent, TTS reply).
        """
        adapter = self.adapters.get(Platform.DISCORD)
        if not adapter:
            return

        text_ch_id = adapter._voice_text_channels.get(guild_id)
        if not text_ch_id:
            return

        # Check authorization before processing voice input
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=str(text_ch_id),
            user_id=str(user_id),
            user_name=str(user_id),
            chat_type="channel",
        )
        if not self._is_user_authorized(source):
            logger.debug("Unauthorized voice input from user %d, ignoring", user_id)
            return

        # Show transcript in text channel (after auth, with mention sanitization)
        try:
            channel = adapter._client.get_channel(text_ch_id)
            if channel:
                safe_text = transcript[:2000].replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
                await channel.send(f"**[Voice]** <@{user_id}>: {safe_text}")
        except Exception:
            pass

        # Build a synthetic MessageEvent and feed through the normal pipeline
        # Use SimpleNamespace as raw_message so _get_guild_id() can extract
        # guild_id and _send_voice_reply() plays audio in the voice channel.
        from types import SimpleNamespace
        event = MessageEvent(
            source=source,
            text=transcript,
            message_type=MessageType.VOICE,
            raw_message=SimpleNamespace(guild_id=guild_id, guild=None),
        )

        await adapter.handle_message(event)

    def _should_send_voice_reply(
        self,
        event: MessageEvent,
        response: str,
        agent_messages: list,
    ) -> bool:
        """Decide whether the runner should send a TTS voice reply.

        Returns False when:
        - voice_mode is off for this chat
        - response is empty or an error
        - agent already called text_to_speech tool (dedup)
        - voice input and base adapter auto-TTS already handled it (skip_double)
          Exception: Discord voice channel — base play_tts is a no-op there,
          so the runner must handle VC playback.
        """
        if not response or response.startswith("Error:"):
            return False

        chat_id = event.source.chat_id
        voice_mode = self._voice_mode.get(chat_id, "off")
        is_voice_input = (event.message_type == MessageType.VOICE)

        should = (
            (voice_mode == "all")
            or (voice_mode == "voice_only" and is_voice_input)
        )
        if not should:
            return False

        # Dedup: agent already called TTS tool
        has_agent_tts = any(
            msg.get("role") == "assistant"
            and any(
                tc.get("function", {}).get("name") == "text_to_speech"
                for tc in (msg.get("tool_calls") or [])
            )
            for msg in agent_messages
        )
        if has_agent_tts:
            return False

        # Dedup: base adapter auto-TTS already handles voice input.
        # Exception: Discord voice channel — play_tts override is a no-op,
        # so the runner must handle VC playback.
        skip_double = is_voice_input
        if skip_double:
            adapter = self.adapters.get(event.source.platform)
            guild_id = self._get_guild_id(event)
            if (guild_id and adapter
                    and hasattr(adapter, "is_in_voice_channel")
                    and adapter.is_in_voice_channel(guild_id)):
                skip_double = False
        if skip_double:
            return False

        return True

    async def _send_voice_reply(self, event: MessageEvent, text: str) -> None:
        """Generate TTS audio and send as a voice message before the text reply."""
        import uuid as _uuid
        audio_path = None
        actual_path = None
        try:
            from tools.tts_tool import text_to_speech_tool, _strip_markdown_for_tts

            tts_text = _strip_markdown_for_tts(text[:4000])
            if not tts_text:
                return

            # Use .mp3 extension so edge-tts conversion to opus works correctly.
            # The TTS tool may convert to .ogg — use file_path from result.
            audio_path = os.path.join(
                tempfile.gettempdir(), "hermes_voice",
                f"tts_reply_{_uuid.uuid4().hex[:12]}.mp3",
            )
            os.makedirs(os.path.dirname(audio_path), exist_ok=True)

            result_json = await asyncio.to_thread(
                text_to_speech_tool, text=tts_text, output_path=audio_path
            )
            result = json.loads(result_json)

            # Use the actual file path from result (may differ after opus conversion)
            actual_path = result.get("file_path", audio_path)
            if not result.get("success") or not os.path.isfile(actual_path):
                logger.warning("Auto voice reply TTS failed: %s", result.get("error"))
                return

            adapter = self.adapters.get(event.source.platform)

            # If connected to a voice channel, play there instead of sending a file
            guild_id = self._get_guild_id(event)
            if (guild_id
                    and hasattr(adapter, "play_in_voice_channel")
                    and hasattr(adapter, "is_in_voice_channel")
                    and adapter.is_in_voice_channel(guild_id)):
                await adapter.play_in_voice_channel(guild_id, actual_path)
            elif adapter and hasattr(adapter, "send_voice"):
                send_kwargs: Dict[str, Any] = {
                    "chat_id": event.source.chat_id,
                    "audio_path": actual_path,
                    "reply_to": event.message_id,
                }
                if event.source.thread_id:
                    send_kwargs["metadata"] = {"thread_id": event.source.thread_id}
                await adapter.send_voice(**send_kwargs)
        except Exception as e:
            logger.warning("Auto voice reply failed: %s", e, exc_info=True)
        finally:
            for p in {audio_path, actual_path} - {None}:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    async def _handle_rollback_command(self, event: MessageEvent) -> str:
        """Handle /rollback command — list or restore filesystem checkpoints."""
        from tools.checkpoint_manager import CheckpointManager, format_checkpoint_list

        # Read checkpoint config from config.yaml
        cp_cfg = {}
        try:
            import yaml as _y
            _cfg_path = _hermes_home / "config.yaml"
            if _cfg_path.exists():
                with open(_cfg_path, encoding="utf-8") as _f:
                    _data = _y.safe_load(_f) or {}
                cp_cfg = _data.get("checkpoints", {})
                if isinstance(cp_cfg, bool):
                    cp_cfg = {"enabled": cp_cfg}
        except Exception:
            pass

        if not cp_cfg.get("enabled", False):
            return (
                "Checkpoints are not enabled.\n"
                "Enable in config.yaml:\n```\ncheckpoints:\n  enabled: true\n```"
            )

        mgr = CheckpointManager(
            enabled=True,
            max_snapshots=cp_cfg.get("max_snapshots", 50),
        )

        cwd = os.getenv("MESSAGING_CWD", str(Path.home()))
        arg = event.get_command_args().strip()

        if not arg:
            checkpoints = mgr.list_checkpoints(cwd)
            return format_checkpoint_list(checkpoints, cwd)

        # Restore by number or hash
        checkpoints = mgr.list_checkpoints(cwd)
        if not checkpoints:
            return f"No checkpoints found for {cwd}"

        target_hash = None
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(checkpoints):
                target_hash = checkpoints[idx]["hash"]
            else:
                return f"Invalid checkpoint number. Use 1-{len(checkpoints)}."
        except ValueError:
            target_hash = arg

        result = mgr.restore(cwd, target_hash)
        if result["success"]:
            return (
                f"✅ Restored to checkpoint {result['restored_to']}: {result['reason']}\n"
                f"A pre-rollback snapshot was saved automatically."
            )
        return f"❌ {result['error']}"

    async def _handle_reasoning_command(self, event: MessageEvent) -> str:
        """Handle /reasoning command — manage reasoning effort and display toggle.

        Usage:
            /reasoning              Show current effort level and display state
            /reasoning <level>      Set reasoning effort (none, low, medium, high, xhigh)
            /reasoning show|on      Show model reasoning in responses
            /reasoning hide|off     Hide model reasoning from responses
        """
        import yaml

        args = event.get_command_args().strip().lower()
        config_path = _hermes_home / "config.yaml"
        self._reasoning_config = self._load_reasoning_config()
        self._show_reasoning = self._load_show_reasoning()

        def _save_config_key(key_path: str, value):
            """Save a dot-separated key to config.yaml."""
            try:
                user_config = {}
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                keys = key_path.split(".")
                current = user_config
                for k in keys[:-1]:
                    if k not in current or not isinstance(current[k], dict):
                        current[k] = {}
                    current = current[k]
                current[keys[-1]] = value
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(user_config, f, default_flow_style=False, sort_keys=False)
                return True
            except Exception as e:
                logger.error("Failed to save config key %s: %s", key_path, e)
                return False

        if not args:
            # Show current state
            rc = self._reasoning_config
            if rc is None:
                level = "medium (default)"
            elif rc.get("enabled") is False:
                level = "none (disabled)"
            else:
                level = rc.get("effort", "medium")
            display_state = "on ✓" if self._show_reasoning else "off"
            return (
                "🧠 **Reasoning Settings**\n\n"
                f"**Effort:** `{level}`\n"
                f"**Display:** {display_state}\n\n"
                "_Usage:_ `/reasoning <none|low|medium|high|xhigh|show|hide>`"
            )

        # Display toggle
        if args in ("show", "on"):
            self._show_reasoning = True
            _save_config_key("display.show_reasoning", True)
            return "🧠 ✓ Reasoning display: **ON**\nModel thinking will be shown before each response."

        if args in ("hide", "off"):
            self._show_reasoning = False
            _save_config_key("display.show_reasoning", False)
            return "🧠 ✓ Reasoning display: **OFF**"

        # Effort level change
        effort = args.strip()
        if effort == "none":
            parsed = {"enabled": False}
        elif effort in ("xhigh", "high", "medium", "low", "minimal"):
            parsed = {"enabled": True, "effort": effort}
        else:
            return (
                f"⚠️ Unknown argument: `{effort}`\n\n"
                "**Valid levels:** none, low, minimal, medium, high, xhigh\n"
                "**Display:** show, hide"
            )

        self._reasoning_config = parsed
        if _save_config_key("agent.reasoning_effort", effort):
            return f"🧠 ✓ Reasoning effort set to `{effort}` (saved to config)\n_(takes effect on next message)_"
        else:
            return f"🧠 ✓ Reasoning effort set to `{effort}` (this session only)"

    async def _handle_title_command(self, event: MessageEvent) -> str:
        """Handle /title command — set or show the current session's title."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_id = session_entry.session_id

        if not self._session_db:
            return "Session database not available."

        title_arg = event.get_command_args().strip()
        if title_arg:
            # Sanitize the title before setting
            try:
                sanitized = self._session_db.sanitize_title(title_arg)
            except ValueError as e:
                return f"⚠️ {e}"
            if not sanitized:
                return "⚠️ Title is empty after cleanup. Please use printable characters."
            # Set the title
            try:
                if self._session_db.set_session_title(session_id, sanitized):
                    return f"✏️ Session title set: **{sanitized}**"
                else:
                    return "Session not found in database."
            except ValueError as e:
                return f"⚠️ {e}"
        else:
            # Show the current title
            title = self._session_db.get_session_title(session_id)
            if title:
                return f"📌 Session title: **{title}**"
            else:
                return "No title set. Usage: `/title My Session Name`"

    async def _handle_resume_command(self, event: MessageEvent) -> str:
        """Handle /resume command — switch to a previously-named session."""
        if not self._session_db:
            return "Session database not available."

        source = event.source
        session_key = build_session_key(source)
        name = event.get_command_args().strip()

        if not name:
            # List recent titled sessions for this user/platform
            try:
                user_source = source.platform.value if source.platform else None
                sessions = self._session_db.list_sessions_rich(
                    source=user_source, limit=10
                )
                titled = [s for s in sessions if s.get("title")]
                if not titled:
                    return (
                        "No named sessions found.\n"
                        "Use `/title My Session` to name your current session, "
                        "then `/resume My Session` to return to it later."
                    )
                lines = ["📋 **Named Sessions**\n"]
                for s in titled[:10]:
                    title = s["title"]
                    preview = s.get("preview", "")[:40]
                    preview_part = f" — _{preview}_" if preview else ""
                    lines.append(f"• **{title}**{preview_part}")
                lines.append("\nUsage: `/resume <session name>`")
                return "\n".join(lines)
            except Exception as e:
                logger.debug("Failed to list titled sessions: %s", e)
                return f"Could not list sessions: {e}"

        # Resolve the name to a session ID
        target_id = self._session_db.resolve_session_by_title(name)
        if not target_id:
            return (
                f"No session found matching '**{name}**'.\n"
                "Use `/resume` with no arguments to see available sessions."
            )

        # Check if already on that session
        current_entry = self.session_store.get_or_create_session(source)
        if current_entry.session_id == target_id:
            return f"📌 Already on session **{name}**."

        # Clear any running agent for this session key
        if session_key in self._running_agents:
            del self._running_agents[session_key]

        # Switch the session entry to point at the old session
        new_entry = self.session_store.switch_session(session_key, target_id)
        if not new_entry:
            return "Failed to switch session."

        # Get the title for confirmation
        title = self._session_db.get_session_title(target_id) or name

        # Count messages for context
        history = self.session_store.load_transcript(target_id)
        msg_count = len([m for m in history if m.get("role") == "user"]) if history else 0
        msg_part = f" ({msg_count} message{'s' if msg_count != 1 else ''})" if msg_count else ""

        return f"↻ Resumed session **{title}**{msg_part}. Conversation restored."

    async def _handle_usage_command(self, event: MessageEvent) -> str:
        """Handle /usage command -- show token usage for the session's last agent run."""
        source = event.source
        session_key = build_session_key(source)

        agent = self._running_agents.get(session_key)
        if agent and hasattr(agent, "session_total_tokens") and agent.session_api_calls > 0:
            lines = [
                "📊 **Session Token Usage**",
                f"Prompt (input): {agent.session_prompt_tokens:,}",
                f"Completion (output): {agent.session_completion_tokens:,}",
                f"Total: {agent.session_total_tokens:,}",
                f"API calls: {agent.session_api_calls}",
            ]
            ctx = agent.context_compressor
            if ctx.last_prompt_tokens:
                pct = ctx.last_prompt_tokens / ctx.context_length * 100 if ctx.context_length else 0
                lines.append(f"Context: {ctx.last_prompt_tokens:,} / {ctx.context_length:,} ({pct:.0f}%)")
            if ctx.compression_count:
                lines.append(f"Compressions: {ctx.compression_count}")
            return "\n".join(lines)

        # No running agent -- check session history for a rough count
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)
        if history:
            from agent.model_metadata import estimate_messages_tokens_rough
            msgs = [m for m in history if m.get("role") in ("user", "assistant") and m.get("content")]
            approx = estimate_messages_tokens_rough(msgs)
            return (
                f"📊 **Session Info**\n"
                f"Messages: {len(msgs)}\n"
                f"Estimated context: ~{approx:,} tokens\n"
                f"_(Detailed usage available during active conversations)_"
            )
        return "No usage data available for this session."

    async def _handle_insights_command(self, event: MessageEvent) -> str:
        """Handle /insights command -- show usage insights and analytics."""
        import asyncio as _asyncio

        args = event.get_command_args().strip()
        days = 30
        source = None

        # Parse simple args: /insights 7  or  /insights --days 7
        if args:
            parts = args.split()
            i = 0
            while i < len(parts):
                if parts[i] == "--days" and i + 1 < len(parts):
                    try:
                        days = int(parts[i + 1])
                    except ValueError:
                        return f"Invalid --days value: {parts[i + 1]}"
                    i += 2
                elif parts[i] == "--source" and i + 1 < len(parts):
                    source = parts[i + 1]
                    i += 2
                elif parts[i].isdigit():
                    days = int(parts[i])
                    i += 1
                else:
                    i += 1

        try:
            from core.state import SessionDB
            from agent.insights import InsightsEngine

            loop = _asyncio.get_event_loop()

            def _run_insights():
                db = SessionDB()
                engine = InsightsEngine(db)
                report = engine.generate(days=days, source=source)
                result = engine.format_gateway(report)
                db.close()
                return result

            return await loop.run_in_executor(None, _run_insights)
        except Exception as e:
            logger.error("Insights command error: %s", e, exc_info=True)
            return f"Error generating insights: {e}"

    async def _handle_reload_mcp_command(self, event: MessageEvent) -> str:
        """Handle /reload-mcp command -- disconnect and reconnect all MCP servers."""
        loop = asyncio.get_event_loop()
        try:
            from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, _load_mcp_config, _servers, _lock

            # Capture old server names before shutdown
            with _lock:
                old_servers = set(_servers.keys())

            # Read new config before shutting down, so we know what will be added/removed
            new_config = _load_mcp_config()
            new_server_names = set(new_config.keys())

            # Shutdown existing connections
            await loop.run_in_executor(None, shutdown_mcp_servers)

            # Reconnect by discovering tools (reads config.yaml fresh)
            new_tools = await loop.run_in_executor(None, discover_mcp_tools)

            # Compute what changed
            with _lock:
                connected_servers = set(_servers.keys())

            added = connected_servers - old_servers
            removed = old_servers - connected_servers
            reconnected = connected_servers & old_servers

            lines = ["🔄 **MCP Servers Reloaded**\n"]
            if reconnected:
                lines.append(f"♻️ Reconnected: {', '.join(sorted(reconnected))}")
            if added:
                lines.append(f"➕ Added: {', '.join(sorted(added))}")
            if removed:
                lines.append(f"➖ Removed: {', '.join(sorted(removed))}")
            if not connected_servers:
                lines.append("No MCP servers connected.")
            else:
                lines.append(f"\n🔧 {len(new_tools)} tool(s) available from {len(connected_servers)} server(s)")

            # Inject a message at the END of the session history so the
            # model knows tools changed on its next turn.  Appended after
            # all existing messages to preserve prompt-cache for the prefix.
            change_parts = []
            if added:
                change_parts.append(f"Added servers: {', '.join(sorted(added))}")
            if removed:
                change_parts.append(f"Removed servers: {', '.join(sorted(removed))}")
            if reconnected:
                change_parts.append(f"Reconnected servers: {', '.join(sorted(reconnected))}")
            tool_summary = f"{len(new_tools)} MCP tool(s) now available" if new_tools else "No MCP tools available"
            change_detail = ". ".join(change_parts) + ". " if change_parts else ""
            reload_msg = {
                "role": "user",
                "content": f"[SYSTEM: MCP servers have been reloaded. {change_detail}{tool_summary}. The tool list for this conversation has been updated accordingly.]",
            }
            try:
                session_entry = self.session_store.get_or_create_session(event.source)
                self.session_store.append_to_transcript(
                    session_entry.session_id, reload_msg
                )
            except Exception:
                pass  # Best-effort; don't fail the reload over a transcript write

            return "\n".join(lines)

        except Exception as e:
            logger.warning("MCP reload failed: %s", e)
            return f"❌ MCP reload failed: {e}"

    async def _handle_update_command(self, event: MessageEvent) -> str:
        """Handle /update command — update Hermes Agent to the latest version.

        Spawns ``hermes update`` in a separate systemd scope so it survives the
        gateway restart that ``hermes update`` may trigger at the end. Marker
        files are written so either the current gateway process or the next one
        can notify the user when the update finishes.
        """
        import json
        import shutil
        import subprocess
        from datetime import datetime

        project_root = Path(__file__).parent.parent.resolve()
        git_dir = project_root / '.git'

        if not git_dir.exists():
            return "✗ Not a git repository — cannot update."

        hermes_bin = shutil.which("hermes")
        if not hermes_bin:
            return "✗ `hermes` command not found on PATH."

        pending_path = _hermes_home / ".update_pending.json"
        output_path = _hermes_home / ".update_output.txt"
        exit_code_path = _hermes_home / ".update_exit_code"
        pending = {
            "platform": event.source.platform.value,
            "chat_id": event.source.chat_id,
            "user_id": event.source.user_id,
            "timestamp": datetime.now().isoformat(),
        }
        pending_path.write_text(json.dumps(pending))
        exit_code_path.unlink(missing_ok=True)

        # Spawn `hermes update` in a separate cgroup so it survives gateway
        # restart. systemd-run --user --scope creates a transient scope unit.
        update_cmd = (
            f"{shlex.quote(hermes_bin)} update > {shlex.quote(str(output_path))} 2>&1; "
            f"status=$?; printf '%s' \"$status\" > {shlex.quote(str(exit_code_path))}"
        )
        try:
            systemd_run = shutil.which("systemd-run")
            if systemd_run:
                subprocess.Popen(
                    [systemd_run, "--user", "--scope",
                     "--unit=hermes-update", "--",
                     "bash", "-c", update_cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            else:
                # Fallback: best-effort detach with start_new_session
                subprocess.Popen(
                    ["bash", "-c", f"nohup {update_cmd} &"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except Exception as e:
            pending_path.unlink(missing_ok=True)
            exit_code_path.unlink(missing_ok=True)
            return f"✗ Failed to start update: {e}"

        self._schedule_update_notification_watch()
        return "⚕ Starting Hermes update… I'll notify you when it's done."

    def _schedule_update_notification_watch(self) -> None:
        """Ensure a background task is watching for update completion."""
        existing_task = getattr(self, "_update_notification_task", None)
        if existing_task and not existing_task.done():
            return

        try:
            self._update_notification_task = asyncio.create_task(
                self._watch_for_update_completion()
            )
        except RuntimeError:
            logger.debug("Skipping update notification watcher: no running event loop")

    async def _watch_for_update_completion(
        self,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> None:
        """Wait for ``hermes update`` to finish, then send its notification."""
        pending_path = _hermes_home / ".update_pending.json"
        claimed_path = _hermes_home / ".update_pending.claimed.json"
        exit_code_path = _hermes_home / ".update_exit_code"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while (pending_path.exists() or claimed_path.exists()) and loop.time() < deadline:
            if exit_code_path.exists():
                await self._send_update_notification()
                return
            await asyncio.sleep(poll_interval)

        if (pending_path.exists() or claimed_path.exists()) and not exit_code_path.exists():
            logger.warning("Update watcher timed out waiting for completion marker")
            exit_code_path.write_text("124")
            await self._send_update_notification()

    async def _send_update_notification(self) -> bool:
        """If an update finished, notify the user.

        Returns False when the update is still running so a caller can retry
        later. Returns True after a definitive send/skip decision.
        """
        import json
        import re as _re

        pending_path = _hermes_home / ".update_pending.json"
        claimed_path = _hermes_home / ".update_pending.claimed.json"
        output_path = _hermes_home / ".update_output.txt"
        exit_code_path = _hermes_home / ".update_exit_code"

        if not pending_path.exists() and not claimed_path.exists():
            return False

        cleanup = True
        active_pending_path = claimed_path
        try:
            if pending_path.exists():
                try:
                    pending_path.replace(claimed_path)
                except FileNotFoundError:
                    if not claimed_path.exists():
                        return True
            elif not claimed_path.exists():
                return True

            pending = json.loads(claimed_path.read_text())
            platform_str = pending.get("platform")
            chat_id = pending.get("chat_id")

            if not exit_code_path.exists():
                logger.info("Update notification deferred: update still running")
                cleanup = False
                active_pending_path = pending_path
                claimed_path.replace(pending_path)
                return False

            exit_code_raw = exit_code_path.read_text().strip() or "1"
            exit_code = int(exit_code_raw)

            # Read the captured update output
            output = ""
            if output_path.exists():
                output = output_path.read_text()

            # Resolve adapter
            platform = Platform(platform_str)
            adapter = self.adapters.get(platform)

            if adapter and chat_id:
                # Strip ANSI escape codes for clean display
                output = _re.sub(r'\x1b\[[0-9;]*m', '', output).strip()
                if output:
                    if len(output) > 3500:
                        output = "…" + output[-3500:]
                    if exit_code == 0:
                        msg = f"✅ Hermes update finished.\n\n```\n{output}\n```"
                    else:
                        msg = f"❌ Hermes update failed.\n\n```\n{output}\n```"
                else:
                    if exit_code == 0:
                        msg = "✅ Hermes update finished successfully."
                    else:
                        msg = "❌ Hermes update failed. Check the gateway logs or run `hermes update` manually for details."
                await adapter.send(chat_id, msg)
                logger.info(
                    "Sent post-update notification to %s:%s (exit=%s)",
                    platform_str,
                    chat_id,
                    exit_code,
                )
        except Exception as e:
            logger.warning("Post-update notification failed: %s", e)
        finally:
            if cleanup:
                active_pending_path.unlink(missing_ok=True)
                claimed_path.unlink(missing_ok=True)
                output_path.unlink(missing_ok=True)
                exit_code_path.unlink(missing_ok=True)

        return True

    def _set_session_env(self, context: SessionContext) -> None:
        """Set environment variables for the current session."""
        os.environ["HERMES_SESSION_PLATFORM"] = context.source.platform.value
        os.environ["HERMES_SESSION_CHAT_ID"] = context.source.chat_id
        if context.source.chat_name:
            os.environ["HERMES_SESSION_CHAT_NAME"] = context.source.chat_name
    
    def _clear_session_env(self) -> None:
        """Clear session environment variables."""
        for var in ["HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_NAME"]:
            if var in os.environ:
                del os.environ[var]
    
    async def _enrich_message_with_attachments(
        self,
        message_text: str,
        media_urls: List[str],
        media_types: List[str],
    ) -> str:
        """Enrich a user message with auto-analyzed images, transcribed audio,
        and document context notes.

        Shared enrichment pipeline used by the HTTP /chat endpoint.
        (Platform adapters used to call this via _handle_message before
        Phase 5.6; the platform path now goes through
        dispatch_platform_message which doesn't yet enrich attachments —
        a follow-up will port this when needed.)

        Args:
            message_text: The original user message text.
            media_urls: List of cached file paths for attachments.
            media_types: Parallel list of MIME types for each attachment.

        Returns:
            The enriched message text with descriptions/transcriptions prepended.
        """
        if not media_urls:
            return message_text

        # --- Images: auto-analyze with vision tool ---
        image_paths = []
        for i, path in enumerate(media_urls):
            mtype = media_types[i] if i < len(media_types) else ""
            if mtype.startswith("image/"):
                image_paths.append(path)
        if image_paths:
            message_text = await self._enrich_message_with_vision(
                message_text, image_paths
            )

        # --- Audio: auto-transcribe ---
        audio_paths = []
        for i, path in enumerate(media_urls):
            mtype = media_types[i] if i < len(media_types) else ""
            if mtype.startswith("audio/"):
                audio_paths.append(path)
        if audio_paths:
            message_text = await self._enrich_message_with_transcription(
                message_text, audio_paths
            )

        # --- Documents and code files: inject context notes ---
        for i, path in enumerate(media_urls):
            mtype = media_types[i] if i < len(media_types) else ""
            if mtype.startswith("image/") or mtype.startswith("audio/"):
                continue  # already handled above
            if not (mtype.startswith("application/") or mtype.startswith("text/")):
                continue
            basename = os.path.basename(path)
            # Strip doc_{uuid12}_ prefix from cached filenames
            parts = basename.split("_", 2)
            display_name = parts[2] if len(parts) >= 3 else basename
            display_name = re.sub(r'[^\w.\- ]', '_', display_name)

            if mtype.startswith("text/") or mtype == "application/json":
                # Read and inject text content (capped at 8000 chars)
                try:
                    content = open(path, "r", errors="replace").read()
                    if len(content) > 8000:
                        content = content[:8000] + "\n\n[... truncated — full file at: " + path + "]"
                    context_note = (
                        f"[The user attached a file: '{display_name}'. "
                        f"Its content is below. Full file at: {path}]\n\n{content}"
                    )
                except Exception:
                    context_note = (
                        f"[The user attached a file: '{display_name}'. "
                        f"The file is saved at: {path}]"
                    )
            else:
                context_note = (
                    f"[The user attached a document: '{display_name}'. "
                    f"The file is saved at: {path}. "
                    f"Use appropriate tools to read or analyze it.]"
                )
            message_text = f"{context_note}\n\n{message_text}"

        return message_text

    async def _enrich_message_with_vision(
        self,
        user_text: str,
        image_paths: List[str],
    ) -> str:
        """
        Auto-analyze user-attached images with the vision tool and prepend
        the descriptions to the message text.

        Each image is analyzed with a general-purpose prompt.  The resulting
        description *and* the local cache path are injected so the model can:
          1. Immediately understand what the user sent (no extra tool call).
          2. Re-examine the image with vision_analyze if it needs more detail.

        Args:
            user_text:   The user's original caption / message text.
            image_paths: List of local file paths to cached images.

        Returns:
            The enriched message string with vision descriptions prepended.
        """
        from tools.vision_tools import vision_analyze_tool
        import json as _json

        analysis_prompt = (
            "Describe everything visible in this image in thorough detail. "
            "Include any text, code, data, objects, people, layout, colors, "
            "and any other notable visual information."
        )

        enriched_parts = []
        for path in image_paths:
            try:
                logger.debug("Auto-analyzing user image: %s", path)
                result_json = await vision_analyze_tool(
                    image_url=path,
                    user_prompt=analysis_prompt,
                )
                result = _json.loads(result_json)
                if result.get("success"):
                    description = result.get("analysis", "")
                    enriched_parts.append(
                        f"[The user sent an image~ Here's what I can see:\n{description}]\n"
                        f"[If you need a closer look, use vision_analyze with "
                        f"image_url: {path} ~]"
                    )
                else:
                    enriched_parts.append(
                        "[The user sent an image but I couldn't quite see it "
                        "this time (>_<) You can try looking at it yourself "
                        f"with vision_analyze using image_url: {path}]"
                    )
            except Exception as e:
                logger.error("Vision auto-analysis error: %s", e)
                enriched_parts.append(
                    f"[The user sent an image but something went wrong when I "
                    f"tried to look at it~ You can try examining it yourself "
                    f"with vision_analyze using image_url: {path}]"
                )

        # Combine: vision descriptions first, then the user's original text
        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            if user_text:
                return f"{prefix}\n\n{user_text}"
            return prefix
        return user_text

    async def _enrich_message_with_transcription(
        self,
        user_text: str,
        audio_paths: List[str],
    ) -> str:
        """
        Auto-transcribe user voice/audio messages using OpenAI Whisper API
        and prepend the transcript to the message text.

        Args:
            user_text:   The user's original caption / message text.
            audio_paths: List of local file paths to cached audio files.

        Returns:
            The enriched message string with transcriptions prepended.
        """
        from tools.transcription_tools import transcribe_audio, get_stt_model_from_config
        import asyncio

        stt_model = get_stt_model_from_config()

        enriched_parts = []
        for path in audio_paths:
            try:
                logger.debug("Transcribing user voice: %s", path)
                result = await asyncio.to_thread(transcribe_audio, path, model=stt_model)
                if result["success"]:
                    transcript = result["transcript"]
                    enriched_parts.append(
                        f'[The user sent a voice message~ '
                        f'Here\'s what they said: "{transcript}"]'
                    )
                else:
                    error = result.get("error", "unknown error")
                    if "No STT provider" in error or "not set" in error:
                        enriched_parts.append(
                            "[The user sent a voice message but I can't listen "
                            "to it right now~ No STT provider is configured "
                            "(';w;') Let them know!]"
                        )
                    else:
                        enriched_parts.append(
                            "[The user sent a voice message but I had trouble "
                            f"transcribing it~ ({error})]"
                        )
            except Exception as e:
                logger.error("Transcription error: %s", e)
                enriched_parts.append(
                    "[The user sent a voice message but something went wrong "
                    "when I tried to listen to it~ Let them know!]"
                )

        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            if user_text:
                return f"{prefix}\n\n{user_text}"
            return prefix
        return user_text

    async def _run_process_watcher(self, watcher: dict) -> None:
        """
        Periodically check a background process and push updates to the user.

        Runs as an asyncio task. Stays silent when nothing changed.
        Auto-removes when the process exits or is killed.

        Notification mode (from ``display.background_process_notifications``):
          - ``all``    — running-output updates + final message
          - ``result`` — final completion message only
          - ``error``  — final message only when exit code != 0
          - ``off``    — no messages at all
        """
        from tools.process_registry import process_registry

        session_id = watcher["session_id"]
        interval = watcher["check_interval"]
        session_key = watcher.get("session_key", "")
        platform_name = watcher.get("platform", "")
        chat_id = watcher.get("chat_id", "")
        notify_mode = self._load_background_notifications_mode()

        logger.debug("Process watcher started: %s (every %ss, notify=%s)",
                      session_id, interval, notify_mode)

        if notify_mode == "off":
            # Still wait for the process to exit so we can log it, but don't
            # push any messages to the user.
            while True:
                await asyncio.sleep(interval)
                session = process_registry.get(session_id)
                if session is None or session.exited:
                    break
            logger.debug("Process watcher ended (silent): %s", session_id)
            return

        last_output_len = 0
        while True:
            await asyncio.sleep(interval)

            session = process_registry.get(session_id)
            if session is None:
                break

            current_output_len = len(session.output_buffer)
            has_new_output = current_output_len > last_output_len
            last_output_len = current_output_len

            if session.exited:
                # Decide whether to notify based on mode
                should_notify = (
                    notify_mode in ("all", "result")
                    or (notify_mode == "error" and session.exit_code not in (0, None))
                )
                if should_notify:
                    new_output = session.output_buffer[-1000:] if session.output_buffer else ""
                    message_text = (
                        f"[Background process {session_id} finished with exit code {session.exit_code}~ "
                        f"Here's the final output:\n{new_output}]"
                    )
                    adapter = None
                    for p, a in self.adapters.items():
                        if p.value == platform_name:
                            adapter = a
                            break
                    if adapter and chat_id:
                        try:
                            await adapter.send(chat_id, message_text)
                        except Exception as e:
                            logger.error("Watcher delivery error: %s", e)
                break

            elif has_new_output and notify_mode == "all":
                # New output available -- deliver status update (only in "all" mode)
                new_output = session.output_buffer[-500:] if session.output_buffer else ""
                message_text = (
                    f"[Background process {session_id} is still running~ "
                    f"New output:\n{new_output}]"
                )
                adapter = None
                for p, a in self.adapters.items():
                    if p.value == platform_name:
                        adapter = a
                        break
                if adapter and chat_id:
                    try:
                        await adapter.send(chat_id, message_text)
                    except Exception as e:
                        logger.error("Watcher delivery error: %s", e)

        logger.debug("Process watcher ended: %s", session_id)



def _start_cron_ticker(stop_event: threading.Event, adapters=None, interval: int = 60):
    """
    Background thread that ticks the cron scheduler at a regular interval.
    
    Runs inside the gateway process so cronjobs fire automatically without
    needing a separate `hermes cron daemon` or system cron entry.

    Also refreshes the channel directory every 5 minutes and prunes the
    image/audio/document cache once per hour.
    """
    from cron.scheduler import tick as cron_tick
    from gateway.channels.base import cleanup_image_cache, cleanup_document_cache

    IMAGE_CACHE_EVERY = 60   # ticks — once per hour at default 60s interval
    CHANNEL_DIR_EVERY = 5    # ticks — every 5 minutes

    logger.info("Cron ticker started (interval=%ds)", interval)
    tick_count = 0
    while not stop_event.is_set():
        try:
            cron_tick(verbose=False)
        except Exception as e:
            logger.debug("Cron tick error: %s", e)

        tick_count += 1

        if tick_count % CHANNEL_DIR_EVERY == 0 and adapters:
            try:
                from gateway.channel_directory import build_channel_directory
                build_channel_directory(adapters)
            except Exception as e:
                logger.debug("Channel directory refresh error: %s", e)

        if tick_count % IMAGE_CACHE_EVERY == 0:
            try:
                removed = cleanup_image_cache(max_age_hours=24)
                if removed:
                    logger.info("Image cache cleanup: removed %d stale file(s)", removed)
            except Exception as e:
                logger.debug("Image cache cleanup error: %s", e)
            try:
                removed = cleanup_document_cache(max_age_hours=24)
                if removed:
                    logger.info("Document cache cleanup: removed %d stale file(s)", removed)
            except Exception as e:
                logger.debug("Document cache cleanup error: %s", e)

        stop_event.wait(timeout=interval)
    logger.info("Cron ticker stopped")


async def start_gateway(config: Optional[GatewayConfig] = None, replace: bool = False) -> bool:
    """
    Start the gateway and run until interrupted.
    
    This is the main entry point for running the gateway.
    Returns True if the gateway ran successfully, False if it failed to start.
    A False return causes a non-zero exit code so systemd can auto-restart.
    
    Args:
        config: Optional gateway configuration override.
        replace: If True, kill any existing gateway instance before starting.
                 Useful for systemd services to avoid restart-loop deadlocks
                 when the previous process hasn't fully exited yet.
    """
    # ── Duplicate-instance guard ──────────────────────────────────────
    # Prevent two gateways from running under the same HERMES_HOME.
    # The PID file is scoped to HERMES_HOME, so future multi-profile
    # setups (each profile using a distinct HERMES_HOME) will naturally
    # allow concurrent instances without tripping this guard.
    #
    # Skip the guard when LOGOS_INSTANCE_NAME is set — that env var is used
    # by container/pod-based executors to identify a child gateway distinct
    # from the host's main process.
    import time as _time
    from gateway.status import get_running_pid, remove_pid_file
    if os.environ.get("LOGOS_INSTANCE_NAME") or os.environ.get("HERMES_INSTANCE_NAME"):
        existing_pid = None  # agent instances coexist with the main gateway
    else:
        existing_pid = get_running_pid()
    if existing_pid is not None and existing_pid != os.getpid():
        if replace:
            logger.info(
                "Replacing existing gateway instance (PID %d) with --replace.",
                existing_pid,
            )
            try:
                os.kill(existing_pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass  # Already gone (OSError covers WinError 6 on Windows)
            except PermissionError:
                logger.error(
                    "Permission denied killing PID %d. Cannot replace.",
                    existing_pid,
                )
                return False
            # Wait up to 10 seconds for the old process to exit
            for _ in range(20):
                try:
                    os.kill(existing_pid, 0)
                    _time.sleep(0.5)
                except (ProcessLookupError, PermissionError, OSError):
                    break  # Process is gone (OSError covers WinError 6 on Windows)
            else:
                # Still alive after 10s — force kill
                logger.warning(
                    "Old gateway (PID %d) did not exit after SIGTERM, sending SIGKILL.",
                    existing_pid,
                )
                try:
                    os.kill(existing_pid, signal.SIGKILL)
                    _time.sleep(0.5)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            remove_pid_file()
        else:
            logos_home = (
                os.getenv("LOGOS_HOME")
                or os.getenv("HERMES_HOME")
                or str(Path.home() / ".logos")
            )
            logger.error(
                "Another gateway instance is already running (PID %d, LOGOS_HOME=%s). "
                "Use 'logos gateway restart' to replace it, or 'logos gateway stop' first.",
                existing_pid, logos_home,
            )
            print(
                f"\n❌ Gateway already running (PID {existing_pid}).\n"
                f"   Use 'hermes gateway restart' to replace it,\n"
                f"   or 'hermes gateway stop' to kill it first.\n"
                f"   Or use 'hermes gateway run --replace' to auto-replace.\n"
            )
            return False

    # Sync bundled skills on gateway start (fast -- skips unchanged)
    try:
        from tools.skills_sync import sync_skills
        sync_skills(quiet=True)
    except Exception:
        pass

    # Configure rotating file log so gateway output is persisted for debugging
    log_dir = _hermes_home / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / 'gateway.log',
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    from agent.redact import RedactingFormatter
    # _SessionFilter must be on the handlers, not the root logger.
    # Child loggers propagate records directly to the root's *handlers*,
    # bypassing the root logger's filter() chain entirely.  Adding the filter
    # to each handler guarantees session_id is always injected before format().
    _sess_fmt = RedactingFormatter('%(asctime)s %(levelname)s %(name)s [%(session_id)s]: %(message)s')
    file_handler.addFilter(_SessionFilter())
    file_handler.setFormatter(_sess_fmt)
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.INFO)

    # Separate errors-only log for easy debugging
    error_handler = RotatingFileHandler(
        log_dir / 'errors.log',
        maxBytes=2 * 1024 * 1024,
        backupCount=2,
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.addFilter(_SessionFilter())
    error_handler.setFormatter(_sess_fmt)
    logging.getLogger().addHandler(error_handler)

    # Structured unified log sink (M6 in docs/MISSING.md). Parallel to the
    # text gateway.log above — same events, but JSON-lines format with
    # correlation IDs attached, optimised for `logos debug tail`, grep by
    # task_id/user_id/worker_id, and future log aggregators (Loki, etc.).
    #
    # History: a long 2026-04-11 debugging session burned hours because
    # the CLI spinner output in `logos gateway run` masked the stdlib
    # logger output entirely. Even finding `~/.logos/logs/gateway.log`
    # was a scramble, and the text format made cross-component correlation
    # painful. This handler is the fix — it writes to a well-known path
    # in a format that's trivial to query, and `logos debug tail` pretty-
    # prints it so humans don't have to read raw JSON.
    unified_handler = RotatingFileHandler(
        log_dir / 'unified.jsonl',
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    unified_handler.addFilter(_SessionFilter())
    unified_handler.setFormatter(JsonRedactingFormatter())
    logging.getLogger().addHandler(unified_handler)

    # Reap any openshell CLI / ssh-proxy processes left over from a prior
    # gateway run that died ungracefully (SIGKILL, crash, power loss).
    # Must run BEFORE GatewayRunner.__init__ creates the WorkerRegistry —
    # otherwise an orphaned worker can re-register with its stale
    # sandbox name as soon as /ws/worker comes up and the gateway routes
    # chats to the wrong agent (the day-long "Hermes thinks it's Ani"
    # bug). shutdown_openshell_children() handles graceful shutdowns;
    # this is its safety net for the SIGKILL/crash path.
    try:
        from gateway.executors.openshell import reap_orphan_openshell_processes
        reap_orphan_openshell_processes()
    except Exception as _reap_err:
        logger.warning("reap_orphan_openshell_processes failed: %s", _reap_err)

    # NOTE: the `migrate_routes_to_model_names` helper (in
    # gateway/openshell_routes.py) used to run here and rename legacy
    # `logos-openshell` / `logos-os-<model>` routes to the bare
    # `<model>` scheme via a client-side `openshell gateway add` alias.
    #
    # That approach is structurally broken: `openshell sandbox create
    # --from <Dockerfile>` derives its target container name from the
    # gateway name (`openshell-cluster-<gateway>`), so the alias works
    # for gRPC/exec calls (endpoint-URL routed) but the image-push
    # path fails with `404: No such container: openshell-cluster-
    # <alias>`. Image push has to target the actual Docker container
    # name, not the client-side alias.
    #
    # Proper rename requires destroying the existing gateway and
    # re-provisioning it under the clean name via `openshell gateway
    # start --name <model>`, which is a user-driven action (loses any
    # per-gateway provider/inference-router state). See TASKS.md entry
    # for "model-based gateway names" and MISSING.md M-TBD for the
    # proper flow — until that lands, the migration must stay OFF or
    # /setup will break for any user whose routes were auto-renamed.

    runner = GatewayRunner(config)
    _set_current_runner(runner)

    # Store loop for cross-thread shutdown via request_gateway_shutdown().
    # Uses gateway.runtime_state so the value is visible to modules that
    # imported via `from gateway import run` — see the docstring on
    # gateway/runtime_state.py for the dual-module gotcha.
    loop = asyncio.get_running_loop()
    _runtime_state.set_current_loop(loop)

    # Set up signal handlers
    def signal_handler():
        asyncio.create_task(runner.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass
    
    # Start the gateway (platform adapters, hooks, session recovery)
    success = await runner.start()
    if not success:
        return False

    # Start the HTTP API + dashboard server alongside the platform adapters.
    # Default is 8091 — this is also the port the OpenShell network policy
    # whitelists for sandbox-worker → gateway WebSocket traffic, so the two
    # are kept in sync. Override with HERMES_PORT / LOGOS_PORT env vars.
    http_port = int(os.getenv("LOGOS_PORT") or os.getenv("HERMES_PORT") or "8091")
    try:
        from gateway.http_api import start_http_api
        asyncio.create_task(start_http_api(runner, http_port))
        logger.info("HTTP API task scheduled on port %d", http_port)
    except Exception as _http_err:
        logger.warning("HTTP API failed to start: %s", _http_err)

    # Write PID file so CLI can detect gateway is running
    import atexit
    from gateway.status import write_pid_file, remove_pid_file
    write_pid_file()
    atexit.register(remove_pid_file)

    # Start background cron ticker so scheduled jobs fire automatically
    cron_stop = threading.Event()
    cron_thread = threading.Thread(
        target=_start_cron_ticker,
        args=(cron_stop,),
        kwargs={"adapters": runner.adapters},
        daemon=True,
        name="cron-ticker",
    )
    cron_thread.start()
    
    # Wait for shutdown
    await runner.wait_for_shutdown()
    
    # Stop cron ticker cleanly
    cron_stop.set()
    cron_thread.join(timeout=5)

    # Reap any in-flight openshell CLI subprocess groups so their
    # ssh-proxy children don't outlive the gateway. The executor
    # tracks every Popen pgid in a module-level registry; this call
    # SIGTERMs the whole tree (then SIGKILLs anything that resists).
    # Without this, restart-the-gateway would routinely leave
    # leaked openshell+ssh-proxy processes pinning ports until the
    # next reboot.
    try:
        from gateway.executors.openshell import shutdown_openshell_children
        shutdown_openshell_children()
    except Exception as _osh_err:
        logger.warning("shutdown_openshell_children failed: %s", _osh_err)

    # Close MCP server connections
    try:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except Exception:
        pass

    return True


def main():
    """CLI entry point for the gateway."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Hermes Gateway - Multi-platform messaging")
    parser.add_argument("--config", "-c", help="Path to gateway config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    config = None
    if args.config:
        import json
        with open(args.config, encoding="utf-8") as f:
            data = json.load(f)
            config = GatewayConfig.from_dict(data)
    
    # Run the gateway - exit with code 1 if no platforms connected,
    # so systemd Restart=on-failure will retry on transient errors (e.g. DNS)
    success = asyncio.run(start_gateway(config))
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
