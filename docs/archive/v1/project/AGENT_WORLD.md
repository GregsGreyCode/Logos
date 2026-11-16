# Agent World — Tamagotchi-Style Agent Visualization

**Status:** Planning  
**Created:** 2026-04-03  
**Depends on:** Multi-agent instances (complete), PixiJS (new dependency)

## Vision

Replace the flat instance list with a living visual world where each agent is a
small animated character in a pixel art sandbox. The world shows agents working,
idling, thinking — and their infrastructure (storage, MCP connections, soul
identity) as physical objects in the space. Spawning a new agent creates a new
character with a unique appearance. Deleting one makes it wave goodbye and fade
out.

This becomes the **first tab** in the Logos UI — the primary way users interact
with their agents.

## Reference Implementation

**a16z/ai-town** (MIT, 9.6K stars) is the closest prior art. It uses PixiJS for
a top-down pixel world with animated characters. Cloned to
`knowledge-repos/ai-town/` for study. Key files:

| File | What we learn from it |
|------|----------------------|
| `src/components/Game.tsx` | Canvas mount + container sizing |
| `src/components/PixiGame.tsx` | Scene graph composition, click handling |
| `src/components/PixiStaticMap.tsx` | Tilemap rendering (tile arrays → sprites) |
| `src/components/Character.tsx` | Directional AnimatedSprite from spritesheet |
| `src/components/PixiViewport.tsx` | Drag/zoom via pixi-viewport |
| `data/gentle.js` | Map data format (bgTiles/objectTiles 3D arrays) |
| `data/spritesheets/f1.ts` | Spritesheet frame definitions |

Their stack: `pixi.js@^7.2.4`, `@pixi/react@^7.1.0`, `pixi-viewport@^5.0.1`.
We skip `@pixi/react` (we use Alpine.js) and use vanilla PixiJS API instead.

---

## Technology Stack

| Component | Library | Size | License | Notes |
|-----------|---------|------|---------|-------|
| **Renderer** | PixiJS v7 | ~150KB gz | MIT | WebGL with Canvas fallback, what ai-town uses |
| **Viewport** | pixi-viewport | ~30KB | MIT | Drag, pinch, wheel zoom, clamping |
| **Agent identity** | DiceBear | ~50KB | MIT | Seed-based unique avatars for inspector panels |
| **Tiles** | Kenney Tiny Town + Tiny Dungeon | CC0 | CC0 | 16x16 pixel art, cozy aesthetic, no attribution needed |
| **Characters** | Shared spritesheet + PixiJS tint | — | — | 1 base walk cycle, recolored per agent |
| **UI overlay** | Alpine.js (existing) | — | — | Inspector panel, name tags, speech bubbles via HTML overlay |

**Total added bundle**: ~230KB gzipped.

### Why not a full game engine?

Phaser (~1MB) is overkill. We need: sprites, tilemap, click events, and
animation. PixiJS gives exactly that. The ai-town team made the same choice.

---

## World Design

### Map

- **Size**: 24×24 tiles (small — this is a dashboard, not a game)
- **Tile size**: 16×16 pixels (384×384 world, scaled up via viewport)
- **Tileset**: Kenney Tiny Town (CC0) — grass, stone paths, water, trees, fences
- **Authored in**: Tiled map editor, exported to JSON, converted to tile arrays
  (same workflow as ai-town)
- **Zones**:
  - Central plaza — where agents congregate by default
  - Workshop area — where agents move when "working" (processing a request)
  - Library — visual area near knowledge base objects
  - Server rack area — where MCP server objects live

### Agent Characters

- **Base spritesheet**: 16×16 or 32×32 humanoid, 4 directions × 3-4 walk frames
- **Source**: Kenney Tiny Dungeon characters (CC0) or Sprout Lands farmer
- **Differentiation**: PixiJS `sprite.tint` applies a unique color per agent
  (derived from hash of instance name). DiceBear avatar shown in inspector popup.
- **States** (each maps to an animation or visual):

| State | Visual | Trigger |
|-------|--------|---------|
| `idle` | Standing still, occasional blink animation | No active request |
| `thinking` | Thought bubble above head (💭) | Processing a user message |
| `working` | Moves to workshop, sits at desk | Executing tools |
| `talking` | Speech bubble with typing dots | Generating response |
| `sleeping` | Zzz animation, eyes closed | Session expired / idle timeout |
| `error` | Red exclamation mark, stumble animation | Pod in CrashLoopBackOff |
| `spawning` | Poof/sparkle animation, fades in | Just created |
| `departing` | Waves, fades out | Being deleted |

### Infrastructure Objects

Each agent has associated objects placed near them in the world:

| Object | Visual | What it represents |
|--------|--------|-------------------|
| **Chest** | Small treasure chest sprite | PVC storage (size indicator) |
| **Bookshelf** | Bookshelf with books | Knowledge base (more books = more sources) |
| **Glowing orb** | Colored floating orb | Soul identity (color matches soul) |
| **Small server** | Mini computer/cabinet | MCP server connections |
| **Scroll** | Rolled scroll/paper | MEMORY.md contents |
| **Badge** | Shield/badge icon | Policy level |

Objects are clickable — clicking opens the relevant inspector tab.

---

## Architecture

### Frontend Integration

```
gateway/html/main_app.html
  └── Tab: "World" (first tab, before Sessions)
      └── <div id="agent-world" x-init="initWorld()">
          ├── PixiJS canvas (fills the div)
          └── HTML overlay (Alpine.js)
              ├── Agent name tags (positioned via CSS transform)
              ├── Speech/thought bubbles
              └── Click → inspector panel (existing)
```

### Data Flow

```
┌─────────────┐    poll /instances     ┌──────────────┐
│  Logos API   │ ◄──── every 5s ─────► │  Alpine.js   │
│  /instances  │                       │  state       │
│  /status     │                       └──────┬───────┘
└─────────────┘                               │
                                              │ update positions/states
                                              ▼
                                       ┌──────────────┐
                                       │  PixiJS      │
                                       │  Scene Graph  │
                                       │              │
                                       │  Tilemap     │
                                       │  Characters  │
                                       │  Objects     │
                                       │  Animations  │
                                       └──────────────┘
```

1. Alpine polls `/instances` every 5 seconds (already does this)
2. On state change, Alpine calls into the PixiJS world manager
3. World manager adds/removes/updates character sprites
4. Character positions are deterministic from state (not server-driven):
   - `idle` → assigned home position in plaza
   - `working` → workshop area
   - Characters smoothly interpolate between positions (tweening)
5. Click events on sprites fire back to Alpine (open inspector)

### File Structure

```
gateway/
  world/
    WorldManager.js       — main orchestrator (init, update, destroy)
    TileMap.js            — load + render tile layers
    AgentCharacter.js     — per-agent sprite, animation state machine
    InfraObject.js        — infrastructure object sprites
    WorldConfig.js        — constants (tile size, world dims, zone coords)
    assets/
      tiles.png           — Kenney tileset spritesheet
      characters.png      — character spritesheet
      objects.png         — infrastructure object sprites
      map.json            — tile array data (from Tiled)
  html/
    main_app.html         — World tab added as first tab
```

### PixiJS ↔ Alpine.js Bridge

No React wrappers needed. The bridge is simple:

```javascript
// In Alpine init:
this.worldManager = new WorldManager(
  document.getElementById('agent-world'),
  { onAgentClick: (name) => this.openInspector(name) }
);

// On instance data update:
this.$watch('clusterInstances', (instances) => {
  this.worldManager.syncAgents(instances);
});

// Cleanup:
this.worldManager.destroy();
```

---

## Implementation Phases

### Phase 1 — Static world with agent dots (MVP)

**Goal**: Get PixiJS rendering in the World tab with a tilemap and one sprite
per running agent. Click to inspect.

- [ ] Add PixiJS + pixi-viewport as CDN script tags (no build step needed)
- [ ] Create `WorldManager.js` — init PIXI.Application, mount to div
- [ ] Create `TileMap.js` — render a simple 24×24 grass/stone map
- [ ] Create `AgentCharacter.js` — colored circle + name label per agent
- [ ] Wire to Alpine: sync agent list, handle clicks
- [ ] Add "World" as first tab in the nav

**Deliverable**: You see a pixel map with colored dots for each agent. Clicking
a dot opens the inspector. No animation yet.

### Phase 2 — Animated characters

**Goal**: Replace dots with animated spritesheet characters.

- [ ] Source character spritesheet (Kenney CC0 or create base sheet)
- [ ] Implement directional AnimatedSprite (study ai-town's Character.tsx)
- [ ] Add state machine: idle/walk animations
- [ ] Per-agent color tinting from instance name hash
- [ ] Smooth movement tweening between positions

### Phase 3 — Agent states and infrastructure

**Goal**: Characters react to real state. Infrastructure objects appear.

- [ ] Map agent k8s status → character state (idle, working, error, etc.)
- [ ] Add thought/speech bubble overlays (HTML positioned over canvas)
- [ ] Add infrastructure objects near each agent (chest, bookshelf, orb)
- [ ] Objects reflect real data (knowledge chunk count → bookshelf fullness)
- [ ] DiceBear avatar in inspector panel header

### Phase 4 — Spawn/delete animations and polish

**Goal**: Living feel — agents appear and disappear with personality.

- [ ] Spawn: poof particle effect, character fades in
- [ ] Delete: wave animation, fade out
- [ ] Idle behaviors: occasional wandering, sitting down, sleeping
- [ ] Zone-based positioning: agents move to workshop when busy
- [ ] Agent-to-agent proximity when in the same soul category
- [ ] Mini sound effects (optional, via @pixi/sound)

### Phase 5 — Interactive world

**Goal**: Direct manipulation — drag agents, build the world.

- [ ] Drag agent to a zone to assign a task context
- [ ] Right-click agent for quick actions (chat, inspect, delete, fork)
- [ ] Hover tooltip with key stats (uptime, memory usage, last active)
- [ ] Live chat preview: agent's latest message scrolls in a speech bubble
- [ ] World editor mode for admins (rearrange objects, resize zones)

---

## Asset Pipeline

### Tilesets (CC0 — no attribution required)

| Pack | Source | Use |
|------|--------|-----|
| Kenney Tiny Town | kenney.nl/assets/tiny-town | Terrain, buildings, paths |
| Kenney Tiny Dungeon | kenney.nl/assets/tiny-dungeon | Furniture, chests, bookshelves, characters |
| Kenney 1-Bit Pack | kenney.nl/assets/1-bit-pack | Fallback icons, gems, orbs |
| Kenney UI Pack | kenney.nl/assets/ui-pack | Health bars, panels, buttons |

### Character Sprites

Option A (simplest): Use Kenney Tiny Dungeon characters directly. Limited
variety but CC0 and consistent style. Tint for differentiation.

Option B (more variety): Use the LPC character generator
(sanderfrenken.github.io/Universal-LPC-Spritesheet-Character-Generator/) to
create several base characters. CC-BY-SA license (requires attribution). 32×32
tiles.

Option C (procedural): Use pixel-sprite-generator
(github.com/zfedoran/pixel-sprite-generator) to create unique body shapes from
agent ID seed. Most unique but most work.

**Recommendation**: Start with Option A (Kenney CC0 + tinting) for Phase 1-2.
Evaluate Option B for Phase 3 if more variety is needed.

### Map Design

Author in Tiled (free, cross-platform map editor), export to JSON, convert to
tile arrays with a script (copy ai-town's `convertMap.js` pattern). The map is
small (24×24 = 576 tiles) so the JSON is ~2KB.

---

## Key Decisions

1. **16×16 vs 32×32 tiles?** — 16×16 for cozier feel and smaller assets.
   Viewport zoom handles visibility. ai-town uses 32×32.

2. **CDN vs bundled?** — CDN for PixiJS (no build step, matches existing
   Alpine.js CDN pattern in the project). `<script src="https://cdn.jsdelivr.net/npm/pixi.js@7/dist/pixi.min.js">`.

3. **Server-driven vs client-driven positions?** — Client-driven. Agent
   positions in the world are derived from their state (idle → plaza, working →
   workshop), not stored on the server. Keeps the backend simple.

4. **Tab name?** — "World" as the first tab, replacing the current instance list
   as the primary interface.

5. **Existing instance list?** — Keep it as a compact secondary view within the
   Infra tab. The World tab is the friendly face; the instance list is the ops
   view.
