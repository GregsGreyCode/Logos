// Notifications (Greg, 21 Nov): one feed for what happened while you were elsewhere.
//   * from the server (/api/v2/notifications): brain pages your agents changed, the AI computer going off
//     and back on, and when you last looked (seen_until, kept with your login);
//   * from the noticeboard: what the agents asked each other, and how it ended;
//   * from Matrix, in this browser: your agents' messages you have not read yet (briefings, replies,
//     reminders), so the count clears when you read them here or in Element X.
// "New" is anything after the last time you opened Notifications; an unread message stays new until read.
//
// Every item has a type (Greg, 21 Nov), so what needs you stands apart from what merely happened:
//   needs    something for you to do: a request, an Advisor question or budget ask, two copies of a page;
//   problem  something wrong right now: the AI computer off, an agent not running;
//   message  your agent wrote to you and you have not read it;
//   update   what happened: brain changes, what the agents asked each other, an outage that is over.
// Only the first three count on the bell; updates are there when you look.
export const TYPES = { needs: 'Needs you', problem: 'Problem', message: 'Message', update: 'Update' };
export const urgent = (i) => i.type === 'needs' || i.type === 'problem';
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
        // A group (F36): its name first, and the link opens the group.
        const inGroup = room.getJoinedMemberCount() > 2;
        out.push({ id: `mx:${e.getId()}`, kind: 'chat', type: 'message', ts: e.getTs() / 1000, unread: true, agent: a.name, who: a.display_name,
          title: inGroup ? `${room.name || 'Group'} · ${a.display_name}` : a.display_name, text: body.replace(/\s+/g, ' ').slice(0, 160),
          link: inGroup ? `#/chat/${a.name}/group/${encodeURIComponent(room.roomId)}`
            : root ? `#/chat/${a.name}/thread/${encodeURIComponent(root)}` : `#/chat/${a.name}` });
      }
    }
  }
  return out;
}

// Brain changes, one line per agent per day (Greg, 21 Nov: one row per page was a wall, every one at
// "12:00", since the brain is saved on the hour). A daily note's title is its date: say so in words.
const dayKey = (ts) => new Date(ts * 1000).toDateString();
const pageTitle = (t) => (/^\d{4}-\d{2}-\d{2}$/.test(t)
  ? `Notes of ${new Date(`${t}T12:00:00`).toLocaleDateString([], { day: 'numeric', month: 'short' })}` : t);
function brainByDay(items) {
  const groups = new Map(), rest = [];
  for (const i of items) {
    if (i.kind !== 'brain') { rest.push(i); continue; }
    const k = `${i.agent}|${dayKey(i.ts)}`;
    const g = groups.get(k) || { ...i, id: `brain:${k}`, pages: [], ts: 0 };
    g.pages.push(i); g.ts = Math.max(g.ts, i.ts);
    groups.set(k, g);
  }
  for (const g of groups.values()) {
    const n = g.pages.length;
    g.title = n === 1 ? pageTitle(g.pages[0].title) : `${n} pages updated`;
    g.text = n === 1 ? g.pages[0].text : g.pages.map((p) => pageTitle(p.title)).join(' · ');
    g.link = n === 1 ? g.pages[0].link : `#/brain/${g.agent}`;
    g.dayOnly = true;                      // the time is when it was saved, not when it was learnt
    rest.push(g);
  }
  return rest;
}

// Everything for the feed, newest first, each marked new or not.
export async function feed() {
  const [server, nb, agents, adv, people] = await Promise.all([
    getJSON('/api/v2/notifications').catch(() => ({ items: [], seen_until: 0 })),
    getJSON('/api/v2/noticeboard').catch(() => ({ waiting: [], log: [] })),
    getJSON('/api/v2/agents').then((d) => d.agents || []).catch(() => []),
    getJSON('/api/v2/advisor').catch(() => null),                 // not set up: nothing from it
    getJSON('/api/v2/people').catch(() => ({ people: [] })),
  ]);
  const nameOf = Object.fromEntries((people.people || []).map((p) => [p.username, p.display_name]));
  const agentName = Object.fromEntries(agents.map((a) => [a.name, a.display_name]));
  // The Advisor: questions waiting for your yes, and (Greg) people asking for more budget.
  const advisor = adv ? [
    ...(adv.pending || []).map((p) => ({ id: `adv:${p.id}`, kind: 'advisor', type: 'needs', ts: p.created, title: `${agentName[p.agent] || p.agent} wants to ask Claude`,
      text: p.question, link: '#/advisor' })),
    ...Object.entries(adv.everyone || {}).flatMap(([who, info]) => (info.requests || []).map((q) => ({ id: `budget:${q.id}`, kind: 'budget', type: 'needs',
      ts: q.created || Date.now() / 1000, title: `${nameOf[who] || who} asks for more Advisor budget`, text: q.reason, link: '#/advisor' }))),
  ] : [];
  const seen = Number(server.seen_until || 0);
  const since = Date.now() / 1000 - DAYS * 86400;
  const between = (nb.log || []).filter((m) => (m.ts || 0) >= since && m.status !== 'open').map((m) => {
    const [label, tone] = outcome(m);
    return { id: `nb:${m.id}`, kind: 'agents', type: 'update', ts: m.ts, title: `${m.from_name} → ${m.to_name}`, text: m.text,
      outcome: label.split(':')[0], tone, link: '#/noticeboard' };
  });
  const chats = await unreadFromAgents(agents).catch(() => []);
  const items = [...advisor, ...chats, ...brainByDay(server.items || []), ...between].sort((a, b) => b.ts - a.ts)
    .map((i) => ({ ...i, fresh: i.unread || i.ts > seen }));
  return { items, waiting: nb.waiting || [], seen };
}

// The number on the bell: what waits on you, plus what is new.
export async function countNew() {
  const { items, waiting } = await feed();
  return waiting.length + items.filter((i) => urgent(i) || (i.type === 'message' && i.fresh)).length;
}

export const markSeen = () => api('/api/v2/notifications/seen', { method: 'POST' }).catch(() => {});
