"""区域密度计算核心：geohash 网格几何、核贡献、空格分类与子区汇总。

这里只放纯函数，不读写文件，便于密度估算、区域维度和审批复算共用同一套口径：

* 网格按 geohash 精度切分，中心点固定取几何中心，避免「有点的格用落点重心、
  空格的用几何中心」造成两种口径混用；
* 每个来源点对网格的核贡献按距离衰减，超过带宽的点贡献为 0；
* 单元格分为三类：有落点、仅有核贡献、确为零；
* 子区归属按网格与子区边界的交叠面积唯一确定，跨区网格整格归属一个子区，
  因此区域样本合计始终回对同一批定位点，不会重复计数。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

CLASS_OBSERVED = "有落点"
CLASS_KERNEL = "仅有核贡献"
CLASS_ZERO = "确为零"

CLASS_OBSERVED_KEY = "observed"
CLASS_KERNEL_KEY = "kernel"
CLASS_ZERO_KEY = "zero"


# ---------------------------------------------------------------------------
# geohash 几何
# ---------------------------------------------------------------------------

def geohash_encode(lat: float, lon: float, precision: int = 6) -> str:
    """将经纬度编码为指定精度的 geohash。"""
    lat_range = (-90.0, 90.0)
    lon_range = (-180.0, 180.0)
    is_even = True
    bit = 0
    ch = 0
    geohash = ""
    while len(geohash) < precision:
        if is_even:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                ch |= 1 << (4 - bit)
                lon_range = (mid, lon_range[1])
            else:
                lon_range = (lon_range[0], mid)
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                ch |= 1 << (4 - bit)
                lat_range = (mid, lat_range[1])
            else:
                lat_range = (lat_range[0], mid)
        is_even = not is_even
        bit += 1
        if bit == 5:
            geohash += BASE32[ch]
            bit = 0
            ch = 0
    return geohash


def geohash_decode(geohash: str) -> dict[str, float]:
    """解码 geohash，返回边界与中心点。"""
    lat_range = (-90.0, 90.0)
    lon_range = (-180.0, 180.0)
    is_even = True
    for char in geohash:
        index = BASE32.index(char)
        for mask in (16, 8, 4, 2, 1):
            if is_even:
                mid = (lon_range[0] + lon_range[1]) / 2
                if index & mask:
                    lon_range = (mid, lon_range[1])
                else:
                    lon_range = (lon_range[0], mid)
            else:
                mid = (lat_range[0] + lat_range[1]) / 2
                if index & mask:
                    lat_range = (mid, lat_range[1])
                else:
                    lat_range = (lat_range[0], mid)
            is_even = not is_even
    return {
        "south": lat_range[0],
        "west": lon_range[0],
        "north": lat_range[1],
        "east": lon_range[1],
        "center_lat": (lat_range[0] + lat_range[1]) / 2,
        "center_lon": (lon_range[0] + lon_range[1]) / 2,
    }


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两坐标点球面距离（米）。"""
    radius = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def cell_area(bounds: dict[str, float]) -> float:
    """网格球面近似面积（平方米），纬度方向乘 cos(lat)。"""
    height = haversine(bounds["south"], bounds["west"], bounds["north"], bounds["west"])
    width = haversine(bounds["south"], bounds["west"], bounds["south"], bounds["east"])
    return max(height, 0.0) * max(width, 0.0)


def overlap_area(cell: dict[str, float], region: dict[str, float]) -> float:
    """网格与子区边界的交叠面积（平方米）；无交叠返回 0。"""
    south = max(cell["south"], region["south"])
    west = max(cell["west"], region["west"])
    north = min(cell["north"], region["north"])
    east = min(cell["east"], region["east"])
    if north <= south or east <= west:
        return 0.0
    height = haversine(south, west, north, west)
    width = haversine(south, west, south, east)
    return max(height, 0.0) * max(width, 0.0)


def kernel_weight(distance: float, bandwidth: float, kernel: str = "gaussian") -> float:
    """单个来源点对网格中心的核权重；超出带宽返回 0。"""
    if bandwidth <= 0:
        return 0.0
    if distance > bandwidth:
        return 0.0
    u = distance / bandwidth
    if kernel == "gaussian":
        return math.exp(-0.5 * u * u) / math.sqrt(2 * math.pi)
    if kernel == "uniform":
        return 1.0
    return max(0.0, 1 - u * u)  # Epanechnikov


# ---------------------------------------------------------------------------
# 点位与网格
# ---------------------------------------------------------------------------

def point_coords(point: dict[str, Any]) -> tuple[float, float]:
    """定位点坐标：优先取坐标标准化后的字段。"""
    lat = point.get("trans_lat", point.get("lat"))
    lon = point.get("trans_lon", point.get("lon"))
    return float(lat or 0.0), float(lon or 0.0)


def point_key(point: dict[str, Any]) -> str:
    """定位点稳定标识：用于核对合计是否回对同一批点。"""
    if point.get("point_uid"):
        return str(point["point_uid"])
    if point.get("point_id") is not None:
        return f"{point.get('batch_key', '')}:{point['point_id']}"
    lat, lon = point_coords(point)
    return f"{lat:.6f},{lon:.6f}@{point.get('timestamp', '')}"


def cell_grid(geohash: str, precision: int = 6) -> dict[str, Any]:
    """网格元数据：编号、层级、边界、中心。"""
    bounds = geohash_decode(geohash)
    return {
        "grid_geohash": geohash,
        "level": f"L{precision}",
        "precision": precision,
        "bounds": bounds,
        "center_lat": round(bounds["center_lat"], 6),
        "center_lon": round(bounds["center_lon"], 6),
    }


def grid_ids_for_region(region: dict[str, float], precision: int = 6,
                        points: Iterable[dict[str, Any]] = ()) -> list[str]:
    """枚举与子区相交的网格。

    按网格尺寸在子区包围盒内步进取样，再用交叠面积过滤；这样边界上的空格
    不会漏，也不会把整张地图都枚举出来。精度越细、区域越大枚举越多，
    超过 20 万个候选点直接报错，提示调用方换粗一级的网格层级。
    """
    lat_bits = (5 * precision) // 2
    lon_bits = (5 * precision + 1) // 2
    step_lat = 180.0 / (2 ** lat_bits)
    step_lon = 360.0 / (2 ** lon_bits)
    lats = _steps(region["south"], region["north"], step_lat / 2)
    lons = _steps(region["west"], region["east"], step_lon / 2)
    if len(lats) * len(lons) > 200_000:
        raise ValueError(
            f"区域在 L{precision} 网格下需要枚举 {len(lats) * len(lons)} 个候选格，请换更粗的层级")

    candidates = {
        geohash_encode(lat, lon, precision)
        for lat in lats for lon in lons
    }
    for point in points:
        lat, lon = point_coords(point)
        if region["south"] <= lat <= region["north"] and region["west"] <= lon <= region["east"]:
            candidates.add(geohash_encode(lat, lon, precision))

    return sorted(
        grid for grid in candidates
        if overlap_area(geohash_decode(grid), region) > 0
    )


def _steps(start: float, end: float, step: float) -> list[float]:
    """从 start 到 end 的等距取样，两端都包含。"""
    if step <= 0 or end < start:
        return [start]
    count = int((end - start) / step)
    values = [start + index * step for index in range(count + 1)]
    if values[-1] < end:
        values.append(end)
    return values


def assign_region(cell_bounds: dict[str, float], regions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按交叠面积把网格唯一归属到一个子区；并列时取区域编号较小者，保证可复算。"""
    best = None
    best_area = 0.0
    for region in regions:
        area = overlap_area(cell_bounds, region["bounds"])
        if area <= 0:
            continue
        if area > best_area or (area == best_area and best is not None
                                and str(region["region_id"]) < str(best["region_id"])):
            best = region
            best_area = area
    return best


# ---------------------------------------------------------------------------
# 单次估算（可复算的输入指纹 + 结果）
# ---------------------------------------------------------------------------

def estimate(
    *,
    batch_key: str,
    points: list[dict[str, Any]],
    region: dict[str, Any],
    all_regions: list[dict[str, Any]] | None = None,
    bandwidth: float = 500.0,
    kernel: str = "gaussian",
    precision: int = 6,
    include_regions: list[str] | None = None,
    grid_ids: list[str] | None = None,
) -> dict[str, Any]:
    """对目标子区做一次可复算的密度估算。

    返回结构直接用于审批持久化和看板展示：
    ``inputs`` 记录网格精度、带宽、核函数、样本点指纹；``cells`` 逐格给出
    核贡献、来源点、分类；``subregions`` 给出上级区域下各子区的采用点。

    ``grid_ids`` 用于复算旧审批：传入当时实际采用的网格集合时，不再按区域
    边界重新枚举，保证复算口径与审批时完全一致。
    """
    if all_regions is None:
        all_regions = [region]
    if grid_ids is not None:
        scoped_grids = sorted(set(grid_ids))
    else:
        scoped_grids = grid_ids_for_region(region["bounds"], precision, points)
    regions_for_cells = all_regions if include_regions is None else [
        item for item in all_regions if str(item["region_id"]) in set(include_regions)
    ]
    if not regions_for_cells:
        regions_for_cells = [region]

    # 先按来源点所在网格归组：有落点的格直接已知来源点；空格的核贡献再逐点算距离。
    observed: dict[str, list[dict[str, Any]]] = {}
    point_lookup: list[tuple[dict[str, Any], float, float]] = []
    for point in points:
        lat, lon = point_coords(point)
        observed.setdefault(geohash_encode(lat, lon, precision), []).append(point)
        point_lookup.append((point, lat, lon))

    cells = []
    for grid in scoped_grids:
        meta = cell_grid(grid, precision)
        bounds = meta["bounds"]
        owner = assign_region(bounds, regions_for_cells) or region
        direct_points = observed.get(grid, [])
        contributions = []
        kernel_sum = 0.0
        nearest_distance = None
        for point, lat, lon in point_lookup:
            distance = haversine(meta["center_lat"], meta["center_lon"], lat, lon)
            if nearest_distance is None or distance < nearest_distance:
                nearest_distance = distance
            if distance > bandwidth:
                continue
            weight = kernel_weight(distance, bandwidth, kernel)
            if weight <= 0:
                continue
            kernel_sum += weight
            contributions.append({
                "point_key": point_key(point),
                "point_id": point.get("point_id"),
                "distance_m": round(distance, 1),
                "weight": round(weight, 8),
            })
        contributions.sort(key=lambda item: (-item["weight"], item["point_key"]))

        if direct_points:
            cell_class = CLASS_OBSERVED_KEY
            reason = "网格内有直接观测落点"
        elif kernel_sum > 0:
            cell_class = CLASS_KERNEL_KEY
            reason = f"网格内无落点，但 {len(contributions)} 个来源点在 {bandwidth:g}m 带宽内"
        else:
            cell_class = CLASS_ZERO_KEY
            reason = f"最近的来源点距网格中心 {round(nearest_distance or 0)}m，超出 {bandwidth:g}m 带宽"

        sample_count = len(direct_points)
        cells.append({
            "grid_geohash": grid,
            "grid_level": meta["level"],
            "owner_region_id": owner["region_id"],
            "owner_region_name": owner["name"],
            "bounds": [bounds["south"], bounds["west"], bounds["north"], bounds["east"]],
            "center": [meta["center_lat"], meta["center_lon"]],
            "cell_class": cell_class,
            "cell_class_label": {
                CLASS_OBSERVED_KEY: CLASS_OBSERVED,
                CLASS_KERNEL_KEY: CLASS_KERNEL,
                CLASS_ZERO_KEY: CLASS_ZERO,
            }[cell_class],
            "class_reason": reason,
            "sample_count": sample_count,
            "source_point_keys": sorted(point_key(item) for item in direct_points),
            "kernel_sum": round(kernel_sum, 8),
            "kernel_mean": round(kernel_sum / max(sample_count, 1), 8),
            "contribution_count": len(contributions),
            "contributions": contributions[:10],
            "nearest_distance_m": round(nearest_distance, 1) if nearest_distance is not None else None,
        })

    subregions = []
    for item in regions_for_cells:
        sample_points = [
            point for point in points
            if item["bounds"]["south"] <= point_coords(point)[0] <= item["bounds"]["north"]
            and item["bounds"]["west"] <= point_coords(point)[1] <= item["bounds"]["east"]
        ]
        owned = [cell for cell in cells if cell["owner_region_id"] == item["region_id"]]
        subregions.append({
            "region_id": item["region_id"],
            "region_name": item["name"],
            "sample_count": len(sample_points),
            "sample_point_keys": sorted(point_key(point) for point in sample_points),
            "grid_count": len(owned),
            "observed_cells": sum(1 for cell in owned if cell["cell_class"] == CLASS_OBSERVED_KEY),
            "kernel_cells": sum(1 for cell in owned if cell["cell_class"] == CLASS_KERNEL_KEY),
            "zero_cells": sum(1 for cell in owned if cell["cell_class"] == CLASS_ZERO_KEY),
            "kernel_sum": round(sum(cell["kernel_sum"] for cell in owned), 8),
        })

    totals = {
        "sample_count": len(points),
        "subregion_sample_count": sum(item["sample_count"] for item in subregions),
        "grid_count": len(cells),
        "observed_cells": sum(1 for cell in cells if cell["cell_class"] == CLASS_OBSERVED_KEY),
        "kernel_cells": sum(1 for cell in cells if cell["cell_class"] == CLASS_KERNEL_KEY),
        "zero_cells": sum(1 for cell in cells if cell["cell_class"] == CLASS_ZERO_KEY),
        "kernel_sum": round(sum(cell["kernel_sum"] for cell in cells), 8),
    }

    return {
        "batch_key": batch_key,
        "region_id": region["region_id"],
        "region_name": region["name"],
        "inputs": {
            "grid_precision": precision,
            "grid_level": f"L{precision}",
            "bandwidth": bandwidth,
            "kernel": kernel,
            "sample_count": len(points),
            "sample_fingerprint": fingerprint(points),
            "region_bounds": region["bounds"],
            "scope_region_ids": [item["region_id"] for item in regions_for_cells],
        },
        "cells": cells,
        "subregions": subregions,
        "totals": totals,
    }


def fingerprint(records: Iterable[dict[str, Any]]) -> str:
    """对一组输入记录做稳定指纹，审批时用于判断输入是否仍为同一批。"""
    payload = json.dumps(list(records), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def classify_points(points: list[dict[str, Any]], precision: int = 6) -> dict[str, list[dict[str, Any]]]:
    """按网格分组定位点，供子区采用点清单使用。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        lat, lon = point_coords(point)
        grouped.setdefault(geohash_encode(lat, lon, precision), []).append(point)
    return grouped
