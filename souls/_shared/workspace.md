# Where to save things

You have write access to four paths inside your sandbox (OpenShell's filesystem policy grants these; everything else is read-only or permission-denied):

- `/tmp/hermes` — your `$HOME` (`~`). Default home for the directories below.
- `/sandbox` — workspace root; persistent across the sandbox's lifetime.
- `/home/sandbox` — Unix-conventional home for your user (`sandbox`, uid 10001).
- `/tmp` — world-writable ephemeral scratch.

Prefer `~` (= `/tmp/hermes`) for anything the user might want to browse — that's where the UI's Files panel opens by default. Put the things you create in predictable subdirectories of home so the user can find them:

- `~/scripts/` — standalone scripts you write and want to reuse (e.g. `generate_newsletter.py`, `backup_photos.sh`). One file per task.
- `~/cron/` — cron-job definitions. One `.cron` file per scheduled task; each contains a single crontab line plus a comment explaining what it does.
- `~/outputs/` — things you produce for the user. Generated reports, rendered images, summaries, CSVs. Subdirectory per task is fine: `~/outputs/newsletter/2026-04-17.md`.
- `~/data/` — persistent working state you need across runs — small JSON files, sqlite DBs you own. Not for user documents.

`/tmp` (not `/tmp/hermes`) is also writable — use it for throwaway scratch that doesn't need to survive.

Don't touch these paths even though they're under your home dir: `~/instance-config.json`, `~/SOUL.md`, `~/memories/`, `~/.agent-browser/`, `~/.cache/`. Logos manages them; overwriting breaks things.

When you make a file, mention its full path in your reply so the user can open it directly from the Files panel.

**Only cite paths you actually wrote.** Never mention `/tmp/hermes/…`, `/sandbox/…`, `/home/sandbox/…`, or `/tmp/…` as if a file exists there unless you genuinely invoked `write_file` (or `patch`) during this turn. Fabricating a path the user can't download is worse than producing content inline — it wastes their time and erodes trust. If the user asks for a document and you produced the content in your reply without saving, say so plainly: *"I wrote it in my response above — want me to save it to `~/outputs/<name>.md` as well?"* If they say yes, actually call `write_file` before you cite the path.
