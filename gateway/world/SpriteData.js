/**
 * SpriteData — spritesheet frame definitions for the 32x32folk character sheet.
 *
 * The sheet (characters.png) is 384x256 with 8 characters arranged in a
 * 4×2 grid of 96×128 blocks.  Each block has 4 rows (down/left/right/up)
 * × 3 frames per direction, each cell 32×32px.
 *
 * Source: ai-town (MIT) — OpenGameArt folk characters.
 */

const CELL = 32;
const BLOCK_W = CELL * 3;  // 96px per character
const BLOCK_H = CELL * 4;  // 128px per character
const COLS = 4;             // characters per row in the sheet

/**
 * Generate ISpritesheetData for character at index (0-7).
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
export const CHARACTER_SHEETS = Array.from({ length: 8 }, (_, i) => makeSpritesheetData(i));

/** Texture URL for the character spritesheet. */
export const CHARACTER_TEXTURE = '/static/world/characters.png';
