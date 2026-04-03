"""
Knowledge Tool — semantic search over a per-agent knowledge base.

Provides three tools under the ``knowledge`` toolset:

  knowledge_ingest   Chunk and embed a document (text, file, or URL) into
                     the agent's persistent knowledge base.

  knowledge_search   Semantic similarity search.  Returns top-k matching
                     chunks with source attribution and relevance scores.

  knowledge_manage   List ingested sources, remove a source, or show
                     knowledge base statistics.

The knowledge base is scoped to each agent instance via $HERMES_HOME/knowledge/.
Embeddings are generated via an OpenAI-compatible endpoint (Ollama by default).
Search uses in-process numpy cosine similarity — no external vector DB.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from tools.knowledge_store import KnowledgeStore

logger = logging.getLogger(__name__)


# ── Store singleton ─────────────────────────────────────────────────────────

_store: Optional[KnowledgeStore] = None


def _get_store() -> KnowledgeStore:
    """Return (or create) the singleton KnowledgeStore for this agent."""
    global _store
    if _store is None:
        hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
        knowledge_dir = hermes_home / "knowledge"

        # Load config if available
        try:
            from logos_cli.config import load_config
            cfg = load_config().get("knowledge", {})
        except Exception:
            cfg = {}

        _store = KnowledgeStore(
            knowledge_dir=knowledge_dir,
            embedding_model=cfg.get("embedding_model", "nomic-embed-text"),
            embedding_endpoint=cfg.get("embedding_endpoint"),
            embedding_api_key=cfg.get("embedding_api_key"),
            chunk_size=cfg.get("chunk_size", 512),
            chunk_overlap=cfg.get("chunk_overlap", 64),
            max_chunks=cfg.get("max_chunks", 10_000),
        )
    return _store


# ── Handlers ────────────────────────────────────────────────────────────────

def knowledge_ingest(
    source_name: str,
    content: str = "",
    file_path: str = "",
    source_type: str = "text",
) -> str:
    """Ingest text or a file into the knowledge base."""
    store = _get_store()

    # Resolve content from file if provided
    text = content
    if file_path and not text:
        p = Path(file_path).expanduser()
        if not p.exists():
            return json.dumps({"success": False, "error": f"File not found: {file_path}"})
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            source_type = "file"
        except Exception as exc:
            return json.dumps({"success": False, "error": f"Failed to read file: {exc}"})

    if not text:
        return json.dumps({"success": False, "error": "Provide 'content' text or a 'file_path' to ingest."})

    result = store.ingest(text, source_name=source_name, source_type=source_type)
    return json.dumps(result, ensure_ascii=False)


def knowledge_search(query: str, top_k: int = 5) -> str:
    """Search the knowledge base semantically."""
    store = _get_store()
    top_k = max(1, min(top_k, 20))  # clamp
    result = store.search(query, top_k=top_k)
    return json.dumps(result, ensure_ascii=False)


def knowledge_manage(action: str, source_name: str = "") -> str:
    """Manage the knowledge base: list, remove, or stats."""
    store = _get_store()

    if action == "list":
        result = store.list_sources()
    elif action == "remove":
        if not source_name:
            return json.dumps({"success": False, "error": "source_name is required for 'remove'."})
        result = store.remove_source(source_name)
    elif action == "stats":
        result = store.stats()
    else:
        return json.dumps({"success": False, "error": f"Unknown action '{action}'. Use: list, remove, stats"})

    return json.dumps(result, ensure_ascii=False)


# ── Availability check ──────────────────────────────────────────────────────

def check_knowledge_available() -> bool:
    """Knowledge tools are available when the knowledge toolset is enabled."""
    return True


# ── Schemas ─────────────────────────────────────────────────────────────────

KNOWLEDGE_INGEST_SCHEMA = {
    "name": "knowledge_ingest",
    "description": (
        "Add a document to your persistent knowledge base. The document is chunked, "
        "embedded, and stored for semantic search in future sessions.\n\n"
        "Use this when you encounter reference material worth keeping: documentation, "
        "research findings, API specs, code analysis, or any information the user wants "
        "you to remember in detail beyond what fits in your curated memory.\n\n"
        "Provide EITHER 'content' (raw text) OR 'file_path' (path to a file in your workspace)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_name": {
                "type": "string",
                "description": "A short descriptive name for this source (e.g. 'kubernetes-api-docs', 'meeting-notes-april'). Must be unique.",
            },
            "content": {
                "type": "string",
                "description": "The text content to ingest. Use this for pasted text, web scrape results, etc.",
            },
            "file_path": {
                "type": "string",
                "description": "Path to a file to ingest (text, markdown, code). Used when 'content' is not provided.",
            },
        },
        "required": ["source_name"],
    },
}

KNOWLEDGE_SEARCH_SCHEMA = {
    "name": "knowledge_search",
    "description": (
        "Search your knowledge base for information relevant to a query. Returns "
        "the most semantically similar chunks with source attribution and relevance scores.\n\n"
        "Use this when the user asks about something you may have previously ingested, "
        "or when you need reference material from your knowledge base to answer a question.\n\n"
        "Results are ranked by relevance. The knowledge base persists across sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query. Can be a question, topic, or description of what you're looking for.",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (1-20, default 5).",
            },
        },
        "required": ["query"],
    },
}

KNOWLEDGE_MANAGE_SCHEMA = {
    "name": "knowledge_manage",
    "description": (
        "Manage your knowledge base: list ingested sources, remove a source, or view statistics.\n\n"
        "Actions:\n"
        "- 'list': Show all ingested sources with chunk counts and metadata\n"
        "- 'remove': Delete a source and all its chunks (requires source_name)\n"
        "- 'stats': Show total chunks, capacity usage, and embedding model info"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "remove", "stats"],
                "description": "The management action to perform.",
            },
            "source_name": {
                "type": "string",
                "description": "Name of the source to remove (required for 'remove' action).",
            },
        },
        "required": ["action"],
    },
}


# ── Registry ────────────────────────────────────────────────────────────────

from tools.registry import registry

registry.register(
    name="knowledge_ingest",
    toolset="knowledge",
    schema=KNOWLEDGE_INGEST_SCHEMA,
    handler=lambda args, **kw: knowledge_ingest(
        source_name=args.get("source_name", ""),
        content=args.get("content", ""),
        file_path=args.get("file_path", ""),
    ),
    check_fn=check_knowledge_available,
)

registry.register(
    name="knowledge_search",
    toolset="knowledge",
    schema=KNOWLEDGE_SEARCH_SCHEMA,
    handler=lambda args, **kw: knowledge_search(
        query=args.get("query", ""),
        top_k=args.get("top_k", 5),
    ),
    check_fn=check_knowledge_available,
)

registry.register(
    name="knowledge_manage",
    toolset="knowledge",
    schema=KNOWLEDGE_MANAGE_SCHEMA,
    handler=lambda args, **kw: knowledge_manage(
        action=args.get("action", ""),
        source_name=args.get("source_name", ""),
    ),
    check_fn=check_knowledge_available,
)
