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

        # WorkerRegistry tracks connected OpenShell sandbox workers over
        # WebSocket (/ws/worker). It lives on the runner, not on the HTTP
        # layer, because the runner has a longer lifecycle and the HTTP
        # /chat endpoint needs to route tasks through it.
        # http_api.start_http_api reads it from the runner at boot rather
        # than creating its own copy.
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

        # LOG-44.3 — Logos gateway no longer runs channel adapters
        # in-process. Each agent's hermes server running inside its
        # sandbox hosts its own TG/Discord/Slack/etc bot. Credentials
        # still land in the DB here, but the bot connection happens in
        # the sandbox via hermes's own gateway/platforms/* adapters,
        # keyed off the env vars written by
        # hermes_server_mode.build_channel_extra_env at spawn / credential
        # refresh. The old in-gateway adapter loop (both legacy
        # env-token and per-agent-credential variants) is deleted.
        logger.info("Gateway running — channels handled in per-agent sandboxes (LOG-44.3).")

        self._running = True
        
        # Emit gateway:startup hook
        hook_count = len(self.hooks.loaded_hooks)
        if hook_count:
            logger.info("%s hook(s) loaded", hook_count)
        await self.hooks.emit("gateway:startup", {
            "platforms": [],
        })

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
        """Stop the gateway. LOG-44.3 — no adapters to disconnect."""
        logger.info("Stopping gateway...")
        self._running = False

        self._shutdown_event.set()
        _set_current_runner(None)
        _runtime_state.set_current_loop(None)

        from gateway.status import remove_pid_file
        remove_pid_file()

        logger.info("Gateway stopped")
    
    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()
    
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



def _start_maintenance_ticker(stop_event: threading.Event, adapters=None, interval: int = 60):
    """Background thread that handles periodic maintenance inside the gateway.

    Refreshes the channel directory every 5 minutes and prunes the
    image/audio/document caches once per hour. The old cron scheduler
    was removed with the rest of the in-process AIAgent paths — agents
    schedule their own work inside their sandboxes now (hermes upstream
    boot hooks + sandbox-side cron tools).
    """
    from gateway.media_cache import cleanup_image_cache, cleanup_document_cache

    IMAGE_CACHE_EVERY = 60   # ticks — once per hour at default 60s interval
    CHANNEL_DIR_EVERY = 5    # ticks — every 5 minutes

    logger.info("Maintenance ticker started (interval=%ds)", interval)
    tick_count = 0
    while not stop_event.is_set():
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

    # Start background maintenance ticker — channel directory refresh +
    # image/document cache prune. Cron scheduling itself lives in the
    # agent sandboxes, not in Logos.
    cron_stop = threading.Event()
    cron_thread = threading.Thread(
        target=_start_maintenance_ticker,
        args=(cron_stop,),
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
