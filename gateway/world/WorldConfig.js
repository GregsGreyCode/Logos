/**
 * WorldConfig — constants for the Agent World visualization.
 */
export const TILE_SIZE = 16;
export const WORLD_COLS = 24;
export const WORLD_ROWS = 24;
export const WORLD_W = WORLD_COLS * TILE_SIZE;
export const WORLD_H = WORLD_ROWS * TILE_SIZE;

// Tile types for procedural map (Phase 1 — replaced by tileset in Phase 2)
export const TILE = {
  GRASS: 0,
  STONE: 1,
  WATER: 2,
  DARK_GRASS: 3,
  SAND: 4,
  FLOWERS: 5,
};

// Colors for procedural tiles
export const TILE_COLORS = {
  [TILE.GRASS]:      0x4a7c3f,
  [TILE.STONE]:      0x8a8a8a,
  [TILE.WATER]:      0x3a6ea5,
  [TILE.DARK_GRASS]: 0x3d6b34,
  [TILE.SAND]:       0xc2b280,
  [TILE.FLOWERS]:    0x5a8c4f,
};

// World zones — agents are placed here based on state
export const ZONES = {
  plaza:    { x: 10, y: 10, w: 4, h: 4 },   // idle agents hang out here
  workshop: { x: 3,  y: 3,  w: 5, h: 4 },   // working agents
  library:  { x: 17, y: 3,  w: 5, h: 4 },   // knowledge/memory related
  servers:  { x: 17, y: 17, w: 5, h: 4 },   // MCP / infra
};

// Agent marker size
export const AGENT_RADIUS = 6;
export const AGENT_LABEL_STYLE = {
  fontFamily: 'monospace',
  fontSize: 8,
  fill: 0xffffff,
  align: 'center',
};
