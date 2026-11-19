# Daily changes

What changed in the homelab and in Logos, posted by the household agent in the household
group chat (stories DC1 and DC2; designed in the spec doc's "Daily changes" tab).

| Job (household agent) | When | What |
| --- | --- | --- |
| `changes-daily` | 16:00 daily | one post: Logos PRs merged into `v2` (each marked live or not yet) and homelab-infra commits + merged (Renovate) PRs, last 24 hours |
| `changes-week` | 16:10 Sunday | the week's roll-up of both, as one post |
| `changes-check` | 16:20 daily | no model: the plain list for any post above that did not go out |

- **Facts by code, words by the model.** `infra/agent-jobs/changes_facts.py` (installed as
  `changes_daily.py` and `changes_week.py`; its file name says how many days) asks the web reader's
  `GET /changes/<repo>/<days>` for both repositories and puts the lists into the prompt (`changes_summary.md`). Nothing changed in either: it prints
  `{"wakeAgent": false}`, so the model is not woken and nothing is sent.
- **The model writes** one message: a bold heading ("What changed today"), then "The agents" and
  "The home servers" with short bullets grouped by theme, in plain words (no repository names,
  PR numbers or "Renovate"; app updates are gathered into one bullet). See `docs/messages.md`. The household agent's history stays on prod and LM Studio.
- **LM Studio off:** the job fails, and `changes-check` posts the plain list at 16:20, with the titles cleaned up
  (`clean_title`: "chore(deps): update jellyfin docker tag to v10.10.4" -> "Jellyfin updated to 10.10.4").
  A run missed while the machine was off is skipped, never sent late.
- **Access:** see `docs/web-reader.md` (the `/changes`-only token, the read-only GitHub token).
- **Settings** (`changes:` in `agents/household.yaml`): `enabled`, and optionally `daily`,
  `week`, `check` (cron times) and `weekly: false`.

## On demand

Any agent of the web reader (Greg's, Béré's and the household agent) has the `recent_changes`
tool: ask "what changed in Logos today?" or "what changed in the homelab this week?" and it
answers in that chat from the same list (1 to 14 days; titles, live marks and short
descriptions). The household agent gets it next to `file_request`, still with no search.
Instructions in `souls/_shared/changes.md`.

