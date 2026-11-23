// F36 harness: a DM with Greg's agent and the Household Group (Greg, Béré, the household agent).
const ME = '@greg:test', BERE = '@bere:test', HH = '@household-agent:test', GA = '@greg-agent:test';
let n = 0; const handlers = new Map();
class Ev {
  constructor(room, type, content, sender, ago = 0) { this.room = room; this.t = type; this.c = content; this.s = sender; this.id = `$e${++n}`; this.ts = Date.now() - ago * 60000 + n; this.red = false; }
  getId() { return this.id; } getType() { return this.t; } getContent() { return this.c; } getWireContent() { return this.c; }
  getSender() { return this.s; } getTs() { return this.ts; } isRedacted() { return false; } isEncrypted() { return false; }
  isDecryptionFailure() { return false; } getRoomId() { return this.room; } getSenderKey() { return 'own'; }
}
const NAMES = { [ME]: 'Greg', [BERE]: 'Béré', [HH]: 'Household agent', [GA]: 'Greg agent' };
function mkRoom(id, name, members, events, mine = 'join') {
  const mems = members.map((u) => ({ userId: u, name: NAMES[u], membership: 'join', roomId: id }));
  return { roomId: id, name, hasEncryptionStateEvent: () => true, getMyMembership: () => mine, getMember: (u) => mems.find((m) => m.userId === u) || null, getMembers: () => mems,
    getJoinedMemberCount: () => mems.length, getLastActiveTimestamp: () => Date.now(), getLiveTimeline: () => ({ getEvents: () => events }),
    findEventById: (x) => events.find((e) => e.id === x), getEventReadUpTo: () => (id === D ? events[events.length - 1].id : null), getThreads: () => [], getTypingMembers: () => [], events };
}
const G = '!group:test', D = '!dm:test';
const gev = [
  new Ev(G, 'm.room.message', { msgtype: 'm.text', body: 'Hermes — changes-daily' }, HH, 30),
  new Ev(G, 'm.room.message', { msgtype: 'm.text', body: '**What changed this week**\n' + Array.from({ length: 16 }, (_, i) => `- **Item ${i + 1}:** something that changed in the agents or the home servers this week.`).join('\n') }, HH, 29),
  new Ev(G, 'm.room.message', { msgtype: 'm.text', body: 'Shall we do pasta tonight?' }, BERE, 10),
  new Ev(G, 'm.room.message', { msgtype: 'm.text', body: 'And buy bread on the way' }, BERE, 9),
  new Ev(G, 'm.room.message', { msgtype: 'm.text', body: 'Yes!' }, ME, 8),
  new Ev(G, 'm.room.message', { msgtype: 'm.text', body: 'Reminder: the green bin goes out tonight, and the recycling too.' }, HH, 3),
];
const dev = [new Ev(D, 'm.room.message', { msgtype: 'm.text', body: 'hi' }, ME, 5), new Ev(D, 'm.room.message', { msgtype: 'm.text', body: 'Your dentist moved to Thursday at 10:30; I updated the calendar.' }, GA, 4)];
dev.push(new Ev(D, 'm.reaction', { 'm.relates_to': { rel_type: 'm.annotation', event_id: dev[0].id, key: '✅' } }, GA, 3));
const rooms = [mkRoom(D, '', [ME, GA], dev), mkRoom(G, 'Household Group', [ME, BERE, HH], gev), mkRoom('!inv:test', 'Holiday planning', [BERE, HH], [], 'invite')];
const emit = (name, ...a) => (handlers.get(name) || []).forEach((f) => f(...a));
window.__mx = { sent: [] };
// Tests may add more group chats before the page draws: localStorage 'fake.groups' = N.
try { const k = Number(localStorage.getItem('fake.groups') || 0); for (let i = 0; i < k; i++) { const id = `!g${i}:test`; rooms.push(mkRoom(id, `Group ${i + 1}${i % 3 === 0 ? ' trip' : ''}`, [ME, BERE, HH], [new Ev(id, 'm.room.message', { msgtype: 'm.text', body: `Plan number ${i + 1}` }, BERE, 60 + i)])); } } catch (e) {}
window.__mx.addEvent = (roomId, content, sender) => { const r = client.getRoom(roomId); const ev = new Ev(roomId, 'm.room.message', content, sender || GA); r.events.push(ev); emit('Room.timeline', ev, r); return ev.id; };
const client = {
  getUserId: () => ME, getRooms: () => rooms, getRoom: (id) => rooms.find((r) => r.roomId === id) || null,
  getCrypto: () => ({ getOwnDeviceKeys: async () => ({ curve25519: 'own' }), getUserDeviceInfo: async () => new Map() }),
  on: (name, f) => handlers.set(name, [...(handlers.get(name) || []), f]), off: () => {},
  sendReadReceipt: async (ev) => { (window.__mx.receipts ||= []).push(ev.getId()); }, scrollback: async () => {}, sendTyping: async () => {},
  sendMessage: async (roomId, a, b) => { const r = client.getRoom(roomId); const content = b || a; window.__mx.sent.push([roomId, content]);
    const ev = new Ev(roomId, 'm.room.message', content, ME); r.events.push(ev); emit('Room.timeline', ev, r); return { event_id: ev.id }; },
  createRoom: async (o) => { window.__mx.created = o; const id = '!new:test'; rooms.push(mkRoom(id, o.name, [ME, ...o.invite], [])); return { room_id: id }; },
  joinRoom: async (id) => { (window.__mx.joined ||= []).push(id); return {}; },
  leave: async (id) => { (window.__mx.left ||= []).push(id); return {}; },
  mxcUrlToHttp: (mxc) => (window.__mx.blobs || {})[mxc] || null,
  getAccessToken: () => 'tok',
  uploadContent: async (blob, opts) => { const id = `mxc://test/up${Object.keys(window.__mx.blobs ||= {}).length}`; window.__mx.blobs[id] = URL.createObjectURL(blob); (window.__mx.uploads ||= []).push({ id, opts, size: blob.size }); return { content_uri: id }; },
  setPusher: async (p) => { (window.__mx.pushers ||= []).push(p); return {}; },
  sendEvent: async () => ({ event_id: 'x' }), redactEvent: async () => ({}),
};
export const saved = () => true;
export const get = async () => client;
export const bind = () => {}; export const isVerified = async () => true;
export const connect = async () => {}; export const unlock = async () => {}; const DAY = 86400000;
export const devices = async () => [
  { id: 'A', name: 'Logos (Chrome on Android)', here: true, verified: true, lastSeen: Date.now() },
  { id: 'B', name: 'Element X', verified: true, lastSeen: Date.now() - 1.5 * 3600000 },
  { id: 'C', name: 'Logos (Chrome on Windows)', verified: true, lastSeen: Date.now() - 1 * DAY },
  { id: 'D', name: 'Logos (Chrome on Android)', verified: true, lastSeen: Date.now() - 1.7 * DAY },
  { id: 'E', name: 'Element Classic Android', verified: true, lastSeen: Date.now() - 4 * DAY },
  { id: 'F', name: 'Logos agent', verified: false, lastSeen: Date.now() - 5 * DAY }];
export const signOut = async (id, pw) => { (window.__mx.signedOut ||= []).push([id, pw]); }; export const disconnect = async () => {}; export const forget = async () => {};
