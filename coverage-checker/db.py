"""sqlite3 持久化：项目参数与照片记录（含人工修正）。"""
import os
import sqlite3
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    altitude REAL NOT NULL DEFAULT 120,
    sensor_w REAL NOT NULL DEFAULT 13.2,
    sensor_h REAL NOT NULL DEFAULT 8.8,
    focal_default REAL NOT NULL DEFAULT 8.8,
    fwd_min REAL NOT NULL DEFAULT 60,
    side_min REAL NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored TEXT NOT NULL,
    taken_at TEXT,
    taken_at_ts REAL,
    lat REAL, lon REAL, gps_alt REAL,
    focal REAL, img_w INTEGER, img_h INTEGER,
    excluded INTEGER NOT NULL DEFAULT 0,
    manual_lat REAL, manual_lon REAL, manual_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_photos_project ON photos(project_id);

CREATE TABLE IF NOT EXISTS sorties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,              -- baseline / reshoot
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    params_json TEXT NOT NULL,       -- 航摄参数快照
    analysis_json TEXT               -- analyze() 结果快照（照片/strips/risks）
);
CREATE INDEX IF NOT EXISTS idx_sorties_project ON sorties(project_id);

CREATE TABLE IF NOT EXISTS sortie_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sortie_id INTEGER NOT NULL REFERENCES sorties(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    origin_photo_id INTEGER,         -- 基准航次快照来源照片 id
    filename TEXT NOT NULL,
    stored TEXT NOT NULL,
    taken_at TEXT,
    taken_at_ts REAL,
    lat REAL, lon REAL, gps_alt REAL,
    focal REAL, img_w INTEGER, img_h INTEGER,
    excluded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sortie_photos_sortie ON sortie_photos(sortie_id);
"""


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init():
    with connect() as db:
        db.executescript(SCHEMA)


def create_project(name, altitude, sensor_w, sensor_h, focal_default, fwd_min, side_min):
    with connect() as db:
        cur = db.execute(
            "INSERT INTO projects(name, created_at, altitude, sensor_w, sensor_h, focal_default, fwd_min, side_min)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (name, time.strftime("%Y-%m-%d %H:%M:%S"), altitude, sensor_w, sensor_h,
             focal_default, fwd_min, side_min))
        return cur.lastrowid


def list_projects():
    with connect() as db:
        rows = db.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM photos WHERE project_id=p.id) AS photo_count"
            " FROM projects p ORDER BY p.id DESC").fetchall()
        return [dict(r) for r in rows]


def get_project(pid):
    with connect() as db:
        r = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def update_project(pid, fields):
    cols = ["altitude", "sensor_w", "sensor_h", "focal_default", "fwd_min", "side_min", "name"]
    sets, vals = [], []
    for c in cols:
        if c in fields and fields[c] is not None:
            sets.append(f"{c}=?")
            vals.append(fields[c])
    if not sets:
        return
    vals.append(pid)
    with connect() as db:
        db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", vals)


def insert_photo(pid, filename, stored, meta, taken_ts):
    with connect() as db:
        cur = db.execute(
            "INSERT INTO photos(project_id, filename, stored, taken_at, taken_at_ts,"
            " lat, lon, gps_alt, focal, img_w, img_h) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, filename, stored, meta["taken_at"], taken_ts,
             meta["lat"], meta["lon"], meta["gps_alt"], meta["focal"],
             meta["img_w"], meta["img_h"]))
        return cur.lastrowid


def list_photos(pid):
    """含有效坐标 eff_lat/eff_lon（人工补正优先）。"""
    with connect() as db:
        rows = db.execute("SELECT * FROM photos WHERE project_id=? ORDER BY id", (pid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["eff_lat"] = d["manual_lat"] if d["manual_lat"] is not None else d["lat"]
        d["eff_lon"] = d["manual_lon"] if d["manual_lon"] is not None else d["lon"]
        out.append(d)
    return out


def set_excluded(photo_id, excluded):
    with connect() as db:
        db.execute("UPDATE photos SET excluded=? WHERE id=?", (1 if excluded else 0, photo_id))


def set_manual(photo_id, lat, lon, note):
    with connect() as db:
        db.execute("UPDATE photos SET manual_lat=?, manual_lon=?, manual_note=? WHERE id=?",
                   (lat, lon, note, photo_id))


def clear_manual(photo_id):
    with connect() as db:
        db.execute("UPDATE photos SET manual_lat=NULL, manual_lon=NULL, manual_note=NULL WHERE id=?",
                   (photo_id,))


def get_photo(photo_id):
    with connect() as db:
        r = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        return dict(r) if r else None


# ---------- 航次（基准 / 补拍）----------

def create_sortie(pid, kind, label, params_json):
    with connect() as db:
        # 每个项目仅保留一个基准航次，重新保存时覆盖旧基准
        if kind == "baseline":
            db.execute("DELETE FROM sorties WHERE project_id=? AND kind='baseline'", (pid,))
        cur = db.execute(
            "INSERT INTO sorties(project_id, kind, label, created_at, params_json)"
            " VALUES(?,?,?,?,?)",
            (pid, kind, label, time.strftime("%Y-%m-%d %H:%M:%S"), params_json))
        return cur.lastrowid


def set_sortie_analysis(sid, analysis_json):
    with connect() as db:
        db.execute("UPDATE sorties SET analysis_json=? WHERE id=?", (analysis_json, sid))


def list_sorties(pid):
    with connect() as db:
        rows = db.execute(
            " SELECT s.*, (SELECT COUNT(*) FROM sortie_photos WHERE sortie_id=s.id) AS photo_count"
            " FROM sorties s WHERE s.project_id=? ORDER BY"
            " CASE s.kind WHEN 'baseline' THEN 0 ELSE 1 END, s.id DESC", (pid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["has_analysis"] = bool(d.get("analysis_json"))
            del d["analysis_json"], d["params_json"]
            out.append(d)
        return out


def get_sortie(sid):
    with connect() as db:
        r = db.execute("SELECT * FROM sorties WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None


def get_baseline(pid):
    with connect() as db:
        r = db.execute("SELECT * FROM sorties WHERE project_id=? AND kind='baseline'"
                       " ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        return dict(r) if r else None


def latest_reshoot(pid, exclude_sid=None):
    q = "SELECT * FROM sorties WHERE project_id=? AND kind='reshoot'"
    args = [pid]
    if exclude_sid is not None:
        q += " AND id<>?"
        args.append(exclude_sid)
    q += " ORDER BY id DESC LIMIT 1"
    with connect() as db:
        r = db.execute(q, args).fetchone()
        return dict(r) if r else None


def delete_sortie(sid):
    with connect() as db:
        db.execute("DELETE FROM sorties WHERE id=?", (sid,))


def insert_sortie_photo(sid, pid, fields):
    with connect() as db:
        cur = db.execute(
            "INSERT INTO sortie_photos(sortie_id, project_id, origin_photo_id, filename, stored,"
            " taken_at, taken_at_ts, lat, lon, gps_alt, focal, img_w, img_h, excluded)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, pid, fields.get("origin_photo_id"), fields["filename"], fields["stored"],
             fields.get("taken_at"), fields.get("taken_at_ts"),
             fields.get("lat"), fields.get("lon"), fields.get("gps_alt"),
             fields.get("focal"), fields.get("img_w"), fields.get("img_h"),
             1 if fields.get("excluded") else 0))
        return cur.lastrowid


def list_sortie_photos(sid):
    with connect() as db:
        rows = db.execute("SELECT * FROM sortie_photos WHERE sortie_id=? ORDER BY id",
                          (sid,)).fetchall()
        return [dict(r) for r in rows]


def get_sortie_photo(sp_id):
    with connect() as db:
        r = db.execute("SELECT * FROM sortie_photos WHERE id=?", (sp_id,)).fetchone()
        return dict(r) if r else None
