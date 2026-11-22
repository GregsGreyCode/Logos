"""logos-job-limits: a scheduled job never sends on the noticeboard (Greg, 22 Nov).

On 22 Nov Béré's agent, writing her weekly digest, sent it to the household agent as an FYI:
her own notes (a childhood memory, wanting to relearn knitting) reached the shared agent, and so
Greg's Notifications. The rules already say an agent sends only when its person asks, and a job
is not its person asking; a line in the digest's instructions was read the wrong way. Words can be
misread again, so this is enforced here: during a scheduled job (hermes's platform "cron") a call
to ``send_message`` is refused, and the model is told why. No Logos job needs to send (the
noticeboard check only reads), so nothing that should work is stopped.

hermes's pre_tool_call does not say which platform a turn is on; pre_llm_call does, at the start
of every turn, so the sessions that are scheduled jobs are remembered from there.
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
BLOCKED_TOOLS = ("send_message",)          # the noticeboard's (as an MCP tool it may carry a prefix)
KEEP = 500
REFUSAL = ("Not sent: a scheduled job does not send messages to other agents or anyone else. Its reply "
           "goes to your person only. If something should be shared, tell your person and let them ask.")

_lock = threading.Lock()
_jobs: dict = {}                           # session id -> when it was seen as a scheduled job


def _log(event: str, **fields) -> None:
    fields.update(at=time.strftime("%Y-%m-%d %H:%M:%S"), event=event)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields) + "\n")
    except OSError:
        pass


def _remember(session_id="", platform="", **_):
    if session_id and str(platform) in JOB_PLATFORMS:
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
