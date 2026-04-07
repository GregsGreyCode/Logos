/**
 * Pathfinder — A* grid pathfinding for the agent world.
 *
 * Operates on a boolean walkable grid. Returns arrays of {col, row}
 * tile coordinates that agents follow step by step.
 */

export class Pathfinder {
  constructor(walkableGrid, cols, rows) {
    this.grid = walkableGrid;
    this.cols = cols;
    this.rows = rows;
  }

  /**
   * Find path from (startCol, startRow) to (endCol, endRow).
   * Returns array of {col, row} or null if no path exists.
   */
  findPath(startCol, startRow, endCol, endRow) {
    // Clamp to bounds
    startCol = Math.max(0, Math.min(this.cols - 1, Math.round(startCol)));
    startRow = Math.max(0, Math.min(this.rows - 1, Math.round(startRow)));
    endCol = Math.max(0, Math.min(this.cols - 1, Math.round(endCol)));
    endRow = Math.max(0, Math.min(this.rows - 1, Math.round(endRow)));

    // If end is not walkable, find nearest walkable
    if (!this.grid[endRow]?.[endCol]) {
      const nearest = this._nearestWalkable(endCol, endRow);
      if (!nearest) return null;
      endCol = nearest.col;
      endRow = nearest.row;
    }
    if (!this.grid[startRow]?.[startCol]) {
      const nearest = this._nearestWalkable(startCol, startRow);
      if (!nearest) return null;
      startCol = nearest.col;
      startRow = nearest.row;
    }

    // A*
    const key = (c, r) => `${c},${r}`;
    const startKey = key(startCol, startRow);
    const endKey = key(endCol, endRow);

    if (startKey === endKey) return [{ col: endCol, row: endRow }];

    const open = new Map();
    const closed = new Set();
    const cameFrom = new Map();
    const gScore = new Map();
    const fScore = new Map();

    gScore.set(startKey, 0);
    fScore.set(startKey, this._heuristic(startCol, startRow, endCol, endRow));
    open.set(startKey, { col: startCol, row: startRow });

    const dirs = [
      [0, -1], [0, 1], [-1, 0], [1, 0], // cardinal
      [-1, -1], [-1, 1], [1, -1], [1, 1], // diagonal
    ];

    let iterations = 0;
    const maxIterations = 2000;

    while (open.size > 0 && iterations++ < maxIterations) {
      // Find node with lowest fScore
      let bestKey = null;
      let bestF = Infinity;
      for (const [k] of open) {
        const f = fScore.get(k) ?? Infinity;
        if (f < bestF) { bestF = f; bestKey = k; }
      }

      if (bestKey === endKey) {
        return this._reconstructPath(cameFrom, endKey, endCol, endRow);
      }

      const current = open.get(bestKey);
      open.delete(bestKey);
      closed.add(bestKey);

      for (const [dc, dr] of dirs) {
        const nc = current.col + dc;
        const nr = current.row + dr;
        if (nc < 0 || nc >= this.cols || nr < 0 || nr >= this.rows) continue;
        if (!this.grid[nr][nc]) continue;

        // Diagonal movement: check both adjacent tiles are walkable
        if (dc !== 0 && dr !== 0) {
          if (!this.grid[current.row][nc] || !this.grid[nr][current.col]) continue;
        }

        const nk = key(nc, nr);
        if (closed.has(nk)) continue;

        const moveCost = (dc !== 0 && dr !== 0) ? 1.414 : 1;
        const tentativeG = (gScore.get(bestKey) ?? Infinity) + moveCost;

        if (tentativeG < (gScore.get(nk) ?? Infinity)) {
          cameFrom.set(nk, bestKey);
          gScore.set(nk, tentativeG);
          fScore.set(nk, tentativeG + this._heuristic(nc, nr, endCol, endRow));
          if (!open.has(nk)) {
            open.set(nk, { col: nc, row: nr });
          }
        }
      }
    }

    return null; // no path found
  }

  _heuristic(c1, r1, c2, r2) {
    // Octile distance
    const dx = Math.abs(c1 - c2);
    const dy = Math.abs(r1 - r2);
    return Math.max(dx, dy) + (1.414 - 1) * Math.min(dx, dy);
  }

  _reconstructPath(cameFrom, endKey, endCol, endRow) {
    const path = [];
    let current = endKey;
    while (current) {
      const [c, r] = current.split(',').map(Number);
      path.unshift({ col: c, row: r });
      current = cameFrom.get(current);
    }
    return path;
  }

  _nearestWalkable(col, row) {
    for (let radius = 1; radius < 10; radius++) {
      for (let dr = -radius; dr <= radius; dr++) {
        for (let dc = -radius; dc <= radius; dc++) {
          if (Math.abs(dr) !== radius && Math.abs(dc) !== radius) continue;
          const nr = row + dr, nc = col + dc;
          if (nr >= 0 && nr < this.rows && nc >= 0 && nc < this.cols && this.grid[nr][nc]) {
            return { col: nc, row: nr };
          }
        }
      }
    }
    return null;
  }
}
