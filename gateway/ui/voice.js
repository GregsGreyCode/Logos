// Speaking a message (Greg, 23 Nov): tap the microphone to start, tap again to finish. Not "hold": letting go by
// mistake lost the recording. One recorder for the chat's box and Home's Ask.
//
// - Tap the microphone: it records, and the box is replaced by a strip with a live waveform of your voice (so you
//   can see it is hearing you), the time, ✕ to throw it away and ■ to finish. Up to 30 minutes.
// - Finishing sends it at once as a voice message: the words are worked out (below), then handed over with the
//   recording's length and its shape (a few dozen bars), so the chat shows a voice bubble with the transcript under
//   it, to glance over and correct names in a later message.
// - The words are worked out as a job on the server (gateway/dashboard_api.py), so a long recording does not
//   hold one connection open, and if the page is reloaded meanwhile the words are fetched again on return.
// - If turning it into words fails, "Try again" sends the same recording again: nothing is lost.
import { h, api, getJSON, icon, ICONS, ask } from './util.js';

export const MAX_RECORD_S = 30 * 60;
export const WAVE_BARS = 48;                    // the shape kept with a sent voice message
const PENDING = 'logos.voice.pending';          // jobs not yet delivered: [{agent, job, at, seconds, wave}]
const KEEP_MS = 25 * 60000;
export const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

const readPending = () => { try { const v = JSON.parse(localStorage.getItem(PENDING) || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; } };
const writePending = (list) => { try { localStorage.setItem(PENDING, JSON.stringify(list.filter((p) => Date.now() - p.at < KEEP_MS))); } catch (e) { /* private window */ } };
const addPending = (p) => writePending([...readPending(), p]);
const dropPending = (job) => writePending(readPending().filter((p) => p.job !== job));

// The loudness samples of a whole recording (0..1) as WAVE_BARS whole numbers 0..100: the loudest in each part.
export function shapeOf(levels, bars = WAVE_BARS) {
  if (!levels.length) return Array(bars).fill(8);
  const out = [];
  for (let i = 0; i < bars; i++) {
    const from = Math.floor((i * levels.length) / bars), to = Math.max(from + 1, Math.floor(((i + 1) * levels.length) / bars));
    out.push(Math.max(8, Math.round(Math.max(...levels.slice(from, to)) * 100)));
  }
  return out;
}

// A voice message's bar: the microphone, its shape, how long. Static (the audio itself is not kept).
export function voiceBar(v, extra = '') {
  const wave = Array.isArray(v.wave) && v.wave.length ? v.wave : Array(WAVE_BARS).fill(30);
  return h('div', { class: `mx-voice-bar${extra}`, role: 'img', 'aria-label': `Voice message, ${clock(v.seconds || 0)}` },
    h('span', { class: 'mx-voice-mic' }, icon(ICONS.mic)),
    h('span', { class: 'mx-voice-wave', 'aria-hidden': 'true' }, ...wave.map((n) => h('i', { style: `--h:${Math.max(8, Math.min(100, Number(n) || 8))}%` }))),
    h('span', { class: 'mx-voice-time' }, clock(v.seconds || 0)));
}

// agent: the agent's name (or something that gives it). say(text | node): the line under the box.
// onWords(text, {seconds, wave}): the words are ready. onBusy(true|false): working on it.
// onTranscribing({seconds, wave}): the recording has gone to be turned into words (a placeholder can show);
// onSettled(): that is over (words delivered, or it failed). onRecording(true|false): the microphone is on or off.
// onAudio(blob, seconds): instead of turning it into words, hand the recording over (Settings › Your voice), with the
// browser's own clean-up switched off (raw: true) so the voice is heard as it is.
export function createRecorder({ agent: agentOf, say, onWords, onBusy = () => {}, onTranscribing = () => {}, onSettled = () => {}, onRecording = () => {}, maxSeconds = MAX_RECORD_S, onAudio = null, raw = false }) {
  const agent = () => (typeof agentOf === 'function' ? agentOf() : agentOf);       // a name, or something that gives it
  let rec = null, destroyed = false, busy = false, last = null;      // last: the recording kept for "Try again"
  const canvas = h('canvas', { class: 'rec-wave', 'aria-hidden': 'true' });
  const time = h('span', { class: 'rec-time', role: 'timer' }, '0:00');
  const bar = h('div', { class: 'rec-bar', role: 'group', 'aria-label': 'Recording', hidden: true },
    h('button', { type: 'button', class: 'rec-discard', 'aria-label': 'Throw this recording away', title: 'Throw it away', onclick: () => discard() }, icon(ICONS.trash)),
    h('span', { class: 'rec-dot', 'aria-hidden': 'true' }), time, canvas,
    h('button', { type: 'button', class: 'rec-done', 'aria-label': 'Finish and send the recording', title: 'Finish and send', onclick: () => finish() }, icon(ICONS.stop)));
  const mic = h('button', { type: 'button', class: 'mic-btn', 'aria-label': 'Record a voice message', title: 'Record a voice message', onclick: () => start() },
    icon(ICONS.mic));
  const setBusy = (v) => { busy = v; mic.disabled = v; onBusy(v); };
  const form = () => mic.closest('form');

  // The live waveform: one bar per sample, the newest at the right, scrolling left.
  function drawWave(levels) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(40, Math.round(canvas.clientWidth)), hgt = Math.max(20, Math.round(canvas.clientHeight));
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr); }
    const g = canvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, hgt);
    const step = 5, barW = 3, n = Math.floor(w / step);
    g.fillStyle = getComputedStyle(canvas).color || '#e5484d';
    const mid = hgt / 2, shown = levels.slice(-n);
    shown.forEach((lv, i) => {
      const bh = Math.max(3, lv * (hgt - 4));
      const x = w - (shown.length - i) * step;
      g.beginPath(); if (g.roundRect) g.roundRect(x, mid - bh / 2, barW, bh, 1.5); else g.rect(x, mid - bh / 2, barW, bh); g.fill();
    });
  }

  async function start() {
    if (rec || busy || destroyed) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      say(window.isSecureContext ? 'This browser cannot record audio.' : 'The microphone only works on the https address.'); return;
    }
    let stream;
    const audio = raw ? { echoCancellation: false, noiseSuppression: false, autoGainControl: false } : { echoCancellation: true, noiseSuppression: true };
    try { stream = await navigator.mediaDevices.getUserMedia({ audio }); } catch (e) {
      say('The microphone is blocked. Allow it for this site in the browser, then try again.'); return;
    }
    if (destroyed) { stream.getTracks().forEach((t) => t.stop()); return; }
    let recorder;
    try { recorder = new MediaRecorder(stream, { audioBitsPerSecond: 32000 }); } catch (e) { recorder = new MediaRecorder(stream); }   // speech needs little: 30 minutes is about 7 MB
    const chunks = [], levels = [], started = Date.now();
    let discarded = false, lock = null, meter = null, ctx = null;
    recorder.addEventListener('dataavailable', (e) => { if (e.data && e.data.size) chunks.push(e.data); });
    recorder.addEventListener('stop', () => {
      stream.getTracks().forEach((t) => t.stop());
      clearInterval(rec && rec.timer); clearInterval(meter);
      if (ctx) ctx.close().catch(() => {});
      if (lock) lock.release().catch(() => {});
      rec = null; bar.hidden = true; form()?.classList.remove('recording'); onRecording(false);
      if (discarded || destroyed) { if (!destroyed) say(''); return; }
      const seconds = Math.round((Date.now() - started) / 100) / 10;
      if (!chunks.length || seconds < 0.8) { say('That was too short: tap the microphone, say it, then tap ■.'); return; }
      const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
      if (onAudio) { onAudio(blob, seconds); return; }
      send(blob, seconds, { wave: shapeOf(levels) });
    });
    // A phone's screen going dark would stop a long recording: ask it to stay on (where the browser lets us).
    try { if (navigator.wakeLock) lock = await navigator.wakeLock.request('screen'); } catch (e) { /* fine */ }
    recorder.start(1000);
    bar.hidden = false; form()?.classList.add('recording'); onRecording(true);
    say('');
    // How loud you are, to see it is hearing you: a sample every 60 ms, drawn as the waveform.
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      const an = ctx.createAnalyser(); an.fftSize = 512; ctx.createMediaStreamSource(stream).connect(an);
      const buf = new Uint8Array(an.fftSize);
      meter = setInterval(() => {
        an.getByteTimeDomainData(buf);
        let sum = 0; for (const v of buf) sum += ((v - 128) / 128) ** 2;
        levels.push(Math.min(1, Math.sqrt(sum / buf.length) * 4));
        drawWave(levels);
      }, 60);
    } catch (e) { drawWave([0.2]); /* no meter: a flat line, still recording */ }
    const tick = () => {
      const s = Math.floor((Date.now() - started) / 1000);
      time.textContent = clock(s);
      if (s >= maxSeconds) { if (!onAudio) say(`That is the ${Math.round(maxSeconds / 60)}-minute limit: sending what was recorded.`); finish(); }
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

  // Send a recording to be turned into words, wait for them (polling), then hand them over (onWords). Resolves with the
  // text (or null when it failed: the line under the box says why, with "Try again").
  async function send(blob, seconds = 0, opts = {}) {
    last = { blob, seconds, opts };
    const meta = { seconds, wave: opts.wave || [] };
    setBusy(true); onTranscribing(meta);
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
      addPending({ agent: agent(), job, at: Date.now(), ...meta });
      const text = await waitFor(job);
      clearInterval(ticker);
      last = null; dropPending(job);
      await deliver(text, meta);
      return text;
    } catch (e) {
      clearInterval(ticker);
      if (job) dropPending(job);
      retry(e.message || 'That did not work.');
      return null;
    } finally { clearInterval(ticker); setBusy(false); onSettled(); }
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
  async function deliver(text, meta) {
    if (!text) { say('No words were heard in that recording.'); return; }
    say('');
    await onWords(text, meta);
  }
  function retry(message) {
    if (destroyed) return;
    if (!last) { say(message); return; }
    say(h('span', {}, `${message} `, h('button', { type: 'button', class: 'linkish', onclick: () => send(last.blob, last.seconds, last.opts) }, 'Try again')));
  }

  // Words from a recording made before the page was reloaded.
  async function resume() {
    for (const p of readPending().filter((x) => x.agent === agent() && Date.now() - x.at < KEEP_MS)) {
      setBusy(true); say('Picking up your recording…'); onTranscribing({ seconds: p.seconds || 0, wave: p.wave || [] });
      try { const text = await waitFor(p.job); dropPending(p.job); await deliver(text, { seconds: p.seconds || 0, wave: p.wave || [] }); }
      catch (e) { dropPending(p.job); if (!destroyed) say(e.message === 'left' ? '' : e.message); }
      setBusy(false); onSettled();
    }
  }
  return {
    mic, bar, send, resume, start,
    get recording() { return !!rec; }, get busy() { return busy; },
    discard: () => { if (rec) { rec.discard(); finish(); } },
    destroy() { destroyed = true; if (rec) { rec.discard(); finish(); } },
  };
}
