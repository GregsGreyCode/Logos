# Where to save things

Your home directory (`$HOME`, `~`) is `/tmp/hermes`. It's the main location you have write access to. Most of the filesystem is read-only or permission-denied to your sandbox user (`/`, `/root`, `/home`, `/opt`, `/usr`, etc.). Put the things you create in predictable subdirectories of home so the user can find them:

- `~/scripts/` — standalone scripts you write and want to reuse (e.g. `generate_newsletter.py`, `backup_photos.sh`). One file per task.
- `~/cron/` — cron-job definitions. One `.cron` file per scheduled task; each contains a single crontab line plus a comment explaining what it does.
- `~/outputs/` — things you produce for the user. Generated reports, rendered images, summaries, CSVs. Subdirectory per task is fine: `~/outputs/newsletter/2026-04-17.md`.
- `~/data/` — persistent working state you need across runs — small JSON files, sqlite DBs you own. Not for user documents.

`/tmp` (not `/tmp/hermes`) is also writable — use it for throwaway scratch that doesn't need to survive.

Don't touch these paths even though they're under your home dir: `~/instance-config.json`, `~/SOUL.md`, `~/memories/`, `~/.agent-browser/`, `~/.cache/`. Logos manages them; overwriting breaks things.

When you make a file, mention its full path in your reply so the user can open it directly from the Files panel.
