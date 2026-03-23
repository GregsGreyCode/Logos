# archive/hermes-origin/

Files moved here during the Logos repo restructuring (2026-03-23).

These are legacy files from the hermes-agent origin codebase that have been
superseded by newer equivalents. They are kept for reference and diff-checking
before permanent deletion.

## Contents

| File | Superseded by | Notes |
|------|--------------|-------|
| `hermes` | `pyproject.toml` entry point `hermes = "hermes_cli.main:main"` | Old root launcher: `from cli import main; fire.Fire(main)`. Ran the old `cli.py` standalone CLI. |

## Review before deleting

Before permanently deleting anything here, verify:
1. No external documentation references the file path
2. No CI/CD scripts reference the file
3. The superseding file covers all functionality

See `docs/project/CLEANUP_REPORT.md` and `docs/project/SUSPICIOUS_FILES.md` for full context.
