// Interactive graph viewer for Global Project Context.
// Zero dependencies — hand-rolled verlet force simulation on a 2D canvas.
// Data: site/data/graph.json — 627 real nodes, 1250 edges from this repo.

const COMMUNITY_PALETTE = [
  "#1f8a70", // 0 — primary accent (green)
  "#e0653f", // 1
  "#6f9bd8", // 2
  "#c4a23f", // 3
  "#c4492d", // 4
  "#4fa88f", // 5
  "#8e6cb3", // 6
  "#d48a5a", // 7
  "#5da0a3", // 8
  "#b5728a", // 9
  "#7a9b4c", // 10
  "#9d7b4c", // 11
  "#4f8caf", // 12
  "#a34f6a", // 13
  "#3d8a5a", // 14
];

const FILE_TYPE_ICON = {
  code: "◆",
  document: "▲",
  rationale: "●",
};

async function loadGraph(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("graph fetch failed");
  return res.json();
}

class Graph {
  constructor(canvas, data, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = {
      mode: opts.mode || "explorer", // "hero" or "explorer"
      onSelect: opts.onSelect || (() => {}),
      onHover: opts.onHover || (() => {}),
    };

    this.rawData = data;

    // Build adjacency
    this.nodeById = new Map();
    this.nodes = data.nodes.map((n) => {
      const node = {
        ...n,
        x: 0,
        y: 0,
        vx: 0,
        vy: 0,
        r: Math.min(14, 3 + Math.sqrt(n.degree || 1) * 1.6),
      };
      this.nodeById.set(n.id, node);
      return node;
    });
    this.edges = data.links
      .map((l) => ({
        source: this.nodeById.get(l.s),
        target: this.nodeById.get(l.t),
        relation: l.r,
        confidence: l.c === 1 ? "EXTRACTED" : "INFERRED",
        weight: l.w || 1,
      }))
      .filter((e) => e.source && e.target);

    this.neighbors = new Map();
    for (const n of this.nodes) this.neighbors.set(n.id, []);
    for (const e of this.edges) {
      this.neighbors.get(e.source.id).push({ node: e.target, edge: e });
      this.neighbors.get(e.target.id).push({ node: e.source, edge: e });
    }

    // Filters
    this.filter = {
      inferred: this.opts.mode === "explorer", // show both by default in explorer
      communities: new Set(this.nodes.map((n) => n.community)),
      search: "",
    };

    // Interaction state
    this.hovered = null;
    this.selected = null;
    this.dragging = null;
    this.pointer = { x: -1000, y: -1000, down: false };

    // Cooling: alpha decays from 1 → 0 over ~4s then the simulation freezes.
    // Re-heated on drag, filter change, or resize.
    this.alpha = 1;
    this.alphaDecay = 0.012;   // ~per-frame decay
    this.alphaMin = 0.004;
    this.running = true;

    this._bind();
    this._seedLayout();
    this._resize();
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  _reheat(a = 0.5) {
    this.alpha = Math.max(this.alpha, a);
    if (!this.running) {
      this.running = true;
      requestAnimationFrame(this._loop);
    }
  }

  _seedLayout() {
    // Circular seed with community clustering
    const byComm = new Map();
    for (const n of this.nodes) {
      if (!byComm.has(n.community)) byComm.set(n.community, []);
      byComm.get(n.community).push(n);
    }
    const communities = [...byComm.keys()].sort((a, b) => a - b);
    const R = 280;
    communities.forEach((c, i) => {
      const angle = (i / communities.length) * Math.PI * 2;
      const cx = Math.cos(angle) * R;
      const cy = Math.sin(angle) * R;
      const members = byComm.get(c);
      members.forEach((n, j) => {
        const a = (j / members.length) * Math.PI * 2;
        const rr = 30 + Math.random() * 40;
        n.x = cx + Math.cos(a) * rr;
        n.y = cy + Math.sin(a) * rr;
      });
    });
  }

  _bind() {
    window.addEventListener("resize", () => this._resize());
    const c = this.canvas;
    c.addEventListener("pointermove", (e) => this._onPointerMove(e));
    c.addEventListener("pointerdown", (e) => this._onPointerDown(e));
    c.addEventListener("pointerup", () => this._onPointerUp());
    c.addEventListener("pointerleave", () => {
      this.pointer.x = -1000;
      this.pointer.y = -1000;
      this.pointer.down = false;
      this.dragging = null;
      this._setHover(null);
    });
  }

  _resize() {
    const ratio = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.w = rect.width;
    this.h = rect.height;
    this.canvas.width = Math.floor(this.w * ratio);
    this.canvas.height = Math.floor(this.h * ratio);
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.cx = this.w / 2;
    this.cy = this.h / 2;
    // Resize may reveal empty background — repaint. If the sim was frozen,
    // a light reheat re-packs the layout to the new viewport.
    this._reheat(0.25);
  }

  _onPointerMove(e) {
    const rect = this.canvas.getBoundingClientRect();
    this.pointer.x = e.clientX - rect.left;
    this.pointer.y = e.clientY - rect.top;
    if (this.opts.mode !== "explorer") return;
    if (this.dragging) {
      const { node } = this.dragging;
      node.x = this.pointer.x - this.cx;
      node.y = this.pointer.y - this.cy;
      node.vx = 0;
      node.vy = 0;
      this._reheat(0.4); // keep the graph "alive" while user drags
    } else {
      const n = this._nodeAt(this.pointer.x, this.pointer.y);
      this._setHover(n);
    }
  }

  _onPointerDown(e) {
    if (this.opts.mode !== "explorer") return;
    const rect = this.canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const n = this._nodeAt(x, y);
    this.pointer.down = true;
    if (n) {
      this.dragging = { node: n };
      this._setSelected(n);
    } else {
      this._setSelected(null);
    }
  }

  _onPointerUp() {
    this.pointer.down = false;
    this.dragging = null;
  }

  _nodeAt(px, py) {
    const x = px - this.cx;
    const y = py - this.cy;
    // reverse order so top-drawn wins
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const n = this.nodes[i];
      if (!this._visible(n)) continue;
      const dx = n.x - x;
      const dy = n.y - y;
      if (dx * dx + dy * dy <= (n.r + 4) * (n.r + 4)) return n;
    }
    return null;
  }

  _setHover(n) {
    if (this.hovered === n) return;
    this.hovered = n;
    this.canvas.style.cursor = n ? "pointer" : "default";
    this.opts.onHover(n);
    if (!this.running) this._redraw(); // repaint highlight even when frozen
  }

  _setSelected(n) {
    this.selected = n;
    this.opts.onSelect(n);
    if (!this.running) this._redraw();
  }

  _visible(node) {
    if (!this.filter.communities.has(node.community)) return false;
    if (this.filter.search) {
      const q = this.filter.search.toLowerCase();
      return node.label.toLowerCase().includes(q) || (node.source_file || "").toLowerCase().includes(q);
    }
    return true;
  }

  _edgeVisible(edge) {
    if (!this.filter.inferred && edge.confidence === "INFERRED") return false;
    return this._visible(edge.source) && this._visible(edge.target);
  }

  setFilter(patch) {
    Object.assign(this.filter, patch);
    if (!this.running) this._redraw();
  }

  focusNode(id) {
    const n = this.nodeById.get(id);
    if (n) this._setSelected(n);
    return n;
  }

  _step() {
    // Cool down. Once alpha falls below alphaMin the sim freezes — no more RAF
    // work, canvas just holds the last frame.
    if (this.alpha <= this.alphaMin) {
      this.alpha = 0;
      this.running = false;
      // Zero-out velocities so any pending inertia stops.
      for (const a of this.nodes) { a.vx = 0; a.vy = 0; }
      return false;
    }

    const nodes = this.nodes;
    const n = nodes.length;
    const alpha = this.alpha;
    const repel = this.opts.mode === "hero" ? 900 : 1600;
    const spring = 0.06;
    const springLen = 70;
    const centerPull = 0.012;
    const damping = 0.6;
    const maxV = 8;

    // Repulsion (O(n^2) but n=627 is fine)
    for (let i = 0; i < n; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < n; j++) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy + 0.01;
        if (d2 > 40000) continue;
        const f = (repel / d2) * alpha;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    // Springs along edges
    for (const e of this.edges) {
      const a = e.source;
      const b = e.target;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const disp = d - springLen;
      const f = spring * disp * alpha;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    }

    // Centering + damping + integrate (with velocity cap)
    for (const a of nodes) {
      a.vx -= a.x * centerPull * alpha;
      a.vy -= a.y * centerPull * alpha;
      a.vx *= damping;
      a.vy *= damping;
      if (a.vx > maxV) a.vx = maxV; else if (a.vx < -maxV) a.vx = -maxV;
      if (a.vy > maxV) a.vy = maxV; else if (a.vy < -maxV) a.vy = -maxV;
      if (this.dragging && this.dragging.node === a) continue;
      a.x += a.vx;
      a.y += a.vy;
    }

    this.alpha -= this.alphaDecay;
    return true;
  }

  _draw() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.h);

    // Background for hero (dark)
    if (this.opts.mode === "hero") {
      ctx.fillStyle = "#0f1413";
      ctx.fillRect(0, 0, this.w, this.h);
    }

    ctx.save();
    ctx.translate(this.cx, this.cy);

    // Highlighted set (neighbors of selected/hovered)
    let focus = this.selected || this.hovered;
    let highlightNodes = null;
    let highlightEdges = null;
    if (focus && this.opts.mode === "explorer") {
      highlightNodes = new Set([focus.id]);
      highlightEdges = new Set();
      for (const { node, edge } of this.neighbors.get(focus.id) || []) {
        highlightNodes.add(node.id);
        highlightEdges.add(edge);
      }
    }

    // Edges
    for (const e of this.edges) {
      if (!this._edgeVisible(e)) continue;
      const isHL = highlightEdges && highlightEdges.has(e);
      const dim = highlightEdges && !isHL;
      if (this.opts.mode === "hero") {
        ctx.strokeStyle = e.confidence === "EXTRACTED"
          ? "rgba(64, 178, 146, 0.24)"
          : "rgba(196, 73, 45, 0.14)";
        ctx.lineWidth = 0.8;
      } else {
        if (isHL) {
          ctx.strokeStyle = e.confidence === "EXTRACTED"
            ? "rgba(31, 138, 112, 0.92)"
            : "rgba(196, 73, 45, 0.85)";
          ctx.lineWidth = 1.8;
        } else if (dim) {
          ctx.strokeStyle = "rgba(80, 86, 84, 0.12)";
          ctx.lineWidth = 0.6;
        } else {
          ctx.strokeStyle = e.confidence === "EXTRACTED"
            ? "rgba(31, 138, 112, 0.28)"
            : "rgba(196, 73, 45, 0.22)";
          ctx.lineWidth = 0.8;
        }
        if (e.confidence === "INFERRED") ctx.setLineDash([3, 3]);
      }
      ctx.beginPath();
      ctx.moveTo(e.source.x, e.source.y);
      ctx.lineTo(e.target.x, e.target.y);
      ctx.stroke();
      if (this.opts.mode === "explorer") ctx.setLineDash([]);
    }

    // Nodes
    for (const n of this.nodes) {
      if (!this._visible(n)) continue;
      const color = COMMUNITY_PALETTE[n.community % COMMUNITY_PALETTE.length];
      const isHL = highlightNodes && highlightNodes.has(n.id);
      const dim = highlightNodes && !isHL;

      if (this.opts.mode === "hero") {
        ctx.fillStyle = color;
        ctx.globalAlpha = 0.85;
      } else {
        ctx.fillStyle = dim ? "rgba(120, 126, 124, 0.35)" : color;
        ctx.globalAlpha = 1;
      }

      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fill();

      if (isHL && n === focus) {
        ctx.strokeStyle = "#111315";
        ctx.lineWidth = 2;
        ctx.stroke();
      } else if (this.opts.mode === "explorer") {
        ctx.strokeStyle = "rgba(16, 21, 20, 0.4)";
        ctx.lineWidth = 0.6;
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    // Labels for hub nodes or highlighted
    if (this.opts.mode === "explorer") {
      ctx.font = "11px 'SFMono-Regular', Consolas, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (const n of this.nodes) {
        if (!this._visible(n)) continue;
        const isHL = highlightNodes && highlightNodes.has(n.id);
        const show = isHL || n.degree >= 12;
        if (!show) continue;
        const label = n.label.length > 28 ? n.label.slice(0, 27) + "…" : n.label;
        const pad = 3;
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = "rgba(255, 250, 240, 0.92)";
        ctx.fillRect(n.x - tw / 2 - pad, n.y + n.r + 4, tw + pad * 2, 16);
        ctx.fillStyle = "#111315";
        ctx.fillText(label, n.x, n.y + n.r + 6);
      }
    }

    ctx.restore();
  }

  _loop() {
    const stepped = this._step();
    this._draw();
    if (stepped) {
      requestAnimationFrame(this._loop);
    }
    // When stepped === false the loop exits; _reheat() re-enters it.
  }

  _redraw() {
    // Repaint without re-running physics (for hover/selection changes).
    this._draw();
  }
}

// ---- Bootstrapping ---------------------------------------------------------

async function bootHeroGraph() {
  const canvas = document.getElementById("heroGraph");
  if (!canvas) return null;
  try {
    const data = await loadGraph("./data/graph.json");
    // Subset for hero: only nodes with degree >= 4 (keeps it uncluttered)
    const ids = new Set(data.nodes.filter((n) => (n.degree || 0) >= 4).map((n) => n.id));
    const subset = {
      nodes: data.nodes.filter((n) => ids.has(n.id)),
      links: data.links.filter((l) => ids.has(l.s) && ids.has(l.t)),
    };
    return new Graph(canvas, subset, { mode: "hero" });
  } catch (err) {
    console.warn("hero graph disabled:", err);
    return null;
  }
}

async function bootExplorer() {
  const canvas = document.getElementById("explorerGraph");
  if (!canvas) return null;
  const info = document.getElementById("explorerInfo");
  const searchInput = document.getElementById("explorerSearch");
  const inferredToggle = document.getElementById("explorerInferred");
  const commChips = document.getElementById("explorerCommunities");
  const stats = document.getElementById("explorerStats");

  let graph;
  try {
    const data = await loadGraph("./data/graph.json");
    graph = new Graph(canvas, data, {
      mode: "explorer",
      onSelect: (n) => renderInfo(n),
      onHover: (n) => {
        if (!graph.selected) renderInfo(n);
      },
    });

    if (stats) {
      stats.textContent = `${data.nodes.length} nodes · ${data.links.length} edges · ${data.meta.community_count} communities · source: ${data.meta.source}`;
    }

    // Populate community chips (top 8 by size)
    if (commChips) {
      const counts = new Map();
      data.nodes.forEach((n) => counts.set(n.community, (counts.get(n.community) || 0) + 1));
      const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
      commChips.innerHTML = "";
      top.forEach(([c, size]) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "commChip is-on";
        chip.style.setProperty("--chip", COMMUNITY_PALETTE[c % COMMUNITY_PALETTE.length]);
        chip.textContent = `community ${c} · ${size}`;
        chip.addEventListener("click", () => {
          chip.classList.toggle("is-on");
          if (chip.classList.contains("is-on")) graph.filter.communities.add(c);
          else graph.filter.communities.delete(c);
        });
        commChips.appendChild(chip);
      });
    }

    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        graph.setFilter({ search: e.target.value || "" });
      });
    }

    if (inferredToggle) {
      inferredToggle.addEventListener("change", (e) => {
        graph.setFilter({ inferred: e.target.checked });
      });
    }

    // Default focus: the hub node — mcp_server.py if present
    const hub =
      data.nodes.find((n) => n.label === "mcp_server.py") ||
      [...data.nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0))[0];
    if (hub) {
      setTimeout(() => {
        graph.focusNode(hub.id);
      }, 900);
    }
  } catch (err) {
    console.error("explorer graph failed:", err);
    if (info) info.innerHTML = `<p class="explorerInfo__error">Graph data unavailable.</p>`;
    return null;
  }

  function renderInfo(n) {
    if (!info) return;
    if (!n) {
      info.innerHTML = `
        <p class="explorerInfo__hint">Hover a node to inspect it. Click to lock.</p>
      `;
      return;
    }
    const ns = graph.neighbors.get(n.id) || [];
    const ext = ns.filter(({ edge }) => edge.confidence === "EXTRACTED").length;
    const inf = ns.length - ext;
    const byRel = new Map();
    ns.forEach(({ edge }) => byRel.set(edge.relation, (byRel.get(edge.relation) || 0) + 1));
    const relList = [...byRel.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([r, c]) => `<li><span>${r}</span><strong>${c}</strong></li>`)
      .join("");
    const icon = FILE_TYPE_ICON[n.file_type] || "○";
    const color = COMMUNITY_PALETTE[n.community % COMMUNITY_PALETTE.length];

    info.innerHTML = `
      <header class="explorerInfo__head">
        <span class="explorerInfo__icon" style="background:${color}">${icon}</span>
        <div>
          <strong>${escapeHtml(n.label)}</strong>
          <small>${escapeHtml(n.source_file || "—")}</small>
        </div>
      </header>
      <dl class="explorerInfo__meta">
        <div><dt>file_type</dt><dd>${n.file_type || "—"}</dd></div>
        <div><dt>community</dt><dd>${n.community}</dd></div>
        <div><dt>degree</dt><dd>${n.degree}</dd></div>
        <div><dt>extracted</dt><dd>${ext}</dd></div>
        <div><dt>inferred</dt><dd>${inf}</dd></div>
      </dl>
      <p class="explorerInfo__call">
        <code>gpc.graph_neighbors(node="${escapeHtml(n.label)}", depth=1)</code>
      </p>
      ${relList ? `<ul class="explorerInfo__rels">${relList}</ul>` : ""}
    `;
  }

  renderInfo(null);
  return graph;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

window.addEventListener("DOMContentLoaded", () => {
  bootHeroGraph();
  bootExplorer();
});
