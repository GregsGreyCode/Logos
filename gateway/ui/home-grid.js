// Home's desktop grid (F49, Greg 23 Nov: "in desktop mode the widgets aren't resizeable or able to be placed anywhere").
//
// A 12-column grid with rows of 8 px. Each widget has a place {x, y, w, h}: x, w in columns; y in rows; h in rows, or 0
// for "as tall as its content". Dropping a widget on another pushes the other down; the rest packs upward so there are
// no holes. Only a wide screen (1100 px and up) uses it; a phone keeps the ordered list. The layout is each person's own,
// kept with their login (the "widget_grid" pref); the order of the widgets (the phone's) follows it, reading down then across.
export const COLS = 12, ROWU = 8, GAPX = 24, GAPV = 22;
export const MIN_W = { next: 3, weather: 4, calendar: 4, messages: 3, coming: 3, lights: 3, lists: 3 };
export const MIN_H = 12;               // rows (96 px): a fixed height is never smaller than this

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
export const minWidth = (id) => MIN_W[id] || 3;

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
