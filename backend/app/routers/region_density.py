"""区域维度扩展接口：层级、密度看板、带宽对照、对照审批与历史复算。

统一资源路由只承载通用 CRUD；区域维度这组接口是业务动作，放在独立路由里，
路径固定写在 ``/api/region_dim`` 下，不会与 ``/api/{module}/{entry_id}`` 冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.geo_density import CLASS_KERNEL_KEY, CLASS_OBSERVED_KEY, CLASS_ZERO_KEY
from app.services.geo_density_demo import DEMO_BATCH, DEMO_LATE_POINT, demo_raw_points, demo_verified_points
from app.services.region_dim import RegionDimService

router = APIRouter(prefix="/api/region_dim", tags=["区域维度"])
region_service = RegionDimService()


class LatePointPayload(BaseModel):
    point: dict[str, Any]
    region_id: str | None = None


class ApprovalCreatePayload(BaseModel):
    batch_key: str
    parent_id: str = "REGION-BJ"
    bandwidth: float = 500.0
    kernel: str = "gaussian"
    precision: int = 6
    label: str = ""


class ConfirmPayload(BaseModel):
    decision: str = ""
    operator: str = "值班员"
    note: str = ""


class DemoBatchPayload(BaseModel):
    batch_key: str = DEMO_BATCH
    include_late: bool = False


def _handle(error: Exception) -> HTTPException:
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail=f"区域密度计算失败：{error}")


@router.get("/hierarchy")
def hierarchy(parent_id: str = Query(default="REGION-BJ")) -> dict[str, Any]:
    """区域层级：父区域与直属子区的边界。"""
    return region_service.get_hierarchy(parent_id)


@router.get("/density-view")
def density_view(
    batch_key: str,
    parent_id: str = Query(default="REGION-BJ"),
    bandwidth: float = Query(default=500.0, gt=0),
    kernel: str = Query(default="gaussian"),
    precision: int = Query(default=6, ge=1, le=12),
) -> dict[str, Any]:
    """按上级区域汇总密度贡献：每块子区给出采用点、网格、权重和样本量。"""
    try:
        view = region_service.region_density_view(
            batch_key, parent_id, bandwidth, kernel, precision, with_cells=False)
    except Exception as error:  # noqa: BLE001 - 统一转成可读的接口错误
        raise _handle(error) from error
    view.pop("cells", None)
    return view


@router.get("/density-board")
def density_board(
    batch_key: str,
    parent_id: str = Query(default="REGION-BJ"),
    bandwidth: float = Query(default=500.0, gt=0),
    kernel: str = Query(default="gaussian"),
    precision: int = Query(default=6, ge=1, le=12),
) -> dict[str, Any]:
    """密度看板：有落点、仅有核贡献、确为零三类格分列展示。"""
    try:
        return region_service.density_board(batch_key, parent_id, bandwidth, kernel, precision)
    except Exception as error:  # noqa: BLE001
        raise _handle(error) from error


@router.get("/bandwidth-compare")
def bandwidth_compare(
    batch_key: str,
    bandwidths: str = Query(default="500,1500", description="逗号分隔的带宽，例如 500,1500"),
    parent_id: str = Query(default="REGION-BJ"),
    kernel: str = Query(default="gaussian"),
    precision: int = Query(default=6, ge=1, le=12),
) -> dict[str, Any]:
    """两种带宽对照：同一批定位点，只让扩散贡献变化。"""
    try:
        values = [float(item) for item in bandwidths.split(",") if item.strip()]
    except ValueError as error:
        raise HTTPException(status_code=400, detail="带宽参数必须是逗号分隔的数字") from error
    if len(values) != 2 or any(value <= 0 for value in values):
        raise HTTPException(status_code=400, detail="带宽对照需要两个正数带宽")
    try:
        return region_service.compare_bandwidths(
            batch_key, values, parent_id, kernel, precision)
    except Exception as error:  # noqa: BLE001
        raise _handle(error) from error


@router.get("/approvals")
def list_approvals(batch_key: str | None = Query(default=None)) -> dict[str, Any]:
    """已持久化的对照审批列表。"""
    approvals = region_service.list_approvals(batch_key)
    return {
        "items": [
            {
                "approval_id": item["approval_id"],
                "label": item.get("label", ""),
                "batch_key": item["batch_key"],
                "parent_name": item.get("parent_name", ""),
                "bandwidth": item["bandwidth"],
                "kernel": item["kernel"],
                "grid_level": item.get("grid_level", ""),
                "status": item["status"],
                "decision": item.get("decision", ""),
                "opened_at": item.get("opened_at"),
                "confirmed_at": item.get("confirmed_at"),
                "grid_count": len(item.get("grid_ids", [])),
                "late_points": len(item.get("late_points", [])),
            }
            for item in approvals
        ],
        "total": len(approvals),
    }


@router.post("/approvals")
def create_approval(payload: ApprovalCreatePayload) -> dict[str, Any]:
    """打开一份对照审批，冻结输入并持久化网格与结果。"""
    try:
        approval = region_service.create_comparison_approval(
            payload.batch_key, payload.parent_id, payload.bandwidth,
            payload.kernel, payload.precision, payload.label)
    except Exception as error:  # noqa: BLE001
        raise _handle(error) from error
    return _approval_detail(approval)


@router.get("/approvals/{approval_id}")
def get_approval(approval_id: str) -> dict[str, Any]:
    # 读取详情时同步登记打开审批之后新增的已核验点，前端能直接看到待核验/已通过状态。
    approval = region_service.get_approval(approval_id, refresh_late=True)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"审批 {approval_id} 不存在")
    return _approval_detail(approval)


@router.post("/approvals/{approval_id}/late-points")
def add_late_point(approval_id: str, payload: LatePointPayload) -> dict[str, Any]:
    """登记一个迟到点并独立核验位置；核验通过才进入已核验输入。"""
    try:
        result = region_service.verify_late_point(approval_id, payload.point, payload.region_id)
    except Exception as error:  # noqa: BLE001
        raise _handle(error) from error
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail={
            "message": "迟到点未通过位置核验",
            "errors": result.get("errors", []),
            "point_key": result.get("point_key"),
        })
    return result


@router.post("/approvals/{approval_id}/confirm")
def confirm_approval(approval_id: str, payload: ConfirmPayload) -> dict[str, Any]:
    """确认审批：历史对账或采用已核验更正重新安排补采。"""
    try:
        approval = region_service.confirm_approval(
            approval_id, payload.decision, payload.operator, payload.note)
    except Exception as error:  # noqa: BLE001
        raise _handle(error) from error
    return _approval_detail(approval)


@router.get("/approvals/{approval_id}/replay")
def replay_approval(
    approval_id: str,
    use_current_inputs: bool = Query(default=False),
) -> dict[str, Any]:
    """按审批保存的网格与输入快照复算旧结果。"""
    try:
        return region_service.replay_approval(approval_id, use_current_inputs)
    except Exception as error:  # noqa: BLE001
        raise _handle(error) from error


@router.get("/approvals/{approval_id}/reconcile")
def reconcile_approvals(
    approval_id: str,
    against_id: str = Query(..., description="较早确认的旧审批编号"),
) -> dict[str, Any]:
    """两份交叠审批的历史对账：旧合计能否按原点复算、本次口径新增了哪些点。"""
    try:
        return region_service.reconcile_approvals(approval_id, against_id)
    except Exception as error:  # noqa: BLE001
        raise _handle(error) from error


@router.get("/cell-classes")
def cell_classes() -> dict[str, Any]:
    """三类格的字典，前端图例直接使用。"""
    return {
        "classes": [
            {"key": CLASS_OBSERVED_KEY, "label": "有落点", "description": "网格内有直接观测落点"},
            {"key": CLASS_KERNEL_KEY, "label": "仅有核贡献", "description": "无落点，但带宽内有来源点扩散"},
            {"key": CLASS_ZERO_KEY, "label": "确为零", "description": "所有来源点都在带宽外，属于待补采缺口"},
        ]
    }


@router.post("/demo-batch")
def create_demo_batch(payload: DemoBatchPayload) -> dict[str, Any]:
    """生成一份横跨两个子区的演示批次，写入真实数据目录。"""
    service = region_service
    batch_key = payload.batch_key
    raw_file = service._data_dir("geo_points") / f"{batch_key}.jsonl"
    if raw_file.exists():
        return {
            "ok": True,
            "batch_key": batch_key,
            "points": len(demo_verified_points(batch_key)),
            "include_late": False,
            "message": "演示批次已存在，直接读取",
        }
    service._write_jsonl(raw_file, demo_raw_points(batch_key))
    transformed_file = service._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
    service._write_jsonl(transformed_file, demo_verified_points(batch_key))
    grid_file = service._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
    service._write_jsonl(grid_file, _demo_grid_rows(batch_key))

    late_written = False
    if payload.include_late:
        late_record = {
            "point_uid": f"{batch_key}:{DEMO_LATE_POINT['point_id']}",
            "point_id": DEMO_LATE_POINT["point_id"],
            "batch_key": batch_key,
            "orig_lat": DEMO_LATE_POINT["lat"],
            "orig_lon": DEMO_LATE_POINT["lon"],
            "trans_lat": DEMO_LATE_POINT["lat"],
            "trans_lon": DEMO_LATE_POINT["lon"],
            "timestamp": DEMO_LATE_POINT["timestamp"],
            "terminal": DEMO_LATE_POINT["terminal"],
            "late": True,
        }
        service._write_jsonl(transformed_file, [late_record])
        late_written = True

    return {
        "ok": True,
        "batch_key": batch_key,
        "points": len(demo_verified_points(batch_key)),
        "include_late": late_written,
        "message": "演示批次已写入，可直接打开密度看板",
    }


@router.post("/demo-late-point")
def add_demo_late_point(approval_id: str) -> dict[str, Any]:
    """把演示迟到点送进指定审批做独立位置核验。"""
    point = {
        "point_id": DEMO_LATE_POINT["point_id"],
        "batch_key": DEMO_BATCH,
        "lat": DEMO_LATE_POINT["lat"],
        "lon": DEMO_LATE_POINT["lon"],
        "timestamp": DEMO_LATE_POINT["timestamp"],
        "terminal": DEMO_LATE_POINT["terminal"],
    }
    try:
        result = region_service.verify_late_point(approval_id, point)
    except Exception as error:  # noqa: BLE001
        raise _handle(error) from error
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail={
            "message": "迟到点未通过位置核验",
            "errors": result.get("errors", []),
            "point_key": result.get("point_key"),
        })
    return result


def _demo_grid_rows(batch_key: str) -> list[dict[str, Any]]:
    """演示批次的网格索引行，与 GridIndexService 生成的结构保持一致。"""
    from app.services.geo_density import cell_grid, geohash_encode

    counts: dict[str, int] = {}
    for point in demo_verified_points(batch_key):
        grid = geohash_encode(point["trans_lat"], point["trans_lon"], 6)
        counts[grid] = counts.get(grid, 0) + 1
    rows = []
    for index, (grid, count) in enumerate(sorted(counts.items()), start=1):
        meta = cell_grid(grid)
        rows.append({
            "grid_id": index,
            "geohash": grid,
            "batch_key": batch_key,
            "level": meta["level"],
            "precision": meta["precision"],
            "center_lat": meta["center_lat"],
            "center_lon": meta["center_lon"],
            "point_count": count,
            "generated_at": 1792900000.0,
        })
    return rows


def _approval_detail(approval: dict[str, Any]) -> dict[str, Any]:
    """审批详情对外只暴露可读的摘要和结果，不重复回传整份点快照。"""
    detail = dict(approval)
    snapshot = detail.pop("input_snapshot", {})
    detail["input_summary"] = {
        "sample_count": snapshot.get("sample_count"),
        "sample_fingerprint": snapshot.get("sample_fingerprint"),
        "source": snapshot.get("source"),
        "verified": snapshot.get("verified"),
    }
    detail["result"] = {
        **approval.get("result", {}),
        "cells": approval.get("result", {}).get("cells", []),
    }
    return detail
