// Notifications (Greg, 21 Nov): one feed for what happened while you were elsewhere.
//   * from the server (/api/v2/notifications): brain pages your agents changed, the AI computer going off
//     and back on, and when you last looked (seen_until, kept with your login);
//   * from the noticeboard: what the agents asked each other, and how it ended;
//   * from Matrix, in this browser: your agents' messages you have not read yet (briefings, replies,
//     reminders), so the count clears when you read them here or in Element X.
// "New" is anything after the last time you opened Notifications; an unread message stays new until read.
import { getJSON, api } from './util.js';
import * as mx from './mx.js';
import { outcome } from './noticeboard.js';

const DAYS = 7;
const CRON_ROOT = /^Hermes\s+[—–-]\s+\S+\s*$/;

// Your agents' messages you have not read: after your read receipt and after your own last message.
export async function unreadFromAgents(agents) {
  const c = mx.saved() ? await mx.get().catch(() => null) : null;
  if (!c) return [];
  const me = c.getUserId();
  const since = Date.now() - DAYS * 86400000;
  const out = [];
  for (const a of agents.filter((x) => x.matrix_id)) {
    const rooms = c.getRooms().filter((r) => r.getMyMembership() === 'join' && r.getMember(a.matrix_id)?.membership === 'join');
    for (const room of rooms) {
      const evs = room.getLiveTimeline().getEvents();
      const readId = room.getEventReadUpTo(me);
      let from = 0;
      evs.forEach((e, i) => { if (e.getId() === readId || e.getSender() === me) from = i + 1; });
      for (const e of evs.slice(from)) {
        const body = String(e.getContent()?.body || '');
        if (e.getSender() !== a.matrix_id || e.getType() !== 'm.room.message' || e.getTs() < since) continue;
        if (e.getContent().msgtype === 'm.notice' || !body || CRON_ROOT.test(body)) continue;
        const root = e.threadRootId && e.threadRootId !== e.getId() ? e.threadRootId : null;
        out.push({ id: `mx:${e.getId()}`, kind: 'chat', ts: e.getTs() / 1000, unread: true, agent: a.name, who: a.display_name,
          title: a.display_name, text: body.replace(/\s+/g, ' ').slice(0, 160),
          link: root ? `#/chat/${a.name}/thread/${encodeURIComponent(root)}` : `#/chat/${a.name}` });
      }
    }
  }
  return out;
}

// Everything for the feed, newest first, each marked new or not.
export async function feed() {
  const [server, nb, agents] = await Promise.all([
    getJSON('/api/v2/notifications').catch(() => ({ items: [], seen_until: 0 })),
    getJSON('/api/v2/noticeboard').catch(() => ({ waiting: [], log: [] })),
    getJSON('/api/v2/agents').then((d) => d.agents || []).catch(() => []),
  ]);
  const seen = Number(server.seen_until || 0);
  const since = Date.now() / 1000 - DAYS * 86400;
  const between = (nb.log || []).filter((m) => (m.ts || 0) >= since && m.status !== 'open').map((m) => {
    const [label, tone] = outcome(m);
    return { id: `nb:${m.id}`, kind: 'agents', ts: m.ts, title: `${m.from_name} → ${m.to_name}`, text: m.text,
      outcome: label.split(':')[0], tone, link: '#/noticeboard' };
  });
  const chats = await unreadFromAgents(agents).catch(() => []);
  const items = [...chats, ...(server.items || []), ...between].sort((a, b) => b.ts - a.ts)
    .map((i) => ({ ...i, fresh: i.unread || i.ts > seen }));
  return { items, waiting: nb.waiting || [], seen };
}

// The number on the bell: what waits on you, plus what is new.
export async function countNew() {
  const { items, waiting } = await feed();
  return waiting.length + items.filter((i) => i.fresh).length;
}

export const markSeen = () => api('/api/v2/notifications/seen', { method: 'POST' }).catch(() => {});
