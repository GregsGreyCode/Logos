"""The eval threads in cases.json as a mail service with made-up mailboxes (no mail server, no
network): the real gateway/mail_service.py, its copy filled from the case. Used by run.py (with a
model) and by tests/unit/test_mail_reply_facts.py (without one)."""
import email.utils
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
AGENT = "alex"


def mail_service():
    """gateway/mail_service.py (standard library only), loaded by path."""
    name = "logos_mail_service_eval"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "gateway" / "mail_service.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def load(path=HERE / "cases.json") -> dict:
    return json.loads(Path(path).read_text())


class FakeBox:
    """One mailbox's Inbox and Sent, as the real Gmail and Outlook clients return them."""

    def __init__(self, rows):
        self.rows = rows

    def fetch(self, folder, since, known=(), until=None):
        return [dict(r) for r in self.rows if r["folder"] == folder]


def rows_for(case: dict, data: dict, ms, at: datetime) -> dict:
    """mailbox id -> rows, from the case's messages (``inbox`` defaults to the first mailbox)."""
    by_address = {b["address"]: b for b in data["inboxes"]}
    first = data["inboxes"][0]["address"]
    out = {b["id"]: [] for b in data["inboxes"]}
    for i, m in enumerate(case["messages"], 1):
        box = by_address[m.get("inbox") or first]
        name, addr = email.utils.parseaddr(m["from"])
        flags = ms.header_flags({k.lower(): str(v) for k, v in (m.get("headers") or {}).items()})
        mid = f"g:{m['folder']}:{i}" if box["kind"] == "gmail" else f"o:{case['name']}-{i}"
        out[box["id"]].append({
            "folder": m["folder"], "id": mid, "thread": m.get("thread") or "",
            "date": ms.iso(at - timedelta(hours=m["hours_ago"])), "from_name": name, "from_addr": addr.lower(),
            "to_addrs": m.get("to") or "", "cc_addrs": m.get("cc") or "", "subject": m["subject"],
            "snippet": m["body"][:240].replace("\n", " "), "body": m["body"], "unread": m["folder"] == "inbox",
            "automated": bool(ms.bulk(flags) or ms.AUTOMATED.search(addr)), "attachments": "", "headers": flags})
    return out


def build(case: dict, data: dict, state_dir, at=None):
    """The mail service for one case, synced, and the id (as the tools give it) of the email under
    test: ``target`` (1-based) or the newest received one."""
    ms = mail_service()
    at = at or datetime.now(timezone.utc)
    rows = rows_for(case, data, ms, at)
    boxes = {b["id"]: {"client": FakeBox(rows[b["id"]]), "person": AGENT, "kind": b["kind"],
                       "address": b["address"], "exclude": []} for b in data["inboxes"]}
    svc = ms.Service(ms.Store(str(Path(state_dir) / "mail.db")), {AGENT: {"token": "t", "person": AGENT}}, boxes,
                     log=lambda m: None)
    for bid in boxes:
        svc.sync(bid)
    every = [(bid, r) for bid, rs in rows.items() for r in rs]
    if case.get("target"):
        bid, r = every[case["target"] - 1]
    else:
        bid, r = max(((b, r) for b, r in every if r["folder"] == "inbox"), key=lambda x: x[1]["date"])
    return svc, f"{bid}:{r['id']}"


def facts(case: dict, data: dict, state_dir) -> dict:
    """Logos' reply facts for the email under test (no model)."""
    svc, mid = build(case, data, state_dir)
    row = svc._find(AGENT, mid)
    return svc.facts_for(AGENT, row)
