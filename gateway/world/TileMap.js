/**
 * TileMap — garden-themed tilemap with a central Logos tree (top-down view).
 *
 * The tree is a circular canopy viewed from above, with hue-cycling colors
 * matching the Logos UI accent cycle. Agents cannot walk on the tree trunk.
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
      const dx = x - cx;
      const dy = y - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const r = Math.random();

      // Default grass with variety
      let tile;
      if (r < 0.12) tile = TILE.DARK_GRASS;
      else if (r < 0.18) tile = TILE.MOSS;
      else if (r < 0.22) tile = TILE.FLOWERS;
      else tile = TILE.GRASS;

      // Clearing around center tree (radius ~5 for larger world)
      if (dist < 5.5) tile = TILE.MOSS;

      // Tree trunk — 1 tile in the center (blocks movement)
      if (x === cx && y === cy) tile = TILE.PATH; // trunk marker

      // Garden beds — scattered around the map
      if ((x >= 4 && x <= 7 && y >= 6 && y <= 9) ||
          (x >= 30 && x <= 34 && y >= 4 && y <= 7) ||
          (x >= 5 && x <= 9 && y >= 28 && y <= 32) ||
          (x >= 28 && x <= 32 && y >= 26 && y <= 30) ||
          (x >= 14 && x <= 17 && y >= 4 && y <= 6) ||
          (x >= 24 && x <= 27 && y >= 33 && y <= 36)) {
        tile = TILE.GARDEN_BED;
        if (r < 0.3) tile = TILE.FLOWERS;
      }

      // Pond — bottom right, larger
      if (x >= 30 && x <= 37 && y >= 30 && y <= 37) {
        const pdx = x - 33.5;
        const pdy = y - 33.5;
        if (pdx * pdx + pdy * pdy < 9) tile = TILE.WATER;
        else if (pdx * pdx + pdy * pdy < 14) tile = TILE.SAND;
      }

      // Second pond — top left area
      if (x >= 3 && x <= 8 && y >= 3 && y <= 8) {
        const pdx = x - 5.5;
        const pdy = y - 5.5;
        if (pdx * pdx + pdy * pdy < 5) tile = TILE.WATER;
        else if (pdx * pdx + pdy * pdy < 8) tile = TILE.SAND;
      }

      // Winding paths — extend to fill larger world
      if (x >= cx - 1 && x <= cx && y < cy - 5 && y > 1) tile = TILE.PATH;
      if (x >= cx && x <= cx + 1 && y > cy + 5 && y < WORLD_ROWS - 2) tile = TILE.PATH;
      if (y >= cy && y <= cy + 1 && x < cx - 5 && x > 1) tile = TILE.PATH;
      if (y >= cy - 1 && y <= cy && x > cx + 5 && x < WORLD_COLS - 2) tile = TILE.PATH;
      // Diagonal paths to corners
      if (Math.abs(x - y) <= 1 && x < cx - 5 && y < cy - 5 && x > 1) tile = TILE.PATH;
      if (Math.abs((WORLD_COLS - 1 - x) - y) <= 1 && x > cx + 5 && y < cy - 5 && x < WORLD_COLS - 2) tile = TILE.PATH;

      // Border
      if (x === 0 || x === WORLD_COLS - 1 || y === 0 || y === WORLD_ROWS - 1) {
        tile = r < 0.4 ? TILE.FLOWERS : TILE.DARK_GRASS;
      }

      row.push(tile);
    }
    map.push(row);
  }

  return map;
}

function hslToHex(h, s, l) {
  h /= 360; s /= 100; l /= 100;
  let r, g, b;
  if (s === 0) { r = g = b = l; }
  else {
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    const hue2rgb = (p, q, t) => {
      if (t < 0) t += 1; if (t > 1) t -= 1;
      if (t < 1/6) return p + (q - p) * 6 * t;
      if (t < 1/2) return q;
      if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
      return p;
    };
    r = hue2rgb(p, q, h + 1/3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1/3);
  }
  return (Math.round(r * 255) << 16) + (Math.round(g * 255) << 8) + Math.round(b * 255);
}

// Top-down canopy pixels — circular shape, ~5 tile diameter for larger world
// Relative to center tile. Each has a brightness multiplier for depth.
const CANOPY_PIXELS = [
  // Core (brightest — top of canopy)
  { dx: 0, dy: 0, l: 1.0 },
  { dx: -1, dy: 0, l: 0.97 },
  { dx: 1, dy: 0, l: 0.97 },
  { dx: 0, dy: -1, l: 0.97 },
  { dx: 0, dy: 1, l: 0.95 },
  // Inner ring
  { dx: -1, dy: -1, l: 0.92 },
  { dx: 1, dy: -1, l: 0.92 },
  { dx: -1, dy: 1, l: 0.90 },
  { dx: 1, dy: 1, l: 0.90 },
  // Middle ring
  { dx: -2, dy: 0, l: 0.85 },
  { dx: 2, dy: 0, l: 0.85 },
  { dx: 0, dy: -2, l: 0.87 },
  { dx: 0, dy: 2, l: 0.82 },
  { dx: -2, dy: -1, l: 0.80 },
  { dx: 2, dy: -1, l: 0.80 },
  { dx: -2, dy: 1, l: 0.78 },
  { dx: 2, dy: 1, l: 0.78 },
  { dx: -1, dy: -2, l: 0.80 },
  { dx: 1, dy: -2, l: 0.80 },
  { dx: -1, dy: 2, l: 0.75 },
  { dx: 1, dy: 2, l: 0.75 },
  // Outer ring
  { dx: -3, dy: 0, l: 0.65 },
  { dx: 3, dy: 0, l: 0.65 },
  { dx: 0, dy: -3, l: 0.68 },
  { dx: 0, dy: 3, l: 0.60 },
  { dx: -2, dy: -2, l: 0.62 },
  { dx: 2, dy: -2, l: 0.62 },
  { dx: -2, dy: 2, l: 0.58 },
  { dx: 2, dy: 2, l: 0.58 },
];

/**
 * Create tilemap container with garden + central Logos tree (top-down).
 */
export function createTileMap(PIXI) {
  const container = new PIXI.Container();
  const map = generateMap();
  const cx = Math.floor(WORLD_COLS / 2);
  const cy = Math.floor(WORLD_ROWS / 2);

  // Ground layer
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

  // Tree trunk (dark brown circle, top-down) — drawn under canopy
  const trunk = new PIXI.Graphics();
  trunk.beginFill(0x5a3a1a);
  const trunkR = TILE_SIZE * 0.4;
  trunk.drawCircle(cx * TILE_SIZE + TILE_SIZE / 2, cy * TILE_SIZE + TILE_SIZE / 2, trunkR);
  trunk.endFill();
  container.addChild(trunk);

  // Canopy layer — separate container so it renders above agents on the south side
  // (agents walking behind the tree appear under the canopy)
  const canopyContainer = new PIXI.Container();
  canopyContainer.zIndex = 100; // above agent layer
  const canopyGraphics = [];

  for (const p of CANOPY_PIXELS) {
    const g = new PIXI.Graphics();
    g.x = (cx + p.dx) * TILE_SIZE;
    g.y = (cy + p.dy) * TILE_SIZE;
    g._canopyL = p.l;
    g._canopyDx = p.dx;
    g._canopyDy = p.dy;
    canopyContainer.addChild(g);
    canopyGraphics.push(g);
  }

  container.addChild(canopyContainer);

  // "Logos" label south of canopy
  const label = new PIXI.Text('\u25c6', {
    fontFamily: 'monospace',
    fontSize: 10,
    fill: 0xffffff,
    align: 'center',
  });
  label.alpha = 0.4;
  label.anchor.set(0.5);
  label.x = cx * TILE_SIZE + TILE_SIZE / 2;
  label.y = (cy + 3) * TILE_SIZE;
  container.addChild(label);

  // Animate canopy hue — 360° in 60 seconds, matching UI accent cycle
  let _tick = 0;
  const ticker = new PIXI.Ticker();
  ticker.add(() => {
    _tick += ticker.deltaMS / 1000;
    const hue = (_tick * 6) % 360;
    for (const g of canopyGraphics) {
      const brightness = 22 + (g._canopyL * 22);
      const saturation = 55 + (g._canopyL * 20);
      const offset = (g._canopyDx * 8 + g._canopyDy * 12) % 25;
      const color = hslToHex(hue + offset, saturation, brightness);
      g.clear();
      g.beginFill(color, 0.9);
      g.drawRect(0, 0, TILE_SIZE, TILE_SIZE);
      g.endFill();
    }
  });
  ticker.start();
  container._treeTicker = ticker;

  // Export collision info for agent movement
  container._treeCenter = { x: cx, y: cy };
  container._treeRadius = 4; // agents should stay 4+ tiles from center (larger world)

  return container;
}
