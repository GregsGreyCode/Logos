# When LM Studio is offline

Stories D1 and D2 (the **Model downtime** tab of the spec doc). All agents think with LM Studio
on Greg's PC. The PC is usually on, but LM Studio is closed while Greg is gaming, and two large
requests at once can fill its shared context. The brain stays local, so there is no cloud
fallback: agents wait, and nothing is lost. People see this as "the AI computer is off"
(docs/messages.md).

## What happens

| Moment | What happens |
| --- | --- |
| LM Studio reloads, closes briefly, or its context is full | The model guard waits up to a minute and sends the request again. The reply is just slower. |
| Still offline after that minute | The person gets one line at once: "I can't answer right now: the AI computer is off. I'll reply here as soon as it's back on." Once the group chat has been told (next row), the household agent answers in the group with one short line instead: "Noted. I'll answer this when the AI computer is back on." That line replaces hermes's own failed-turn text ("temporarily unavailable ... add a backup provider ... Send it again"), which before 20 Nov arrived instead of it (`patch_failed_turn_notice` in the waitlist plugin). |
| Offline for over 2 minutes | The household agent tells the household group chat ("**The AI computer is off.** ..."); again when it is back on. |
| LM Studio is back | Each agent answers its waiting conversations by itself, oldest first, one at a time (within about 30 seconds). |
| A message waited over 12 hours | A short note instead of an answer: ask again if you still need it. |
| Scheduled jobs | The weekly digest runs again the same day; the nightly brain tidy before 09:00. The morning briefing has already sent its plain version at 07:15. |
| Noticeboard requests | Checked again every half hour until the agent has read them. |
| Web chat (dashboard) | The message waits on the page (keep it open) and is sent by itself when LM Studio is back. Home says "The AI computer is off since 19:40". |

## The parts

- **Model guard** (`gateway/model_guard.py`, container `logos-model-guard`, port 8094): sits in
  front of LM Studio.
  - It waits up to a minute for a model request (`GUARD_WAIT`), and resends on LM Studio's
    "Context size has been exceeded".
  - After one full minute in vain, it answers "off" at once for the next 2 minutes, so hermes'
    own retries do not each wait another minute.
  - `GET /guard/health` tests the connection to LM Studio (for at most a second, so it answers
    inside the dashboard's 3 s limit) and says since when it is off.
- **`logos-waitlist`** (`infra/agent-plugins/logos-waitlist/`): a hermes plugin that `logos-agent
  up` copies into every agent's home and enables.
  - It tells hermes the guard's "off" is final for that turn.
  - It remembers the conversation in `/opt/data/waitlist.json` (session and time, never text).
  - It asks `/guard/health` every 30 seconds while anything waits, then re-sends each waiting
    conversation's last message with hermes' own `/retry`.
  - It checks that the re-sent turn was answered (the transcript's last row is not hermes's
    `failed_turn` mark). A model just loaded can loop in its thinking until hermes cuts it, so a
    re-sent turn that failed for any reason but the model being off again is sent once more.
    The log says `retried`, `retried twice`, `off again` or `failed: <why>`.
  - Its log is `/opt/data/logs/waitlist.log`.
- **`rerun-after-outage`** (`infra/agent-jobs/rerun_after_outage.py`): every 5 minutes, no
  model, in every agent.
- **`model-watch`** (`infra/agent-jobs/model_watch.py`): every minute, no model, household
  agent only. It posts to the group chat on the two changes.
- **`noticeboard_state.py`**: the half-hour retry stamp while a message is unread.
- **Sandbox rule `model_health`**: `GET /guard/health` only.

## Limits

- A voice note sent while LM Studio is offline cannot be re-sent by `/retry` (hermes cannot
  rebuild an attachment); the agent's reply then asks for it again.
- The plugin starts watching on the first message or error after an agent starts. If an agent
  is re-created during an outage, its waiting list is kept and is answered after its next message.
- If the person sends the message again themselves after LM Studio is back but before the agent
  noticed (up to 30 seconds), it can be answered twice.

## Checking

```shell
docker exec logos-model-guard python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8094/guard/health').read().decode())"
docker logs logos-model-guard | tail          # "model server not answering", "answering again"
scripts/logos-agent exec greg -- sh -c 'cat /opt/data/waitlist.json; tail /opt/data/logs/waitlist.log'
```
