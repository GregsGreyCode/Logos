"""The dashboard's access to an agent's brain (its markdown vault).

The vault lives in the Docker volume ``logos-brain-<agent>``. It is reached through a
short-lived helper container with no network. Listing, reading and searching mount the
volume read-only. The one write, :func:`write_page`, replaces a single existing page with
the person's edit and refuses if the page changed since they opened it.

Every function blocks (it runs ``docker``); call it with ``asyncio.to_thread``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

HELPER = "alpine/git:latest"            # has sh, find and git; already used to seed brains
MAX_PAGE_BYTES = 400_000
_SAFE_PATH = re.compile(r"^[^/\x00][^\x00]*\.md$")
# Syncthing keeps both versions when a page changed in two places before they synced: the other
# one as "<name>.sync-conflict-<date>-<time>-<device>.md" next to it.
_CONFLICT = re.compile(r"\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]+(?=\.md$)")

_LIST = r'''
cd /v 2>/dev/null || exit 0
find . -name '*.md' -not -path './.*/*' | while read -r f; do
  printf 'P\t%s\t%s\t%s\n' "${f#./}" "$(stat -c %Y "$f")" "$(grep -m1 '^# ' "$f" | cut -c3-120)"
done
git config --global --add safe.directory /v 2>/dev/null
git log --since='7 days ago' --name-only --pretty=format:'C%x09%ct' 2>/dev/null | grep -v '^$'
# What changed in the last day, per page: lines added and removed, and the first new lines (Home shows
# them as a short "what was added", not the file's path).
base=$(git rev-list -1 --before='24 hours ago' HEAD 2>/dev/null)
[ -n "$base" ] || base=4b825dc642cb6eb9a060e54bf8d69288fbee4904      # no history that old: compare with an empty brain
if [ -n "$base" ]; then
  git diff --numstat "$base" HEAD -- '*.md' 2>/dev/null | while IFS="$(printf '\t')" read -r a r f; do
    printf 'D\t%s\t%s\t%s\n' "$f" "$a" "$r"
    git diff -U0 "$base" HEAD -- "$f" 2>/dev/null | grep '^+[^+]' | head -12 | cut -c2-200 | tr '\t' ' ' \
      | while read -r l; do printf 'A\t%s\t%s\n' "$f" "$l"; done
  done
fi
'''
_SEARCH = r'''
cd /v 2>/dev/null || exit 0
find . -name '*.md' -not -path './.*/*' -exec grep -ilF -- "$Q" {} + 2>/dev/null | head -40 | while read -r f; do
  printf '%s\t%s\n' "${f#./}" "$(grep -m1 -iF -- "$Q" "$f" | cut -c1-160)"
done
'''


class BrainError(Exception):
    """The brain could not be read. ``str()`` is safe to show."""


def volume(agent: str) -> str:
    return f"logos-brain-{agent}"


def safe_path(path: str) -> str:
    """A vault-relative markdown path, or raise. No absolute paths, no ``..``, no hidden folders."""
    path = (path or "").strip()
    parts = path.split("/")
    if (not _SAFE_PATH.match(path) or len(path) > 300
            or any(p in ("", ".", "..") or p.startswith(".") for p in parts)):
        raise BrainError("That is not a page in this brain.")
    return path


def _run(runtime: Any, agent: str, argv: List[str], env: Dict[str, str] | None = None) -> str:
    vol = volume(agent)
    if runtime.runner.run(["docker", "volume", "inspect", vol]).returncode != 0:
        raise BrainError("This agent has no brain yet.")
    cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{vol}:/v:ro"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    p = runtime.runner.run(cmd + argv)
    if p.returncode != 0:
        raise BrainError("The brain could not be read.")
    return p.stdout if isinstance(p.stdout, str) else p.stdout.decode(errors="replace")


def title_of(path: str, heading: str = "") -> str:
    return heading.strip() or path.rsplit("/", 1)[-1][:-3]


_MD = [(re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?"), ""),          # bullets, numbers, to-do boxes
       (re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]"), r"\2"), (re.compile(r"\[\[([^\]]+)\]\]"), r"\1"),
       (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"), (re.compile(r"[*_`]{1,3}"), ""), (re.compile(r"\s+"), " ")]
_FRONT = re.compile(r"^(?:---+|[A-Za-z_-]+:\s.*|[A-Za-z_-]+:)$")


def plain(line: str) -> str:
    """One line of markdown as plain words: no bullets, links, emphasis or front-matter keys."""
    line = line.strip()
    if not line or _FRONT.match(line):
        return ""
    for pat, to in _MD:
        line = pat.sub(to, line)
    return line.strip()


def summary_of(added: List[str]) -> str:
    """What was added to a page, in a line: its first two lines of text, else a new heading."""
    text = [plain(l) for l in added if not l.lstrip().startswith("#")]
    text = [t for t in text if len(t) > 2]
    if not text:
        heads = [plain(l.lstrip("# ")) for l in added if l.lstrip().startswith("#")]
        return f"New section: {heads[0]}" if heads and heads[0] else ""
    s = text[0] if len(text[0]) >= 60 or len(text) == 1 else f"{text[0]} · {text[1]}"
    return s if len(s) <= 140 else s[:139].rsplit(" ", 1)[0] + "…"


def parse_listing(out: str) -> Dict[str, list]:
    """Turn the listing script's output into ``{"pages": [...], "changes": [...]}``."""
    pages, changes, conflicts, seen = [], [], [], set()
    stamp, sizes, added = 0, {}, {}
    for line in out.splitlines():
        if line.startswith("D\t"):
            _, path, a, r = (line.split("\t", 3) + ["", ""])[:4]
            sizes[path] = (int(a) if a.isdigit() else 0, int(r) if r.isdigit() else 0)
            continue
        if line.startswith("A\t"):
            _, path, text = (line.split("\t", 2) + [""])[:3]
            added.setdefault(path, []).append(text)
            continue
        if line.startswith("P\t"):
            _, path, mtime, heading = (line.split("\t", 3) + [""])[:4]
            folder = path.split("/", 1)[0] if "/" in path else ""
            if _CONFLICT.search(path):
                conflicts.append({"path": path, "of": _CONFLICT.sub("", path),
                                  "modified": int(mtime) if mtime.isdigit() else 0})
                continue
            pages.append({"path": path, "folder": folder, "title": title_of(path, heading),
                          "modified": int(mtime) if mtime.isdigit() else 0})
        elif line.startswith("C\t"):
            stamp = int(line.split("\t", 1)[1] or 0)
        elif line.endswith(".md") and line not in seen:      # a file named by the commit above
            seen.add(line)
            changes.append({"path": line, "when": stamp})
    known = {p["path"]: p["title"] for p in pages}
    changes = [{**c, "title": known[c["path"]], "summary": summary_of(added.get(c["path"], [])),
                "added": sizes.get(c["path"], (0, 0))[0], "removed": sizes.get(c["path"], (0, 0))[1]}
               for c in changes if c["path"] in known]
    pages.sort(key=lambda p: (p["folder"], -p["modified"] if p["folder"] in ("daily", "inbox") else 0, p["title"].lower()))
    for c in conflicts:
        c["title"] = known.get(c["of"], title_of(c["of"]))
    return {"pages": pages, "changes": changes[:12], "conflicts": conflicts}


def list_pages(runtime: Any, agent: str) -> Dict[str, list]:
    return parse_listing(_run(runtime, agent, ["--entrypoint", "sh", HELPER, "-c", _LIST]))


_READ = r'''stat -c %Y "/v/$P" || exit 4; head -c "$N" -- "/v/$P"'''
# Refuse if the page changed since it was opened (exit 3), then replace it in one step so a
# reader or the sync never sees half a file. Owned by the sandbox user, like every other page.
_WRITE = r'''
f="/v/$P"
[ -f "$f" ] || exit 4
[ "$(stat -c %Y "$f")" = "$EXPECT" ] || exit 3
t="$(dirname "$f")/.logos-edit.$$"
cat > "$t" && chown 10000:10000 "$t" && chmod 0644 "$t" && mv -f "$t" "$f" && stat -c %Y "$f"
'''


def read_page(runtime: Any, agent: str, path: str) -> Dict[str, Any]:
    path = safe_path(path)
    # The path travels as an environment variable, never spliced into the shell text.
    out = _run(runtime, agent, ["--entrypoint", "sh", HELPER, "-c", _READ], env={"P": path, "N": str(MAX_PAGE_BYTES)})
    stamp, _, text = out.partition("\n")
    first = next((l[2:] for l in text.splitlines() if l.startswith("# ")), "")
    return {"path": path, "title": title_of(path, first), "text": text,
            "modified": int(stamp) if stamp.strip().isdigit() else 0}


class PageChanged(BrainError):
    """The page was changed (by the agent, or on another device) after it was opened."""


def write_page(runtime: Any, agent: str, path: str, text: str, expected_modified: int) -> Dict[str, Any]:
    """Replace an existing page with ``text``. Raises :class:`PageChanged` if it is no longer
    the version the person was editing."""
    path = safe_path(path)
    if len(text.encode()) > MAX_PAGE_BYTES:
        raise BrainError("That page is too long to save here.")
    if not text.endswith("\n"):
        text += "\n"
    vol = volume(agent)
    if runtime.runner.run(["docker", "volume", "inspect", vol]).returncode != 0:
        raise BrainError("This agent has no brain yet.")
    p = runtime.runner.run(["docker", "run", "--rm", "-i", "--network", "none", "-v", f"{vol}:/v",
                            "-e", f"P={path}", "-e", f"EXPECT={int(expected_modified)}",
                            "--entrypoint", "sh", HELPER, "-c", _WRITE], input=text)
    if p.returncode == 3:
        raise PageChanged("This page was changed while you were editing it. Reload it and try again.")
    if p.returncode != 0:
        raise BrainError("The page could not be saved.")
    out = p.stdout if isinstance(p.stdout, str) else p.stdout.decode(errors="replace")
    stamp = out.strip().splitlines()[-1] if out.strip() else ""
    return {"path": path, "modified": int(stamp) if stamp.isdigit() else 0}


NEW_PAGE_FOLDERS = ("projects", "people", "topics")
_CREATE = r'''
f="/v/$P"
[ -e "$f" ] && exit 5
d="$(dirname "$f")"
mkdir -p "$d" && chown 10000:10000 "$d"
cat > "$f" && chown 10000:10000 "$f" && chmod 0644 "$f" && stat -c %Y "$f"
'''


def new_page_path(folder: str, title: str) -> str:
    """Where a new page with this title goes: ``<folder>/<Title>.md``."""
    title = " ".join((title or "").split())
    name = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]', "", title).strip(" .")[:80]
    if folder not in NEW_PAGE_FOLDERS or not name:
        raise BrainError("A new page needs a title, and goes under projects, people or topics.")
    return safe_path(f"{folder}/{name}.md")


def create_page(runtime: Any, agent: str, folder: str, title: str) -> Dict[str, Any]:
    """Create a page holding just its title. Refuses if a page of that name exists."""
    path = new_page_path(folder, title)
    vol = volume(agent)
    if runtime.runner.run(["docker", "volume", "inspect", vol]).returncode != 0:
        raise BrainError("This agent has no brain yet.")
    p = runtime.runner.run(["docker", "run", "--rm", "-i", "--network", "none", "-v", f"{vol}:/v",
                            "-e", f"P={path}", "--entrypoint", "sh", HELPER, "-c", _CREATE],
                           input=f"# {' '.join(title.split())}\n\n")
    if p.returncode == 5:
        raise BrainError("A page with that name already exists.")
    if p.returncode != 0:
        raise BrainError("The page could not be created.")
    out = p.stdout if isinstance(p.stdout, str) else p.stdout.decode(errors="replace")
    stamp = out.strip().splitlines()[-1] if out.strip() else ""
    return {"path": path, "modified": int(stamp) if stamp.isdigit() else 0}


def search(runtime: Any, agent: str, query: str) -> List[Dict[str, str]]:
    query = " ".join((query or "").split())[:80]
    if len(query) < 2:
        return []
    out = _run(runtime, agent, ["--entrypoint", "sh", HELPER, "-c", _SEARCH], env={"Q": query})
    hits = []
    for line in out.splitlines():
        path, _, snippet = line.partition("\t")
        if path.endswith(".md"):
            hits.append({"path": path, "title": title_of(path), "snippet": snippet.strip()})
    return hits
