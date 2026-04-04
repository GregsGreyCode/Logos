/**
 * AgentCharacter — animated sprite representation of an agent in the world.
 *
 * Each agent is assigned a character from the 32x32folk spritesheet (8 variants)
 * and tinted with a unique color derived from their instance name.  Characters
 * have idle/walk states with directional animation and smooth movement.
 */
import {
  TILE_SIZE, WORLD_COLS, WORLD_ROWS, ZONES, AGENT_LABEL_STYLE,
} from './WorldConfig.js';
import { CHARACTER_SHEETS, CHARACTER_TEXTURE } from './SpriteData.js';

// ── Color generation ───────────────────────────────────────────────────

function hashCode(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  return hash;
}

/** Deterministic vibrant color from a string. */
function hashColor(str) {
  const h = Math.abs(hashCode(str)) % 360;
  const s = 0.65, l = 0.6;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = l - c / 2;
  let r, g, b;
  if (h < 60)       { r = c; g = x; b = 0; }
  else if (h < 120) { r = x; g = c; b = 0; }
  else if (h < 180) { r = 0; g = c; b = x; }
  else if (h < 240) { r = 0; g = x; b = c; }
  else if (h < 300) { r = x; g = 0; b = c; }
  else              { r = c; g = 0; b = x; }
  const toHex = (v) => Math.round((v + m) * 255);
  return (toHex(r) << 16) | (toHex(g) << 8) | toHex(b);
}

// ── Position assignment ────────────────────────────────────────────────

function positionForAgent(inst, index, total) {
  const status = inst.status || inst.k8s_status || 'unknown';
  // Running agents gather in the clearing, others in the meadow
  const zone = status === 'running'
    ? (ZONES.clearing || ZONES.plaza)
    : (ZONES.meadow || ZONES.workshop);

  const cols = Math.max(Math.ceil(Math.sqrt(total || 1)), 2);
  const row = Math.floor(index / cols);
  const col = index % cols;
  const spacing = TILE_SIZE * 2;

  return {
    x: (zone.x + 1) * TILE_SIZE + col * spacing,
    y: (zone.y + 1) * TILE_SIZE + row * spacing,
  };
}

// ── Spritesheet cache ──────────────────────────────────────────────────

const _sheetCache = new Map();

async function getSheet(PIXI, charIndex) {
  if (_sheetCache.has(charIndex)) return _sheetCache.get(charIndex);

  const data = CHARACTER_SHEETS[charIndex];
  const texture = PIXI.BaseTexture.from(CHARACTER_TEXTURE, {
    scaleMode: PIXI.SCALE_MODES.NEAREST,
  });
  const sheet = new PIXI.Spritesheet(texture, data);
  await sheet.parse();
  _sheetCache.set(charIndex, sheet);
  return sheet;
}

// ── Create character ───────────────────────────────────────────────────

/**
 * Create an animated AgentCharacter container.
 * Returns a PIXI.Container with the sprite, name label, and state bubble.
 */
export async function createAgentCharacter(PIXI, inst, index, total) {
  const container = new PIXI.Container();
  container.interactive = true;
  container.cursor = 'pointer';
  container._agentName = inst.name;
  container._direction = 'down';
  container._isMoving = false;
  container._idleTimer = Math.random() * 200; // stagger idle wandering

  // Pick a character variant from the sheet (deterministic from name)
  const charIndex = Math.abs(hashCode(inst.name)) % CHARACTER_SHEETS.length;
  const color = hashColor(inst.name);
  const status = inst.status || inst.k8s_status || 'unknown';
  const isRunning = status === 'running';

  // Load spritesheet
  const sheet = await getSheet(PIXI, charIndex);

  // Animated sprite — start facing down
  const sprite = new PIXI.AnimatedSprite(sheet.animations['down']);
  sprite.animationSpeed = 0.08;
  sprite.anchor.set(0.5, 0.7);
  sprite.tint = color;
  if (!isRunning) sprite.alpha = 0.5;
  container.addChild(sprite);
  container._sprite = sprite;
  container._sheet = sheet;

  // Play idle frame
  sprite.gotoAndStop(0);

  // Status indicator
  const statusDot = new PIXI.Graphics();
  statusDot.beginFill(isRunning ? 0x22c55e : 0xeab308);
  statusDot.drawCircle(0, 0, 3);
  statusDot.endFill();
  statusDot.x = 12;
  statusDot.y = -22;
  container.addChild(statusDot);
  container._statusDot = statusDot;

  // Name label
  const displayName = inst.instance_label || inst.instance_name || inst.name;
  const shortName = displayName.length > 14 ? displayName.slice(0, 13) + '\u2026' : displayName;
  const label = new PIXI.Text(shortName, {
    fontFamily: 'monospace',
    fontSize: 7,
    fill: 0xffffff,
    align: 'center',
    dropShadow: true,
    dropShadowColor: 0x000000,
    dropShadowDistance: 1,
    dropShadowAlpha: 0.8,
  });
  label.anchor.set(0.5, 0);
  label.y = 10;
  container.addChild(label);

  // Soul chip
  if (inst.soul?.name) {
    const soulLabel = new PIXI.Text(inst.soul.name, {
      fontFamily: 'monospace',
      fontSize: 6,
      fill: 0x818cf8,
      dropShadow: true,
      dropShadowColor: 0x000000,
      dropShadowDistance: 1,
      dropShadowAlpha: 0.8,
    });
    soulLabel.anchor.set(0.5, 0);
    soulLabel.y = 19;
    soulLabel.alpha = 0.7;
    container.addChild(soulLabel);
  }

  // State bubble (thinking/speaking) — starts hidden
  const bubble = new PIXI.Text('', {
    fontSize: 14,
  });
  bubble.anchor.set(0.5, 1);
  bubble.y = -26;
  bubble.visible = false;
  container.addChild(bubble);
  container._bubble = bubble;

  // Position
  const pos = positionForAgent(inst, index, total);
  container.x = pos.x;
  container.y = pos.y;
  container._targetX = pos.x;
  container._targetY = pos.y;

  // Hit area
  container.hitArea = new PIXI.Rectangle(-16, -24, 32, 48);

  return container;
}

// ── Update character ───────────────────────────────────────────────────

/**
 * Update an existing agent character each tick.
 * Handles smooth movement, direction changes, and idle wandering.
 */
export function updateAgentCharacter(container, inst, index, total) {
  const pos = positionForAgent(inst, index, total);
  container._targetX = pos.x;
  container._targetY = pos.y;

  // Smooth movement towards target
  const dx = container._targetX - container.x;
  const dy = container._targetY - container.y;
  const dist = Math.sqrt(dx * dx + dy * dy);

  if (dist > 1) {
    // Moving
    const speed = 0.08;
    container.x += dx * speed;
    container.y += dy * speed;

    // Determine direction
    let dir;
    if (Math.abs(dx) > Math.abs(dy)) {
      dir = dx > 0 ? 'right' : 'left';
    } else {
      dir = dy > 0 ? 'down' : 'up';
    }

    if (dir !== container._direction || !container._isMoving) {
      container._direction = dir;
      container._isMoving = true;
      const sprite = container._sprite;
      if (sprite && container._sheet) {
        sprite.textures = container._sheet.animations[dir];
        sprite.play();
      }
    }
  } else {
    // Arrived — idle
    if (container._isMoving) {
      container._isMoving = false;
      const sprite = container._sprite;
      if (sprite) sprite.gotoAndStop(0);
    }

    // Idle wandering — small random movements, avoid tree center
    container._idleTimer = (container._idleTimer || 0) + 1;
    if (container._idleTimer > 180 + Math.random() * 120) {
      container._idleTimer = 0;
      const wobble = TILE_SIZE * 1.5;
      let tx = pos.x + (Math.random() - 0.5) * wobble;
      let ty = pos.y + (Math.random() - 0.5) * wobble;
      // Avoid tree center (world center, ~2 tile radius)
      const treeCx = Math.floor(WORLD_COLS / 2) * TILE_SIZE + TILE_SIZE / 2;
      const treeCy = Math.floor(WORLD_ROWS / 2) * TILE_SIZE + TILE_SIZE / 2;
      const treeDist = Math.sqrt((tx - treeCx) ** 2 + (ty - treeCy) ** 2);
      if (treeDist < TILE_SIZE * 2.5) {
        // Push away from tree
        const angle = Math.atan2(ty - treeCy, tx - treeCx);
        tx = treeCx + Math.cos(angle) * TILE_SIZE * 3;
        ty = treeCy + Math.sin(angle) * TILE_SIZE * 3;
      }
      container._targetX = tx;
      container._targetY = ty;
    }
  }

  // Update status dot
  const status = inst.status || inst.k8s_status || 'unknown';
  const isRunning = status === 'running';
  const dot = container._statusDot;
  if (dot) {
    dot.clear();
    dot.beginFill(isRunning ? 0x22c55e : 0xeab308);
    dot.drawCircle(0, 0, 3);
    dot.endFill();
  }

  // Bubble for status
  const bubble = container._bubble;
  if (bubble) {
    if (!isRunning && status !== 'unknown') {
      bubble.text = '\u23f3'; // hourglass
      bubble.visible = true;
    } else {
      bubble.visible = false;
    }
  }
}
