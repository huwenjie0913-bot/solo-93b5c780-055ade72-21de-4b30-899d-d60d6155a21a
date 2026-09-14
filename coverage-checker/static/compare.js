/* 航次对比模块 —— 与 app.js 同为全局脚本，复用 api()/toast()/esc() 等工具。 */
"use strict";

const cmpCv = $("#cvCompare"), cmpCtx = cmpCv.getContext("2d");

const PAIR_CN = { matched: "对应照片", shifted: "位置偏移", new_gps: "补拍新增",
  missing: "补拍缺失", missing_gps: "坐标仍缺失" };
const RISK_DIFF_CN = { resolved: "风险已解决", new: "新增风险", persisted: "风险仍存在" };
const DIFF_COLORS = {
  shifted: "#d97706", new_gps: "#16a34a", missing: "#dc2626", missing_gps: "#dc2626",
  resolved: "#22c55e", new: "#ef4444", persisted: "#f97316",
};
const BASE_COLOR = "#64748b";   // 基准航次：灰
const RESHOOT_COLOR = "#2563eb"; // 补拍航次：蓝

let cmpData = null;             // /compare 返回数据
let cmpSel = null;              // {kind:'pair'|'risk', idx}
let cmpView = { cx: 0, cy: 0, scale: 1 };
let cmpOrigin = { lat0: 0, lon0: 0 };
let cmpFilter = { strip: "all", type: "all" };
let cmpPreferredReshoot = "";   // 新导入后期望选中的补拍航次 id

/* ---------- 投影 ---------- */
function cmpToXY(lat, lon) {
  return [(lon - cmpOrigin.lon0) * 111320 * Math.cos(cmpOrigin.lat0 * Math.PI / 180),
          (lat - cmpOrigin.lat0) * 110540];
}
function cmpW2S(x, y) {
  return [cmpCv.width / 2 + (x - cmpView.cx) * cmpView.scale,
          cmpCv.height / 2 - (y - cmpView.cy) * cmpView.scale];
}
function cmpS2W(px, py) {
  return [cmpView.cx + (px - cmpCv.width / 2) / cmpView.scale,
          cmpView.cy - (py - cmpCv.height / 2) / cmpView.scale];
}

/* ---------- 差异项统一视图 ---------- */
function diffItems() {
  if (!cmpData) return [];
  const items = [];
  cmpData.pairs.forEach((p, idx) => {
    if (p.status === "matched") return;
    items.push({ kind: "pair", idx, status: p.status,
      strip: (p.baseline_photo && p.baseline_photo.strip_id) || null,
      lat: p.lat, lon: p.lon });
  });
  cmpData.risk_entries.forEach((e, idx) => {
    items.push({ kind: "risk", idx, status: e.status === "new" ? "new"
        : e.status === "resolved" ? "resolved" : "persisted",
      strip: null,
      rtype: (e.reshoot_risk || e.baseline_risk).type,
      lat: e.lat, lon: e.lon });
  });
  return items;
}

function pairStripMatch(p) {
  if (cmpFilter.strip === "all") return true;
  const b = p.baseline_photo, n = p.reshoot_photo;
  return (b && String(b.strip_id) === cmpFilter.strip)
      || (n && String(n.strip_id) === cmpFilter.strip);
}

function pairPassesFilter(p) {
  const t = cmpFilter.type;
  if (t !== "all") {
    if (t === "shifted" && p.status !== "shifted") return false;
    if (t === "new" && p.status !== "new_gps") return false;
    if (t === "missing" && !["missing", "missing_gps"].includes(p.status)) return false;
    if (["risk_resolved", "risk_new", "risk_persisted"].includes(t)) return false;
  }
  return pairStripMatch(p);
}

function riskPassesFilter(e) {
  const t = cmpFilter.type;
  if (t === "shifted" || t === "new" || t === "missing") return false;
  if (t === "risk_resolved" && e.status !== "resolved") return false;
  if (t === "risk_new" && e.status !== "new") return false;
  if (t === "risk_persisted" && e.status !== "persisted") return false;
  if (cmpFilter.strip !== "all") {
    const br = e.baseline_risk, nr = e.reshoot_risk;
    const inB = br && br.strips.map(String).includes(cmpFilter.strip);
    const inN = nr && nr.strips.map(String).includes(cmpFilter.strip);
    if (!inB && !inN) return false;
  }
  return true;
}

/* ---------- 加载 ---------- */
async function loadCompare() {
  const pid = cur.project.id;
  let sorties;
  try {
    sorties = await api(`/api/projects/${pid}/sorties`);
  } catch (e) { sorties = []; }
  fillSortieUi(sorties);
  const rid = $("#cmpReshootSelect").value;
  if (!sorties.some(s => s.kind === "baseline")) {
    cmpData = null;
    renderCompareAll();
    return;
  }
  if (!rid || !sorties.some(s => String(s.id) === rid && s.kind === "reshoot")) {
    cmpData = null;
    renderCompareAll();
    return;
  }
  try {
    cmpData = await api(`/api/projects/${pid}/compare?reshoot_id=${rid}`);
  } catch (e) {
    cmpData = null;
    toast("对比失败：" + e.message);
  }
  cmpSel = null;
  computeCmpOrigin();
  cmpFitView();
  buildStripFilter();
  renderCompareAll();
}

function fillSortieUi(sorties) {
  const base = sorties.find(s => s.kind === "baseline");
  $("#btnSaveBaseline").textContent = base ? "重新保存基准航次" : "保存当前项目为基准航次";
  const sel = $("#cmpReshootSelect");
  const reshoots = sorties.filter(s => s.kind === "reshoot");
  const prev = sel.value;
  sel.innerHTML = reshoots.length
    ? reshoots.map(s => `<option value="${s.id}">#${s.id} ${esc(s.label)}（${s.photo_count}张 ${s.created_at}）</option>`).join("")
    : `<option value="">（未导入补拍）</option>`;
  const want = cmpPreferredReshoot;
  cmpPreferredReshoot = "";
  if (want && reshoots.some(s => String(s.id) === want)) sel.value = want;
  else if (prev && reshoots.some(s => String(s.id) === prev)) sel.value = prev;
  $("#btnDeleteSortie").disabled = !reshoots.length;
  const pid = cur.project.id;
  const rid = sel.value;
  $("#btnCmpCsv").href = rid
    ? `/api/projects/${pid}/compare/report.csv?reshoot_id=${rid}` : "#";
  $("#btnCmpJson").href = rid
    ? `/api/projects/${pid}/compare/report.json?reshoot_id=${rid}` : "#";
}

function buildStripFilter() {
  const sel = $("#cmpStripFilter");
  const prev = cmpFilter.strip;
  let html = `<option value="all">全部航带</option>`;
  if (cmpData) {
    const bStrips = new Set(), nStrips = new Set();
    for (const p of cmpData.pairs) {
      if (p.baseline_photo && p.baseline_photo.strip_id) bStrips.add(p.baseline_photo.strip_id);
      if (p.reshoot_photo && p.reshoot_photo.strip_id) nStrips.add(p.reshoot_photo.strip_id);
    }
    const all = [...new Set([...bStrips, ...nStrips])].sort((a, b) => a - b);
    for (const s of all) {
      const mapped = cmpData.strip_map[String(s)];
      html += `<option value="${s}">航带 ${s}${mapped ? ` ↔ 补拍${mapped}` : ""}</option>`;
    }
  }
  sel.innerHTML = html;
  sel.value = [...sel.options].some(o => o.value === prev) ? prev : "all";
  cmpFilter.strip = sel.value;
}

function computeCmpOrigin() {
  if (!cmpData) { cmpOrigin = { lat0: 0, lon0: 0 }; return; }
  const pts = [];
  for (const pr of cmpData.pairs) {
    for (const k of ["baseline_photo", "reshoot_photo"]) {
      const p = pr[k];
      if (p && p.lat != null) pts.push(p);
    }
  }
  if (!pts.length) { cmpOrigin = { lat0: 0, lon0: 0 }; return; }
  cmpOrigin = {
    lat0: pts.reduce((s, p) => s + p.lat, 0) / pts.length,
    lon0: pts.reduce((s, p) => s + p.lon, 0) / pts.length,
  };
}

function cmpFitView() {
  const pts = [];
  if (cmpData) for (const pr of cmpData.pairs) {
    for (const k of ["baseline_photo", "reshoot_photo"]) {
      const p = pr[k];
      if (p && p.lat != null) pts.push(p);
    }
  }
  if (!pts.length) { cmpView = { cx: 0, cy: 0, scale: 1 }; return; }
  const xy = pts.map(p => cmpToXY(p.lat, p.lon));
  const m = Math.max(...pts.map(p => Math.max(p.fp_w || 0, p.fp_l || 0)), 10);
  const minX = Math.min(...xy.map(a => a[0])) - m, maxX = Math.max(...xy.map(a => a[0])) + m;
  const minY = Math.min(...xy.map(a => a[1])) - m, maxY = Math.max(...xy.map(a => a[1])) + m;
  cmpView.cx = (minX + maxX) / 2; cmpView.cy = (minY + maxY) / 2;
  cmpView.scale = Math.min(cmpCv.width / (maxX - minX || 1), cmpCv.height / (maxY - minY || 1));
}

/* ---------- 绘制 ---------- */
function cmpDraw() {
  if (!cmpCv.width) return;
  cmpCtx.clearRect(0, 0, cmpCv.width, cmpCv.height);
  if (!cur) { cmpHint("请先选择项目"); return; }
  if (!cmpData) {
    cmpHint("保存基准航次并导入补拍航片后，在此叠加两次航摄足迹");
    return;
  }
  cmpDrawGrid();

  // 两次航次的足迹 + 航迹
  drawSortieLayer("baseline_photo", BASE_COLOR, true);
  drawSortieLayer("reshoot_photo", RESHOOT_COLOR, false);

  // 配对连线
  for (const p of cmpData.pairs) {
    if (!pairPassesFilter(p)) continue;
    if (!p.baseline_photo || !p.reshoot_photo) continue;
    if (p.baseline_photo.lat == null || p.reshoot_photo.lat == null) continue;
    const [ax, ay] = cmpW2S(...cmpToXY(p.baseline_photo.lat, p.baseline_photo.lon));
    const [bx, by] = cmpW2S(...cmpToXY(p.reshoot_photo.lat, p.reshoot_photo.lon));
    const c = DIFF_COLORS[p.status] || "#94a3b8";
    cmpCtx.save();
    cmpCtx.strokeStyle = c;
    cmpCtx.globalAlpha = p.status === "matched" ? .25 : .85;
    cmpCtx.lineWidth = p.status === "shifted" ? 2.5 : 1;
    cmpCtx.setLineDash(p.status === "matched" ? [2, 4] : [6, 4]);
    cmpCtx.beginPath(); cmpCtx.moveTo(ax, ay); cmpCtx.lineTo(bx, by); cmpCtx.stroke();
    cmpCtx.restore();
  }

  // 差异/风险标记
  diffItems().forEach((it, i) => {
    if (it.kind === "pair") {
      const p = cmpData.pairs[it.idx];
      if (pairPassesFilter(p)) drawPairMarker(p, it.idx);
    } else {
      const e = cmpData.risk_entries[it.idx];
      if (riskPassesFilter(e)) drawRiskMarker(e, it.idx);
    }
  });
}

function drawSortieLayer(key, color, dashed) {
  // 航迹线
  const strips = {};
  for (const pr of cmpData.pairs) {
    const p = pr[key];
    if (!p || p.lat == null || !p.strip_id) continue;
    if (!pairStripMatch(pr)) continue;
    (strips[p.strip_id] = strips[p.strip_id] || []).push(p);
  }
  for (const sid of Object.keys(strips)) {
    const arr = strips[sid].sort((a, b) => (a.strip_seq || 0) - (b.strip_seq || 0));
    if (arr.length < 2) continue;
    cmpCtx.save();
    cmpCtx.strokeStyle = color; cmpCtx.globalAlpha = .55; cmpCtx.lineWidth = 1.5;
    cmpCtx.setLineDash(dashed ? [5, 4] : []);
    cmpCtx.beginPath();
    arr.forEach((p, i) => {
      const [x, y] = cmpW2S(...cmpToXY(p.lat, p.lon));
      i ? cmpCtx.lineTo(x, y) : cmpCtx.moveTo(x, y);
    });
    cmpCtx.stroke();
    cmpCtx.restore();
  }
  // 足迹 + 摄站点
  for (const pr of cmpData.pairs) {
    const p = pr[key];
    if (!p || p.lat == null) continue;
    if (!pairStripMatch(pr)) continue;
    const [x, y] = cmpW2S(...cmpToXY(p.lat, p.lon));
    const w = (p.fp_w || 0) * cmpView.scale, l = (p.fp_l || 0) * cmpView.scale;
    const a = (p.heading || 0) * Math.PI / 180;
    cmpCtx.save();
    cmpCtx.translate(x, y); cmpCtx.rotate(a);
    cmpCtx.fillStyle = hexA(color, .08);
    cmpCtx.strokeStyle = hexA(color, .55); cmpCtx.lineWidth = 1;
    cmpCtx.beginPath(); cmpCtx.rect(-w / 2, -l / 2, w, l); cmpCtx.fill(); cmpCtx.stroke();
    cmpCtx.restore();
    cmpCtx.beginPath(); cmpCtx.arc(x, y, 3, 0, 7);
    cmpCtx.fillStyle = color; cmpCtx.fill();
  }
}

function isCmpSel(kind, idx) {
  return cmpSel && cmpSel.kind === kind && cmpSel.idx === idx;
}

function drawPairMarker(p, idx) {
  if (p.lat == null) return;
  const c = DIFF_COLORS[p.status] || "#dc2626";
  const [x, y] = cmpW2S(...cmpToXY(p.lat, p.lon));
  const sel = isCmpSel("pair", idx);
  cmpCtx.save();
  if (p.status === "new_gps") {
    // 补拍新增：蓝方点外绿色圈
    cmpCtx.strokeStyle = c; cmpCtx.lineWidth = 2;
    cmpCtx.beginPath(); cmpCtx.arc(x, y, sel ? 11 : 8, 0, 7); cmpCtx.stroke();
  } else if (p.status === "missing" || p.status === "missing_gps") {
    // 缺失：红色 X（missing_gps 无坐标，跳过）
    if (p.status === "missing_gps") { cmpCtx.restore(); return; }
    cmpCtx.strokeStyle = c; cmpCtx.lineWidth = 2.5;
    const r = sel ? 9 : 6;
    cmpCtx.beginPath();
    cmpCtx.moveTo(x - r, y - r); cmpCtx.lineTo(x + r, y + r);
    cmpCtx.moveTo(x + r, y - r); cmpCtx.lineTo(x - r, y + r);
    cmpCtx.stroke();
  } else {
    // 偏移：橙色菱形
    cmpCtx.fillStyle = c;
    const r = sel ? 9 : 6;
    cmpCtx.beginPath();
    cmpCtx.moveTo(x, y - r); cmpCtx.lineTo(x + r, y);
    cmpCtx.lineTo(x, y + r); cmpCtx.lineTo(x - r, y); cmpCtx.closePath();
    cmpCtx.fill();
  }
  if (sel) { cmpCtx.strokeStyle = "#111827"; cmpCtx.lineWidth = 1.5;
    cmpCtx.beginPath(); cmpCtx.arc(x, y, 14, 0, 7); cmpCtx.stroke(); }
  cmpCtx.restore();
}

function drawRiskMarker(e, idx) {
  if (e.lat == null) return;
  const c = DIFF_COLORS[e.status === "new" ? "new" : e.status === "resolved" ? "resolved" : "persisted"];
  const [x, y] = cmpW2S(...cmpToXY(e.lat, e.lon));
  const sel = isCmpSel("risk", idx);
  cmpCtx.save();
  // 仍存在/新增：两端连线（若两侧风险都有坐标）
  if (e.baseline_risk && e.reshoot_risk) {
    const [bx, by] = cmpW2S(...cmpToXY(e.baseline_risk.lat, e.baseline_risk.lon));
    const [nx, ny] = cmpW2S(...cmpToXY(e.reshoot_risk.lat, e.reshoot_risk.lon));
    cmpCtx.strokeStyle = c; cmpCtx.lineWidth = 1.5; cmpCtx.globalAlpha = .7;
    cmpCtx.setLineDash([3, 3]);
    cmpCtx.beginPath(); cmpCtx.moveTo(bx, by); cmpCtx.lineTo(nx, ny); cmpCtx.stroke();
    cmpCtx.setLineDash([]); cmpCtx.globalAlpha = 1;
  }
  cmpCtx.beginPath(); cmpCtx.arc(x, y, sel ? 10 : 7, 0, 7);
  cmpCtx.fillStyle = c; cmpCtx.fill();
  cmpCtx.lineWidth = 2; cmpCtx.strokeStyle = "#fff"; cmpCtx.stroke();
  cmpCtx.fillStyle = "#fff"; cmpCtx.font = "bold 10px sans-serif"; cmpCtx.textAlign = "center";
  cmpCtx.fillText(e.status === "resolved" ? "✓" : "!", x, y + 3.5);
  cmpCtx.restore();
}

function cmpHint(t) {
  cmpCtx.fillStyle = "#94a3b8"; cmpCtx.font = "15px sans-serif"; cmpCtx.textAlign = "center";
  cmpCtx.fillText(t, cmpCv.width / 2, cmpCv.height / 2);
}

function cmpDrawGrid() {
  const step = niceStep(50 / cmpView.scale);
  const [x0, y1] = cmpS2W(0, 0), [x1, y0] = cmpS2W(cmpCv.width, cmpCv.height);
  cmpCtx.strokeStyle = "#dde5ec"; cmpCtx.lineWidth = 1; cmpCtx.beginPath();
  for (let x = Math.floor(x0 / step) * step; x <= x1; x += step) {
    const [px] = cmpW2S(x, 0); cmpCtx.moveTo(px, 0); cmpCtx.lineTo(px, cmpCv.height);
  }
  for (let y = Math.floor(y0 / step) * step; y <= y1; y += step) {
    const [, py] = cmpW2S(0, y); cmpCtx.moveTo(0, py); cmpCtx.lineTo(cmpCv.width, py);
  }
  cmpCtx.stroke();
  cmpCtx.fillStyle = "#94a3b8"; cmpCtx.font = "10px sans-serif"; cmpCtx.textAlign = "left";
  cmpCtx.fillText(`网格 ${step} m`, 8, cmpCv.height - 8);
}

/* ---------- 画布交互 ---------- */
let cmpDrag = null;
cmpCv.addEventListener("mousedown", e => {
  cmpDrag = { x: e.offsetX, y: e.offsetY, moved: false };
});
cmpCv.addEventListener("mousemove", e => {
  if (!cmpDrag) return;
  const dx = e.offsetX - cmpDrag.x, dy = e.offsetY - cmpDrag.y;
  if (Math.abs(dx) + Math.abs(dy) > 3) cmpDrag.moved = true;
  cmpView.cx -= dx / cmpView.scale; cmpView.cy += dy / cmpView.scale;
  cmpDrag.x = e.offsetX; cmpDrag.y = e.offsetY;
  cmpDraw();
});
cmpCv.addEventListener("mouseup", e => {
  if (cmpDrag && !cmpDrag.moved) cmpPick(e.offsetX, e.offsetY);
  cmpDrag = null;
});
cmpCv.addEventListener("mouseleave", () => cmpDrag = null);
cmpCv.addEventListener("wheel", e => {
  e.preventDefault();
  const [wx, wy] = cmpS2W(e.offsetX, e.offsetY);
  cmpView.scale *= e.deltaY < 0 ? 1.15 : 1 / 1.15;
  cmpView.scale = Math.min(200, Math.max(0.001, cmpView.scale));
  const [nx, ny] = cmpS2W(e.offsetX, e.offsetY);
  cmpView.cx += wx - nx; cmpView.cy += wy - ny;
  cmpDraw();
}, { passive: false });
cmpCv.addEventListener("dblclick", () => { cmpFitView(); cmpDraw(); });

function cmpPick(px, py) {
  if (!cmpData) return;
  let best = null, bd = 14;
  diffItems().forEach(it => {
    if (it.lat == null) return;
    const pass = it.kind === "pair"
      ? pairPassesFilter(cmpData.pairs[it.idx])
      : riskPassesFilter(cmpData.risk_entries[it.idx]);
    if (!pass) return;
    const [x, y] = cmpW2S(...cmpToXY(it.lat, it.lon));
    const d = Math.hypot(x - px, y - py);
    if (d < bd) { bd = d; best = it; }
  });
  if (best) cmpSel = { kind: best.kind, idx: best.idx };
  renderCompareAll();
}

/* ---------- 侧栏 ---------- */
function renderCompareAll() {
  cmpDraw();
  renderCmpSummary();
  renderCmpDiffList();
  renderCmpDetail();
}

function renderCmpSummary() {
  const box = $("#cmpSummary");
  if (!cmpData) {
    box.innerHTML = `<p class="muted">先在单航次校核中完成照片导入、排除与坐标补正，
      再点击「保存当前项目为基准航次」冻结现场，随后导入补拍航片即可自动建立对应关系。</p>`;
    return;
  }
  const c = cmpData.counts;
  const b = cmpData.baseline, n = cmpData.reshoot;
  box.innerHTML = `
    <div class="summary-meta">
      基准：<b>${esc(b.label)}</b>（${esc(b.created_at)}，${b.photo_count} 张）<br>
      补拍：<b>${esc(n.label)}</b>（${esc(n.created_at)}，${n.photo_count} 张）<br>
      配对半径 ${cmpData.match_radius_m} m · 偏移阈值 ${cmpData.shift_threshold_m} m
    </div>
    <div class="summary-grid">
      <div class="cell">对应照片<br><b>${c.matched}</b></div>
      <div class="cell warn">位置偏移<br><b>${c.shifted}</b></div>
      <div class="cell ok">补拍新增<br><b>${c.new}</b></div>
      <div class="cell bad">补拍缺失<br><b>${c.missing}</b></div>
      <div class="cell ok">风险已解决<br><b>${c.risk_resolved}</b></div>
      <div class="cell warn">风险仍存在<br><b>${c.risk_persisted}</b></div>
      <div class="cell bad">新增风险<br><b>${c.risk_new}</b></div>
    </div>`;
}

function renderCmpDiffList() {
  $("#cmpDiffCount").textContent = cmpData ? "" : "";
  const ul = $("#cmpDiffList");
  if (!cmpData) { ul.innerHTML = ""; return; }
  const items = diffItems().filter(it => it.kind === "pair"
    ? pairPassesFilter(cmpData.pairs[it.idx])
    : riskPassesFilter(cmpData.risk_entries[it.idx]));
  $("#cmpDiffCount").textContent = `（${items.length}）`;
  if (!items.length) {
    ul.innerHTML = `<li style="border-color:#16a34a">当前筛选下无差异 ✓</li>`;
    return;
  }
  ul.innerHTML = items.map(it => {
    const selCls = isCmpSel(it.kind, it.idx) ? "sel" : "";
    if (it.kind === "pair") return pairLi(cmpData.pairs[it.idx], it.idx, selCls);
    return riskLi(cmpData.risk_entries[it.idx], it.idx, selCls);
  }).join("");
  ul.querySelectorAll("li[data-cmp]").forEach(li => {
    li.onclick = () => {
      cmpSel = { kind: li.dataset.kind, idx: +li.dataset.idx };
      renderCompareAll();
    };
  });
}

function pairLi(p, idx, selCls) {
  const c = DIFF_COLORS[p.status];
  const b = p.baseline_photo, n = p.reshoot_photo;
  let title, sub = "";
  if (p.status === "shifted") {
    title = `位置偏移 ${p.dist_m} m`;
    sub = `${esc(b.filename)} → ${esc(n.filename)}`;
  } else if (p.status === "new_gps") {
    title = `补拍新增：${esc(n.filename)}`;
    sub = n.strip_id ? `补拍航带 ${n.strip_id}` : "未入带";
  } else if (p.status === "missing_gps") {
    title = `坐标仍缺失：${esc(b.filename)}`;
  } else {
    title = `补拍缺失：${esc(b.filename)}`;
    sub = b.strip_id ? `基准航带 ${b.strip_id}` : "";
  }
  return `<li class="${selCls}" data-cmp="1" data-kind="pair" data-idx="${idx}" style="border-left-color:${c}">
    <span class="tag" style="background:${c}">${PAIR_CN[p.status]}</span>${esc(title)}
    ${sub ? `<span class="sub">${esc(sub)}</span>` : ""}</li>`;
}

function riskLi(e, idx, selCls) {
  const r = e.reshoot_risk || e.baseline_risk;
  const c = DIFF_COLORS[e.status === "new" ? "new" : e.status === "resolved" ? "resolved" : "persisted"];
  const tag = RISK_DIFF_CN[e.status];
  return `<li class="${selCls}" data-cmp="1" data-kind="risk" data-idx="${idx}" style="border-left-color:${c}">
    <span class="tag" style="background:${c}">${tag}</span>${esc(TYPE_CN[r.type] || r.type)}
    <span class="sub">${esc(r.label)}</span></li>`;
}

/* ---------- 详情：照片对 ---------- */
function sortiePhotoCard(p, sideLabel) {
  if (!p) {
    return `<div class="photo-card"><div class="pc-body"><span class="muted">${sideLabel}：无对应照片</span></div></div>`;
  }
  const coord = p.lat != null ? `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}` : "无坐标";
  const badge = sideLabel === "基准" ? "base" : "reshoot";
  return `<div class="photo-card">
    ${p.image_url ? `<img src="${p.image_url}" loading="lazy" alt="">` : ""}
    <div class="pc-body">
      <span class="badge ${badge}">${sideLabel}</span>
      <b>${esc(p.filename)}</b><br>
      ${p.strip_id ? `航带${p.strip_id}-${String(p.strip_seq).padStart(2, "0")}` : "未入带"}<br>
      ${coord}<br>
      ${p.taken_at ? esc(p.taken_at) : "无拍摄时间"}
    </div>
  </div>`;
}

function deltaHtml(v) {
  if (v == null) return "—";
  const cls = v > 0 ? "delta-up" : v < 0 ? "delta-down" : "";
  const sign = v > 0 ? "+" : "";
  return `<span class="${cls}">${sign}${v} pt</span>`;
}

function renderCmpDetail() {
  const body = $("#cmpDetailBody"), title = $("#cmpDetailTitle");
  if (!cmpData || !cmpSel) {
    title.textContent = "详情";
    body.innerHTML = `<p class="muted">在画布或差异列表中选择区段。</p>`;
    return;
  }
  if (cmpSel.kind === "pair") {
    const p = cmpData.pairs[cmpSel.idx];
    const b = p.baseline_photo, n = p.reshoot_photo;
    title.textContent = PAIR_CN[p.status];
    const overlapRows = (p.fwd_overlap_base != null || p.fwd_overlap_reshoot != null) ? `
      <dl class="kv">
        <dt>基准前向重叠</dt><dd>${p.fwd_overlap_base != null ? p.fwd_overlap_base + "%" : "—"}</dd>
        <dt>补拍前向重叠</dt><dd>${p.fwd_overlap_reshoot != null ? p.fwd_overlap_reshoot + "%" : "—"}</dd>
        <dt>重叠率变化</dt><dd>${deltaHtml(p.fwd_overlap_delta)}</dd>
        ${p.dist_m != null ? `<dt>摄站偏移</dt><dd>${p.dist_m} m（阈值 ${cmpData.shift_threshold_m} m）</dd>` : ""}
      </dl>` : (p.dist_m != null ? `<dl class="kv"><dt>摄站偏移</dt><dd>${p.dist_m} m</dd></dl>` : "");
    body.innerHTML = `
      <p><span class="tag t-${p.status === "new_gps" ? "new" : p.status === "missing" || p.status === "missing_gps" ? "missing" : "shifted"}">${PAIR_CN[p.status]}</span></p>
      <div class="photo-pair">
        ${sortiePhotoCard(b, "基准")}
        ${sortiePhotoCard(n, "补拍")}
      </div>
      ${overlapRows}`;
  } else {
    const e = cmpData.risk_entries[cmpSel.idx];
    const br = e.baseline_risk, nr = e.reshoot_risk;
    const r = nr || br;
    title.textContent = `${RISK_DIFF_CN[e.status]}：${TYPE_CN[r.type] || r.type}`;
    const tagCls = e.status === "resolved" ? "resolved" : e.status === "new" ? "risk_new" : "persisted";
    let metricRow = "";
    if (e.metric_delta != null) {
      const mk = { forward_overlap_pct: "前向重叠率", side_overlap_pct: "旁向重叠率" }[e.metric_key] || e.metric_key;
      metricRow = `<dl class="kv">
        <dt>${mk}（基准）</dt><dd>${e.metric_base}%</dd>
        <dt>${mk}（补拍）</dt><dd>${e.metric_reshoot}%</dd>
        <dt>变化</dt><dd>${deltaHtml(e.metric_delta)}</dd></dl>`;
    }
    const bp = br ? br.photos.map(id => cmpData.photo_by_id[id]).filter(Boolean) : [];
    const np = nr ? nr.photos.map(id => cmpData.photo_by_id[id]).filter(Boolean) : [];
    body.innerHTML = `
      <p><span class="tag t-${tagCls}">${RISK_DIFF_CN[e.status]}</span>${esc(r.label)}</p>
      ${br ? `<div class="explain" style="margin-bottom:6px"><b>基准：</b>${esc(br.explain)}</div>` : ""}
      ${nr ? `<div class="explain"><b>补拍：</b>${esc(nr.explain)}</div>` : ""}
      ${metricRow}
      <h4 style="margin:8px 0 2px;font-size:12.5px">基准照片</h4>
      <div class="photo-pair">${bp.map(p => sortiePhotoCard(p, "基准")).join("") || '<span class="muted">无</span>'}</div>
      <h4 style="margin:8px 0 2px;font-size:12.5px">补拍照片</h4>
      <div class="photo-pair">${np.map(p => sortiePhotoCard(p, "补拍")).join("") || '<span class="muted">无</span>'}</div>`;
  }
}

/* ---------- 操作 ---------- */
$("#btnSaveBaseline").onclick = async () => {
  if (!cur) return toast("请先选择项目");
  if (!cur.photos.length) return toast("当前项目还没有照片");
  if (!confirm("将当前项目的照片、参数与人工补正冻结为基准航次？\n（之后在单航次校核中的修改不影响已保存的基准）")) return;
  try {
    await api(`/api/projects/${cur.project.id}/sorties/baseline`,
      postJson({ label: `基准航次 ${new Date().toLocaleString("zh-CN", { hour12: false })}` }));
    toast("基准航次已保存");
    await loadCompare();
  } catch (e) { toast("保存失败：" + e.message); }
};

$("#cmpFileInput").addEventListener("change", async ev => {
  if (!cur) return toast("请先选择项目");
  const files = [...ev.target.files];
  if (!files.length) return;
  const fd = new FormData();
  files.forEach(f => fd.append("files", f));
  fd.append("label", `补拍航次 ${new Date().toLocaleString("zh-CN", { hour12: false })}`);
  toast(`正在导入 ${files.length} 张补拍照片…`);
  try {
    const r = await fetch(`/api/projects/${cur.project.id}/sorties/reshoot`,
      { method: "POST", body: fd });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    const noGps = data.saved.filter(s => !s.has_gps).length;
    toast(`补拍航次已建立：${data.saved.length} 张`
      + (noGps ? `，${noGps} 张无 GPS` : "")
      + (data.failed.length ? `，${data.failed.length} 张无法解析` : ""));
    cmpPreferredReshoot = String(data.id);
    ev.target.value = "";
    await loadCompare();
  } catch (e) { toast("导入失败：" + e.message); ev.target.value = ""; }
});

$("#cmpReshootSelect").addEventListener("change", () => {
  cmpFilter.strip = "all";
  loadCompare();
});

$("#btnDeleteSortie").onclick = async () => {
  const rid = $("#cmpReshootSelect").value;
  if (!rid) return;
  if (!confirm("删除该补拍航次？（基准航次与原始照片不受影响）")) return;
  try {
    await fetch(`/api/sorties/${rid}`, { method: "DELETE" });
    toast("补拍航次已删除");
    await loadCompare();
  } catch (e) { toast("删除失败：" + e.message); }
};

$("#cmpStripFilter").addEventListener("change", e => {
  cmpFilter.strip = e.target.value; renderCompareAll();
});
$("#cmpTypeFilter").addEventListener("change", e => {
  cmpFilter.type = e.target.value; renderCompareAll();
});

/* ---------- 页签切换 ---------- */
$("#tabSingle").onclick = () => switchTab("single");
$("#tabCompare").onclick = () => switchTab("compare");

function switchTab(name) {
  const single = name === "single";
  $("#viewSingle").classList.toggle("hidden", !single);
  $("#viewCompare").classList.toggle("hidden", single);
  $("#tabSingle").classList.toggle("active", single);
  $("#tabCompare").classList.toggle("active", !single);
  if (!single) {
    cmpResize();
    if (cmpData) cmpDraw(); else loadCompare();
  } else {
    resize();
  }
}

function cmpResize() {
  const r = cmpCv.parentElement.getBoundingClientRect();
  cmpCv.width = r.width; cmpCv.height = r.height;
  cmpDraw();
}
window.addEventListener("resize", () => { if (!$("#viewCompare").classList.contains("hidden")) cmpResize(); });

/* 项目切换 / 单航次数据变化后，若对比页可见则刷新 */
document.addEventListener("projectchange", () => {
  cmpData = null;
  if (!$("#viewCompare").classList.contains("hidden")) loadCompare();
  else { /* 下次切到对比页时 loadCompare 会被 switchTab 调用 */ }
});
