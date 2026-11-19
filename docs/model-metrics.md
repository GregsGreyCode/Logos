# Model status and spend in Grafana

The [Models screen](model-gateway.md) shows who answers each agent and what the paid
fallbacks cost. The same figures can be sent to an OpenTelemetry collector once a minute, to
graph and alert on them next to everything else. Logos pushes; it opens no port for this.

## Switching it on

Add the collector's OTLP/HTTP address to the gateway's environment (`~/.logos/.env`) and
restart the gateway:

```
LOGOS_OTLP_ENDPOINT=http://<collector>:4318
```

Without that line nothing is sent. If the collector stops accepting data the gateway logs it
once and keeps trying each minute.

## Metrics

All are gauges, with `service_name="logos"`.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `logos_model_server_up` | | 1 when the local model server answers |
| `logos_model_server_ready` | | 1 when the agents' model is loaded there |
| `logos_service_up` | `service` | 1 when that service answers: `model_gateway`, `model_guard`, `voice`, `noticeboard`, `dashboard`, and where they run, `matrix`, `calendar_server`, `calendar_service`, `syncthing` |
| `logos_agent_sandbox_running` | `agent` | 1 when the agent's sandbox is Ready (OpenShell) |
| `logos_agent_can_answer` | `agent` | 0 when nothing would answer its next message |
| `logos_agent_on_paid_fallback` | `agent` | 1 while a paid model would answer |
| `logos_agent_paid_fallback_seconds` | `agent` | how long its answers have been coming from a paid model |
| `logos_agent_spend_usd` | `agent` | paid spend this month, as the model gateway recorded it |
| `logos_agent_limit_usd` | `agent` | its monthly limit |
| `logos_agent_projected_usd` | `agent` | projected spend at month end |
| `logos_agent_requests_month` | `agent`, `kind` | model calls this month, `local` or `paid` |
| `logos_provider_spend_usd` | `provider` | paid spend this month per provider |

## Suggested alerts

| Alert | Expression | For |
| --- | --- | --- |
| An agent cannot answer | `logos_agent_can_answer{agent!="bench"} == 0` | 10m |
| On the paid fallback for a long time | `min_over_time(logos_agent_on_paid_fallback{agent!="bench"}[6h]) == 1` | 0m |
| Projected to pass its limit | `logos_agent_projected_usd > logos_agent_limit_usd` | 1h |
| Most of the limit is spent | `logos_agent_spend_usd / logos_agent_limit_usd > 0.8` | 0m |
| A Logos service is down | `logos_service_up == 0` | 5m |
| An agent's sandbox is not running | `logos_agent_sandbox_running == 0` | 5m |
| No data from Logos | `absent(logos_service_up{service="dashboard"})` | 10m |

`logos_model_server_up == 0` is normal whenever the model server's machine is used for
something else, so it is better as a panel than as an alert.
