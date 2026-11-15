# Brain

This vault is an agent's long-term memory. The agent writes it; its person can read and edit it.

| Folder | What goes here | File naming |
| --- | --- | --- |
| `inbox/` | Quick captures, as they happen. One file per day, newest at the bottom. | `inbox/YYYY-MM-DD.md` |
| `daily/` | One summary note per day, written by the nightly consolidation. | `daily/YYYY-MM-DD.md` |
| `people/` | One page per person: who they are, dates, preferences, open threads. | `people/firstname-lastname.md` |
| `projects/` | One page per project or ongoing effort: goal, status, decisions, next steps. | `projects/short-name.md` |
| `topics/` | Everything else worth keeping: how-tos, reference facts, household info. | `topics/short-name.md` |

Rules of the vault:

- Plain markdown, lowercase file names with hyphens.
- Every page starts with a `# Title` line and a one-line summary under it.
- Decisions are written as `- YYYY-MM-DD: decided ...` under a `## Decisions` heading.
- Link related pages with relative links, like `[Béré](../people/bere.md)`.
