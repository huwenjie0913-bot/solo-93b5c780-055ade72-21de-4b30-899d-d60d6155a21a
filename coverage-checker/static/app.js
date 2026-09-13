/* 航片覆盖校核台 —— 标准 DOM + Canvas 2D，无框架。 */
"use strict";

const $ = (s) => document.querySelector(s);
const cv = $("#cv"), ctx = cv.getContext("2d");

const STRIP_COLORS = ["#2563eb", "#059669", "#d97706", "#dc2626", "#7c3aed",
  "#0891b2", "#be185d", "#65a30d", "#9333ea", "#0d9488"];
const RISK_COLORS = { gap: "#dc2626", low_forward: "#f59e0b", duplicate: "#8b5cf6",
  gap_side: "#0ea5e9", low_side: "#0ea5e9", no_gps: "#64748b" };
const TYPE_CN = { gap: "断带", low_forward: "前向重叠不足", duplicate: "疑似重复拍摄",
  gap_side: "旁向断带", low_side: "旁向重叠不足", no_gps: "缺少坐标" };

let projects = [];
let cur = null;            // 当前项目数据 {project, photos, strips, risks}
let sel = null;            // {kind:'risk'|'photo', id}
let view = { cx: 0, cy: 0, scale: 1 };  // 世界(米)→屏幕
let photoById = new Map();

/* ---------- 投影（与后端一致的局部切平面） ---------- */
let origin = { lat0: 0, lon0: 0 };
function toXY(lat, lon) {
  return [ (lon - origin.lon0) * 111320 * Math.cos(origin.lat0 * Math.PI / 180),
           (lat - origin.lat0) * 110540 ];
}
function w2s(x, y) { return [ cv.width / 2 + (x - view.cx) * view.scale,
                              cv.height / 2 - (y - view.cy) * view.scale ]; }
function s2w(px, py) { return [ view.cx + (px - cv.width / 2) / view.scale,
                                view.cy - (py - cv.height / 2) / view.scale ]; }

/* ---------- 数据加载 ---------- */
async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}
async function loadProjects() {
  projects = await api("/api/projects");
  const selEl = $("#projectSelect");
  selEl.innerHTML = projects.map(p =>
    `<option value="${p.id}">#${p.id} ${esc(p.name)}（${p.photo_count}张）</option>`).join("");
  if (projects.length && !projects.some(p => cur && p.id === cur.project.id)) {
    await openProject(projects[0].id);
  } else if (cur) {
    selEl.value = cur.project.id;
  }
  updateExportLinks();
}
async function openProject(pid) {
  cur = await api(`/api/projects/${pid}`);
  photoById = new Map(cur.photos.map(p => [p.id, p]));
  $("#projectSelect").value = pid;
  sel = null;
  computeOrigin();
  fitView();
  renderAll();
  updateExportLinks();
}
async function refresh() {
  if (!cur) return;
  cur = await api(`/api/projects/${cur.project.id}`);
  photoById = new Map(cur.photos.map(p => [p.id, p]));
  computeOrigin();
  renderAll();
}
function computeOrigin() {
  const pts = cur.photos.filter(p => p.lat != null);
  if (!pts.length) { origin = { lat0: 0, lon0: 0 }; return; }
  origin = {
    lat0: pts.reduce((s, p) => s + p.lat, 0) / pts.length,
    lon0: pts.reduce((s, p) => s + p.lon, 0) / pts.length,
  };
}
function updateExportLinks() {
  const pid = cur ? cur.project.id : 0;
  $("#btnCsv").href = `/api/projects/${pid}/report.csv`;
  $("#btnJson").href = `/api/projects/${pid}/report.json`;
}

/* ---------- 视图 ---------- */
function fitView() {
  const pts = cur ? cur.photos.filter(p => p.lat != null) : [];
  if (!pts.length) { view = { cx: 0, cy: 0, scale: 1 }; return; }
  const xy = pts.map(p => toXY(p.lat, p.lon));
  const xs = xy.map(a => a[0]), ys = xy.map(a => a[1]);
  const m = Math.max(...pts.map(p => Math.max(p.fp_w || 0, p.fp_l || 0)), 10);
  const minX = Math.min(...xs) - m, maxX = Math.max(...xs) + m;
  const minY = Math.min(...ys) - m, maxY = Math.max(...ys) + m;
  view.cx = (minX + maxX) / 2; view.cy = (minY + maxY) / 2;
  view.scale = Math.min(cv.width / (maxX - minX || 1), cv.height / (maxY - minY || 1));
}
function resize() {
  const r = cv.parentElement.getBoundingClientRect();
  cv.width = r.width; cv.height = r.height;
  draw();
}

/* ---------- 绘制 ---------- */
function draw() {
  if (!cv.width) return;
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!cur) { hint("新建项目并导入照片开始校核"); return; }
  if (!cur.photos.some(p => p.lat != null)) { hint("导入带 GPS 的照片后在此显示足迹与航迹"); return; }

  drawGrid();
  for (const s of cur.strips) drawStrip(s);
  for (const p of cur.photos) if (p.lat != null) drawFootprint(p);
  for (const r of cur.risks) drawRisk(r);
  for (const p of cur.photos) if (p.lat != null) drawDot(p);
}
function hint(t) {
  ctx.fillStyle = "#94a3b8"; ctx.font = "15px sans-serif"; ctx.textAlign = "center";
  ctx.fillText(t, cv.width / 2, cv.height / 2);
}
function drawGrid() {
  const step = niceStep(50 / view.scale);
  const [x0, y1] = s2w(0, 0), [x1, y0] = s2w(cv.width, cv.height);
  ctx.strokeStyle = "#dde5ec"; ctx.lineWidth = 1; ctx.beginPath();
  for (let x = Math.floor(x0 / step) * step; x <= x1; x += step) {
    const [px] = w2s(x, 0); ctx.moveTo(px, 0); ctx.lineTo(px, cv.height);
  }
  for (let y = Math.floor(y0 / step) * step; y <= y1; y += step) {
    const [, py] = w2s(0, y); ctx.moveTo(0, py); ctx.lineTo(cv.width, py);
  }
  ctx.stroke();
  ctx.fillStyle = "#94a3b8"; ctx.font = "10px sans-serif"; ctx.textAlign = "left";
  ctx.fillText(`网格 ${step} m`, 8, cv.height - 8);
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (p * m >= raw) return p * m;
  return p * 10;
}
function stripColor(id) { return STRIP_COLORS[(id - 1) % STRIP_COLORS.length]; }

function drawStrip(s) {
  const pts = s.photo_ids.map(id => photoById.get(id)).filter(p => p && p.lat != null);
  if (pts.length < 2) return;
  ctx.strokeStyle = stripColor(s.id); ctx.lineWidth = 2; ctx.globalAlpha = .8;
  ctx.beginPath();
  pts.forEach((p, i) => {
    const [x, y] = w2s(...toXY(p.lat, p.lon));
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke(); ctx.globalAlpha = 1;
  const [lx, ly] = w2s(...toXY(pts[0].lat, pts[0].lon));
  ctx.fillStyle = stripColor(s.id); ctx.font = "bold 12px sans-serif"; ctx.textAlign = "left";
  ctx.fillText(`航带${s.id}`, lx + 6, ly - 8);
}

function drawFootprint(p) {
  const [cx, cy] = w2s(...toXY(p.lat, p.lon));
  const w = (p.fp_w || 0) * view.scale, l = (p.fp_l || 0) * view.scale;
  const a = (p.heading || 0) * Math.PI / 180;
  ctx.save();
  ctx.translate(cx, cy); ctx.rotate(a);
  const isSel = sel && sel.kind === "photo" && sel.id === p.id;
  if (p.excluded) {
    ctx.fillStyle = "rgba(209,213,219,.25)"; ctx.strokeStyle = "#d1d5db";
  } else {
    ctx.fillStyle = hexA(stripColor(p.strip_id || 1), .13);
    ctx.strokeStyle = isSel ? "#111827" : hexA(stripColor(p.strip_id || 1), .8);
  }
  ctx.lineWidth = isSel ? 2.5 : 1;
  ctx.beginPath(); ctx.rect(-w / 2, -l / 2, w, l); ctx.fill(); ctx.stroke();
  ctx.restore();
}

function drawDot(p) {
  const [x, y] = w2s(...toXY(p.lat, p.lon));
  ctx.beginPath(); ctx.arc(x, y, p.excluded ? 2.5 : 3.5, 0, 7);
  ctx.fillStyle = p.excluded ? "#cbd5e1" : stripColor(p.strip_id || 1);
  ctx.fill();
  if (p.manual) { ctx.strokeStyle = "#e11d48"; ctx.lineWidth = 1.5; ctx.stroke(); }
  if (view.scale > 0.25 && !p.excluded) {
    ctx.fillStyle = "#334155"; ctx.font = "10px sans-serif"; ctx.textAlign = "left";
    ctx.fillText(`#${p.id}`, x + 5, y + 3);
  }
}

function riskEndpoints(r) {
  if (r.lat == null) return null;
  const [mx, my] = toXY(r.lat, r.lon);
  if (r.photos.length >= 2 && r.type !== "no_gps") {
    const a = photoById.get(r.photos[0]), b = photoById.get(r.photos[1]);
    if (a && b && a.lat != null && b.lat != null)
      return { a: toXY(a.lat, a.lon), b: toXY(b.lat, b.lon), mid: [mx, my] };
  }
  return { a: null, b: null, mid: [mx, my] };
}

function drawRisk(r) {
  const ep = riskEndpoints(r);
  if (!ep) return;
  const c = RISK_COLORS[r.type] || "#dc2626";
  const [mx, my] = w2s(...ep.mid);
  ctx.save();
  ctx.strokeStyle = c; ctx.fillStyle = c;
  if (ep.a && ep.b) {
    const [ax, ay] = w2s(...ep.a), [bx, by] = w2s(...ep.b);
    ctx.lineWidth = r.type === "duplicate" ? 3 : 5; ctx.globalAlpha = .85;
    ctx.setLineDash(r.type === "duplicate" ? [4, 4] : []);
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    ctx.setLineDash([]);
  }
  const isSel = sel && sel.kind === "risk" && sel.id === r.id;
  ctx.globalAlpha = 1;
  ctx.beginPath(); ctx.arc(mx, my, isSel ? 10 : 7, 0, 7);
  ctx.fillStyle = hexA(c, .9); ctx.fill();
  ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.stroke();
  if (isSel) { ctx.strokeStyle = c; ctx.beginPath(); ctx.arc(mx, my, 14, 0, 7); ctx.stroke(); }
  ctx.fillStyle = "#fff"; ctx.font = "bold 9px sans-serif"; ctx.textAlign = "center";
  ctx.fillText("!", mx, my + 3);
  ctx.restore();
}
function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}

/* ---------- 交互 ---------- */
let drag = null;
cv.addEventListener("mousedown", e => { drag = { x: e.offsetX, y: e.offsetY, moved: false }; });
cv.addEventListener("mousemove", e => {
  if (!drag) return;
  const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  view.cx -= dx / view.scale; view.cy += dy / view.scale;
  drag.x = e.offsetX; drag.y = e.offsetY;
  draw();
});
cv.addEventListener("mouseup", e => {
  if (drag && !drag.moved) pick(e.offsetX, e.offsetY);
  drag = null;
});
cv.addEventListener("mouseleave", () => drag = null);
cv.addEventListener("wheel", e => {
  e.preventDefault();
  const [wx, wy] = s2w(e.offsetX, e.offsetY);
  view.scale *= e.deltaY < 0 ? 1.15 : 1 / 1.15;
  view.scale = Math.min(200, Math.max(0.001, view.scale));
  const [nx, ny] = s2w(e.offsetX, e.offsetY);
  view.cx += wx - nx; view.cy += wy - ny;
  draw();
}, { passive: false });
cv.addEventListener("dblclick", () => { fitView(); draw(); });

function pick(px, py) {
  if (!cur) return;
  // 先命中风险标记
  let best = null, bd = 16;
  for (const r of cur.risks) {
    const ep = riskEndpoints(r);
    if (!ep) continue;
    const [mx, my] = w2s(...ep.mid);
    const d = Math.hypot(mx - px, my - py);
    if (d < bd) { bd = d; best = { kind: "risk", id: r.id }; }
  }
  if (best) { sel = best; renderAll(); return; }
  // 再命中照片（中心点 8px 内）
  best = null; bd = 10;
  for (const p of cur.photos) {
    if (p.lat == null) continue;
    const [x, y] = w2s(...toXY(p.lat, p.lon));
    const d = Math.hypot(x - px, y - py);
    if (d < bd) { bd = d; best = { kind: "photo", id: p.id }; }
  }
  sel = best;
  renderAll();
}

/* ---------- 侧栏渲染 ---------- */
function renderAll() {
  draw();
  renderRiskList();
  renderDetail();
}
function renderRiskList() {
  const ul = $("#riskList");
  $("#riskCount").textContent = cur ? `（${cur.risks.length}）` : "";
  if (!cur || !cur.risks.length) {
    ul.innerHTML = `<li style="border-color:#16a34a">未发现风险区段 ✓</li>`;
    return;
  }
  ul.innerHTML = cur.risks.map(r =>
    `<li class="${r.severity} ${sel && sel.kind === "risk" && sel.id === r.id ? "sel" : ""}"
         data-id="${r.id}"><span class="tag ${r.severity}">${TYPE_CN[r.type]}</span>${esc(r.label)}</li>`).join("");
  ul.querySelectorAll("li[data-id]").forEach(li =>
    li.onclick = () => { sel = { kind: "risk", id: +li.dataset.id }; renderAll(); });
}

function photoCard(p) {
  const coord = p.lat != null ? `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}` : "无坐标";
  return `<div class="photo-card">
    <img src="/api/photos/${p.id}/image" loading="lazy" alt="">
    <div class="pc-body">
      <b>#${p.id}</b> ${p.strip_id ? `航带${p.strip_id}-${String(p.strip_seq).padStart(2, "0")}` : "未入带"}<br>
      ${esc(p.filename)}<br>${coord}${p.manual ? ' <span class="tag info">人工补正</span>' : ""}<br>
      ${p.taken_at ? esc(p.taken_at) : "无拍摄时间"}
    </div>
    <button data-act="exclude" data-id="${p.id}">${p.excluded ? "恢复" : "排除"}</button>
    <button data-act="correct" data-id="${p.id}">补正坐标</button>
  </div>`;
}

function renderDetail() {
  const body = $("#detailBody"), title = $("#detailTitle");
  if (!sel || !cur) {
    title.textContent = "详情";
    body.innerHTML = `<p class="muted">在画布或列表中选择风险区段 / 照片。</p>`;
    return;
  }
  if (sel.kind === "photo") {
    const p = photoById.get(sel.id);
    if (!p) { sel = null; renderDetail(); return; }
    title.textContent = `照片 #${p.id}`;
    body.innerHTML = `
      <div class="photo-pair">${photoCard(p)}</div>
      <dl class="kv">
        <dt>图像尺寸</dt><dd>${p.img_w ?? "?"} × ${p.img_h ?? "?"} px</dd>
        <dt>焦距</dt><dd>${p.focal} mm${p.focal ? "" : "（默认）"}</dd>
        <dt>地面足迹</dt><dd>${p.fp_w ? `${p.fp_w.toFixed(1)} × ${p.fp_l.toFixed(1)} m（旁向×航向）` : "—"}</dd>
        <dt>航向角</dt><dd>${p.heading != null ? p.heading.toFixed(1) + "°" : "—"}</dd>
        <dt>EXIF 坐标</dt><dd>${p.exif_lat != null ? `${p.exif_lat.toFixed(6)}, ${p.exif_lon.toFixed(6)}` : "无"}</dd>
        <dt>状态</dt><dd>${p.excluded ? "已排除（不参与计算）" : "参与计算"}</dd>
      </dl>`;
  } else {
    const r = cur.risks.find(x => x.id === sel.id);
    if (!r) { sel = null; renderDetail(); return; }
    title.textContent = `风险 #${r.id}：${TYPE_CN[r.type]}`;
    const photos = r.photos.map(id => photoById.get(id)).filter(Boolean);
    const m = r.metrics;
    body.innerHTML = `
      <p><span class="tag ${r.severity}">${{ high: "高风险", mid: "注意", info: "提示" }[r.severity]}</span>${esc(r.label)}</p>
      <div class="photo-pair">${photos.map(photoCard).join("")}</div>
      ${Object.keys(m).length ? `<dl class="kv">${Object.entries(m).map(([k, v]) =>
        `<dt>${METRIC_CN[k] || k}</dt><dd>${v}</dd>`).join("")}</dl>` : ""}
      <div class="explain"><b>计算依据：</b>${esc(r.explain)}</div>
      ${r.reshoot ? `<div class="reshoot"><b>建议重拍范围：</b>照片 #${r.reshoot.from}
        (${r.reshoot.from_coord.map(v => v.toFixed(6)).join(", ")})
        → #${r.reshoot.to} (${r.reshoot.to_coord.map(v => v.toFixed(6)).join(", ")}) 之间区段。</div>` : ""}`;
  }
  body.querySelectorAll("button[data-act]").forEach(b => {
    b.onclick = () => photoAction(b.dataset.act, +b.dataset.id);
  });
}
const METRIC_CN = { dist_m: "摄站间距 (m)", footprint_l_m: "航向足迹 (m)",
  forward_overlap_pct: "前向重叠率 (%)", threshold_pct: "阈值 (%)",
  strip_spacing_m: "航带间距 (m)", footprint_w_m: "旁向足迹 (m)",
  side_overlap_pct: "旁向重叠率 (%)" };

async function photoAction(act, id) {
  const p = photoById.get(id);
  if (act === "exclude") {
    await api(`/api/photos/${id}/exclude`, postJson({ excluded: !p.excluded }));
    toast(p.excluded ? `已恢复照片 #${id}` : `已排除照片 #${id}`);
    await refresh();
  } else if (act === "correct") {
    openCorrectDialog(p);
  }
}

/* ---------- 对话框 ---------- */
const projectDialog = $("#projectDialog"), correctDialog = $("#correctDialog");
let editingParams = false, correctingId = null;

$("#btnNewProject").onclick = () => {
  editingParams = false;
  $("#projectDialogTitle").textContent = "新建项目";
  $("#projectForm").reset();
  projectDialog.showModal();
};
$("#btnParams").onclick = () => {
  if (!cur) return toast("请先新建项目");
  editingParams = true;
  $("#projectDialogTitle").textContent = "航摄参数（保存后立即重算）";
  const f = $("#projectForm"), p = cur.project;
  for (const k of ["name", "altitude", "sensor_w", "sensor_h", "focal_default", "fwd_min", "side_min"])
    f.elements[k].value = p[k];
  projectDialog.showModal();
};
$("#projectForm").addEventListener("submit", async e => {
  const fd = new FormData(e.target);
  const data = Object.fromEntries(fd.entries());
  for (const k of ["altitude", "sensor_w", "sensor_h", "focal_default", "fwd_min", "side_min"])
    data[k] = parseFloat(data[k]);
  if (editingParams) {
    await api(`/api/projects/${cur.project.id}`, { method: "PUT", ...postJson(data) });
    toast("参数已更新，风险区段已重算");
    await openProject(cur.project.id);
  } else {
    const r = await api("/api/projects", postJson(data));
    await loadProjects();
    await openProject(r.id);
    toast("项目已创建，请导入照片");
  }
});

function openCorrectDialog(p) {
  correctingId = p.id;
  $("#correctPhotoId").textContent = `#${p.id}（${p.filename}）`;
  const f = $("#correctForm");
  f.elements.lat.value = p.lat != null ? p.lat.toFixed(7) : "";
  f.elements.lon.value = p.lon != null ? p.lon.toFixed(7) : "";
  f.elements.note.value = "";
  correctDialog.showModal();
}
$("#correctForm").addEventListener("submit", async e => {
  const btn = e.submitter && e.submitter.value;
  if (btn === "cancel") return;
  if (btn === "clear") {
    await api(`/api/photos/${correctingId}/correct`, postJson({ clear: true }));
    toast("已清除人工补正");
  } else {
    const fd = new FormData(e.target);
    await api(`/api/photos/${correctingId}/correct`, postJson({
      lat: parseFloat(fd.get("lat")), lon: parseFloat(fd.get("lon")), note: fd.get("note") }));
    toast("坐标已补正，风险区段已重算");
  }
  await refresh();
});

/* ---------- 上传 ---------- */
$("#fileInput").addEventListener("change", async e => {
  if (!cur) { toast("请先新建项目"); return; }
  const files = [...e.target.files];
  if (!files.length) return;
  const fd = new FormData();
  files.forEach(f => fd.append("files", f));
  toast(`正在导入 ${files.length} 张照片…`);
  try {
    const r = await api(`/api/projects/${cur.project.id}/photos`, { method: "POST", body: fd });
    const noGps = r.saved.filter(s => !s.has_gps).length;
    toast(`导入 ${r.saved.length} 张` + (noGps ? `，其中 ${noGps} 张无 GPS 需补正` : "")
      + (r.failed.length ? `，${r.failed.length} 张无法解析` : ""));
    cur = r; photoById = new Map(cur.photos.map(p => [p.id, p]));
    computeOrigin(); fitView(); renderAll(); loadProjects();
  } catch (err) { toast("导入失败：" + err.message); }
  e.target.value = "";
});

$("#projectSelect").addEventListener("change", e => openProject(+e.target.value));

/* ---------- 工具 ---------- */
function postJson(obj) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) };
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

/* ---------- 启动 ---------- */
window.addEventListener("resize", resize);
resize();
loadProjects();
