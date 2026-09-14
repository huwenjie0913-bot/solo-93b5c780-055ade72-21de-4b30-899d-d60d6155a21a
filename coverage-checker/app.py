"""Flask 入口：项目/照片 API、分析结果、航次对比与报告导出。"""
import csv
import io
import json
import os
import time

from flask import Flask, jsonify, request, send_from_directory, abort, Response

import db
from analyzer import read_exif, analyze
from compare import build_comparison, PAIR_STATUS_CN, RISK_STATUS_CN

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE, "uploads")
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
db.init()


@app.errorhandler(400)
@app.errorhandler(404)
def _json_error(e):
    return jsonify({"error": getattr(e, "description", str(e))}), e.code


def _parse_taken_at(s):
    """'YYYY:MM:DD HH:MM:SS' 或 'YYYY-MM-DD HH:MM:SS' → epoch 秒；失败返回 None。"""
    if not s:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(s.strip(), fmt))
        except ValueError:
            continue
    return None


def _project_payload(pid):
    proj = db.get_project(pid)
    if not proj:
        abort(404)
    photos = db.list_photos(pid)
    result = analyze(photos, proj)
    return {"project": proj, **result}


# ---------- 页面 ----------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- 项目 ----------

@app.get("/api/projects")
def projects():
    return jsonify(db.list_projects())


@app.post("/api/projects")
def new_project():
    d = request.get_json(force=True)
    pid = db.create_project(
        d.get("name") or f"项目 {time.strftime('%m-%d %H:%M')}",
        float(d.get("altitude", 120)), float(d.get("sensor_w", 13.2)),
        float(d.get("sensor_h", 8.8)), float(d.get("focal_default", 8.8)),
        float(d.get("fwd_min", 60)), float(d.get("side_min", 30)))
    return jsonify({"id": pid})


@app.get("/api/projects/<int:pid>")
def project_detail(pid):
    return jsonify(_project_payload(pid))


@app.put("/api/projects/<int:pid>")
def update_project(pid):
    if not db.get_project(pid):
        abort(404)
    db.update_project(pid, request.get_json(force=True))
    return jsonify(_project_payload(pid))


# ---------- 照片 ----------

@app.post("/api/projects/<int:pid>/photos")
def upload_photos(pid):
    if not db.get_project(pid):
        abort(404)
    pdir = os.path.join(UPLOADS, str(pid))
    os.makedirs(pdir, exist_ok=True)
    saved, failed = [], []
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        safe = os.path.basename(f.filename)
        stored = f"{int(time.time()*1000)}_{os.getpid()}_{safe}"
        path = os.path.join(pdir, stored)
        f.save(path)
        try:
            meta = read_exif(path)
        except Exception:
            os.remove(path)
            failed.append(safe)
            continue
        photo_id = db.insert_photo(pid, safe, stored, meta, _parse_taken_at(meta["taken_at"]))
        saved.append({"id": photo_id, "filename": safe, "has_gps": meta["lat"] is not None})
    return jsonify({"saved": saved, "failed": failed, **_project_payload(pid)})


@app.get("/api/photos/<int:photo_id>/image")
def photo_image(photo_id):
    p = db.get_photo(photo_id)
    if not p:
        abort(404)
    return send_from_directory(os.path.join(UPLOADS, str(p["project_id"])), p["stored"])


@app.post("/api/photos/<int:photo_id>/exclude")
def exclude_photo(photo_id):
    p = db.get_photo(photo_id)
    if not p:
        abort(404)
    d = request.get_json(force=True)
    db.set_excluded(photo_id, bool(d.get("excluded")))
    return jsonify(_project_payload(p["project_id"]))


@app.post("/api/photos/<int:photo_id>/correct")
def correct_photo(photo_id):
    p = db.get_photo(photo_id)
    if not p:
        abort(404)
    d = request.get_json(force=True)
    if d.get("clear"):
        db.clear_manual(photo_id)
    else:
        lat, lon = float(d["lat"]), float(d["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            abort(400, "坐标超出范围")
        db.set_manual(photo_id, lat, lon, str(d.get("note") or ""))
    return jsonify(_project_payload(p["project_id"]))


# ---------- 航次对比 ----------

def _analyze_sortie_photos(rows, params):
    """sortie_photos 行 → analyze() 输入格式（eff_* 即原始坐标，无人工补正）。"""
    out = []
    for r in rows:
        d = dict(r)
        d["eff_lat"], d["eff_lon"] = d["lat"], d["lon"]
        d["manual_lat"] = d["manual_lon"] = None
        out.append(d)
    return analyze(out, params)


def _persist_analysis(sid, analysis):
    """写入分析快照（派生字段已随 analyze 输入行/结果存储）。"""
    db.set_sortie_analysis(sid, json.dumps(analysis, ensure_ascii=False))


def _save_baseline(pid, label):
    proj = db.get_project(pid)
    if not proj:
        abort(404)
    photos = db.list_photos(pid)
    if not photos:
        abort(400, "当前项目还没有照片，无法保存基准航次")
    params = _params_snapshot(proj)
    sid = db.create_sortie(pid, "baseline",
                           label or f"基准航次 {time.strftime('%m-%d %H:%M')}",
                           json.dumps(params, ensure_ascii=False))
    for p in photos:
        # 快照写入“当时生效”的坐标：人工补正（eff_*）优先，未补正则为 EXIF 坐标，
        # 否则为 None。这样补拍配对、足迹与差异都按校核员补正后的位置计算。
        fields = {k: p.get(k) for k in
                  ("filename", "stored", "taken_at", "taken_at_ts",
                   "gps_alt", "focal", "img_w", "img_h", "excluded")}
        fields["lat"] = p["eff_lat"]
        fields["lon"] = p["eff_lon"]
        fields["origin_photo_id"] = p["id"]
        db.insert_sortie_photo(sid, pid, fields)
    # sortie_photos 的新 id 与分析结果一致（顺序插入、空表起步）
    analysis = _analyze_sortie_photos(db.list_sortie_photos(sid), params)
    _persist_analysis(sid, analysis)
    return sid


def _params_snapshot(proj):
    return {k: proj[k] for k in
            ("altitude", "sensor_w", "sensor_h", "focal_default", "fwd_min", "side_min")}


def _import_reshoot(pid, files, label, params_override):
    proj = db.get_project(pid)
    if not proj:
        abort(404)
    if not db.get_baseline(pid):
        abort(400, "请先把当前项目保存为基准航次，再导入补拍航片")
    pdir = os.path.join(UPLOADS, str(pid))
    os.makedirs(pdir, exist_ok=True)
    params = _params_snapshot(proj)
    params.update({k: float(v) for k, v in (params_override or {}).items()
                   if v is not None and k in params})
    sid = db.create_sortie(pid, "reshoot",
                           label or f"补拍航次 {time.strftime('%m-%d %H:%M')}",
                           json.dumps(params, ensure_ascii=False))
    saved, failed = [], []
    for f in files:
        if not f.filename:
            continue
        safe = os.path.basename(f.filename)
        stored = f"{int(time.time()*1000)}_{os.getpid()}_{safe}"
        path = os.path.join(pdir, stored)
        f.save(path)
        try:
            meta = read_exif(path)
        except Exception:
            os.remove(path)
            failed.append(safe)
            continue
        sp_id = db.insert_sortie_photo(sid, pid, {
            "filename": safe, "stored": stored,
            "taken_at": meta["taken_at"], "taken_at_ts": _parse_taken_at(meta["taken_at"]),
            "lat": meta["lat"], "lon": meta["lon"], "gps_alt": meta["gps_alt"],
            "focal": meta["focal"], "img_w": meta["img_w"], "img_h": meta["img_h"]})
        saved.append({"id": sp_id, "filename": safe, "has_gps": meta["lat"] is not None})
    if not saved:
        db.delete_sortie(sid)
        abort(400, "没有可解析的照片")
    analysis = _analyze_sortie_photos(db.list_sortie_photos(sid), params)
    _persist_analysis(sid, analysis)
    return sid, saved, failed


@app.post("/api/projects/<int:pid>/sorties/baseline")
def save_baseline(pid):
    d = request.get_json(silent=True) or {}
    sid = _save_baseline(pid, d.get("label"))
    return jsonify({"id": sid, "sorties": db.list_sorties(pid)})


@app.post("/api/projects/<int:pid>/sorties/reshoot")
def upload_reshoot(pid):
    files = request.files.getlist("files")
    label = request.form.get("label") or ""
    override = {}
    for k in ("altitude", "sensor_w", "sensor_h", "focal_default", "fwd_min", "side_min"):
        if request.form.get(k):
            override[k] = request.form.get(k)
    sid, saved, failed = _import_reshoot(pid, files, label, override)
    return jsonify({"id": sid, "saved": saved, "failed": failed,
                    "sorties": db.list_sorties(pid)})


@app.get("/api/projects/<int:pid>/sorties")
def project_sorties(pid):
    if not db.get_project(pid):
        abort(404)
    return jsonify(db.list_sorties(pid))


@app.delete("/api/sorties/<int:sid>")
def remove_sortie(sid):
    s = db.get_sortie(sid)
    if not s:
        abort(404)
    pid = s["project_id"]
    db.delete_sortie(sid)
    return jsonify({"sorties": db.list_sorties(pid)})


@app.get("/api/sorties/<int:sid>/photos/<int:sp_id>/image")
def sortie_photo_image(sid, sp_id):
    p = db.get_sortie_photo(sp_id)
    if not p or p["sortie_id"] != sid:
        abort(404)
    return send_from_directory(os.path.join(UPLOADS, str(p["project_id"])), p["stored"])


def _prefix_analysis(sid, analysis, prefix):
    """复制分析结果并给所有照片 id 加前缀，避免基准/补拍 id 空间冲突；
    同时为每张照片附上图地址。"""
    import copy
    a = copy.deepcopy(analysis)
    idmap = {}
    for p in a["photos"]:
        old = p["id"]
        new = f"{prefix}{old}"
        idmap[old] = new
        p["id"] = new
        p["image_url"] = f"/api/sorties/{sid}/photos/{old}/image"
        if p.get("next_id") is not None:
            p["next_id"] = f"{prefix}{p['next_id']}"
    for s in a["strips"]:
        s["photo_ids"] = [idmap[i] for i in s["photo_ids"]]
    for i, r in enumerate(a["risks"], 1):
        r["id"] = f"{prefix}r{i}"
        r["photos"] = [idmap.get(x, x) for x in r["photos"]]
        if r.get("reshoot"):
            r["reshoot"]["from"] = idmap.get(r["reshoot"]["from"], r["reshoot"]["from"])
            r["reshoot"]["to"] = idmap.get(r["reshoot"]["to"], r["reshoot"]["to"])
    return a


def _comparison_payload(pid, reshoot_id):
    b_row = db.get_baseline(pid)
    if not b_row or not b_row["analysis_json"]:
        abort(400, "尚未保存基准航次")
    if reshoot_id is not None:
        n_row = db.get_sortie(reshoot_id)
        if not n_row or n_row["project_id"] != pid or n_row["kind"] != "reshoot":
            abort(404)
    else:
        n_row = db.latest_reshoot(pid)
        if not n_row:
            abort(400, "尚未导入补拍航次")
    base = json.loads(b_row["analysis_json"])
    new = json.loads(n_row["analysis_json"])
    base["_sortie"] = {"sortie_id": b_row["id"], "label": b_row["label"],
                       "kind": b_row["kind"], "created_at": b_row["created_at"]}
    new["_sortie"] = {"sortie_id": n_row["id"], "label": n_row["label"],
                      "kind": n_row["kind"], "created_at": n_row["created_at"]}
    pb = _prefix_analysis(b_row["id"], base, "b")
    pn = _prefix_analysis(n_row["id"], new, "n")
    comp = build_comparison(pb, pn)
    comp["baseline_sortie_id"] = b_row["id"]
    comp["reshoot_sortie_id"] = n_row["id"]
    comp["sorties"] = db.list_sorties(pid)
    return comp


@app.get("/api/projects/<int:pid>/compare")
def compare(pid):
    if not db.get_project(pid):
        abort(404)
    rid = request.args.get("reshoot_id", type=int)
    return jsonify(_comparison_payload(pid, rid))


def _comparison_export_rows(comp):
    """差异明细行（照片配对 + 风险对账合并）。"""
    photo_by_id = comp["photo_by_id"]

    def fname(pid):
        p = photo_by_id.get(pid)
        return p["filename"] if p else ""

    rows = []
    for pr in comp["pairs"]:
        b, n = pr.get("baseline_photo"), pr.get("reshoot_photo")
        rows.append({
            "类别": "照片-" + PAIR_STATUS_CN[pr["status"]],
            "基准航带": b.get("strip_id") if b else "",
            "补拍航带": n.get("strip_id") if n else "",
            "基准照片": b["filename"] if b else "",
            "补拍照片": n["filename"] if n else "",
            "基准坐标": (f'{b["lat"]:.7f},{b["lon"]:.7f}' if b and b.get("lat") is not None else ""),
            "补拍坐标": (f'{n["lat"]:.7f},{n["lon"]:.7f}' if n and n.get("lat") is not None else ""),
            "偏移米": pr["dist_m"] if pr["dist_m"] is not None else "",
            "基准前向重叠%": pr.get("fwd_overlap_base") if pr.get("fwd_overlap_base") is not None else "",
            "补拍前向重叠%": pr.get("fwd_overlap_reshoot") if pr.get("fwd_overlap_reshoot") is not None else "",
            "重叠率变化": pr.get("fwd_overlap_delta") if pr.get("fwd_overlap_delta") is not None else "",
            "风险类型": "", "风险级别": "", "指标变化": "", "说明": "",
        })
    for e in comp["risk_entries"]:
        br, nr = e.get("baseline_risk"), e.get("reshoot_risk")
        r = nr or br
        sev = {"high": "高", "mid": "中", "info": "提示"}.get(r["severity"], "")
        delta_txt = ""
        if e.get("metric_delta") is not None:
            mk = {"forward_overlap_pct": "前向重叠率", "side_overlap_pct": "旁向重叠率"}.get(
                e["metric_key"], e["metric_key"])
            delta_txt = f"{mk} {e['metric_base']}%→{e['metric_reshoot']}%（{e['metric_delta']:+.1f}pt）"
        if e["status"] == "resolved":
            explain = "基准航次风险区段在补拍航次中已无对应风险"
        elif e["status"] == "new":
            explain = nr["explain"]
        else:
            explain = "补拍后该位置仍存在同类覆盖风险"
        rows.append({
            "类别": "风险-" + RISK_STATUS_CN[e["status"]],
            "基准航带": "/".join(str(s) for s in br["strips"]) if br else "",
            "补拍航带": "/".join(str(s) for s in nr["strips"]) if nr else "",
            "基准照片": " ".join(fname(i) for i in br["photos"]) if br else "",
            "补拍照片": " ".join(fname(i) for i in nr["photos"]) if nr else "",
            "基准坐标": (f'{br["lat"]:.7f},{br["lon"]:.7f}' if br and br.get("lat") is not None else ""),
            "补拍坐标": (f'{nr["lat"]:.7f},{nr["lon"]:.7f}' if nr and nr.get("lat") is not None else ""),
            "偏移米": e["dist_m"] if e["dist_m"] is not None else "",
            "基准前向重叠%": "", "补拍前向重叠%": "", "重叠率变化": "",
            "风险类型": TYPE_CN.get(r["type"], r["type"]),
            "风险级别": sev, "指标变化": delta_txt, "说明": explain,
        })
    return rows


@app.get("/api/projects/<int:pid>/compare/report.csv")
def compare_report_csv(pid):
    if not db.get_project(pid):
        abort(404)
    comp = _comparison_payload(pid, request.args.get("reshoot_id", type=int))
    rows = _comparison_export_rows(comp)
    buf = io.StringIO()
    buf.write("﻿")
    c = comp["counts"]
    buf.write(f"# 项目,{db.get_project(pid)['name']},"
              f"基准航次,{comp['baseline']['label']},补拍航次,{comp['reshoot']['label']}\n")
    buf.write(f"# 基准照片数,{c['base_photo']},补拍照片数,{c['reshoot_photo']},"
              f"对应,{c['matched']},偏移,{c['shifted']},新增,{c['new']},缺失,{c['missing']},"
              f"风险已解决,{c['risk_resolved']},新增风险,{c['risk_new']},仍存在,{c['risk_persisted']}\n")
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=sortie_compare_{pid}.csv"})


@app.get("/api/projects/<int:pid>/compare/report.json")
def compare_report_json(pid):
    if not db.get_project(pid):
        abort(404)
    comp = _comparison_payload(pid, request.args.get("reshoot_id", type=int))
    return Response(json.dumps(comp, ensure_ascii=False, indent=2),
                    mimetype="application/json", headers={
                        "Content-Disposition":
                            f"attachment; filename=sortie_compare_{pid}.json"})


# ---------- 报告 ----------

TYPE_CN = {"gap": "断带", "low_forward": "前向重叠不足", "duplicate": "疑似重复拍摄",
           "gap_side": "旁向断带", "low_side": "旁向重叠不足", "no_gps": "缺少坐标"}


def _report_rows(pid):
    data = _project_payload(pid)
    photos = {p["id"]: p for p in data["photos"]}
    rows = []
    for r in data["risks"]:
        ph = r["photos"]
        rs = r.get("reshoot") or {}
        rows.append({
            "风险编号": r["id"], "类型": TYPE_CN.get(r["type"], r["type"]),
            "级别": {"high": "高", "mid": "中", "info": "提示"}[r["severity"]],
            "航带": "/".join(str(s) for s in r["strips"]) or "-",
            "涉及照片编号": " ".join(f"#{i}" for i in ph),
            "涉及照片文件": " ".join(photos[i]["filename"] for i in ph if i in photos),
            "中心纬度": f'{r["lat"]:.7f}' if r["lat"] is not None else "",
            "中心经度": f'{r["lon"]:.7f}' if r["lon"] is not None else "",
            "计算依据": "; ".join(f"{k}={v}" for k, v in r["metrics"].items()),
            "说明": r["explain"],
            "建议重拍起点": (f'#{rs["from"]} ({rs["from_coord"][0]:.7f},{rs["from_coord"][1]:.7f})'
                          if rs else ""),
            "建议重拍终点": (f'#{rs["to"]} ({rs["to_coord"][0]:.7f},{rs["to_coord"][1]:.7f})'
                          if rs else ""),
        })
    return data, rows


@app.get("/api/projects/<int:pid>/report.csv")
def report_csv(pid):
    data, rows = _report_rows(pid)
    buf = io.StringIO()
    buf.write("﻿")  # BOM 便于 Excel 打开
    proj = data["project"]
    buf.write(f"# 项目,{proj['name']},照片数,{len(data['photos'])},"
              f"航带数,{len(data['strips'])},风险区段,{len(rows)}\n")
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=coverage_report_{pid}.csv"})


@app.get("/api/projects/<int:pid>/report.json")
def report_json(pid):
    data, rows = _report_rows(pid)
    return jsonify({"project": data["project"], "risks": data["risks"], "rows": rows})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
