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

    def _flush_memories_for_session(self, old_session_id: str):
        """Prompt the agent to save memories/skills before context is lost.

        Synchronous worker — meant to be called via run_in_executor from
        an async context so it doesn't block the event loop.
        """
        try:
            history = self.session_store.load_transcript(old_session_id)
            if not history or len(history) < 4:
                return

            from agents.hermes.agent import AIAgent
            runtime_kwargs = _resolve_runtime_agent_kwargs()
            if not runtime_kwargs.get("api_key"):
                return

            # Resolve model from config — AIAgent's default is OpenRouter-
            # formatted ("anthropic/claude-opus-4.6") which fails when the
            # active provider is openai-codex.
            model = _resolve_gateway_model()

            tmp_agent = AIAgent(
                **runtime_kwargs,
                model=model,
                max_iterations=8,
                quiet_mode=True,
                enabled_toolsets=["memory", "skills"],
                session_id=old_session_id,
            )

            # Build conversation history from transcript
            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]

            # Give the agent a real turn to think about what to save
            flush_prompt = (
                "[System: This session is about to be automatically reset due to "
                "inactivity or a scheduled daily reset. The conversation context "
                "will be cleared after this turn.\n\n"
                "Review the conversation above and:\n"
                "1. Save any important facts, preferences, or decisions to memory "
                "(user profile or your notes) that would be useful in future sessions.\n"
                "2. If you discovered a reusable workflow or solved a non-trivial "
                "problem, consider saving it as a skill.\n"
                "3. If nothing is worth saving, that's fine — just skip.\n\n"
                "Do NOT respond to the user. Just use the memory and skill_manage "
                "tools if needed, then stop.]"
            )

            tmp_agent.run_conversation(
                user_message=flush_prompt,
                conversation_history=msgs,
            )
            logger.info("Pre-reset memory flush completed for session %s", old_session_id)

            # ── Auto-ingest session transcript into knowledge base ────
            self._auto_ingest_session(old_session_id, msgs)

        except Exception as e:
            logger.debug("Pre-reset memory flush failed for session %s: %s", old_session_id, e)

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

    async def _async_flush_memories(self, old_session_id: str, timeout: float = 30.0):
        """Run the sync memory flush in a thread pool so it won't block the event loop.

        Hard-capped at ``timeout`` seconds (default 30). The flush path
        runs a full AIAgent turn (LLM call + optional tool calls) which
        can stall for 30-60s per attempt against a dead/misconfigured
        auxiliary provider. Without a cap, a cold gateway with dozens of
        expired sessions in its backlog would park every available thread
        pool worker in retry loops — and aiohttp's own static file serving
        uses the same default executor, so the HTTP listener effectively
        stops responding. The cap lets the watcher move on to the next
        session instead of stalling indefinitely.
        """
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._flush_memories_for_session, old_session_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.info(
                "Memory flush for session %s exceeded %.0fs — continuing; "
                "the flush will run opportunistically on the next message "
                "to this session instead.",
                old_session_id, timeout,
            )
    
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
                    # Session has expired — flush memories in the background
                    logger.info(
                        "Session %s expired (key=%s), flushing memories proactively",
                        entry.session_id, key,
                    )
                    try:
                        await self._async_flush_memories(entry.session_id)
                        self.session_store._pre_flushed_sessions.add(entry.session_id)
                        _flushed_this_cycle += 1
                    except Exception as e:
                        logger.debug("Proactive memory flush failed for %s: %s", entry.session_id, e)
                        # Count timeouts toward the cycle budget too —
                        # otherwise a misconfigured aux provider can make
                        # us retry every expired session every cycle.
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

        # History is passed empty for 5.3 — the sandbox worker already
        # maintains its own per-session context via session_id, and the
        # web /chat path also passes an empty history on every turn. Rich
        # history-injection for cross-session agent memory is a later
        # phase concern.
        history: list[dict] = []

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
                os.environ.get("HERMES_MAX_ITERATIONS", "90"),
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
            # user_id: the Telegram chat user isn't a Logos user — the
            # number is their platform ID. Resolve to the agent's
            # creator so the Runs "User" column shows a real name
            # (e.g. Greg) instead of a raw Telegram ID. The platform
            # user_id is still captured in origin_detail for audit.
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
            result = await self.worker_registry.dispatch_task(
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

        # ── 5. Return the adapter-friendly final response ────────────
        final = (result or {}).get("final_response") or ""
        if not final:
            logger.warning("dispatch_platform_message: worker returned empty final_response")
            return "⚠️ The agent returned an empty response."
        return final

    async def _handle_reset_command(self, event: MessageEvent) -> str:
        """Handle /new or /reset command."""
        source = event.source
        
        # Get existing session key
        session_key = self.session_store._generate_session_key(source)
        
        # Flush memories in the background (fire-and-forget) so the user
        # gets the "Session reset!" response immediately.
        try:
            old_entry = self.session_store._entries.get(session_key)
            if old_entry:
                asyncio.create_task(self._async_flush_memories(old_entry.session_id))
        except Exception as e:
            logger.debug("Gateway memory flush on reset failed: %s", e)

        # Reset the session
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
            "`/compress` — Compress conversation context",
            "`/title [name]` — Set or show the session title",
            "`/resume [name]` — Resume a previously-named session",
            "`/usage` — Show token usage for this session",
            "`/insights [days]` — Show usage insights and analytics",
            "`/reasoning [level|show|hide]` — Set reasoning effort or toggle display",
            "`/rollback [number]` — List or restore filesystem checkpoints",
            "`/background <prompt>` — Run a prompt in a separate background session",
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

    async def _handle_background_command(self, event: MessageEvent) -> str:
        """Handle /background <prompt> — run a prompt in a separate background session.

        Spawns a new AIAgent in a background thread with its own session.
        When it completes, sends the result back to the same chat without
        modifying the active session's conversation history.
        """
        prompt = event.get_command_args().strip()
        if not prompt:
            return (
                "Usage: /background <prompt>\n"
                "Example: /background Summarize the top HN stories today\n\n"
                "Runs the prompt in a separate session. "
                "You can keep chatting — the result will appear here when done."
            )

        source = event.source
        task_id = f"bg_{datetime.now().strftime('%H%M%S')}_{os.urandom(3).hex()}"

        # Fire-and-forget the background task
        asyncio.create_task(
            self._run_background_task(prompt, source, task_id)
        )

        preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
        return f'🔄 Background task started: "{preview}"\nTask ID: {task_id}\nYou can keep chatting — results will appear when done.'

    async def _run_background_task(
        self, prompt: str, source: "SessionSource", task_id: str
    ) -> None:
        """Execute a background agent task and deliver the result to the chat."""
        from agents.hermes.agent import AIAgent

        adapter = self.adapters.get(source.platform)
        if not adapter:
            logger.warning("No adapter for platform %s in background task %s", source.platform, task_id)
            return

        _thread_metadata = {"thread_id": source.thread_id} if source.thread_id else None

        try:
            runtime_kwargs = _resolve_runtime_agent_kwargs()
            if not runtime_kwargs.get("api_key"):
                await adapter.send(
                    source.chat_id,
                    f"❌ Background task {task_id} failed: no provider credentials configured.",
                    metadata=_thread_metadata,
                )
                return

            # Read model from config via shared helper
            model = _resolve_gateway_model()

            # Toolset: config.yaml override if present, else hermes-cli.
            # Per-channel toolsets used to be distinct entries but collapsed
            # into hermes-cli since they were identical.
            platform_toolsets_config = {}
            try:
                config_path = _hermes_home / 'config.yaml'
                if config_path.exists():
                    import yaml
                    with open(config_path, 'r', encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                    platform_toolsets_config = user_config.get("platform_toolsets", {})
            except Exception:
                pass

            platform_config_key = {
                Platform.LOCAL: "cli",
                Platform.TELEGRAM: "telegram",
                Platform.DISCORD: "discord",
                Platform.WHATSAPP: "whatsapp",
                Platform.SLACK: "slack",
                Platform.SIGNAL: "signal",
                Platform.HOMEASSISTANT: "homeassistant",
                Platform.EMAIL: "email",
            }.get(source.platform, "cli")

            config_toolsets = platform_toolsets_config.get(platform_config_key)
            if config_toolsets and isinstance(config_toolsets, list):
                enabled_toolsets = config_toolsets
            else:
                enabled_toolsets = ["hermes-cli"]

            platform_key = "cli" if source.platform == Platform.LOCAL else source.platform.value

            pr = self._provider_routing
            max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
            reasoning_config = self._load_reasoning_config()
            self._reasoning_config = reasoning_config

            def run_sync():
                agent = AIAgent(
                    model=model,
                    **runtime_kwargs,
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=False,
                    enabled_toolsets=enabled_toolsets,
                    reasoning_config=reasoning_config,
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=task_id,
                    platform=platform_key,
                    session_db=self._session_db,
                    fallback_model=self._fallback_model,
                )

                return agent.run_conversation(
                    user_message=prompt,
                    task_id=task_id,
                )

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, run_sync)

            response = result.get("final_response", "") if result else ""
            if not response and result and result.get("error"):
                response = f"Error: {result['error']}"

            # Extract media files from the response
            if response:
                media_files, response = adapter.extract_media(response)
                images, text_content = adapter.extract_images(response)

                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                header = f'✅ Background task complete\nPrompt: "{preview}"\n\n'

                if text_content:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + text_content,
                        metadata=_thread_metadata,
                    )
                elif not images and not media_files:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + "(No response generated)",
                        metadata=_thread_metadata,
                    )

                # Send extracted images
                for image_url, alt_text in (images or []):
                    try:
                        await adapter.send_image(
                            chat_id=source.chat_id,
                            image_url=image_url,
                            caption=alt_text,
                        )
                    except Exception:
                        pass

                # Send media files
                for media_path in (media_files or []):
                    try:
                        await adapter.send_file(
                            chat_id=source.chat_id,
                            file_path=media_path,
                        )
                    except Exception:
                        pass
            else:
                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f'✅ Background task complete\nPrompt: "{preview}"\n\n(No response generated)',
                    metadata=_thread_metadata,
                )

        except Exception as e:
            logger.exception("Background task %s failed", task_id)
            try:
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f"❌ Background task {task_id} failed: {e}",
                    metadata=_thread_metadata,
                )
            except Exception:
                pass

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

    async def _handle_compress_command(self, event: MessageEvent) -> str:
        """Handle /compress command -- manually compress conversation context."""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)

        if not history or len(history) < 4:
            return "Not enough conversation to compress (need at least 4 messages)."

        try:
            from agents.hermes.agent import AIAgent
            from agent.model_metadata import estimate_messages_tokens_rough

            runtime_kwargs = _resolve_runtime_agent_kwargs()
            if not runtime_kwargs.get("api_key"):
                return "No provider configured -- cannot compress."

            # Resolve model from config (same reason as memory flush above).
            model = _resolve_gateway_model()

            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            original_count = len(msgs)
            approx_tokens = estimate_messages_tokens_rough(msgs)

            tmp_agent = AIAgent(
                **runtime_kwargs,
                model=model,
                max_iterations=4,
                quiet_mode=True,
                enabled_toolsets=["memory"],
                session_id=session_entry.session_id,
            )

            loop = asyncio.get_event_loop()
            compressed, _ = await loop.run_in_executor(
                None,
                lambda: tmp_agent._compress_context(msgs, "", approx_tokens=approx_tokens),
            )

            self.session_store.rewrite_transcript(session_entry.session_id, compressed)
            # Reset stored token count — transcript changed, old value is stale
            self.session_store.update_session(
                session_entry.session_key, last_prompt_tokens=0,
            )
            new_count = len(compressed)
            new_tokens = estimate_messages_tokens_rough(compressed)

            return (
                f"🗜️ Compressed: {original_count} → {new_count} messages\n"
                f"~{approx_tokens:,} → ~{new_tokens:,} tokens"
            )
        except Exception as e:
            logger.warning("Manual compress failed: %s", e)
            return f"Compression failed: {e}"

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

        # Flush memories for current session before switching
        try:
            asyncio.create_task(self._async_flush_memories(current_entry.session_id))
        except Exception as e:
            logger.debug("Memory flush on resume failed: %s", e)

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

    async def _run_agent(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        auth_user_id: str = None,
        http_sse_queue: "asyncio.Queue | None" = None,
        agent_config: dict = None,
    ) -> Dict[str, Any]:
        """
        Run the agent with the given message and context.
        
        Returns the full result dict from run_conversation, including:
          - "final_response": str (the text to send back)
          - "messages": list (full conversation including tool calls)
          - "api_calls": int
          - "completed": bool
        
        This is run in a thread pool to not block the event loop.
        Supports interruption via new messages.
        """
        from agents.hermes.agent import AIAgent
        import queue

        # LM Studio: pre-load the model with the largest context it supports.
        # The agent system prompt + tool definitions alone can be 3-5 K tokens,
        # so we try descending context sizes until LM Studio accepts the load.
        # Models like moonlight-16b cap at ~8 K; others support 16 K+.
        # Best-effort — if all attempts fail we let LM Studio use its default.
        _lmstudio_server_type = os.getenv("HERMES_SERVER_TYPE", "")
        if _lmstudio_server_type == "lmstudio":
            import aiohttp as _aiohttp
            _lms_base  = re.sub(r"/v1/?$", "", os.getenv("OPENAI_BASE_URL", "")).rstrip("/")
            _lms_model = os.getenv("HERMES_MODEL", "")
            _lms_key   = os.getenv("OPENAI_API_KEY", "")
            if _lms_base and _lms_model:
                _lms_headers = {"Authorization": f"Bearer {_lms_key}"} if _lms_key and _lms_key != "ollama" else {}
                # Known model context limits (substring-matched against model name,
                # case-insensitive). Only lists models known to cap BELOW 64K.
                # At LM Studio's default n_parallel=4, models under 64K total get
                # <16K per slot which is insufficient for agent use (~17K system prompt).
                # Persisted per-model config overrides this; both override the default
                # cascade.
                _LMS_KNOWN_CTX: list[tuple[str, int]] = [
                    # Moonlight / small MoE models
                    ("moonlight", 8192),
                    # Phi-2 / Phi-1 family
                    ("phi-2", 4096),
                    ("phi-1", 4096),
                    # TinyLlama
                    ("tinyllama", 4096),
                    # StableLM-2 1.6B
                    ("stablelm-2-1", 4096),
                    # Gemma 2B
                    ("gemma-2b", 8192),
                    # Qwen 1.5 / 2 with explicit 4K tag
                    ("qwen1.5", 8192),
                    # SmolLM
                    ("smollm", 4096),
                    # Mistral 7B v0.1 (original 8K window)
                    ("mistral-7b-v0.1", 8192),
                ]
                _lms_name_lower = _lms_model.lower()

                # Lookup order:
                # 1. Persisted value from a previous successful load of this model
                # 2. Known-model table (best-guess starting point)
                # 3. Default cascade from 16384
                try:
                    import yaml as _lms_yaml
                    _lms_cfg_now: dict = _lms_yaml.safe_load(_config_path.read_text(encoding="utf-8")) if _config_path.exists() else {}
                except Exception:
                    _lms_cfg_now = {}
                _lms_ctx_map: dict = _lms_cfg_now.get("lmstudio_context_lengths") or {}
                _saved_ctx = int(_lms_ctx_map.get(_lms_model, 0) or 0)

                # n_parallel: how many concurrent requests the server handles.
                # More slots = more parallel users but less context per request.
                # Default 2 (covers most homelab use). Configurable in config.yaml
                # under lmstudio.n_parallel or via the dashboard.
                _n_parallel = 1  # default: 1 slot (LM Studio UI default is 4)
                try:
                    _lms_parallel_cfg = (_lms_cfg_now.get("lmstudio") or {}).get("n_parallel")
                    if _lms_parallel_cfg and isinstance(_lms_parallel_cfg, int) and _lms_parallel_cfg >= 1:
                        _n_parallel = _lms_parallel_cfg
                except Exception:
                    pass

                # Query LM Studio native API for model's max context length.
                # This is the same metadata the benchmark uses — real data, no guessing.
                _native_max_ctx = 0
                try:
                    async with _aiohttp.ClientSession() as _meta_http:
                        _meta_headers = {"Authorization": f"Bearer {_lms_key}"} if _lms_key and _lms_key != "not-needed" else {}
                        async with _meta_http.get(
                            f"{_lms_base}/api/v1/models",
                            headers=_meta_headers,
                            timeout=_aiohttp.ClientTimeout(total=5),
                        ) as _meta_r:
                            if _meta_r.status == 200:
                                _meta_data = await _meta_r.json(content_type=None)
                                for _m in _meta_data.get("models", []):
                                    if _m.get("key", "").lower() == _lms_model.lower():
                                        _native_max_ctx = _m.get("max_context_length", 0)
                                        break
                except Exception:
                    pass

                # Build context size: prefer saved benchmark value, then native metadata,
                # then known-model table, then cascade.
                # Load at half of known max for balance between context and performance.
                _all_sizes = [262144, 131072, 65536, 32768, 16384, 8192, 4096]
                _best_ctx = _saved_ctx or _native_max_ctx
                if _best_ctx and _best_ctx > 0:
                    # Use half of known max for good performance, with fallbacks
                    _target = min(_best_ctx // 2, 65536) if _best_ctx > 32768 else _best_ctx
                    _ctx_sizes = [_target] + [s for s in _all_sizes if s < _target]
                    _ctx_sizes = list(dict.fromkeys(_ctx_sizes))
                    if _native_max_ctx:
                        logger.info("LM Studio: %s native max context: %d, loading at %d", _lms_model, _native_max_ctx, _target)
                else:
                    # Check known-model table
                    _known_ctx = next(
                        (ctx for kw, ctx in _LMS_KNOWN_CTX if kw in _lms_name_lower),
                        None,
                    )
                    if _known_ctx:
                        _ctx_sizes = [_known_ctx] + [s for s in _all_sizes if s < _known_ctx]
                    else:
                        _ctx_sizes = _all_sizes
                try:
                    async with _aiohttp.ClientSession() as _lms_http:
                        # Check if model is already loaded — avoid creating a second instance.
                        # LM Studio creates a new instance on every /load call regardless of
                        # whether the model is already running.
                        _already_loaded = _lms_model in _LMS_CONFIRMED_LOADED
                        _loaded_ctx = 0
                        _other_loaded: list[str] = []  # models in VRAM that are NOT the target
                        if _already_loaded:
                            # Confirmed loaded within this gateway process — skip all
                            # management API queries. The model doesn't unload on its own.
                            logger.debug("LM Studio: %s in confirmed set, skipping pre-load", _lms_model)
                        if not _already_loaded:
                            # Always query the server on first check — _LMS_CONFIRMED_LOADED
                            # only persists within this process lifetime.
                            _models_check_failed = False
                            try:
                                async with _lms_http.get(
                                    f"{_lms_base}/api/v1/models",
                                    headers=_lms_headers,
                                    timeout=_aiohttp.ClientTimeout(total=5),
                                ) as _gr:
                                    if _gr.status == 200:
                                        _gd = await _gr.json(content_type=None)
                                        _model_lower = _lms_model.lower()
                                        for _inst in (_gd.get("data") or []):
                                            _raw_iid = (_inst.get("id") or "")
                                            # Strip any :N suffix from returned IDs before comparing —
                                            # a :2 instance means our model is running; treat as loaded.
                                            _iid = re.sub(r":\d+$", "", _raw_iid.lower())
                                            if _iid == _model_lower or _model_lower in _iid or _iid in _model_lower:
                                                _already_loaded = True
                                                _loaded_ctx = int(_inst.get("max_context_length") or _inst.get("context_length") or 0)
                                                logger.info(
                                                    "LM Studio: found %s already loaded (id=%s, ctx=%s)",
                                                    _lms_model, _raw_iid, _loaded_ctx or "unknown",
                                                )
                                            else:
                                                # A different model is occupying VRAM — record it for eviction
                                                _other_loaded.append(_raw_iid)
                                                logger.debug("LM Studio: other model in VRAM: %s", _raw_iid)
                                    else:
                                        _models_check_failed = True
                                        logger.warning(
                                            "LM Studio: /api/v1/models returned %d — "
                                            "cannot verify loaded models (auth issue?)",
                                            _gr.status,
                                        )
                            except Exception as _models_exc:
                                _models_check_failed = True
                                logger.warning("LM Studio: /api/v1/models failed: %s", _models_exc)

                            # If the management API is unreachable (auth, network) but we
                            # have a benchmark-confirmed context length, trust it and skip
                            # the load cascade.  The model is likely already loaded in LM
                            # Studio by the user — retrying /load will just fail too.
                            if _models_check_failed and _saved_ctx > 0:
                                logger.info(
                                    "LM Studio: management API unavailable but benchmark "
                                    "confirms %s with %d ctx — assuming loaded, skipping pre-load",
                                    _lms_model, _saved_ctx,
                                )
                                _already_loaded = True
                                _loaded_ctx = _saved_ctx

                        # Skip reload if:
                        # - Model is confirmed loaded (from previous check or this query)
                        # - AND context is unknown (0) or sufficient for our target
                        # The target is _ctx_sizes[0] which is the total model context.
                        # We don't need to reload just to change n_parallel — that
                        # requires an unload+reload cycle which is disruptive.
                        _skip_reload = _already_loaded and (_loaded_ctx == 0 or _loaded_ctx >= _ctx_sizes[0])
                        if _skip_reload:
                            # Already loaded with sufficient (or unknown) context — skip load
                            _LMS_CONFIRMED_LOADED.add(_lms_model)
                            # Warn if the loaded context seems to be per-slot limited
                            _desired_per_slot = _ctx_sizes[0] // max(_n_parallel, 1) if _ctx_sizes else 0
                            if _loaded_ctx > 0 and _desired_per_slot > 0:
                                # Detect slot splitting: if loaded context is exactly total/N,
                                # the user's LM Studio is splitting across N parallel slots.
                                # LM Studio defaults to n_parallel=4 ("Max Concurrent Predictions")
                                # which cannot be changed via API — only in the LM Studio UI.
                                for _guess_slots in (2, 3, 4, 6, 8):
                                    if _loaded_ctx == _ctx_sizes[0] // _guess_slots:
                                        _usable = _loaded_ctx
                                        if _usable < 16384:
                                            logger.error(
                                                "LM Studio: %s context is split across %d parallel slots "
                                                "= %d tokens per request — INSUFFICIENT for agent use "
                                                "(system prompt + tools need ~17K tokens). "
                                                "Either use a model with ≥128K context (for %d concurrent users "
                                                "at 32K/slot), or reduce 'Max Concurrent Predictions' in "
                                                "LM Studio advanced settings to fit within %d total context.",
                                                _lms_model, _guess_slots, _usable, _guess_slots, _ctx_sizes[0],
                                            )
                                        else:
                                            logger.warning(
                                                "LM Studio: %s loaded with %d total context split across %d parallel slots "
                                                "= %d tokens per request. For full 32K/slot, use a model with ≥128K "
                                                "context or reduce 'Max Concurrent Predictions' in LM Studio settings.",
                                                _lms_model, _ctx_sizes[0], _guess_slots, _usable,
                                            )
                                        # Update the saved context to the per-slot value so the
                                        # compressor uses the right limit
                                        try:
                                            _lms_cfg_now.setdefault("lmstudio_context_lengths", {})[_lms_model] = _usable
                                            _config_path.write_text(_lms_yaml.dump(_lms_cfg_now, default_flow_style=False, allow_unicode=True))
                                        except Exception:
                                            pass
                                        break
                            logger.info("LM Studio: %s already loaded (ctx=%s), skipping pre-load", _lms_model, _loaded_ctx or "?")
                        else:
                            logger.info(
                                "LM Studio: need to load %s (already_loaded=%s, loaded_ctx=%s, target_ctx=%s, n_parallel=%d)",
                                _lms_model, _already_loaded, _loaded_ctx or "?", _ctx_sizes[0] if _ctx_sizes else "?", _n_parallel,
                            )
                            # Evict any other models occupying VRAM before loading the target.
                            # Without this, loading a new model alongside an existing one causes
                            # OOM crashes (exit code 18446744072635812000 / CUDA OOM).
                            for _other_id in _other_loaded:
                                try:
                                    async with _lms_http.post(
                                        f"{_lms_base}/api/v1/models/unload",
                                        headers=_lms_headers,
                                        json={"instance_id": _other_id},
                                        timeout=_aiohttp.ClientTimeout(total=10),
                                    ):
                                        pass
                                    logger.debug("LM Studio: evicted %s before loading %s", _other_id, _lms_model)
                                    _LMS_CONFIRMED_LOADED.discard(_other_id)
                                except Exception:
                                    pass
                            # Not loaded or loaded with insufficient context — load/reload
                            if _already_loaded:
                                # Unload the insufficient-context instance first
                                try:
                                    async with _lms_http.post(
                                        f"{_lms_base}/api/v1/models/unload",
                                        headers=_lms_headers,
                                        json={"instance_id": _lms_model},
                                        timeout=_aiohttp.ClientTimeout(total=10),
                                    ):
                                        pass
                                except Exception:
                                    pass
                            for _ctx in _ctx_sizes:
                                try:
                                    _load_params = {
                                        "model": _lms_model,
                                        "context_length": _ctx,
                                        "flash_attention": True,
                                        "echo_load_config": True,
                                    }
                                    # n_parallel is configured in LM Studio settings, not via the load API.
                                    # Newer LM Studio versions reject it as an unrecognized key.
                                    _per_slot = _ctx // max(_n_parallel, 1)
                                    logger.info(
                                        "LM Studio: loading %s ctx=%d n_parallel=%d (per-slot=%d)",
                                        _lms_model, _ctx, _n_parallel, _per_slot,
                                    )
                                    _load_timeout = max(60, _ctx // 1000) if _ctx else 60
                                    async with _lms_http.post(
                                        f"{_lms_base}/api/v1/models/load",
                                        headers=_lms_headers,
                                        json=_load_params,
                                        timeout=_aiohttp.ClientTimeout(total=_load_timeout),
                                    ) as _lr:
                                        if _lr.status == 200:
                                            # Read actual applied config from echo_load_config
                                            try:
                                                _lr_data = await _lr.json(content_type=None)
                                                _applied = (_lr_data.get("load_config") or {}).get("context_length")
                                                if _applied and _applied != _ctx:
                                                    logger.info("LM Studio: requested ctx=%d, applied ctx=%d", _ctx, _applied)
                                                    _ctx = _applied
                                            except Exception:
                                                pass
                                            # Persist the PER-SLOT context for this model.
                                            # This is what a single request actually gets.
                                            # context_length / n_parallel = usable tokens.
                                            _effective_ctx = _ctx // max(_n_parallel, 1)
                                            if _effective_ctx != _saved_ctx:
                                                try:
                                                    _lms_cfg_now.setdefault("lmstudio_context_lengths", {})[_lms_model] = _effective_ctx
                                                    # Also store the total + parallel config for reference
                                                    _lms_cfg_now.setdefault("lmstudio", {})["n_parallel"] = _n_parallel
                                                    _config_path.write_text(_lms_yaml.dump(_lms_cfg_now, default_flow_style=False, allow_unicode=True))
                                                    logger.info(
                                                        "LM Studio: persisted per-slot context %d for %s (total=%d, slots=%d)",
                                                        _effective_ctx, _lms_model, _ctx, _n_parallel,
                                                    )
                                                except Exception:
                                                    pass
                                            _LMS_CONFIRMED_LOADED.add(_lms_model)
                                            break   # accepted — stop trying smaller sizes
                                        # Log the actual failure reason
                                        try:
                                            _lr_body = await _lr.text()
                                        except Exception:
                                            _lr_body = ""
                                        logger.warning(
                                            "LM Studio: /api/v1/models/load returned %d for ctx=%d: %s",
                                            _lr.status, _ctx, _lr_body[:200],
                                        )
                                        # Failed — unload before retrying smaller
                                        try:
                                            async with _lms_http.post(
                                                f"{_lms_base}/api/v1/models/unload",
                                                headers=_lms_headers,
                                                json={"instance_id": _lms_model},
                                                timeout=_aiohttp.ClientTimeout(total=10),
                                            ):
                                                pass
                                        except Exception:
                                            pass
                                except Exception as _load_exc:
                                    logger.warning("LM Studio: /api/v1/models/load exception for ctx=%d: %s", _ctx, _load_exc)
                                    break   # network error — don't retry
                except Exception as _preload_exc:
                    logger.warning("LM Studio: pre-load failed: %s", _preload_exc)

        # Toolset: config.yaml per-channel override if present, else hermes-cli.
        # Per-channel toolset variants used to exist but collapsed into
        # hermes-cli since they were identical clones.
        platform_toolsets_config = {}
        try:
            config_path = _hermes_home / 'config.yaml'
            if config_path.exists():
                import yaml
                with open(config_path, 'r', encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
                platform_toolsets_config = user_config.get("platform_toolsets", {})
        except Exception as e:
            logger.debug("Could not load platform_toolsets config: %s", e)

        platform_config_key = {
            Platform.LOCAL: "cli",
            Platform.TELEGRAM: "telegram",
            Platform.DISCORD: "discord",
            Platform.WHATSAPP: "whatsapp",
            Platform.SLACK: "slack",
            Platform.SIGNAL: "signal",
            Platform.HOMEASSISTANT: "homeassistant",
            Platform.EMAIL: "email",
        }.get(source.platform, "cli")

        config_toolsets = platform_toolsets_config.get(platform_config_key)
        if config_toolsets and isinstance(config_toolsets, list):
            enabled_toolsets = config_toolsets
        else:
            enabled_toolsets = ["hermes-cli"]

        # Named agent override: use agent-specific toolsets if configured
        if agent_config:
            import json as _json
            _agent_toolsets = agent_config.get("toolsets")
            if _agent_toolsets:
                try:
                    _parsed = _json.loads(_agent_toolsets) if isinstance(_agent_toolsets, str) else _agent_toolsets
                    if isinstance(_parsed, list) and _parsed:
                        enabled_toolsets = _parsed
                except Exception:
                    pass
        
        # Tool progress mode from config.yaml: "all", "new", "verbose", "off"
        # Falls back to env vars for backward compatibility
        _progress_cfg = {}
        try:
            _tp_cfg_path = _hermes_home / "config.yaml"
            if _tp_cfg_path.exists():
                import yaml as _tp_yaml
                with open(_tp_cfg_path, encoding="utf-8") as _tp_f:
                    _tp_data = _tp_yaml.safe_load(_tp_f) or {}
                _progress_cfg = _tp_data.get("display", {})
        except Exception:
            pass
        progress_mode = (
            _progress_cfg.get("tool_progress")
            or os.getenv("HERMES_TOOL_PROGRESS_MODE")
            or "all"
        )
        tool_progress_enabled = progress_mode != "off"
        
        # Queue for progress messages (thread-safe)
        progress_queue = queue.Queue() if tool_progress_enabled else None
        last_tool = [None]  # Mutable container for tracking in closure
        last_progress_msg = [None]  # Track last message for dedup
        repeat_count = [0]  # How many times the same message repeated
        tools_used_count = [0]  # Actual tool calls made this turn
        _call_counter = [0]

        def progress_callback(tool_name: str, preview: str = None, args: dict = None):
            """Callback invoked by agent when a tool is about to execute."""
            tools_used_count[0] += 1
            _call_counter[0] += 1
            call_id = _call_counter[0]

            # Accumulate tool call log for the run record
            _tool_calls_log.append({"tool": tool_name, "preview": (preview or "")[:80]})

            # Emit structured tool_start event for HTTP SSE clients
            if http_sse_queue is not None:
                try:
                    http_sse_queue.put_nowait({
                        "type": "tool_start",
                        "call_id": call_id,
                        "tool": tool_name,
                        "preview": (preview or "")[:80],
                    })
                except Exception:
                    pass

            if not progress_queue:
                return call_id
            
            # "new" mode: only report when tool changes
            if progress_mode == "new" and tool_name == last_tool[0]:
                return
            last_tool[0] = tool_name
            
            # Build progress message with primary argument preview
            tool_emojis = {
                "terminal": "💻",
                "process": "⚙️",
                "web_search": "🔍",
                "web_extract": "📄",
                "read_file": "📖",
                "write_file": "✍️",
                "patch": "🔧",
                "search": "🔎",
                "search_files": "🔎",
                "list_directory": "📂",
                "image_generate": "🎨",
                "text_to_speech": "🔊",
                "browser_navigate": "🌐",
                "browser_click": "👆",
                "browser_type": "⌨️",
                "browser_snapshot": "📸",
                "browser_scroll": "📜",
                "browser_back": "◀️",
                "browser_press": "⌨️",
                "browser_close": "🚪",
                "browser_get_images": "🖼️",
                "browser_vision": "👁️",
                "moa_query": "🧠",
                "mixture_of_agents": "🧠",
                "vision_analyze": "👁️",
                "skill_view": "📚",
                "skills_list": "📋",
                "todo": "📋",
                "memory": "🧠",
                "session_search": "🔍",
                "send_message": "📨",
                "schedule_cronjob": "⏰",
                "list_cronjobs": "⏰",
                "remove_cronjob": "⏰",
                "execute_code": "🐍",
                "delegate_task": "🔀",
                "clarify": "❓",
                "skill_manage": "📝",
            }
            emoji = tool_emojis.get(tool_name, "⚙️")
            
            # Verbose mode: show detailed arguments
            if progress_mode == "verbose" and args:
                import json as _json
                args_str = _json.dumps(args, ensure_ascii=False, default=str)
                if len(args_str) > 200:
                    args_str = args_str[:197] + "..."
                msg = f"{emoji} {tool_name}({list(args.keys())})\n{args_str}"
                progress_queue.put(msg)
                return call_id
            
            if preview:
                # Truncate preview to keep messages clean
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                msg = f"{emoji} {tool_name}: \"{preview}\""
            else:
                msg = f"{emoji} {tool_name}..."
            
            # Dedup: collapse consecutive identical progress messages.
            # Common with execute_code where models iterate with the same
            # code (same boilerplate imports → identical previews).
            if msg == last_progress_msg[0]:
                repeat_count[0] += 1
                # Update the last line in progress_lines with a counter
                # via a special "dedup" queue message.
                progress_queue.put(("__dedup__", msg, repeat_count[0]))
                return call_id
            last_progress_msg[0] = msg
            repeat_count[0] = 0

            progress_queue.put(msg)
            return call_id
        
        # Background task to send progress messages
        # Accumulates tool lines into a single message that gets edited
        _progress_metadata = {"thread_id": source.thread_id} if source.thread_id else None

        async def send_progress_messages():
            if not progress_queue:
                return

            adapter = self.adapters.get(source.platform)
            if not adapter:
                return

            progress_lines = []      # Accumulated tool lines
            progress_msg_id = None   # ID of the progress message to edit
            can_edit = True          # False once an edit fails (platform doesn't support it)

            while True:
                try:
                    raw = progress_queue.get_nowait()
                    
                    # Handle dedup messages: update last line with repeat counter
                    if isinstance(raw, tuple) and len(raw) == 3 and raw[0] == "__dedup__":
                        _, base_msg, count = raw
                        if progress_lines:
                            progress_lines[-1] = f"{base_msg} (×{count + 1})"
                        msg = progress_lines[-1] if progress_lines else base_msg
                    else:
                        msg = raw
                        progress_lines.append(msg)

                    if can_edit and progress_msg_id is not None:
                        # Try to edit the existing progress message
                        full_text = "\n".join(progress_lines)
                        result = await adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=progress_msg_id,
                            content=full_text,
                        )
                        if not result.success:
                            # Platform doesn't support editing — stop trying,
                            # send just this new line as a separate message
                            can_edit = False
                            await adapter.send(chat_id=source.chat_id, content=msg, metadata=_progress_metadata)
                    else:
                        if can_edit:
                            # First tool: send all accumulated text as new message
                            full_text = "\n".join(progress_lines)
                            result = await adapter.send(chat_id=source.chat_id, content=full_text, metadata=_progress_metadata)
                        else:
                            # Editing unsupported: send just this line
                            result = await adapter.send(chat_id=source.chat_id, content=msg, metadata=_progress_metadata)
                        if result.success and result.message_id:
                            progress_msg_id = result.message_id

                    # Restore typing indicator
                    await asyncio.sleep(0.3)
                    await adapter.send_typing(source.chat_id, metadata=_progress_metadata)

                except queue.Empty:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    # Drain remaining queued messages
                    while not progress_queue.empty():
                        try:
                            raw = progress_queue.get_nowait()
                            if isinstance(raw, tuple) and len(raw) == 3 and raw[0] == "__dedup__":
                                _, base_msg, count = raw
                                if progress_lines:
                                    progress_lines[-1] = f"{base_msg} (×{count + 1})"
                            else:
                                progress_lines.append(raw)
                        except Exception:
                            break
                    # Final edit with all remaining tools (only if editing works)
                    if can_edit and progress_lines and progress_msg_id:
                        full_text = "\n".join(progress_lines)
                        try:
                            await adapter.edit_message(
                                chat_id=source.chat_id,
                                message_id=progress_msg_id,
                                content=full_text,
                            )
                        except Exception:
                            pass
                    return
                except Exception as e:
                    logger.error("Progress message error: %s", e)
                    await asyncio.sleep(1)
        
        # We need to share the agent instance for interrupt support
        agent_holder = [None]  # Mutable container for the agent instance
        result_holder = [None]  # Mutable container for the result
        tools_holder = [None]   # Mutable container for the tool definitions
        
        # Bridge sync step_callback → async hooks.emit for agent:step events
        _loop_for_step = asyncio.get_event_loop()
        _hooks_ref = self.hooks
        _now = time.time
        _platform_val = source.platform.value if source.platform else "unknown"

        # Seed session_status entry so /status can see it immediately
        if session_key:
            self._session_status[session_key] = {
                "platform": _platform_val,
                "current_tool": "thinking…",
                "tool_started_at": _now(),
                "session_started_at": _now(),
                "tool_count": 0,
                "error_count": 0,
                "recent_tools": [],
                "stuck": False,
            }

        # Create the run record so the Runs tab shows this chat immediately.
        # Resolve model now so it shows in the list before the run completes.
        _run_model_early = _resolve_gateway_model()
        _run_id = start_run(
            session_id=session_id,
            user_id=auth_user_id,
            user_message=message,
            model=_run_model_early,
        )
        _tool_calls_log: list = []

        def tool_complete_callback(tool_name: str, call_id, success: bool, duration_ms: float, error: str = None, result: str = None):
            """Callback invoked by agent after a tool finishes executing."""
            # Enrich the most recent matching entry in _tool_calls_log
            for entry in reversed(_tool_calls_log):
                if entry.get("tool") == tool_name and "ok" not in entry:
                    entry["ok"] = success
                    entry["ms"] = round(duration_ms, 1)
                    if error:
                        entry["err"] = (error or "")[:100]
                    break
            # Emit structured tool_end event for HTTP SSE clients
            if http_sse_queue is not None:
                try:
                    http_sse_queue.put_nowait({
                        "type": "tool_end",
                        "call_id": call_id,
                        "tool": tool_name,
                        "success": success,
                        "duration_ms": round(duration_ms, 1),
                        "error": (error or "")[:100] if error else None,
                    })
                    # Surface memory writes for the chat UI (parity with the
                    # in-sandbox dispatcher in docker/sandbox_worker.py).
                    if tool_name == "memory" and success:
                        http_sse_queue.put_nowait({
                            "type": "memory_write",
                            "preview": str(result or "")[:200],
                        })
                except Exception:
                    pass

        def _step_callback_sync(iteration: int, tool_names: list) -> None:
            # Update live session status (dict writes are GIL-safe in CPython)
            if session_key and session_key in self._session_status:
                tool_name = tool_names[0] if tool_names else "unknown"
                entry = self._session_status[session_key]
                entry["current_tool"] = tool_name
                entry["tool_started_at"] = _now()
                entry["tool_count"] = iteration
                recent = entry["recent_tools"]
                if not recent or recent[-1] != tool_name:
                    recent.append(tool_name)
                    if len(recent) > 10:
                        recent.pop(0)
            # Note: _tool_calls_log is now populated by progress_callback (with preview)
            try:
                asyncio.run_coroutine_threadsafe(
                    _hooks_ref.emit("agent:step", {
                        "platform": _platform_val,
                        "user_id": source.user_id,
                        "session_id": session_id,
                        "iteration": iteration,
                        "tool_names": tool_names,
                    }),
                    _loop_for_step,
                )
            except Exception as _e:
                logger.debug("agent:step hook error: %s", _e)

        def run_sync():
            # Pass session_key to process registry via env var so background
            # processes can be mapped back to this gateway session
            os.environ["HERMES_SESSION_KEY"] = session_key or ""

            # Read from env var or use default (same as CLI)
            max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
            
            # Map platform enum to the platform hint key the agent understands.
            # Platform.LOCAL ("local") maps to "cli"; others pass through as-is.
            platform_key = "cli" if source.platform == Platform.LOCAL else source.platform.value
            
            # Combine platform context with user-configured ephemeral system prompt
            combined_ephemeral = context_prompt or ""
            if self._ephemeral_system_prompt:
                combined_ephemeral = (combined_ephemeral + "\n\n" + self._ephemeral_system_prompt).strip()

            # Inject named agent identity so the agent knows its own name
            if agent_config and agent_config.get("name"):
                _agent_name = agent_config["name"]
                combined_ephemeral = (
                    f"**Your name is {_agent_name}.** "
                    f"When asked your name, introduce yourself as {_agent_name}.\n\n"
                    + combined_ephemeral
                )

            # Re-read .env for fresh credentials (gateway is long-lived — keys
            # may change without restart via the setup wizard or manual edits).
            # config.yaml is bridged to env at startup and by /model, /provider
            # commands — manual config.yaml edits require a gateway restart.
            try:
                load_dotenv(_env_path, override=True, encoding="utf-8")
            except UnicodeDecodeError:
                load_dotenv(_env_path, override=True, encoding="latin-1")
            except Exception:
                pass
            # Inject tool credentials from DB (set via /api/services/keys UI).
            # Only sets keys NOT already in os.environ, so .env takes priority.
            try:
                from gateway.services import inject_credentials
                inject_credentials()
            except Exception:
                pass

            model = _resolve_gateway_model()

            # Named agent override: use agent-specific model if configured
            if agent_config and agent_config.get("model"):
                model = agent_config["model"]
                # Resolve the correct provider for the agent's model
                try:
                    from gateway.auth import db as _adb
                    _resolved = False
                    for cp in _adb.list_cloud_providers():
                        if cp.get("active_model") == model:
                            os.environ["OPENAI_API_KEY"] = cp.get("api_key") or ""
                            os.environ["OPENAI_BASE_URL"] = cp.get("base_url") or ""
                            _resolved = True
                            break
                    if not _resolved:
                        for _m in _adb.list_machines():
                            if _m.get("enabled") and _m.get("endpoint_url"):
                                os.environ["OPENAI_BASE_URL"] = _m["endpoint_url"].rstrip("/")
                                os.environ["OPENAI_API_KEY"] = _m.get("api_key") or "not-needed"
                                break
                except Exception:
                    pass

            try:
                runtime_kwargs = _resolve_runtime_agent_kwargs()
            except Exception as exc:
                return {
                    "final_response": f"⚠️ Provider authentication failed: {exc}",
                    "messages": [],
                    "api_calls": 0,
                    "tools": [],
                }

            pr = self._provider_routing
            reasoning_config = self._load_reasoning_config()
            self._reasoning_config = reasoning_config

            # Inject approved MCP toolsets for this session.
            # Grants are stored in mcp_access when the user approves an access request.
            # We add the corresponding mcp-{name} toolset here so tools appear this turn.
            # NOTE: we must not reassign `enabled_toolsets` here because it is a
            # closure variable from _run_agent — reassigning would make Python treat
            # it as local to run_sync(), causing UnboundLocalError on the read.
            _effective_toolsets = list(enabled_toolsets)
            try:
                from gateway.mcp_access import get_grants as _get_mcp_grants
                _mcp_grants = _get_mcp_grants(session_id)
                if _mcp_grants:
                    for _mcp_server in _mcp_grants:
                        _mcp_ts = f"mcp-{_mcp_server}"
                        if _mcp_ts not in _effective_toolsets:
                            _effective_toolsets.append(_mcp_ts)
            except Exception:
                pass

            # Auto-inject docker-deployed MCP servers. User explicitly
            # deployed them via the dashboard "+ Add server → Deploy as
            # container" flow, which is as strong a consent signal as
            # clicking Approve on a per-session grant. Skipping the
            # session-grant dance makes dashboard-deployed tools
            # immediately usable without the agent having to discover
            # and request_mcp_access first — which doesn't work anyway
            # if the agent's toolset config doesn't include mcp-gateway.
            try:
                from gateway.auth import db as _auth_db
                for _srv in _auth_db.list_mcp_servers():
                    if _srv.get("deploy_mode") == "docker" and _srv.get("status") == "running":
                        _mcp_ts = f"mcp-{_srv['name']}"
                        if _mcp_ts not in _effective_toolsets:
                            _effective_toolsets.append(_mcp_ts)
            except Exception:
                pass

            _runtime_id = self._resolve_runtime(session_id)

            if _runtime_id == "claude-direct":
                # ── Claude Direct runtime — Anthropic SDK with native tool use ──
                from logos.adapters.claude_direct.adapter import ClaudeDirectAdapter
                from logos.agent.interface import AgentContext as _AgentContext
                _cd_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
                if not _cd_api_key:
                    # Fall back to OpenAI key if Anthropic key not set
                    _cd_api_key = runtime_kwargs.get("api_key", "")
                _cd_model = model
                # Map generic model names to Anthropic model IDs
                if "/" not in _cd_model and not _cd_model.startswith("claude"):
                    _cd_model = "claude-sonnet-4-20250514"
                adapter = ClaudeDirectAdapter(
                    model=_cd_model,
                    api_key=_cd_api_key,
                    system_prompt=combined_ephemeral or None,
                    enabled_toolsets=_effective_toolsets,
                    tool_progress_callback=progress_callback,
                    tool_complete_callback=tool_complete_callback,
                    session_id=session_id,
                )
                agent_holder[0] = adapter
            else:
                # ── Hermes runtime (default) ─────────────────────────────────
                adapter = None

            if adapter is None:
                # ── Debug: log runtime config before agent creation ──
                logger.info(
                    "Creating AIAgent: model=%s base_url=%s api_key=%s... provider=%s toolsets=%s",
                    model,
                    runtime_kwargs.get("base_url", "?"),
                    (runtime_kwargs.get("api_key", "") or "")[:15],
                    runtime_kwargs.get("provider", "?"),
                    _effective_toolsets,
                )
                agent = AIAgent(
                    model=model,
                    **runtime_kwargs,
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=False,
                    enabled_toolsets=_effective_toolsets,
                    ephemeral_system_prompt=combined_ephemeral or None,
                    prefill_messages=self._prefill_messages or None,
                    reasoning_config=reasoning_config,
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=session_id,
                    tool_progress_callback=progress_callback,
                    tool_complete_callback=tool_complete_callback,
                    step_callback=_step_callback_sync if _hooks_ref.loaded_hooks else None,
                    platform=platform_key,
                    session_db=self._session_db,
                    fallback_model=self._fallback_model,
                )
            
            if adapter is None:
                # Store agent reference for interrupt support (Hermes path)
                agent_holder[0] = agent
                # Capture the full tool definitions for transcript logging
                tools_holder[0] = agent.tools if hasattr(agent, 'tools') else None
            
            # Convert history to agent format.
            # Two cases:
            #   1. Normal path (from transcript): simple {role, content, timestamp} dicts
            #      - Strip timestamps, keep role+content
            #   2. Interrupt path (from agent result["messages"]): full agent messages
            #      that may include tool_calls, tool_call_id, reasoning, etc.
            #      - These must be passed through intact so the API sees valid
            #        assistant→tool sequences (dropping tool_calls causes 500 errors)
            agent_history = []
            for msg in history:
                role = msg.get("role")
                if not role:
                    continue
                
                # Skip metadata entries (tool definitions, session info)
                # -- these are for transcript logging, not for the LLM
                if role in ("session_meta",):
                    continue
                
                # Skip system messages -- the agent rebuilds its own system prompt
                if role == "system":
                    continue
                
                # Rich agent messages (tool_calls, tool results) must be passed
                # through intact so the API sees valid assistant→tool sequences
                has_tool_calls = "tool_calls" in msg
                has_tool_call_id = "tool_call_id" in msg
                is_tool_message = role == "tool"
                
                if has_tool_calls or has_tool_call_id or is_tool_message:
                    clean_msg = {k: v for k, v in msg.items() if k != "timestamp"}
                    agent_history.append(clean_msg)
                else:
                    # Simple text message - just need role and content
                    content = msg.get("content")
                    if content:
                        # Tag cross-platform mirror messages so the agent knows their origin
                        if msg.get("mirror"):
                            mirror_src = msg.get("mirror_source", "another session")
                            content = f"[Delivered from {mirror_src}] {content}"
                        agent_history.append({"role": role, "content": content})
            
            # Collect MEDIA paths already in history so we can exclude them
            # from the current turn's extraction. This is compression-safe:
            # even if the message list shrinks, we know which paths are old.
            _history_media_paths: set = set()
            for _hm in agent_history:
                if _hm.get("role") in ("tool", "function"):
                    _hc = _hm.get("content", "")
                    if "MEDIA:" in _hc:
                        for _match in re.finditer(r'MEDIA:(\S+)', _hc):
                            _p = _match.group(1).strip().rstrip('",}')
                            if _p:
                                _history_media_paths.add(_p)
            
            if adapter is not None:
                # Claude Direct (or other non-Hermes runtime)
                _ctx = _AgentContext(
                    user_message=message,
                    conversation_history=agent_history,
                    task_id=session_id,
                    tool_progress_callback=progress_callback,
                    tool_complete_callback=tool_complete_callback,
                )
                result = adapter.run(_ctx).to_dict()
            else:
                result = agent.run_conversation(message, conversation_history=agent_history, task_id=session_id)
            result_holder[0] = result

            # ── Auto-upscale context on context errors (Hermes only) ────
            # If the agent failed due to context length, and we're below the
            # model's native max, reload at a higher context and retry once.
            _err = result.get("error", "")
            _ctx_error_phrases = [
                "context length exceeded", "cannot compress further",
                "context size", "maximum context", "too many tokens",
                "payload too large", "prompt is too long",
            ]
            if _runtime_id == "hermes" and _err and any(p in _err.lower() for p in _ctx_error_phrases):
                _current_ctx = getattr(agent.context_compressor, "context_length", 0) if hasattr(agent, "context_compressor") else 0
                # Fetch model's native max context via sync HTTP
                _model_max = 0
                try:
                    import requests as _req
                    _retry_base = os.environ.get("OPENAI_BASE_URL", "").replace("/v1", "")
                    _retry_key = os.environ.get("OPENAI_API_KEY", "not-needed")
                    _retry_headers = {"Authorization": f"Bearer {_retry_key}"} if _retry_key and _retry_key != "not-needed" else {}
                    _rr = _req.get(f"{_retry_base}/api/v1/models", headers=_retry_headers, timeout=5)
                    if _rr.status_code == 200:
                        for _rm in _rr.json().get("models", []):
                            if model and _rm.get("key", "").lower() == model.lower():
                                _model_max = _rm.get("max_context_length", 0)
                                break
                except Exception:
                    pass

                _upscale_target = min(_model_max, 131072) if _model_max else 0
                if _upscale_target and _upscale_target > _current_ctx * 1.5:
                    logger.info(
                        "Context error at %d tokens — reloading model at %d (native max: %d)",
                        _current_ctx, _upscale_target, _model_max,
                    )
                    try:
                        _rl = _req.post(
                            f"{_retry_base}/api/v1/models/load",
                            headers=_retry_headers,
                            json={"model": model, "context_length": _upscale_target, "flash_attention": True},
                            timeout=120,
                        )
                        if _rl.status_code == 200:
                            logger.info("Model reloaded at %d context — retrying agent", _upscale_target)
                            agent2 = AIAgent(
                                model=model,
                                **runtime_kwargs,
                                max_iterations=max_iterations,
                                quiet_mode=True,
                                verbose_logging=False,
                                enabled_toolsets=_effective_toolsets,
                                ephemeral_system_prompt=combined_ephemeral or None,
                                reasoning_config=reasoning_config,
                                session_id=session_id,
                                platform=platform_key,
                                fallback_model=self._fallback_model,
                            )
                            agent_holder[0] = agent2
                            result = agent2.run_conversation(message, conversation_history=agent_history, task_id=session_id)
                            result_holder[0] = result
                    except Exception as _reload_exc:
                        logger.warning("Context upscale reload failed: %s", _reload_exc)

            # Return final response, or a message if something went wrong
            final_response = result.get("final_response")

            # Extract last actual prompt token count from the agent's compressor
            _last_prompt_toks = 0
            _agent = agent_holder[0]
            if _agent and hasattr(_agent, "context_compressor"):
                _last_prompt_toks = getattr(_agent.context_compressor, "last_prompt_tokens", 0)
            _resolved_model = getattr(_agent, "model", None) if _agent else None

            if not final_response:
                _err_raw = result.get("error", "")
                if _err_raw:
                    _err_lc = _err_raw.lower()
                    if "cookie auth" in _err_lc or "no cookie" in _err_lc:
                        error_msg = (
                            "⚠️ LM Studio is requiring cookie authentication — "
                            "this blocks server-side requests. "
                            "Go to LM Studio → Developer → Require Authentication "
                            "and disable it, then restart LM Studio."
                        )
                    elif "401" in _err_raw or "403" in _err_raw:
                        _code_m = re.search(r"\b([45]\d\d)\b", _err_raw)
                        _http_code = _code_m.group(1) if _code_m else "4xx"
                        error_msg = (
                            f"⚠️ Authentication error from the AI provider (HTTP {_http_code}). "
                            "Check your API key in Setup, or disable authentication in your local server."
                        )
                    else:
                        error_msg = f"⚠️ {_err_raw}"
                else:
                    error_msg = "(No response generated)"
                return {
                    "final_response": error_msg,
                    "messages": result.get("messages", []),
                    "api_calls": result.get("api_calls", 0),
                    "tools": tools_holder[0] or [],
                    "history_offset": len(agent_history),
                    "last_prompt_tokens": _last_prompt_toks,
                    "model": _resolved_model,
                }
            
            # Scan tool results for MEDIA:<path> tags that need to be delivered
            # as native audio/file attachments.  The TTS tool embeds MEDIA: tags
            # in its JSON response, but the model's final text reply usually
            # doesn't include them.  We collect unique tags from tool results and
            # append any that aren't already present in the final response, so the
            # adapter's extract_media() can find and deliver the files exactly once.
            #
            # Uses path-based deduplication against _history_media_paths (collected
            # before run_conversation) instead of index slicing. This is safe even
            # when context compression shrinks the message list. (Fixes #160)
            if "MEDIA:" not in final_response:
                media_tags = []
                has_voice_directive = False
                for msg in result.get("messages", []):
                    if msg.get("role") in ("tool", "function"):
                        content = msg.get("content", "")
                        if "MEDIA:" in content:
                            for match in re.finditer(r'MEDIA:(\S+)', content):
                                path = match.group(1).strip().rstrip('",}')
                                if path and path not in _history_media_paths:
                                    media_tags.append(f"MEDIA:{path}")
                            if "[[audio_as_voice]]" in content:
                                has_voice_directive = True
                
                if media_tags:
                    seen = set()
                    unique_tags = []
                    for tag in media_tags:
                        if tag not in seen:
                            seen.add(tag)
                            unique_tags.append(tag)
                    if has_voice_directive:
                        unique_tags.insert(0, "[[audio_as_voice]]")
                    final_response = final_response + "\n" + "\n".join(unique_tags)
            
            # Sync session_id: the agent may have created a new session during
            # mid-run context compression (_compress_context splits sessions).
            # If so, update the session store entry so the NEXT message loads
            # the compressed transcript, not the stale pre-compression one.
            agent = agent_holder[0]
            if agent and session_key and hasattr(agent, 'session_id') and agent.session_id != session_id:
                logger.info(
                    "Session split detected: %s → %s (compression)",
                    session_id, agent.session_id,
                )
                entry = self.session_store._entries.get(session_key)
                if entry:
                    entry.session_id = agent.session_id
                    self.session_store._save()

            effective_session_id = getattr(agent, 'session_id', session_id) if agent else session_id

            return {
                "final_response": final_response,
                "last_reasoning": result.get("last_reasoning"),
                "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                "api_calls": result_holder[0].get("api_calls", 0) if result_holder[0] else 0,
                "tools": tools_holder[0] or [],
                "tools_used": tools_used_count[0],
                "tool_detail": _tool_calls_log or [],
                "history_offset": len(agent_history),
                "last_prompt_tokens": _last_prompt_toks,
                "model": _resolved_model,
                "session_id": effective_session_id,
            }
        
        # Start progress message sender if enabled
        progress_task = None
        if tool_progress_enabled:
            progress_task = asyncio.create_task(send_progress_messages())
        
        # Track this agent as running for this session (for interrupt support)
        # We do this in a callback after the agent is created
        async def track_agent():
            # Wait for agent to be created
            while agent_holder[0] is None:
                await asyncio.sleep(0.05)
            if session_key:
                self._running_agents[session_key] = agent_holder[0]
        
        tracking_task = asyncio.create_task(track_agent())
        
        # Monitor for interrupts from the adapter (new messages arriving)
        async def monitor_for_interrupt():
            adapter = self.adapters.get(source.platform)
            if not adapter or not session_key:
                return
            
            while True:
                await asyncio.sleep(0.2)  # Check every 200ms
                # Check if adapter has a pending interrupt for this session.
                # Must use session_key (build_session_key output) — NOT
                # source.chat_id — because the adapter stores interrupt events
                # under the full session key.
                if hasattr(adapter, 'has_pending_interrupt') and adapter.has_pending_interrupt(session_key):
                    agent = agent_holder[0]
                    if agent:
                        pending_event = adapter.get_pending_message(session_key)
                        pending_text = pending_event.text if pending_event else None
                        logger.debug("Interrupt detected from adapter, signaling agent...")
                        agent.interrupt(pending_text)
                        break
        
        interrupt_monitor = asyncio.create_task(monitor_for_interrupt())
        
        try:
            # Run in thread pool to not block
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, run_sync)
            
            # Check if we were interrupted and have a pending message
            result = result_holder[0]
            adapter = self.adapters.get(source.platform)
            
            # Get pending message from adapter if interrupted.
            # Use session_key (not source.chat_id) to match adapter's storage keys.
            pending = None
            if result and result.get("interrupted") and adapter:
                pending_event = adapter.get_pending_message(session_key) if session_key else None
                if pending_event:
                    pending = pending_event.text
                elif result.get("interrupt_message"):
                    pending = result.get("interrupt_message")
            
            if pending:
                logger.debug("Processing interrupted message: '%s...'", pending[:40])
                
                # Clear the adapter's interrupt event so the next _run_agent call
                # doesn't immediately re-trigger the interrupt before the new agent
                # even makes its first API call (this was causing an infinite loop).
                if adapter and hasattr(adapter, '_active_sessions') and session_key and session_key in adapter._active_sessions:
                    adapter._active_sessions[session_key].clear()
                
                # Don't send the interrupted response to the user — it's just noise
                # like "Operation interrupted." They already know they sent a new
                # message, so go straight to processing it.
                
                # Now process the pending message with updated history
                updated_history = result.get("messages", history)
                return await self._run_agent(
                    message=pending,
                    context_prompt=context_prompt,
                    history=updated_history,
                    source=source,
                    session_id=session_id,
                    session_key=session_key
                )
        finally:
            # Stop progress sender and interrupt monitor
            if progress_task:
                progress_task.cancel()
            interrupt_monitor.cancel()
            
            # Clean up tracking
            tracking_task.cancel()
            if session_key and session_key in self._running_agents:
                del self._running_agents[session_key]

            # Finish the run record
            if _run_id:
                _r = result_holder[0] or {}
                _agent_final = _r.get("final_response") or ""
                _run_error = _r.get("error")
                finish_run(
                    _run_id,
                    status="failed" if _run_error else "success",
                    final_response=_agent_final[:2000] if _agent_final else None,
                    error=str(_run_error)[:500] if _run_error else None,
                    api_calls=_r.get("api_calls", 0),
                    model=_r.get("model"),
                    tool_calls_log=_tool_calls_log or None,
                )

            # Move completed session into the recent ring buffer
            if session_key and session_key in self._session_status:
                completed = self._session_status.pop(session_key)
                import time as _t
                _final = ""
                if result_holder[0]:
                    _txt = result_holder[0].get("final_response", "") or ""
                    _final = _txt[:120].strip()
                self._recent_sessions.append({
                    "session_key": session_key,
                    "platform": completed.get("platform", "unknown"),
                    "current_tool": completed.get("current_tool", ""),
                    "tool_count": completed.get("tool_count", 0),
                    "elapsed_session_s": int(_t.time() - (completed.get("session_started_at") or _t.time())),
                    "ended_at": _t.time(),
                    "snippet": _final,
                })
            
            # Wait for cancelled tasks
            for task in [progress_task, interrupt_monitor, tracking_task]:
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        
        return response


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
