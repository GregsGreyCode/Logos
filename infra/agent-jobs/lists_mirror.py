"""A read-only copy of the shared lists in the household brain, for Obsidian.

The shared lists (shopping, to-do) live in Home Assistant. Every quarter of an hour, with no model,
this asks the house service for them (the same list_items the agents use) and writes them to
``/opt/data/brain/lists.md``, which Syncthing carries to both phones. The page says it is a copy:
an edit made there is overwritten. It is rewritten only when the lists changed, so the brain's git
history stays quiet. Prints nothing; never fails. Standard library only.
"""
import datetime
import json
import os
import urllib.request

BRAIN = os.environ.get("LOGOS_BRAIN", "/opt/data/brain")
PAGE = os.path.join(BRAIN, "lists.md")
HEADER = ("# Shared lists\n\n"
          "A copy of the Home Assistant lists, refreshed every 15 minutes. Do not edit it here: changes are "
          "overwritten. Change the lists in the Home Assistant app, or ask an agent.\n")


def env():
    try:
        return dict(l.strip().split("=", 1) for l in open("/opt/data/.env") if "=" in l and not l.startswith("#"))
    except OSError:
        return {}


def fetch(url, token):
    """The lists as list_items returns them, or None when they cannot be read."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_items", "arguments": {}}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    try:
        result = json.load(urllib.request.urlopen(req, timeout=15))["result"]
        if result.get("isError"):
            return None
        return json.loads(result["content"][0]["text"])["lists"]
    except Exception:
        return None


def line(item):
    """One open item: its title, then its due date and details when it has them."""
    if not isinstance(item, dict):
        return f"- [ ] {item}\n"
    text = f"- [ ] {item.get('item', '')}"
    if item.get("due"):
        text += f" (due {item['due']})"
    if item.get("details"):
        text += "\n" + "".join(f"    {d}\n" for d in str(item["details"]).splitlines() if d.strip()).rstrip("\n")
    return text + "\n"


def render(lists):
    out = [HEADER]
    for lst in lists:
        out.append(f"\n## {lst.get('list') or 'List'}\n\n")
        items = lst.get("open") or []
        out.append("".join(line(i) for i in items) if items else "Nothing on it.\n")
    return "".join(out)


def write_if_changed(text, path=PAGE):
    """Write the page when the lists changed (the date line is not compared). True when written."""
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = None
    stamp = f"\nLast changed {datetime.datetime.now():%Y-%m-%d %H:%M}.\n"
    if old is not None and old.rsplit("\nLast changed ", 1)[0] == text.rstrip("\n") + "\n":
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n" + stamp)
    os.replace(tmp, path)
    return True


def main():
    e = env()
    if not e.get("LOGOS_HOUSE_URL") or not e.get("LOGOS_HOUSE_TOKEN"):
        return
    lists = fetch(e["LOGOS_HOUSE_URL"], e["LOGOS_HOUSE_TOKEN"])
    if lists is None:
        return                     # Home Assistant or the house service is down: keep the last copy
    try:
        write_if_changed(render(lists))
    except OSError:
        pass


if __name__ == "__main__":
    main()
