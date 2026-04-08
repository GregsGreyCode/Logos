/**
 * WorldScene — main Phaser scene for the agent world.
 *
 * Renders the tilemap, manages agent sprites, handles camera,
 * and drives pathfinding + zone behaviour.
 */
import { TILE_SIZE, WORLD_COLS, WORLD_ROWS, WORLD_W, WORLD_H, TILE, TILE_COLORS, ZONES } from '../WorldConfig.js';
import { CHARACTER_TEXTURE } from '../SpriteData.js';
import { AgentSprite } from './AgentSprite.js';
import { Pathfinder } from './Pathfinder.js';

const CELL = 32;
const BLOCK_W = CELL * 3;
const BLOCK_H = CELL * 4;
const SHEET_COLS = 4;

export class WorldScene extends Phaser.Scene {
  constructor() {
    super({ key: 'WorldScene' });
    this.agents = new Map();       // name → AgentSprite
    this.walkableGrid = null;      // 2D boolean array
    this.pathfinder = null;
    this.treeCenter = { x: 0, y: 0 };
    this.canopyGraphics = null;
    this.canopyPixels = [];
    this._onAgentClick = null;
    this._onAgentHover = null;
    this._hueOffset = 0;
    this._ready = false;           // set true after create() completes
    this._pendingSync = null;      // buffered syncAgents call
  }

  init(data) {
    this._onAgentClick = data.onAgentClick || (() => {});
    this._onAgentHover = data.onAgentHover || (() => {});
  }

  preload() {
    // Load master sheet as a spritesheet with 32x32 cells.
    // The image is 384x256 = 12 cols x 8 rows = 96 frames total.
    this.load.spritesheet('characters', CHARACTER_TEXTURE, {
      frameWidth: CELL,
      frameHeight: CELL,
    });
  }

  create() {
    // Build tilemap
    this._buildTilemap();

    // Create spritesheet frames from the characters image
    this._buildSpritesheets();

    // Agent container group (between ground and canopy)
    this.agentGroup = this.add.group();

    // Draw tree canopy above agents
    this._buildCanopy();

    // Set up camera
    this.cameras.main.setBounds(0, 0, WORLD_W, WORLD_H);
    // Zoom to fill the container — no dead space margin
    this.cameras.main.setZoom(Math.max(
      this.scale.width / WORLD_W,
      this.scale.height / WORLD_H
    ));
    this.cameras.main.centerOn(WORLD_W / 2, WORLD_H / 2);

    // Camera drag + zoom — ignore drags that started on an agent
    this._isDraggingAgent = false;
    this.input.on('gameobjectdown', () => { this._isDraggingAgent = true; });
    this.input.on('pointerup', () => { this._isDraggingAgent = false; });
    this.input.on('pointermove', (pointer) => {
      if (pointer.isDown && !this._isDraggingAgent) {
        this.cameras.main.scrollX -= (pointer.x - pointer.prevPosition.x) / this.cameras.main.zoom;
        this.cameras.main.scrollY -= (pointer.y - pointer.prevPosition.y) / this.cameras.main.zoom;
      }
    });
    this.input.on('wheel', (pointer, gameObjects, deltaX, deltaY) => {
      const cam = this.cameras.main;
      const newZoom = Phaser.Math.Clamp(cam.zoom - deltaY * 0.001, 0.5, 6);
      cam.setZoom(newZoom);
    });

    // Pathfinder
    this.pathfinder = new Pathfinder(this.walkableGrid, WORLD_COLS, WORLD_ROWS);

    // Resize handler
    this.scale.on('resize', (gameSize) => {
      this.cameras.main.setSize(gameSize.width, gameSize.height);
    });

    // Mark ready and flush pending sync
    this._ready = true;
    if (this._pendingSync) {
      this.syncAgents(this._pendingSync);
      this._pendingSync = null;
    }
  }

  update(time, delta) {
    // Wall-clock anchored so the tree canopy's hue rotation stays in lock
    // step with the Logos logo in the top-left of the main app, which uses
    // the same `Date.now()/1000 * 6` formula to drive its CSS --hue-deg
    // variable. Phaser's `time` parameter is relative to scene start, so
    // using it would drift out of phase the moment the world tab is opened.
    this._hueOffset = (((Date.now() / 1000) * 6) % 360 + 360) % 360;
    this._updateCanopy();

    for (const [, agent] of this.agents) {
      agent.update(time, delta);
    }
  }

  // -- Public API (called by PhaserWorldManager) --

  syncAgents(instances) {
    if (!this._ready) {
      this._pendingSync = instances;
      return;
    }
    const currentNames = new Set(instances.map(i => i.name));

    // Remove departed agents
    for (const [name, agent] of this.agents) {
      if (!currentNames.has(name)) {
        agent.destroy();
        this.agents.delete(name);
      }
    }

    // Add or update agents
    const total = instances.length;
    instances.forEach((inst, index) => {
      const existing = this.agents.get(inst.name);
      if (existing) {
        existing.syncState(inst, index, total);
      } else {
        const agent = new AgentSprite(this, inst, index, total, {
          onClick: this._onAgentClick,
          onHover: this._onAgentHover,
        });
        this.agents.set(inst.name, agent);
      }
    });
  }

  // -- Tilemap --

  _buildTilemap() {
    const grid = Array.from({ length: WORLD_ROWS }, () =>
      new Array(WORLD_COLS).fill(TILE.GRASS)
    );

    const cx = Math.floor(WORLD_COLS / 2);
    const cy = Math.floor(WORLD_ROWS / 2);
    this.treeCenter = { x: cx, y: cy };

    // Central clearing (moss)
    for (let r = 0; r < WORLD_ROWS; r++) {
      for (let c = 0; c < WORLD_COLS; c++) {
        const dist = Math.sqrt((c - cx) ** 2 + (r - cy) ** 2);
        if (dist < 5.5) grid[r][c] = TILE.MOSS;
      }
    }
    grid[cy][cx] = TILE.PATH; // tree trunk

    // Garden beds
    const beds = [[14, 17, 3, 3], [30, 34, 2, 2], [5, 9, 2, 3], [28, 32, 3, 2]];
    for (const [bx, by, bw, bh] of beds) {
      for (let r = by; r < by + bh && r < WORLD_ROWS; r++) {
        for (let c = bx; c < bx + bw && c < WORLD_COLS; c++) {
          grid[r][c] = TILE.GARDEN_BED;
        }
      }
    }

    // Ponds
    const ponds = [[30, 30, 3], [8, 32, 2.5]];
    for (const [px, py, pr] of ponds) {
      for (let r = 0; r < WORLD_ROWS; r++) {
        for (let c = 0; c < WORLD_COLS; c++) {
          const dist = Math.sqrt((c - px) ** 2 + (r - py) ** 2);
          if (dist < pr) grid[r][c] = TILE.WATER;
          else if (dist < pr + 1 && grid[r][c] === TILE.GRASS) grid[r][c] = TILE.SAND;
        }
      }
    }

    // Paths — from center to edges
    const pathPoints = [
      [[cx, cy], [cx, 0]],       // north
      [[cx, cy], [cx, WORLD_ROWS - 1]], // south
      [[cx, cy], [0, cy]],       // west
      [[cx, cy], [WORLD_COLS - 1, cy]], // east
      [[cx, cy], [4, 4]],        // to meadow
      [[cx, cy], [30, 30]],      // to pond
    ];
    for (const [from, to] of pathPoints) {
      this._drawPath(grid, from[0], from[1], to[0], to[1]);
    }

    // Flower border
    for (let c = 0; c < WORLD_COLS; c++) {
      if (grid[0][c] === TILE.GRASS) grid[0][c] = TILE.FLOWERS;
      if (grid[WORLD_ROWS - 1][c] === TILE.GRASS) grid[WORLD_ROWS - 1][c] = TILE.FLOWERS;
    }
    for (let r = 0; r < WORLD_ROWS; r++) {
      if (grid[r][0] === TILE.GRASS) grid[r][0] = TILE.FLOWERS;
      if (grid[r][WORLD_COLS - 1] === TILE.GRASS) grid[r][WORLD_COLS - 1] = TILE.FLOWERS;
    }

    // Scatter flowers on some grass tiles
    for (let r = 2; r < WORLD_ROWS - 2; r++) {
      for (let c = 2; c < WORLD_COLS - 2; c++) {
        if (grid[r][c] === TILE.GRASS && Math.random() < 0.06) {
          grid[r][c] = TILE.FLOWERS;
        }
        if (grid[r][c] === TILE.GRASS && Math.random() < 0.08) {
          grid[r][c] = TILE.DARK_GRASS;
        }
      }
    }

    // Render tiles — no grid lines (they read as "programmer art"), and
    // each tile gets a tiny deterministic brightness jitter so adjacent
    // tiles feel organic instead of lego-block flat.
    const gfx = this.add.graphics();
    for (let r = 0; r < WORLD_ROWS; r++) {
      for (let c = 0; c < WORLD_COLS; c++) {
        const base = TILE_COLORS[grid[r][c]] || 0x2f4537;
        // Deterministic pseudo-noise so the world is stable across redraws.
        // Scale RGB channels ±7% for subtle per-tile variation.
        const n = ((r * 73856093) ^ (c * 19349663)) & 0xff;
        const k = 1 + ((n / 255) * 0.14 - 0.07);
        const br = Math.max(0, Math.min(255, Math.round(((base >> 16) & 0xff) * k)));
        const bgc = Math.max(0, Math.min(255, Math.round(((base >> 8) & 0xff) * k)));
        const bb = Math.max(0, Math.min(255, Math.round((base & 0xff) * k)));
        gfx.fillStyle((br << 16) | (bgc << 8) | bb, 1);
        gfx.fillRect(c * TILE_SIZE, r * TILE_SIZE, TILE_SIZE, TILE_SIZE);
      }
    }
    gfx.setDepth(0);

    // Radial vignette overlay — darker at the edges, focuses attention on
    // the tree and the agents in the centre. Drawn as a stack of hollow
    // ring strokes (not filled circles) so the alphas don't accumulate at
    // the centre. Each ring covers a narrow radial band with an alpha
    // that ramps up toward the outer edge of the world.
    const vignette = this.add.graphics();
    vignette.setDepth(1);
    const vcx = WORLD_W / 2;
    const vcy = WORLD_H / 2;
    const vmaxR = Math.sqrt(vcx * vcx + vcy * vcy);
    const STEPS = 48;
    for (let i = 0; i < STEPS; i++) {
      const rNorm = (i + 0.5) / STEPS;
      if (rNorm < 0.55) continue;  // leave the centre 55% untouched
      const t = (rNorm - 0.55) / 0.45;  // 0..1 across the outer band
      const alpha = Math.min(0.55, t * t * 0.65);  // quadratic darkening
      const bandWidth = vmaxR / STEPS + 2;  // slight overlap kills seams
      vignette.lineStyle(bandWidth, 0x050510, alpha);
      vignette.strokeCircle(vcx, vcy, rNorm * vmaxR);
    }

    // No visible trunk — canopy covers the center completely

    // Build walkable grid (for pathfinding)
    this.walkableGrid = grid.map(row =>
      row.map(tile => tile !== TILE.WATER && tile !== TILE.GARDEN_BED)
    );
    // Block entire tree canopy area (radius 6 tiles) — agents must path around
    const canopyBlock = 6;
    for (let dr = -canopyBlock; dr <= canopyBlock; dr++) {
      for (let dc = -canopyBlock; dc <= canopyBlock; dc++) {
        const dist = Math.sqrt(dr * dr + dc * dc);
        if (dist > canopyBlock) continue;
        const rr = cy + dr, cc = cx + dc;
        if (rr >= 0 && rr < WORLD_ROWS && cc >= 0 && cc < WORLD_COLS) {
          this.walkableGrid[rr][cc] = false;
        }
      }
    }
  }

  _drawPath(grid, x0, y0, x1, y1) {
    const steps = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0));
    for (let i = 0; i <= steps; i++) {
      const t = steps === 0 ? 0 : i / steps;
      const c = Math.round(x0 + (x1 - x0) * t);
      const r = Math.round(y0 + (y1 - y0) * t);
      if (r >= 0 && r < WORLD_ROWS && c >= 0 && c < WORLD_COLS) {
        if (grid[r][c] !== TILE.WATER && grid[r][c] !== TILE.MOSS) {
          grid[r][c] = TILE.PATH;
        }
      }
    }
  }

  // -- Tree Canopy --

  _buildCanopy() {
    this.canopyGraphics = this.add.graphics();
    this.canopyGraphics.setDepth(100); // above agents

    const cx = this.treeCenter.x * TILE_SIZE + TILE_SIZE / 2;
    const cy = this.treeCenter.y * TILE_SIZE + TILE_SIZE / 2;
    const radius = 5;  // 2x larger canopy

    this.canopyPixels = [];
    for (let dy = -6; dy <= 6; dy++) {
      for (let dx = -6; dx <= 6; dx++) {
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist > radius) continue;
        const brightness = 1 - (dist / radius) * 0.35;
        this.canopyPixels.push({
          x: cx + dx * TILE_SIZE,
          y: cy + dy * TILE_SIZE,
          brightness,
          distNorm: dist / radius,
        });
      }
    }
  }

  _updateCanopy() {
    const gfx = this.canopyGraphics;
    gfx.clear();

    // Base hues match the Logos logo gradient: indigo #6366f1 (H=239)
    // and purple #a855f7 (H=271). The whole canopy rotates through the
    // colour wheel as a unit — no per-pixel rainbow — so the tree reads
    // as the same identity mark as the logo in the top-left of the app.
    const BASE_INNER_H = 239;  // indigo
    const BASE_OUTER_H = 271;  // purple
    const rotate = this._hueOffset;  // deg, cycles 360 in 60s

    for (let i = 0; i < this.canopyPixels.length; i++) {
      const p = this.canopyPixels[i];
      // Lerp hue by distance from centre: inner leaves are indigo, outer
      // leaves are purple. Both rotate in lock-step.
      const hue = ((p.distNorm < 1 ? BASE_INNER_H + (BASE_OUTER_H - BASE_INNER_H) * p.distNorm : BASE_OUTER_H) + rotate) % 360;
      const sat = 0.72;
      const lit = 0.38 + p.brightness * 0.22;
      const color = Phaser.Display.Color.HSLToColor(hue / 360, sat, lit);
      // Softer blob with an inner highlight for depth
      gfx.fillStyle(color.color, 0.92);
      gfx.fillCircle(p.x, p.y, TILE_SIZE * 0.65);
      // Glow rim (lighter, larger, lower alpha) gives the tree a halo
      gfx.fillStyle(color.color, 0.18);
      gfx.fillCircle(p.x, p.y, TILE_SIZE * 1.1);
    }
  }

  // -- Spritesheets --

  _buildSpritesheets() {
    // The master spritesheet is 384x256 = 12 columns x 8 rows of 32x32 frames.
    // Characters are arranged in a 4x2 grid of 96x128 blocks (3 frames x 4 dirs).
    // Block layout in the sheet:
    //   char0(cols 0-2) | char1(cols 3-5) | char2(cols 6-8) | char3(cols 9-11)
    //   char4(cols 0-2) | char5(cols 3-5) | char6(cols 6-8) | char7(cols 9-11)
    // Within each block, rows are: down, left, right, up (3 frames each).
    // Global frame = (globalRow * 12) + globalCol
    // No extra textures needed — we just compute frame indices.
  }

  /**
   * Get global frame indices for a character's walk animation in a direction.
   */
  getCharFrames(charIndex, direction) {
    const dirMap = { down: 0, left: 1, right: 2, up: 3 };
    const dirRow = dirMap[direction] || 0;

    // Character's top-left in the global grid
    const blockCol = (charIndex % SHEET_COLS) * 3;  // 0, 3, 6, or 9
    const blockRow = Math.floor(charIndex / SHEET_COLS) * 4;  // 0 or 4

    const globalRow = blockRow + dirRow;
    const sheetCols = 12; // 384 / 32

    return [0, 1, 2].map(f => globalRow * sheetCols + blockCol + f);
  }

  getCharIdleFrame(charIndex, direction) {
    return this.getCharFrames(charIndex, direction)[0];
  }

  getCharTextureKey(charIndex) {
    return 'characters'; // all characters share the master spritesheet
  }
}
