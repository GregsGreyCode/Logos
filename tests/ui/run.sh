#!/usr/bin/env bash
# Screen checks (Greg, 23 Nov): the dashboard in a real browser at phone, foldable and desktop sizes,
# against a fake server (serve.py: the API answers as prod did on 22 Nov; fake-mx.js: a Matrix client
# with a DM, a group and an invitation). Each checks/*.mjs prints PASS/FAIL lines; any FAIL or error
# fails the run. Screenshots go to $OUT (default tests/ui/out) to look at.
#   tests/ui/run.sh                 all checks
#   tests/ui/run.sh foldables cache only those
# Needs Node with playwright-core (npm install --prefix tests/ui playwright-core@1.58.0) and a Chrome:
# $CHROME, else Playwright's own Chromium, else google-chrome.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
OUT="${OUT:-$HERE/out}"; PORT="${PORT:-8779}"
mkdir -p "$OUT/desk" "$OUT/wide"
if [ -z "${CHROME:-}" ]; then
  CHROME="$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux64/chrome 2>/dev/null | tail -1)"
  [ -n "$CHROME" ] || CHROME="$(command -v google-chrome || command -v chromium || true)"
fi
[ -n "$CHROME" ] || { echo "No Chrome found: set CHROME"; exit 2; }
export CHROME OUT
# The checks talk to 127.0.0.1:8779; another server already there would answer instead of this one.
if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then echo "Port $PORT is already in use: stop what is listening there first."; exit 2; fi
python3 "$HERE/serve.py" "$REPO" "$PORT" > "$OUT/serve.log" 2>&1 &
SERVER=$!; trap 'kill $SERVER 2>/dev/null' EXIT
for _ in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/" >/dev/null && break; sleep 0.1; done
checks=("$@"); [ ${#checks[@]} -gt 0 ] || checks=($(cd "$HERE/checks" && ls *.mjs | sed 's/\.mjs$//'))
failed=0; total=0
for c in "${checks[@]}"; do
  out="$(node "$HERE/checks/$c.mjs" "$OUT" 2>&1)"
  p=$(grep -c '^PASS' <<<"$out"); f=$(grep -cE '^FAIL|Error' <<<"$out"); total=$((total + p))
  if [ "$f" -gt 0 ] || [ "$p" -eq 0 ]; then failed=$((failed + 1)); echo "✗ $c ($p passed)"; grep -E '^FAIL|Error' <<<"$out" | head -5 | sed 's/^/    /'
  else echo "✓ $c ($p)"; fi
done
echo "$total checks passed; $failed of ${#checks[@]} files failed"
[ "$failed" -eq 0 ]
