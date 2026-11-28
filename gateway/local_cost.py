"""Local vs cloud (Greg, 28 Nov): what running the agents' model at home costs, against what the same
work would cost on Claude.

One small ledger, ``~/.logos/local-usage.json`` (owner-only), per day (Europe/London):

- **Tokens and requests** from the model guard's log (``docker logs logos-model-guard``, one
  ``{"event": "chat", …}`` line per request), by ``who`` (person, speaker, scheduled). The guard cannot
  tell agents apart (the model gateway calls it with one shared key), so the split by agent on the
  Models page comes from the gateway's own records instead. Each line counts once: the ledger keeps the
  timestamp of the last line it counted (a high-water mark) in the same file as the totals, so a
  restart of the dashboard neither loses nor double-counts a line.
- **Cached prompt tokens**: llama-server reports prompt tokens it read and those it reused from its
  cache (``prompt_tokens_total`` / ``prompt_tokens_cached_total``). Their increase between two looks
  gives that day's cached share, applied to the day's prompt tokens. A day without those numbers
  (no ``--metrics``) counts nothing as cached, and the page says so.
- **Energy**: the GPU's power draw (llama-swap's nvidia-smi numbers) between two looks at most
  :data:`MAX_GAP` seconds apart, split into *answering* (a request was running, or llama-server's token
  counters moved) and *loaded* (the model kept in memory, nothing running). With the PC off, nothing
  is counted; across a longer gap (the dashboard was down), nothing is guessed. Draw while no model is
  loaded is the GPU's own idle baseline: its median can be subtracted (Settings in the card).

Only the GPU is measured (not the rest of the PC), and only electricity (not the hardware).
Prices for the comparison are in gateway/cloud_prices.py. Stdlib only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from gateway import cloud_prices

logger = logging.getLogger(__name__)

FILE = "local-usage.json"
EVERY = 15.0                # seconds between two looks
MAX_GAP = 90.0              # two looks further apart than this: the time between is not counted
SAVE_EVERY = 60.0           # the ledger is written at most this often (and when the dashboard stops)
IDLE_SAMPLES = 480          # power readings kept for the idle baseline (about two hours of looks)
WHO = ("person", "speaker", "scheduled")
DEFAULTS = {"price_per_kwh": 0.25, "currency": "GBP", "usd_rate": 0.79, "baseline_watts": None}
_CURRENCY = re.compile(r"^[A-Z]{3}$")

try:
    from zoneinfo import ZoneInfo
    LONDON: Any = ZoneInfo("Europe/London")
except Exception:                                   # no time zone data: UTC is the next best
    LONDON = timezone.utc


def path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("LOGOS_HOME") or Path(env.get("HOME") or Path.home()) / ".logos") / FILE


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, LONDON).date().isoformat()


def _empty() -> dict:
    return {"version": 1, "since": None, "last": [0, 0], "days": {}, "idle_watts": [], "settings": {}}


def _who_row() -> dict:
    return {"requests": 0, "prompt": 0, "completion": 0, "busy_s": 0.0,
            "long_requests": 0, "long_prompt": 0, "long_completion": 0}


def _day_row() -> dict:
    return {"who": {}, "cache": {"processed": 0, "cached": 0},
            "energy": {"answering_wh": 0.0, "answering_s": 0.0, "loaded_wh": 0.0, "loaded_s": 0.0}}


def clean_settings(body: Mapping[str, Any], old: Optional[Mapping[str, Any]] = None) -> dict:
    """Checked settings (ValueError with a message for a bad one). Missing keys keep ``old``."""
    out = {**DEFAULTS, **(old or {})}
    if "price_per_kwh" in body:
        v = float(body["price_per_kwh"])
        if not (math.isfinite(v) and 0 <= v <= 10):
            raise ValueError("The price per kWh must be between 0 and 10.")
        out["price_per_kwh"] = round(v, 4)
    if "currency" in body:
        c = str(body["currency"] or "").strip().upper()
        if not _CURRENCY.match(c):
            raise ValueError("The currency is a three-letter code, like GBP or EUR.")
        out["currency"] = c
    if "usd_rate" in body:
        v = float(body["usd_rate"])
        if not (math.isfinite(v) and 0 < v <= 1000):
            raise ValueError("The dollar rate must be above 0.")
        out["usd_rate"] = round(v, 6)
    if "baseline_watts" in body:
        b = body["baseline_watts"]
        if b is None or b == "":
            out["baseline_watts"] = None
        else:
            v = float(b)
            if not (math.isfinite(v) and 0 <= v <= 2000):
                raise ValueError("The idle baseline must be between 0 and 2000 watts.")
            out["baseline_watts"] = round(v, 1)
    if out["currency"] == "USD":
        out["usd_rate"] = 1.0
    return out


# ── one look at the model PC, reduced to what the ledger needs ───────────────────────────────

def reading(snaps: List[Optional[Mapping[str, Any]]], guard_running: int = 0, now: Optional[float] = None) -> dict:
    """``{at, up, loaded, busy, watts, counters}`` from model_server_metrics looks (one per server).
    ``watts`` is None without power numbers; ``counters`` None without llama-server's."""
    ups = [s for s in snaps if s and s.get("up") and s.get("kind") == "llama-swap"]
    at = max([float(s.get("at") or 0) for s in ups] or [time.time() if now is None else now])
    watts = [float(g["power_watts"]) for s in ups for g in s.get("gpus") or [] if isinstance(g.get("power_watts"), (int, float))]
    ready = [m for s in ups for m in s.get("models") or [] if m.get("state") == "ready"]
    stats = [m["llama"] for m in ready if m.get("llama")]
    counters = None
    if stats:
        counters = {k: sum(float(L.get(k) or 0) for L in stats)
                    for k in ("prompt_tokens_total", "prompt_tokens_cached_total", "tokens_predicted_total")}
    processing = sum(int(L.get("requests_processing") or 0) for L in stats)
    return {"at": at, "up": bool(ups), "loaded": bool(ready), "busy": processing > 0 or guard_running > 0,
            "watts": sum(watts) if watts else None, "counters": counters}


class Ledger:
    """The per-day totals. Thread-safe; :meth:`save` writes the file atomically, owner-only."""

    def __init__(self, file: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.file = file or path()
        self.clock = clock
        self.lock = threading.RLock()
        self.data = self._load()
        self.prev: Optional[dict] = None             # the last reading (memory only: a restart is a gap)
        self.dirty = False
        self.saved_at = -math.inf

    def _load(self) -> dict:
        try:
            d = json.loads(self.file.read_text())
            if isinstance(d, dict) and isinstance(d.get("days"), dict):
                base = _empty()
                base.update(d)
                return base
        except (OSError, ValueError):
            pass
        return _empty()

    def save(self, force: bool = False) -> None:
        with self.lock:
            if not self.dirty and not force:
                return
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, separators=(",", ":"))
            os.replace(tmp, self.file)
            os.chmod(self.file, 0o600)
            self.dirty, self.saved_at = False, time.monotonic()

    def maybe_save(self) -> None:
        if self.dirty and time.monotonic() - self.saved_at >= SAVE_EVERY:
            self.save()

    def _day(self, date: str) -> dict:
        d = self.data["days"].setdefault(date, _day_row())
        for k, v in _day_row().items():
            d.setdefault(k, v)
        return d

    def start(self) -> None:
        """Tracking begins now (the first time only)."""
        with self.lock:
            if not self.data.get("since"):
                self.data["since"] = self.clock()
                self.dirty = True

    @property
    def settings(self) -> dict:
        return {**DEFAULTS, **(self.data.get("settings") or {})}

    def set_settings(self, body: Mapping[str, Any]) -> dict:
        with self.lock:
            self.data["settings"] = clean_settings(body, self.data.get("settings"))
            self.dirty = True
            self.save()
            return self.settings

    # ── the guard's log ──

    def since_arg(self) -> str:
        """``docker logs --since`` for the lines after the last one counted (or since tracking began)."""
        sec, ns = self.data.get("last") or [0, 0]
        if sec:
            return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{int(ns):09d}Z"
        return str(int(self.data.get("since") or self.clock()))

    def feed(self, lines: List[str]) -> int:
        """Lines of ``docker logs --timestamps``; each newer than the high-water mark counts once."""
        from gateway.model_server_metrics import _TS, _stamp
        n = 0
        with self.lock:
            last = tuple(self.data.get("last") or (0, 0))
            for line in lines:
                m = _TS.match(line.strip())
                if not m or not m.group(3).startswith("{"):
                    continue
                stamp = _stamp(m.group(1), m.group(2))
                if stamp <= last:
                    continue
                last = stamp
                self.data["last"] = list(stamp)
                self.dirty = True
                try:
                    r = json.loads(m.group(3))
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("event") == "chat":
                    self._add(day_of(stamp[0]), r)
                    n += 1
        return n

    def _add(self, date: str, r: Mapping[str, Any]) -> None:
        who = r.get("who") if r.get("who") in WHO else "person"
        row = self._day(date)["who"].setdefault(who, _who_row())
        row["requests"] += 1
        try:
            row["busy_s"] = round(row["busy_s"] + max(float(r.get("seconds") or 0), 0.0), 1)
        except (TypeError, ValueError):
            pass
        p = r.get("prompt_tokens") if isinstance(r.get("prompt_tokens"), int) and r["prompt_tokens"] >= 0 else 0
        c = r.get("completion_tokens") if isinstance(r.get("completion_tokens"), int) and r["completion_tokens"] >= 0 else 0
        row["prompt"] += p
        row["completion"] += c
        if p > cloud_prices.HAIKU_LONG_OVER:        # the long-prompt tier is per request
            row["long_requests"] += 1
            row["long_prompt"] += p
            row["long_completion"] += c

    # ── the model PC's looks ──

    def sample(self, cur: Mapping[str, Any]) -> None:
        """One :func:`reading`: integrate power and counters since the previous one."""
        with self.lock:
            if self.prev is not None and float(cur["at"]) == float(self.prev["at"]):
                return                                       # the same look again (it is cached 15 s)
            prev, self.prev = self.prev, dict(cur)
            if cur.get("up") and not cur.get("loaded") and cur.get("watts") is not None:
                idle = self.data.setdefault("idle_watts", [])
                idle.append(round(float(cur["watts"]), 1))      # the GPU with no model: the baseline
                del idle[:-IDLE_SAMPLES]
                self.dirty = True
            if prev is None:
                return
            dt = float(cur["at"]) - float(prev["at"])
            if dt <= 0 or not (prev.get("up") and cur.get("up")):
                return
            day = self._day(day_of(float(cur["at"])))
            moved = False
            a, b = prev.get("counters"), cur.get("counters")
            if a and b and dt <= 3600:
                delta = {}
                for k in b:
                    d = b[k] - a.get(k, 0)
                    delta[k] = b[k] if d < 0 else d          # llama-server restarted: counted from 0 again
                day["cache"]["processed"] += int(delta["prompt_tokens_total"])
                day["cache"]["cached"] += int(delta["prompt_tokens_cached_total"])
                moved = any(v > 0 for v in delta.values())
                self.dirty = True
            if dt > MAX_GAP or prev.get("watts") is None or cur.get("watts") is None:
                return
            if prev.get("busy") or cur.get("busy") or moved:
                phase = "answering"
            elif prev.get("loaded") or cur.get("loaded"):
                phase = "loaded"
            else:
                return                                       # nothing loaded: not the agents' cost
            watts = (float(prev["watts"]) + float(cur["watts"])) / 2
            e = day["energy"]
            e[f"{phase}_wh"] = round(e[f"{phase}_wh"] + watts * dt / 3600, 6)
            e[f"{phase}_s"] = round(e[f"{phase}_s"] + dt, 3)
            self.dirty = True

    def idle_median(self) -> Optional[float]:
        w = self.data.get("idle_watts") or []
        return round(statistics.median(w), 1) if w else None

    def baseline(self) -> Tuple[float, str]:
        """The watts subtracted, and where they come from (setting, measured, none)."""
        s = self.settings
        if s.get("baseline_watts") is not None:
            return float(s["baseline_watts"]), "setting"
        m = self.idle_median()
        return (m, "measured") if m is not None else (0.0, "none")

    # ── totals ──

    def totals(self, dates: List[str]) -> dict:
        """Tokens, energy and money over these days, at the current settings."""
        with self.lock:
            days = [self.data["days"][d] for d in dates if d in self.data["days"]]
            settings, (base, _) = self.settings, self.baseline()
        t = {"requests": 0, "busy_s": 0.0, "prompt": 0, "cached": 0.0, "completion": 0,
             "long_prompt": 0, "long_cached": 0.0, "long_completion": 0, "long_requests": 0}
        by_who: Dict[str, dict] = {}
        e = {"answering_kwh": 0.0, "loaded_kwh": 0.0, "answering_s": 0.0, "loaded_s": 0.0}
        cache_days = token_days = 0
        for d in days:
            c = d.get("cache") or {}
            seen = c.get("processed", 0) + c.get("cached", 0)
            share = c.get("cached", 0) / seen if seen > 0 else 0.0
            day_prompt = 0
            for who, r in (d.get("who") or {}).items():
                w = by_who.setdefault(who, {"requests": 0, "prompt": 0, "cached": 0.0, "completion": 0, "busy_s": 0.0})
                for k in ("requests", "prompt", "completion", "busy_s"):
                    w[k] += r.get(k, 0)
                    t[k] += r.get(k, 0)
                w["cached"] += r.get("prompt", 0) * share
                t["cached"] += r.get("prompt", 0) * share
                for k in ("long_prompt", "long_completion", "long_requests"):
                    t[k] += r.get(k, 0)
                t["long_cached"] += r.get("long_prompt", 0) * share
                day_prompt += r.get("prompt", 0)
            if day_prompt:
                token_days += 1
                cache_days += 1 if seen > 0 else 0
            en = d.get("energy") or {}
            for phase in ("answering", "loaded"):
                s = float(en.get(f"{phase}_s", 0))
                e[f"{phase}_kwh"] += max(float(en.get(f"{phase}_wh", 0)) - base * s / 3600, 0.0) / 1000
                e[f"{phase}_s"] += s
        price, fx = settings["price_per_kwh"], settings["usd_rate"]
        local = {"answering": e["answering_kwh"] * price, "loaded": e["loaded_kwh"] * price}
        local["total"] = local["answering"] + local["loaded"]
        cloud = []
        for key in cloud_prices.ORDER:
            usd = cloud_prices.cost(key, t["prompt"], t["cached"], t["completion"],
                                    t["long_prompt"], t["long_cached"], t["long_completion"])
            cloud.append({"model": key, "label": cloud_prices.MODELS[key]["label"], "usd": usd, "cost": usd * fx,
                          "saved": usd * fx - local["total"]})
        return {"tokens": {"prompt": t["prompt"], "cached": round(t["cached"]), "completion": t["completion"]},
                "requests": t["requests"], "busy_s": round(t["busy_s"], 1), "long_requests": t["long_requests"],
                "energy": {k: round(v, 6) if k.endswith("kwh") else round(v) for k, v in e.items()},
                "local": {k: round(v, 6) for k, v in local.items()},
                "cloud": [{**c, "usd": round(c["usd"], 6), "cost": round(c["cost"], 6), "saved": round(c["saved"], 6)} for c in cloud],
                "by_who": {k: {**v, "cached": round(v["cached"]), "busy_s": round(v["busy_s"], 1)} for k, v in sorted(by_who.items())},
                "cache_measured": token_days == cache_days, "days_without_cache": token_days - cache_days,
                "energy_measured": e["answering_s"] + e["loaded_s"] > 0}

    def summary(self, now: Optional[float] = None) -> dict:
        """The Models page's card: today, this month, since tracking began, and a projected month."""
        now = self.clock() if now is None else now
        here = datetime.fromtimestamp(now, LONDON)
        today = here.date()
        with self.lock:
            dates = sorted(self.data["days"])
            since = self.data.get("since")
        month = [d for d in dates if d.startswith(today.strftime("%Y-%m"))]
        out = {"as_of": here.isoformat(), "since": datetime.fromtimestamp(since, LONDON).isoformat() if since else None,
               "month": today.strftime("%B %Y"),
               "today": self.totals([today.isoformat()]), "this_month": self.totals(month), "all": self.totals(dates)}
        # The projection carries on the last 7 days' rate (or since tracking began, if later).
        start = datetime.combine(today - timedelta(days=6), datetime.min.time(), LONDON).timestamp()
        window_start = max(start, since or now)
        span = now - window_start
        first_next = (here.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        left = datetime.combine(first_next.date(), datetime.min.time(), LONDON).timestamp() - now
        out["projected"] = None
        if span >= 3600:
            window = self.totals([(today - timedelta(days=i)).isoformat() for i in range(7)])
            k = left / span
            m = out["this_month"]
            out["projected"] = {
                "local": round(m["local"]["total"] + window["local"]["total"] * k, 4),
                "cloud": [{"model": c["model"], "label": c["label"], "cost": round(c["cost"] + w["cost"] * k, 4),
                           "saved": round(c["saved"] + w["saved"] * k, 4)}
                          for c, w in zip(m["cloud"], window["cloud"])],
                "from_days": round(span / 86400, 1)}
        base, where = self.baseline()
        out["settings"] = {**self.settings, "baseline_used": base, "baseline_from": where, "idle_measured_watts": self.idle_median()}
        out["prices"] = {"as_of": cloud_prices.AS_OF, "models": cloud_prices.table()}
        return out

    # ── Prometheus ──

    def render(self) -> str:
        """Totals since tracking began, for /metrics (counters; repriced if the settings change)."""
        from gateway.model_server_metrics import _metric
        with self.lock:
            if not self.data.get("since"):
                return ""
            dates = sorted(self.data["days"])
        t = self.totals(dates)
        out: List[str] = []
        _metric(out, "logos_local_energy_kwh_total", "counter",
                "GPU energy for the agents' model, less the idle baseline (answering: requests running; loaded: kept in memory).",
                [({"phase": "answering"}, float(t["energy"]["answering_kwh"])), ({"phase": "loaded"}, float(t["energy"]["loaded_kwh"]))])
        _metric(out, "logos_local_cost_total", "counter", "That energy's electricity cost, at the price set on the Models page.",
                [({"currency": self.settings["currency"]}, float(t["local"]["total"]))])
        _metric(out, "logos_cloud_equivalent_cost_usd_total", "counter",
                "What the same tokens would have cost on Claude (list prices in gateway/cloud_prices.py).",
                [({"model": c["model"]}, float(c["usd"])) for c in t["cloud"]])
        _metric(out, "logos_tokens_total", "counter", "Tokens the local model handled (prompt, of which cached, and completion), from the model guard's log.",
                [({"kind": "prompt"}, float(t["tokens"]["prompt"])), ({"kind": "cached"}, float(t["tokens"]["cached"])),
                 ({"kind": "completion"}, float(t["tokens"]["completion"]))])
        return "\n".join(out) + ("\n" if out else "")


# ── the dashboard: a look every 15 s, the card's API ─────────────────────────────────────────

def step(ledger: Ledger, runtime: Any) -> None:
    """Blocking: look at the model PC and the guard, and add what is new to the ledger."""
    from gateway import dashboard_api, model_gateway, model_server, model_server_metrics as msm
    http, run = runtime.runner.http, runtime.runner.run
    ledger.start()
    bases = sorted(model_server.wanted(dashboard_api._local_definitions(runtime)))
    snaps = [msm.SCRAPER.get(http, b) for b in bases]
    gw = model_gateway.ModelGateway(runtime)
    running = 0
    try:
        h = json.loads(http(f"http://{gw.bind_address()}:{model_gateway.GUARD_PORT}/guard/health", timeout=3))
        running = int(((h.get("queue") or {}).get("running")) or len(h.get("live") or []))
    except Exception:
        running = 0
    if snaps:
        ledger.sample(reading(snaps, running))
    try:
        p = run(["docker", "logs", "--timestamps", "--since", ledger.since_arg(), model_gateway.GUARD_CONTAINER])
        if p.returncode == 0:
            ledger.feed(((p.stdout or "") + "\n" + (p.stderr or "")).splitlines())
    except Exception as exc:
        logger.debug("local cost: guard log not read: %s", exc)
    ledger.maybe_save()


def _admin(request: Any) -> bool:
    from gateway.dashboard_api import _user
    return _user(request)["role"] == "admin"


async def handle_get(request: Any) -> Any:
    """GET /api/v2/models/local-cost — the card (whole-household figures: whoever manages Logos)."""
    from aiohttp import web
    if not _admin(request):
        raise web.HTTPForbidden()
    return web.json_response(await asyncio.to_thread(request.app["local_cost"].summary))


async def handle_set(request: Any) -> Any:
    """PUT /api/v2/models/local-cost {price_per_kwh, currency, usd_rate, baseline_watts}."""
    from aiohttp import web
    if not _admin(request):
        raise web.HTTPForbidden()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("bad request")
        settings = await asyncio.to_thread(request.app["local_cost"].set_settings, body)
    except (ValueError, TypeError) as e:
        return web.json_response({"error": str(e) or "bad request"}, status=400)
    return web.json_response({"settings": settings})


def register(app: Any, file: Optional[Path] = None) -> None:
    from gateway import agent_chat
    from gateway.auth.middleware import require_csrf
    app["local_cost"] = Ledger(file)
    app.router.add_get("/api/v2/models/local-cost", handle_get)
    app.router.add_put("/api/v2/models/local-cost", require_csrf(handle_set))

    async def _start(_app: Any) -> None:
        async def loop() -> None:
            while True:
                try:
                    runtime = agent_chat.runtime_of(_app.get("sandbox_backend"))
                    if runtime is not None:
                        await asyncio.to_thread(step, _app["local_cost"], runtime)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("local cost: look failed: %s", exc)
                await asyncio.sleep(EVERY)
        _app["local_cost_task"] = asyncio.create_task(loop())

    async def _stop(_app: Any) -> None:
        task = _app.get("local_cost_task")
        if task:
            task.cancel()
        try:
            _app["local_cost"].save()
        except OSError:
            pass
    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
