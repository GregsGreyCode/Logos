# Where to save things

Your home directory is `/root` (or `~`). Keep the layout predictable so the user can find what you've made without asking. When you create files, put them in one of these locations:

- `~/scripts/` — standalone scripts you write and want to reuse (e.g. `generate_newsletter.py`, `backup_photos.sh`). One file per task.
- `~/cron/` — cron-job definitions. One `.cron` file per scheduled task; each contains a single crontab line plus a comment explaining what it does.
- `~/outputs/` — things you produce for the user. Generated reports, rendered images, summaries, CSVs. Subdirectory per task is fine: `~/outputs/newsletter/2026-04-17.md`.
- `~/data/` — persistent working state you need across runs — small JSON files, sqlite DBs you own. Not for user documents.
- `~/tmp/` — throwaway scratch. You can write here freely; assume it may be cleaned up. Don't put outputs here.

Don't write to `/tmp/hermes/` — that's where Logos stores your config, memories, and session state. Treat it as read-only except via the memory tools.

When you make a file, mention its path in your reply so the user can open it directly from the Files panel.
