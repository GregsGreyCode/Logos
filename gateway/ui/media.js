// Voice notes, images and files in a chat (Greg, 22 Nov: so Element X is not needed to hear or see
// them). In an encrypted room a file is encrypted on its own (AES-CTR, the key in the message, which is
// itself end-to-end encrypted): this browser downloads the ciphertext through Logos and decrypts it
// here, as Element does. Nothing readable passes through the server. Sending works the same way back.
import { h, icon, ICONS } from './util.js';

const b64 = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const unb64 = (s) => Uint8Array.from(atob(String(s).replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (String(s).length % 4)) % 4)), (c) => c.charCodeAt(0));
const unpad = (s) => s.replace(/=+$/, '');

// The Matrix attachment format (v2): AES-256-CTR, the key as a JWK, a 16-byte counter block whose last
// 8 bytes start at zero, and the SHA-256 of the ciphertext.
export async function decryptAttachment(data, file) {
  const want = file && file.hashes && file.hashes.sha256;
  if (!want) throw new Error('no hash');
  const got = unpad(b64(await crypto.subtle.digest('SHA-256', data)));
  if (got !== unpad(want)) throw new Error('the file does not match its hash');
  const key = await crypto.subtle.importKey('jwk', { ...file.key, key_ops: ['encrypt', 'decrypt'] }, { name: 'AES-CTR' }, false, ['decrypt']);
  return crypto.subtle.decrypt({ name: 'AES-CTR', counter: unb64(file.iv), length: 64 }, key, data);
}

export async function encryptAttachment(data) {
  const key = await crypto.subtle.generateKey({ name: 'AES-CTR', length: 256 }, true, ['encrypt', 'decrypt']);
  const iv = new Uint8Array(16);
  crypto.getRandomValues(iv.subarray(0, 8));
  const cipher = await crypto.subtle.encrypt({ name: 'AES-CTR', counter: iv, length: 64 }, key, data);
  const jwk = await crypto.subtle.exportKey('jwk', key);
  return {
    data: cipher,
    file: { v: 'v2', key: { kty: 'oct', key_ops: ['encrypt', 'decrypt'], alg: 'A256CTR', k: jwk.k, ext: true },
      iv: unpad(b64(iv)), hashes: { sha256: unpad(b64(await crypto.subtle.digest('SHA-256', cipher))) } },
  };
}

// Fetching: through the dashboard (the Matrix pass-through), with the Matrix login of this browser.
const cache = new Map();                       // mxc -> Promise<object URL>
async function download(c, mxc) {
  const url = c.mxcUrlToHttp(mxc, undefined, undefined, undefined, false, true, true);
  if (!url) throw new Error('no address');
  const r = await fetch(url, { headers: { Authorization: `Bearer ${c.getAccessToken()}` }, credentials: 'same-origin' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.arrayBuffer();
}
// An object URL for the file in a message's content (encrypted: content.file; plain: content.url).
export function mediaUrl(c, content, thumb = false) {
  const enc = thumb ? (content.info && content.info.thumbnail_file) || content.file : content.file;
  const plain = thumb ? (content.info && content.info.thumbnail_url) || content.url : content.url;
  const mxc = enc ? enc.url : plain;
  if (!mxc) return Promise.reject(new Error('no file'));
  if (!cache.has(mxc)) {
    cache.set(mxc, (async () => {
      let data = await download(c, mxc);
      if (enc) data = await decryptAttachment(data, enc);
      const type = (thumb && content.info && content.info.thumbnail_info && content.info.thumbnail_info.mimetype)
        || (content.info && content.info.mimetype) || 'application/octet-stream';
      return URL.createObjectURL(new Blob([data], { type }));
    })().catch((e) => { cache.delete(mxc); throw e; }));
  }
  return cache.get(mxc);
}

const size = (n) => (!n ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const clock = (s) => (Number.isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}` : '–:––');

// A voice note: ▶, its length, and a bar to drag. The sound is fetched on the first ▶.
export function voiceNote(c, content) {
  const dur = content.info && content.info.duration ? content.info.duration / 1000 : NaN;
  const audio = new Audio();
  const btn = h('button', { type: 'button', class: 'mx-play', 'aria-label': 'Play the voice note' }, '▶');
  const bar = h('input', { type: 'range', class: 'mx-seek', min: '0', max: '1000', value: '0', 'aria-label': 'Position' });
  const time = h('span', { class: 'when mx-dur' }, clock(dur));
  const note = h('span', { class: 'when mx-media-err' });
  let ready = null;
  const load = () => (ready = ready || mediaUrl(c, content).then((u) => { audio.src = u; }));
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!audio.paused) { audio.pause(); return; }
    btn.disabled = true;
    try { await load(); await audio.play(); } catch (err) { note.textContent = 'This voice note could not be played.'; }
    btn.disabled = false;
  });
  audio.addEventListener('play', () => { btn.textContent = '❚❚'; btn.setAttribute('aria-label', 'Pause'); });
  audio.addEventListener('pause', () => { btn.textContent = '▶'; btn.setAttribute('aria-label', 'Play the voice note'); });
  audio.addEventListener('timeupdate', () => {
    const d = Number.isFinite(audio.duration) ? audio.duration : dur;
    if (d) bar.value = String(Math.round((audio.currentTime / d) * 1000));
    time.textContent = audio.paused && !audio.currentTime ? clock(d) : clock(audio.currentTime);
  });
  audio.addEventListener('ended', () => { bar.value = '0'; time.textContent = clock(audio.duration || dur); });
  bar.addEventListener('input', async () => {
    await load().catch(() => {});
    const d = Number.isFinite(audio.duration) ? audio.duration : dur;
    if (d) audio.currentTime = (Number(bar.value) / 1000) * d;
  });
  for (const ev of ['pointerdown', 'click']) bar.addEventListener(ev, (e) => e.stopPropagation());
  return h('div', { class: 'mx-voice' }, btn, bar, time, note);
}

// An image: a thumbnail in the bubble; a tap opens it full screen.
export function imageThumb(c, content) {
  const img = h('img', { class: 'mx-img', alt: content.body || 'An image' });
  const w = content.info && content.info.w, hgt = content.info && content.info.h;
  if (w && hgt) img.style.aspectRatio = `${w} / ${hgt}`;
  const box = h('button', { type: 'button', class: 'mx-img-box', 'aria-label': `Open the image${content.body ? `: ${content.body}` : ''}` }, img);
  mediaUrl(c, content, true).then((u) => { img.src = u; }).catch(() => mediaUrl(c, content).then((u) => { img.src = u; }))
    .catch(() => box.replaceChildren(h('em', { class: 'when' }, 'This image could not be opened.')));
  box.addEventListener('click', (e) => { e.stopPropagation(); viewer(c, content); });
  return box;
}

// Full screen: the image fitted to the screen; pinch or double-tap to zoom, Save, and ✕ (or a tap on the
// dark edge, Escape, or a swipe down) to close.
function viewer(c, content) {
  const img = h('img', { class: 'mx-view-img', alt: content.body || 'An image' });
  const save = h('a', { class: 'btn quiet small', download: content.body || 'image' }, 'Save');
  const layer = h('div', { class: 'mx-view', role: 'dialog', 'aria-modal': 'true', 'aria-label': content.body || 'Image' },
    h('div', { class: 'mx-view-bar' }, h('span', { class: 'mx-view-name' }, content.body || ''), save,
      h('button', { type: 'button', class: 'btn quiet small', 'aria-label': 'Close', onclick: () => close() }, '✕')),
    h('div', { class: 'mx-view-stage' }, img));
  const key = (e) => { if (e.key === 'Escape') close(); };
  function close() { layer.remove(); document.removeEventListener('keydown', key); }
  document.addEventListener('keydown', key);
  layer.querySelector('.mx-view-stage').addEventListener('click', (e) => { if (e.target === e.currentTarget) close(); });
  let y0 = null;
  img.addEventListener('touchstart', (e) => { y0 = e.touches.length === 1 && img.classList.contains('zoomed') === false ? e.touches[0].clientY : null; }, { passive: true });
  img.addEventListener('touchend', (e) => { if (y0 !== null && e.changedTouches[0].clientY - y0 > 120) close(); y0 = null; });
  img.addEventListener('dblclick', () => img.classList.toggle('zoomed'));
  mediaUrl(c, content).then((u) => { img.src = u; save.href = u; }).catch(() => { img.replaceWith(h('p', { class: 'when' }, 'This image could not be opened.')); });
  document.body.append(layer);
}

// A file or a video: its name and size; a tap opens it (a video plays in the bubble).
export function fileCard(c, content, kind) {
  const name = content.body || 'A file';
  const card = h('button', { type: 'button', class: 'mx-file' },
    h('span', { class: 'mx-file-ico', 'aria-hidden': 'true' }, kind === 'm.video' ? '▶' : icon(ICONS.file)),
    h('span', { class: 'mx-file-text' }, h('strong', {}, name), h('span', { class: 'when' }, size(content.info && content.info.size))));
  card.addEventListener('click', async (e) => {
    e.stopPropagation();
    card.disabled = true;
    try {
      const u = await mediaUrl(c, content);
      if (kind === 'm.video') card.replaceWith(h('video', { class: 'mx-video', src: u, controls: '', autoplay: '', playsinline: '' }));
      else { const a = h('a', { href: u, download: name, target: '_blank', rel: 'noopener' }); document.body.append(a); a.click(); a.remove(); }
    } catch (err) { card.querySelector('.when').textContent = 'This file could not be opened.'; }
    card.disabled = false;
  });
  return card;
}

// Sending a photo or a file (📎): encrypted first when the room is, then uploaded and sent as an
// m.image / m.video / m.audio / m.file message. ``extra`` adds fields (a group's mentions).
export async function sendFile(c, roomId, file, { threadId = null, extra = {} } = {}) {
  const data = await file.arrayBuffer();
  const room = c.getRoom(roomId);
  const encrypted = !!(room && (room.hasEncryptionStateEvent ? room.hasEncryptionStateEvent() : c.isRoomEncrypted?.(roomId)));
  const type = file.type || 'application/octet-stream';
  const msgtype = type.startsWith('image/') ? 'm.image' : type.startsWith('video/') ? 'm.video' : type.startsWith('audio/') ? 'm.audio' : 'm.file';
  const info = { mimetype: type, size: file.size };
  if (msgtype === 'm.image') {
    try { const bmp = await createImageBitmap(file); info.w = bmp.width; info.h = bmp.height; bmp.close?.(); } catch (e) { /* size unknown */ }
  }
  let content;
  if (encrypted) {
    const enc = await encryptAttachment(data);
    const up = await c.uploadContent(new Blob([enc.data], { type: 'application/octet-stream' }), { type: 'application/octet-stream', includeFilename: false });
    content = { msgtype, body: file.name, info, file: { ...enc.file, url: up.content_uri, mimetype: type } };
  } else {
    const up = await c.uploadContent(file, { type, name: file.name });
    content = { msgtype, body: file.name, info, url: up.content_uri };
  }
  Object.assign(content, extra);
  return threadId ? c.sendMessage(roomId, threadId, content) : c.sendMessage(roomId, content);
}

export { size as fileSize, icon, ICONS };
