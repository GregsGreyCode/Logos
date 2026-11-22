# Noticeboard: how agents talk to each other

Agents never call each other. The only channel is the noticeboard (story S5.3): a small service
where one agent leaves a message and the other reads it when it next checks. A message is a
request or a note, never an instruction. The recipient, following its own rules and its own
person, decides what happens.

## What an agent can send

| Kind | Meaning | Answer expected |
| --- | --- | --- |
| `fyi` | Something the other agent may want to know | No |
| `question` | A question | Yes: accepted or declined, with a reason |
| `shared_change` | A proposed change to shared household things. Only the household agent can be asked | Yes |
| `reminder` | "Please remind your person of X". The recipient schedules it itself, or declines | Yes |
| `answer` | The reply to one of the three request kinds | - |

Agents get exactly two tools: `send_message` and `read_messages`.

## Rules the service enforces

- **The sender cannot be faked.** It is whoever owns the token on the request, not a field in the message.
- **Unknown kinds and unknown recipients are rejected**, with a plain explanation the agent can read.
- **Ten requests a day** from one agent to another. Answers do not count.
- **Duplicates are stored once**: the same message to the same agent within a day is not sent again.
- **Requests expire after 24 hours** without an answer. The sender is told once.
- **One answer per request**, and only from the agent that was asked.
- **Everything is logged**, including rejections, in the `logos-noticeboard` Docker volume.

## Setting it up

Nothing to do by hand. An agent whose definition has `noticeboard: true` gets a token on its first
`scripts/logos-agent up <name>`, and that command starts or updates the noticeboard container.
`role: household` in a definition marks the agent that can be asked for shared changes.

The service is one standard-library Python file, `gateway/noticeboard.py`, run in the agent image
with the file mounted read-only. It listens on the Docker bridge address (usually
`172.17.0.1:8090`, with `8092` for the sandbox scripts' polling), which sandboxes can reach and the LAN cannot. Each sandbox's network policy
allows that one address.

To look at the log:

```shell
docker run --rm -v logos-noticeboard:/d:ro alpine:3 sh -c \
  'apk add -q sqlite && sqlite3 -header /d/noticeboard.db "select * from messages"'
```

## How an agent notices and responds (S5.4)

- **Regular check.** Every ten minutes a small script in each sandbox asks the noticeboard whether
  anything new is waiting (`infra/agent-jobs/noticeboard_state.py`). The model runs only when
  something is: a new message, or one of the agent's own requests expiring.
- **Rules.** `souls/_shared/noticeboard.md` is added to the soul of every agent on the noticeboard.
  The person decides, not the agent. During the check an agent may not answer anything: it files
  FYIs and passes each question, reminder or shared change to its person with the request number.
  It answers only after the person says what to answer, using their words. Requests the person
  ignores expire. Unanswered requests are listed again on every `read_messages`, so the agent can
  answer later from any conversation.
- **Why so strict.** In the first live test the local model answered a personal question from its
  notes and "accepted" a reminder it never scheduled. Judgement-based rules were not reliable
  enough, so the rule is now binary. It can be loosened later for things a person pre-approves.
- **Telling the person.** What the check produces goes to the person in the agent's Matrix home room (the
  Household room for the household agent). If there is nothing they need to know, the agent stays silent.

## Urgent messages (S5.7)

`urgent: true` on a message tells the recipient's person straight away. A script in the recipient's
sandbox runs every minute with no model call and delivers the line to the person in the agent's Matrix home room
("Urgent from Béré's agent: ..."). It never runs the recipient's agent; the agent deals with the
message at its normal check. An agent can send three urgent messages a day to another agent; more
go as normal messages. In the live test the line arrived 21 seconds after it was sent.

## Lock-down (S5.8)

Checked from inside a sandbox, with the policy in `infra/agent-image/policy.yaml`:

| A sandbox tries to reach | Result |
| --- | --- |
| LM Studio directly, the public internet, the OpenShell gateway, another agent's API | Blocked |
| The model gateway's admin API and web UI | Refused (only `/v1/**` is allowed) |
| The noticeboard with a method or tool outside the list | Refused by the sandbox policy itself |
| The noticeboard's poll port, any path but `/pending` and `/urgent` | Refused |
| Another agent's noticeboard identity | Not possible without that agent's token |

The noticeboard port is inspected as MCP: only `initialize`, `notifications/initialized`, `ping`,
`tools/list` and `tools/call` for `send_message` and `read_messages` get through. There is no
network path from one sandbox to another.

Each brain has a `sharing.md` page with the written rule for what may leave it: nothing, unless
its person says so each time.

## Not built yet

- No noticeboard page in the dashboard (S6.4).
- Standing permissions in `sharing.md` are a record only; agents still ask every time.
- The model gateway lists model names to a caller with no key (`/v1/models`). Inference needs a key.

## Logos's own jobs never send (22 Nov 2026)

Logos's own model jobs cannot call `send_message`: the weekly digest, the morning briefings, the
brain tidy-up, the noticeboard check and the daily changes. Their prompts start with
`LOGOS_JOB_MARK`, the `logos-job-limits` plugin refuses the call in a job carrying it, and the model
is told why. Their reply goes to the person only.

A job a person set up in chat ("every Friday, remind Béré's agent the bins go out") has no mark and
may send, because the person asked for it ahead of time. The noticeboard's rules still apply: the
other agent passes it to its person, and requests wait for that person's answer.

The household agent's digest covers only the household: the house, shared lists, shared plans
and dates. It never includes one person's own matters, even if they reached the household agent.

**Why:** on 22 Nov Béré's agent, writing her weekly digest, sent it to the household agent as an
FYI. Her own notes reached the shared agent, and so Greg's Notifications. A line in the digest's
instructions about the household chat had been read as "send it to the household agent". That
line is now given only to the household agent, and the digest says it is not to be sent anywhere
else. Because a model can misread instructions again, the plugin enforces the rule in code.

**How:** hermes's `pre_tool_call` does not say which platform a turn is on, so the plugin
remembers from `pre_llm_call` which sessions are Logos jobs (platform `cron` and the mark in the
job's prompt). hermes's own notice to scheduled jobs already says "do NOT use send_message"; the
digest sent one anyway.

**Cleanup:** the message (noticeboard id 45) was deleted. The household agent's noticeboard check
had stayed silent, and nothing from it was in the household brain or memory.

## Routines (22 Nov 2026)

Agents › an agent › **Routines** replaces the About page. **+ New routine** is at the top: say or
type what and when, and the agent sets it up. Below it is one list of routines (23 Nov: whether a
routine is built in or asked for only changes how it is changed). Each card has a switch, the
routine's name and when it runs, and a tap opens its options:

- **built in** (the morning briefing on a person's own agent, the weekly digest, what changed on the
  household agent): their times or day, saved at once; the switch turns it on or off;
- **asked for** (set up through the agent): how to change it (ask the agent) and Delete; the switch
  pauses and resumes it.
- **Behind the scenes:** folded away; read-only.
- **About:** the agent's model and what it can reach, folded away.

**Where choices are kept:** `~/.logos/routines.json` (`gateway/agent_runtime.py`), outside git. They
are laid over the agent's definition whenever it is loaded, so every deploy keeps them. A change
also re-creates only the routine jobs at once (`refresh_routines`, `ROUTINE_JOBS`); everything else
is left alone.

**Who may change them:** anyone who may use the agent (a person their own; either person the
household agent's). An agent only offers the routines it has: no briefing for the household
agent, and no daily changes for a personal one.
