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

// Colors for procedural tiles — a calmer twilight-garden palette that
// sits visually under the indigo/purple Logos tree without competing.
// Greens shift slightly toward the blue end of the wheel so the whole
// scene reads as one chilly, coherent landscape instead of a saturated
// green lawn with a rainbow tree sat on top.
export const TILE_COLORS = {
  [TILE.GRASS]:      0x2f4537,
  [TILE.DARK_GRASS]: 0x26382c,
  [TILE.FLOWERS]:    0x385641,
  [TILE.WATER]:      0x2d4a72,
  [TILE.SAND]:       0x8a7a5a,
  [TILE.PATH]:       0x5a4c3a,
  [TILE.GARDEN_BED]: 0x253a22,
  [TILE.MOSS]:       0x334a37,
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
