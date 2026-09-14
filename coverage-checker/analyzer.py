"""航片覆盖分析：EXIF 读取、地面足迹估算、航带归并、重叠率与风险区段计算。"""
import math
from PIL import Image

EARTH_M = 111320.0  # 每度经度米数（赤道），纬度方向用 110540


# ---------- EXIF ----------

def _rat(v):
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms_to_deg(dms, ref):
    if not dms or len(dms) < 3:
        return None
    d, m, s = (_rat(x) for x in dms[:3])
    if d is None or m is None or s is None:
        return None
    deg = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W"):
        deg = -deg
    return deg


def read_exif(path):
    """返回 {img_w, img_h, taken_at, lat, lon, gps_alt, focal}，缺失项为 None。"""
    out = {"img_w": None, "img_h": None, "taken_at": None,
           "lat": None, "lon": None, "gps_alt": None, "focal": None}
    with Image.open(path) as img:
        out["img_w"], out["img_h"] = img.size
        exif = img.getexif()
        if not exif:
            return out
        ex = exif.get_ifd(0x8769)  # EXIF IFD
        dto = ex.get(36867) or ex.get(36868) or exif.get(306)  # DateTimeOriginal / Digitized / FileDateTime
        if dto:
            out["taken_at"] = str(dto).strip()
        f = _rat(ex.get(37386))  # FocalLength
        if f and f > 0:
            out["focal"] = round(f, 3)
        gps = exif.get_ifd(0x8825)
        if gps:
            lat = _dms_to_deg(gps.get(2), gps.get(1))
            lon = _dms_to_deg(gps.get(4), gps.get(3))
            out["lat"], out["lon"] = lat, lon
            alt = _rat(gps.get(6))
            if alt is not None and gps.get(5) == 1:  # 海平面以下
                alt = -alt
            out["gps_alt"] = alt
    return out


# ---------- 几何 ----------

def to_xy(lat, lon, lat0, lon0):
    """局部切平面投影（米），y 向北。"""
    return ((lon - lon0) * EARTH_M * math.cos(math.radians(lat0)),
            (lat - lat0) * 110540.0)


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1, lon1, lat2, lon2):
    """航向角，0=北，顺时针。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _offset_m(a, b):
    """b 相对 a 的 (东, 北) 偏移，米。"""
    x, y = to_xy(b["eff_lat"], b["eff_lon"], a["eff_lat"], a["eff_lon"])
    return x, y


def footprint(focal_mm, img_w, img_h, altitude_m, sensor_w_mm, sensor_h_mm):
    """返回 (旁向宽 W, 航向长 L) 米。假设像幅长边垂直于航线。"""
    f = focal_mm
    across = altitude_m * sensor_w_mm / f
    along = altitude_m * sensor_h_mm / f
    if img_w and img_h and img_h > img_w:  # 竖幅则交换
        across, along = along, across
    return across, along


# ---------- 主分析 ----------

def analyze(photos, params):
    """photos: 数据库行字典（含有效坐标 eff_lat/eff_lon）。
    返回 {photos, strips, risks}，全部可 JSON 序列化。"""
    fwd_min = params["fwd_min"]      # 前向重叠阈值 %
    side_min = params["side_min"]    # 旁向重叠阈值 %
    alt = params["altitude"]
    sw, sh = params["sensor_w"], params["sensor_h"]

    by_id = {p["id"]: dict(p) for p in photos}
    risks = []

    # 无坐标照片（含未补正）→ 风险
    for p in photos:
        if p["excluded"]:
            continue
        if p["eff_lat"] is None or p["eff_lon"] is None:
            risks.append({
                "type": "no_gps", "severity": "high",
                "label": f"照片 #{p['id']} 缺少坐标",
                "photos": [p["id"]], "strips": [],
                "lat": None, "lon": None,
                "metrics": {}, "reshoot": None,
                "explain": "EXIF 中无 GPS 信息，且未人工补正；该照片未参与航带与重叠计算。",
            })

    usable = [p for p in by_id.values()
              if not p["excluded"] and p["eff_lat"] is not None and p["eff_lon"] is not None]
    usable.sort(key=lambda p: (p["taken_at_ts"] if p["taken_at_ts"] is not None else 1e18, p["id"]))

    # 足迹
    for p in usable:
        f = p["focal"] or params["focal_default"]
        p["focal_used"] = f
        p["fp_w"], p["fp_l"] = footprint(f, p["img_w"], p["img_h"], alt, sw, sh)

    # ---- 航带归并：时间间隔 / 距离跳变 / 航向突变 ----
    strips = []  # list of list of photo dict
    MAX_GAP_S, BEARING_TURN = 60.0, 70.0
    for p in usable:
        placed = False
        if strips:
            prev = strips[-1][-1]
            dt = None
            if p["taken_at_ts"] is not None and prev["taken_at_ts"] is not None:
                dt = p["taken_at_ts"] - prev["taken_at_ts"]
            dist = haversine(prev["eff_lat"], prev["eff_lon"], p["eff_lat"], p["eff_lon"])
            jump = dist > 2.5 * max(prev["fp_l"], 1.0)
            turn = False
            if len(strips[-1]) >= 2:
                pp = strips[-1][-2]
                d1 = haversine(pp["eff_lat"], pp["eff_lon"], prev["eff_lat"], prev["eff_lon"])
                # 距离过近的摄站（如原地重复拍摄）航向无意义，不参与转弯判断
                if d1 > max(3.0, 0.1 * prev["fp_l"]) and dist > max(3.0, 0.1 * prev["fp_l"]):
                    b1 = bearing(pp["eff_lat"], pp["eff_lon"], prev["eff_lat"], prev["eff_lon"])
                    b2 = bearing(prev["eff_lat"], prev["eff_lon"], p["eff_lat"], p["eff_lon"])
                    d = abs(b2 - b1)
                    turn = min(d, 360 - d) > BEARING_TURN
            if (dt is None or dt <= MAX_GAP_S) and not jump and not turn:
                strips[-1].append(p)
                placed = True
        if not placed:
            strips.append([p])

    strip_list = []
    for si, sp in enumerate(strips, 1):
        # 航向：由前后邻点推算
        for i, p in enumerate(sp):
            if len(sp) == 1:
                p["heading"] = 0.0
            elif i < len(sp) - 1:
                q = sp[i + 1]
                p["heading"] = bearing(p["eff_lat"], p["eff_lon"], q["eff_lat"], q["eff_lon"])
            else:
                q = sp[i - 1]
                p["heading"] = bearing(q["eff_lat"], q["eff_lon"], p["eff_lat"], p["eff_lon"])
            p["strip_id"] = si
            p["strip_seq"] = i + 1
        strip_list.append({"id": si, "photo_ids": [p["id"] for p in sp]})

    # ---- 每条前向边的间距/重叠率（同时供航次对比使用；风险判定在下方）----
    for sp in strips:
        for i in range(len(sp) - 1):
            a, b = sp[i], sp[i + 1]
            dist = haversine(a["eff_lat"], a["eff_lon"], b["eff_lat"], b["eff_lon"])
            L = (a["fp_l"] + b["fp_l"]) / 2.0
            a["next_id"] = b["id"]
            a["fwd_dist"] = round(dist, 2)
            a["fwd_overlap_pct"] = round((L - dist) / L * 100.0, 1)

    # ---- 前向重叠 / 断带 / 疑似重复 ----
    for sp in strips:
        for i in range(len(sp) - 1):
            a, b = sp[i], sp[i + 1]
            dist = haversine(a["eff_lat"], a["eff_lon"], b["eff_lat"], b["eff_lon"])
            L = (a["fp_l"] + b["fp_l"]) / 2.0
            overlap = (L - dist) / L * 100.0
            mid_lat = (a["eff_lat"] + b["eff_lat"]) / 2.0
            mid_lon = (a["eff_lon"] + b["eff_lon"]) / 2.0
            metrics = {"dist_m": round(dist, 2), "footprint_l_m": round(L, 2),
                       "forward_overlap_pct": round(overlap, 1), "threshold_pct": fwd_min}
            base = dict(strips=[a["strip_id"]], lat=mid_lat, lon=mid_lon,
                        photos=[a["id"], b["id"]],
                        reshoot={"from": a["id"], "to": b["id"],
                                 "from_coord": [a["eff_lat"], a["eff_lon"]],
                                 "to_coord": [b["eff_lat"], b["eff_lon"]]})
            if dist < max(1.0, 0.03 * L):
                risks.append({**base, "type": "duplicate", "severity": "info",
                              "label": f"航带{a['strip_id']}：#{a['id']} 与 #{b['id']} 疑似重复拍摄",
                              "metrics": metrics,
                              "explain": f"两摄站间距 {dist:.2f} m，不足航向足迹的 3%，基本拍摄同一区域，"
                                         f"可考虑剔除其中一张。"})
            elif overlap < 0:
                risks.append({**base, "type": "gap", "severity": "high",
                              "label": f"航带{a['strip_id']}：#{a['id']}→#{b['id']} 断带",
                              "metrics": metrics,
                              "explain": f"相邻摄站间距 {dist:.1f} m 大于航向足迹长度 {L:.1f} m，"
                                         f"两张照片之间出现 {dist - L:.1f} m 的无覆盖空洞，必须补拍。"})
            elif overlap < fwd_min:
                risks.append({**base, "type": "low_forward", "severity": "mid",
                              "label": f"航带{a['strip_id']}：#{a['id']}→#{b['id']} 前向重叠 {overlap:.0f}% 不足",
                              "metrics": metrics,
                              "explain": f"前向重叠率 = (航向足迹 {L:.1f} m − 摄站间距 {dist:.1f} m) / 航向足迹 "
                                         f"= {overlap:.1f}%，低于阈值 {fwd_min:.0f}%，立体匹配可能失败，建议补拍。"})

    # ---- 旁向重叠（相邻航带）----
    # 先用"每张照片在其他航带的最近邻"建立航带邻接图，避免把隔着一条航带的
    # 两条航带误判为旁向断带；再对相邻航带把最近邻偏移分解到航向/垂直方向，
    # 仅当沿航向分量小于半个航向足迹（两摄站基本相对）时，用垂直分量估计带间距。
    if len(strips) > 1:
        adj = [set() for _ in strips]  # adj[i]: 与 strips[i] 相邻的条带下标
        for si, A in enumerate(strips):
            from collections import Counter
            cnt = Counter()
            for pa in A:
                best_s, bd = None, None
                for sj, B in enumerate(strips):
                    if sj == si:
                        continue
                    for pb in B:
                        d = haversine(pa["eff_lat"], pa["eff_lon"], pb["eff_lat"], pb["eff_lon"])
                        if bd is None or d < bd:
                            best_s, bd = sj, d
                if best_s is not None:
                    cnt[best_s] += 1
            for sj, c in cnt.items():
                if c >= max(2, 0.2 * len(A)):
                    adj[si].add(sj)

        for i in range(len(strips)):
            for j in range(i + 1, len(strips)):
                if j not in adj[i] and i not in adj[j]:
                    continue
                A, B = strips[i], strips[j]
                W = (sum(p["fp_w"] for p in A) / len(A) + sum(p["fp_w"] for p in B) / len(B)) / 2.0
                L = (sum(p["fp_l"] for p in A) / len(A) + sum(p["fp_l"] for p in B) / len(B)) / 2.0
                offenders = []  # (idx_a, idx_b, spacing, side_ov)
                for ia, pa in enumerate(A):
                    best, bd = None, None
                    for ib, pb in enumerate(B):
                        d = haversine(pa["eff_lat"], pa["eff_lon"], pb["eff_lat"], pb["eff_lon"])
                        if bd is None or d < bd:
                            best, bd = ib, d
                    if best is None or bd > W * 1.5:  # 只考察相邻航带
                        continue
                    pb = B[best]
                    h = math.radians((pa["heading"] + pb["heading"]) / 2.0)
                    dx, dy = _offset_m(pa, pb)
                    along = abs(dx * math.sin(h) + dy * math.cos(h))
                    cross = abs(dx * math.cos(h) - dy * math.sin(h))
                    if along > 0.5 * L:  # 摄站在航向上错开太远，不能代表带间距
                        continue
                    side_ov = (W - cross) / W * 100.0
                    if side_ov < side_min:
                        offenders.append((ia, best, cross, side_ov))
                # 连续 offenders 归并为一个风险区段
                for grp in _group_contiguous(offenders):
                    ia0, ib0 = grp[0][0], grp[0][1]
                    ia1, ib1 = grp[-1][0], grp[-1][1]
                    pa0, pa1 = A[ia0], A[ia1]
                    pb0, pb1 = B[ib0], B[ib1]
                    worst = min(g[3] for g in grp)
                    wdist = max(g[2] for g in grp)
                    lat = (pa0["eff_lat"] + pa1["eff_lat"] + pb0["eff_lat"] + pb1["eff_lat"]) / 4
                    lon = (pa0["eff_lon"] + pa1["eff_lon"] + pb0["eff_lon"] + pb1["eff_lon"]) / 4
                    sev = "high" if worst < 0 else "mid"
                    typ = "gap_side" if worst < 0 else "low_side"
                    name = "旁向断带" if worst < 0 else f"旁向重叠 {worst:.0f}% 不足"
                    risks.append({
                        "type": typ, "severity": sev,
                        "label": f"航带{A[0]['strip_id']}×{B[0]['strip_id']}：{name}",
                        "photos": [pa0["id"], pa1["id"], pb0["id"], pb1["id"]],
                        "strips": [A[0]["strip_id"], B[0]["strip_id"]],
                        "lat": lat, "lon": lon,
                        "metrics": {"strip_spacing_m": round(wdist, 2),
                                    "footprint_w_m": round(W, 2),
                                    "side_overlap_pct": round(worst, 1),
                                    "threshold_pct": side_min},
                        "reshoot": {"from": pa0["id"], "to": pa1["id"],
                                    "from_coord": [pa0["eff_lat"], pa0["eff_lon"]],
                                    "to_coord": [pa1["eff_lat"], pa1["eff_lon"]]},
                        "explain": f"两航带垂直间距最大 {wdist:.1f} m，旁向足迹宽 {W:.1f} m，"
                                   f"旁向重叠率 = (足迹宽 − 带间距) / 足迹宽 = {worst:.1f}%，"
                                   f"阈值 {side_min:.0f}%。建议在航带 {A[0]['strip_id']} 与 {B[0]['strip_id']} 之间补飞一条航带。",
                    })

    for k, r in enumerate(risks, 1):
        r["id"] = k

    out_photos = []
    for p in photos:
        q = by_id[p["id"]]
        out_photos.append({
            "id": q["id"], "filename": q["filename"], "taken_at": q["taken_at"],
            "lat": q["eff_lat"], "lon": q["eff_lon"],
            "exif_lat": q["lat"], "exif_lon": q["lon"],
            "manual": q["manual_lat"] is not None,
            "excluded": bool(q["excluded"]),
            "focal": q.get("focal_used") or q["focal"] or params["focal_default"],
            "img_w": q["img_w"], "img_h": q["img_h"],
            "strip_id": q.get("strip_id"), "strip_seq": q.get("strip_seq"),
            "heading": q.get("heading"), "fp_w": q.get("fp_w"), "fp_l": q.get("fp_l"),
            "next_id": q.get("next_id"),
            "fwd_dist": q.get("fwd_dist"), "fwd_overlap_pct": q.get("fwd_overlap_pct"),
        })
    return {"photos": out_photos, "strips": strip_list, "risks": risks}


def _group_contiguous(offenders):
    """offenders: [(ia, ib, dist, ov)] 按 ia 排序后把相邻（间隔≤1）的归并。"""
    if not offenders:
        return []
    offenders = sorted(offenders, key=lambda x: x[0])
    groups, cur = [[offenders[0]]], offenders[0][0]
    for o in offenders[1:]:
        if o[0] - cur <= 1:
            groups[-1].append(o)
        else:
            groups.append([o])
        cur = o[0]
    return groups
