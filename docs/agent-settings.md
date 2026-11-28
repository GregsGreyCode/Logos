# Agent settings (story E6)

The people an agent belongs to can change a few things about it in the dashboard, without
editing files or running commands: open the agent, then **Settings** in its menu (Chat · Brain ·
Routines · Settings) or the **Settings** button on its Routines page. The address is
`#/agents/<name>/settings`.

| Section | What can be changed | Checked on the server |
| --- | --- | --- |
| Personality | The agent's own soul text (who it is, how it talks), with a preview of what changes, earlier versions to compare and restore, and Reset to default | At most 8,000 characters; with the shared rules no more than hermes takes whole (`SOUL_MAX_CHARS`); no control characters; refused if it looks like it holds a password or key |
| Model | The model on the local model server (LM Studio) and its context window | The model must be on that server (its list is read, nothing is loaded); the window one of 16k, 32k, 64k, 94k, 128k and within what the model takes; asks first when the model is not loaded (a second model in graphics memory) or when the window is larger than it is loaded with (a reload, which pauses the agents that share it); notes a small window and a model that cannot see photos; the brain-stays-local rule still applies |
| Chat | Its name; "show what it heard" (the text of each voice note before the answer, `voice_echo`); its name at the house speaker (`speaker_name`, waking to "Hey <name>") | Name: 1-40 ordinary characters. Speaker name: one or two words of letters, not a wake word another agent has. Its speaker voice stays in Settings › House speaker (linked) |

The shared rules every agent follows (`souls/_shared/`) are not editable here, and nothing secret
(tokens, keys, owners, policy, providers, budgets) can be set from this page.

## Who

The people in the agent's `owners` (everyone, for an agent without owners), and whoever manages
Logos (admin). Anyone else gets a 404, as for the agent's other pages. Every save is in the audit
log as `agent_settings` (which settings changed, never the text) and every restart as
`agent_restart` (how it went and how long it took).

## Where the choices are kept

On the machine, outside the repo, so nothing personal reaches the public repo and a deploy
(`scripts/logos-deploy`, a git fast-forward of the checkout) never touches them:

```
~/.config/logos/agents/<name>/      ($XDG_CONFIG_HOME, or $LOGOS_AGENT_SETTINGS instead of the whole path)
    settings.yaml      the chosen values: display_name, voice_echo, speaker_name/speaker_wake, model
    soul.md            the agent's own soul, used instead of the file its definition names
    history/           the last 20 saved versions of soul.md (history.json: when, who, how long)
    applied.json       what the running agent was made from (the page says "saved, not in use yet")
```

The folder and files are readable by the Logos account only. `settings.yaml` can hold only the keys
above: anything else in it is ignored.

How an agent's definition is put together (`AgentRuntime.load`), later ones winning:

1. `agents/<name>.yaml` (tracked)
2. **the dashboard's `settings.yaml`**
3. `agents/all.local.yaml`, then `agents/<name>.local.yaml` (untracked, by hand on the machine)
4. the Routines page's choices (`~/.logos/routines.json`)

So a value an administrator set by hand in a `.local.yaml` file still wins; the page shows that
setting as fixed, with the file's name. `scripts/logos-models local use <model>` (every agent onto
another model for a while) writes `all.local.yaml`, so it wins over a model chosen here too.

`soul_text` takes `soul.md` from that folder when there is one, else the `soul:` file of the
definition, then adds the shared rules as before.

## Applying: "Save and restart the agent"

Nothing changes in a running agent until its sandbox is recreated. The button saves, asks, and then
runs, in the background:

```
scripts/logos-agent up <name> --no-model-load
```

the same code path as by hand. It shows progress (waiting, recreating, back) and the command's
output under "What it did".

- **Never mid-reply:** first it asks the agent whether it is in the middle of something (as the
  logo's "working" does; if the agent cannot be asked, any reply the model guard is writing counts)
  and waits, up to three minutes, for that to finish. Still busy then: it is not restarted, and the
  page says so (the settings stay saved).
- **One at a time:** `up` also refreshes the shared services (model gateway and guard, noticeboard,
  wake words), so a second restart waits until the first is done.
- **LM Studio is left alone** (`--no-model-load`): the model guard loads the model at its window
  when the agent next asks for it. A restart takes about a minute, with no replies meanwhile; the
  agent's memory, brain and chats are kept.
- A restart started by hand (`scripts/logos-agent up`) or by a deploy picks up the same settings.
  Two `up`s of one agent at the same moment (say a person on the page and someone at the command
  line) overwrite each other's files: run it again.

## Endpoints

```
GET  /api/v2/agents/{name}/settings                  everything the page shows
PUT  /api/v2/agents/{name}/settings                  {soul | soul_reset | soul_restore: <id>, model: {default, context_length},
                                                      display_name, voice_echo, speaker_name, confirm}
GET  /api/v2/agents/{name}/settings/soul/{id}        one earlier version
POST /api/v2/agents/{name}/settings/apply            restart (202), 409 while another restart runs
GET  /api/v2/agents/{name}/settings/apply            {state: idle|waiting|restarting|done|failed, message, seconds, lines}
```

A PUT checks every change before keeping any. A model change that needs agreeing to answers 409
`{"error": "confirm", "warnings": [...]}` and keeps nothing; the page asks, then sends it again with
`confirm: true`.

Code: `gateway/agent_settings.py` (the folder, checks, history), `gateway/agent_settings_api.py`
(the endpoints and the restart), `gateway/ui/agent-settings.js` (the page). Tests:
`tests/unit/test_agent_settings.py`, `tests/ui/checks/agent_settings.mjs`.
