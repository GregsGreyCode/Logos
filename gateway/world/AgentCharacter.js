/**
 * AgentCharacter — visual representation of an agent instance in the world.
 *
 * Phase 1: colored circle + name label, positioned by state.
 * Phase 2: animated spritesheet character with walk cycle.
 */
import {
  TILE_SIZE, ZONES, AGENT_RADIUS, AGENT_LABEL_STYLE,
} from './WorldConfig.js';

/**
 * Generate a deterministic color from a string (instance name).
 */
function hashColor(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  const h = Math.abs(hash) % 360;
  // HSL to RGB (s=70%, l=55% for vivid but not neon)
  const s = 0.7, l = 0.55;
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

/**
 * Determine world position (in pixels) from agent state.
 * Returns {x, y} in world coordinates.
 */
function positionForAgent(inst, index, total) {
  const status = inst.status || inst.k8s_status || 'unknown';
  let zone;

  if (status === 'running') {
    // Spread running agents around the plaza
    zone = ZONES.plaza;
  } else {
    // Starting/pending agents go to workshop
    zone = ZONES.workshop;
  }

  // Distribute agents within the zone in a grid pattern
  const cols = Math.ceil(Math.sqrt(total || 1));
  const row = Math.floor(index / cols);
  const col = index % cols;
  const spacing = TILE_SIZE * 1.2;

  return {
    x: (zone.x + 0.5) * TILE_SIZE + col * spacing + TILE_SIZE / 2,
    y: (zone.y + 0.5) * TILE_SIZE + row * spacing + TILE_SIZE / 2,
  };
}

/**
 * Create an AgentCharacter — a PIXI.Container with circle + label.
 */
export function createAgentCharacter(PIXI, inst, index, total) {
  const container = new PIXI.Container();
  container.interactive = true;
  container.cursor = 'pointer';
  container._agentName = inst.name;

  const color = hashColor(inst.name);
  const status = inst.status || inst.k8s_status || 'unknown';
  const isRunning = status === 'running';

  // Circle body
  const body = new PIXI.Graphics();
  body.beginFill(color);
  body.drawCircle(0, 0, AGENT_RADIUS);
  body.endFill();

  // Outline
  body.lineStyle(1.5, 0xffffff, isRunning ? 0.8 : 0.3);
  body.drawCircle(0, 0, AGENT_RADIUS);

  // Pulse effect for non-running agents
  if (!isRunning) {
    body.alpha = 0.6;
  }

  container.addChild(body);

  // Status indicator dot
  const statusDot = new PIXI.Graphics();
  statusDot.beginFill(isRunning ? 0x22c55e : 0xeab308);
  statusDot.drawCircle(0, 0, 2);
  statusDot.endFill();
  statusDot.x = AGENT_RADIUS - 1;
  statusDot.y = -AGENT_RADIUS + 1;
  container.addChild(statusDot);

  // Name label
  const displayName = inst.instance_label || inst.instance_name || inst.name;
  const shortName = displayName.length > 12 ? displayName.slice(0, 11) + '\u2026' : displayName;
  const label = new PIXI.Text(shortName, {
    ...AGENT_LABEL_STYLE,
    fill: 0xffffff,
  });
  label.anchor.set(0.5, 0);
  label.y = AGENT_RADIUS + 2;
  container.addChild(label);

  // Soul chip (tiny text below name)
  if (inst.soul?.name) {
    const soulLabel = new PIXI.Text(inst.soul.name, {
      fontFamily: 'monospace',
      fontSize: 6,
      fill: 0x818cf8,
    });
    soulLabel.anchor.set(0.5, 0);
    soulLabel.y = AGENT_RADIUS + 12;
    soulLabel.alpha = 0.7;
    container.addChild(soulLabel);
  }

  // Position in world
  const pos = positionForAgent(inst, index, total);
  container.x = pos.x;
  container.y = pos.y;

  // Hit area (larger than visual for easy clicking)
  container.hitArea = new PIXI.Circle(0, 0, AGENT_RADIUS + 8);

  return container;
}

/**
 * Update an existing agent character's position and state.
 */
export function updateAgentCharacter(container, inst, index, total) {
  const pos = positionForAgent(inst, index, total);
  // Smooth interpolation (lerp) towards target position
  container.x += (pos.x - container.x) * 0.1;
  container.y += (pos.y - container.y) * 0.1;
}
