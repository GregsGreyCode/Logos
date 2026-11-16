"""The gateway's view of the noticeboard, used by the dashboard.

The gateway holds no noticeboard identity of its own. For an agent the signed-in
person owns, it reads that agent's token from the agent's home volume and talks
to the noticeboard as that agent: to list its messages, and to send the answer
the person chose with a button. A person's decision is the authority the
noticeboard rules ask for, so no model is involved in passing it on.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp


class NoticeboardError(Exception):
    """The noticeboard refused or could not be reached. ``str()`` is safe to show."""


def _host_module():
    name = "logos_noticeboard_host"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("noticeboard_host.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


class NoticeboardClient:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self._host = None
        self._tokens: Dict[str, str] = {}
        self._n = 0

    def uses(self, definition: Dict[str, Any]) -> bool:
        return _host_module().uses_noticeboard(definition)

    async def _endpoints(self) -> tuple[str, str]:
        if self._host is None:
            self._host = _host_module().NoticeboardHost(self.runtime)
        return (await asyncio.to_thread(self._host.url), await asyncio.to_thread(self._host.poll_url))

    async def _token(self, agent: str) -> str:
        if agent not in self._tokens:
            await self._endpoints()
            token = await asyncio.to_thread(self._host.token_for, agent)
            if not token:
                raise NoticeboardError(f"{agent} is not on the noticeboard yet")
            self._tokens[agent] = token
        return self._tokens[agent]

    async def history(self, agent: str) -> List[dict]:
        _mcp, poll = await self._endpoints()
        headers = {"Authorization": f"Bearer {await self._token(agent)}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(poll + "/history", headers=headers) as r:
                    if r.status == 401:
                        self._tokens.pop(agent, None)       # the agent was recreated with a new token
                        raise NoticeboardError("the noticeboard did not accept this agent's token")
                    r.raise_for_status()
                    return (await r.json())["messages"]
        except aiohttp.ClientError as e:
            raise NoticeboardError(f"the noticeboard is not reachable ({type(e).__name__})") from None

    async def answer(self, agent: str, to: str, reply_to: int, decision: str, text: str) -> dict:
        """Send ``agent``'s answer to request ``reply_to``. Raises with the noticeboard's reason."""
        mcp, _poll = await self._endpoints()
        self._n += 1
        body = {"jsonrpc": "2.0", "id": self._n, "method": "tools/call", "params": {
            "name": "send_message",
            "arguments": {"to": to, "kind": "answer", "reply_to": reply_to, "decision": decision, "text": text}}}
        headers = {"Authorization": f"Bearer {await self._token(agent)}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(mcp, json=body, headers=headers) as r:
                    r.raise_for_status()
                    result = (await r.json())["result"]
        except aiohttp.ClientError as e:
            raise NoticeboardError(f"the noticeboard is not reachable ({type(e).__name__})") from None
        text_out = result["content"][0]["text"]
        if result.get("isError"):
            raise NoticeboardError(text_out)
        return {"ok": True}
