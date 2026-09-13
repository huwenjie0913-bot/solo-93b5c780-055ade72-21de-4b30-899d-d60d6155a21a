"""生成带 EXIF（GPS/拍摄时间/焦距）的模拟航片，用于演示校核台。
用法: python make_demo.py [输出目录] [照片基准纬度 经度]
场景：3 条航带，含一处断带、一处前向重叠不足、两张重复拍摄、一张丢失 GPS。
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


def gen(outdir):
    os.makedirs(outdir, exist_ok=True)
    random.seed(7)
    t0 = 9 * 3600 + 12 * 60  # 09:12:00
    sec = t0
    n = 0
    plan = []  # (strip_idx, lat, lon, drop_gps)

    for strip in range(3):
        y = strip * STEP_SIDE
        heading = 0 if strip % 2 == 0 else 180  # 往复航线
        count = 14
        for i in range(count):
            x = i * STEP_ALONG if heading == 0 else (count - 1 - i) * STEP_ALONG
            # 第 1 条航带中段抽掉 2 张 → 断带
            if strip == 0 and i in (6, 7):
                continue
            # 第 2 条航带有一处间距拉大 → 重叠不足（沿飞行方向拉开）
            xx = x + (STEP_ALONG * 0.9 if strip == 1 and i >= 9 else 0) * (1 if heading == 0 else -1)
            lat, lon = m_to_lonlat(xx + random.uniform(-1, 1), y + random.uniform(-1, 1), LAT0, LON0)
            plan.append((strip, lat, lon, False))
            # 第 2 条航带第 4 张原地重复拍一张
            if strip == 1 and i == 3:
                plan.append((strip, lat, lon, False))

    # 一张丢失 GPS 的照片（插在计划中间）
    lat, lon = m_to_lonlat(5 * STEP_ALONG, STEP_SIDE, LAT0, LON0)
    plan.insert(len(plan) // 2, (1, lat, lon, True))

    sec = t0
    prev_strip = None
    for strip, lat, lon, drop in plan:
        if prev_strip is not None and strip != prev_strip:
            sec += 90  # 转弯间隔，触发航带切分
        prev_strip = strip
        n += 1
        h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
        dt = f"2026:09:13 {h:02d}:{m:02d}:{s:02d}"
        img = Image.new("RGB", (1600, 1200),
                        (random.randint(60, 200), random.randint(60, 200), random.randint(60, 200)))
        path = os.path.join(outdir, f"DJI_{n:04d}.jpg")
        if drop:
            img.save(path, quality=88)
        else:
            img.save(path, quality=88, exif=make_exif(lat, lon, ALT + 480, dt))
        sec += 3  # 拍摄间隔 3 秒
    print(f"已生成 {n} 张照片 → {outdir}")
    print(f"足迹估算: 旁向 {FP_ACROSS:.0f} m × 航向 {FP_ALONG:.0f} m, "
          f"航向间隔 {STEP_ALONG:.0f} m, 航带间距 {STEP_SIDE:.0f} m")


if __name__ == "__main__":
    gen(sys.argv[1] if len(sys.argv) > 1 else "demo_photos")
