// Home's desktop grid (F49, Greg 23 Nov: "in desktop mode the widgets aren't resizeable or able to be placed anywhere").
//
// A 12-column grid with rows of 8 px. Each widget has a place {x, y, w, h}: x, w in columns; y in rows; h in rows, or 0
// for "as tall as its content". Dropping a widget on another pushes the other down; the rest packs upward so there are
// no holes. Only a wide screen (1100 px and up) uses it; a phone keeps the ordered list. The layout is each person's own,
// kept with their login (the "widget_grid" pref); the order of the widgets (the phone's) follows it, reading down then across.
export const COLS = 12, ROWU = 8, GAPX = 24, GAPV = 22;
// The smallest each widget may be made, Compact and Full, in pixels: [width, height] (F49 stage 2, Greg 24 Nov: "correct
// each widget's minimum size so it makes sense for what it shows"). Measured, not guessed: tests/ui/checks/widget_minimums.mjs
// draws every widget with real-looking content at every width and keeps the narrowest at which nothing is cut off, spills
// over or is squeezed onto more lines than it needs, and the height that shows what the widget is for there (all of it
// for a fixed-shape widget like Network; the heading and the first two rows for a list like Coming up). The check
// fails when a widget no longer fits its minimum, so a change to a widget's look comes with a new measurement.
// The edit bar (handle, name, Compact/Full, ✕) wraps on a narrow widget, so it does not count.
// MIN_TABLE_START (measured 27 Nov 2026: what makes each smaller is in widget_minimums.mjs's MEASURE=1 output)
export const MIN = {
  next: { compact: [270, 48], full: [160, 120] },           // compact: one line, dot, "15:00 Dinner with Alex", "40 min" (28 Nov); full: on two lines at its narrowest
  weather: { compact: [270, 40], full: [270, 104] },        // one place's row; narrower cuts the town's name (compact) or wraps tomorrow (full)
  calendar: { compact: [200, 64], full: [270, 104] },       // Today and its first event (full: and whose); narrower cuts the titles
  messages: { compact: [330, 72], full: [330, 96] },        // two messages; narrower cuts a group's name ("Household Group · Household agent")
  coming: { compact: [200, 112], full: [280, 112] },        // two events; narrower cuts their titles
  month: { compact: [190, 344], full: [250, 400] },         // the whole small month (full: and today's first event); narrower spills the day numbers or cuts a title
  lights: { compact: [160, 96], full: [160, 120] },         // one row of rooms: a pill a row at its narrowest
  lists: { compact: [170, 120], full: [260, 192] },         // one list's tile or card; narrower spills its card
  services: { compact: [280, 112], full: [280, 112] },      // two services; narrower cuts the names
  servers: { compact: [270, 72], full: [300, 144] },        // one server; full: its three meters
  proxmox: { compact: [220, 136], full: [380, 96] },        // two guests: rows (compact), or cards two by two (full)
  backups: { compact: [260, 112], full: [260, 112] },       // two backups
  adblock: { compact: [160, 128], full: [240, 160] },       // the ring and its words; full: and the top blocked
  playing: { compact: [270, 72], full: [210, 160] },        // one thing playing: a line (compact), or poster, bar and times (full)
  downloads: { compact: [270, 96], full: [270, 120] },      // two downloads
  bookmarks: { compact: [160, 64], full: [160, 72] },       // the first row of links
  energy: { compact: [190, 120], full: [190, 256] },        // using now and today; full: and the UPS
  network: { compact: [220, 96], full: [330, 128] },        // down and up; full: and ping, side by side
  cameras: { compact: [380, 152], full: [380, 152] },       // a snapshot with its label, two side by side
  agents: { compact: [180, 184], full: [200, 224] },        // two agents and how they are doing today
};
// MIN_TABLE_END
const MIN_ANY = { compact: [200, 96], full: [240, 96] };         // a widget not measured yet
export const MIN_H = 6;                // rows: never shorter than this, whatever the table says
const sizeKey = (size) => (size === 'compact' ? 'compact' : 'full');
export const minPx = (id, size) => (MIN[id] || MIN_ANY)[sizeKey(size)];

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const hits = (p, it, x, y) => x < p.x + p.w && x + it.w > p.x && y < p.y + p.h && y + it.h > p.y;

// The lowest row at column x where `it` does not touch anything already placed.
export function dropY(placed, it, x) {
  const ys = [0, ...placed.filter((p) => x < p.x + p.w && x + it.w > p.x).map((p) => p.y + p.h)].sort((a, b) => a - b);
  return ys.find((y) => !placed.some((p) => hits(p, it, x, y))) ?? 0;
}

// Where a widget with no place yet goes: the highest free spot, leftmost on a tie (the shortest column first).
export function autoPlace(placed, it) {
  let best = null;
  for (let x = 0; x + it.w <= COLS; x++) {
    const y = dropY(placed, it, x);
    if (!best || y < best.y || (y === best.y && x < best.x)) best = { x, y };
  }
  return best || { x: 0, y: 0 };
}

const gravity = (fixed, order) => {
  const placed = [...fixed];                   // `fixed` stay exactly where they are; each of `order` falls to its lowest free row
  for (const it of order) placed.push({ ...it, y: dropY(placed, it, it.x) });
  return placed;
};
const byPlace = (a, b) => a.y - b.y || a.x - b.x;

// items: [{id, x, y, w, h}] (x null: no place yet; h in rows, already measured for an "auto" widget). movingId: the one
// being dragged or resized keeps its place and the others make room, then everything packs up. Returns the same items
// with places that do not overlap, in the same order.
export function compact(items, movingId = null) {
  const placedItems = items.filter((i) => i.x !== null && i.x !== undefined).map((i) => ({ ...i, x: clamp(i.x, 0, COLS - i.w), y: Math.max(0, i.y) }));
  let step = placedItems;
  const mover = movingId ? placedItems.find((i) => i.id === movingId) : null;
  if (mover) {
    const others = gravity([mover], placedItems.filter((i) => i !== mover).sort(byPlace)).slice(1);   // the mover first, where it was put
    step = [mover, ...others];
  }
  let done = gravity([], [...step].sort(byPlace));
  for (const it of items.filter((i) => i.x === null || i.x === undefined)) {
    const spot = autoPlace(done, it);
    done.push({ ...it, ...spot });
  }
  const byId = new Map(done.map((i) => [i.id, i]));
  return items.map((i) => byId.get(i.id));
}

// A starting width for a widget with no saved place: two columns on a normal desktop, three or four on a very wide one.
export const defaultWidth = (screenWidth) => (screenWidth >= 2300 ? 3 : screenWidth >= 1500 ? 4 : 6);   // by the screen: four columns, three, or two
// The fewest columns a widget may span on a board this wide (a pixel minimum is more columns on a small screen), and the
// fewest rows.
export const minCols = (id, size, boardWidth) => {
  const m = metrics(boardWidth || 1100);
  return clamp(Math.ceil((minPx(id, size)[0] + GAPX) / m.stepX - 1e-6), 1, COLS);
};
export const minRows = (id, size) => Math.max(MIN_H, rowsFor(minPx(id, size)[1]));
// A place made to respect its minimum: wider or taller if it is less (a fixed height only; 0 stays "as tall as its content").
export function atLeast(c, id, size, boardWidth) {
  const w = Math.max(c.w, minCols(id, size, boardWidth));
  return { ...c, w, x: clamp(c.x, 0, COLS - w), h: c.h ? Math.max(c.h, minRows(id, size)) : 0 };
}

// Tidy up (Greg, 24 Nov): everything packed neatly, in the same order (reading down, then across). The board is cut into
// equal tracks (`track` columns each: the screen's usual widget width, as Reset gives), each widget is made a whole number
// of tracks wide (the nearest to its width, never under its minimum), so every edge lines up with the ones above and below.
// Then, in order, each goes to the highest free spot on a track (leftmost on a tie) that still comes after the one before
// it, so the order reads the same; nothing is left hanging over a gap (Home packs widgets upward, and a tidied Home is
// already packed). Heights are kept. items: [{id, w, h, min}] in reading order (h in rows, measured for one as tall as its
// content; min: its fewest columns). Returns [{id, x, y, w}].
export function tidy(items, track = 6) {
  const placed = [];
  let prev = null;
  for (const it of items) {
    const need = Math.max(it.min || 1, 1);
    const tracks = clamp(Math.max(Math.round(it.w / track), Math.ceil(need / track)), 1, Math.floor(COLS / track));
    const w = Math.min(COLS, Math.max(tracks * track, Math.min(need, COLS)));
    let best = null, latest = null;
    for (let x = 0; x + w <= COLS; x += track) {
      const cand = { ...it, x, w };
      const y = dropY(placed, cand, x);
      const after = !prev || y > prev.y || (y === prev.y && x > prev.x);
      if (after && (!best || y < best.y || (y === best.y && x < best.x))) best = { ...cand, y };
      if (!latest || y > latest.y) latest = { ...cand, y };
    }
    const spot = best || latest;                       // none after the one before: as far down as it can go
    placed.push(spot);
    prev = spot;
  }
  return placed.map(({ id, x, y, w }) => ({ id, x, y, w }));
}

// Snap guides while a widget is moved or resized: the column edges and rows where its edges line up with another
// widget's. Returns {cols: [column edge 0..12], rows: [row]}.
export function guides(items, mover) {
  const cols = new Set(), rows = new Set();
  const left = mover.x, right = mover.x + mover.w, top = mover.y, bottom = mover.y + mover.h;
  for (const o of items) {
    if (o.id === mover.id) continue;
    for (const e of [o.x, o.x + o.w]) if (e === left || e === right) cols.add(e);
    for (const e of [o.y, o.y + o.h]) if (e === top || e === bottom) rows.add(e);
  }
  if (left === 0) cols.add(0);
  if (right === COLS) cols.add(COLS);
  return { cols: [...cols].sort((a, b) => a - b), rows: [...rows].sort((a, b) => a - b) };
}

// The grid's geometry for a board of this width.
export function metrics(boardWidth) {
  const col = (boardWidth - GAPX * (COLS - 1)) / COLS;
  return { col, stepX: col + GAPX, stepY: ROWU };
}
// The cell under a point (pixels from the board's top left), for a widget w columns wide, with its top left grabbed.
export function cellAt(m, px, py, w) {
  return { x: clamp(Math.round(px / m.stepX), 0, COLS - w), y: Math.max(0, Math.round(py / m.stepY)) };
}
export const rowsFor = (px) => Math.max(1, Math.ceil((px + GAPV) / ROWU));
export const pxFor = (rows) => rows * ROWU - GAPV;

// What is kept: only the plain numbers of each place.
export function clean(layout, ids) {
  const out = {};
  for (const id of ids) {
    const c = layout[id];
    if (c && [c.x, c.y, c.w, c.h].every(Number.isInteger)) out[id] = { x: c.x, y: c.y, w: c.w, h: c.h };
  }
  return out;
}
// The widgets in reading order (down the page, then across): the phone's order follows what was arranged here.
export const readingOrder = (layout, ids) => [...ids].sort((a, b) => ((layout[a]?.y ?? 1e9) - (layout[b]?.y ?? 1e9)) || ((layout[a]?.x ?? 0) - (layout[b]?.x ?? 0)));
