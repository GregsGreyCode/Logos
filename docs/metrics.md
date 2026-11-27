# Prometheus metrics, and what "Agents today" counts (F42, 23 Nov 2026)

For people who run Prometheus or Grafana at home. Off by default. Numbers only.

## Turning it on

Settings › Logos › **Metrics** › **Turn on** (whoever manages Logos). It shows a token **once**, and a
`prometheus.yml` section with it filled in. Logos keeps only the token's hash
(`~/.config/logos/metrics.json`, owner-only). **New token** replaces it (the old one stops working);
**Turn off** forgets it, and `/metrics` stops existing.

Prometheus sends the token as `Authorization: Bearer …`:

```yaml
scrape_configs:
  - job_name: logos
    metrics_path: /metrics
    scheme: http
    authorization:
      credentials: <the token>
    static_configs:
      - targets: ['<logos address>:8091']
```

Off, or without the right token, `/metrics` answers 404 as if it were not there.

## What it exposes

| Metric | Meaning |
|---|---|
| `logos_info{version}` | which Logos this is (the commit) |
| `logos_agent_up{agent}` | 1 when the agent's sandbox is running |
| `logos_agent_messages_today{agent}` | messages the agent sent today |
| `logos_agent_messages_total{agent}` | the same, ever (a counter; survives restarts) |
| `logos_agent_last_message_timestamp_seconds{agent}` | when it last sent one (0: never) |
| `logos_vault_files{agent}` | photos and documents kept in the agent's vault (F47) |
| `logos_vault_bytes{agent}` | disk the vault uses |
| `logos_vault_files_by_state{agent,state}` | files by what has been done: `new` (waiting for a note), `retry`, `done`, `failed` |
| `logos_vault_oldest_file_timestamp_seconds{agent}`, `..._newest_...` | when the oldest and newest file was kept (0: none) |
| `logos_model_server_off`, `logos_model_server_off_since_timestamp_seconds` | the model guard's own view: 1 while the GPU computer does not answer, and since when |
| `logos_model_requests_running`, `..._waiting`, `logos_model_slots` | the guard's queue: in progress, waiting their turn, and how many may run at once |
| `logos_model_context_tokens_running`, `logos_model_context_window_tokens` | context in use now, and the loaded window |
| `logos_model_requests_served_total`, `..._queued_total` | requests served since the guard started, and how many had to wait (counters) |
| `logos_model_wait_seconds_avg`, `..._max` | how long requests waited in the queue since the guard started |
| `logos_agent_home_bytes{agent}` | disk the agent's home volume uses |
| `logos_job_last_run_timestamp_seconds{agent,job}`, `..._last_success_...` | when each scheduled job last ran, and last finished without error |
| `logos_job_last_run_failed{agent,job}` | 1 when the latest run failed or was missed |
| `logos_job_runs_24h{agent,job}`, `logos_job_failures_24h{agent,job}` | runs in the last day, and those that failed (a script job exits cleanly when the model is off, so those show as successful) |
| `logos_brain_pages{agent}`, `logos_brain_bytes{agent}` | pages in the brain and the disk it uses (with history) |
| `logos_brain_last_commit_timestamp_seconds{agent}` | when the brain was last saved to its history |
| `logos_brain_sync_conflicts{agent}` | Syncthing conflict copies waiting to be sorted out |
| `logos_sync_up`, `logos_sync_devices_behind{agent}` | Syncthing answers; phones behind on a brain (a count: never a phone's name) |
| `logos_noticeboard_open_requests{agent}` | requests waiting on the agent's person |
| `logos_model_up` | 1 when the AI computer answers (absent with no local model) |
| `logos_widget_needs{widget}` | 1 when a Home widget's last check needs someone |
| `logos_voice_turn_seconds{agent,model,turn,outcome}` | histogram: how long a question through the house speaker took. `turn` is `first` (the day's first, which reads the agent's whole instructions) or `followup`; `outcome` is `ok`, `slow` (answer promised in the chat), `failed` or `down`. Since the dashboard last started |
| `logos_voice_probe_seconds{agent,model,turn,outcome}`, `logos_voice_probe_timestamp_seconds` | the nightly fixed check (04:30, `LOGOS_VOICE_PROBE_AT`, `off` to stop): the same two questions every night, fresh and follow-up, so a faster GPU or model shows as a step in the line |

The jobs and the brain are read the same way (a helper container with no network and the volume mounted read-only: the agent's own Python for the job history, which it opens immutable, and alpine/git for the brain), and kept five minutes. The vault numbers are counted by a short helper container with no network that mounts the agent's volume read-only; the count is kept five minutes (`gateway/vault_stats.py`), so a frequent scrape costs nothing. Only counts and sizes: never a file name or what is in it.

**Never:** a message, a person's name, a room, a key. Agent names only as labels (`greg`, not "Greg's
agent"). The widget numbers come from each widget's last copy: `/metrics` never calls a service itself.

### Watching the speaker get faster (Grafana)

- The nightly check, the cleanest line: `logos_voice_probe_seconds{turn="first"}` and `{turn="followup"}`.
- Real questions, typical time: `histogram_quantile(0.5, sum by (le, turn) (rate(logos_voice_turn_seconds_bucket{outcome="ok"}[1d])))`
  (0.9 for the slow ones).
- Split by `model` to compare models after a swap.

## Where the numbers come from

`gateway/activity.py`: each time an agent sends a text message, its `logos-alert-text` plugin tells Logos
(the same hand-over the phone alerts use), and Logos adds one to that agent's count for the day. Only the
agent's name, the day and the time are kept (`~/.logos/activity.json`, 40 days). Not the text.

## "Agents today" on Home

The same numbers for the agents you may use: messages sent today and when the last went, "Quiet today",
or "Not running", and whether the AI computer is on. It needs no setup, and answers from Logos itself
(never cached: it is only counters).

Not counted yet: scheduled jobs that run without sending a message (the briefing counts, since it sends
one), and messages from people. Both could be added to the counter later.
