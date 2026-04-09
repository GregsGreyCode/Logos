/**
 * WorldManager — boots a Phaser.Game with WorldScene and exposes
 * a small imperative facade to the Alpine app.
 *
 * Public API:
 *   new WorldManager(containerEl, { onAgentClick, onAgentHover })
 *   .syncAgents(instances)
 *   .destroy()
 */
import { WORLD_W, WORLD_H } from './WorldConfig.js';
import { WorldScene } from './WorldScene.js';

export class WorldManager {
  constructor(containerEl, options = {}) {
    this.container = containerEl;
    this.onAgentClick = options.onAgentClick || (() => {});
    this.onAgentHover = options.onAgentHover || (() => {});
    this._destroyed = false;
    this._scene = null;
    this._pendingSync = null;

    const width = containerEl.clientWidth || 800;
    const height = containerEl.clientHeight || 600;

    // Background colour matches the predominant GRASS tile (0x2f4537) so
    // the empty area outside the square tilemap (when the container is
    // wider than tall, or vice versa) blends into the world instead of
    // showing as a contrasting dark bar at the top/bottom or sides.
    this.game = new Phaser.Game({
      type: Phaser.AUTO,
      parent: containerEl,
      width,
      height,
      backgroundColor: '#2f4537',
      pixelArt: true,
      scale: {
        mode: Phaser.Scale.RESIZE,
        autoCenter: Phaser.Scale.CENTER_BOTH,
      },
      scene: [],
      audio: { noAudio: true },
      banner: false,
    });

    // Start the world scene with callbacks
    this.game.scene.add('WorldScene', WorldScene, true, {
      onAgentClick: (name, inst) => {
        this.onAgentClick(name, inst);
      },
      onAgentHover: (name, inst, isOver) => {
        this.onAgentHover(name, inst, isOver);
      },
    });

    // Poll for scene readiness (scene.create() sets _ready = true)
    const checkReady = () => {
      if (this._destroyed) return;
      const scene = this.game?.scene?.getScene('WorldScene');
      if (scene?._ready) {
        this._scene = scene;
        if (this._pendingSync) {
          this._scene.syncAgents(this._pendingSync);
          this._pendingSync = null;
        }
      } else {
        setTimeout(checkReady, 100);
      }
    };
    setTimeout(checkReady, 200);

    // Watch the host element for size changes (sidebar collapses/expands,
    // window resize). Phaser's RESIZE mode reads from style on the parent,
    // so we explicitly call scale.resize with the new dimensions to keep
    // the renderer in lock-step with our square container.
    if (typeof ResizeObserver !== 'undefined') {
      this._resizeObserver = new ResizeObserver(() => {
        if (this._destroyed || !this.game) return;
        const w = containerEl.clientWidth;
        const h = containerEl.clientHeight;
        if (w > 0 && h > 0) {
          this.game.scale.resize(w, h);
          // refresh() forces Phaser to re-read the parent dimensions and
          // re-emit the 'resize' event, which the WorldScene listens to
          // for camera refit. Without this the canvas updates but the
          // camera/zoom can drift after layout changes (e.g. agent
          // column 16rem ↔ 24rem toggle when the create form opens),
          // leaving the scene rendered at the old zoom and "centered"
          // inside the new container.
          this.game.scale.refresh();
        }
      });
      this._resizeObserver.observe(containerEl);
    }
  }

  /**
   * Force the renderer to re-fit the current parent dimensions. Used by
   * the Alpine app when it knows a layout change just happened that the
   * ResizeObserver might have missed (display:none → display:block on
   * tab change, agent column width toggle on create-form open/close).
   * Safe to call repeatedly.
   */
  forceResize() {
    if (this._destroyed || !this.game) return;
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    if (w > 0 && h > 0) {
      this.game.scale.resize(w, h);
      this.game.scale.refresh();
    }
  }

  /**
   * Sync agent state into the world. Pass the full agent list each
   * time — the scene diffs to add/remove/update sprites.
   */
  syncAgents(instances) {
    if (this._destroyed) return;
    if (this._scene) {
      this._scene.syncAgents(instances);
    } else {
      // Scene not ready yet — buffer for when it is
      this._pendingSync = instances;
    }
  }

  /**
   * Full cleanup.
   */
  destroy() {
    if (this._destroyed) return;
    this._destroyed = true;
    if (this._resizeObserver) {
      try { this._resizeObserver.disconnect(); } catch (_) {}
      this._resizeObserver = null;
    }
    if (this.game) {
      this.game.destroy(true);
      this.game = null;
    }
    this._scene = null;
  }
}
