"""区域密度演示数据：构造一批横跨两个子区的已核验定位点。

服务启动后不会自动写入；通过 ``POST /api/region_dim/demo-batch`` 或前端按钮生成，
写入文件的位置和真实批次完全一致（geo_points + coord_transformed + grid_index），
因此密度看板、区域汇总和审批复算都走同一套生产逻辑，不做任何特判。
"""
from __future__ import annotations

from typing import Any

DEMO_BATCH = "REGION-DEMO-001"

# 15 个已核验定位点：西侧 6 个落在 REGION-A 的密集格，边界以西 3 个，
# 边界以东 6 个落在 REGION-B 的密集格；其中 wx4g0b 横跨两个子区。
DEMO_POINTS: list[tuple[float, float]] = [
    (39.9042, 116.3912), (39.9051, 116.3928), (39.9038, 116.3946),
    (39.9064, 116.3961), (39.9049, 116.3979), (39.9057, 116.3988),
    (39.9045, 116.4011), (39.9061, 116.4016),
    (39.9048, 116.4032), (39.9062, 116.4047), (39.9041, 116.4064),
    (39.9044, 116.4125), (39.9058, 116.4140), (39.9047, 116.4156),
    (39.9061, 116.4171),
]

# 迟到点：位于东城片区北侧的空格 wx4g11，靠近 wx4g0b 的密集格。
DEMO_LATE_POINT = {
    "point_id": 9001,
    "lat": 39.9095,
    "lon": 116.4135,
    "timestamp": 1793000000.0,
    "terminal": "TERM-DEMO-LATE",
}


def demo_verified_points(batch_key: str = DEMO_BATCH) -> list[dict[str, Any]]:
    """生成与坐标标准化结果同构的记录，便于直接写入 _transformed.jsonl。"""
    points = []
    for index, (lat, lon) in enumerate(DEMO_POINTS, start=1):
        points.append({
            "point_uid": f"{batch_key}:{index}",
            "point_id": index,
            "batch_key": batch_key,
            "orig_lat": lat,
            "orig_lon": lon,
            "trans_lat": lat,
            "trans_lon": lon,
            "timestamp": 1792900000.0 + index * 60,
            "terminal": f"TERM-DEMO-{index % 3 + 1:02d}",
            "accuracy": 8.0,
            "rule_version": "v2.0",
        })
    return points


def demo_raw_points(batch_key: str = DEMO_BATCH) -> list[dict[str, Any]]:
    """原始采集点，字段与 geo_source 写出的定位点一致。"""
    return [
        {
            "point_id": item["point_id"],
            "lat": item["orig_lat"],
            "lon": item["orig_lon"],
            "timestamp": item["timestamp"],
            "accuracy": item["accuracy"],
            "terminal": item["terminal"],
            "batch_key": batch_key,
        }
        for item in demo_verified_points(batch_key)
    ]
