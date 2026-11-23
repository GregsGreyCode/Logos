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
| `logos_model_up` | 1 when the AI computer answers (absent with no local model) |
| `logos_widget_needs{widget}` | 1 when a Home widget's last check needs someone |

**Never:** a message, a person's name, a room, a key. Agent names only as labels (`greg`, not "Greg's
agent"). The widget numbers come from each widget's last copy: `/metrics` never calls a service itself.

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
