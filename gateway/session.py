"""
Session management for the gateway.

Handles:
- Session context tracking (where messages come from)
- Session storage (conversations persisted to disk)
- Reset policy evaluation (when to start fresh)
- Dynamic system prompt injection (agent knows its context)
"""

import logging
import os
import json
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

from .config import (
    Platform,
    GatewayConfig,
    SessionResetPolicy,
    HomeChannel,
)


@dataclass
class SessionSource:
    """
    Describes where a message originated from.
    
    This information is used to:
    1. Route responses back to the right place
    2. Inject context into the system prompt
    3. Track origin for cron job delivery
    """
    platform: Platform
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"  # "dm", "group", "channel", "thread"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None  # For forum topics, Discord threads, etc.
    chat_topic: Optional[str] = None  # Channel topic/description (Discord, Slack)
    user_id_alt: Optional[str] = None  # Signal UUID (alternative to phone number)
    chat_id_alt: Optional[str] = None  # Signal group internal ID
    # When set, identifies the target agent directly: the adapter that
    # received the update is owned by that agent (per-agent credentials).
    # dispatch_platform_message prefers this over the platform_routing
    # table lookup. None = legacy path (global env token, routing table
    # decides which agent handles the message).
    agent_id: Optional[str] = None
    
    @property
    def description(self) -> str:
        """Human-readable description of the source."""
        if self.platform == Platform.LOCAL:
            return "CLI terminal"
        
        parts = []
        if self.chat_type == "dm":
            parts.append(f"DM with {self.user_name or self.user_id or 'user'}")
        elif self.chat_type == "group":
            parts.append(f"group: {self.chat_name or self.chat_id}")
        elif self.chat_type == "channel":
            parts.append(f"channel: {self.chat_name or self.chat_id}")
        else:
            parts.append(self.chat_name or self.chat_id)
        
        if self.thread_id:
            parts.append(f"thread: {self.thread_id}")
        
        return ", ".join(parts)
    
    def to_dict(self) -> Dict[str, Any]:
        d = {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "thread_id": self.thread_id,
            "chat_topic": self.chat_topic,
        }
        if self.user_id_alt:
            d["user_id_alt"] = self.user_id_alt
        if self.chat_id_alt:
            d["chat_id_alt"] = self.chat_id_alt
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionSource":
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            chat_name=data.get("chat_name"),
            chat_type=data.get("chat_type", "dm"),
            user_id=data.get("user_id"),
            user_name=data.get("user_name"),
            thread_id=data.get("thread_id"),
            chat_topic=data.get("chat_topic"),
            user_id_alt=data.get("user_id_alt"),
            chat_id_alt=data.get("chat_id_alt"),
        )
    
    @classmethod
    def local_cli(cls) -> "SessionSource":
        """Create a source representing the local CLI."""
        return cls(
            platform=Platform.LOCAL,
            chat_id="cli",
            chat_name="CLI terminal",
            chat_type="dm",
        )


@dataclass
class SessionContext:
    """
    Full context for a session, used for dynamic system prompt injection.

    The agent receives this information to understand:
    - Where messages are coming from
    - What platforms are available
    - Where it can deliver scheduled task outputs
    """
    source: SessionSource
    connected_platforms: List[Platform]
    home_channels: Dict[Platform, HomeChannel]

    # Session metadata
    session_key: str = ""
    session_id: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Deployment environment
    runtime_mode: str = "openshell"   # Only "openshell" is supported now — field kept for prompt/debug readouts
    host_platform: str = "linux"  # "linux" | "windows" | "darwin"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "connected_platforms": [p.value for p in self.connected_platforms],
            "home_channels": {
                p.value: hc.to_dict() for p, hc in self.home_channels.items()
            },
            "session_key": self.session_key,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def build_capability_manifest(context: SessionContext) -> str:
    """Build a structured capability section for the agent system prompt.

    Summarises messaging platforms, inference backend, MCP tools,
    tool integrations, sandbox runtime, and any constraints — all
    derived from live state rather than hardcoded checks.
    """
    sections = []

    # Messaging
    messaging = [p.value for p in context.connected_platforms if p != Platform.LOCAL]
    if messaging:
        sections.append(f"**Messaging:** {', '.join(messaging)} (connected)")
    else:
        sections.append("**Messaging:** web only (no external platforms connected)")

    # Inference
    model = os.environ.get("HERMES_MODEL") or os.environ.get("LLM_MODEL") or "unknown"
    backend = os.environ.get("HERMES_SERVER_TYPE") or "local"
    sections.append(f"**Inference:** {model} via {backend}")

    # MCP tools
    try:
        from gateway.auth.db import list_mcp_servers
        mcp_servers = list_mcp_servers()
        running = [s["name"] for s in mcp_servers if s.get("enabled")]
        if running:
            sections.append(f"**MCP Tools:** {', '.join(running)}")
    except Exception:
        pass

    # Tool integrations
    try:
        from gateway.services import get_tool_integrations
        tools = [t["label"] for t in get_tool_integrations() if t.get("has_key")]
        if tools:
            sections.append(f"**Integrations:** {', '.join(tools)}")
    except Exception:
        pass

    # Sandbox runtime
    runtime = context.runtime_mode or "local"
    sections.append(f"**Sandbox:** {runtime}")

    # Constraints
    constraints = []
    if not messaging:
        constraints.append(
            "No external messaging — cannot deliver notifications or scheduled "
            "messages outside the web UI. Do not suggest platform setup unprompted. "
            "If the user asks, mention that messaging platforms can be added in Settings."
        )
    if constraints:
        sections.append("**Constraints:** " + " ".join(constraints))

    return "## Available Capabilities\n\n" + "\n".join(sections)


def build_session_context_prompt(context: SessionContext) -> str:
    """
    Build the dynamic system prompt section that tells the agent about its context.
    
    This is injected into the system prompt so the agent knows:
    - Where messages are coming from
    - What platforms are connected
    - Where it can deliver scheduled task outputs
    """
    lines = [
        "## Current Session Context",
        "",
    ]

    # Current time. Cheap, single line — gives the agent unambiguous
    # awareness of "now" without needing a tool call. Format includes
    # ISO date+time AND a human-readable weekday so the agent can
    # reason about both. Uses the gateway server's local timezone via
    # datetime.now().astimezone(); if the server is UTC the agent
    # gets UTC and can convert if needed. A future enhancement could
    # accept the user's timezone in the request body for client-local
    # awareness — for now, server time is good enough for the "what
    # time / day is it?" use case the user raised.
    from datetime import datetime as _dt
    _now = _dt.now().astimezone()
    _tzname = _now.tzname() or "UTC"
    lines.append(f"**Current time:** {_now.strftime('%A %Y-%m-%d %H:%M:%S')} {_tzname}")

    # Deployment environment — OpenShell is the only supported runtime now
    platform_label = {"windows": "Windows", "darwin": "macOS"}.get(context.host_platform, "Linux")
    lines.append(f"**Deployment:** OpenShell ({platform_label})")

    # Source info
    platform_name = context.source.platform.value.title()
    if context.source.platform == Platform.LOCAL:
        lines.append(f"**Source:** {platform_name} (the machine running this agent)")
    else:
        lines.append(f"**Source:** {platform_name} ({context.source.description})")
    
    # Channel topic (if available - provides context about the channel's purpose)
    if context.source.chat_topic:
        lines.append(f"**Channel Topic:** {context.source.chat_topic}")

    # User identity (especially useful for WhatsApp where multiple people DM)
    if context.source.user_name:
        lines.append(f"**User:** {context.source.user_name}")
    elif context.source.user_id:
        lines.append(f"**User ID:** {context.source.user_id}")
    
    # Platform-specific behavioral notes
    if context.source.platform == Platform.SLACK:
        lines.append("")
        lines.append(
            "**Platform notes:** You are running inside Slack. "
            "You do NOT have access to Slack-specific APIs — you cannot search "
            "channel history, pin/unpin messages, manage channels, or list users. "
            "Do not promise to perform these actions. If the user asks, explain "
            "that you can only read messages sent directly to you and respond."
        )
    elif context.source.platform == Platform.DISCORD:
        lines.append("")
        lines.append(
            "**Platform notes:** You are running inside Discord. "
            "You do NOT have access to Discord-specific APIs — you cannot search "
            "channel history, pin messages, manage roles, or list server members. "
            "Do not promise to perform these actions. If the user asks, explain "
            "that you can only read messages sent directly to you and respond."
        )

    # Capability manifest — structured summary of available capabilities.
    lines.append("")
    lines.append(build_capability_manifest(context))

    # Home channels
    if context.home_channels:
        lines.append("")
        lines.append("**Home Channels (default destinations):**")
        for platform, home in context.home_channels.items():
            lines.append(f"  - {platform.value}: {home.name} (ID: {home.chat_id})")
    
    # Delivery options for scheduled tasks
    lines.append("")
    lines.append("**Delivery options for scheduled tasks:**")
    
    # Origin delivery
    if context.source.platform == Platform.LOCAL:
        lines.append("- `\"origin\"` → Local output (saved to files)")
    else:
        lines.append(f"- `\"origin\"` → Back to this chat ({context.source.chat_name or context.source.chat_id})")
    
    # Local always available
    lines.append("- `\"local\"` → Save to local files only (~/.hermes/cron/output/)")
    
    # Platform home channels
    for platform, home in context.home_channels.items():
        lines.append(f"- `\"{platform.value}\"` → Home channel ({home.name})")
    
    # Note about explicit targeting
    lines.append("")
    lines.append("*For explicit targeting, use `\"platform:chat_id\"` format if the user provides a specific chat ID.*")

    return "\n".join(lines)


def build_agent_system_prompt(agent_record: dict, session_context_prompt: str) -> str:
    """Compose the FULL system prompt sent to a sandbox worker.

    The sandbox worker is a thin transport: it ships whatever
    `context_prompt` it receives straight to inference. So the gateway
    has to assemble:

      1. Identity preamble — "You are {name}." Without this the model
         has no idea what name it's wearing and answers "I'm an AI
         assistant" when asked. The user noticed this regression after
         the move to sandboxed agents because the legacy in-process
         path baked the name into a different prompt builder that no
         longer runs.
      2. Soul markdown — the persona definition (souls/<slug>/soul.md).
         Loaded from the in-process soul registry; falls back gracefully
         if the slug is unknown or has no soul.md on disk.
      3. Description — the per-agent free-text description from the
         agents table (if any), so users can customise voice without
         editing soul files.
      4. Session context — deployment, source, capability manifest,
         delivery options. Built by ``build_session_context_prompt``.
    """
    name = (agent_record or {}).get("name") or "Agent"
    soul_slug = (agent_record or {}).get("soul_slug") or ""
    description = ((agent_record or {}).get("description") or "").strip()

    parts: list[str] = []
    # Identity preamble — just one natural-language line.
    #
    # Earlier this had a multi-paragraph "you must respond with X,
    # this is non-negotiable" follow-up to defeat a one-off
    # qwen3.5-9b hallucination ("Hermes" → "I'm Ani"). That worked
    # for qwen but caught fire on gpt-oss-20b: the OpenAI Harmony
    # chat format treats developer-constraint language as a signal
    # to route the response through the `commentary` channel, and
    # LM Studio's OpenAI-compat shim doesn't strip the channel
    # framing — so users saw raw `<|channel|>commentary to=developer
    # ... You must say "Michael". Michael` tokens leak into the
    # response.
    #
    # The fix is to drop the constraint language entirely and rely
    # on the standard OpenAI-style "You are X." preamble. If a
    # specific name still trips a model's training bias (the
    # original qwen + "Hermes" case), the user can either rename
    # the agent or pin it to a different model.
    parts.append(f"You are {name}.")
    parts.append("")

    # World awareness — self appearance + peer roster. Lets an agent
    # answer "what do you look like?" correctly and know who else is
    # around without waiting for a tool call. Best-effort: a failure
    # here must never block dispatch, and the snippet is skipped on
    # single-agent installs (no peers → nothing added beyond the self
    # description, which the renderer also decides).
    try:
        from gateway.world_awareness import render_self_and_peers_prompt
        awareness = render_self_and_peers_prompt(agent_record)
        if awareness:
            parts.append(awareness)
            parts.append("")
    except Exception as exc:
        logger.debug("world_awareness failed (non-fatal): %s", exc)

    # Soul persona — pulled from the registry, not from disk per-call,
    # so this is fast even on the dispatch hot path.
    if soul_slug:
        try:
            from gateway.souls import get_soul_registry
            soul = get_soul_registry().get(soul_slug)
            if soul and soul.soul_md:
                parts.append(soul.soul_md.strip())
                parts.append("")
        except Exception:
            pass

    if description:
        parts.append(f"## About you")
        parts.append("")
        parts.append(description)
        parts.append("")

    if session_context_prompt:
        parts.append(session_context_prompt)

    return "\n".join(parts).strip()


@dataclass
class SessionEntry:
    """
    Entry in the session store.
    
    Maps a session key to its current session ID and metadata.
    """
    session_key: str
    session_id: str
    created_at: datetime
    updated_at: datetime
    
    # Origin metadata for delivery routing
    origin: Optional[SessionSource] = None
    
    # Display metadata
    display_name: Optional[str] = None
    platform: Optional[Platform] = None
    chat_type: str = "dm"
    
    # Token tracking
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    
    # Last API-reported prompt tokens (for accurate compression pre-check)
    last_prompt_tokens: int = 0
    
    # Set when a session was created because the previous one expired;
    # consumed once by the message handler to inject a notice into context
    was_auto_reset: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "session_key": self.session_key,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "display_name": self.display_name,
            "platform": self.platform.value if self.platform else None,
            "chat_type": self.chat_type,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
        }
        if self.origin:
            result["origin"] = self.origin.to_dict()
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionEntry":
        origin = None
        if "origin" in data and data["origin"]:
            origin = SessionSource.from_dict(data["origin"])
        
        platform = None
        if data.get("platform"):
            try:
                platform = Platform(data["platform"])
            except ValueError as e:
                logger.debug("Unknown platform value %r: %s", data["platform"], e)
        
        return cls(
            session_key=data["session_key"],
            session_id=data["session_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            origin=origin,
            display_name=data.get("display_name"),
            platform=platform,
            chat_type=data.get("chat_type", "dm"),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            last_prompt_tokens=data.get("last_prompt_tokens", 0),
        )


def build_session_key(source: SessionSource) -> str:
    """Build a deterministic session key from a message source.

    This is the single source of truth for session key construction.

    DM rules:
      - WhatsApp DMs include chat_id (multi-user support).
      - Other DMs include thread_id when present (e.g. Slack threaded DMs),
        so each DM thread gets its own session while top-level DMs share one.
      - Without thread_id or chat_id, all DMs share a single session.

    Group/channel rules:
      - thread_id differentiates threads within a channel.
      - Without thread_id, all messages in a channel share one session.
    """
    platform = source.platform.value
    if source.chat_type == "dm":
        if source.thread_id:
            return f"agent:main:{platform}:dm:{source.thread_id}"
        # Whenever the source has a chat_id, key the session by it.
        # Earlier this was guarded by `platform == "whatsapp"` and
        # everything else (including local DMs from /chat) fell through
        # to the bare `agent:main:{platform}:dm` key — meaning EVERY
        # local-platform chat shared the same session_key, the same
        # transcript, and therefore the same conversation history.
        # The user hit this with two agents (Hilo + Michael): Hilo's
        # worker loaded Michael's transcript and qwen3.5-9b parroted
        # "I am Michael" because the history contained Michael's
        # earlier turns. Fix is to include chat_id whenever it exists,
        # not just for WhatsApp.
        if source.chat_id:
            return f"agent:main:{platform}:dm:{source.chat_id}"
        return f"agent:main:{platform}:dm"
    if source.thread_id:
        return f"agent:main:{platform}:{source.chat_type}:{source.chat_id}:{source.thread_id}"
    return f"agent:main:{platform}:{source.chat_type}:{source.chat_id}"


class SessionStore:
    """
    Manages session storage and retrieval.

    SQLite (via SessionDB) is the sole session store.  Legacy JSONL files
    are no longer written; the JSONL read path is retained for one-time
    migration of existing transcript data.
    """
    
    def __init__(self, sessions_dir: Path, config: GatewayConfig,
                 has_active_processes_fn=None,
                 on_auto_reset=None):
        self.sessions_dir = sessions_dir
        self.config = config
        self._entries: Dict[str, SessionEntry] = {}
        self._loaded = False
        self._has_active_processes_fn = has_active_processes_fn
        # on_auto_reset is deprecated — memory flush now runs proactively
        # via the background session expiry watcher in GatewayRunner.
        self._pre_flushed_sessions: set = set()  # session_ids already flushed by watcher
        
        # Initialize SQLite session database
        self._db = None
        try:
            from core.state import SessionDB
            self._db = SessionDB()
        except Exception as e:
            print(f"[gateway] Warning: SQLite session store unavailable, falling back to JSONL: {e}")
    
    def _ensure_loaded(self) -> None:
        """Load sessions index from disk if not already loaded."""
        if self._loaded:
            return
        
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_file = self.sessions_dir / "sessions.json"
        
        if sessions_file.exists():
            try:
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for key, entry_data in data.items():
                        try:
                            self._entries[key] = SessionEntry.from_dict(entry_data)
                        except (ValueError, KeyError):
                            # Skip entries with unknown/removed platform values
                            continue
            except Exception as e:
                print(f"[gateway] Warning: Failed to load sessions: {e}")
        
        self._loaded = True
    
    def _save(self) -> None:
        """Save sessions index to disk (kept for session key -> ID mapping)."""
        import tempfile
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_file = self.sessions_dir / "sessions.json"

        data = {key: entry.to_dict() for key, entry in self._entries.items()}
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.sessions_dir), suffix=".tmp", prefix=".sessions_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, sessions_file)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError as e:
                logger.debug("Could not remove temp file %s: %s", tmp_path, e)
            raise
    
    def _generate_session_key(self, source: SessionSource) -> str:
        """Generate a session key from a source."""
        return build_session_key(source)
    
    def _is_session_expired(self, entry: SessionEntry) -> bool:
        """Check if a session has expired based on its reset policy.
        
        Works from the entry alone — no SessionSource needed.
        Used by the background expiry watcher to proactively flush memories.
        Sessions with active background processes are never considered expired.
        """
        if self._has_active_processes_fn:
            if self._has_active_processes_fn(entry.session_key):
                return False

        policy = self.config.get_reset_policy(
            platform=entry.platform,
            session_type=entry.chat_type,
        )

        if policy.mode == "none":
            return False

        now = datetime.now()

        if policy.mode in ("idle", "both"):
            idle_deadline = entry.updated_at + timedelta(minutes=policy.idle_minutes)
            if now > idle_deadline:
                return True

        if policy.mode in ("daily", "both"):
            today_reset = now.replace(
                hour=policy.at_hour,
                minute=0, second=0, microsecond=0,
            )
            if now.hour < policy.at_hour:
                today_reset -= timedelta(days=1)
            if entry.updated_at < today_reset:
                return True

        return False

    def _should_reset(self, entry: SessionEntry, source: SessionSource) -> bool:
        """
        Check if a session should be reset based on policy.
        
        Sessions with active background processes are never reset.
        """
        if self._has_active_processes_fn:
            session_key = self._generate_session_key(source)
            if self._has_active_processes_fn(session_key):
                return False

        policy = self.config.get_reset_policy(
            platform=source.platform,
            session_type=source.chat_type
        )
        
        if policy.mode == "none":
            return False
        
        now = datetime.now()
        
        if policy.mode in ("idle", "both"):
            idle_deadline = entry.updated_at + timedelta(minutes=policy.idle_minutes)
            if now > idle_deadline:
                return True
        
        if policy.mode in ("daily", "both"):
            today_reset = now.replace(
                hour=policy.at_hour, 
                minute=0, 
                second=0, 
                microsecond=0
            )
            if now.hour < policy.at_hour:
                today_reset -= timedelta(days=1)
            
            if entry.updated_at < today_reset:
                return True
        
        return False
    
    def has_any_sessions(self) -> bool:
        """Check if any sessions have ever been created (across all platforms).

        Uses the SQLite database as the source of truth because it preserves
        historical session records (ended sessions still count).  The in-memory
        ``_entries`` dict replaces entries on reset, so ``len(_entries)`` would
        stay at 1 for single-platform users — which is the bug this fixes.

        The current session is already in the DB by the time this is called
        (get_or_create_session runs first), so we check ``> 1``.
        """
        if self._db:
            try:
                return self._db.session_count() > 1
            except Exception:
                pass  # fall through to heuristic
        # Fallback: check if sessions.json was loaded with existing data.
        # This covers the rare case where the DB is unavailable.
        self._ensure_loaded()
        return len(self._entries) > 1
    
    def get_or_create_session(
        self, 
        source: SessionSource,
        force_new: bool = False
    ) -> SessionEntry:
        """
        Get an existing session or create a new one.
        
        Evaluates reset policy to determine if the existing session is stale.
        Creates a session record in SQLite when a new session starts.
        """
        self._ensure_loaded()
        
        session_key = self._generate_session_key(source)
        now = datetime.now()
        
        if session_key in self._entries and not force_new:
            entry = self._entries[session_key]
            
            if not self._should_reset(entry, source):
                entry.updated_at = now
                self._save()
                return entry
            else:
                # Session is being auto-reset.  The background expiry watcher
                # should have already flushed memories proactively; discard
                # the marker so it doesn't accumulate.
                was_auto_reset = True
                self._pre_flushed_sessions.discard(entry.session_id)
                if self._db:
                    try:
                        self._db.end_session(entry.session_id, "session_reset")
                    except Exception as e:
                        logger.debug("Session DB operation failed: %s", e)
        else:
            was_auto_reset = False
        
        # Create new session
        session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        
        entry = SessionEntry(
            session_key=session_key,
            session_id=session_id,
            created_at=now,
            updated_at=now,
            origin=source,
            display_name=source.chat_name,
            platform=source.platform,
            chat_type=source.chat_type,
            was_auto_reset=was_auto_reset,
        )
        
        self._entries[session_key] = entry
        self._save()
        
        # Create session in SQLite
        if self._db:
            try:
                self._db.create_session(
                    session_id=session_id,
                    source=source.platform.value,
                    user_id=source.user_id,
                )
            except Exception as e:
                print(f"[gateway] Warning: Failed to create SQLite session: {e}")
        
        return entry
    
    def update_session(
        self, 
        session_key: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        last_prompt_tokens: int = None,
        model: str = None,
    ) -> None:
        """Update a session's metadata after an interaction."""
        self._ensure_loaded()
        
        if session_key in self._entries:
            entry = self._entries[session_key]
            entry.updated_at = datetime.now()
            entry.input_tokens += input_tokens
            entry.output_tokens += output_tokens
            if last_prompt_tokens is not None:
                entry.last_prompt_tokens = last_prompt_tokens
            entry.total_tokens = entry.input_tokens + entry.output_tokens
            self._save()
            
            if self._db:
                try:
                    self._db.update_token_counts(
                        entry.session_id, input_tokens, output_tokens,
                        model=model,
                    )
                except Exception as e:
                    logger.debug("Session DB operation failed: %s", e)
    
    def reset_session(self, session_key: str) -> Optional[SessionEntry]:
        """Force reset a session, creating a new session ID."""
        self._ensure_loaded()
        
        if session_key not in self._entries:
            return None
        
        old_entry = self._entries[session_key]
        
        # End old session in SQLite
        if self._db:
            try:
                self._db.end_session(old_entry.session_id, "session_reset")
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)
        
        now = datetime.now()
        session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        
        new_entry = SessionEntry(
            session_key=session_key,
            session_id=session_id,
            created_at=now,
            updated_at=now,
            origin=old_entry.origin,
            display_name=old_entry.display_name,
            platform=old_entry.platform,
            chat_type=old_entry.chat_type,
        )
        
        self._entries[session_key] = new_entry
        self._save()
        
        # Create new session in SQLite
        if self._db:
            try:
                self._db.create_session(
                    session_id=session_id,
                    source=old_entry.platform.value if old_entry.platform else "unknown",
                    user_id=old_entry.origin.user_id if old_entry.origin else None,
                )
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)
        
        return new_entry

    def switch_session(self, session_key: str, target_session_id: str) -> Optional[SessionEntry]:
        """Switch a session key to point at an existing session ID.

        Used by ``/resume`` to restore a previously-named session.
        Ends the current session in SQLite (like reset), but instead of
        generating a fresh session ID, re-uses ``target_session_id`` so the
        old transcript is loaded on the next message.
        """
        self._ensure_loaded()

        if session_key not in self._entries:
            return None

        old_entry = self._entries[session_key]

        # Don't switch if already on that session
        if old_entry.session_id == target_session_id:
            return old_entry

        # End the current session in SQLite
        if self._db:
            try:
                self._db.end_session(old_entry.session_id, "session_switch")
            except Exception as e:
                logger.debug("Session DB end_session failed: %s", e)

        now = datetime.now()
        new_entry = SessionEntry(
            session_key=session_key,
            session_id=target_session_id,
            created_at=now,
            updated_at=now,
            origin=old_entry.origin,
            display_name=old_entry.display_name,
            platform=old_entry.platform,
            chat_type=old_entry.chat_type,
        )

        self._entries[session_key] = new_entry
        self._save()
        return new_entry

    def list_sessions(self, active_minutes: Optional[int] = None) -> List[SessionEntry]:
        """List all sessions, optionally filtered by activity."""
        self._ensure_loaded()
        
        entries = list(self._entries.values())
        
        if active_minutes is not None:
            cutoff = datetime.now() - timedelta(minutes=active_minutes)
            entries = [e for e in entries if e.updated_at >= cutoff]
        
        entries.sort(key=lambda e: e.updated_at, reverse=True)
        
        return entries
    
    def get_transcript_path(self, session_id: str) -> Path:
        """Get the path to a session's legacy transcript file."""
        return self.sessions_dir / f"{session_id}.jsonl"
    
    def append_to_transcript(self, session_id: str, message: Dict[str, Any], skip_db: bool = False) -> None:
        """Append a message to a session's transcript (SQLite only).

        Args:
            skip_db: When True, skip the SQLite write.  Used when the agent
                     already persisted messages via _flush_messages_to_session_db(),
                     preventing duplicate writes (bug #860).
        """
        if skip_db:
            return
        if self._db:
            try:
                self._db.append_message(
                    session_id=session_id,
                    role=message.get("role", "unknown"),
                    content=message.get("content"),
                    tool_name=message.get("tool_name"),
                    tool_calls=message.get("tool_calls"),
                    tool_call_id=message.get("tool_call_id"),
                )
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)
    
    def rewrite_transcript(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Replace the entire transcript for a session with new messages.

        Used by /retry, /undo, and /compress to persist modified conversation history.
        """
        if self._db:
            try:
                self._db.clear_messages(session_id)
                for msg in messages:
                    self._db.append_message(
                        session_id=session_id,
                        role=msg.get("role", "unknown"),
                        content=msg.get("content"),
                        tool_name=msg.get("tool_name"),
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                    )
            except Exception as e:
                logger.debug("Failed to rewrite transcript in DB: %s", e)

    def load_transcript(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages from a session's transcript."""
        # Try SQLite first
        if self._db:
            try:
                messages = self._db.get_messages_as_conversation(session_id)
                if messages:
                    return messages
            except Exception as e:
                logger.debug("Could not load messages from DB: %s", e)
        
        # Fall back to legacy JSONL
        transcript_path = self.get_transcript_path(session_id)
        
        if not transcript_path.exists():
            return []
        
        messages = []
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
        
        return messages


def build_session_context(
    source: SessionSource,
    config: GatewayConfig,
    session_entry: Optional[SessionEntry] = None
) -> SessionContext:
    """
    Build a full session context from a source and config.
    
    This is used to inject context into the agent's system prompt.
    """
    connected = config.get_connected_platforms()
    
    home_channels = {}
    for platform in connected:
        home = config.get_home_channel(platform)
        if home:
            home_channels[platform] = home
    
    import sys as _sys
    context = SessionContext(
        source=source,
        connected_platforms=connected,
        home_channels=home_channels,
        runtime_mode="openshell",
        host_platform="windows" if _sys.platform == "win32" else ("darwin" if _sys.platform == "darwin" else "linux"),
    )
    
    if session_entry:
        context.session_key = session_entry.session_key
        context.session_id = session_entry.session_id
        context.created_at = session_entry.created_at
        context.updated_at = session_entry.updated_at
    
    return context
