"""区域密度与对照审批测试：跨区网格、空格分类、带宽对照、迟到点与旧审批复算。"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def data_root(monkeypatch):
    """每个用例独立数据目录，并准备两个子区的示例批次。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_region_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir

    from app.services.geo_density_demo import demo_verified_points
    from app.services.region_dim import RegionDimService

    service = RegionDimService()
    service._write_jsonl(
        service._data_dir("coord_transformed") / "BATCH-REGION_transformed.jsonl",
        demo_verified_points("BATCH-REGION"),
    )
    yield service, tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_region_view_reconciles_same_points(data_root):
    service, _ = data_root
    view = service.region_density_view("BATCH-REGION")
    assert view["sample_count"] == 15
    assert view["reconciliation"]["point_sets_match"] is True
    assert view["reconciliation"]["parent_sample_count"] == 15
    assert view["reconciliation"]["subregion_sample_total"] == 15
    assert view["reconciliation"]["unassigned_points"] == []
    subregions = {item["region_id"]: item for item in view["subregions"]}
    assert sum(item["sample_count"] for item in subregions.values()) == 15
    assert all(item["bandwidth"] == 500 for item in subregions.values())
    assert all(item["grid_level"] == "L6" for item in subregions.values())
    # 每块子区都能列出自己采用的定位点
    assert set().union(*(set(item["sample_points"]) for item in subregions.values())) == {
        key for item in view["subregions"] for key in item["sample_points"]}


def test_cross_boundary_grid_assigned_once(data_root):
    service, _ = data_root
    view = service.region_density_view("BATCH-REGION")
    boundary = {item["grid_geohash"] for item in view["reconciliation"]["cross_boundary_grids"]}
    assert "wx4g0b" in boundary
    owners = [
        cell["owner_region_id"] for cell in view["cells"]
        if cell["grid_geohash"] == "wx4g0b"
    ]
    assert len(owners) == 1
    # 同一批定位点，不会因为网格横跨两区而重复计入区域合计
    assert view["totals"]["subregion_sample_count"] == view["totals"]["sample_count"]


def test_empty_cells_split_between_kernel_and_zero(data_root):
    service, _ = data_root
    narrow = service.density_board("BATCH-REGION", bandwidth=500)
    wide = service.density_board("BATCH-REGION", bandwidth=1500)
    assert narrow["totals"]["kernel_cells"] > 0
    assert narrow["totals"]["zero_cells"] > 0
    # 带宽扩大后，原本带宽外的空格收到扩散贡献
    assert wide["totals"]["zero_cells"] < narrow["totals"]["zero_cells"]
    assert wide["totals"]["kernel_cells"] > narrow["totals"]["kernel_cells"]
    zero = narrow["cells"]["zero"]
    assert all(not item["sources"] for item in zero)
    assert all("超出" in item["reason"] for item in zero)
    kernel = narrow["cells"]["kernel"]
    assert all(item["sources"] and item["sample_count"] == 0 for item in kernel)
    observed = narrow["cells"]["observed"]
    assert all(item["sample_count"] > 0 for item in observed)


def test_bandwidth_compare_keeps_same_points(data_root):
    service, _ = data_root
    compare = service.compare_bandwidths("BATCH-REGION", [500, 1500])
    assert compare["same_sample_set"] is True
    first, second = compare["rounds"]
    assert first["sample_count"] == second["sample_count"] == 15
    assert first["sample_fingerprint"] == second["sample_fingerprint"]
    assert first["observed_cells"] == second["observed_cells"]
    assert first["kernel_cells"] != second["kernel_cells"]
    for round_ in compare["rounds"]:
        assert round_["reconciliation"]["point_sets_match"] is True


def test_approval_persists_grids_and_replays(data_root):
    service, _ = data_root
    approval = service.create_comparison_approval("BATCH-REGION", label="第一份区域合计")
    assert approval["status"] == "待复核"
    assert approval["grid_ids"]
    stored = {item["region_id"]: item["kernel_sum"] for item in approval["result"]["subregions"]}

    replay = service.replay_approval(approval["approval_id"])
    assert replay["matches"] is True
    assert replay["source"] == "approval_snapshot"
    assert replay["grid_ids"] == approval["grid_ids"]
    replayed = {item["region_id"]: item["kernel_sum"] for item in replay["replayed"]}
    assert replayed == stored

    # 已持久化到文件，重新实例化服务后仍能读取
    from app.services.region_dim import RegionDimService
    fresh = RegionDimService()
    loaded = fresh.get_approval(approval["approval_id"])
    assert loaded is not None
    assert loaded["grid_ids"] == approval["grid_ids"]


def test_late_point_does_not_change_open_approval(data_root):
    service, _ = data_root
    # 值班员已确认第一份区域合计
    first = service.create_comparison_approval("BATCH-REGION", label="第一份区域合计")
    service.confirm_approval(first["approval_id"], decision="历史对账")
    before = service.replay_approval(first["approval_id"])
    assert before["matches"] is True

    # 第二份审批已经打开、尚未确认，输入里还没有邻区迟到点
    second = service.create_comparison_approval(
        "BATCH-REGION", bandwidth=1500, label="第二份区域合计")
    assert second["status"] == "待复核"
    assert second["input_snapshot"]["sample_count"] == 15

    # 邻区迟到点在第二份审批打开后独立通过位置核验
    late = {"point_id": 9001, "lat": 39.9095, "lon": 116.4135,
            "timestamp": 1793000000.0, "terminal": "TERM-LATE"}
    result = service.verify_late_point(second["approval_id"], late)
    assert result["ok"] is True
    assert result["region_id"] == "REGION-B"
    assert result["approval_status"] == "待复核"

    # 第二份审批仍未被迟到点改写，只能通过确认时的选择采用
    refreshed = service.get_approval(second["approval_id"])
    assert refreshed["input_snapshot"]["sample_count"] == 15
    service._refresh_late_marks(refreshed)
    assert refreshed["late_points"][0]["verification"] == "已通过"
    assert refreshed["late_points"][0]["point_key"] == "BATCH-REGION:9001"

    # 第一份审批仍可按原点复算，迟到点不参与
    after = service.replay_approval(first["approval_id"])
    assert after["matches"] is True
    assert after["sample_count"] == before["sample_count"] == 15
    # 按当前输入复算则会看到新点，差异必须显式列出
    current = service.replay_approval(first["approval_id"], use_current_inputs=True)
    assert current["sample_count"] == 16
    assert current["matches"] is False


def test_only_verified_inputs_accepted(data_root):
    service, tmpdir = data_root
    raw = service._data_dir("geo_points") / "RAW-ONLY.jsonl"
    service._write_jsonl(raw, [{"point_id": 1, "lat": 39.9, "lon": 116.4, "timestamp": 1}])
    with pytest.raises(FileNotFoundError):
        service.region_density_view("RAW-ONLY")

    approval = service.create_comparison_approval("BATCH-REGION")
    outside = {"point_id": 1, "lat": 31.0, "lon": 121.0, "timestamp": 1}
    result = service.verify_late_point(approval["approval_id"], outside)
    assert result["ok"] is False
    assert any("不在上级区域范围内" in item for item in result["errors"])

    wrong_region = {"point_id": 2, "lat": 39.9095, "lon": 116.4135, "timestamp": 2}
    result = service.verify_late_point(approval["approval_id"], wrong_region, region_id="REGION-A")
    assert result["ok"] is False
    assert any("不落在子区" in item for item in result["errors"])


def test_confirm_decisions_history_and_correction(data_root):
    service, _ = data_root
    first = service.create_comparison_approval("BATCH-REGION", label="第一份区域合计")
    service.verify_late_point(first["approval_id"], {
        "point_id": 9100, "lat": 39.9095, "lon": 116.4135,
        "timestamp": 1793000100.0, "terminal": "TERM-LATE",
    })
    confirmed = service.confirm_approval(first["approval_id"], decision="更正补采")
    assert confirmed["status"] == "已确认"
    assert confirmed["decision"] == "更正补采"
    assert confirmed["backfill_plan"]
    assert confirmed["backfill_plan"][0]["region_id"] == "REGION-B"
    # 审批结果保持打开时快照，不掺入迟到点
    assert confirmed["result"]["sample_count"] == 15
    assert confirmed["result"]["late_point_handling"]["verified_points"] == ["BATCH-REGION:9100"]
    replay = service.replay_approval(first["approval_id"])
    assert replay["matches"] is True

    with pytest.raises(ValueError):
        service.confirm_approval(first["approval_id"], decision="历史对账")

    second = service.create_comparison_approval("BATCH-REGION", label="历史对账")
    history = service.confirm_approval(second["approval_id"], decision="历史对账")
    assert history["decision"] == "历史对账"
    assert history["backfill_plan"] == []
    assert history["result"]["late_point_handling"]["decision"] == "历史对账"


def test_correction_blocked_by_unverified_late_point(data_root):
    service, _ = data_root
    approval = service.create_comparison_approval("BATCH-REGION")
    # 绕过核验直接把迟到点写进已核验输入
    service._write_jsonl(
        service._data_dir("coord_transformed") / "BATCH-REGION_transformed.jsonl",
        [{"point_uid": "BATCH-REGION:9200", "point_id": 9200, "batch_key": "BATCH-REGION",
          "trans_lat": 39.9095, "trans_lon": 116.4135, "timestamp": 1793000200.0}],
    )
    with pytest.raises(ValueError, match="尚未通过位置核验"):
        service.confirm_approval(approval["approval_id"], decision="更正补采")

    # 历史对账仍可完成，只采用已核验的完整输入
    confirmed = service.confirm_approval(approval["approval_id"], decision="历史对账")
    assert confirmed["status"] == "已确认"
    assert confirmed["result"]["late_point_handling"]["pending_points"] == ["BATCH-REGION:9200"]


def test_density_estimate_records_cell_classes(data_root):
    service, _ = data_root
    from app.services.density_estimate import DensityEstimateService

    density = DensityEstimateService()
    # 网格索引交给真实服务生成，保证和区域汇总同口径
    from app.services.grid_index import GridIndexService
    grid = GridIndexService()
    grid.index_points_from_batch("BATCH-REGION")
    result = density.estimate_density("BATCH-REGION", bandwidth=500)
    assert result["observed_cells"] > 0
    assert result["kernel_cells"] > 0
    assert result["zero_cells"] > 0
    rows, _ = density.get_density_estimates("BATCH-REGION", bandwidth=500)
    classes = {row["cell_class"] for row in rows}
    assert classes == {"observed", "kernel", "zero"}
    for row in rows:
        assert "class_reason" in row
        assert row["kernel_sum"] >= 0
        if row["cell_class"] == "zero":
            assert row["kernel_sum"] == 0
