"""区域维度领域服务：区域层级、按上级区域汇总的密度贡献、对照审批与历史复算。

本服务把「区域」从纯维度表扩展成可复核的口径中心：

* ``region_density_view`` 按父区域汇总各子区的密度贡献，明确每块子区采用的
  定位点、网格、带宽和样本量；
* 每次对照审批都会把实际采用的网格集合与逐格结果持久化到文件，确认后仍可
  用当时的输入快照复算；
* 迟到定位点必须独立通过位置核验才可进入已核验输入；正在打开、尚未确认的
  审批不会因为迟到点而改动，历史对账和更正补采是确认时的两个显式选择。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.geo_density import (
    CLASS_KERNEL_KEY,
    CLASS_OBSERVED_KEY,
    CLASS_ZERO_KEY,
    estimate,
    fingerprint,
    geohash_decode,
    geohash_encode,
    overlap_area,
    point_coords,
    point_key,
)
from app.store import store

APPROVAL_DIR = "region_approvals"


class RegionDimService(BaseModuleService):
    MODULE = "region_dim"
    ENTITY = "区域层级"
    OPERATION_TERM = "入湖"
    LIST_FIELDS = ["区域编号", "上级区域", "区域名称", "区域级别", "边界编码", "网格数量", "维度版本", "生效时间"]
    REQUIRED_FIELDS = ["区域编号", "上级区域", "区域名称"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
    ACTION_RULES = {"提交校验": "待校验", "发布维度": "已发布", "标记下线": "已下线"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 区域层级与边界
    # ------------------------------------------------------------------

    @staticmethod
    def _region_from_row(row: dict[str, Any]) -> dict[str, Any]:
        bounds = row.get("边界范围") or {}
        return {
            "region_id": str(row.get("区域编号")),
            "name": row.get("区域名称") or "未知区域",
            "level": row.get("区域级别") or "未知",
            "parent_id": str(row.get("上级区域编号") or ""),
            "parent_name": row.get("上级区域") or "",
            "boundary_code": row.get("边界编码") or "",
            "status": row.get("status") or "",
            "bounds": {
                "south": float(bounds.get("south", 0.0)),
                "west": float(bounds.get("west", 0.0)),
                "north": float(bounds.get("north", 0.0)),
                "east": float(bounds.get("east", 0.0)),
            },
            "has_bounds": bool(bounds),
        }

    def _region(self, region_id: str | int) -> dict[str, Any] | None:
        """按区域编号或行 id 查找区域，并整理成计算用结构。"""
        for item in store.rows(self.MODULE):
            if str(item.get("区域编号")) == str(region_id):
                return self._region_from_row(item)
        if str(region_id).isdigit():
            row = store.find(self.MODULE, int(region_id))
            if row is not None:
                return self._region_from_row(row)
        return None

    def _subregions(self, parent_id: str | int) -> list[dict[str, Any]]:
        """取父区域下的直属子区，按区域编号排序保证结果稳定。"""
        parent = self._region(parent_id)
        if parent is None:
            return []
        target_ids = {parent["region_id"], parent["name"]}
        children = [
            self._region_from_row(row) for row in store.rows(self.MODULE)
            if str(row.get("上级区域编号") or row.get("上级区域")) in target_ids
            and str(row.get("区域编号")) != parent["region_id"]
        ]
        return sorted(children, key=lambda item: item["region_id"])

    def get_hierarchy(self, parent_id: str | int = "REGION-BJ") -> dict[str, Any]:
        """返回父区域及其子区边界，供前端画出密度贡献视图。"""
        parent = self._region(parent_id)
        if parent is None:
            return {"parent": None, "subregions": []}
        return {"parent": parent, "subregions": self._subregions(parent["region_id"])}

    # ------------------------------------------------------------------
    # 已核验输入
    # ------------------------------------------------------------------

    def _load_verified_points(self, batch_key: str) -> list[dict[str, Any]]:
        """只读取坐标标准化后的已核验定位点；缺少该数据集时拒绝估算。"""
        points_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(
                f"批次 {batch_key} 缺少已核验的坐标标准化数据集，不能用于区域密度估算")
        points = self._read_jsonl(points_file)
        for index, point in enumerate(points):
            point.setdefault("point_uid", f"{batch_key}:{point.get('point_id', index)}")
        return points

    # ------------------------------------------------------------------
    # 按上级区域汇总密度贡献
    # ------------------------------------------------------------------

    def region_density_view(self, batch_key: str, parent_id: str | int = "REGION-BJ",
                            bandwidth: float = 500.0, kernel: str = "gaussian",
                            precision: int = 6, with_cells: bool = True) -> dict[str, Any]:
        """按父区域计算密度贡献：每块子区记录采用的点、网格、权重和样本量。

        网格按交叠面积唯一归属一个子区，子区样本量按点是否落在子区边界内统计；
        两者相加都能回对父区域的同一批定位点，跨区网格不会被重复计数。
        """
        parent = self._region(parent_id)
        if parent is None:
            raise LookupError(f"上级区域 {parent_id} 不存在")
        if not parent["has_bounds"]:
            raise ValueError(f"上级区域 {parent['name']} 未配置边界范围")
        subregions = self._subregions(parent["region_id"])
        if not subregions:
            raise LookupError(f"上级区域 {parent['name']} 下没有可汇总的子区")
        missing_bounds = [item["name"] for item in subregions if not item["has_bounds"]]
        if missing_bounds:
            raise ValueError(f"子区未配置边界范围：{'、'.join(missing_bounds)}")

        points = self._load_verified_points(batch_key)
        parent_points = [point for point in points if _in_bounds(point_coords(point), parent["bounds"])]
        result = estimate(
            batch_key=batch_key,
            points=parent_points,
            region=parent,
            all_regions=subregions,
            bandwidth=bandwidth,
            kernel=kernel,
            precision=precision,
        )

        owned: dict[str, list[dict[str, Any]]] = {item["region_id"]: [] for item in subregions}
        for cell in result["cells"]:
            owned.setdefault(cell["owner_region_id"], []).append(cell)
        boundary_grids = self._boundary_grids(subregions, precision)

        subregion_views = []
        for subregion in subregions:
            cells = owned.get(subregion["region_id"], [])
            sample = [item for item in result["subregions"]
                      if item["region_id"] == subregion["region_id"]]
            sample_info = sample[0] if sample else {}
            zero_cells = [cell for cell in cells if cell["cell_class"] == CLASS_ZERO_KEY]
            subregion_views.append({
                "region_id": subregion["region_id"],
                "region_name": subregion["name"],
                "bounds": subregion["bounds"],
                "sample_count": sample_info.get("sample_count", 0),
                "sample_points": sample_info.get("sample_point_keys", []),
                "grid_count": len(cells),
                "grids": [cell["grid_geohash"] for cell in cells],
                "observed_cells": sum(1 for cell in cells if cell["cell_class"] == CLASS_OBSERVED_KEY),
                "kernel_cells": sum(1 for cell in cells if cell["cell_class"] == CLASS_KERNEL_KEY),
                "zero_cells": len(zero_cells),
                "kernel_sum": round(sum(cell["kernel_sum"] for cell in cells), 8),
                "weight_total": round(sum(cell["kernel_sum"] for cell in cells), 8),
                "bandwidth": bandwidth,
                "kernel": kernel,
                "grid_level": f"L{precision}",
                "backfill_gaps": [
                    {
                        "grid_geohash": cell["grid_geohash"],
                        "nearest_distance_m": cell["nearest_distance_m"],
                        "reason": cell["class_reason"],
                    }
                    for cell in zero_cells
                ],
            })

        adopted_keys = sorted({key for item in result["subregions"] for key in item["sample_point_keys"]})
        parent_keys = sorted(point_key(point) for point in parent_points)
        missing = sorted(set(parent_keys) - set(adopted_keys))

        view = {
            "batch_key": batch_key,
            "parent": parent,
            "bandwidth": bandwidth,
            "kernel": kernel,
            "grid_level": f"L{precision}",
            "sample_fingerprint": result["inputs"]["sample_fingerprint"],
            "sample_count": len(parent_points),
            "subregions": subregion_views,
            "totals": result["totals"],
            "reconciliation": {
                "parent_sample_count": len(parent_points),
                "subregion_sample_total": sum(item["sample_count"] for item in subregion_views),
                "unassigned_points": missing,
                "point_sets_match": not missing and len(adopted_keys) == len(parent_keys),
                "cross_boundary_grids": [
                    {
                        "grid_geohash": grid,
                        "owners": [
                            {
                                "region_id": region["region_id"],
                                "overlap_ratio": round(
                                    overlap_area(geohash_decode(grid), region["bounds"])
                                    / max(overlap_area(geohash_decode(grid), parent["bounds"]), 1e-9), 4),
                            }
                            for region in subregions
                            if overlap_area(geohash_decode(grid), region["bounds"]) > 0
                        ],
                    }
                    for grid in boundary_grids
                ],
                "note": "跨区网格整格归属交叠面积较大的子区，父区域合计不会重复计入",
            },
        }
        if with_cells:
            view["cells"] = result["cells"]
        return view

    @staticmethod
    def _boundary_grids(subregions: list[dict[str, Any]], precision: int) -> list[str]:
        """找出与两个及以上子区都有交叠的网格，用于说明边界交叠情况。

        沿每个子区的四条边按半格步长取样，避免只取四角时漏掉横跨边界的中段网格。
        """
        step_lat = (180.0 / (2 ** ((5 * precision) // 2))) / 2
        step_lon = (360.0 / (2 ** ((5 * precision + 1) // 2))) / 2
        seeds: set[str] = set()
        for region in subregions:
            bounds = region["bounds"]
            lat = bounds["south"]
            while lat <= bounds["north"]:
                seeds.add(geohash_encode(lat, bounds["west"], precision))
                seeds.add(geohash_encode(lat, bounds["east"], precision))
                lat += step_lat
            lon = bounds["west"]
            while lon <= bounds["east"]:
                seeds.add(geohash_encode(bounds["south"], lon, precision))
                seeds.add(geohash_encode(bounds["north"], lon, precision))
                lon += step_lon
            seeds.add(geohash_encode(bounds["north"], bounds["east"], precision))
        result = []
        for grid in sorted(seeds):
            cell = geohash_decode(grid)
            if sum(1 for region in subregions if overlap_area(cell, region["bounds"]) > 0) > 1:
                result.append(grid)
        return result

    def density_board(self, batch_key: str, parent_id: str | int = "REGION-BJ",
                      bandwidth: float = 500.0, kernel: str = "gaussian",
                      precision: int = 6) -> dict[str, Any]:
        """密度看板：分别展示有落点、仅有核贡献、确为零的格，并列出各子区采用点。"""
        view = self.region_density_view(
            batch_key, parent_id, bandwidth, kernel, precision, with_cells=True)
        groups: dict[str, list[dict[str, Any]]] = {
            CLASS_OBSERVED_KEY: [], CLASS_KERNEL_KEY: [], CLASS_ZERO_KEY: [],
        }
        for cell in view["cells"]:
            groups[cell["cell_class"]].append({
                "grid_geohash": cell["grid_geohash"],
                "center": cell["center"],
                "owner_region_id": cell["owner_region_id"],
                "owner_region_name": cell["owner_region_name"],
                "sample_count": cell["sample_count"],
                "kernel_sum": cell["kernel_sum"],
                "nearest_distance_m": cell["nearest_distance_m"],
                "reason": cell["class_reason"],
                "sources": [
                    {"point_key": item["point_key"], "weight": item["weight"],
                     "distance_m": item["distance_m"]}
                    for item in cell["contributions"]
                ],
            })
        return {
            "batch_key": batch_key,
            "parent": view["parent"],
            "inputs": {
                "bandwidth": bandwidth,
                "kernel": kernel,
                "grid_level": view["grid_level"],
                "sample_count": view["sample_count"],
                "sample_fingerprint": view["sample_fingerprint"],
            },
            "columns": [
                {"key": CLASS_OBSERVED_KEY, "label": "有落点", "count": len(groups[CLASS_OBSERVED_KEY]),
                 "description": "网格内有直接观测落点"},
                {"key": CLASS_KERNEL_KEY, "label": "仅有核贡献", "count": len(groups[CLASS_KERNEL_KEY]),
                 "description": "无落点，但带宽内有来源点扩散"},
                {"key": CLASS_ZERO_KEY, "label": "确为零", "count": len(groups[CLASS_ZERO_KEY]),
                 "description": "所有来源点都在带宽外，属于待补采缺口"},
            ],
            "cells": groups,
            "subregions": view["subregions"],
            "totals": view["totals"],
            "reconciliation": view["reconciliation"],
        }

    def compare_bandwidths(self, batch_key: str, bandwidths: list[float] | None = None,
                           parent_id: str | int = "REGION-BJ", kernel: str = "gaussian",
                           precision: int = 6) -> dict[str, Any]:
        """用同一批定位点分别跑两种带宽，只让扩散贡献变化，落点归属保持不变。"""
        bandwidths = bandwidths or [500.0, 1500.0]
        rounds = [
            self.region_density_view(batch_key, parent_id, bandwidth, kernel, precision,
                                     with_cells=False)
            for bandwidth in bandwidths
        ]
        fingerprints = {round_["sample_fingerprint"] for round_ in rounds}
        return {
            "batch_key": batch_key,
            "parent": rounds[0]["parent"],
            "kernel": kernel,
            "rounds": [
                {
                    "bandwidth": round_["bandwidth"],
                    "sample_count": round_["sample_count"],
                    "sample_fingerprint": round_["sample_fingerprint"],
                    "observed_cells": round_["totals"]["observed_cells"],
                    "kernel_cells": round_["totals"]["kernel_cells"],
                    "zero_cells": round_["totals"]["zero_cells"],
                    "kernel_sum": round_["totals"]["kernel_sum"],
                    "subregions": [
                        {
                            "region_id": item["region_id"],
                            "region_name": item["region_name"],
                            "sample_count": item["sample_count"],
                            "sample_points": item["sample_points"],
                            "grid_count": item["grid_count"],
                            "observed_cells": item["observed_cells"],
                            "kernel_cells": item["kernel_cells"],
                            "zero_cells": item["zero_cells"],
                            "kernel_sum": item["kernel_sum"],
                        }
                        for item in round_["subregions"]
                    ],
                    "reconciliation": round_["reconciliation"],
                }
                for round_ in rounds
            ],
            "same_sample_set": len(fingerprints) == 1,
            "note": "两轮使用同一批定位点，差异只来自带宽决定的扩散贡献",
        }

    # ------------------------------------------------------------------
    # 对照审批：持久化网格与结果
    # ------------------------------------------------------------------

    def _approval_path(self, approval_id: str) -> Path:
        return self._data_dir(APPROVAL_DIR) / f"{approval_id}.json"

    def _save_approval(self, approval: dict[str, Any]) -> Path:
        path = self._approval_path(approval["approval_id"])
        path.write_text(json.dumps(approval, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        return path

    def list_approvals(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        """列出已持久化的对照审批，按打开时间倒序。"""
        approvals = []
        for path in self._data_dir(APPROVAL_DIR).glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if batch_key and data.get("batch_key") != batch_key:
                continue
            approvals.append(data)
        return sorted(approvals, key=lambda item: item.get("opened_at", 0), reverse=True)

    def get_approval(self, approval_id: str, refresh_late: bool = False) -> dict[str, Any] | None:
        """读取审批；``refresh_late`` 为真时顺带把当前已核验输入里的新点登记为迟到点。"""
        path = self._approval_path(approval_id)
        if not path.exists():
            return None
        approval = json.loads(path.read_text(encoding="utf-8"))
        if refresh_late:
            self._refresh_late_marks(approval)
            self._save_approval(approval)
        return approval

    def create_comparison_approval(self, batch_key: str, parent_id: str | int = "REGION-BJ",
                                   bandwidth: float = 500.0, kernel: str = "gaussian",
                                   precision: int = 6, label: str = "") -> dict[str, Any]:
        """打开一份对照审批：冻结当前已核验输入，持久化实际采用的网格和结果。

        审批一旦打开就使用输入快照；随后到达的迟到点必须在独立核验后才可进入
        已核验输入，并且不会自动改写这份尚未确认的审批。
        """
        view = self.region_density_view(batch_key, parent_id, bandwidth, kernel, precision)
        points = self._load_verified_points(batch_key)
        point_index = {point_key(point): point for point in points}
        scope_keys = sorted({key for item in view["subregions"] for key in item["sample_points"]})
        snapshot = [
            {
                "point_key": key,
                "lat": point_coords(point_index[key])[0],
                "lon": point_coords(point_index[key])[1],
                "timestamp": point_index[key].get("timestamp"),
            }
            for key in scope_keys if key in point_index
        ]
        sequence = len(self.list_approvals(batch_key)) + 1
        approval_id = f"RA-{batch_key}-{sequence:03d}"
        approval = {
            "approval_id": approval_id,
            "label": label or f"{bandwidth:g}m 对照审批",
            "batch_key": batch_key,
            "parent_id": view["parent"]["region_id"],
            "parent_name": view["parent"]["name"],
            "bandwidth": bandwidth,
            "kernel": kernel,
            "grid_level": f"L{precision}",
            "precision": precision,
            "status": "待复核",
            "opened_at": time.time(),
            "confirmed_at": None,
            "confirmed_by": "",
            "decision": "",
            "grid_ids": sorted({grid for item in view["subregions"] for grid in item["grids"]}),
            # 区域边界也写进审批，维度表后续调整不会影响旧合计的复算。
            "region_bounds": {
                "parent": view["parent"]["bounds"],
                "subregions": {
                    item["region_id"]: {
                        "name": item["region_name"],
                        "bounds": item["bounds"],
                    }
                    for item in view["subregions"]
                },
            },
            "input_snapshot": {
                "sample_count": len(scope_keys),
                "sample_fingerprint": fingerprint(snapshot),
                "sample_point_keys": scope_keys,
                "points": snapshot,
                "source": "coord_transformed",
                "verified": True,
            },
            "result": {
                "sample_count": view["sample_count"],
                "sample_fingerprint": view["sample_fingerprint"],
                "subregions": [
                    {
                        "region_id": item["region_id"],
                        "region_name": item["region_name"],
                        "sample_count": item["sample_count"],
                        "sample_points": item["sample_points"],
                        "grids": item["grids"],
                        "observed_cells": item["observed_cells"],
                        "kernel_cells": item["kernel_cells"],
                        "zero_cells": item["zero_cells"],
                        "kernel_sum": item["kernel_sum"],
                        "weight_total": item["weight_total"],
                    }
                    for item in view["subregions"]
                ],
                "totals": view["totals"],
                "reconciliation": view["reconciliation"],
                "cells": [
                    {
                        "grid_geohash": cell["grid_geohash"],
                        "owner_region_id": cell["owner_region_id"],
                        "cell_class": cell["cell_class"],
                        "sample_count": cell["sample_count"],
                        "kernel_sum": cell["kernel_sum"],
                        "source_point_keys": cell["source_point_keys"],
                    }
                    for cell in view["cells"]
                ],
            },
            "late_points": [],
            "backfill_plan": [],
            "history": [{"at": time.time(), "event": "打开审批",
                         "detail": "冻结输入快照并保存实际采用的网格与逐格结果"}],
        }
        self._save_approval(approval)
        return approval

    def _refresh_late_marks(self, approval: dict[str, Any]) -> None:
        """把本次审批打开之后新增的已核验点登记为迟到点。

        核验状态与审批是否确认无关：只要定位点在已核验输入里带有
        ``position_verified`` 标记，就视为已通过位置核验，可直接被后续审批采用。
        """
        known = set(approval["input_snapshot"]["sample_point_keys"])
        current = {item["point_key"] for item in approval["late_points"]}
        parent = self._region(approval["parent_id"])
        subregions = self._subregions(parent["region_id"]) if parent else []
        for point in self._load_verified_points(approval["batch_key"]):
            key = point_key({**point, "batch_key": approval["batch_key"]})
            if key in known:
                continue
            lat, lon = point_coords(point)
            target = next((item for item in subregions if _in_bounds((lat, lon), item["bounds"])), None)
            verified = bool(point.get("position_verified"))
            record = {
                "point_key": key,
                "lat": lat,
                "lon": lon,
                "timestamp": point.get("timestamp"),
                "arrived_at": time.time(),
                "verification": "已通过" if verified else "待核验",
                "region_id": target["region_id"] if target and verified else "",
                "region_name": target["name"] if target and verified else "",
                "note": ("此前已独立通过位置核验" if verified else ""),
            }
            if key in current:
                for existing in approval["late_points"]:
                    if existing["point_key"] == key and existing["verification"] != "已通过" and verified:
                        existing.update(record)
                continue
            approval["late_points"].append(record)

    @staticmethod
    def _manual_key(point: dict[str, Any], batch_key: str) -> str:
        """外部传入的迟到点可能没有 point_uid，单列一套稳定键避免和已核验点冲突。"""
        if point.get("point_uid"):
            return str(point["point_uid"])
        if point.get("point_id") is not None:
            return f"{batch_key}:{point['point_id']}"
        lat, lon = point_coords(point)
        return f"{batch_key}:{lat:.6f},{lon:.6f}@{point.get('timestamp', '')}"

    def verify_late_point(self, approval_id: str, point: dict[str, Any],
                          region_id: str | None = None) -> dict[str, Any]:
        """独立核验一个迟到定位点的位置，通过后才允许追加进已核验输入。

        核验只做位置与完整性判断，不依赖审批是否确认；因此第二次审批打开后、
        尚未确认时，邻区迟到点也能独立走完这套核验。
        """
        approval = self.get_approval(approval_id)
        if approval is None:
            raise LookupError(f"审批 {approval_id} 不存在")
        parent = self._region(approval["parent_id"])
        if parent is None:
            # 维度表调整后仍可用审批里冻结的边界做位置核验
            stored = (approval.get("region_bounds") or {}).get("parent")
            if not stored:
                raise LookupError(f"上级区域 {approval['parent_id']} 不存在")
            parent = {
                "region_id": approval["parent_id"], "name": approval["parent_name"],
                "level": "审批快照", "parent_id": "", "parent_name": "",
                "boundary_code": "", "status": approval["status"],
                "bounds": stored, "has_bounds": True,
            }
        lat, lon = point_coords(point)
        batch_key = str(point.get("batch_key") or approval["batch_key"])
        errors: list[str] = []
        if batch_key != approval["batch_key"]:
            errors.append("批次不一致")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            errors.append("坐标越界")
        if not point.get("timestamp"):
            errors.append("缺少时间戳")
        if not _in_bounds((lat, lon), parent["bounds"]):
            errors.append("不在上级区域范围内")
        subregions = self._subregions(parent["region_id"])
        if not subregions:
            subregions = [
                {
                    "region_id": region_key, "name": value.get("name") or region_key,
                    "level": "审批快照", "parent_id": approval["parent_id"],
                    "parent_name": approval["parent_name"], "boundary_code": "",
                    "status": approval["status"], "bounds": value["bounds"],
                    "has_bounds": True,
                }
                for region_key, value in ((approval.get("region_bounds") or {}).get("subregions") or {}).items()
            ]
        target = None
        if region_id:
            target = next((item for item in subregions if item["region_id"] == str(region_id)), None)
            if target is None:
                errors.append("指定子区不存在")
            elif not _in_bounds((lat, lon), target["bounds"]):
                errors.append(f"不落在子区 {target['name']} 边界内")
        else:
            target = next((item for item in subregions if _in_bounds((lat, lon), item["bounds"])), None)

        key = self._manual_key(point, batch_key)
        self._refresh_late_marks(approval)
        late_keys = {item["point_key"] for item in approval["late_points"]}
        current_keys = {point_key(item) for item in self._load_verified_points(approval["batch_key"])}
        if key in late_keys or key in current_keys:
            errors.append("重复定位点")

        if errors:
            return {"ok": False, "errors": errors, "point_key": key}

        # 通过核验后追加到已核验输入；审批快照保持不变。后续审批读到同样的
        # 定位点会带上 position_verified 标记，无需重复核验。
        record = {
            "point_uid": key,
            "point_id": point.get("point_id"),
            "batch_key": approval["batch_key"],
            "orig_lat": lat,
            "orig_lon": lon,
            "trans_lat": lat,
            "trans_lon": lon,
            "timestamp": point.get("timestamp"),
            "terminal": point.get("terminal"),
            "late": True,
            "position_verified": True,
            "verified_by": approval_id,
        }
        transformed_file = self._data_dir("coord_transformed") / f"{approval['batch_key']}_transformed.jsonl"
        self._write_jsonl(transformed_file, [record])

        target_info = {
            "point_key": key,
            "lat": lat,
            "lon": lon,
            "timestamp": point.get("timestamp"),
            "arrived_at": time.time(),
            "verification": "已通过",
            "region_id": target["region_id"] if target else "",
            "region_name": target["name"] if target else "",
            "note": "独立位置核验通过，已进入已核验输入",
        }
        existing = next((item for item in approval["late_points"]
                         if item["point_key"] == key and item["verification"] != "已通过"), None)
        if existing:
            existing.update(target_info)
        else:
            approval["late_points"].append(target_info)
        approval["history"].append({
            "at": time.time(), "event": "迟到点核验",
            "detail": f"{key} 通过位置核验，归属 {target['name'] if target else '未知'}",
        })
        self._save_approval(approval)
        return {
            "ok": True,
            "point_key": key,
            "region_id": target["region_id"] if target else "",
            "region_name": target["name"] if target else "",
            "approval_id": approval_id,
            "approval_status": approval["status"],
            "note": "核验结果独立生效，不改写已打开审批的输入快照",
        }

    def add_late_point(self, approval_id: str, point: dict[str, Any]) -> dict[str, Any]:
        """把迟到点写入待核验队列；核验必须另走 ``verify_late_point``。"""
        approval = self.get_approval(approval_id)
        if approval is None:
            raise LookupError(f"审批 {approval_id} 不存在")
        self._refresh_late_marks(approval)
        self._save_approval(approval)
        return {"approval_id": approval_id, "late_points": approval["late_points"]}

    def confirm_approval(self, approval_id: str, decision: str = "",
                         operator: str = "值班员", note: str = "") -> dict[str, Any]:
        """确认一份对照审批。

        ``decision`` 为空时按旧观测直接完成历史对账；选择 ``更正补采`` 时，
        只采用已通过位置核验的迟到点，并重新安排补采任务，审批自身的计算
        结果仍保持打开时保存的原样。
        """
        approval = self.get_approval(approval_id)
        if approval is None:
            raise LookupError(f"审批 {approval_id} 不存在")
        if approval["status"] == "已确认":
            raise ValueError(f"审批 {approval_id} 已确认，不能重复确认")
        self._refresh_late_marks(approval)
        verified = [item for item in approval["late_points"] if item["verification"] == "已通过"]
        pending = [item for item in approval["late_points"] if item["verification"] != "已通过"]

        if decision not in ("", "历史对账", "更正补采"):
            raise ValueError("确认处理方式只能是「历史对账」或「更正补采」")
        if decision == "更正补采" and pending:
            raise ValueError("存在尚未通过位置核验的迟到点，不能采用更正补采")
        if decision == "更正补采" and not verified:
            raise ValueError("没有已通过核验的更正点，无需重新安排补采")

        approval["status"] = "已确认"
        approval["confirmed_at"] = time.time()
        approval["confirmed_by"] = operator
        approval["decision"] = decision or "历史对账"
        approval["result"]["late_point_handling"] = {
            "decision": approval["decision"],
            "verified_points": [item["point_key"] for item in verified],
            "pending_points": [item["point_key"] for item in pending],
            "note": "审批结果仍按打开时的快照保存，未混入迟到点",
        }

        if approval["decision"] == "更正补采" and verified:
            approval["backfill_plan"] = self._schedule_backfill(approval, verified)

        approval["history"].append({
            "at": time.time(), "event": f"确认审批（{approval['decision']}）",
            "detail": note or "结果按打开时快照固化，已通过位置核验的更正点单独安排补采",
        })
        self._save_approval(approval)
        return approval

    def _schedule_backfill(self, approval: dict[str, Any],
                           verified: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """为已核验的更正点重新安排补采任务，只登记计划，不伪造定位点。"""
        backfill = self._safe_import("app.services.geo_backfill", "GeoBackfillService")()
        plan = []
        for item in verified:
            region_id = item.get("region_id") or approval["parent_id"]
            region = self._region(region_id)
            region_name = region["name"] if region else approval["parent_name"]
            entry, _ = backfill.create_entry({
                "回补编号": f"BF-{approval['approval_id']}-{item['point_key']}",
                "目标数据集": approval["batch_key"],
                "起始日期": time.strftime("%Y-%m-%d", time.localtime(item.get("arrived_at", time.time()))),
                "截止日期": time.strftime("%Y-%m-%d", time.localtime(item.get("arrived_at", time.time()))),
                "回补范围": f"{region_name}/{item['point_key']}",
                "并行度": "1",
                "覆盖方式": "corrected_reschedule",
                "回补水位": "待补采",
            })
            if entry:
                backfill.run_action(entry["id"], "启动回补")
            plan.append({
                "backfill_id": entry.get("回补编号") if entry else "",
                "point_key": item["point_key"],
                "region_id": region_id,
                "region_name": region_name,
                "status": entry.get("status") if entry else "",
                "reason": "已通过位置核验的迟到更正点",
            })
        return plan

    # ------------------------------------------------------------------
    # 历史复算
    # ------------------------------------------------------------------

    def replay_approval(self, approval_id: str, use_current_inputs: bool = False) -> dict[str, Any]:
        """按审批保存的网格集合复算旧结果。

        默认用审批打开时记录的定位点快照复算：之后追加的迟到点不会进入本次
        复算，第一份审批在迟到点到达后仍能得到与确认时一致的结果。
        """
        approval = self.get_approval(approval_id)
        if approval is None:
            raise LookupError(f"审批 {approval_id} 不存在")
        if use_current_inputs:
            points = self._load_verified_points(approval["batch_key"])
            source = "current_verified_inputs"
        else:
            points = [
                {
                    "point_uid": item["point_key"],
                    "trans_lat": item["lat"],
                    "trans_lon": item["lon"],
                    "timestamp": item.get("timestamp"),
                }
                for item in approval["input_snapshot"]["points"]
            ]
            source = "approval_snapshot"
        stored_bounds = approval.get("region_bounds") or {}
        if stored_bounds.get("parent"):
            parent = {
                "region_id": approval["parent_id"],
                "name": approval["parent_name"],
                "level": "审批快照",
                "parent_id": "",
                "parent_name": "",
                "boundary_code": "",
                "status": approval["status"],
                "bounds": stored_bounds["parent"],
                "has_bounds": True,
            }
            subregions = [
                {
                    "region_id": region_id,
                    "name": value.get("name") or region_id,
                    "level": "审批快照",
                    "parent_id": approval["parent_id"],
                    "parent_name": approval["parent_name"],
                    "boundary_code": "",
                    "status": approval["status"],
                    "bounds": value["bounds"],
                    "has_bounds": True,
                }
                for region_id, value in (stored_bounds.get("subregions") or {}).items()
            ]
        else:
            # 旧版本审批没存边界，只能回到维度表；子区缺失时用快照里的名称补齐，
            # 宁可报错也不静默少算一个子区。
            parent = self._region(approval["parent_id"])
            stored_regions = (approval.get("region_bounds") or {}).get("subregions") or {}
            subregions = []
            for item in approval["result"]["subregions"]:
                region = self._region(item["region_id"])
                if region is None:
                    stored = stored_regions.get(item["region_id"]) or {}
                    region = {
                        "region_id": item["region_id"],
                        "name": item["region_name"],
                        "level": "审批快照",
                        "parent_id": approval["parent_id"],
                        "parent_name": approval["parent_name"],
                        "boundary_code": "",
                        "status": approval["status"],
                        "bounds": stored.get("bounds") or {"south": 0.0, "west": 0.0, "north": 0.0, "east": 0.0},
                        "has_bounds": bool(stored.get("bounds")),
                    }
                subregions.append(region)
        if parent is None:
            raise LookupError(f"上级区域 {approval['parent_id']} 不存在，且审批未保存边界")
        if any(not item.get("has_bounds") for item in subregions):
            raise LookupError("子区边界缺失，无法按原点复算旧审批")
        replay = estimate(
            batch_key=approval["batch_key"],
            points=points,
            region=parent,
            all_regions=subregions,
            bandwidth=float(approval["bandwidth"]),
            kernel=approval["kernel"],
            precision=int(approval["precision"]),
            grid_ids=approval["grid_ids"],
        )
        replayed = {
            str(item["region_id"]): item
            for item in replay["subregions"]
        }
        differences = []
        for stored in approval["result"]["subregions"]:
            current = replayed.get(str(stored["region_id"]))
            if current is None:
                differences.append({"region_id": stored["region_id"], "diff": "复算缺少该子区"})
                continue
            if current["sample_count"] != stored["sample_count"]:
                differences.append({
                    "region_id": stored["region_id"], "field": "sample_count",
                    "stored": stored["sample_count"], "replayed": current["sample_count"],
                })
            if abs(current["kernel_sum"] - float(stored["kernel_sum"])) > 1e-6:
                differences.append({
                    "region_id": stored["region_id"], "field": "kernel_sum",
                    "stored": stored["kernel_sum"], "replayed": current["kernel_sum"],
                })
        return {
            "approval_id": approval_id,
            "approval_status": approval["status"],
            "decision": approval.get("decision", ""),
            "source": source,
            "grid_ids": approval["grid_ids"],
            "bandwidth": approval["bandwidth"],
            "kernel": approval["kernel"],
            "sample_count": len(points),
            "matches": not differences,
            "differences": differences,
            "replayed": replay["subregions"],
            "replayed_totals": replay["totals"],
            "stored": approval["result"]["subregions"],
            "stored_totals": approval["result"]["totals"],
            "note": "复算沿用审批保存的网格集合与输入快照，迟到点不参与",
        }

    def reconcile_approvals(self, approval_id: str, against_id: str) -> dict[str, Any]:
        """两份交叠审批的历史对账：核对此前合计能否在本次审批中被复算。

        ``against_id`` 是较早确认的旧审批。返回两个口径的保存合计与复算合计，
        并显式列出迟到点带来的差异；旧审批本身按快照复算，不因迟到点而改变。
        """
        current = self.get_approval(approval_id)
        older = self.get_approval(against_id)
        if current is None:
            raise LookupError(f"审批 {approval_id} 不存在")
        if older is None:
            raise LookupError(f"审批 {against_id} 不存在")
        if older.get("opened_at", 0) > current.get("opened_at", 0):
            current, older = older, current

        older_replay = self.replay_approval(older["approval_id"])
        current_replay = self.replay_approval(current["approval_id"])
        older_stored = {
            "approval_id": older["approval_id"],
            "status": older["status"],
            "sample_count": older["result"]["sample_count"],
            "subregions": [
                {
                    "region_id": item["region_id"],
                    "region_name": item["region_name"],
                    "sample_count": item["sample_count"],
                    "kernel_sum": item["kernel_sum"],
                }
                for item in older["result"]["subregions"]
            ],
            "totals": older["result"]["totals"],
        }
        current_stored = {
            "approval_id": current["approval_id"],
            "status": current["status"],
            "sample_count": current["result"]["sample_count"],
            "subregions": [
                {
                    "region_id": item["region_id"],
                    "region_name": item["region_name"],
                    "sample_count": item["sample_count"],
                    "kernel_sum": item["kernel_sum"],
                }
                for item in current["result"]["subregions"]
            ],
            "totals": current["result"]["totals"],
        }
        older_keys = set(older["input_snapshot"]["sample_point_keys"])
        current_keys = set(current["input_snapshot"]["sample_point_keys"])
        added = sorted(current_keys - older_keys)
        removed = sorted(older_keys - current_keys)
        return {
            "batch_key": older["batch_key"],
            "older": older_stored,
            "current": current_stored,
            "older_replay": {
                "matches": older_replay["matches"],
                "source": older_replay["source"],
                "sample_count": older_replay["sample_count"],
            },
            "current_replay": {
                "matches": current_replay["matches"],
                "source": current_replay["source"],
                "sample_count": current_replay["sample_count"],
            },
            "history_reproducible": older_replay["matches"],
            "input_changes": {
                "added_points": added,
                "removed_points": removed,
                "same_sample_set": not added and not removed,
            },
            "note": (
                "旧审批按打开时的输入快照复算，迟到点不影响历史合计；"
                "新增点只在本次审批口径里体现，必要时单独安排补采"
            ),
        }

    # ------------------------------------------------------------------
    # 兼容旧链路的区域汇总
    # ------------------------------------------------------------------

    def aggregate_by_region(self, batch_key: str, region_id: int = 1) -> dict[str, Any]:
        """兼容旧链路：读取网格索引数据集，按区域归属汇总生成区域聚合数据集。

        这条路径沿用「全部网格参与」的口径，新的区域密度视图请走
        ``region_density_view``，两者互不影响。
        """
        region = store.find(self.MODULE, region_id)
        if region is None:
            region = {"区域编号": "REGION-001", "区域名称": "东城区", "区域级别": "区级",
                      "边界编码": "110101", "上级区域": "北京市"}

        try:
            grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
            grids, _ = grid_svc.get_grid_index(batch_key)
        except Exception:
            grids_file = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
            if not grids_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的网格索引数据不存在")
            grids = self._read_jsonl(grids_file)

        if not grids:
            return {"batch_key": batch_key, "total_points_aggregated": 0}

        total_points = sum(g["point_count"] for g in grids)
        region_center_lat = sum(g["center_lat"] * g["point_count"] for g in grids) / max(total_points, 1)
        region_center_lon = sum(g["center_lon"] * g["point_count"] for g in grids) / max(total_points, 1)

        summary = {
            "batch_key": batch_key,
            "region_id": region_id,
            "region_name": region.get("区域名称", "未知"),
            "region_level": region.get("区域级别", "未知"),
            "boundary_code": region.get("边界编码", ""),
            "grid_count": len(grids),
            "total_points": total_points,
            "region_center_lat": round(region_center_lat, 6),
            "region_center_lon": round(region_center_lon, 6),
            "generated_at": time.time(),
        }

        output_file = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        written = self._write_jsonl(output_file, [summary])

        if region.get("id"):
            region["网格数量"] = len(grids)

        return {
            "batch_key": batch_key,
            "region_name": summary["region_name"],
            "grids_aggregated": len(grids),
            "total_points": total_points,
            "output_file": str(output_file),
        }

    def get_region_aggregates(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取批次的区域聚合数据集文件。"""
        file_path = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        data = self._read_jsonl(file_path)
        return data, str(file_path)


def _in_bounds(coords: tuple[float, float], bounds: dict[str, float]) -> bool:
    lat, lon = coords
    return (bounds["south"] <= lat < bounds["north"]
            and bounds["west"] <= lon < bounds["east"])
