"""gateway/model_metrics.py: the Models overview as OTLP gauges."""
import json
from datetime import datetime, timezone

from gateway import model_metrics as mm

NOW = datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)
OVERVIEW = {
    "status": {"model_server": {"up": False, "ready": False},
               "services": [{"name": "Model gateway", "up": True}, {"name": "Model guard", "up": False}]},
    "agents": [
        {"name": "greg", "now": {"kind": "paid"}, "month_usd": 1.25, "limit_usd": 10.0, "projected_usd": 4.5,
         "current": {"kind": "paid", "since": "2026-11-20T13:30:00+00:00"},
         "models": [{"kind": "paid", "requests": 40}, {"kind": "local", "requests": 80}, {"kind": "paid", "requests": 2}]},
        {"name": "bere", "now": {"kind": "none"}, "month_usd": 10.0, "limit_usd": 10.0, "projected_usd": 10.0,
         "current": {"kind": "local", "since": "2026-11-20T09:00:00+00:00"}, "models": []}],
    "providers": [{"provider": "anthropic", "kind": "paid", "usd": 11.25}, {"provider": "lm-studio", "kind": "local", "usd": 0}],
}


def value(rows, name, **labels):
    return next(v for n, l, v in rows if n == name and l == labels)


def test_points_cover_status_agents_and_providers():
    rows = mm.points(OVERVIEW, NOW)
    assert value(rows, "logos_model_server_up") == 0.0
    assert value(rows, "logos_service_up", service="model_gateway") == 1.0
    assert value(rows, "logos_service_up", service="model_guard") == 0.0
    assert value(rows, "logos_agent_on_paid_fallback", agent="greg") == 1.0
    assert value(rows, "logos_agent_paid_fallback_seconds", agent="greg") == 7200.0
    assert value(rows, "logos_agent_paid_fallback_seconds", agent="bere") == 0.0
    assert value(rows, "logos_agent_can_answer", agent="bere") == 0.0
    assert value(rows, "logos_agent_can_answer", agent="greg") == 1.0
    assert value(rows, "logos_agent_spend_usd", agent="greg") == 1.25
    assert value(rows, "logos_agent_projected_usd", agent="greg") == 4.5
    assert value(rows, "logos_agent_requests_month", agent="greg", kind="paid") == 42.0
    assert value(rows, "logos_agent_requests_month", agent="greg", kind="local") == 80.0
    assert value(rows, "logos_provider_spend_usd", provider="anthropic") == 11.25
    assert not [r for r in rows if r[0] == "logos_provider_spend_usd" and r[1] == {"provider": "lm-studio"}]


def test_payload_is_otlp_json_with_one_gauge_per_name():
    body = mm.payload(mm.points(OVERVIEW, NOW), NOW)
    resource = body["resourceMetrics"][0]
    assert {"key": "service.name", "value": {"stringValue": "logos"}} in resource["resource"]["attributes"]
    metrics = {m["name"]: m for m in resource["scopeMetrics"][0]["metrics"]}
    spend = metrics["logos_agent_spend_usd"]["gauge"]["dataPoints"]
    assert len(spend) == 2 and spend[0]["timeUnixNano"] == str(int(NOW.timestamp() * 1e9))
    assert spend[0]["attributes"] == [{"key": "agent", "value": {"stringValue": "greg"}}] and spend[0]["asDouble"] == 1.25
    json.dumps(body)


def test_push_posts_to_the_metrics_path():
    sent = {}

    def http(url, *, data=None, headers=None, timeout=10):
        sent.update(url=url, body=json.loads(data), headers=headers)
        return b"{}"

    mm.push(http, "http://collector.lan:4318/", {"resourceMetrics": []})
    assert sent["url"] == "http://collector.lan:4318/v1/metrics" and sent["headers"] == {"Content-Type": "application/json"}


def test_an_empty_overview_sends_nothing_odd():
    assert mm.points({}, NOW) == []
