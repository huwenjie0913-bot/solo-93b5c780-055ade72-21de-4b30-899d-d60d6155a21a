"""Flask 入口：项目/照片 API、分析结果与报告导出。"""
import csv
import io
import os
import time

from flask import Flask, jsonify, request, send_from_directory, abort, Response

import db
from analyzer import read_exif, analyze

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE, "uploads")
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
db.init()


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
