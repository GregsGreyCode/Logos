"""logos-job-limits: a scheduled job never sends on the noticeboard (Greg, 22 Nov)."""
import importlib.util
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "infra" / "agent-plugins" / "logos-job-limits" / "__init__.py"


def load(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("logos_job_limits_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MARKED = "[IMPORTANT: You are running as a scheduled cron job.]\n\n[Logos job: its reply goes to your person only; it sends nothing to anyone else.]\n\nWeekly digest…"


def test_logos_own_jobs_cannot_send_but_a_chat_or_a_persons_own_job_can(tmp_path, monkeypatch):
    jl = load(tmp_path, monkeypatch)
    from gateway.agent_runtime import LOGOS_JOB_MARK
    assert LOGOS_JOB_MARK.startswith(jl.MARK)                  # the plugin knows the mark the runtime writes
    jl._remember(session_id="digest-run", platform="cron", user_message=MARKED)
    jl._remember(session_id="bins-friday", platform="cron", user_message="Every Friday: tell Béré's agent the bins go out.")
    jl._remember(session_id="greg-chat", platform="matrix", user_message=MARKED)
    out = jl._check(tool_name="send_message", session_id="digest-run", args={"to": "household"})
    assert out["action"] == "block" and "scheduled job" in out["message"]
    assert jl._check(tool_name="mcp_noticeboard_send_message", session_id="digest-run")["action"] == "block"
    assert jl._check(tool_name="send_message", session_id="greg-chat") is None          # its person asked
    assert jl._check(tool_name="send_message", session_id="bins-friday") is None        # set up by its person
    assert jl._check(tool_name="read_messages", session_id="digest-run") is None         # reading is fine
    assert jl._check(tool_name="send_message", session_id="never-seen") is None
    assert "refused" in (tmp_path / "logs" / "job-limits.log").read_text()


def test_registers_both_hooks_and_forgets_old_sessions(tmp_path, monkeypatch):
    jl = load(tmp_path, monkeypatch)
    hooks = []
    jl.register(types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name)))
    assert hooks == ["pre_llm_call", "pre_tool_call"]
    for i in range(jl.KEEP + 20):
        jl._remember(session_id=f"s{i}", platform="cron", user_message=MARKED)
    assert len(jl._jobs) == jl.KEEP and "s0" not in jl._jobs
    assert not jl.blocked("resend_messages") and jl.blocked("noticeboard.send_message")
