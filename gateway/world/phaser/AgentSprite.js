/**
 * AgentSprite — a Phaser-based agent entity in the world.
 *
 * Owns: animated sprite, name label, soul label, status dot, state bubble.
 * Handles: pathfinding movement, idle wandering, zone assignment, interactions.
 */
import { TILE_SIZE, WORLD_COLS, WORLD_ROWS, ZONES } from '../WorldConfig.js';

const WALK_SPEED = 60;  // pixels per second
const IDLE_WANDER_MIN = 3000; // ms
const IDLE_WANDER_MAX = 6000; // ms

// Deterministic color from name
function hashCode(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) - h + str.charCodeAt(i)) | 0;
  }
  return h;
}

function nameToCharIndex(name) {
  return Math.abs(hashCode(name)) % 8;
}

function nameToTint(name) {
  const hue = Math.abs(hashCode(name)) % 360;
  const color = Phaser.Display.Color.HSLToColor(hue / 360, 0.65, 0.6);
  return color.color;
}

export class AgentSprite {
  constructor(scene, inst, index, total, callbacks) {
    this.scene = scene;
    this.inst = inst;
    this.index = index;
    this.total = total;
    this.callbacks = callbacks;

    this.charIndex = nameToCharIndex(inst.name);
    this.direction = 'down';
    this.isMoving = false;
    this.path = [];          // current A* path [{col, row}, ...]
    this.pathIndex = 0;
    this.targetWorldX = 0;
    this.targetWorldY = 0;
    this.idleTimer = 0;
    this.nextIdleTime = this._randomIdleDelay();
    this.lastStatus = null;

    // Create sprite
    const startPos = this._zonePosition(inst, index, total);
    const texKey = 'characters';
    const idleFrame = scene.getCharIdleFrame(this.charIndex, 'down');
    this.sprite = scene.add.sprite(startPos.x, startPos.y, texKey, idleFrame);
    this.sprite.setOrigin(0.5, 0.7);
    this.sprite.setScale(2);  // 2x size for visibility
    this.sprite.setDepth(10);
    // No tint — use the character's natural pixel art colors.
    // Each of the 8 characters has distinct hair, outfit, and skin.

    // Alpha based on status
    const isRunning = this._isRunning(inst);
    this.sprite.setAlpha(isRunning ? 1 : 0.6);

    // Create walk animations for this character
    this._createAnimations();

    // Interactive
    this.sprite.setInteractive({ useHandCursor: true });
    this.sprite.on('pointerdown', () => {
      this.sprite._agentDrag = true;
      if (this.callbacks.onClick) this.callbacks.onClick(inst.name, inst);
    });
    this.sprite.on('pointerup', () => { this.sprite._agentDrag = false; });
    this._hovered = false;
    this.sprite.on('pointerover', () => {
      this._hovered = true;
      this.sprite.setScale(2.3);
      if (this.callbacks.onHover) this.callbacks.onHover(inst.name, inst, true);
    });
    this.sprite.on('pointerout', () => {
      this._hovered = false;
      this.sprite.setScale(2);
      if (this.callbacks.onHover) this.callbacks.onHover(inst.name, inst, false);
    });

    // Logo badge below sprite — agent's initial on a colored background
    const tint = nameToTint(inst.name);
    const tintHex = '#' + tint.toString(16).padStart(6, '0');
    const initial = (inst.name || '?')[0].toUpperCase();
    this.logoText = scene.add.text(startPos.x, startPos.y + 22, initial, {
      fontFamily: 'monospace',
      fontSize: '12px',
      fontStyle: 'bold',
      color: '#ffffff',
      align: 'center',
      backgroundColor: tintHex,
      padding: { x: 4, y: 2 },
      fixedWidth: 16,
      fixedHeight: 16,
    }).setOrigin(0.5, 0.5).setDepth(13);
    this.logoBg = null;

    // No name or soul labels — the badge below the sprite is the only identifier.
    this.nameLabel = null;
    this.soulLabel = null;

    // Status dot
    this.statusDot = scene.add.graphics().setDepth(14);
    this._drawStatusDot(isRunning);

    // State bubble (hourglass when not running)
    this.bubble = scene.add.text(startPos.x, startPos.y - 50, '\u23f3', {
      fontSize: '16px',
      align: 'center',
    }).setOrigin(0.5, 0.5).setDepth(14);
    this.bubble.setVisible(!isRunning && this._getStatus(inst) !== 'unknown');

    // Initial target
    this.targetWorldX = startPos.x;
    this.targetWorldY = startPos.y;
  }

  _createAnimations() {
    const texKey = 'characters';
    const dirs = ['down', 'left', 'right', 'up'];
    for (const dir of dirs) {
      const key = `char${this.charIndex}_walk_${dir}`;
      if (this.scene.anims.exists(key)) continue;
      const frameIndices = this.scene.getCharFrames(this.charIndex, dir);
      this.scene.anims.create({
        key,
        frames: frameIndices.map(f => ({ key: texKey, frame: f })),
        frameRate: 8,
        repeat: -1,
      });
    }
  }

  syncState(inst, index, total) {
    this.inst = inst;
    this.index = index;
    this.total = total;

    const status = this._getStatus(inst);
    const isRunning = status === 'running';

    // Alpha
    this.sprite.setAlpha(isRunning ? 1 : 0.6);

    // Status dot
    this._drawStatusDot(isRunning);

    // Bubble
    this.bubble.setVisible(!isRunning && status !== 'unknown');

    // If status changed, move to new zone
    if (status !== this.lastStatus) {
      this.lastStatus = status;
      const target = this._zonePosition(inst, index, total);
      this._navigateTo(target.x, target.y);
    }

    // No labels to update — only the logo badge identifies the agent.
  }

  update(time, delta) {
    if (this.path.length > 0 && this.pathIndex < this.path.length) {
      this._followPath(delta);
    } else {
      // Arrived or idle
      if (this.isMoving) {
        this.isMoving = false;
        // Stop any running animation and reset to neutral standing pose
        if (this.sprite.anims.isPlaying) this.sprite.anims.stop();
        this.direction = 'down';
        const idleIdx = this.scene.getCharIdleFrame(this.charIndex, 'down');
        this.sprite.setTexture('characters', idleIdx);
      }

      // Idle wandering
      this.idleTimer += delta;
      if (this.idleTimer >= this.nextIdleTime) {
        this.idleTimer = 0;
        this.nextIdleTime = this._randomIdleDelay();
        this._idleWander();
      }
    }

    // Sync logo badge + status positions
    this.statusDot.setPosition(this.sprite.x + 16, this.sprite.y - 14);
    this.bubble.setPosition(this.sprite.x, this.sprite.y - 26);
    this.logoText.setPosition(this.sprite.x, this.sprite.y + 22);
  }

  _followPath(delta) {
    const target = this.path[this.pathIndex];
    // Clamp target tile to world bounds so a stale or out-of-range
    // path entry can never walk the sprite off the visible map.
    const tCol = Math.max(1, Math.min(WORLD_COLS - 2, target.col));
    const tRow = Math.max(1, Math.min(WORLD_ROWS - 2, target.row));
    const tx = tCol * TILE_SIZE + TILE_SIZE / 2;
    const ty = tRow * TILE_SIZE + TILE_SIZE / 2;

    const dx = tx - this.sprite.x;
    const dy = ty - this.sprite.y;
    const dist = Math.sqrt(dx * dx + dy * dy);

    if (dist < 2) {
      this.sprite.setPosition(tx, ty);
      this.pathIndex++;
      return;
    }

    // Move toward target tile
    const step = (WALK_SPEED * delta) / 1000;
    const ratio = Math.min(step / dist, 1);
    this.sprite.x += dx * ratio;
    this.sprite.y += dy * ratio;

    // Hard-clamp the sprite position itself as a final safety net
    const minX = TILE_SIZE;
    const maxX = (WORLD_COLS - 1) * TILE_SIZE;
    const minY = TILE_SIZE;
    const maxY = (WORLD_ROWS - 1) * TILE_SIZE;
    this.sprite.x = Math.max(minX, Math.min(maxX, this.sprite.x));
    this.sprite.y = Math.max(minY, Math.min(maxY, this.sprite.y));

    // Direction + animation
    const newDir = this._directionFromDelta(dx, dy);
    if (newDir !== this.direction || !this.isMoving) {
      this.direction = newDir;
      this.isMoving = true;
      this.sprite.play(`char${this.charIndex}_walk_${newDir}`, true);
    }
  }

  _navigateTo(worldX, worldY) {
    const fromCol = Math.floor(this.sprite.x / TILE_SIZE);
    const fromRow = Math.floor(this.sprite.y / TILE_SIZE);
    const toCol = Math.floor(worldX / TILE_SIZE);
    const toRow = Math.floor(worldY / TILE_SIZE);

    if (this.scene.pathfinder) {
      const path = this.scene.pathfinder.findPath(fromCol, fromRow, toCol, toRow);
      if (path && path.length > 1) {
        this.path = path;
        this.pathIndex = 1; // skip current tile
        this.idleTimer = 0;
        return;
      }
    }

    // Fallback: direct lerp to target (no pathfinding)
    this.path = [{ col: toCol, row: toRow }];
    this.pathIndex = 0;
  }

  _idleWander() {
    const currentCol = Math.floor(this.sprite.x / TILE_SIZE);
    const currentRow = Math.floor(this.sprite.y / TILE_SIZE);

    // Wander 2-4 tiles in a random direction
    const range = 2 + Math.floor(Math.random() * 3);
    const angle = Math.random() * Math.PI * 2;
    let targetCol = Math.round(currentCol + Math.cos(angle) * range);
    let targetRow = Math.round(currentRow + Math.sin(angle) * range);

    // Clamp
    targetCol = Math.max(1, Math.min(WORLD_COLS - 2, targetCol));
    targetRow = Math.max(1, Math.min(WORLD_ROWS - 2, targetRow));

    // Avoid tree canopy area (radius 7 tiles from center)
    const tcx = this.scene.treeCenter.x;
    const tcy = this.scene.treeCenter.y;
    const treeDist = Math.sqrt((targetCol - tcx) ** 2 + (targetRow - tcy) ** 2);
    if (treeDist < 7) {
      const a = Math.atan2(targetRow - tcy, targetCol - tcx);
      targetCol = Math.round(tcx + Math.cos(a) * 8);
      targetRow = Math.round(tcy + Math.sin(a) * 8);
    }

    this._navigateTo(targetCol * TILE_SIZE + TILE_SIZE / 2, targetRow * TILE_SIZE + TILE_SIZE / 2);
  }

  _zonePosition(inst, index, total) {
    const status = this._getStatus(inst);
    // Running agents go to the grove (top-right, away from tree)
    // Idle agents go to the meadow (top-left)
    const zone = status === 'running' ? ZONES.grove : ZONES.meadow;
    const cols = Math.max(Math.ceil(Math.sqrt(total || 1)), 2);
    const row = Math.floor(index / cols);
    const col = index % cols;
    const spacing = TILE_SIZE * 2;

    return {
      x: (zone.x + 1) * TILE_SIZE + col * spacing + (Math.random() - 0.5) * TILE_SIZE,
      y: (zone.y + 1) * TILE_SIZE + row * spacing + (Math.random() - 0.5) * TILE_SIZE,
    };
  }

  _directionFromDelta(dx, dy) {
    if (Math.abs(dx) > Math.abs(dy)) {
      return dx > 0 ? 'right' : 'left';
    }
    return dy > 0 ? 'down' : 'up';
  }

  _getStatus(inst) {
    return inst.status || inst.k8s_status || 'unknown';
  }

  _isRunning(inst) {
    return this._getStatus(inst) === 'running';
  }

  _drawStatusDot(isRunning) {
    this.statusDot.clear();
    this.statusDot.fillStyle(isRunning ? 0x22c55e : 0xeab308, 1);
    this.statusDot.fillCircle(0, 0, 3);
  }

  _randomIdleDelay() {
    return IDLE_WANDER_MIN + Math.random() * (IDLE_WANDER_MAX - IDLE_WANDER_MIN);
  }

  destroy() {
    this.sprite.destroy();
    if (this.nameLabel) this.nameLabel.destroy();
    if (this.soulLabel) this.soulLabel.destroy();
    this.statusDot.destroy();
    this.bubble.destroy();
    if (this.logoBg) this.logoBg.destroy();
    this.logoText.destroy();
  }
}

// Re-export the deterministic char-index hash so the agent card sprite
// preview matches the in-world sprite without divergence.
export function nameToCharIndexExport(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = ((h << 5) - h + name.charCodeAt(i)) | 0;
  return Math.abs(h) % 8;
}
