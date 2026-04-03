/**
 * TileMap — procedural tilemap renderer for the Agent World.
 *
 * Phase 1: generates a simple grass/stone/water map procedurally.
 * Phase 2: replaced by Tiled JSON + tileset spritesheet.
 */
import {
  TILE_SIZE, WORLD_COLS, WORLD_ROWS, WORLD_W, WORLD_H,
  TILE, TILE_COLORS, ZONES,
} from './WorldConfig.js';

/**
 * Generate a procedural tile grid.
 * Returns a 2D array [row][col] of tile type IDs.
 */
function generateMap() {
  const map = [];
  for (let y = 0; y < WORLD_ROWS; y++) {
    const row = [];
    for (let x = 0; x < WORLD_COLS; x++) {
      // Default: grass with random dark grass patches
      let tile = Math.random() < 0.15 ? TILE.DARK_GRASS : TILE.GRASS;

      // Flower patches
      if (tile === TILE.GRASS && Math.random() < 0.05) tile = TILE.FLOWERS;

      // Water — small pond in bottom-left
      if (x >= 2 && x <= 5 && y >= 18 && y <= 21) tile = TILE.WATER;
      // Sand border around water
      if (tile !== TILE.WATER &&
          x >= 1 && x <= 6 && y >= 17 && y <= 22 &&
          !(x >= 2 && x <= 5 && y >= 18 && y <= 21)) {
        tile = TILE.SAND;
      }

      row.push(tile);
    }
    map.push(row);
  }

  // Stone paths connecting zones
  // Horizontal path through center
  for (let x = 0; x < WORLD_COLS; x++) {
    map[12][x] = TILE.STONE;
    map[11][x] = TILE.STONE;
  }
  // Vertical path through center
  for (let y = 0; y < WORLD_ROWS; y++) {
    map[y][12] = TILE.STONE;
    map[y][11] = TILE.STONE;
  }

  // Zone floors — slightly different stone for zones
  for (const zone of Object.values(ZONES)) {
    for (let y = zone.y; y < zone.y + zone.h; y++) {
      for (let x = zone.x; x < zone.x + zone.w; x++) {
        if (y >= 0 && y < WORLD_ROWS && x >= 0 && x < WORLD_COLS) {
          map[y][x] = TILE.STONE;
        }
      }
    }
  }

  return map;
}

/**
 * Create a PIXI.Container with the tilemap rendered as colored rectangles.
 */
export function createTileMap(PIXI) {
  const container = new PIXI.Container();
  const map = generateMap();

  for (let y = 0; y < WORLD_ROWS; y++) {
    for (let x = 0; x < WORLD_COLS; x++) {
      const tile = map[y][x];
      const color = TILE_COLORS[tile] || TILE_COLORS[TILE.GRASS];
      const g = new PIXI.Graphics();
      g.beginFill(color);
      g.drawRect(0, 0, TILE_SIZE, TILE_SIZE);
      g.endFill();

      // Subtle grid lines
      g.lineStyle(0.5, 0x000000, 0.08);
      g.drawRect(0, 0, TILE_SIZE, TILE_SIZE);

      g.x = x * TILE_SIZE;
      g.y = y * TILE_SIZE;
      container.addChild(g);
    }
  }

  // Zone labels
  for (const [name, zone] of Object.entries(ZONES)) {
    const label = new PIXI.Text(name, {
      fontFamily: 'monospace',
      fontSize: 7,
      fill: 0xffffff,
      align: 'center',
    });
    label.alpha = 0.3;
    label.anchor.set(0.5);
    label.x = (zone.x + zone.w / 2) * TILE_SIZE;
    label.y = (zone.y - 0.5) * TILE_SIZE;
    container.addChild(label);
  }

  return container;
}
