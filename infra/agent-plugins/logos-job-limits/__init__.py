"""logos-job-limits: Logos's own scheduled jobs never send messages (Greg, 22 Nov).

On 22 Nov Béré's agent, writing her weekly digest, sent it to the household agent as an FYI:
her own notes (a childhood memory, wanting to relearn knitting) reached the shared agent, and so
Greg's Notifications. The rules already say an agent sends only when its person asks, and a job
is not its person asking; a line in the digest's instructions was read the wrong way. Words can be
misread again, so this is enforced here: during one of Logos's own jobs (the digest, the briefings,
the brain tidy-up, the noticeboard check, the daily changes: their prompts carry LOGOS_JOB_MARK) a
call to ``send_message`` is refused, and the model is told why. None of them needs to send.

A job a person set up in chat ("every Friday remind Béré's agent the bins go out") has no mark and
may send: the person asked for it, ahead of time. The noticeboard's own rules still apply to it.

hermes's pre_tool_call says neither the platform nor the job; pre_llm_call gives both (platform and
the job's prompt as the user message), so those sessions are remembered from there.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger("logos.job_limits")
HOME = os.environ.get("HERMES_HOME", "/opt/data")
LOG = os.path.join(HOME, "logs", "job-limits.log")
JOB_PLATFORMS = {"cron"}
MARK = "[Logos job:"                       # gateway/agent_runtime.py LOGOS_JOB_MARK
BLOCKED_TOOLS = ("send_message",)          # the noticeboard's (as an MCP tool it may carry a prefix)
KEEP = 500
REFUSAL = ("Not sent: this scheduled job does not send messages to other agents or anyone else. Its reply "
           "goes to your person only. If something should be shared, say so in your reply and let them ask.")

_lock = threading.Lock()
_jobs: dict = {}                           # session id -> when it was seen as a scheduled job


def _log(event: str, **fields) -> None:
    fields.update(at=time.strftime("%Y-%m-%d %H:%M:%S"), event=event)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields) + "\n")
    except OSError:
        pass


def _remember(session_id="", platform="", user_message="", **_):
    if session_id and str(platform) in JOB_PLATFORMS and MARK in str(user_message or ""):
        with _lock:
            _jobs[session_id] = time.time()
            if len(_jobs) > KEEP:
                for sid, _t in sorted(_jobs.items(), key=lambda kv: kv[1])[: len(_jobs) - KEEP]:
                    _jobs.pop(sid, None)
    return None


def blocked(tool_name: str) -> bool:
    name = str(tool_name or "")
    return any(name == t or name.endswith("_" + t) or name.endswith("." + t) for t in BLOCKED_TOOLS)


def _check(tool_name="", session_id="", **_):
    if not blocked(tool_name):
        return None
    with _lock:
        in_job = session_id in _jobs
    if not in_job:
        return None
    _log("refused", tool=tool_name, session=str(session_id)[:16])
    logger.warning("job-limits: %s refused during a scheduled job", tool_name)
    return {"action": "block", "message": REFUSAL}


def register(ctx):
    ctx.register_hook("pre_llm_call", _remember)
    ctx.register_hook("pre_tool_call", _check)
