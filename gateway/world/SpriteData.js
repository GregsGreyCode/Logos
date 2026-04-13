/**
 * SpriteData — spritesheet frame definitions for the character sheet.
 *
 * The sheet (characters.png) is 1920×1024 with 160 variants arranged in
 * a 20×8 grid of 96×128 blocks (8 bodies × 4 skin tones × 5 hair/theme colors).
 * Each block has 4 rows (down/left/right/up) × 3 frames per direction,
 * each cell 32×32px.
 *
 * char_index encoding:
 *     char_index = body * 20 + skin * 5 + hair
 *     body  ∈ [0..7]   — base body (inherited hairstyle + face)
 *     skin  ∈ [0..3]   — 0=lightest, 1=light (V×0.82), 2=medium (V×0.65), 3=dark (V×0.42)
 *     hair  ∈ [0..4]   — hair/outfit theme: 0=original, 1=midnight, 2=crimson, 3=terminal, 4=dusk
 *
 * Sheet layout:
 *     row = body
 *     col = skin * 5 + hair
 */

const CELL = 32;
const BLOCK_W = CELL * 3;  // 96px per character
const BLOCK_H = CELL * 4;  // 128px per character
const COLS = 20;            // characters per row in the sheet (4 skin × 5 hair)
const TOTAL_CHARS = 160;

/**
 * Generate ISpritesheetData for character at index (0..TOTAL_CHARS-1).
 */
function makeSpritesheetData(charIndex) {
  const bx = (charIndex % COLS) * BLOCK_W;
  const by = Math.floor(charIndex / COLS) * BLOCK_H;

  // Row order within each block: 0=down, 1=left, 2=right, 3=up
  const dirs = ['down', 'left', 'right', 'up'];
  const frames = {};
  const animations = {};

  for (let d = 0; d < 4; d++) {
    const dir = dirs[d];
    const frameNames = [];
    for (let f = 0; f < 3; f++) {
      const name = `${dir}${f}`;
      frames[name] = {
        frame: { x: bx + f * CELL, y: by + d * CELL, w: CELL, h: CELL },
        sourceSize: { w: CELL, h: CELL },
        spriteSourceSize: { x: 0, y: 0 },
      };
      frameNames.push(name);
    }
    animations[dir] = frameNames;
  }

  return { frames, animations, meta: { scale: '1' } };
}

/** All 8 character spritesheet definitions. */
export const CHARACTER_SHEETS = Array.from({ length: TOTAL_CHARS }, (_, i) => makeSpritesheetData(i));

export const CHARACTER_COUNT = TOTAL_CHARS;

/** Texture URL for the character spritesheet. */
export const CHARACTER_TEXTURE = '/static/world/characters.png?v=7';
