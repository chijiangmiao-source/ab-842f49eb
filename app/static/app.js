"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
let DRILLS = [];
let CURRENT = null;   // get_drill 结果
let TIMELINE = null;  // timeline 结果
let frameIdx = 0;
let playTimer = null;

const $ = (id) => document.getElementById(id);

async function api(url, opts) {
  const resp = await fetch(url, opts);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* ignore */ }
  return { ok: resp.ok, status: resp.status, data };
}

// ---------------------------------------------------------------------------
// 初始化与演练切换
// ---------------------------------------------------------------------------

async function loadDrills(selectId) {
  const { data } = await api("/api/drills");
  DRILLS = (data && data.drills) || [];
  const sel = $("drill-select");
  sel.innerHTML = "";
  for (const d of DRILLS) {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = d.id + (d.name ? ` — ${d.name}` : "");
    sel.appendChild(opt);
  }
  if (DRILLS.length && !selectId) selectId = DRILLS[0].id;
  if (selectId) sel.value = selectId;
  return sel.value;
}

async function openDrill(id) {
  const [a, b] = await Promise.all([
    api(`/api/drills/${id}`),
    api(`/api/drills/${id}/timeline`),
  ]);
  if (!a.ok) { alert("加载演练失败: " + JSON.stringify(a.data)); return; }
  CURRENT = a.data;
  TIMELINE = b.data;
  $("drill-name").textContent = CURRENT.name || "";
  const slider = $("frame-slider");
  slider.max = String(TIMELINE.frames.length - 1);
  slider.value = String(TIMELINE.frames.length - 1);
  frameIdx = TIMELINE.frames.length - 1;
  renderLedger();
  renderFrame();
  renderRecoveryBanner(null);
  $("recovery-out").textContent = "";
}

// ---------------------------------------------------------------------------
// 帧渲染
// ---------------------------------------------------------------------------

function frameSnap() {
  return TIMELINE.frames[frameIdx].snapshot;
}

function renderFrame() {
  const frame = TIMELINE.frames[frameIdx];
  const snap = frame.snapshot;
  const slider = $("frame-slider");
  slider.value = String(frameIdx);

  $("frame-label").textContent =
    `帧 ${frameIdx}/${TIMELINE.frames.length - 1}` +
    (frame.event_id ? ` · 事件 ${frame.event_id} (${frame.type})` : "");

  $("st-phase").textContent =
    snap.phase + (snap.phase === "ready" ? "（灰队列已空，可清扫）"
      : snap.phase === "marking" ? "（标记进行中，禁止清扫）" : "");
  $("st-queue").textContent = snap.gray_queue.length ? snap.gray_queue.join(" → ") : "（空）";
  $("st-roots").textContent = snap.roots.join(", ") || "—";
  $("st-alive").textContent = snap.alive.join(", ") || "—";
  $("st-reclaimed").textContent = snap.reclaimed.join(", ") || "—";

  const chips = $("st-colors");
  chips.innerHTML = "";
  const universe = CURRENT.spec.objects;
  for (const o of universe) {
    const span = document.createElement("span");
    if (snap.colors[o]) {
      span.className = `chip ${snap.colors[o]}`;
      span.textContent = `${o}·${snap.colors[o]}`;
    } else {
      span.className = "chip dead";
      span.textContent = `${o}·reclaimed`;
    }
    chips.appendChild(span);
  }

  let result = frame.snapshot.last_result;
  if (!frame.accepted && frame.event_id) {
    result = { rejected: true, reason: frame.reason, detail: frame.detail };
  }
  $("st-result").textContent = frame.event_id
    ? JSON.stringify(result, null, 2) : "（初始帧）";

  renderGraph(snap);
  renderEvidence(snap.evidence);
  renderRejectBanner(snap.first_rejection);
  highlightLedger(frame);
}

function positions(snap) {
  const nodes = CURRENT.spec.objects;
  const n = nodes.length;
  const cx = 260, cy = 195, r = n <= 6 ? 130 : n <= 12 ? 150 : 162;
  const pos = {};
  nodes.forEach((o, i) => {
    const ang = -Math.PI / 2 + (2 * Math.PI * i) / n;
    pos[o] = [cx + r * Math.cos(ang), cy + r * Math.sin(ang)];
  });
  return pos;
}

function el(name, attrs, text) {
  const node = document.createElementNS(SVG_NS, name);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  if (text != null) node.textContent = text;
  return node;
}

function renderGraph(snap) {
  const svg = $("graph");
  svg.innerHTML = "";
  svg.appendChild(el("defs", {}, ""));
  const defs = svg.querySelector("defs");
  const marker = el("marker", {
    id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
    markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
  });
  marker.appendChild(el("path", { d: "M0,0 L10,5 L0,10 z", fill: "#5f7392" }));
  defs.appendChild(marker);

  const pos = positions(snap);
  const alive = new Set(snap.alive);
  const edgeSet = new Set(snap.survival_graph.edges.map(([u, v]) => u + "->" + v));

  // 边（存活图）
  for (const [u, v] of snap.survival_graph.edges) {
    const [x1, y1] = pos[u], [x2, y2] = pos[v];
    if (u === v) {
      svg.appendChild(el("path", {
        class: "edge", d: `M ${x1} ${y1 - 18} a 14 14 0 1 1 -0.1 0`,
        "marker-end": "url(#arrow)",
      }));
      continue;
    }
    const dx = x2 - x1, dy = y2 - y1;
    const d = Math.hypot(dx, dy), gap = 22;
    const ax = x1 + (dx / d) * gap, ay = y1 + (dy / d) * gap;
    const bx = x2 - (dx / d) * gap, by = y2 - (dy / d) * gap;
    svg.appendChild(el("line", {
      class: "edge", x1: ax, y1: ay, x2: bx, y2: by,
      "marker-end": "url(#arrow)",
    }));
  }

  // 节点
  for (const o of CURRENT.spec.objects) {
    const [x, y] = pos[o];
    const isAlive = alive.has(o);
    const color = snap.colors[o] || "dead";
    const g = el("g", {});
    g.appendChild(el("circle", {
      class: `node-circle node-${isAlive ? color : "dead"}`,
      cx: x, cy: y, r: 18,
    }));
    const lblClass = isAlive && color === "black" ? "node-label-black" : "node-label";
    g.appendChild(el("text", { x, y: y + 4.5, "text-anchor": "middle", class: lblClass }, o));
    if (snap.roots.includes(o)) {
      g.appendChild(el("text", { x, y: y - 26, "text-anchor": "middle",
        class: "root-mark", "font-size": 15 }, "◆"));
    }
    if (snap.gray_queue.includes(o)) {
      g.appendChild(el("text", { x: x + 20, y: y + 5,
        "font-size": 11, fill: "#e0b341" }, "G"));
    }
    svg.appendChild(g);
  }
}

function renderEvidence(evidence) {
  const ol = $("evidence");
  ol.innerHTML = "";
  for (const e of evidence) {
    const li = document.createElement("li");
    if (e.kind === "barrier_retain") {
      li.className = "barrier";
      li.innerHTML = `[${e.seq}] <b>屏障保留</b> ${e.type}：白色目标 ` +
        `<b>${e.objects.join(", ")}</b> 染灰，本周期不回收` +
        (e.rescanned && e.rescanned.length ? `；${e.rescanned.join(", ")} 退回灰色重扫` : "");
    } else if (e.kind === "scan") {
      li.textContent = `[${e.seq}] 扫描灰对象 ${e.scanned}` +
        (e.new_gray.length ? `，新染灰：${e.new_gray.join(", ")}` : "（无白色目标）");
    } else if (e.kind === "roots_gray") {
      li.textContent = `[${e.seq}] 启动标记，根置灰：${e.objects.join(", ") || "（无存活根）"}`;
    } else if (e.kind === "sweep") {
      li.className = "sweep";
      li.textContent = `[${e.seq}] 清扫：回收 ${e.reclaimed.length ? e.reclaimed.join(", ") : "（无）"}` +
        `；存活 ${e.alive.join(", ")}`;
    } else if (e.kind === "reopen") {
      li.className = "reopen";
      li.textContent = `[${e.seq}] 标记中途重开：保持 ${e.phase}，灰队列 ${e.gray_queue.join(", ") || "（空）"}，颜色不变`;
    }
    ol.appendChild(li);
  }
}

function renderLedger() {
  const ol = $("ledger");
  ol.innerHTML = "";
  TIMELINE.frames.forEach((frame, i) => {
    if (i === 0) return;
    const li = document.createElement("li");
    li.dataset.idx = String(i);
    li.className = frame.accepted ? "accepted" : "rejected";
    const tag = frame.accepted ? "✓" : "✗ 拒绝";
    li.innerHTML = `<span class="seq">#${frame.index}</span>` +
      `${frame.event_id} · ${frame.type} ${tag}` +
      (frame.accepted ? "" : ` <b>(${frame.reason})</b>`);
    li.title = JSON.stringify(frame.accepted ? (frame.result || {})
      : { reason: frame.reason, detail: frame.detail }, null, 2);
    li.style.cursor = "pointer";
    li.addEventListener("click", () => { frameIdx = i; stopPlay(); renderFrame(); });
    ol.appendChild(li);
  });
}

function highlightLedger(frame) {
  for (const li of $("ledger").children) {
    li.style.background = Number(li.dataset.idx) === frameIdx ? "#1d2c44" : "";
  }
}

function renderRejectBanner(rej) {
  const banner = $("reject-banner");
  if (!rej) { banner.hidden = true; return; }
  banner.hidden = false;
  banner.innerHTML = `🚫 首个拒因（事件 <b>${rej.event_id}</b> · ${rej.type}）：` +
    `<b>${rej.reason}</b><pre class="code">${escapeHtml(JSON.stringify(rej.detail, null, 2))}</pre>` +
    `该拒单仅落账本回放，不改变任何颜色/队列/存活状态。`;
}

function renderRecoveryBanner(report) {
  const banner = $("recovery-banner");
  if (!report) { banner.hidden = true; return; }
  banner.hidden = false;
  if (report.consistent) {
    banner.innerHTML = `✅ 重开 ${report.reopen_count} 次：中断恢复后的存活裁决与连续运行` +
      ` <b>完全一致</b>（阶段/颜色/灰队列/存活/回收/证据链）。`;
  } else {
    banner.innerHTML = "❌ 恢复裁决不一致！";
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// ---------------------------------------------------------------------------
// 回放控制
// ---------------------------------------------------------------------------

function stopPlay() {
  if (playTimer) { clearInterval(playTimer); playTimer = null; }
  $("btn-play").textContent = "整段回放 ▶▶";
}

$("btn-first").addEventListener("click", () => { stopPlay(); frameIdx = 0; renderFrame(); });
$("btn-prev").addEventListener("click", () => { stopPlay(); frameIdx = Math.max(0, frameIdx - 1); renderFrame(); });
$("btn-next").addEventListener("click", () => {
  stopPlay();
  frameIdx = Math.min(TIMELINE.frames.length - 1, frameIdx + 1);
  renderFrame();
});
$("btn-last").addEventListener("click", () => {
  stopPlay();
  frameIdx = TIMELINE.frames.length - 1; renderFrame();
});
$("frame-slider").addEventListener("input", (e) => {
  stopPlay(); frameIdx = Number(e.target.value); renderFrame();
});
$("btn-play").addEventListener("click", () => {
  if (playTimer) { stopPlay(); return; }
  $("btn-play").textContent = "暂停 ⏸";
  playTimer = setInterval(() => {
    if (frameIdx >= TIMELINE.frames.length - 1) { stopPlay(); return; }
    frameIdx += 1;
    renderFrame();
  }, 900);
});

// ---------------------------------------------------------------------------
// 投递 / 导入 / 恢复比对
// ---------------------------------------------------------------------------

$("drill-select").addEventListener("change", (e) => openDrill(e.target.value));

$("deliver-btn").addEventListener("click", async () => {
  const msg = $("deliver-msg");
  const id = $("ev-id").value.trim();
  const type = $("ev-type").value;
  let payload = {};
  const raw = $("ev-payload").value.trim();
  if (raw) {
    try { payload = JSON.parse(raw); }
    catch (err) { msg.className = "msg err"; msg.textContent = "payload 不是合法 JSON"; return; }
  }
  const { ok, data } = await api(`/api/drills/${CURRENT.id}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, type, payload }),
  });
  if (data && data.replayed) {
    msg.className = "msg ok";
    msg.textContent = `重复投递：回放原结果（accepted=${data.accepted}${data.reason ? ", " + data.reason : ""}）`;
  } else if (ok) {
    msg.className = "msg ok";
    msg.textContent = "已接受并持久化";
  } else {
    msg.className = "msg err";
    msg.textContent = `被拒绝：${data && data.reason} ${JSON.stringify((data && data.detail) || {})}`;
  }
  await openDrill(CURRENT.id);
});

$("import-btn").addEventListener("click", async () => {
  const msg = $("import-msg");
  let spec;
  try { spec = JSON.parse($("import-json").value); }
  catch (err) { msg.className = "msg err"; msg.textContent = "不是合法 JSON"; return; }
  const { ok, data } = await api("/api/drills", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(spec),
  });
  if (!ok) {
    msg.className = "msg err";
    msg.textContent = `导入被拒：${data && data.reason} ${JSON.stringify((data && data.detail) || {})}`;
    return;
  }
  msg.className = "msg ok";
  msg.textContent = `已导入 ${data.id}`;
  await loadDrills(data.id);
  await openDrill(data.id);
});

$("recovery-btn").addEventListener("click", async () => {
  const { data } = await api(`/api/drills/${CURRENT.id}/recovery`);
  $("recovery-out").textContent = JSON.stringify(data, null, 2);
  renderRecoveryBanner(data);
});

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------

(async function main() {
  const id = await loadDrills();
  if (id) await openDrill(id);
})();
