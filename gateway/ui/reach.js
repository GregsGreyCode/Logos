// When the phone or computer cannot reach the Logos server at all (Greg, 23 Nov: Tailscale was off on
// his phone, and the app just failed quietly). A request that gets no answer, not even an error, means
// the server is out of reach: after a second look a line says so, with Try again. It keeps trying by
// itself and goes away when the server answers. A dashboard restart (a few seconds, on a deploy) is
// usually over before the second look, so it does not flash up for that.

let bar = null, timer = null, checking = false, down = false;
const backHooks = [];

// Any answer at all, even a 401 or 404, means the server is there.
async function answers() {
  try {
    await fetch('/sw.js', { method: 'HEAD', cache: 'no-store', credentials: 'same-origin' });
    return true;
  } catch (e) { return false; }
}

function draw() {
  if (bar) return;
  bar = document.createElement('div');
  bar.className = 'reach-bar';
  bar.setAttribute('role', 'alert');
  const text = document.createElement('span');
  text.innerHTML = '<strong>Can’t reach Logos.</strong> Is Tailscale on?';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'reach-again';
  btn.textContent = 'Try again';
  btn.addEventListener('click', () => check(true));
  bar.append(text, btn);
  document.body.append(bar);
}

async function check(byHand = false) {
  if (checking) return;
  checking = true;
  const btn = bar && bar.querySelector('.reach-again');
  if (btn && byHand) { btn.disabled = true; btn.textContent = 'Trying…'; }
  const ok = await answers();
  checking = false;
  if (btn) { btn.disabled = false; btn.textContent = 'Try again'; }
  if (ok) back(); else { draw(); down = true; later(); }
}

function later() {
  clearTimeout(timer);
  timer = setTimeout(() => check(), 8000);
}

function back() {
  clearTimeout(timer); timer = null;
  const was = down;
  down = false;
  if (bar) { bar.remove(); bar = null; }
  if (was) for (const f of backHooks) { try { f(); } catch (e) { /* one screen's trouble stays its own */ } }
}

// A request got no answer: look again shortly, then say so if it is still out of reach.
export function lost() {
  if (down || timer) return;
  timer = setTimeout(() => { timer = null; check(); }, 2500);
}

// A request got an answer.
export function ok() { if (down) back(); }

// Called once the server is back after the line was shown (to redraw the screen).
export function onBack(f) { backHooks.push(f); }

export const isDown = () => down;

window.addEventListener('online', () => { if (down) check(); });
document.addEventListener('visibilitychange', () => { if (down && document.visibilityState === 'visible') check(); });
