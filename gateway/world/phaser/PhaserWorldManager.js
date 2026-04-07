/**
 * PhaserWorldManager — drop-in replacement for the PIXI-based WorldManager.
 *
 * Same API:
 *   new PhaserWorldManager(containerEl, { onAgentClick })
 *   .syncAgents(instances)
 *   .destroy()
 *
 * Internally boots a Phaser.Game with WorldScene.
 */
import { WORLD_W, WORLD_H } from '../WorldConfig.js';
import { WorldScene } from './WorldScene.js';

export class PhaserWorldManager {
  constructor(containerEl, options = {}) {
    this.container = containerEl;
    this.onAgentClick = options.onAgentClick || (() => {});
    this.onAgentHover = options.onAgentHover || (() => {});
    this._destroyed = false;
    this._scene = null;
    this._pendingSync = null;

    const width = containerEl.clientWidth || 800;
    const height = containerEl.clientHeight || 600;

    this.game = new Phaser.Game({
      type: Phaser.AUTO,
      parent: containerEl,
      width,
      height,
      backgroundColor: '#0a0a0f',
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

    // Style the canvas
    const canvas = containerEl.querySelector('canvas');
    if (canvas) {
      canvas.style.width = '100%';
      canvas.style.height = '100%';
    }
  }

  /**
   * Sync agent state into the world.
   * Identical contract to the old PIXI WorldManager.
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
    if (this.game) {
      this.game.destroy(true);
      this.game = null;
    }
    this._scene = null;
  }
}

// Also export as WorldManager for backward compatibility
export { PhaserWorldManager as WorldManager };
