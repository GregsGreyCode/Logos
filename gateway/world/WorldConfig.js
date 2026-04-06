/**
 * WorldConfig — constants for the Agent World visualization.
 * Theme: a garden with a central Logos tree. Agents wander the garden.
 */
export const TILE_SIZE = 24;
export const WORLD_COLS = 40;
export const WORLD_ROWS = 40;
export const WORLD_W = WORLD_COLS * TILE_SIZE;
export const WORLD_H = WORLD_ROWS * TILE_SIZE;

// Tile types
export const TILE = {
  GRASS: 0,
  DARK_GRASS: 1,
  FLOWERS: 2,
  WATER: 3,
  SAND: 4,
  PATH: 5,
  GARDEN_BED: 6,
  MOSS: 7,
};

// Colors for procedural tiles
export const TILE_COLORS = {
  [TILE.GRASS]:      0x4a7c3f,
  [TILE.DARK_GRASS]: 0x3d6b34,
  [TILE.FLOWERS]:    0x5a8c4f,
  [TILE.WATER]:      0x3a6ea5,
  [TILE.SAND]:       0xc2b280,
  [TILE.PATH]:       0x7a7060,
  [TILE.GARDEN_BED]: 0x3a5e2f,
  [TILE.MOSS]:       0x4d7a42,
};

// Garden zones — organic areas where agents gather
export const ZONES = {
  clearing:  { x: 16, y: 16, w: 8, h: 8 },  // central clearing around the tree
  meadow:    { x: 4,  y: 4,  w: 8, h: 6 },   // open meadow (top-left)
  pond:      { x: 30, y: 30, w: 6, h: 6 },   // near the pond (bottom-right)
  grove:     { x: 28, y: 4,  w: 8, h: 6 },   // shaded grove (top-right)
};

// Agent marker size
export const AGENT_RADIUS = 6;
export const AGENT_LABEL_STYLE = {
  fontFamily: 'monospace',
  fontSize: 8,
  fill: 0xffffff,
  align: 'center',
};
