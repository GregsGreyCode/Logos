/**
 * AgentSprite — a Phaser-based agent entity in the world.
 *
 * Owns: animated sprite, name label, soul label, status dot, state bubble.
 * Handles: pathfinding movement, idle wandering, zone assignment, interactions.
 */
import { TILE_SIZE, WORLD_COLS, WORLD_ROWS, ZONES } from './WorldConfig.js?v=12';

const WALK_SPEED = 60;  // pixels per second
const IDLE_WANDER_MIN = 3000; // ms — how long to stand still between walk phases
const IDLE_WANDER_MAX = 6000; // ms
const MIN_WALK_DURATION = 10000; // ms — once an agent starts walking, keep
                                  // walking for at least this long (chain paths
                                  // together if needed) so walks don't look
                                  // like twitchy 1-tile hops.

// Deterministic color from name
function hashCode(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) - h + str.charCodeAt(i)) | 0;
  }
  return h;
}

const CHARACTER_COUNT = 160;

function nameToCharIndex(name) {
  return Math.abs(hashCode(name)) % CHARACTER_COUNT;
}

export class AgentSprite {
  constructor(scene, inst, index, total, callbacks) {
    this.scene = scene;
    this.inst = inst;
    this.index = index;
    this.total = total;
    this.callbacks = callbacks;

    // Prefer an explicit char_index stored on the agent record (set via the
    // sprite picker in Create Agent / setup wizard). Fall back to the
    // name-hash so agents that predate the picker still render deterministically.
    const explicitCI = (inst && inst.char_index !== undefined && inst.char_index !== null)
      ? Number(inst.char_index)
      : null;
    this.charIndex = (explicitCI !== null && explicitCI >= 0 && explicitCI < CHARACTER_COUNT)
      ? explicitCI
      : nameToCharIndex(inst.name);
    this.direction = 'down';
    this.isMoving = false;
    this._fading = false;
    this.path = [];          // current A* path [{col, row}, ...]
    this.pathIndex = 0;
    this.targetWorldX = 0;
    this.targetWorldY = 0;
    // First movement should happen almost immediately after spawn so a
    // page refresh doesn't leave the world frozen for 3-6 seconds. We
    // seed ``idleTimer`` close to the random threshold — within
    // ~0-500ms of the target — so the next ``update()`` tick picks a
    // wander destination. Only *subsequent* idles fall back to the
    // full 3-6s range so between-walk pauses still feel lazy rather
    // than robotic. Without this seeding the world looks broken on
    // refresh: sprites are visible but stand motionless until the
    // first random timer elapses.
    this.nextIdleTime = this._randomIdleDelay();
    this.idleTimer = Math.max(0, this.nextIdleTime - 500 * Math.random());
    this.walkPhaseStart = 0;  // ms (Date.now) when the current walk phase began
    // Seed lastStatus to the current status so the first syncState() call
    // does NOT trip the "status changed" branch and snap the freshly-spawned
    // agent into a zone tile — that defeats the persisted/random spawn.
    this.lastStatus = this._getStatus(inst);

    // Pick a spawn position. Priority order:
    //   1. Persisted position from localStorage (so a page refresh leaves
    //      the agent exactly where it was standing)
    //   2. Random walkable tile (first-time visit / new agent / cleared
    //      browser storage)
    //   3. Zone-based fallback (only if the scene helpers are missing)
    const stored = scene.getStoredPosition ? scene.getStoredPosition(inst.name) : null;
    const startPos = stored
      || (scene.getRandomWalkablePosition ? scene.getRandomWalkablePosition() : this._zonePosition(inst, index, total));
    if (stored && stored.dir) this.direction = stored.dir;
    const texKey = 'characters';
    const idleFrame = scene.getCharIdleFrame(this.charIndex, 'down');
    this.sprite = scene.add.sprite(startPos.x, startPos.y, texKey, idleFrame);
    this.sprite.setOrigin(0.5, 0.7);
    this.sprite.setScale(2);  // 2x size for visibility
    this.sprite.setDepth(10);
    // No baseline tint — each of the 8 characters has distinct hair,
    // outfit, and skin colors that we want visible in normal lighting.
    // The update loop applies a dynamic indigo-purple tint when the
    // sprite walks within range of the tree canopy glow (see
    // _applyTreeGlowTint), which is the only sanctioned tint source.

    // Alpha + grey tint based on status. Starting/disconnected agents
    // render at 0.6 alpha AND with a desaturated grey tint so the user
    // can immediately tell which agents are still spinning up vs which
    // are alive and ready. The update loop maintains this state and
    // clears the tint when the agent transitions to running.
    const isRunning = this._isRunning(inst);
    this.sprite.setAlpha(isRunning ? 1 : 0.6);
    if (!isRunning) {
      this.sprite.setTint(0x6b6b6b);
      this._greyTinted = true;
    } else {
      this._greyTinted = false;
    }

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

    // Header strip above the sprite is now a row of three state
    // indicators at y - 52 instead of a letter badge:
    //   [tier glyph]  [status dot]  [state bubble]
    // Letter badge removed — agents are identified by their sprite +
    // colour, not a letter over their head. Status dot takes the
    // centre (where the badge used to live); the thinking/hourglass
    // bubble slides over to where the dot used to be (right side).
    // Tier glyph stays on the left.
    this.logoText = null;
    this.logoBg = null;

    this.tierGlyph = scene.add.text(startPos.x - 18, startPos.y - 52, inst.tier_glyph || '', {
      fontSize: '14px',
      align: 'center',
    }).setOrigin(0.5, 0.5).setDepth(14);
    if (!inst.tier_glyph) this.tierGlyph.setVisible(false);

    // No name or soul labels — the badge above the sprite is the only identifier.
    this.nameLabel = null;
    this.soulLabel = null;

    // Status dot
    this.statusDot = scene.add.graphics().setDepth(14);
    this._drawStatusDot(isRunning);

    // State bubble — three glyphs, mutually exclusive:
    //   ⏳  (hourglass)        — not running (provisioning / disconnected)
    //   💭  (thought bubble)   — running AND at least one task in flight
    //   (hidden)               — running and idle
    //
    // ``inst.active_tasks`` is computed in main_app._worldAgentList from
    // status.active_sessions (the same source the agent card's
    // "thinking…" row reads), so the bubble and card flip in lockstep.
    this.bubble = scene.add.text(startPos.x + 18, startPos.y - 52, '\u23f3', {
      fontSize: '16px',
      align: 'center',
    }).setOrigin(0.5, 0.5).setDepth(14);
    this._updateBubble(inst);

    // Tween handle for the busy-state bob animation — created on demand
    // the first time the agent enters the busy state, paused/resumed as
    // the state toggles. Phaser disallows multiple simultaneous tweens
    // on the same target so we track the single tween here.
    this._busyTween = null;

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

    // Bubble (hourglass / thought bubble / hidden — see _updateBubble)
    this._updateBubble(inst);

    // Status transition handling. The freeze + walk animation reset
    // happens in update() based on isRunning, so we don't need to
    // re-trigger anything here. The zone snap from the old code has
    // been removed — agents stay where they are when their status
    // changes; the new design lets the user observe the worker
    // connect in place rather than warping the sprite around.
    if (status !== this.lastStatus) {
      this.lastStatus = status;
    }

    // Refresh the maturity glyph if the underlying agent's tier moved
    // (a new dispatch nudged Sapling → Branch). Cheap string compare.
    if (this.tierGlyph) {
      const newGlyph = inst.tier_glyph || '';
      if (newGlyph !== this.tierGlyph.text) {
        this.tierGlyph.setText(newGlyph);
        this.tierGlyph.setVisible(!!newGlyph);
      }
    }

    // No labels to update — only the logo badge identifies the agent.
  }

  update(time, delta) {
    if (this._fading) return;
    const isRunning = this._isRunning(this.inst);

    // Provisioning / disconnected agents are visibly grey AND frozen
    // in place — no wandering, no walk animation, no tree glow tint.
    // The freeze stops the moment the worker_registry flips
    // worker_connected → true (status flows in via syncState).
    if (!isRunning) {
      // Stop any in-progress walk so we don't get stuck mid-stride.
      if (this.isMoving) {
        if (this.sprite.anims.isPlaying) this.sprite.anims.stop();
        const idleIdx = this.scene.getCharIdleFrame(this.charIndex, this.direction);
        this.sprite.setTexture('characters', idleIdx);
        this.isMoving = false;
      }
      // Clear any in-progress path/wander state so the agent doesn't
      // resume walking the moment status flips back to running.
      this.path = [];
      this.pathIndex = 0;
      this.idleTimer = 0;
      this.walkPhaseStart = 0;
      // Apply the desaturated grey tint. Cheaply guarded by a dirty
      // bit so we don't hammer setTint every frame on stationary
      // provisioning agents.
      if (!this._greyTinted) {
        this.sprite.setTint(0x6b6b6b);
        this._greyTinted = true;
        this._lastGlowT = -1; // force tree-glow to re-apply when running again
      }
      // Sync the header row (tier glyph · status dot · state bubble)
      // to the sprite's current position. All three share y - 52; the
      // status dot now owns the centre and the bubble sits to its right.
      this.statusDot.setPosition(this.sprite.x, this.sprite.y - 52);
      this.bubble.setPosition(this.sprite.x + 18, this.sprite.y - 52);
      if (this.tierGlyph) this.tierGlyph.setPosition(this.sprite.x - 18, this.sprite.y - 52);
      // Still record position for persistence (cheap, in-memory).
      if (this.scene.recordPosition) {
        this.scene.recordPosition(this.inst.name, this.sprite.x, this.sprite.y, this.direction);
      }
      return;
    }

    // Running path — clear the grey tint so the tree-glow tint can
    // take over. Only happens once at the running transition.
    if (this._greyTinted) {
      this.sprite.clearTint();
      this._greyTinted = false;
    }

    if (this.path.length > 0 && this.pathIndex < this.path.length) {
      this._followPath(delta);
    } else {
      // Path finished. Stop the walk animation IMMEDIATELY so we never
      // show a walk-in-place pose. The Phaser animation auto-loops once
      // play() is called, so without an explicit stop() the cycle keeps
      // running even when _followPath returns early on a near-zero
      // movement step. Resetting isMoving here also lets the next
      // _followPath tick (after a chained path is generated below) hit
      // the `!this.isMoving` branch in _followPath and re-trigger play().
      if (this.isMoving) {
        if (this.sprite.anims.isPlaying) this.sprite.anims.stop();
        const idleIdx = this.scene.getCharIdleFrame(this.charIndex, this.direction);
        this.sprite.setTexture('characters', idleIdx);
        this.isMoving = false;
      }

      // If we're still inside the minimum-walk window, immediately
      // generate another path so the agent keeps strolling instead of
      // stopping after 1-2 tiles. The next update tick will pick the
      // new path up via _followPath and resume the walk animation.
      if (this.walkPhaseStart > 0) {
        const walkedFor = Date.now() - this.walkPhaseStart;
        if (walkedFor < MIN_WALK_DURATION) {
          this._idleWander();  // chain another path, keep walking
          return;
        }
        this.walkPhaseStart = 0;
      }

      // Idle wandering — after the standing-still timer elapses, start a
      // new walk phase and stamp walkPhaseStart so the chain logic above
      // knows when this phase began.
      this.idleTimer += delta;
      if (this.idleTimer >= this.nextIdleTime) {
        this.idleTimer = 0;
        this.nextIdleTime = this._randomIdleDelay();
        this.walkPhaseStart = Date.now();
        this._idleWander();
      }
    }

    // Header-row sync: [tier glyph] [status dot] [state bubble], all at
    // y - 52 above the sprite's head. Dot owns the centre now that the
    // letter badge is gone; bubble lives to the right where the dot used
    // to be; tier glyph stays on the left.
    this.statusDot.setPosition(this.sprite.x, this.sprite.y - 52);
    this.bubble.setPosition(this.sprite.x + 18, this.sprite.y - 52);
    if (this.tierGlyph) this.tierGlyph.setPosition(this.sprite.x - 18, this.sprite.y - 52);

    // Apply the tree-proximity glow tint. Cheap (~50 lines of math)
    // and self-throttled — only re-applies when the tint actually
    // changed by a meaningful amount.
    this._applyTreeGlowTint();

    // Record current position for persistence. Cheap in-memory write — the
    // scene throttles the actual localStorage flush to once a second.
    if (this.scene.recordPosition) {
      this.scene.recordPosition(this.inst.name, this.sprite.x, this.sprite.y, this.direction);
    }
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

    // Clamp to world bounds. The tree canopy is no longer avoided —
    // agents are allowed to wander under the leaves; the canopy
    // (depth 100) naturally renders above the agent sprite (depth 10)
    // so it visually reads as walking beneath foliage.
    targetCol = Math.max(1, Math.min(WORLD_COLS - 2, targetCol));
    targetRow = Math.max(1, Math.min(WORLD_ROWS - 2, targetRow));

    this._navigateTo(targetCol * TILE_SIZE + TILE_SIZE / 2, targetRow * TILE_SIZE + TILE_SIZE / 2);
  }

  /**
   * Tint the sprite based on its distance from the tree centre. Inside
   * the inner radius the sprite picks up the canopy's indigo-purple
   * glow; outside the outer radius the sprite renders with no tint.
   * The effect is much stronger at night so an agent walking under
   * the tree at night reads as dramatically lit by the foliage glow.
   */
  _applyTreeGlowTint() {
    const tcx = this.scene.treeCenter?.x;
    const tcy = this.scene.treeCenter?.y;
    if (tcx === undefined || tcy === undefined) return;
    const myCol = this.sprite.x / TILE_SIZE;
    const myRow = this.sprite.y / TILE_SIZE;
    const dist = Math.sqrt((myCol - tcx) ** 2 + (myRow - tcy) ** 2);
    // Inner radius (full tint) → tree centre. Outer radius (no tint)
    // → 14 tiles (matches the ground-glow outer ring in WorldScene
    // so the visual effect on the ground and the tint on the agent
    // line up).
    const INNER_R = 0;
    const OUTER_R = 14;
    let t;
    if (dist <= INNER_R) {
      t = 1;
    } else if (dist >= OUTER_R) {
      t = 0;
    } else {
      // Quadratic falloff matches the ground-glow rendering for visual
      // consistency.
      const lin = 1 - (dist - INNER_R) / (OUTER_R - INNER_R);
      t = lin * lin;
    }
    if (t <= 0) {
      // Outside the glow — clear any previous tint and skip the work.
      if (this._lastGlowT && this._lastGlowT > 0) {
        this.sprite.clearTint();
        this._lastGlowT = 0;
      }
      return;
    }
    // Stronger tint at night (the ground/canopy glow are also much
    // brighter at night, so the effect on the agent should match).
    const nightStrength = this.scene._nightStrength || 0;
    const intensity = Math.min(1, t * (0.35 + nightStrength * 0.65));
    // Target tint colour: light indigo with a purple lean. Picked to
    // match the canopy hue range (239°-271°) so the agent looks
    // illuminated by the same light source.
    const targetR = 130;
    const targetG = 120;
    const targetB = 255;
    const r = Math.round(255 * (1 - intensity) + targetR * intensity);
    const g = Math.round(255 * (1 - intensity) + targetG * intensity);
    const b = Math.round(255 * (1 - intensity) + targetB * intensity);
    const color = (r << 16) | (g << 8) | b;
    // Cheap dirty-bit — only re-apply when the tint actually changed
    // by a meaningful amount, to avoid hammering the renderer every
    // frame on stationary agents.
    if (Math.abs((this._lastGlowT || 0) - intensity) > 0.01) {
      this.sprite.setTint(color);
      this._lastGlowT = intensity;
    }
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

  _isBusy(inst) {
    // "Busy" = at least one active chat session for this agent. The
    // count is derived in main_app._worldAgentList from
    // status.active_sessions so the bubble and the agent-card row read
    // the same source of truth.
    return Number(inst && inst.active_tasks) > 0;
  }

  _updateBubble(inst) {
    // Three mutually-exclusive visual states for the bubble glyph:
    //   hourglass (⏳) — sandbox is not running (provisioning, disconnected,
    //                    or unknown); this is the existing "cold agent" indicator
    //   thought    (💭) — sandbox is running AND has at least one task in flight;
    //                    driven by inst.active_tasks > 0
    //   hidden           — sandbox is running and idle (no bubble at all)
    //
    // The busy state also gets a gentle vertical bob (±2px at 600ms)
    // as a secondary "this is alive" cue. Tween is created on demand
    // and stopped cleanly when the state transitions out.
    const isRunning = this._isRunning(inst);
    const isBusy = isRunning && this._isBusy(inst);
    const status = this._getStatus(inst);

    if (!isRunning) {
      // Cold / provisioning — hourglass, no bob
      this.bubble.setText('\u23f3');
      this.bubble.setVisible(status !== 'unknown');
      this._stopBusyTween();
      return;
    }

    if (isBusy) {
      // Running + currently processing a task — thought bubble + bob
      this.bubble.setText('\ud83d\udcad');
      this.bubble.setVisible(true);
      this._startBusyTween();
      return;
    }

    // Running + idle — hide the bubble entirely
    this.bubble.setVisible(false);
    this._stopBusyTween();
  }

  _startBusyTween() {
    // Only create the tween once; re-starting on every syncState()
    // poll would jitter the animation. If the tween exists and is
    // playing, leave it alone.
    if (this._busyTween && this._busyTween.isPlaying()) return;
    if (this._busyTween) {
      // Tween exists but was paused after a previous busy window
      this._busyTween.resume();
      return;
    }
    // Pulse the bubble's scale between 1.0 and 1.2 instead of its
    // position — update() unconditionally resets bubble.setPosition()
    // on every frame to keep it above the sprite head, which would
    // clobber a position tween. Scale isn't touched by update(), so
    // it's safe to animate here. The effect reads as a subtle
    // "thinking harder" breath.
    this._busyTween = this.scene.tweens.add({
      targets: this.bubble,
      scale: 1.2,
      duration: 600,
      ease: 'Sine.easeInOut',
      yoyo: true,
      repeat: -1,
    });
  }

  _stopBusyTween() {
    if (!this._busyTween) return;
    this._busyTween.stop();
    this._busyTween = null;
    // Restore default scale so the next time this bubble is shown
    // (e.g. an hourglass after the agent's sandbox goes cold) it
    // renders at its intended size.
    this.bubble.setScale(1);
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
    this._stopBusyTween();
    this.sprite.destroy();
    if (this.nameLabel) this.nameLabel.destroy();
    if (this.soulLabel) this.soulLabel.destroy();
    this.statusDot.destroy();
    this.bubble.destroy();
    if (this.logoBg) this.logoBg.destroy();
    if (this.logoText) this.logoText.destroy();
    if (this.tierGlyph) this.tierGlyph.destroy();
  }

  // Tasteful departure: float upward ~30px while fading to transparent,
  // then hard-destroy. Use this instead of destroy() when an agent is
  // being removed due to user action (hard-delete) rather than a
  // transient sync artefact.
  fadeAndDestroy(onComplete) {
    if (this._fading) return;
    this._fading = true;
    this._stopBusyTween();
    if (this.isMoving) {
      this.isMoving = false;
      this.sprite.anims?.stop();
    }
    const targets = [
      this.sprite,
      this.statusDot,
      this.bubble,
    ];
    if (this.logoText) targets.push(this.logoText);
    if (this.nameLabel) targets.push(this.nameLabel);
    if (this.soulLabel) targets.push(this.soulLabel);
    if (this.logoBg) targets.push(this.logoBg);
    if (this.tierGlyph) targets.push(this.tierGlyph);
    let _destroyed = false;
    const _doDestroy = () => {
      if (_destroyed) return;
      _destroyed = true;
      try { this.destroy(); } catch (_) {}
      if (onComplete) onComplete();
    };
    this.scene.tweens.add({
      targets,
      y: '-=30',
      alpha: 0,
      duration: 1500,
      ease: 'Sine.easeOut',
      onComplete: _doDestroy,
    });
    // Safety fallback — if the tween doesn't fire onComplete (scene
    // suspended, error in a callback, sprite already torn down by the
    // time it runs, etc.), force destroy after 2s so a deleted agent
    // never lingers visibly on screen. Catches the "animation failed"
    // case the user reported where deleted sprites stayed up forever.
    setTimeout(_doDestroy, 2000);
  }
}

// Re-export the deterministic char-index hash so the agent card sprite
// preview matches the in-world sprite without divergence.
export function nameToCharIndexExport(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = ((h << 5) - h + name.charCodeAt(i)) | 0;
  return Math.abs(h) % CHARACTER_COUNT;
}
