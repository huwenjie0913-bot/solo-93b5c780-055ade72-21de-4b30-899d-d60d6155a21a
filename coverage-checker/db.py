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
