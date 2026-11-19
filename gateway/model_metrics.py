"""Model status and spend as metrics, pushed to an OpenTelemetry collector.

The Models screen shows these figures to a person; this sends the same ones, once a
minute, to a collector (OTLP over HTTP, JSON), so they can be graphed and alerted on
next to the rest of the house. Pushing means Logos opens no new port.

Switched on by ``LOGOS_OTLP_ENDPOINT`` (for example ``http://collector.lan:4318``) in the
gateway's environment; without it nothing is sent. Stdlib only.

Every metric is a gauge:

==================================== ============== ==========================================
name                                 labels         meaning
==================================== ============== ==========================================
logos_model_server_up                               1 when the local model server answers
logos_model_server_ready                            1 when the agents' model is loaded there
logos_service_up                     service        1 when that Logos service answers
logos_agent_sandbox_running          agent          1 when the agent's sandbox is Ready
logos_agent_can_answer               agent          0 when nothing would answer its next message
logos_agent_on_paid_fallback         agent          1 while a paid model would answer
logos_agent_paid_fallback_seconds    agent          how long its answers have come from a paid model
logos_agent_spend_usd                agent          paid spend this month
logos_agent_limit_usd                agent          its monthly limit
logos_agent_projected_usd            agent          projected spend at month end
logos_agent_requests_month           agent, kind    model calls this month (kind: local, paid)
logos_provider_spend_usd             provider       paid spend this month per provider
==================================== ============== ==========================================
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

ENV = "LOGOS_OTLP_ENDPOINT"
INTERVAL_SECONDS = 60


def points(overview: Mapping[str, Any], now: Optional[datetime] = None) -> List[tuple]:
    """``[(name, {label: value}, number)]`` from a Models overview (see ``dashboard_api.models_overview``)."""
    now = now or datetime.now(timezone.utc)
    out: List[tuple] = []
    status = overview.get("status") or {}
    server = status.get("model_server")
    if server is not None:
        out.append(("logos_model_server_up", {}, 1.0 if server.get("up") else 0.0))
        out.append(("logos_model_server_ready", {}, 1.0 if server.get("ready") else 0.0))
    for s in status.get("services") or []:
        label = re.sub(r"[^a-z0-9]+", "_", str(s["name"]).lower()).strip("_")
        out.append(("logos_service_up", {"service": label}, 1.0 if s.get("up") else 0.0))
    for name, ready in sorted((status.get("sandboxes") or {}).items()):
        out.append(("logos_agent_sandbox_running", {"agent": name}, 1.0 if ready else 0.0))
    for a in overview.get("agents") or []:
        labels = {"agent": a["name"]}
        kind = (a.get("now") or {}).get("kind")
        out.append(("logos_agent_can_answer", labels, 0.0 if kind == "none" else 1.0))
        out.append(("logos_agent_on_paid_fallback", labels, 1.0 if kind == "paid" else 0.0))
        current = a.get("current") or {}
        seconds = 0.0
        if current.get("kind") == "paid" and current.get("since"):
            try:
                seconds = max((now - datetime.fromisoformat(current["since"])).total_seconds(), 0.0)
            except ValueError:
                seconds = 0.0
        out.append(("logos_agent_paid_fallback_seconds", labels, seconds))
        out.append(("logos_agent_spend_usd", labels, float(a.get("month_usd") or 0)))
        out.append(("logos_agent_limit_usd", labels, float(a.get("limit_usd") or 0)))
        out.append(("logos_agent_projected_usd", labels, float(a.get("projected_usd") or 0)))
        by_kind: Dict[str, float] = {"local": 0.0, "paid": 0.0}
        for m in a.get("models") or []:
            by_kind[m["kind"]] = by_kind.get(m["kind"], 0.0) + float(m.get("requests") or 0)
        for k, v in sorted(by_kind.items()):
            out.append(("logos_agent_requests_month", {**labels, "kind": k}, v))
    for p in overview.get("providers") or []:
        if p.get("kind") == "paid":
            out.append(("logos_provider_spend_usd", {"provider": p["provider"]}, float(p.get("usd") or 0)))
    return out


def payload(rows: List[tuple], now: Optional[datetime] = None, instance: str = "logos") -> dict:
    """The OTLP/HTTP JSON body for ``rows``."""
    now = now or datetime.now(timezone.utc)
    stamp = str(int(now.timestamp() * 1e9))
    metrics: Dict[str, dict] = {}
    for name, labels, value in rows:
        m = metrics.setdefault(name, {"name": name, "gauge": {"dataPoints": []}})
        m["gauge"]["dataPoints"].append({
            "timeUnixNano": stamp, "asDouble": float(value),
            "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in sorted(labels.items())]})
    return {"resourceMetrics": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "logos"}},
                                    {"key": "service.instance.id", "value": {"stringValue": instance}}]},
        "scopeMetrics": [{"scope": {"name": "logos.models"}, "metrics": list(metrics.values())}]}]}


def push(http: Callable[..., bytes], endpoint: str, body: dict) -> None:
    """POST ``body`` to the collector. ``http`` is ``Runner.http``; raises on failure."""
    http(endpoint.rstrip("/") + "/v1/metrics", data=json.dumps(body).encode(),
         headers={"Content-Type": "application/json"}, timeout=10)
