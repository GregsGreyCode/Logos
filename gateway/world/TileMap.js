/**
 * TileMap — garden-themed tilemap with a central Logos tree.
 *
 * The tree is pixel art in the center of the map, with its canopy
 * cycling through hue-shifted colors matching the Logos UI accent cycle.
 */
import {
  TILE_SIZE, WORLD_COLS, WORLD_ROWS,
  TILE, TILE_COLORS,
} from './WorldConfig.js';

/**
 * Generate a garden-themed tile grid.
 */
function generateMap() {
  const map = [];
  const cx = Math.floor(WORLD_COLS / 2);
  const cy = Math.floor(WORLD_ROWS / 2);

  for (let y = 0; y < WORLD_ROWS; y++) {
    const row = [];
    for (let x = 0; x < WORLD_COLS; x++) {
      // Distance from center
      const dx = x - cx;
      const dy = y - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);

      // Default: grass with variety
      let tile;
      const r = Math.random();
      if (r < 0.12) tile = TILE.DARK_GRASS;
      else if (r < 0.18) tile = TILE.MOSS;
      else if (r < 0.22) tile = TILE.FLOWERS;
      else tile = TILE.GRASS;

      // Circular clearing around center tree (radius ~3 tiles)
      if (dist < 3.5) tile = TILE.MOSS;

      // Garden beds — scattered organic patches
      if ((x >= 2 && x <= 4 && y >= 3 && y <= 5) ||
          (x >= 15 && x <= 17 && y >= 2 && y <= 4) ||
          (x >= 3 && x <= 5 && y >= 14 && y <= 16) ||
          (x >= 14 && x <= 16 && y >= 13 && y <= 15)) {
        tile = TILE.GARDEN_BED;
        if (r < 0.3) tile = TILE.FLOWERS;
      }

      // Pond — bottom right, organic shape
      if (x >= 15 && x <= 18 && y >= 15 && y <= 18) {
        const pdx = x - 16.5;
        const pdy = y - 16.5;
        if (pdx * pdx + pdy * pdy < 5) tile = TILE.WATER;
        else if (pdx * pdx + pdy * pdy < 7) tile = TILE.SAND;
      }

      // Winding paths from center to edges
      // Path to top
      if (x >= cx - 1 && x <= cx && y < cy - 3 && y > 1) tile = TILE.PATH;
      // Path to bottom
      if (x >= cx && x <= cx + 1 && y > cy + 3 && y < WORLD_ROWS - 2) tile = TILE.PATH;
      // Path to left
      if (y >= cy && y <= cy + 1 && x < cx - 3 && x > 1) tile = TILE.PATH;
      // Path to right
      if (y >= cy - 1 && y <= cy && x > cx + 3 && x < WORLD_COLS - 2) tile = TILE.PATH;

      // Flower border along edges
      if (x === 0 || x === WORLD_COLS - 1 || y === 0 || y === WORLD_ROWS - 1) {
        tile = r < 0.4 ? TILE.FLOWERS : TILE.DARK_GRASS;
      }

      row.push(tile);
    }
    map.push(row);
  }

  return map;
}

// Pixel art tree (12x14 tiles, drawn relative to center)
// Each row is [relativeX, relativeY, color] — the tree is centered at (0, 0)
const TREE_PIXELS = [
  // Trunk (brown)
  { dx: 0, dy: 3, color: 0x6b4423 },
  { dx: 0, dy: 2, color: 0x6b4423 },
  { dx: 0, dy: 1, color: 0x7a5233 },
  { dx: -1, dy: 3, color: 0x5a3a1a },
  { dx: 1, dy: 3, color: 0x5a3a1a },
  { dx: 0, dy: 4, color: 0x5a3a1a },
  // Roots
  { dx: -2, dy: 4, color: 0x4a3015 },
  { dx: 2, dy: 4, color: 0x4a3015 },
  { dx: -1, dy: 4, color: 0x5a3a1a },
  { dx: 1, dy: 4, color: 0x5a3a1a },
];

// Canopy pixels — these get hue-cycled
const CANOPY_PIXELS = [
  // Layer 1 (top)
  { dx: -1, dy: -4, l: 0.9 },
  { dx: 0, dy: -4, l: 1.0 },
  { dx: 1, dy: -4, l: 0.9 },
  // Layer 2
  { dx: -2, dy: -3, l: 0.85 },
  { dx: -1, dy: -3, l: 0.95 },
  { dx: 0, dy: -3, l: 1.0 },
  { dx: 1, dy: -3, l: 0.95 },
  { dx: 2, dy: -3, l: 0.85 },
  // Layer 3
  { dx: -3, dy: -2, l: 0.8 },
  { dx: -2, dy: -2, l: 0.9 },
  { dx: -1, dy: -2, l: 1.0 },
  { dx: 0, dy: -2, l: 0.95 },
  { dx: 1, dy: -2, l: 1.0 },
  { dx: 2, dy: -2, l: 0.9 },
  { dx: 3, dy: -2, l: 0.8 },
  // Layer 4
  { dx: -3, dy: -1, l: 0.85 },
  { dx: -2, dy: -1, l: 0.95 },
  { dx: -1, dy: -1, l: 1.0 },
  { dx: 0, dy: -1, l: 0.9 },
  { dx: 1, dy: -1, l: 1.0 },
  { dx: 2, dy: -1, l: 0.95 },
  { dx: 3, dy: -1, l: 0.85 },
  // Layer 5 (widest)
  { dx: -3, dy: 0, l: 0.8 },
  { dx: -2, dy: 0, l: 0.9 },
  { dx: -1, dy: 0, l: 0.95 },
  { dx: 1, dy: 0, l: 0.95 },
  { dx: 2, dy: 0, l: 0.9 },
  { dx: 3, dy: 0, l: 0.8 },
];

function hslToHex(h, s, l) {
  h /= 360; s /= 100; l /= 100;
  let r, g, b;
  if (s === 0) { r = g = b = l; }
  else {
    const hue2rgb = (p, q, t) => {
      if (t < 0) t += 1; if (t > 1) t -= 1;
      if (t < 1/6) return p + (q - p) * 6 * t;
      if (t < 1/2) return q;
      if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
      return p;
    };
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r = hue2rgb(p, q, h + 1/3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1/3);
  }
  return (Math.round(r * 255) << 16) + (Math.round(g * 255) << 8) + Math.round(b * 255);
}

/**
 * Create tilemap container with garden + central Logos tree.
 */
export function createTileMap(PIXI) {
  const container = new PIXI.Container();
  const map = generateMap();

  const cx = Math.floor(WORLD_COLS / 2);
  const cy = Math.floor(WORLD_ROWS / 2);

  // Draw tiles
  for (let y = 0; y < WORLD_ROWS; y++) {
    for (let x = 0; x < WORLD_COLS; x++) {
      const tile = map[y][x];
      const color = TILE_COLORS[tile] || TILE_COLORS[TILE.GRASS];
      const g = new PIXI.Graphics();
      g.beginFill(color);
      g.drawRect(0, 0, TILE_SIZE, TILE_SIZE);
      g.endFill();
      g.lineStyle(0.5, 0x000000, 0.05);
      g.drawRect(0, 0, TILE_SIZE, TILE_SIZE);
      g.x = x * TILE_SIZE;
      g.y = y * TILE_SIZE;
      container.addChild(g);
    }
  }

  // Draw tree trunk (static)
  for (const p of TREE_PIXELS) {
    const g = new PIXI.Graphics();
    g.beginFill(p.color);
    g.drawRect(0, 0, TILE_SIZE, TILE_SIZE);
    g.endFill();
    g.x = (cx + p.dx) * TILE_SIZE;
    g.y = (cy + p.dy) * TILE_SIZE;
    container.addChild(g);
  }

  // Draw canopy (hue-cycling) — store references for animation
  const canopyGraphics = [];
  for (const p of CANOPY_PIXELS) {
    const g = new PIXI.Graphics();
    g.x = (cx + p.dx) * TILE_SIZE;
    g.y = (cy + p.dy) * TILE_SIZE;
    g._canopyL = p.l;
    g._canopyDx = p.dx;
    g._canopyDy = p.dy;
    container.addChild(g);
    canopyGraphics.push(g);
  }

  // "Logos" label under the tree
  const label = new PIXI.Text('Logos', {
    fontFamily: 'monospace',
    fontSize: 7,
    fill: 0xffffff,
    align: 'center',
  });
  label.alpha = 0.5;
  label.anchor.set(0.5);
  label.x = cx * TILE_SIZE + TILE_SIZE / 2;
  label.y = (cy + 5.5) * TILE_SIZE;
  container.addChild(label);

  // Animate canopy hue — matches the Logos UI hue cycle (360° in 60s)
  let _tick = 0;
  const ticker = new PIXI.Ticker();
  ticker.add(() => {
    _tick += ticker.deltaMS / 1000;
    const hue = (_tick * 6) % 360; // 360° in 60 seconds
    for (const g of canopyGraphics) {
      const brightness = 25 + (g._canopyL * 20);
      const saturation = 60 + (g._canopyL * 15);
      // Each pixel gets a slight hue offset based on position for shimmer
      const offset = (g._canopyDx * 5 + g._canopyDy * 8) % 30;
      const color = hslToHex(hue + offset, saturation, brightness);
      g.clear();
      g.beginFill(color);
      g.drawRect(0, 0, TILE_SIZE, TILE_SIZE);
      g.endFill();
    }
  });
  ticker.start();

  // Store ticker on container for cleanup
  container._treeTicker = ticker;

  return container;
}
