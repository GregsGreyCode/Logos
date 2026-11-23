// Speaking a message (Greg, 23 Nov): tap the microphone to start, tap again to finish. Not "hold": letting go by
// mistake lost the recording. One recorder for the chat's box and Home's Ask.
//
// - Tap the microphone: it records, and the box is replaced by a strip with a pulsing dot, the time, a small
//   level that moves with your voice (so you can see it is hearing you), ✕ to throw it away and ■ to finish.
// - Finishing turns it into words, which land in the box to check, then Send. It records up to 30 minutes.
// - The words are worked out as a job on the server (gateway/dashboard_api.py), so a long recording does not
//   hold one connection open, and if the page is reloaded meanwhile the words are fetched again on return.
// - If turning it into words fails, "Try again" sends the same recording again: nothing is lost.
import { h, api, getJSON, icon, ICONS, ask } from './util.js';

export const MAX_RECORD_S = 30 * 60;
const PENDING = 'logos.voice.pending';          // jobs not yet delivered: [{agent, job, at}]
const KEEP_MS = 25 * 60000;
const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

const readPending = () => { try { const v = JSON.parse(localStorage.getItem(PENDING) || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; } };
const writePending = (list) => { try { localStorage.setItem(PENDING, JSON.stringify(list.filter((p) => Date.now() - p.at < KEEP_MS))); } catch (e) { /* private window */ } };
const addPending = (p) => writePending([...readPending(), p]);
const dropPending = (job) => writePending(readPending().filter((p) => p.job !== job));

// agent: the agent's name. say(text | node): the line under the box. onWords(text): the words are ready.
// onBusy(true|false): working on it (the caller may dim its Send).
export function createRecorder({ agent: agentOf, say, onWords, onBusy = () => {}, maxSeconds = MAX_RECORD_S }) {
  const agent = () => (typeof agentOf === 'function' ? agentOf() : agentOf);       // a name, or something that gives it
  let rec = null, destroyed = false, busy = false, last = null;      // last: the recording kept for "Try again"
  const dot = h('span', { class: 'rec-dot', 'aria-hidden': 'true' });
  const time = h('span', { class: 'rec-time', role: 'timer' }, '0:00');
  const bar = h('div', { class: 'rec-bar', role: 'group', 'aria-label': 'Recording', hidden: true },
    h('button', { type: 'button', class: 'rec-discard', 'aria-label': 'Throw this recording away', title: 'Throw it away', onclick: () => discard() }, icon(ICONS.trash)),
    dot, time, h('span', { class: 'rec-hint' }, 'Recording · tap ■ to finish'),
    h('button', { type: 'button', class: 'rec-done', 'aria-label': 'Finish the recording', title: 'Finish', onclick: () => finish() }, icon(ICONS.stop)));
  const mic = h('button', { type: 'button', class: 'mic-btn', 'aria-label': 'Record a voice message', title: 'Record a voice message', onclick: () => start() },
    icon(ICONS.mic));
  const setBusy = (v) => { busy = v; mic.disabled = v; onBusy(v); };
  const form = () => mic.closest('form');

  async function start() {
    if (rec || busy || destroyed) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      say(window.isSecureContext ? 'This browser cannot record audio.' : 'The microphone only works on the https address.'); return;
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); } catch (e) {
      say('The microphone is blocked. Allow it for this site in the browser, then try again.'); return;
    }
    if (destroyed) { stream.getTracks().forEach((t) => t.stop()); return; }
    let recorder;
    try { recorder = new MediaRecorder(stream, { audioBitsPerSecond: 32000 }); } catch (e) { recorder = new MediaRecorder(stream); }   // speech needs little: 30 minutes is about 7 MB
    const chunks = [], started = Date.now();
    let discarded = false, lock = null, meter = null, ctx = null;
    recorder.addEventListener('dataavailable', (e) => { if (e.data && e.data.size) chunks.push(e.data); });
    recorder.addEventListener('stop', () => {
      stream.getTracks().forEach((t) => t.stop());
      clearInterval(rec && rec.timer); clearInterval(meter);
      if (ctx) ctx.close().catch(() => {});
      if (lock) lock.release().catch(() => {});
      rec = null; bar.hidden = true; form()?.classList.remove('recording');
      if (discarded || destroyed) { if (!destroyed) say(''); return; }
      const seconds = (Date.now() - started) / 1000;
      if (!chunks.length || seconds < 0.8) { say('That was too short: tap the microphone, say it, then tap ■.'); return; }
      send(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }), seconds);
    });
    // A phone's screen going dark would stop a long recording: ask it to stay on (where the browser lets us).
    try { if (navigator.wakeLock) lock = await navigator.wakeLock.request('screen'); } catch (e) { /* fine */ }
    // How loud you are, to see it is hearing you.
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      const an = ctx.createAnalyser(); an.fftSize = 512; ctx.createMediaStreamSource(stream).connect(an);
      const buf = new Uint8Array(an.fftSize);
      meter = setInterval(() => {
        an.getByteTimeDomainData(buf);
        let sum = 0; for (const v of buf) sum += ((v - 128) / 128) ** 2;
        bar.style.setProperty('--level', String(Math.min(1, Math.sqrt(sum / buf.length) * 5).toFixed(2)));
      }, 90);
    } catch (e) { /* no meter: the dot still pulses */ }
    recorder.start(1000);
    bar.hidden = false; form()?.classList.add('recording');
    say('');
    const tick = () => {
      const s = Math.floor((Date.now() - started) / 1000);
      time.textContent = clock(s);
      if (s >= maxSeconds) { say(`That is the ${Math.round(maxSeconds / 60)}-minute limit: turning what was recorded into words.`); finish(); }
    };
    tick(); rec = { recorder, started, timer: setInterval(tick, 500), discard: () => { discarded = true; } };
  }
  function finish() { if (rec && rec.recorder.state !== 'inactive') rec.recorder.stop(); }
  // Throwing a recording away asks first once it is worth keeping (15 seconds or more).
  async function discard() {
    if (!rec) return;
    if ((Date.now() - rec.started) / 1000 >= 15 && !await ask({ title: 'Throw this recording away?', text: `It is ${time.textContent} long, and cannot be brought back.`, yes: 'Throw it away', danger: true })) return;
    if (!rec) return;
    rec.discard(); finish();
  }

  // Send a recording to be turned into words, wait for them (polling), then hand them over. Resolves with the text
  // (or null when it failed: the line under the box says why, with "Try again").
  async function send(blob, seconds = 0, opts = {}) {
    last = { blob, seconds, opts };
    setBusy(true);
    const began = Date.now();
    const working = () => `Turning it into words… ${clock((Date.now() - began) / 1000)}${seconds > 90 ? ` (a long one: about ${Math.max(1, Math.round(seconds / 5 / 60))} min)` : ''}`;
    say(working());
    const ticker = setInterval(() => { if (!destroyed) say(working()); }, 1000);
    let job = null;
    try {
      const r = await api(`/api/v2/agents/${agent()}/voice?job=1`, { method: 'POST', raw: blob, headers: { 'Content-Type': blob.type || 'audio/webm' } });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.job) throw new Error(d.message || 'That did not go. Try again.');
      job = d.job;
      addPending({ agent: agent(), job, at: Date.now() });
      const text = await waitFor(job);
      clearInterval(ticker);
      last = null; dropPending(job);
      if (opts.auto) say(text ? '' : 'No words were heard in that recording.'); else deliver(text);   // auto: the caller sends it
      return text;
    } catch (e) {
      clearInterval(ticker);
      if (job) dropPending(job);
      retry(e.message || 'That did not work.');
      return null;
    } finally { clearInterval(ticker); setBusy(false); }
  }
  async function waitFor(job) {
    for (let i = 0; i < 1500; i++) {                              // up to about 40 minutes
      const d = await getJSON(`/api/v2/agents/${agent()}/voice/${job}`).catch((e) => (e.status === 404 ? { state: 'failed', message: 'That recording is no longer kept.' } : { state: 'working' }));
      if (d.state === 'done') return d.text || '';
      if (d.state === 'failed') throw new Error(d.message || 'That did not work.');
      await new Promise((ok) => setTimeout(ok, i < 5 ? 1000 : 2000));
      if (destroyed) throw new Error('left');
    }
    throw new Error('That is taking too long.');
  }
  function deliver(text) {
    if (!text) { say('No words were heard in that recording.'); return; }
    say('Check it, then send.');
    onWords(text);
  }
  function retry(message) {
    if (destroyed) return;
    if (!last) { say(message); return; }
    say(h('span', {}, `${message} `, h('button', { type: 'button', class: 'linkish', onclick: () => send(last.blob, last.seconds, last.opts) }, 'Try again')));
  }

  // Words from a recording made before the page was reloaded, or elsewhere.
  async function resume() {
    for (const p of readPending().filter((x) => x.agent === agent() && Date.now() - x.at < KEEP_MS)) {
      setBusy(true); say('Picking up your recording…');
      try { const text = await waitFor(p.job); dropPending(p.job); deliver(text); }
      catch (e) { dropPending(p.job); if (!destroyed) say(e.message === 'left' ? '' : e.message); }
      setBusy(false);
    }
  }
  return {
    mic, bar, send, resume,
    get recording() { return !!rec; }, get busy() { return busy; },
    discard: () => { if (rec) { rec.discard(); finish(); } },
    destroy() { destroyed = true; if (rec) { rec.discard(); finish(); } },
  };
}
