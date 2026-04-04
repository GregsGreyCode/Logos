/**
 * WorldConfig — constants for the Agent World visualization.
 * Theme: a garden with a central Logos tree. Agents wander the garden.
 */
export const TILE_SIZE = 24;
export const WORLD_COLS = 20;
export const WORLD_ROWS = 20;
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
  clearing:  { x: 8,  y: 8,  w: 4, h: 4 },  // central clearing around the tree
  meadow:    { x: 2,  y: 2,  w: 4, h: 3 },   // open meadow
  pond:      { x: 15, y: 15, w: 3, h: 3 },   // near the pond
  grove:     { x: 14, y: 2,  w: 4, h: 3 },   // shaded grove
};

// Agent marker size
export const AGENT_RADIUS = 6;
export const AGENT_LABEL_STYLE = {
  fontFamily: 'monospace',
  fontSize: 8,
  fill: 0xffffff,
  align: 'center',
};
