"""航次对比：基准航次 vs 补拍航次。

- 照片配对：以坐标距离为主、时间与焦距相似度为辅的一对一贪心匹配
- 足迹分类：新增（补拍有/基准无）、缺失（基准有/补拍无）、位置偏移
- 风险对账：按空间位置/共有照片匹配，得到已解决、新增、仍存在
- 重叠率变化：配对照片的前向重叠率差值（同航带后继）
"""
from analyzer import haversine

# 状态标签（中文）
PAIR_STATUS_CN = {"matched": "对应照片", "shifted": "位置偏移", "new": "补拍新增",
                  "missing": "补拍缺失", "new_gps": "新增有坐标照片", "missing_gps": "坐标仍缺失"}
RISK_STATUS_CN = {"resolved": "风险已解决", "new": "新增风险", "persisted": "风险仍存在"}

SHIFT_M = 10.0          # 摄站偏移判定阈值（米）
RISK_MATCH_M = 60.0     # 风险区段空间匹配半径（米）
DUP_KIND = {"gap", "low_forward", "duplicate", "gap_side", "low_side"}  # 有坐标的风险类型


def _strip_map(pairs):
    """按配对投票建立 基准航带 → 补拍航带 的对应关系。"""
    votes = {}
    for pr in pairs:
        b, n = pr.get("baseline_photo"), pr.get("reshoot_photo")
        if b is None or n is None:
            continue
        bs, ns = b.get("strip_id"), n.get("strip_id")
        if bs is None or ns is None:
            continue
        votes.setdefault(bs, {})
        votes[bs][ns] = votes[bs].get(ns, 0) + 1
    m = {}
    for bs, ns_votes in votes.items():
        m[bs] = max(ns_votes, key=ns_votes.get)
    return m


def _strip_centroid_map(base_photos, new_photos):
    """按各航带质心距离建立 基准航带 → 补拍航带 的一一对应。
    航带是长条，用最近邻摄站距离（而非质心直线距离）判断对应关系。"""
    def groups(photos):
        g = {}
        for p in photos:
            if p["lat"] is not None and p.get("strip_id"):
                g.setdefault(p["strip_id"], []).append(p)
        return g
    bg, ng = groups(base_photos), groups(new_photos)
    mapping = {}
    for bs, bps in bg.items():
        best_ns, best_d = None, None
        for ns, nps in ng.items():
            # 两航带最近摄站距离
            md = min(haversine(b["lat"], b["lon"], n["lat"], n["lon"])
                     for b in bps for n in nps)
            if best_d is None or md < best_d:
                best_d, best_ns = md, ns
        if best_ns is not None:
            mapping[bs] = best_ns
    return mapping


def match_pairs(base_photos, new_photos, match_radius_m):
    """一对一贪心配对。返回 (pairs, b_index, n_index, fixed_files)。

    pairs: [{baseline_photo, reshoot_photo, dist_m, status, ...}]，
           未匹配照片各自成项（另一侧为 None）。
    """
    n_by_file = {}
    for n in new_photos:
        n_by_file.setdefault(n["filename"], []).append(n)

    # 基准无坐标照片：补拍中存在同名且有坐标的照片 → 视为补拍补回，先行占用
    used_n = set()
    pre = {}  # baseline photo id → reshoot photo
    for b in base_photos:
        if b["lat"] is not None:
            continue
        cand = next((n for n in n_by_file.get(b["filename"], [])
                     if n["lat"] is not None and n["id"] not in used_n), None)
        if cand is not None:
            used_n.add(cand["id"])
            pre[b["id"]] = cand

    bgeo = [p for p in base_photos if p["lat"] is not None]
    ngeo = [p for p in new_photos if p["lat"] is not None and p["id"] not in used_n]
    strip_map = _strip_centroid_map(bgeo, ngeo)
    radius = match_radius_m

    cands = []
    for b in bgeo:
        allowed_ns = strip_map.get(b.get("strip_id"))
        for n in ngeo:
            # 仅在对应航带内配对，防止补拍多/漏片跨航带误配
            if allowed_ns is not None and n.get("strip_id") != allowed_ns:
                continue
            d = haversine(b["lat"], b["lon"], n["lat"], n["lon"])
            if d > radius:
                continue
            score = d
            # 焦距差异大（换机/换镜头）扣分
            if b.get("focal") and n.get("focal"):
                score += 20.0 * abs(b["focal"] - n["focal"]) / max(b["focal"], 1e-6)
            # 航带内序号接近优先
            if b.get("strip_seq") and n.get("strip_seq"):
                score += 2.0 * abs((b.get("strip_seq") or 1) - (n.get("strip_seq") or 1))
            # 同名文件（原机位重拍）优先
            if b["filename"] == n["filename"]:
                score -= 8.0
            cands.append((score, d, b["id"], n["id"]))
    cands.sort()

    links = {}
    for score, d, bid, nid in cands:
        if bid in links or nid in used_n:
            continue
        used_n.add(nid)
        links[bid] = (nid, d)

    bmap = {p["id"]: p for p in base_photos}
    nmap = {p["id"]: p for p in new_photos}
    pairs = []
    fixed_files = set()
    for b in base_photos:
        if b["lat"] is None:
            hit = pre.get(b["id"])
            if hit is not None:
                fixed_files.add(b["filename"])
                pairs.append({"baseline_photo": b, "reshoot_photo": hit,
                              "dist_m": None, "status": "matched", "gps_fixed": True,
                              "lat": hit["lat"], "lon": hit["lon"]})
            else:
                pairs.append({"baseline_photo": b, "reshoot_photo": None,
                              "dist_m": None, "status": "missing_gps",
                              "lat": None, "lon": None})
            continue
        hit = links.get(b["id"])
        if hit is None:
            pairs.append({"baseline_photo": b, "reshoot_photo": None,
                          "dist_m": None, "status": "missing",
                          "lat": b["lat"], "lon": b["lon"]})
        else:
            nid, d = hit
            pairs.append({"baseline_photo": b, "reshoot_photo": nmap[nid],
                          "dist_m": round(d, 2),
                          "status": "shifted" if d > SHIFT_M else "matched",
                          "lat": (b["lat"] + nmap[nid]["lat"]) / 2,
                          "lon": (b["lon"] + nmap[nid]["lon"]) / 2})
    for n in new_photos:
        if n["id"] in used_n:
            continue
        if n["lat"] is None:
            continue  # 补拍无坐标照片以新增风险形式呈现，不在配对中重复
        pairs.append({"baseline_photo": None, "reshoot_photo": n,
                      "dist_m": None, "status": "new_gps",
                      "lat": n["lat"], "lon": n["lon"]})
    return pairs, bmap, nmap, fixed_files


def reconcile_risks(base_risks, new_risks, bmap, nmap, b2n, fixed_gps_files):
    """风险对账 → entries: [{baseline_risk, reshoot_risk, status, ...}]。"""
    entries = []

    b_geo = [r for r in base_risks if r["type"] in DUP_KIND]
    n_geo = [r for r in new_risks if r["type"] in DUP_KIND]

    cands = []
    for br in b_geo:
        for nr in n_geo:
            d = haversine(br["lat"], br["lon"], nr["lat"], nr["lon"])
            if d > RISK_MATCH_M:
                continue
            common = 0
            for bid in br.get("photos", []):
                nid = b2n.get(bid)
                if nid is not None and nid in nr.get("photos", []):
                    common += 1
            score = d - 50.0 * common - 30.0 * (1 if br["type"] == nr["type"] else 0)
            cands.append((score, d, br["id"], nr["id"]))
    cands.sort()
    used_b, used_n, rlinks = set(), set(), {}
    for score, d, brid, nrid in cands:
        if brid in used_b or nrid in used_n:
            continue
        used_b.add(brid)
        used_n.add(nrid)
        rlinks[brid] = (nrid, d)

    br_map = {r["id"]: r for r in base_risks}
    nr_map = {r["id"]: r for r in new_risks}

    for br in base_risks:
        if br["type"] == "no_gps":
            # 无坐标风险：看涉及照片是否在补拍中以同名出现且有坐标
            fixed = any(bmap[i]["filename"] in fixed_gps_files
                        for i in br.get("photos", []) if i in bmap)
            entries.append({"baseline_risk": br, "reshoot_risk": None,
                            "status": "resolved" if fixed else "persisted",
                            "dist_m": None,
                            "lat": None, "lon": None})
            continue
        hit = rlinks.get(br["id"])
        if hit is None:
            entries.append({"baseline_risk": br, "reshoot_risk": None,
                            "status": "resolved", "dist_m": None,
                            "lat": br["lat"], "lon": br["lon"]})
        else:
            nrid, d = hit
            nr = nr_map[nrid]
            entries.append({"baseline_risk": br, "reshoot_risk": nr,
                            "status": "persisted", "dist_m": round(d, 2),
                            "lat": (br["lat"] + nr["lat"]) / 2,
                            "lon": (br["lon"] + nr["lon"]) / 2})

    for nr in new_risks:
        if nr["id"] in used_n:
            continue
        entries.append({"baseline_risk": None, "reshoot_risk": nr,
                        "status": "new", "dist_m": None,
                        "lat": nr.get("lat"), "lon": nr.get("lon")})
    return entries


def _metric_delta(b_risk, n_risk, keys):
    for k in keys:
        bv = (b_risk or {}).get("metrics", {}).get(k)
        nv = (n_risk or {}).get("metrics", {}).get(k)
        if isinstance(bv, (int, float)) and isinstance(nv, (int, float)):
            return k, round(nv - bv, 1), bv, nv
    return None, None, None, None


def build_comparison(base, new):
    """base/new: 已 parse 的 analyze() 结果（photo id 统一加前缀防止冲突）。

    直接由 compare_snapshot 预先完成 id 前缀处理，此处只接收。
    """
    base_photos, new_photos = base["photos"], new["photos"]
    base_risks, new_risks = base["risks"], new["risks"]

    # 配对半径：0.6 倍平均旁向足迹，至少 30 m
    avg_w = _avg_fp(base_photos, new_photos)
    radius = max(30.0, 0.6 * avg_w)

    pairs, bmap, nmap, fixed_files = match_pairs(base_photos, new_photos, radius)
    b2n = {pr["baseline_photo"]["id"]: pr["reshoot_photo"]["id"]
           for pr in pairs if pr["baseline_photo"] and pr["reshoot_photo"]}
    strip_map = _strip_map(pairs)

    # 前向重叠率变化（配对照片各自同航带后继处的重叠率）
    for pr in pairs:
        b, n = pr.get("baseline_photo"), pr.get("reshoot_photo")
        pr["fwd_overlap_base"] = b.get("fwd_overlap_pct") if b else None
        pr["fwd_overlap_reshoot"] = n.get("fwd_overlap_pct") if n else None
        if (b and n and b.get("fwd_overlap_pct") is not None
                and n.get("fwd_overlap_pct") is not None):
            pr["fwd_overlap_delta"] = round(n["fwd_overlap_pct"] - b["fwd_overlap_pct"], 1)

    risk_entries = reconcile_risks(base_risks, new_risks, bmap, nmap, b2n, fixed_files)

    # 风险指标变化（前向/旁向重叠率）
    for e in risk_entries:
        k, delta, bv, nv = _metric_delta(
            e["baseline_risk"], e["reshoot_risk"],
            ["forward_overlap_pct", "side_overlap_pct"])
        e["metric_key"] = k
        e["metric_delta"] = delta
        e["metric_base"] = bv
        e["metric_reshoot"] = nv

    counts = {
        "base_photo": len(base_photos), "reshoot_photo": len(new_photos),
        "matched": sum(1 for p in pairs if p["status"] == "matched"),
        "shifted": sum(1 for p in pairs if p["status"] == "shifted"),
        "new": sum(1 for p in pairs if p["status"] == "new_gps"),
        "missing": sum(1 for p in pairs if p["status"] in ("missing", "missing_gps")),
        "risk_resolved": sum(1 for e in risk_entries if e["status"] == "resolved"),
        "risk_new": sum(1 for e in risk_entries if e["status"] == "new"),
        "risk_persisted": sum(1 for e in risk_entries if e["status"] == "persisted"),
    }

    return {
        "baseline": _sortie_info(base),
        "reshoot": _sortie_info(new),
        "match_radius_m": round(radius, 1),
        "shift_threshold_m": SHIFT_M,
        "strip_map": {str(k): v for k, v in strip_map.items()},
        "counts": counts,
        "pairs": pairs,
        "risk_entries": risk_entries,
        "photo_by_id": {p["id"]: p for p in base_photos + new_photos},
    }


def _avg_fp(bp, np_):
    ws = [p["fp_w"] for p in bp + np_ if p.get("fp_w")]
    return sum(ws) / len(ws) if ws else 100.0


def _sortie_info(ana):
    s = ana.get("_sortie", {})
    return {"sortie_id": s.get("sortie_id"), "label": s.get("label"),
            "kind": s.get("kind"), "created_at": s.get("created_at"),
            "photo_count": len(ana.get("photos", [])),
            "risk_count": len(ana.get("risks", []))}
