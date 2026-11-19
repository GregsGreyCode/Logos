# Morning briefing

Story C4. Each person's agent sends one short message in Element X each morning: the date,
today's weather and today's events. Decided 19 Nov 2026.

```
Thursday 19 November. Rain, 6 to 10°C, 100% chance of rain.
16:30 Dentist, Smile Clinic
18:00 Boiler service
```

- **When:** 07:00 Monday to Friday, 09:00 Saturday and Sunday, in the agent's time zone. A
  morning missed while the server was off is skipped, never sent late.
- **Who:** the person agents (`calendar.person` set, Matrix on). Never the household agent.
- **Settings**, in `agents/<name>.yaml`:

  ```yaml
  briefing:
    enabled: true
    weekdays: "0 7 * * 1-5"
    weekends: "0 9 * * 0,6"
  ```

## How it works

Four hermes jobs per agent, installed by `scripts/logos-agent up` (`gateway/agent_runtime.py`):

| Job | When | What |
| --- | --- | --- |
| `morning-briefing` | weekdays | `briefing_today.py` gathers the facts; the local model writes the message |
| `morning-briefing-weekend` | weekends | the same |
| `morning-briefing-check` | 15 minutes after | no model: if the briefing did not go out, sends a plain version |
| `morning-briefing-weekend-check` | 15 minutes after | the same |

- **The facts** (`infra/agent-jobs/briefing_today.py`) are gathered by a script before the model
  runs, so the message never says "nothing on" because a tool call went wrong:
  - the weather, from the web reader's `GET /weather` (port 8097, the agent's web token);
  - today's events, from the calendar service's `list_events` (the agent's calendar token).
    Other people's events are passed on marked read only; the model mentions one only when it
    matters ("Béré is out until 21:00").
- **The message** (`infra/agent-jobs/morning_briefing.md`) is written by the agent's own local
  model. It also adds any reminder the agent set for today.
- **The fallback** (`infra/agent-jobs/briefing_check.py`) covers a busy or switched-off model
  server and failed deliveries. It reads hermes' job record (`/opt/data/cron/jobs.json`): when
  today's briefing ran and was delivered it sends nothing; otherwise it sends the same facts,
  ending "(Plain version: I couldn't write this morning's briefing.)"

## The weather

- The web reader asks Open-Meteo (free, no key) for today's forecast at most once an hour and
  keeps the last one if Open-Meteo is down.
- It sends only coordinates rounded to 0.1 degree (about 10 km), never a place name. They are
  set on the server in `~/.config/logos/location` (mode 0600), for example `51.5, -0.1`, and
  read when the web reader starts (`scripts/logos-deploy`, or `scripts/logos-agent up`).
- The sandbox itself still has no internet: its only new rule is `GET /weather` on the web
  reader (`weather` in `infra/agent-image/policy.yaml`).
- No location file: no weather line, and the rest of the briefing is unchanged.

## Checking it

```shell
scripts/logos-agent exec greg -- sh -c 'export HERMES_HOME=/opt/data; hermes cron list'   # next runs
scripts/logos-agent exec greg -- sh -c 'export HERMES_HOME=/opt/data; hermes cron run <id>'  # send one now
docker logs logos-reader | grep weather       # "greg weather: fetched" (never the forecast)
```
