# 航片覆盖校核台

无人机航测返场后的覆盖质量校核工具：批量导入航片，自动估算地面足迹、归并航带、
计算前向/旁向重叠率，标出断带、重叠不足与疑似重复拍摄的区段，支持人工排除照片、
补正缺失坐标，并导出包含问题照片编号、坐标与建议重拍范围的报告。

## 技术栈

- **后端**：Flask（API）+ Pillow（EXIF 元数据读取）+ sqlite3（项目与人工修正持久化，Python 自带）
- **前端**：无框架，标准 DOM + Canvas 2D

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install flask pillow   # 或复用已有环境
.venv/bin/python app.py
# 打开 http://127.0.0.1:5000
```

生成一批带 EXIF 的模拟航片（含断带、重叠不足、重复拍摄、丢 GPS 各一处）：

```bash
.venv/bin/python make_demo.py demo_photos
```

然后在页面上「新建项目 → 批量导入照片」即可看到完整校核结果。

## 计算依据

- **地面足迹**：`W = H·Sw/f`，`L = H·Sh/f`（H 飞行高度 AGL，Sw/Sh 传感器尺寸，f 焦距，
  均可在「航摄参数」中按机型修改；EXIF 焦距缺失时用默认焦距）
- **航带归并**：按拍摄时间排序，时间间隔 > 60 s、摄站间距跳变 > 2.5 倍航向足迹、
  或航向突变 > 70° 时切分新航带（距离过近的摄站对不参与转弯判断）
- **前向重叠率**：`(航向足迹 − 相邻摄站间距) / 航向足迹`，< 0 判为断带
- **旁向重叠率**：先以最近邻关系建立航带邻接图，再把相邻航带最近摄站的偏移分解到
  航向/垂直方向，以垂直分量估计带间距：`(旁向足迹 − 带间距) / 旁向足迹`
- **疑似重复**：相邻摄站间距 < max(1 m, 3% 航向足迹)

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/projects` | 项目列表 / 新建 |
| GET/PUT | `/api/projects/<id>` | 打开（含分析结果）/ 修改参数并重算 |
| POST | `/api/projects/<id>/photos` | 批量上传照片（multipart，`files` 字段） |
| POST | `/api/photos/<id>/exclude` | 排除 / 恢复照片 |
| POST | `/api/photos/<id>/correct` | 人工补正坐标（`{lat,lon,note}` 或 `{clear:true}`） |
| GET | `/api/photos/<id>/image` | 原图 |
| GET | `/api/projects/<id>/report.csv` `.json` | 导出报告 |

分析结果（航带、风险区段）由照片与参数即时算出，不落库；项目、照片元数据与人工
修正保存在 `data.db`，上传原图在 `uploads/`，再次打开项目即恢复现场。
