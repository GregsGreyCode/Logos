# Multi-Agent Instances & Per-Agent Memory

**Status:** Complete (all phases implemented)  
**Created:** 2026-04-03  
**Implemented:** 2026-04-03  
**Depends on:** Executor architecture (complete), Memory tool (complete), Soul system (complete)

## Problem

Today each user can only run **one agent instance**. The instance name is derived
deterministically from the username (`safe_k8s_name(requester)` → `hermes-greg`),
so spawning a second agent for the same user returns "already exists".

Users want to run multiple specialised agents simultaneously — a researcher, a
coder, a sysadmin — each with its own persistent memory, notes, and learned
context. The infrastructure mostly supports this already; the constraint is a
naming shortcut and shared memory paths.

## Current State

### What already works
- **K8s executor**: each instance gets its own 1Gi PVC at `/home/hermes/.hermes/`
  with a `memories/` directory. If we fix the naming, each agent already has
  isolated storage.
- **Memory tool** (`tools/memory_tool.py`): file-backed `MEMORY.md` + `USER.md`
  under `$HERMES_HOME/memories/`. Injected into system prompt at session start.
- **Bug notes tool** (`tools/bug_notes_tool.py`): `$HERMES_HOME/bug_notes.md`.
- **Soul system**: persona instructions via `soul.md`, toolset policies via
  manifest. Already selected per-instance at spawn time.
- **Init container** (k8s): seeds `config.yaml`, `SOUL.md`, creates `memories/`.

### What's broken or missing
1. **Naming**: `name=safe_k8s_name(requester)` hardcodes one instance per user.
2. **Local executor**: all instances share `~/.hermes/memories/` — no isolation.
3. **Docker executor**: same shared `$HERMES_HOME` problem as local.
4. **No management UI**: no way to browse, edit, or transfer an agent's memories.
5. **No RAG**: memory is a flat bounded list (~2200 chars). No semantic search,
   no large knowledge base, no document ingestion.
6. **No identity persistence**: an agent's "personality" is its soul.md + whatever
   it saves to MEMORY.md. There's no structured identity document per instance.

---

## Design

### Phase 1 — Multi-instance support (naming + storage isolation)

**Goal:** Users can spawn multiple named agents. Each agent gets isolated storage.

#### 1a. Instance naming

The spawn flow needs an **instance label** provided by the user (or auto-generated).

**API change** — `POST /instances`:
```
Current:  { "requester": "greg", "soul_slug": "researcher", ... }
Proposed: { "requester": "greg", "instance_label": "deep-research", "soul_slug": "researcher", ... }
```

Name derivation changes from:
```python
name = safe_k8s_name(requester)                          # hermes-greg
```
to:
```python
label = body.get("instance_label") or soul_slug           # fallback to soul name
name = safe_k8s_name(f"{requester}-{label}")              # hermes-greg-deep-research
```

**UI change**: Add an "Instance name" text field to the spawn dialog (Step 2),
pre-filled with the soul slug. Validate: lowercase alphanumeric + hyphens, max
32 chars. Show the derived k8s name as a preview.

**Duplicate handling**: The k8s executor's early-exit at `kubernetes.py:104`
remains correct — it guards against duplicate *instance names*, which is the
right constraint. The "already exists" message should include the existing
instance name so the user knows which one conflicts.

**Instance limits**: Add a configurable per-user instance cap (default: 5) checked
in `_handle_instances_post` before calling `executor.spawn()`. Prevents runaway
resource consumption.

#### 1b. Storage isolation (local + docker executors)

**Local executor** — each instance already creates
`~/.hermes/workspaces/{instance_name}/`, but `HERMES_HOME` is shared.

Change: set a per-instance `HERMES_HOME`:
```python
instance_home = _HERMES_HOME / "instances" / config.name
instance_home.mkdir(parents=True, exist_ok=True)
env["HERMES_HOME"] = str(instance_home)
```

This gives each agent:
```
~/.hermes/instances/hermes-greg-researcher/
  ├── memories/
  │   ├── MEMORY.md      # agent's notes
  │   └── USER.md        # what it knows about the user
  ├── bug_notes.md
  ├── config.yaml
  └── SOUL.md
```

**Docker executor** — same pattern. Mount a per-instance host directory or named
volume into the container at `/home/hermes/.hermes/`.

**K8s executor** — already isolated via per-instance PVC. No changes needed.

**Migration**: On first spawn with the new naming, check if the legacy
`hermes-{requester}` instance exists. If so, offer to migrate its memories to
the new instance (copy files from old PVC/directory).

#### 1c. Shared user profile

Problem: if each agent has its own `USER.md`, the user has to re-teach preferences
to every new agent. But some knowledge should be per-agent (specialised context).

Solution: **two-tier memory**:
```
~/.hermes/shared/
  └── USER.md                # shared across all agents for this user

~/.hermes/instances/{name}/
  └── memories/
      ├── MEMORY.md          # agent-specific notes (specialisation)
      └── CONTEXT.md         # agent-specific domain knowledge
```

- `USER.md` is loaded from the shared directory, read-only to individual agents.
  Writes to the `user` target go to the shared file.
- `MEMORY.md` stays per-agent. This is where specialisation lives.
- The memory tool gains a third target: `context` — a larger (8000 char) store
  for domain-specific reference material the agent accumulates.

The k8s executor already has a `hermes-shared-memory` PVC mounted read-only at
`/home/hermes/.hermes-shared/`. This becomes the shared user profile location.

---

### Phase 2 — Per-agent knowledge base (RAG)

**Goal:** Each agent can ingest documents and search its knowledge base
semantically. The knowledge base persists across sessions and is scoped to the
individual agent instance.

#### 2a. Storage format

Each agent gets a knowledge directory:
```
~/.hermes/instances/{name}/
  └── knowledge/
      ├── index.json         # metadata: source, ingested_at, chunk_count
      ├── chunks.jsonl        # chunked text with embeddings
      └── sources/            # original documents (optional, for re-indexing)
```

**Chunking strategy**: Fixed-size overlapping chunks (512 tokens, 64 token
overlap). Metadata preserved: source filename, chunk index, section heading if
parseable.

#### 2b. Embedding

**Local-first** (matches Logos philosophy):
- Default: `sentence-transformers/all-MiniLM-L6-v2` via the already-available
  inference endpoint (Ollama supports embeddings). ~23M params, runs on CPU.
- Optional: OpenAI `text-embedding-3-small` for users on the frontier track.

Embedding model is configured in `config.yaml` under a new `knowledge` section:
```yaml
knowledge:
  embedding_model: "all-MiniLM-L6-v2"     # or "text-embedding-3-small"
  embedding_endpoint: null                  # null = use default inference endpoint
  chunk_size: 512
  chunk_overlap: 64
  max_chunks_per_agent: 10000               # ~5M tokens of source material
```

#### 2c. Vector search

**No external vector DB**. Use a simple in-process approach:
- Load chunk embeddings into a numpy array at agent startup.
- Cosine similarity search. For 10k chunks this is <10ms on CPU.
- Top-k results (default k=5) returned with source attribution.

This keeps the system self-contained — no Pinecone, no ChromaDB, no separate
service to manage. If an agent's knowledge base grows beyond what fits in memory,
that's a signal it should be split into multiple specialised agents.

#### 2d. Knowledge tools

Three new tools in a `knowledge` toolset:

| Tool | Description |
|------|-------------|
| `knowledge_ingest` | Ingest a file or URL into the agent's knowledge base. Chunks, embeds, stores. |
| `knowledge_search` | Semantic search over the knowledge base. Returns top-k chunks with sources. |
| `knowledge_manage` | List sources, remove a source, show stats (chunk count, total size). |

**Ingest sources**:
- Local files (from the agent's workspace)
- URLs (via the existing web scraping tool)
- Pasted text (user provides content directly)
- PDF, markdown, plain text, code files

**System prompt integration**: Unlike memory (which is injected into the system
prompt), knowledge search results are returned as tool responses. This keeps the
system prompt stable and the knowledge base unbounded.

#### 2e. Auto-ingest from sessions

At session end (during the existing memory flush), the agent can optionally save
important findings to its knowledge base — not just the curated memory entries,
but raw reference material it encountered (web scrape results, long explanations,
code analysis) that would be useful in future sessions.

This is gated by a config flag:
```yaml
knowledge:
  auto_ingest_sessions: false    # opt-in, can generate significant storage
```

---

### Phase 3 — Management UI

**Goal:** Users can view, edit, and manage each agent's memories and knowledge
from the Logos web interface.

#### 3a. Instance management panel

Extend the existing Instances tab:
- Show all instances for the current user (not just one)
- Each instance card shows: name, soul, model, status, memory usage, knowledge
  base size
- Actions: stop, restart, delete, **inspect**

#### 3b. Memory inspector

Clicking "inspect" on an instance opens a panel with tabs:

| Tab | Contents |
|-----|----------|
| **Memory** | Live view of MEMORY.md entries. Inline edit, add, remove. |
| **User Profile** | Shared USER.md. Edit here, changes visible to all agents. |
| **Context** | Agent-specific CONTEXT.md. Domain knowledge the agent has built up. |
| **Knowledge** | List of ingested sources with chunk counts. Upload new docs. Delete sources. Search preview. |
| **Bug Notes** | Agent's self-reported issues. Mark resolved, add notes. |
| **Config** | Agent-specific config overrides (model, toolsets, policies). |

#### 3c. Memory transfer

When creating a new agent, offer to seed its memory from:
- Another agent's MEMORY.md (copy specialised knowledge)
- A template (pre-written memory entries for common specialisations)
- Blank (start fresh)

This enables a "fork agent" workflow: take your best researcher agent, fork it
into a new instance, and let it specialise further in a sub-domain.

#### 3d. API endpoints

```
GET    /instances/{name}/memory              # read memory + user + context
PUT    /instances/{name}/memory/{target}      # replace entries for a target
GET    /instances/{name}/knowledge            # list sources + stats
POST   /instances/{name}/knowledge/ingest     # upload document for ingestion
DELETE /instances/{name}/knowledge/{source}   # remove a source
GET    /instances/{name}/knowledge/search?q=  # semantic search (debug/preview)
GET    /instances/{name}/config               # agent-specific config
PUT    /instances/{name}/config               # update agent config overrides
```

---

## Implementation Status

| Phase | Scope | Status | Notes |
|-------|-------|--------|-------|
| **1a** | Instance naming | DONE | `instance_label` in POST /instances, `safe_k8s_name(requester, label)` |
| **1b** | Storage isolation | DONE | Per-instance `HERMES_HOME` under `~/.hermes/instances/{name}/` |
| **1c** | Shared user profile | DONE | `HERMES_SHARED_HOME` env var, shared `USER.md` |
| **2a-c** | Knowledge base + RAG | DONE | `tools/knowledge_store.py` — chunking, Ollama embeddings, numpy cosine search |
| **2d** | Knowledge tools | DONE | `knowledge_ingest`, `knowledge_search`, `knowledge_manage` in `knowledge` toolset |
| **2e** | Auto-ingest | DONE | Gated by `knowledge.auto_ingest_sessions` config flag; extracts assistant turns on session expiry |
| **3a-b** | Management UI | DONE | Inspector panel with Memory/User Profile/Knowledge/Bug Notes tabs |
| **3c** | Memory transfer | DONE | Fork agent via `POST /instances/{name}/fork`; copies MEMORY.md + knowledge/ |
| **3d** | Management API | DONE | REST endpoints for memory read/write, knowledge CRUD, search preview |

---

## Key Decisions to Make

1. **Instance limit per user**: Default 5? Configurable per role?
2. **Auto-naming vs user-naming**: Should the UI require a name or generate one
   from soul + timestamp? Recommendation: pre-fill with soul slug, let user edit.
3. **Shared USER.md writability**: Should agents write to the shared profile, or
   should only the management UI do that? Risk: agents overwriting each other's
   observations. Recommendation: agents can write; last-write-wins with append
   semantics is fine for curated entries.
4. **Embedding model**: Ship with local-only (MiniLM) or support cloud embeddings
   from day one? Recommendation: local-only in Phase 2, cloud as follow-up.
5. **Knowledge base size cap**: Per-agent cap on chunks to prevent storage bloat?
   Recommendation: 10k chunks (~5MB embeddings) default, configurable.
6. **Memory flush scope**: When an agent session expires, should the memory flush
   also update the shared USER.md? Recommendation: yes, but only for the `user`
   target — agent-specific discoveries go to MEMORY.md.
