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


def test_a_scheduled_job_cannot_send_but_a_chat_can(tmp_path, monkeypatch):
    jl = load(tmp_path, monkeypatch)
    jl._remember(session_id="digest-run", platform="cron")
    jl._remember(session_id="greg-chat", platform="matrix")
    out = jl._check(tool_name="send_message", session_id="digest-run", args={"to": "household"})
    assert out["action"] == "block" and "scheduled job" in out["message"]
    assert jl._check(tool_name="mcp_noticeboard_send_message", session_id="digest-run")["action"] == "block"
    assert jl._check(tool_name="send_message", session_id="greg-chat") is None          # its person asked
    assert jl._check(tool_name="read_messages", session_id="digest-run") is None         # reading is fine
    assert jl._check(tool_name="send_message", session_id="never-seen") is None
    assert "refused" in (tmp_path / "logs" / "job-limits.log").read_text()


def test_registers_both_hooks_and_forgets_old_sessions(tmp_path, monkeypatch):
    jl = load(tmp_path, monkeypatch)
    hooks = []
    jl.register(types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(name)))
    assert hooks == ["pre_llm_call", "pre_tool_call"]
    for i in range(jl.KEEP + 20):
        jl._remember(session_id=f"s{i}", platform="cron")
    assert len(jl._jobs) == jl.KEEP and "s0" not in jl._jobs
    assert not jl.blocked("resend_messages") and jl.blocked("noticeboard.send_message")
