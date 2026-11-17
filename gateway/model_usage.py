"""What the models are doing and what they cost, read from the model gateway's own records.

Bifrost keeps a row per model call in ``logs.db`` and the money spent against each limit
in ``config.db``, both on its ``logos-models`` volume. Nothing here estimates a price:
every cost is the figure Bifrost recorded.

Two halves:

* :data:`QUERY` runs inside a throwaway Python container with the volume mounted, opens the
  databases read-only and prints one JSON document (:func:`fetch`).
* :func:`summarise` turns that document into what the dashboard and the metrics need:
  which model each agent is on right now and since when, spend this month per agent and
  per provider, what is left of each limit, a projection to the end of the month, and a
  day-by-day series.

Stdlib only. ``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`.
"""
from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

VOLUME = "logos-models"
GATEWAY_UID = "1000:0"
LOCAL_PROVIDER = "lm-studio"
KEY_PREFIX = "agent-"                  # virtual keys are named agent-<name>
PROJECTION_DAYS = 7                    # how far back the run-rate looks
SERIES_DAYS = 30

#: argv[1] = first instant of this month, argv[2] = start of the series, argv[3] = start of
#: the run-rate window; all as "YYYY-MM-DD HH:MM:SS" in UTC, which sorts like the stored text.
QUERY = r"""
import json, os, sqlite3, sys
month, series, recent = sys.argv[1:4]
out = {"month": [], "daily": [], "hourly": [], "last": [], "budgets": [], "errors": []}
ok = "status = 'success'"
if os.path.exists("/v/logs.db"):
    db = sqlite3.connect("file:/v/logs.db?mode=ro", uri=True, timeout=10)
    out["month"] = db.execute(
        "select virtual_key_name, provider, model, count(*), coalesce(sum(prompt_tokens),0),"
        " coalesce(sum(completion_tokens),0), coalesce(sum(cached_read_tokens),0), coalesce(sum(cost),0)"
        " from logs where timestamp >= ? and " + ok + " group by 1,2,3", (month,)).fetchall()
    out["daily"] = db.execute(
        "select substr(timestamp,1,10), virtual_key_name, provider, count(*), coalesce(sum(cost),0)"
        " from logs where timestamp >= ? and " + ok + " group by 1,2,3", (series,)).fetchall()
    out["hourly"] = db.execute(
        "select substr(timestamp,1,10), cast(substr(timestamp,12,2) as integer), virtual_key_name,"
        " coalesce(sum(cost),0) from logs where timestamp >= ? and " + ok + " and cost > 0 group by 1,2,3",
        (recent,)).fetchall()
    for (key,) in db.execute("select distinct virtual_key_name from logs where timestamp >= ?", (recent,)).fetchall():
        out["last"] += db.execute(
            "select virtual_key_name, timestamp, provider, model, status from logs"
            " where virtual_key_name = ? order by timestamp desc limit 200", (key,)).fetchall()
    out["first"] = db.execute("select min(timestamp) from logs").fetchone()[0]
if os.path.exists("/v/config.db"):
    db = sqlite3.connect("file:/v/config.db?mode=ro", uri=True, timeout=10)
    out["budgets"] = db.execute("select id, max_limit, current_usage from governance_budgets").fetchall()
print(json.dumps(out))
"""


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fetch(runtime: Any, image: str, now: Optional[datetime] = None) -> dict:
    """Run :data:`QUERY` against the gateway's volume and return its JSON."""
    now = now or datetime.now(timezone.utc)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    series = (now - timedelta(days=SERIES_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    recent = now - timedelta(days=PROJECTION_DAYS)
    # The databases are in WAL mode, which needs a writable directory even to read; they are
    # still opened read-only, as the gateway's own user.
    out = runtime.run(["docker", "run", "--rm", "--user", GATEWAY_UID, "--network", "none",
                       "-v", f"{VOLUME}:/v", "--entrypoint", "python3", image, "-c", QUERY,
                       _stamp(month), _stamp(min(series, month)), _stamp(recent)])
    return json.loads(out)


def agent_of(key_name: Optional[str]) -> str:
    key_name = key_name or ""
    return key_name[len(KEY_PREFIX):] if key_name.startswith(KEY_PREFIX) else key_name


def kind_of(provider: str) -> str:
    return "local" if provider == LOCAL_PROVIDER else "paid"


def parse_time(text: str) -> Optional[datetime]:
    """Bifrost writes "2026-11-16 09:55:41.796467157+00:00"; keep it to the second."""
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def current_model(rows: List[list]) -> Optional[dict]:
    """Which model answered an agent's latest call, and since when it has been on that one.

    ``rows`` are that agent's recent calls, newest first: ``[key, timestamp, provider, model, status]``.
    "Since" is the oldest call in the unbroken run of successful answers from the same provider.
    """
    good = [r for r in rows if r[4] == "success"]
    if not good:
        return None
    provider, model = good[0][2], good[0][3]
    since = good[0][1]
    for r in good[1:]:
        if r[2] != provider:
            break
        since = r[1]
    t, last = parse_time(since), parse_time(good[0][1])
    return {"provider": provider, "model": model, "kind": kind_of(provider),
            "since": t.isoformat() if t else None, "last_call": last.isoformat() if last else None}


def project(spent: float, hourly: Mapping[tuple, float], now: datetime, first: Optional[datetime]) -> float:
    """Month-end spend if the recent pattern continues.

    ``hourly`` maps ``(date, hour)`` to paid cost over the last :data:`PROJECTION_DAYS` days.
    Paid calls cluster in the hours the local model is off (afternoons and evenings), so the
    rest of today is projected from what those same hours cost on recent days, and each
    remaining day from the average whole day.
    """
    window_start = now - timedelta(days=PROJECTION_DAYS)
    days = PROJECTION_DAYS if not first or first <= window_start else max((now - first).total_seconds() / 86400, 1.0)
    by_hour = [0.0] * 24
    for (_date, hour), cost in hourly.items():
        by_hour[int(hour)] += float(cost)
    by_hour = [c / days for c in by_hour]
    rest_of_today = sum(by_hour[now.hour + 1:])
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day
    return spent + rest_of_today + days_left * sum(by_hour)


def summarise(raw: Mapping[str, Any], agents: Mapping[str, Mapping[str, Any]],
              chains: Mapping[str, List[dict]], now: Optional[datetime] = None) -> dict:
    """The dashboard's view. ``agents`` maps name -> definition for the agents to show;
    ``chains`` maps name -> :func:`gateway.model_gateway.fallback_chain` (may be empty)."""
    now = now or datetime.now(timezone.utc)
    first = parse_time(raw.get("first") or "") if raw.get("first") else None
    budgets = {b[0]: {"limit": float(b[1] or 0), "used": float(b[2] or 0)} for b in raw.get("budgets") or []}

    last: Dict[str, List[list]] = {}
    for row in raw.get("last") or []:
        last.setdefault(agent_of(row[0]), []).append(row)
    hourly: Dict[str, Dict[tuple, float]] = {}
    for date, hour, key, cost in raw.get("hourly") or []:
        slot = hourly.setdefault(agent_of(key), {})
        slot[(date, hour)] = slot.get((date, hour), 0.0) + float(cost)

    out_agents = []
    providers: Dict[str, dict] = {}
    for name in sorted(agents):
        definition = agents[name]
        models = []
        spent = 0.0
        for key, provider, model, requests, prompt, completion, cached, cost in raw.get("month") or []:
            if agent_of(key) != name:
                continue
            models.append({"provider": provider, "model": model, "kind": kind_of(provider),
                           "requests": requests, "prompt_tokens": prompt, "completion_tokens": completion,
                           "cached_tokens": cached, "usd": round(float(cost), 6)})
            spent += float(cost)
            p = providers.setdefault(provider, {"provider": provider, "kind": kind_of(provider),
                                                "requests": 0, "usd": 0.0})
            p["requests"] += requests
            p["usd"] += float(cost)
        limit = float((definition.get("budget") or {}).get("monthly_usd") or 0)
        shares = []
        for c in chains.get(name) or []:
            b = budgets.get(f"budget-{name}-{c['provider']}")
            shares.append({"provider": c["provider"], "model": c["model"], "limit_usd": c["monthly_usd"],
                           "used_usd": round(b["used"], 6) if b else 0.0,
                           "spent_out": bool(b and b["used"] >= b["limit"] > 0)})
        projected = project(spent, hourly.get(name, {}), now, first)
        out_agents.append({
            "name": name, "display_name": str(definition.get("display_name") or name),
            "current": current_model(last.get(name, [])),
            "fallbacks": shares,
            "month_usd": round(spent, 6), "limit_usd": limit,
            "left_usd": round(max(limit - spent, 0.0), 6),
            "projected_usd": round(projected, 4), "over": limit > 0 and projected > limit,
            "models": sorted(models, key=lambda m: (-m["usd"], -m["requests"])),
        })

    shown = set(agents)
    days: Dict[str, dict] = {}
    for date, key, provider, requests, cost in raw.get("daily") or []:
        if agent_of(key) not in shown:
            continue
        d = days.setdefault(date, {"date": date, "local": 0, "paid": 0, "usd": 0.0})
        d[kind_of(provider)] += requests
        d["usd"] += float(cost)
    start = (now - timedelta(days=SERIES_DAYS - 1)).date()
    series = []
    for i in range(SERIES_DAYS):
        date = (start + timedelta(days=i)).isoformat()
        d = days.get(date, {"date": date, "local": 0, "paid": 0, "usd": 0.0})
        d["usd"] = round(d["usd"], 6)
        series.append(d)

    total = sum(a["month_usd"] for a in out_agents)
    return {
        "as_of": now.isoformat(), "month": now.strftime("%B %Y"),
        "agents": out_agents,
        "providers": sorted(({**p, "usd": round(p["usd"], 6)} for p in providers.values()),
                            key=lambda p: (p["kind"] != "paid", -p["usd"], p["provider"])),
        "daily": series,
        "total": {"month_usd": round(total, 6), "limit_usd": sum(a["limit_usd"] for a in out_agents),
                  "projected_usd": round(sum(a["projected_usd"] for a in out_agents), 4)},
        "records_since": first.isoformat() if first else None,
    }
