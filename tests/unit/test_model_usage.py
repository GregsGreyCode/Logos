"""gateway/model_usage.py: spend, current model and projection from the gateway's records."""
from datetime import datetime, timezone

from gateway import model_usage as mu

NOW = datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)      # a 30-day month, 10 days left after today
T = "2026-11-20 {}:00.123456789+00:00"


def test_current_model_is_the_latest_success_and_since_is_the_start_of_that_run():
    rows = [["agent-greg", T.format("15:20"), "anthropic", "claude-haiku-5-5", "success"],
            ["agent-greg", T.format("15:10"), "lm-studio", "qwen/q", "error"],        # failed attempts do not count
            ["agent-greg", T.format("14:05"), "anthropic", "claude-haiku-5-5", "success"],
            ["agent-greg", T.format("11:00"), "lm-studio", "qwen/q", "success"]]
    cur = mu.current_model(rows)
    assert (cur["provider"], cur["kind"], cur["model"]) == ("anthropic", "paid", "claude-haiku-5-5")
    assert cur["since"] == "2026-11-20T14:05:00+00:00" and cur["last_call"] == "2026-11-20T15:20:00+00:00"
    assert mu.current_model([["agent-greg", T.format("15:10"), "lm-studio", "qwen/q", "error"]]) is None
    assert mu.current_model([]) is None


def test_projection_weights_the_rest_of_today_by_hour():
    first = datetime(2026, 11, 1, tzinfo=timezone.utc)
    # $0.70 at 20:00 on each of the last 7 days, nothing at other hours: $0.10 an evening on average... times 7 days
    hourly = {(f"2026-11-{d:02d}", 20): 0.70 for d in range(13, 20)}
    # average day $0.70; today's 20:00 is still to come; 10 more days after today
    assert round(mu.project(5.0, hourly, NOW, first), 4) == round(5.0 + 0.70 + 10 * 0.70, 4)
    late = NOW.replace(hour=21)                                    # past the expensive hour: nothing more today
    assert round(mu.project(5.0, hourly, late, first), 4) == round(5.0 + 10 * 0.70, 4)
    assert mu.project(2.5, {}, NOW, first) == 2.5


def test_projection_with_less_than_a_week_of_records_averages_over_what_exists():
    first = datetime(2026, 11, 18, 15, 30, tzinfo=timezone.utc)   # two days of records
    hourly = {("2026-11-19", 9): 1.0}
    assert round(mu.project(1.0, hourly, NOW, first), 4) == round(1.0 + 10 * 0.5, 4)


RAW = {
    "first": "2026-11-01 00:00:00.000000000+00:00",
    "month": [["agent-greg", "lm-studio", "qwen/q", 80, 800000, 6000, 0, 0.0],
              ["agent-greg", "anthropic", "claude-haiku-5-5", 40, 400000, 3000, 300000, 0.0415],
              ["agent-bere", "lm-studio", "qwen/q", 45, 450000, 4000, 0, 0.0],
              ["agent-bench", "anthropic", "claude-haiku-5-5", 5, 50000, 300, 0, 0.0052]],
    "daily": [["2026-11-19", "agent-greg", "lm-studio", 50, 0.0], ["2026-11-19", "agent-greg", "anthropic", 30, 0.03],
              ["2026-11-20", "agent-greg", "anthropic", 10, 0.0115], ["2026-11-20", "agent-bere", "lm-studio", 45, 0.0],
              ["2026-11-20", "agent-stranger", "anthropic", 99, 9.0]],
    "hourly": [["2026-11-19", 20, "agent-greg", 0.03]],
    "last": [["agent-greg", T.format("15:20"), "anthropic", "claude-haiku-5-5", "success"],
             ["agent-bere", T.format("09:00"), "lm-studio", "qwen/q", "success"]],
    "budgets": [["budget-greg-anthropic", 10.0, 0.0415], ["budget-bere-anthropic", 10.0, 10.0]],
}
AGENTS = {"greg": {"display_name": "Greg's agent", "budget": {"monthly_usd": 10}},
          "bere": {"display_name": "Béré's agent", "budget": {"monthly_usd": 10}}}
CHAINS = {n: [{"provider": "anthropic", "model": "claude-haiku-5-5", "monthly_usd": 10.0}] for n in AGENTS}


def test_summary_per_agent_and_only_for_the_agents_asked_for():
    s = mu.summarise(RAW, AGENTS, CHAINS, NOW)
    greg = next(a for a in s["agents"] if a["name"] == "greg")
    bere = next(a for a in s["agents"] if a["name"] == "bere")
    assert [a["name"] for a in s["agents"]] == ["bere", "greg"]              # bench was not asked for
    assert greg["month_usd"] == 0.0415 and greg["left_usd"] == 9.9585 and greg["limit_usd"] == 10.0
    assert greg["models"][0]["model"] == "claude-haiku-5-5" and greg["models"][0]["cached_tokens"] == 300000
    assert greg["current"]["kind"] == "paid" and bere["current"]["kind"] == "local"
    assert greg["fallbacks"] == [{"provider": "anthropic", "model": "claude-haiku-5-5", "limit_usd": 10.0,
                                  "used_usd": 0.0415, "spent_out": False}]
    assert bere["fallbacks"][0]["spent_out"] is True
    assert greg["projected_usd"] > greg["month_usd"] and greg["over"] is False
    assert s["total"]["month_usd"] == 0.0415 and s["total"]["limit_usd"] == 20.0
    assert [(p["provider"], p["requests"]) for p in s["providers"]] == [("anthropic", 40), ("lm-studio", 125)]


def test_daily_series_covers_thirty_days_and_leaves_out_other_agents():
    s = mu.summarise(RAW, AGENTS, CHAINS, NOW)
    assert len(s["daily"]) == 30 and s["daily"][-1]["date"] == "2026-11-20" and s["daily"][0]["date"] == "2026-10-22"
    assert s["daily"][-1] == {"date": "2026-11-20", "local": 45, "paid": 10, "usd": 0.0115}   # the stranger's $9 is not ours
    assert s["daily"][-2] == {"date": "2026-11-19", "local": 50, "paid": 30, "usd": 0.03}
    assert s["daily"][0] == {"date": "2026-10-22", "local": 0, "paid": 0, "usd": 0.0}


def test_empty_records_still_give_a_page():
    s = mu.summarise({}, AGENTS, {}, NOW)
    assert [a["month_usd"] for a in s["agents"]] == [0.0, 0.0] and s["agents"][0]["current"] is None
    assert s["agents"][0]["fallbacks"] == [] and len(s["daily"]) == 30
