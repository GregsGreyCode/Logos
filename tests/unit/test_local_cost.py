"""gateway/local_cost.py and gateway/cloud_prices.py: the local-vs-cloud ledger (made-up numbers)."""
import json
import os
import stat
from datetime import datetime

import pytest

from gateway import cloud_prices, local_cost as lc

# 28 Nov 2026, 12:00 in London (GMT, so also 12:00 UTC)
NOON = datetime(2026, 11, 28, 12, 0, 0, tzinfo=lc.LONDON).timestamp()


def line(ts, **r):
    t = datetime.fromtimestamp(ts, lc.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + f".{int((ts % 1) * 1e9):09d}Z " + json.dumps({"event": "chat", **r})


def chat(ts, who="person", prompt=1000, completion=100, seconds=4.0, status=200):
    return line(ts, who=who, prompt_tokens=prompt, completion_tokens=completion, seconds=seconds, status=status)


def ledger(tmp_path, now=NOON):
    led = lc.Ledger(tmp_path / "local-usage.json", clock=lambda: now)
    led.start()
    return led


def look(at, watts=None, loaded=True, up=True, processing=0, counters=None):
    snap = {"at": at, "up": up, "kind": "llama-swap" if up else "", "gpus": [], "models": []}
    if up and watts is not None:
        snap["gpus"] = [{"id": "0", "name": "GPU", "power_watts": watts}]
    if up and loaded:
        L = None
        if counters is not None:
            L = {"prompt_tokens_total": counters[0], "prompt_tokens_cached_total": counters[1],
                 "tokens_predicted_total": counters[2], "requests_processing": processing}
        snap["models"] = [{"model": "m", "state": "ready", "llama": L}]
    return snap


# ── cloud prices ──

def test_prices_per_million_and_cache_read():
    # 1M prompt of which 400K cached, 100K out, on Sonnet: 0.6*2 + 0.4*0.10 + 0.1*10
    assert cloud_prices.cost("sonnet-5.5", 1_000_000, 400_000, 100_000) == pytest.approx(1.2 + 0.04 + 1.0)
    assert cloud_prices.cost("opus-5.5", 1_000_000, 0, 1_000_000) == pytest.approx(24.0)
    # cached never above the prompt
    assert cloud_prices.cost("opus-5.5", 10, 50, 0) == pytest.approx(10 * 0.20 / 1e6)


def test_haiku_long_tier_is_per_request():
    # two requests: 50K prompt (normal) and 150K prompt (long), 1K out each, nothing cached
    normal = (50_000 * 0.10 + 1_000 * 0.50) / 1e6
    long_ = (150_000 * 0.50 + 1_000 * 2.50) / 1e6
    assert cloud_prices.cost("haiku-5.5", 200_000, 0, 2_000, 150_000, 0, 1_000) == pytest.approx(normal + long_)
    # without the long part, all at the normal rate
    assert cloud_prices.cost("haiku-5.5", 200_000, 0, 2_000) == pytest.approx((200_000 * 0.10 + 2_000 * 0.50) / 1e6)
    t = {r["model"]: r for r in cloud_prices.table()}
    assert t["haiku-5.5"]["assumed"] == ["cache_read"] and t["haiku-5.5"]["long"]["over"] == 100_000
    assert cloud_prices.AS_OF == "2026-11"


# ── the guard's log ──

def test_each_line_counts_once_and_survives_a_restart(tmp_path):
    led = ledger(tmp_path)
    lines = [chat(NOON + 1, prompt=1000, completion=100), chat(NOON + 2, who="speaker", prompt=500, completion=20),
             line(NOON + 3, event="warm"), "not a log line", chat(NOON + 4, status="loop_cut", prompt=None, completion=None)]
    assert led.feed(lines) == 3
    assert led.feed(lines) == 0                                  # docker logs --since repeats the last line
    led.save()
    assert stat.S_IMODE(os.stat(tmp_path / "local-usage.json").st_mode) == 0o600

    again = lc.Ledger(tmp_path / "local-usage.json", clock=lambda: NOON + 10)
    assert again.since_arg().startswith("2026-11-28T12:00:04.")
    assert again.feed(lines + [chat(NOON + 5, prompt=10, completion=1)]) == 1
    t = again.totals(["2026-11-28"])
    assert t["requests"] == 4 and t["tokens"]["prompt"] == 1510 and t["tokens"]["completion"] == 121
    assert t["by_who"]["speaker"]["prompt"] == 500 and t["by_who"]["person"]["requests"] == 3
    assert t["busy_s"] == pytest.approx(16.0)


def test_unsaved_lines_are_read_again_not_lost(tmp_path):
    led = ledger(tmp_path)
    led.save()
    led.feed([chat(NOON + 1)])                                   # never saved: the dashboard stopped
    again = lc.Ledger(tmp_path / "local-usage.json")
    assert again.feed([chat(NOON + 1)]) == 1                     # its high-water mark was not saved either


def test_first_start_reads_from_now(tmp_path):
    led = ledger(tmp_path)
    assert led.since_arg() == str(int(NOON))


def test_days_are_london_days(tmp_path):
    led = ledger(tmp_path)
    summer_late = datetime(2026, 7, 1, 23, 30, tzinfo=lc.timezone.utc).timestamp()   # 00:30 BST on 2 July
    led.feed([chat(summer_late)])
    assert "2026-07-02" in led.data["days"]


# ── energy ──

def test_energy_answering_loaded_and_gaps(tmp_path):
    led = ledger(tmp_path)
    t = NOON
    seq = [look(t, 100, counters=(0, 0, 0)),
           look(t + 15, 300, processing=1, counters=(1000, 3000, 50)),   # answering: 15 s at (100+300)/2
           look(t + 30, 100, counters=(1000, 3000, 50)),                 # answering (busy at the start)
           look(t + 45, 100, counters=(1000, 3000, 50)),                 # loaded, idle
           look(t + 45 + 600, 100, counters=(1000, 3000, 50)),           # a 10-minute gap: not counted
           look(t + 660, None, up=False),                                # PC off
           look(t + 675, None, up=False),
           look(t + 690, 30, loaded=False),                              # on again, nothing loaded
           look(t + 705, 30, loaded=False)]
    for s in seq:
        led.sample(lc.reading([s]))
    e = led.data["days"]["2026-11-28"]["energy"]
    assert e["answering_s"] == 30 and e["answering_wh"] == pytest.approx((200 * 15 + 200 * 15) / 3600)
    assert e["loaded_s"] == 15 and e["loaded_wh"] == pytest.approx(100 * 15 / 3600)
    assert led.idle_median() == 30.0
    assert led.data["days"]["2026-11-28"]["cache"] == {"processed": 1000, "cached": 3000}


def test_counters_moving_mean_answering_and_a_restart_counts_from_zero(tmp_path):
    led = ledger(tmp_path)
    led.sample(lc.reading([look(NOON, 150, counters=(5000, 0, 100))]))
    led.sample(lc.reading([look(NOON + 15, 150, counters=(5200, 0, 120))]))    # a short request between looks
    led.sample(lc.reading([look(NOON + 30, 150, counters=(300, 100, 10))]))    # llama-server restarted
    d = led.data["days"]["2026-11-28"]
    assert d["energy"]["answering_s"] == 30 and d["energy"]["loaded_s"] == 0
    assert d["cache"] == {"processed": 500, "cached": 100}


def test_same_look_twice_is_ignored(tmp_path):
    led = ledger(tmp_path)
    s = look(NOON, 100, loaded=False)
    led.sample(lc.reading([s]))
    led.sample(lc.reading([s]))
    assert led.data["idle_watts"] == [100.0]


def test_baseline_subtracted_measured_or_set(tmp_path):
    led = ledger(tmp_path)
    led.data["idle_watts"] = [20, 30, 40]
    day = led._day("2026-11-28")
    day["energy"].update(answering_wh=100.0, answering_s=3600.0, loaded_wh=50.0, loaded_s=3600.0)
    t = led.totals(["2026-11-28"])
    assert led.baseline() == (30.0, "measured")
    assert t["energy"]["answering_kwh"] == pytest.approx(0.070) and t["energy"]["loaded_kwh"] == pytest.approx(0.020)
    led.set_settings({"baseline_watts": 0})
    assert led.totals(["2026-11-28"])["energy"]["answering_kwh"] == pytest.approx(0.100)
    led.set_settings({"baseline_watts": 80})
    assert led.totals(["2026-11-28"])["energy"]["loaded_kwh"] == 0          # never below nothing
    led.data["idle_watts"] = []
    led.set_settings({"baseline_watts": None})
    assert led.baseline() == (0.0, "none")


# ── money ──

def _day(led, date, prompt, completion, cached_share=None, long_prompt=0, long_completion=0, answering_wh=0.0, loaded_wh=0.0):
    d = led._day(date)
    d["who"]["person"] = {**lc._who_row(), "requests": 10, "prompt": prompt, "completion": completion,
                          "long_requests": 1 if long_prompt else 0, "long_prompt": long_prompt, "long_completion": long_completion}
    if cached_share is not None:
        d["cache"] = {"processed": int(1000 * (1 - cached_share)), "cached": int(1000 * cached_share)}
    d["energy"].update(answering_wh=answering_wh, answering_s=3600.0 if answering_wh else 0.0,
                       loaded_wh=loaded_wh, loaded_s=3600.0 if loaded_wh else 0.0)


def test_cached_share_fx_and_savings(tmp_path):
    led = ledger(tmp_path)
    led.set_settings({"baseline_watts": 0})
    _day(led, "2026-11-28", 1_000_000, 100_000, cached_share=0.75, answering_wh=1000.0, loaded_wh=1000.0)
    t = led.totals(["2026-11-28"])
    assert t["tokens"]["cached"] == 750_000 and t["cache_measured"]
    assert t["local"]["total"] == pytest.approx(2 * 0.25)                     # 2 kWh at £0.25
    sonnet = next(c for c in t["cloud"] if c["model"] == "sonnet-5.5")
    usd = (250_000 * 2 + 750_000 * 0.10 + 100_000 * 10) / 1e6
    assert sonnet["usd"] == pytest.approx(usd) and sonnet["cost"] == pytest.approx(usd * 0.79)
    assert sonnet["saved"] == pytest.approx(usd * 0.79 - 0.5)
    led.set_settings({"currency": "eur", "usd_rate": 0.9, "price_per_kwh": 0.3})
    t = led.totals(["2026-11-28"])
    assert next(c for c in t["cloud"] if c["model"] == "sonnet-5.5")["cost"] == pytest.approx(usd * 0.9)
    assert t["local"]["total"] == pytest.approx(0.6)
    assert led.set_settings({"currency": "USD"})["usd_rate"] == 1.0


def test_no_cache_numbers_counts_nothing_cached_and_says_so(tmp_path):
    led = ledger(tmp_path)
    _day(led, "2026-11-28", 1000, 10)
    t = led.totals(["2026-11-28"])
    assert t["tokens"]["cached"] == 0 and not t["cache_measured"] and t["days_without_cache"] == 1


def test_cloud_can_be_cheaper(tmp_path):
    # a few tokens and a GPU kept loaded all day: Haiku would have cost less than the electricity
    led = ledger(tmp_path)
    led.set_settings({"baseline_watts": 0})
    _day(led, "2026-11-28", 20_000, 2_000, cached_share=0.5, loaded_wh=2400.0)
    t = led.totals(["2026-11-28"])
    haiku = next(c for c in t["cloud"] if c["model"] == "haiku-5.5")
    assert haiku["saved"] < 0 and t["local"]["total"] == pytest.approx(0.6)


def test_long_requests_priced_apart_for_haiku(tmp_path):
    led = ledger(tmp_path)
    led.feed([chat(NOON + 1, prompt=150_000, completion=1000), chat(NOON + 2, prompt=50_000, completion=1000)])
    t = led.totals(["2026-11-28"])
    assert t["long_requests"] == 1
    haiku = next(c for c in t["cloud"] if c["model"] == "haiku-5.5")
    assert haiku["usd"] == pytest.approx((150_000 * 0.50 + 1000 * 2.50 + 50_000 * 0.10 + 1000 * 0.50) / 1e6)


def test_summary_periods_and_projection(tmp_path):
    since = datetime(2026, 11, 21, 12, 0, tzinfo=lc.LONDON).timestamp()
    led = lc.Ledger(tmp_path / "l.json", clock=lambda: NOON)
    led.data["since"] = since
    led.set_settings({"baseline_watts": 0})
    for i in range(7):
        _day(led, f"2026-11-{22 + i}", 100_000, 10_000, cached_share=0.5, answering_wh=400.0)
    _day(led, "2026-10-31", 100_000, 10_000)
    s = led.summary()
    assert s["today"]["tokens"]["prompt"] == 100_000
    assert s["this_month"]["tokens"]["prompt"] == 700_000 and s["all"]["tokens"]["prompt"] == 800_000
    p = s["projected"]
    # 7 days' cost over the 6.5 days since the window began, carried on for the 2.5 days left
    rate = 7 * 0.1 / 6.5
    assert p["local"] == pytest.approx(0.7 + rate * 2.5, rel=1e-3)
    assert s["settings"]["baseline_from"] == "setting" and s["prices"]["as_of"] == "2026-11"


def test_settings_are_checked(tmp_path):
    led = ledger(tmp_path)
    for bad in ({"price_per_kwh": -1}, {"currency": "pounds"}, {"usd_rate": 0}, {"baseline_watts": 5000}, {"price_per_kwh": "x"}):
        with pytest.raises(ValueError):
            led.set_settings(bad)
    assert led.settings == lc.DEFAULTS


def test_prometheus_text(tmp_path):
    led = ledger(tmp_path)
    led.set_settings({"baseline_watts": 0})
    _day(led, "2026-11-28", 1000, 100, cached_share=0.5, answering_wh=500.0, loaded_wh=250.0)
    text = led.render()
    assert 'logos_local_energy_kwh_total{phase="answering"} 0.5' in text
    assert 'logos_local_energy_kwh_total{phase="loaded"} 0.25' in text
    assert 'logos_local_cost_total{currency="GBP"} 0.1875' in text
    assert 'logos_cloud_equivalent_cost_usd_total{model="opus-5.5"}' in text
    assert 'logos_tokens_total{kind="cached"} 500' in text
    assert lc.Ledger(tmp_path / "none.json").render() == ""        # not tracking yet: nothing


def test_step_reads_pc_guard_and_log(tmp_path, monkeypatch):
    from gateway import dashboard_api, model_server, model_server_metrics as msm

    class P:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    calls = []

    class Runner:
        def http(self, url, timeout=0):
            if url.endswith("/guard/health"):
                return json.dumps({"queue": {"running": 1}}).encode()
            raise OSError("no")

        def run(self, argv, **kw):
            calls.append(argv)
            return P(chat(NOON + 5) + "\n")

    class RT:
        runner = Runner()
        env = {}

    monkeypatch.setattr(dashboard_api, "_local_definitions", lambda rt: {})
    monkeypatch.setattr(model_server, "wanted", lambda defs: {"http://192.0.2.10:8080": {}})
    monkeypatch.setattr(msm.SCRAPER, "get", lambda http, base: look(NOON, 200, counters=(0, 0, 0)))
    from gateway import model_gateway
    monkeypatch.setattr(model_gateway.ModelGateway, "bind_address", lambda self: "127.0.0.1")
    led = ledger(tmp_path)
    lc.step(led, RT())
    assert led.prev["busy"] is True and led.prev["watts"] == 200
    assert calls[0][:4] == ["docker", "logs", "--timestamps", "--since"]
    assert led.totals(["2026-11-28"])["requests"] == 1
