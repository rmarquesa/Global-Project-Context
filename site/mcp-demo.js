// Pre-computed MCP query demo. Shows what the client sees when an AI tool
// calls the GPC MCP server — request JSON, streamed response, token math.

const TYPE_SPEED = 10; // ms/char for streamed response

async function loadQueries(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("demo queries fetch failed");
  return res.json();
}

function formatJson(obj) {
  return JSON.stringify(obj, null, 2);
}

async function typeInto(el, text, speedMs) {
  el.textContent = "";
  for (let i = 0; i < text.length; i++) {
    el.textContent += text[i];
    if (i % 4 === 0) await new Promise((r) => setTimeout(r, speedMs));
  }
}

function streamChildren(container, elements, delayMs) {
  container.innerHTML = "";
  return new Promise((resolve) => {
    let i = 0;
    const next = () => {
      if (i >= elements.length) return resolve();
      container.appendChild(elements[i]);
      i++;
      setTimeout(next, delayMs);
    };
    next();
  });
}

function animateNumber(el, from, to, durationMs) {
  const t0 = performance.now();
  function frame(t) {
    const p = Math.min(1, (t - t0) / durationMs);
    const eased = 1 - Math.pow(1 - p, 3);
    const v = Math.round(from + (to - from) * eased);
    el.textContent = v.toLocaleString();
    if (p < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function makeResultCard(r) {
  const div = document.createElement("article");
  div.className = "demoChunk";
  div.innerHTML = `
    <header>
      <span class="demoChunk__score">score ${r.score.toFixed(2)}</span>
      <span class="demoChunk__file">${escapeHtml(r.source_file)}</span>
    </header>
    <pre><code>${escapeHtml(r.snippet)}</code></pre>
  `;
  return div;
}

function makeNeighborRow(r) {
  const div = document.createElement("li");
  div.className = `demoRow demoRow--${r.direction} demoRow--${r.confidence.toLowerCase()}`;
  div.innerHTML = `
    <span class="demoRow__rel">${r.direction === "out" ? "→" : "←"} ${escapeHtml(r.relation)}</span>
    <strong class="demoRow__target">${escapeHtml(r.target)}</strong>
    <small class="demoRow__file">${escapeHtml(r.file)}</small>
    <span class="demoRow__conf" title="confidence">${r.confidence}</span>
  `;
  return div;
}

function makePathNode(segment, idx) {
  if (segment.node !== undefined) {
    const div = document.createElement("li");
    div.className = "demoPath__node";
    div.innerHTML = `
      <span class="demoPath__index">${Math.floor(idx / 2)}</span>
      <strong>${escapeHtml(segment.node)}</strong>
      <small>${escapeHtml(segment.file || "")}</small>
    `;
    return div;
  }
  const div = document.createElement("li");
  div.className = `demoPath__edge demoPath__edge--${segment.confidence.toLowerCase()}`;
  div.innerHTML = `<span>↓ ${escapeHtml(segment.edge)} · ${segment.confidence}</span>`;
  return div;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function runQuery(q, ui) {
  const { requestEl, responseEl, savedEl, beforeEl, afterEl, status, toolTag } = ui;

  // status + tool tag
  toolTag.textContent = q.tool;
  status.textContent = "sending request…";
  status.dataset.state = "sending";

  // Request: typewriter
  await typeInto(requestEl, formatJson(q.request), TYPE_SPEED);

  status.textContent = "embedding query · querying Qdrant · hydrating from Postgres…";
  await new Promise((r) => setTimeout(r, 500));

  // Response: streamed
  responseEl.innerHTML = "";
  if (q.tool === "gpc.search") {
    const cards = q.results.map(makeResultCard);
    await streamChildren(responseEl, cards, 220);
  } else if (q.tool === "gpc.graph_neighbors") {
    const ul = document.createElement("ul");
    ul.className = "demoNeighbors";
    responseEl.appendChild(ul);
    const rows = q.results.map(makeNeighborRow);
    await streamChildren(ul, rows, 90);
  } else if (q.tool === "gpc.graph_path") {
    const ul = document.createElement("ul");
    ul.className = "demoPath";
    responseEl.appendChild(ul);
    const items = q.path.map(makePathNode);
    await streamChildren(ul, items, 180);
  }

  // Tokens
  status.textContent = "response complete";
  status.dataset.state = "done";
  animateNumber(beforeEl, 0, q.tokens_before, 900);
  animateNumber(afterEl, 0, q.tokens_after, 900);
  const saved = Math.round(((q.tokens_before - q.tokens_after) / q.tokens_before) * 1000) / 10;
  animateNumber(savedEl, 0, Math.floor(saved), 900);
}

async function bootDemo() {
  const root = document.getElementById("mcpDemo");
  if (!root) return;

  const presetsEl = root.querySelector("[data-demo-presets]");
  const requestEl = root.querySelector("[data-demo-request]");
  const responseEl = root.querySelector("[data-demo-response]");
  const savedEl = root.querySelector("[data-demo-saved]");
  const beforeEl = root.querySelector("[data-demo-before]");
  const afterEl = root.querySelector("[data-demo-after]");
  const status = root.querySelector("[data-demo-status]");
  const toolTag = root.querySelector("[data-demo-tool]");

  let data;
  try {
    data = await loadQueries("./data/demo-queries.json");
  } catch (err) {
    console.error("demo unavailable", err);
    root.innerHTML = `<p class="demoError">Demo data unavailable.</p>`;
    return;
  }

  const ui = { requestEl, responseEl, savedEl, beforeEl, afterEl, status, toolTag };

  presetsEl.innerHTML = "";
  let active = null;
  let running = false;

  data.queries.forEach((q, idx) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "demoPreset";
    btn.innerHTML = `<strong>${escapeHtml(q.label)}</strong><span>${escapeHtml(q.tool)}</span>`;
    btn.addEventListener("click", async () => {
      if (running) return;
      if (active) active.classList.remove("is-active");
      active = btn;
      btn.classList.add("is-active");
      running = true;
      try {
        await runQuery(q, ui);
      } finally {
        running = false;
      }
    });
    presetsEl.appendChild(btn);
    if (idx === 0) active = btn;
  });

  if (active) {
    active.classList.add("is-active");
    // Auto-run first query after a short delay when the section scrolls into view
    const section = root.closest("section") || root;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting && !running && !requestEl.textContent.trim()) {
            running = true;
            runQuery(data.queries[0], ui).finally(() => {
              running = false;
            });
            io.disconnect();
          }
        }
      },
      { threshold: 0.4 },
    );
    io.observe(section);
  }
}

window.addEventListener("DOMContentLoaded", bootDemo);
