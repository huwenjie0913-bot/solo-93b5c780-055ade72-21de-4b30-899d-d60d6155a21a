"""生成带 EXIF（GPS/拍摄时间/焦距）的模拟航片，用于演示校核台。
用法:
  python make_demo.py [输出目录]
  python make_demo.py --reshoot [输出目录]   # 生成补拍返场照片（次日）

首航次场景：3 条航带，含一处断带、一处前向重叠不足、两张重复拍摄、一张丢失 GPS。
补拍场景：断带处补拍、重叠不足处补点、丢 GPS 的同名片重拍并带回坐标，
另有 2 张超出原范围的新增片、1 处 ~45 m 摄站偏移、1 处中段漏拍形成的新风险，
以及第 3 条航带末端漏拍 3 张（补拍缺失）。
"""
import math
import os
import sys
import random

from PIL import Image

ALT = 120.0          # 飞行高度 m
FOCAL = 8.8          # mm
SENSOR_W, SENSOR_H = 13.2, 8.8   # mm
LAT0, LON0 = 31.2304, 121.4737   # 上海人民广场附近

FP_ALONG = ALT * SENSOR_H / FOCAL   # 航向足迹 ~120m
FP_ACROSS = ALT * SENSOR_W / FOCAL  # 旁向足迹 ~180m
STEP_ALONG = FP_ALONG * 0.35        # 前向重叠 65%
STEP_SIDE = FP_ACROSS * 0.6         # 旁向重叠 40%


def m_to_lonlat(dx, dy, lat0, lon0):
    return (lat0 + dy / 110540.0,
            lon0 + dx / (111320.0 * math.cos(math.radians(lat0))))


def deg_to_dms(deg):
    d = int(abs(deg))
    m = int((abs(deg) - d) * 60)
    s = round((abs(deg) - d - m / 60) * 3600, 2)
    return (float(d), float(m), s)


def make_exif(lat, lon, alt, dt_str):
    exif = Image.Exif()
    exif[271] = "DemoUAV"           # Make
    exif[272] = "FC300X"            # Model
    exif[306] = dt_str              # DateTime
    exif_ifd = {36867: dt_str, 37386: 8.8}  # DateTimeOriginal, FocalLength
    gps_ifd = {
        0: b"\x02\x03\x00\x00",
        1: "N" if lat >= 0 else "S", 2: deg_to_dms(lat),
        3: "E" if lon >= 0 else "W", 4: deg_to_dms(lon),
        5: 0, 6: float(alt),
    }
    exif[0x8769] = exif_ifd
    exif[0x8825] = gps_ifd
    return exif


def build_first_flight():
    """返回 strips: [[(x, y, drop_gps, name_override), ...], ...]，按拍摄顺序。"""
    strips = [[], [], []]
    for i in range(14):
        # 第 1 条航带中段抽掉 2 张 → 断带
        if i in (6, 7):
            continue
        strips[0].append((i * STEP_ALONG, 0.0, False, None))
    for i in range(14):
        # 第 2 条航带 i>=9 沿飞行方向拉开 → 前向重叠不足（南向飞行）
        x = (13 - i) * STEP_ALONG - (STEP_ALONG * 0.9 if i >= 9 else 0.0)
        strips[1].append((x, STEP_SIDE, False, None))
        if i == 3:
            strips[1].append((x, STEP_SIDE, False, None))  # 原地重复拍一张
    for i in range(14):
        strips[2].append((i * STEP_ALONG, 2 * STEP_SIDE, False, None))

    # 一张丢失 GPS 的照片，插在第 2 条航带中段（编号实测为 DJI_0021）
    strips[1].insert(8, (5 * STEP_ALONG, STEP_SIDE, True, None))
    return strips


GPS_MISSING_NAME = 21  # 首航次丢 GPS 照片编号 DJI_0021.jpg


def build_reshoot():
    strips = [[], [], []]
    # 航带0（北向）：补齐断带，并向末端延伸 2 张（补拍新增）
    for i in range(16):
        strips[0].append((i * STEP_ALONG, 0.0, False, None))
    # 航带1（南向）：完整重飞，i=3 处仍有一张重复片；
    # 在拉开的间距处补一张，文件名沿用首航次丢 GPS 的 DJI_0021
    for i in range(14):
        x = (13 - i) * STEP_ALONG - (STEP_ALONG * 0.9 if i >= 9 else 0.0)
        strips[1].append((x, STEP_SIDE, False, None))
        if i == 3:
            strips[1].append((x, STEP_SIDE, False, None))
        if i == 8:
            strips[1].append(((13 - i) * STEP_ALONG - STEP_ALONG * 0.95,
                              STEP_SIDE, False, GPS_MISSING_NAME))
    # 航带2（北向）：中段漏拍 i=4（新的前向重叠不足风险），
    # i=7 摄站横移 ~45 m（位置偏移），末端 i=11~13 漏拍（补拍缺失）
    for i in range(14):
        if i in (4, 11, 12, 13):
            continue
        strips[2].append((i * STEP_ALONG + (45.0 if i == 7 else 0.0),
                          2 * STEP_SIDE, False, None))
    return strips


def gen(outdir, reshoot=False):
    os.makedirs(outdir, exist_ok=True)
    random.seed(27 if reshoot else 7)
    strips = build_reshoot() if reshoot else build_first_flight()
    day = "2026:09:14" if reshoot else "2026:09:13"
    sec = 9 * 3600 + (14 if reshoot else 12) * 60

    n = 0
    for si, sp in enumerate(strips):
        if si > 0:
            sec += 90  # 转弯间隔，触发航带切分
        for x, y, drop_gps, name_override in sp:
            n += 1
            jitter_x = random.uniform(-1, 1)
            jitter_y = random.uniform(-1, 1)
            lat = LAT0 + (y + jitter_y) / 110540.0
            lon = LON0 + (x + jitter_x) / (111320.0 * math.cos(math.radians(LAT0)))
            hh, mm, ss = sec // 3600, (sec % 3600) // 60, sec % 60
            dt = f"{day} {hh:02d}:{mm:02d}:{ss:02d}"
            img = Image.new("RGB", (1600, 1200),
                            (random.randint(60, 200), random.randint(60, 200),
                             random.randint(60, 200)))
            seq = name_override if name_override is not None else n
            path = os.path.join(outdir, f"DJI_{seq:04d}.jpg")
            if drop_gps:
                img.save(path, quality=88)
            else:
                img.save(path, quality=88, exif=make_exif(lat, lon, ALT + 480, dt))
            sec += 3
    print(f"已生成 {n} 张{'补拍' if reshoot else '首航次'}照片 → {outdir}")
    print(f"足迹估算: 旁向 {FP_ACROSS:.0f} m × 航向 {FP_ALONG:.0f} m, "
          f"航向间隔 {STEP_ALONG:.0f} m, 航带间距 {STEP_SIDE:.0f} m")


if __name__ == "__main__":
    args = sys.argv[1:]
    reshoot = False
    if args and args[0] == "--reshoot":
        reshoot = True
        args = args[1:]
    outdir = args[0] if args else ("reshoot_photos" if reshoot else "demo_photos")
    gen(outdir, reshoot=reshoot)
