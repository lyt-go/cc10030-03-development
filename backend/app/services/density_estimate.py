"""密度估算领域服务：对网格单元执行核密度估算，逐格生成核贡献、来源点和三类空格分类。

估算口径与区域维度共用 ``geo_density`` 里的同一套纯函数：网格按几何中心取点，
核贡献只由带宽决定，超过带宽的来源点贡献为 0。区域维度需要按上级区域拆分时，
直接走 ``RegionDimService.region_density_view``，本模块只提供网格级入口。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.geo_density import (
    CLASS_KERNEL_KEY,
    CLASS_OBSERVED_KEY,
    CLASS_ZERO_KEY,
    estimate,
    geohash_decode,
    geohash_encode,
    point_coords,
)
from app.store import store


def _expand_neighbors(seeds: set[str], precision: int, rings: int = 2) -> list[str]:
    """按网格尺寸向外扩指定圈数，得到邻域网格集合。"""
    step_lat = 180.0 / (2 ** ((5 * precision) // 2))
    step_lon = 360.0 / (2 ** ((5 * precision + 1) // 2))
    grids: set[str] = set(seeds)
    for grid in seeds:
        bounds = geohash_decode(grid)
        for dy in range(-rings, rings + 1):
            for dx in range(-rings, rings + 1):
                lat = bounds["center_lat"] + dy * step_lat
                lon = bounds["center_lon"] + dx * step_lon
                grids.add(geohash_encode(lat, lon, precision))
    return sorted(grids)


def _points_bbox(points: list[dict[str, Any]], padding_deg: float = 0.0) -> dict[str, float]:
    """点位包围盒，作为网格邻域视图的展示边界。"""
    coords = [point_coords(point) for point in points]
    lats = [item[0] for item in coords]
    lons = [item[1] for item in coords]
    return {
        "south": min(lats) - padding_deg,
        "west": min(lons) - padding_deg,
        "north": max(lats) + padding_deg,
        "east": max(lons) + padding_deg,
    }


class DensityEstimateService(BaseModuleService):
    MODULE = "density_estimate"
    ENTITY = "密度网格"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["估算编号", "网格编号", "带宽参数", "核函数类型", "估算密度", "样本点数", "置信区间", "估算版本"]
    REQUIRED_FIELDS = ["估算编号", "网格编号", "带宽参数"]
    STATUS_ORDER = ["草稿", "待估算", "已估算", "已过期"]
    ACTION_RULES = {"启动估算": "待估算", "确认估算": "已估算", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    # -- 数据读取：所有输入只走已核验的网格索引和坐标标准化数据集 --

    def _load_points(self, batch_key: str) -> list[dict[str, Any]]:
        """读取已核验的坐标标准化定位点；不完整的输入不允许进入估算。"""
        points_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(
                f"批次 {batch_key} 缺少已核验的坐标标准化数据集，不能用于密度估算")
        points = self._read_jsonl(points_file)
        for index, point in enumerate(points):
            point.setdefault("point_uid", f"{batch_key}:{point.get('point_id', index)}")
        return points

    def _load_grids(self, batch_key: str) -> list[dict[str, Any]]:
        """读取已生成的网格索引数据集。"""
        grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
        grids, _ = grid_svc.get_grid_index(batch_key)
        return grids

    # -- 主流程 --

    def estimate_density(self, batch_key: str, bandwidth: float = 500.0,
                          kernel: str = "gaussian", precision: int | None = None) -> dict[str, Any]:
        """对指定批次执行核密度估算，生成逐格核贡献与来源点数据集。

        网格集合 = 网格索引里的有落点网格 + 向外两圈的邻格。这样相邻格的扩散
        和带宽外的确为零都有位置落，空洞格不再被直接跳过；按上级区域拆分请走
        区域维度的 ``region_density_view``。
        """
        grids = self._load_grids(batch_key)
        points = self._load_points(batch_key)
        if not grids or not points:
            return {"batch_key": batch_key, "grids_estimated": 0}

        precision = precision or int(grids[0].get("precision", 6))
        seeds = {str(grid["geohash"]) for grid in grids}
        grid_ids = _expand_neighbors(seeds, precision, rings=2)
        region = {
            "region_id": "GRID-NEIGHBORHOOD",
            "name": "网格邻域",
            "bounds": _points_bbox(points, padding_deg=bandwidth / 111000.0 * 3),
        }
        result = estimate(
            batch_key=batch_key,
            points=points,
            region=region,
            bandwidth=bandwidth,
            kernel=kernel,
            precision=precision,
            grid_ids=grid_ids,
        )

        density_grids = []
        for cell in result["cells"]:
            density_grids.append({
                "grid_geohash": cell["grid_geohash"],
                "batch_key": batch_key,
                "grid_level": cell["grid_level"],
                "owner_region_id": cell["owner_region_id"],
                "bandwidth": bandwidth,
                "kernel": kernel,
                "estimated_density": round(
                    cell["kernel_sum"] / (max(cell["sample_count"], 1) * bandwidth), 8),
                "cell_class": cell["cell_class"],
                "cell_class_label": cell["cell_class_label"],
                "class_reason": cell["class_reason"],
                "sample_count": cell["sample_count"],
                "kernel_sum": cell["kernel_sum"],
                "source_point_keys": cell["source_point_keys"],
                "nearest_distance_m": cell["nearest_distance_m"],
                "contributions": cell["contributions"],
                "estimated_at": time.time(),
            })
        output_file = self._data_dir("density_estimate") / f"{batch_key}_density.jsonl"
        written = self._write_jsonl(output_file, density_grids)

        self._record_estimates(batch_key, density_grids, bandwidth)

        return {
            "batch_key": batch_key,
            "grids_processed": len(grids),
            "grids_estimated": written,
            "bandwidth": bandwidth,
            "kernel": kernel,
            "observed_cells": result["totals"]["observed_cells"],
            "kernel_cells": result["totals"]["kernel_cells"],
            "zero_cells": result["totals"]["zero_cells"],
            "sample_fingerprint": result["inputs"]["sample_fingerprint"],
            "region": "网格邻域",
            "output_file": str(output_file),
        }

    def _record_estimates(self, batch_key: str, rows: list[dict[str, Any]], bandwidth: float) -> None:
        """为每格登记估算记录；同一批次同一带宽重复估算时标记旧版本过期。"""
        tag = f"{bandwidth:g}"
        existing = {
            str(row.get("估算编号")): row for row in store.rows(self.MODULE)
            if str(row.get("估算编号", "")).startswith(f"DENS-{batch_key}-")
            and str(row.get("带宽参数")) == f"{tag}m"
        }
        for item in rows:
            entry, _ = self.create_entry({
                "估算编号": f"DENS-{batch_key}-{item['grid_geohash']}-{tag}",
                "网格编号": item["grid_geohash"],
                "带宽参数": f"{tag}m",
                "核函数类型": item["kernel"],
                "估算密度": f"{item['estimated_density']:.8f}",
                "样本点数": str(item["sample_count"]),
                "置信区间": "—" if item["cell_class"] == CLASS_ZERO_KEY else f"核贡献{item['kernel_sum']:.6f}",
                "估算版本": "v2.0",
            })
            if entry:
                self.run_action(entry["id"], "确认估算")
        for older in existing.values():
            older["status"] = "已过期"
            older["pending"] = False

    def get_density_estimates(self, batch_key: str, bandwidth: float | None = None) -> tuple[list[dict[str, Any]], str]:
        """读取批次的密度估算数据集；可按带宽过滤到某一版结果。"""
        file_path = self._data_dir("density_estimate") / f"{batch_key}_density.jsonl"
        data = self._read_jsonl(file_path)
        if bandwidth is not None:
            data = [row for row in data if float(row.get("bandwidth", 0)) == bandwidth]
        return data, str(file_path)

    def density_board(self, batch_key: str, bandwidth: float = 500.0,
                      kernel: str = "gaussian", region_id: int | str = "REGION-BJ",
                      precision: int = 6) -> dict[str, Any]:
        """密度看板：分列有落点、仅有核贡献、确为零三类格，并列出各子区采用点。"""
        region_svc = self._safe_import("app.services.region_dim", "RegionDimService")()
        return region_svc.density_board(
            batch_key=batch_key, parent_id=region_id,
            bandwidth=bandwidth, kernel=kernel, precision=precision)

    def compare_bandwidths(self, batch_key: str, bandwidths: list[float] | None = None,
                           kernel: str = "gaussian", region_id: int | str = "REGION-BJ",
                           precision: int = 6) -> dict[str, Any]:
        """用同一批定位点分别跑两种带宽，只让扩散贡献变化，落点归属保持不变。"""
        region_svc = self._safe_import("app.services.region_dim", "RegionDimService")()
        return region_svc.compare_bandwidths(
            batch_key=batch_key, bandwidths=bandwidths, parent_id=region_id,
            kernel=kernel, precision=precision)
