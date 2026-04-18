# Souls — authoring layout

Each subdirectory under `souls/` defines one agent persona. The registry
is loaded from this directory at gateway startup (`gateway/souls.py`)
and exposed to the admin UI as the soul picker. Directory names are
the slugs the UI displays (`general`, `news-anchor`, etc).

## Per-soul files

A soul directory must contain:

- **`soul.md`** — the persona text. Becomes the agent's system prompt
  (prepended with the `_shared/` fragments below and the per-session
  context block). Write in second person: *"You are …"*.
- **`soul.manifest.yaml`** — metadata + tool defaults. Required fields
  are handled by `SoulManifest` in `gateway/souls.py`; the existing
  souls are the canonical reference.

And may optionally contain:

- **`boot.md`** — instructions the agent runs once on **every gateway
  startup** inside its sandbox. Uploaded by `deploy_boot_md` in
  `gateway/executors/hermes_server_mode.py` as `~/.hermes/BOOT.md`; the
  built-in `boot_md` hook (shipped with the sandbox image) picks it up
  and runs a one-shot `AIAgent` over it. Ideal for housekeeping:
  > On startup:
  > 1. If `~/outputs/news/` has nothing from yesterday, run the daily
  >    digest and save there.
  > 2. Create a 5-minute heartbeat cron if not already scheduled.
  >
  > Reply with `[SILENT]` if nothing needed doing.

  A soul with no `boot.md` has no boot behavior — the spawn explicitly
  clears any stale `BOOT.md` so changing your mind mid-life actually
  takes effect on next respawn. Keep boot steps short; the hook caps
  at 20 iterations and reply with `[SILENT]` when there's nothing to
  say so the log stays clean.

## Shared fragments

`_shared/` holds guidance that applies to every soul regardless of
persona — filesystem layout, tool-use conventions, citation rules.
Every `*.md` file in that directory is concatenated and appended to
every soul's `soul.md` at load time (`gateway/souls.py::_load_shared_fragments`),
so you don't have to copy the same paragraphs into ten souls. Keep it
short and general — anything soul-specific belongs in the soul's own
directory.

## Hot-reload

Editing `soul.md` / `soul.manifest.yaml` / `_shared/*.md` hot-reloads
the in-process soul registry on save from the admin UI; the next agent
dispatch picks up the new manifest without a gateway restart. Editing
`boot.md` only takes effect on the next **sandbox spawn** for agents
using that soul (the boot hook reads `BOOT.md` at gateway startup
inside the sandbox, and `deploy_boot_md` only runs during
`enable_hermes_server_mode` at spawn).
