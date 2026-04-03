/**
 * WorldManager — main orchestrator for the Agent World visualization.
 *
 * Creates a PixiJS Application, renders the tilemap, and manages agent
 * characters.  Bridges between Alpine.js state and the PixiJS scene graph.
 *
 * Usage (from Alpine.js):
 *   const wm = new WorldManager(containerEl, { onAgentClick: (name) => ... });
 *   wm.syncAgents(instancesArray);   // call on each data update
 *   wm.destroy();                    // cleanup
 */
import { WORLD_W, WORLD_H, TILE_SIZE } from './WorldConfig.js';
import { createTileMap } from './TileMap.js';
import { createAgentCharacter, updateAgentCharacter } from './AgentCharacter.js';

export class WorldManager {
  constructor(containerEl, options = {}) {
    this.container = containerEl;
    this.onAgentClick = options.onAgentClick || (() => {});
    this.agents = new Map();  // name → { container, inst }
    this._destroyed = false;

    // Create PixiJS Application
    this.app = new PIXI.Application({
      width: containerEl.clientWidth || 600,
      height: containerEl.clientHeight || 400,
      backgroundColor: 0x2d3748,
      antialias: false,
      resolution: window.devicePixelRatio || 1,
      autoDensity: true,
    });

    // Set pixel-art scaling
    PIXI.BaseTexture.defaultOptions.scaleMode = PIXI.SCALE_MODES.NEAREST;

    containerEl.appendChild(this.app.view);
    this.app.view.style.width = '100%';
    this.app.view.style.height = '100%';
    this.app.view.style.display = 'block';

    // Viewport (drag + zoom) via pixi-viewport
    if (typeof Viewport !== 'undefined' && Viewport.Viewport) {
      this.viewport = new Viewport.Viewport({
        screenWidth: containerEl.clientWidth || 600,
        screenHeight: containerEl.clientHeight || 400,
        worldWidth: WORLD_W,
        worldHeight: WORLD_H,
        events: this.app.renderer.events,
      });
      this.viewport.drag().pinch().wheel({ smooth: 5 }).decelerate();
      this.viewport.clampZoom({ minScale: 0.8, maxScale: 6 });
      this.viewport.clamp({ direction: 'all' });
      // Fit world to screen, then center
      const fitScale = Math.min(
        (containerEl.clientWidth || 600) / WORLD_W,
        (containerEl.clientHeight || 400) / WORLD_H,
      ) * 0.9;
      this.viewport.setZoom(Math.max(fitScale, 0.8));
      this.viewport.moveCenter(WORLD_W / 2, WORLD_H / 2);
      this.app.stage.addChild(this.viewport);
      this.worldContainer = this.viewport;
    } else {
      // Fallback: no viewport library, just use a scaled container
      this.worldContainer = new PIXI.Container();
      const scale = Math.min(
        (containerEl.clientWidth || 600) / WORLD_W,
        (containerEl.clientHeight || 400) / WORLD_H,
      );
      this.worldContainer.scale.set(scale);
      this.app.stage.addChild(this.worldContainer);
    }

    // Render tilemap
    this.tileMap = createTileMap(PIXI);
    this.worldContainer.addChild(this.tileMap);

    // Agent layer (above tilemap)
    this.agentLayer = new PIXI.Container();
    this.worldContainer.addChild(this.agentLayer);

    // Resize handler
    this._onResize = () => this._handleResize();
    window.addEventListener('resize', this._onResize);

    // Animation ticker for smooth movement
    this.app.ticker.add(() => this._tick());
  }

  /**
   * Sync the world with the current agent instance list.
   * Adds new agents, removes departed ones, updates existing.
   */
  syncAgents(instances) {
    if (this._destroyed) return;

    const currentNames = new Set(instances.map(i => i.name));

    // Remove agents that no longer exist
    for (const [name, entry] of this.agents) {
      if (!currentNames.has(name)) {
        this.agentLayer.removeChild(entry.container);
        entry.container.destroy({ children: true });
        this.agents.delete(name);
      }
    }

    // Add or update agents
    instances.forEach(async (inst, index) => {
      const existing = this.agents.get(inst.name);
      if (existing) {
        // Update state for lerp
        existing.inst = inst;
        existing.index = index;
        existing.total = instances.length;
      } else if (!this.agents.has(inst.name)) {
        // Mark as pending to prevent duplicate creation
        this.agents.set(inst.name, { container: null, inst, index, total: instances.length });
        try {
          const char = await createAgentCharacter(PIXI, inst, index, instances.length);
          if (this._destroyed) return;
          char.on('pointertap', () => this.onAgentClick(inst.name));
          this.agentLayer.addChild(char);
          this.agents.set(inst.name, {
            container: char,
            inst,
            index,
            total: instances.length,
          });
        } catch (e) {
          console.warn('Failed to create agent character:', inst.name, e);
          this.agents.delete(inst.name);
        }
      }
    });
  }

  _tick() {
    // Smooth position updates
    for (const entry of this.agents.values()) {
      if (entry.container) {
        updateAgentCharacter(entry.container, entry.inst, entry.index, entry.total);
      }
    }
  }

  _handleResize() {
    if (this._destroyed || !this.container) return;
    const w = this.container.clientWidth || 600;
    const h = this.container.clientHeight || 400;
    this.app.renderer.resize(w, h);
    if (this.viewport) {
      this.viewport.resize(w, h);
    } else if (this.worldContainer) {
      const scale = Math.min(w / WORLD_W, h / WORLD_H);
      this.worldContainer.scale.set(scale);
    }
  }

  destroy() {
    this._destroyed = true;
    window.removeEventListener('resize', this._onResize);
    if (this.app) {
      this.app.destroy(true, { children: true, texture: true, baseTexture: true });
    }
    this.agents.clear();
  }
}
