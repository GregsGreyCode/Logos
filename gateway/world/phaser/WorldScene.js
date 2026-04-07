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
    this._hueOffset = (time / 1000) * 6; // 6 deg/sec = 360 in 60s
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

    // Render tiles
    const gfx = this.add.graphics();
    for (let r = 0; r < WORLD_ROWS; r++) {
      for (let c = 0; c < WORLD_COLS; c++) {
        const color = TILE_COLORS[grid[r][c]] || 0x4a7c3f;
        gfx.fillStyle(color, 1);
        gfx.fillRect(c * TILE_SIZE, r * TILE_SIZE, TILE_SIZE, TILE_SIZE);
        // Subtle grid lines
        gfx.lineStyle(0.5, 0x000000, 0.05);
        gfx.strokeRect(c * TILE_SIZE, r * TILE_SIZE, TILE_SIZE, TILE_SIZE);
      }
    }
    gfx.setDepth(0);

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

    for (let i = 0; i < this.canopyPixels.length; i++) {
      const p = this.canopyPixels[i];
      const hue = (this._hueOffset + i * 8) % 360;
      const sat = 55 + p.brightness * 20;
      const lit = 22 + p.brightness * 22;
      const color = Phaser.Display.Color.HSLToColor(hue / 360, sat / 100, lit / 100);
      gfx.fillStyle(color.color, 0.95);
      gfx.fillCircle(p.x, p.y, TILE_SIZE * 0.6);
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
