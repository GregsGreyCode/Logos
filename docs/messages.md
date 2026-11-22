# How Logos talks to people

Everything Logos or an agent sends Greg and Béré, in Element X or on the dashboard, follows
these rules (decided 19 Nov 2026). Check a new message, prompt or fallback against them.

## Rules

- **Plain words, as a person would say it.** No job names or ids, request numbers, tool or
  service names, repository names, PR numbers, "cron", "noticeboard", "Renovate", "LM Studio".
- **Name things the way people know them.**
  - The model being unavailable is always **"the AI computer is off"** / **"back on"**. Never
    "the model", "LM Studio", "the model server" or "I'm offline" (the agent is running; the
    computer it thinks with is not).
  - The parts of the system are **"the agents"** (Logos) and **"the home servers"** (homelab).
  - Other agents are "Béré's agent", "Greg's agent", "the household agent".
- **One message per event.** The group chat hears about the AI computer going off once (from the
  household agent's model-watch). After that, the household agent answers each message there
  with one short line ("Noted. I'll answer this when the AI computer is back on."), not the full
  explanation again.
- **Light formatting.** Scheduled posts start with a **bold first line** (the date, "What changed
  today", "Your week, ..."). Short "- " bullets where there is a list. No headings, no tables,
  no emoji in message text (the robot in an agent's display name is the only one).
- **Say what to do, simply.** "Just reply here with the answer, or say no", not "say decline 12".
  Several requests are numbered for the person ("1.", "2."); ids stay with the agent.
- **No system-speak in fallbacks.** When a model-written post could not be made, the plain version
  is sent as it is, without "(Plain version: ...)" or apologies.
- **No admin instructions to people.** The dashboard says "Greg can start it again", never a
  shell command.

## Reactions

An emoji reaction on an agent's message reaches the agent (`infra/agent-plugins/logos-reactions/`,
22 Nov). It does not reply to a reaction: it is told at the start of its next turn in that chat
("Greg reacted 👍 to your message: "…""). Some reactions do more:

- 👎 and ❤️ also add a dated line to the agent's `topics/lessons.md`, which the nightly
  consolidation and the weekly digest learn from.
- ❓ asks it to explain that message more, 🔁 to try it again a different way. Each starts a turn,
  as a reply to that message.

`/emojis` (or `/reactions`) in a chat lists these, answered without the model. It is the one
message that has emoji in its text.

Only messages sent after 22 Nov are known; a reaction on an older one, or one taken away, is not
told. In the household group chat a reaction counts as addressing the agent, as an @mention does.

## Where the words live

| What | File |
| --- | --- |
| "The AI computer is off" reply in a chat | `gateway/model_guard.py` `OFF_MESSAGE`, `infra/agent-plugins/logos-waitlist/` `OFF_REPLY`, `NOTED_REPLY`, `EXPIRED_NOTE` |
| Group notice when it goes off and comes back | `infra/agent-jobs/model_watch.py` |
| Dashboard status, banners and chat errors | `gateway/ui/chat.js`, `gateway/ui/home.js`, `gateway/agent_chat.py`, job labels in `gateway/dashboard_api.py` `JOB_LABELS` |
| Morning briefing | `infra/agent-jobs/morning_briefing.md` (model), `briefing_check.py` (plain) |
| Weekly digest | `infra/agent-jobs/weekly_digest.md` |
| What changed today / this week | `infra/agent-jobs/changes_summary.md` (model), `changes_facts.py` `plain` and `clean_title` (plain) |
| Requests from other agents | `infra/agent-jobs/noticeboard_check.md`, `souls/_shared/noticeboard.md`, urgent line in `gateway/noticeboard.py` |
| Lights | `souls/_shared/house.md` |
| Weather line | `gateway/web_service.py` `weather_line` (says rain once) |

hermes' own "Cronjob Response: ... (job_id: ...)" wrapper is switched off for every agent
(`cron.wrap_response: false` in `gateway/agent_runtime.py`), so scheduled messages arrive exactly as
written. `cron.mirror_delivery: true` also writes each delivered message into the chat's session,
so the agent can see what its own jobs sent when the person replies (without it, a daily question
arrives in the chat but the agent has no record of having asked it).
