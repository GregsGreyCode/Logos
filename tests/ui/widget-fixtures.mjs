// Stand-in data for every Home widget at once (F49 stage 2): the homelab kinds with the numbers the other checks use, two
// weather places and the shared lists. Used by checks that need every widget on Home (widget_minimums, widget_sizes).
// Not a check itself: it lives outside checks/.
const now = Date.now() / 1000;
const kind = (id, name, extra = {}) => ({ id, name, who: 'home', refresh: 60, set_up: true, can_set_up: true, fields: [], ...extra });
export const KINDS = { widgets: [kind('services', 'Services'), kind('servers', 'Servers'), kind('proxmox', 'Proxmox', { uses: 'servers' }),
  kind('backups', 'Backups'), kind('adblock', 'Ad blocking'), kind('playing', 'Now playing'), kind('downloads', 'Downloads'),
  kind('bookmarks', 'Bookmarks'), kind('energy', 'Energy'), kind('network', 'Network'), kind('cameras', 'Cameras'), kind('agents', 'Agents today')] };
export const DATA = {
  services: { state: 'needs', summary: '1 down', at: now, down: 1, items: [
    { name: 'Nextcloud', url: 'http://nc', up: false, detail: 'not answering', since: now - 720 },
    { name: 'Immich', url: 'http://im', up: true, ms: 2400, slow: true },
    { name: 'Home Assistant', url: 'http://ha', up: true, ms: 84 },
    { name: 'Jellyfin', url: 'http://jf', up: true, ms: 40 }] },
  servers: { state: 'ok', summary: '1 node', at: now, items: [
    { name: 'pve', online: true, cpu: 18, mem: 64, mem_gb: 41, mem_total_gb: 64, disk: 86, uptime: 23 * 86400 }] },
  proxmox: { state: 'ok', summary: '2 running · 1 stopped', at: now, items: [
    { id: 101, name: 'homeassistant', running: true, kind: 'vm', cpu: 3, mem_gb: 2 }, { id: 110, name: 'logos', running: true, kind: 'vm', cpu: 9, mem_gb: 6.1 },
    { id: 130, name: 'test', running: false, kind: 'container' }] },
  backups: { state: 'needs', summary: 'Photos to NAS: failed', at: now, items: [
    { name: 'Photos to NAS', ran: now - 3 * 86400, ok: false, detail: 'failed' }, { name: 'Logos export', ran: now - 7200, ok: true, detail: '' }] },
  adblock: { state: 'ok', summary: 'Pi-hole', at: now, percent: 23, blocked: 4812, total: 20940, top: ['ads.example', 'telemetry.example'] },
  playing: { state: 'ok', summary: '2 playing', at: now, items: [
    { title: '[Film title]', where: 'Living room TV', who: 'Béré', paused: false, position: 3720, length: 6420, percent: 58 },
    { title: '[Series] · S2 E4', where: "Greg's laptop", who: 'Greg', paused: true, position: 600, length: 2800, percent: 21 }] },
  downloads: { state: 'needs', summary: '1 stalled', at: now, upcoming: 4, items: [
    { title: '[Episode] S01E05', app: 'sonarr', percent: 72, left: '00:04:00', waiting: false, stalled: false },
    { title: '[Film]', app: 'radarr', percent: 0, left: '', waiting: true, stalled: false },
    { title: '[Episode] S01E06', app: 'sonarr', percent: 0, left: '', waiting: false, stalled: true }] },
  bookmarks: { state: 'ok', summary: '5 links', at: now, items: [
    { name: 'Nextcloud', url: 'http://nc' }, { name: 'Home Assistant', url: 'http://ha' }, { name: 'Immich', url: 'http://im' },
    { name: 'Jellyfin', url: 'http://jf' }, { name: 'Router', url: 'http://r' }] },
  energy: { state: 'ok', summary: 'Home Assistant', at: now, power_w: 412, energy_kwh: 6.8, cost: 1.71,
    ups: { battery: 100, status: 'Online', on_battery: false, minutes: 52 } },
  network: { state: 'ok', summary: 'Speedtest Tracker', at: now, down: 512, up: 96, ping: 9, tested: '2026-11-23 07:00:00' },
  cameras: { state: 'ok', summary: '2 cameras', at: now, items: [{ name: 'front_door', label: 'person', at: now - 3600 }, { name: 'hallway', label: '', at: null }] },
  agents: { state: 'ok', summary: 'AI computer on', at: now, model_up: true, items: [
    { agent: 'greg', name: "Greg's agent", ready: true, messages: 6, last: now - 1800, total: 120 },
    { agent: 'household', name: 'Household agent', ready: true, messages: 0, last: now - 86400 * 2, total: 40 },
    { agent: 'bere', name: "Béré's agent", ready: false, messages: 0, last: 0, total: 3 }] },
};
const day = (code, high, low, rain) => ({ code, high, low, rain });
export const PLACES = [
  { name: 'Brighton, England, United Kingdom', lat: 50.83, lon: -0.14 },
  { name: 'Paris, Île-de-France, France', lat: 48.85, lon: 2.35 },
  { name: 'Abidjan, Abidjan, Ivory Coast', lat: 5.36, lon: -4.01 },
];
const SKY = { 'Brighton, England, United Kingdom': [9.4, 61, day(61, 11, 4, 80), day(2, 12, 6, 20)],
  'Paris, Île-de-France, France': [6.1, 3, day(3, 8, 2, 0), day(0, 9, 1, 0)], 'Abidjan, Abidjan, Ivory Coast': [29.2, 2, day(80, 31, 24, 60), day(95, 30, 24, 70)] };
// What GET /api/v2/weather answers for these places, in this order (the server's shape: the first at the top too).
export function weatherFor(places) {
  const out = places.map((p) => { const [t, c, today, tomorrow] = SKY[p.name] || [12, 1, day(1, 14, 8, 0), day(1, 14, 8, 0)];
    return { place: p.name, lat: p.lat, lon: p.lon, now: { temp: t, code: c, day: true }, today, tomorrow }; });
  return out.length ? { ...out[0], places: out } : { error: 'no_place', places: [] };
}
export const ALL = ['weather', 'next', 'calendar', 'messages', 'coming', 'lights', 'lists', ...KINDS.widgets.map((k) => k.id)];

// Routes every widget's data on a page. prefs: what /api/v2/me's prefs become (a function of the saved ones).
export async function routeAll(page, { widgets = ALL, prefs = {} } = {}) {
  const state = { prefs: { widgets, widgets_v2: true, weather_places: PLACES.slice(0, 2), ...prefs }, puts: [] };
  await page.route('**/api/v2/me', async (r) => { const res = await r.fetch(); const me = await res.json();
    me.prefs = { ...me.prefs, ...state.prefs }; await r.fulfill({ json: me }); });
  await page.route('**/api/v2/me/prefs', async (r) => {
    if (r.request().method() !== 'PUT') return r.continue();
    const b = r.request().postDataJSON(); state.puts.push(b); Object.assign(state.prefs, b); await r.fulfill({ json: state.prefs });
  });
  await page.route('**/api/v2/widgets', (r) => r.fulfill({ json: KINDS }));
  await page.route(/\/api\/v2\/widgets\/(\w+)$/, (r) => r.fulfill({ json: DATA[r.request().url().split('/').pop()] || { state: 'setup' } }));
  await page.route('**/api/v2/widgets/cameras/snapshot/**', (r) => r.fulfill({ status: 404, body: '' }));
  await page.route('**/api/v2/weather', (r) => r.fulfill({ json: weatherFor(state.prefs.weather_places || []) }));
  return state;
}
