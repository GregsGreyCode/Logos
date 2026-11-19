# Daily changes

What changed in the homelab and in Logos, posted by the household agent in the household
group chat (stories DC1 and DC2; designed in the spec doc's "Daily changes" tab).

| Job (household agent) | When | What |
| --- | --- | --- |
| `changes-homelab` | 16:00 daily | homelab-infra commits on `main` + merged (Renovate) PRs, last 24 hours |
| `changes-logos` | 16:05 daily | Logos PRs merged into `v2`, last 24 hours, each marked live or not yet |
| `changes-homelab-week` | 16:10 Sunday | the week's roll-up (titles only) |
| `changes-logos-week` | 16:15 Sunday | the week's roll-up (titles only) |
| `changes-check` | 16:20 daily | no model: the plain list for any job above that did not go out |

- **Facts by code, words by the model.** `infra/agent-jobs/changes_facts.py` (installed under the
  four job names; its file name says which list) asks the web reader's `GET /changes/<repo>/<days>`
  and puts the list into the prompt (`changes_summary.md`). Nothing changed: it prints
  `{"wakeAgent": false}`, so the model is not woken and nothing is sent.
- **The model writes** a short bullet summary grouped by theme on LM Studio; Renovate updates
  get one bullet each. The household agent's history stays on prod and LM Studio.
- **LM Studio off:** the job fails, and `changes-check` posts the plain list of titles at 16:20.
  A run missed while the machine was off is skipped, never sent late.
- **Access:** see `docs/web-reader.md` (the `/changes`-only token, the read-only GitHub token).
- **Settings** (`changes:` in `agents/household.yaml`): `enabled`, and optionally `homelab`,
  `logos`, `homelab_week`, `logos_week`, `check` (cron times) and `weekly: false`.
