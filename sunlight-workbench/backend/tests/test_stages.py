"""建筑高度阶段（height stages）逻辑测试。

不依赖 PostgreSQL：阶段表为 ORM 行或 dict 均可。验证：
- 早于首个阶段 → 原高度；生效日当天起采用新阶段；之后取最新阶段
- 重复日期 / 非正数 / NaN / 低于 base_height 被拒绝
- 生效日前后对同一窗点跑出不同遮挡（物理验证）
- 快照固定运行时体量，之后修改阶段旧快照仍可追查原体量
"""
from types import SimpleNamespace as NS
from datetime import date

import pytest
from geoalchemy2.shape import from_shape
from pydantic import ValidationError as PydanticValidationError
from shapely.geometry import Point, Polygon

from app.stages import (StageValidationError, resolve_height, validate_stages)
from app.main import HeightStageIn, _bundle_payload
from app.analysis import MeasurePointGeom, analyze_point
from app.geometry import BuildingGeom, box_footprint, build_scene


ORIGINAL = 45.0


def _stage_rows(pairs, note=""):
    return [NS(effective_date=d, top_height=h, note=note)
            for d, h in pairs]


# ---------- 解析：早于首阶段采用原高度，之后取最新生效阶段 ----------

def test_before_first_stage_keeps_original():
    rows = _stage_rows([(date(2026, 6, 1), 60.0), (date(2027, 1, 1), 75.0)])
    # 早于首个阶段：继续采用建筑原高度，active_stage 为 None
    for d in ("2025-12-31", "2026-01-15", "2026-05-31"):
        r = resolve_height(ORIGINAL, rows, d)
        assert r["effective_top_height"] == ORIGINAL
        assert r["active_stage"] is None


def test_effective_day_takes_new_stage_and_latest_wins():
    rows = _stage_rows([(date(2026, 6, 1), 60.0), (date(2027, 1, 1), 75.0)])
    # 生效日当天（含）起采用新阶段
    r = resolve_height(ORIGINAL, rows, "2026-06-01")
    assert r["effective_top_height"] == 60.0
    assert r["active_stage"]["effective_date"] == "2026-06-01"
    # 两阶段之间取第一阶段
    assert resolve_height(ORIGINAL, rows, "2026-12-31")[
        "effective_top_height"] == 60.0
    # 第二阶段生效后取最新
    r2 = resolve_height(ORIGINAL, rows, "2027-01-01")
    assert r2["effective_top_height"] == 75.0
    assert r2["active_stage"]["effective_date"] == "2027-01-01"


def test_no_stages_building_unchanged():
    # 普通未配置阶段的建筑：任何日期都保持现有行为
    for d in ("2026-01-15", "2026-07-15"):
        assert resolve_height(ORIGINAL, [], d) == {
            "effective_top_height": ORIGINAL, "active_stage": None}


# ---------- 校验：日期唯一、高度有效 ----------

def test_duplicate_date_rejected():
    with pytest.raises(StageValidationError, match="重复"):
        validate_stages(
            [{"effective_date": "2026-06-01", "top_height": 60.0},
             {"effective_date": "2026-06-01", "top_height": 70.0}])


def test_validates_before_database_like_sorting():
    # 即使乱序提交，重复日期仍被识别
    with pytest.raises(StageValidationError, match="重复"):
        validate_stages(
            [{"effective_date": "2027-01-01", "top_height": 75.0},
             {"effective_date": "2026-06-01", "top_height": 60.0},
             {"effective_date": "2026-06-01", "top_height": 55.0}])


@pytest.mark.parametrize("h", [0, -12, float("inf"), -float("inf"), float("nan")])
def test_illegal_height_rejected(h):
    with pytest.raises(StageValidationError):
        validate_stages([{"effective_date": "2026-06-01", "top_height": h}])


def test_height_below_base_rejected():
    with pytest.raises(StageValidationError, match="底高"):
        validate_stages(
            [{"effective_date": "2026-06-01", "top_height": 3.0}],
            base_height=10.0)


def test_api_schema_rejects_bad_date_and_nonpositive():
    with pytest.raises(PydanticValidationError):
        HeightStageIn(effective_date="2026/06/01", top_height=60.0)
    with pytest.raises(PydanticValidationError):
        HeightStageIn(effective_date="2026-06-01", top_height=0)
    with pytest.raises(PydanticValidationError):
        HeightStageIn(effective_date="2026-06-01", top_height=float("nan"))
    ok = HeightStageIn(effective_date="2026-06-01", top_height=60,
                       note="塔楼加高")
    assert ok.note == "塔楼加高"


# ---------- 物理验证：生效日前后同一窗点遮挡不同 ----------

POINT = MeasurePointGeom(id="P1", name="南窗中点",
                         position=(0.0, 0.0, 3.0),
                         normal=(0.0, -1.0, 0.0))
WALL_FP = box_footprint(0.0, -11.5, 30.0, 3.0)


def _run_wall(top, date_str):
    scene = build_scene([BuildingGeom(
        name="B_tower", footprint=WALL_FP, base_height=0.0, top_height=top)])
    return analyze_point(scene, POINT, latitude=39.9, longitude=116.4,
                         tz="Asia/Shanghai", date=date_str,
                         north_offset_deg=0.0, step_minutes=5)


def test_same_window_differs_around_effective_day():
    """墙 2026-06-01 由 15 m 加高到 40 m：生效日当天同一窗点遮挡立即增加。

    两天都用同一日期跑几何（太阳位置相同），仅高度不同，
    差异完全来自生效阶段切换。
    """
    stages = _stage_rows([(date(2026, 6, 1), 40.0)])
    before_d, after_d = "2026-05-31", "2026-06-01"
    h_before = resolve_height(15.0, stages, before_d)["effective_top_height"]
    h_after = resolve_height(15.0, stages, after_d)["effective_top_height"]
    assert (h_before, h_after) == (15.0, 40.0)
    before = _run_wall(h_before, after_d)   # 同一日期、仅高度不同
    after = _run_wall(h_after, after_d)
    assert after["summary"]["sunlit_minutes"] < \
        before["summary"]["sunlit_minutes"]
    assert before["summary"]["sunlit_minutes"] == 890
    assert after["summary"]["sunlit_minutes"] == 695
    # 加高后必然存在该墙造成的遮挡
    assert any(s["occluder"] == "B_tower"
               for s in after["fine_samples"] if s["status"] == "shaded")
    assert not any(s["occluder"] == "B_tower"
                   for s in before["fine_samples"]
                   if s["status"] == "shaded")


# ---------- 快照追溯：修改阶段后旧运行快照仍是原体量 ----------

def test_snapshot_keeps_old_massing_after_stage_edit():
    from app.seed import LAT, LON, TZ

    scene = NS(id=1, name="S", description="", latitude=LAT, longitude=LON,
               timezone=TZ, north_offset_deg=0.0, unmodeled_occluders="")
    b_old = NS(id=10, name="B_tower", kind="building", color="#888",
               footprint=from_shape(Polygon(WALL_FP), srid=0),
               base_height=0.0, top_height=15.0,
               height_stages=_stage_rows([(date(2026, 6, 1), 40.0)]))
    points = [NS(id=1, name="p", window_id="W",
                 geom=from_shape(Point(*POINT.position), srid=0),
                 normal=list(POINT.normal))]
    old_snap = _bundle_payload(scene, [b_old], points,
                               on_date=date(2026, 7, 1))
    old_b = old_snap["buildings"][0]
    assert old_b["effective_top_height"] == 40.0
    assert old_b["active_stage"]["effective_date"] == "2026-06-01"
    assert old_snap["height_resolution_date"] == "2026-07-01"

    # 之后把加高阶段改成 55 m（模拟"修改阶段"）：旧快照内容不变
    b_new = NS(id=10, name="B_tower", kind="building", color="#888",
               footprint=from_shape(Polygon(WALL_FP), srid=0),
               base_height=0.0, top_height=15.0,
               height_stages=_stage_rows([(date(2026, 6, 1), 55.0)]))
    new_snap = _bundle_payload(scene, [b_new], points,
                               on_date=date(2026, 7, 1))
    assert old_snap["buildings"][0]["effective_top_height"] == 40.0
    assert new_snap["buildings"][0]["effective_top_height"] == 55.0
    # 旧快照仍能说明该运行当时采用的阶段与高度
    assert old_snap["height_resolution"][0]["effective_top_height"] == 40.0
