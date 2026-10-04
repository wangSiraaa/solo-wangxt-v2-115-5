"""高度阶段：解析、校验、快照记录与"生效日前后不同高度"验收测试。

不依赖 PostgreSQL（与 test_api_logic 同风格，用内存对象模拟 ORM 行）：
- 解析规则：取运行日期已生效的最新阶段；早于首个阶段用建筑原高度
- 校验规则：生效日期唯一、高度为有限数值且大于基座高度
- 快照：as_of 给定时 top_height 为实际采用高度并记录 applied_stage
- 端到端：同一窗点在生效日前后两个运行日期采用不同高度，结果不同
"""
from datetime import date
from types import SimpleNamespace as NS

import numpy as np
import pytest
from fastapi import HTTPException
from geoalchemy2.shape import from_shape
from shapely.geometry import Point, Polygon

from app.analysis import analyze_point
from app.geometry import box_footprint, cast_sun_ray
from app.main import (_bundle_payload, _geom_from_bundle, put_stages,
                      StagesPut, StageIn)
from app.solar import enu_to_model, sun_vector_enu
from app.stages import (resolve_height_stage, resolve_top_height,
                        validate_height_stages)

# ---- 合成算例：测点正南 10 m 处 10×3 m 墙，原高 4 m，2026-01-15 起加高到 10 m ----
# 冬季太阳低：4 m 墙全天遮不到该点，10 m 墙正午前后遮挡约 3.5 小时（手算可核）
STAGE_DATE = date(2026, 1, 15)
WALL_STAGES = [NS(effective_date=STAGE_DATE, top_height=10.0)]


def _wall_row(stages=WALL_STAGES):
    return NS(id=1, name="wall_south", kind="building", color="#888",
              footprint=from_shape(Polygon(box_footprint(0.0, -11.5, 10.0, 3.0)),
                                   srid=0),
              base_height=0.0, top_height=4.0, height_stages=list(stages))


def _point_row():
    return NS(id=1, name="南窗中点", window_id="W1",
              geom=from_shape(Point(0.0, 0.0, 1.2), srid=0),
              normal=[0.0, -1.0, 0.0])


# ---------- 解析规则 ----------

def test_resolve_picks_latest_effective_stage():
    stages = [NS(effective_date=date(2026, 3, 1), top_height=50.0),
              NS(effective_date=date(2026, 6, 1), top_height=60.0)]
    # 早于首个阶段 → None（调用方回落原高度）
    assert resolve_height_stage(stages, date(2026, 2, 28)) is None
    # 生效当日即生效
    assert resolve_height_stage(stages, date(2026, 3, 1)).top_height == 50.0
    # 两阶段之间取较早的；之后取最新
    assert resolve_height_stage(stages, date(2026, 5, 31)).top_height == 50.0
    assert resolve_height_stage(stages, date(2026, 6, 1)).top_height == 60.0
    assert resolve_height_stage(stages, date(2027, 1, 1)).top_height == 60.0


def test_resolve_top_height_falls_back_to_base():
    assert resolve_top_height(4.0, WALL_STAGES, date(2026, 1, 14)) == (4.0, None)
    top, st = resolve_top_height(4.0, WALL_STAGES, "2026-01-15")  # 字符串日期也可
    assert top == 10.0 and st.effective_date == STAGE_DATE
    # 未配置阶段的建筑永远用原高度
    assert resolve_top_height(45.0, [], date(2026, 1, 15)) == (45.0, None)


# ---------- 校验规则 ----------

def test_validate_rejects_duplicate_dates():
    with pytest.raises(ValueError, match="重复"):
        validate_height_stages([(date(2026, 6, 1), 60.0),
                                (date(2026, 6, 1), 70.0)], base_height=0.0)


def test_validate_rejects_invalid_heights():
    for bad in (0.0, -5.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            validate_height_stages([(date(2026, 6, 1), bad)], base_height=0.0)
    # 不得小于等于基座高度
    with pytest.raises(ValueError, match="基座"):
        validate_height_stages([(date(2026, 6, 1), 2.0)], base_height=2.0)
    # 合法集合通过（含多条、乱序）
    validate_height_stages([(date(2026, 6, 1), 60.0),
                            (date(2026, 3, 1), 50.0)], base_height=0.0)


class _FakeDB:
    """最小 DB 替身：get 返回建筑、commit 空操作。"""

    def __init__(self, building):
        self._building = building

    def get(self, model, pk):
        return self._building

    def commit(self):
        pass


def test_put_stages_endpoint_rejects_duplicates_and_bad_height():
    b = NS(id=1, base_height=0.0, top_height=45.0, height_stages=[])
    dup = StagesPut(stages=[StageIn(effective_date="2026-06-01", top_height=60.0),
                            StageIn(effective_date="2026-06-01", top_height=70.0)])
    with pytest.raises(HTTPException) as e:
        put_stages(1, dup, db=_FakeDB(b))
    assert e.value.status_code == 400 and "重复" in e.value.detail
    bad = StagesPut(stages=[StageIn(effective_date="2026-06-01", top_height=-1.0)])
    with pytest.raises(HTTPException) as e:
        put_stages(1, bad, db=_FakeDB(b))
    assert e.value.status_code == 400


def test_put_stages_endpoint_accepts_valid_set():
    b = NS(id=1, base_height=0.0, top_height=45.0, height_stages=[])
    ok = StagesPut(stages=[StageIn(effective_date="2026-06-01", top_height=60.0),
                           StageIn(effective_date="2026-03-01", top_height=50.0)])
    out = put_stages(1, ok, db=_FakeDB(b))
    assert len(out["stages"]) == 2
    assert {s["effective_date"] for s in out["stages"]} == {"2026-03-01",
                                                            "2026-06-01"}


# ---------- 快照记录 ----------

def _scene_row():
    return NS(id=1, name="S", description="", latitude=39.9, longitude=116.4,
              timezone="Asia/Shanghai", north_offset_deg=0.0,
              unmodeled_occluders="")


def test_snapshot_payload_records_applied_stage_and_height():
    buildings = [_wall_row()]
    # 生效日前：原高度、无命中阶段
    before = _bundle_payload(_scene_row(), buildings, [_point_row()],
                             as_of=date(2026, 1, 14))
    bb = before["buildings"][0]
    assert before["resolved_for_date"] == "2026-01-14"
    assert bb["top_height"] == 4.0 and bb["base_top_height"] == 4.0
    assert bb["applied_stage"] is None
    assert bb["height_stages"] == [{"effective_date": "2026-01-15",
                                    "top_height": 10.0}]
    # 生效日后：实际采用高度 + 命中阶段写入快照（旧运行据此追查原体量）
    after = _bundle_payload(_scene_row(), buildings, [_point_row()],
                            as_of=date(2026, 1, 15))
    ba = after["buildings"][0]
    assert ba["top_height"] == 10.0 and ba["base_top_height"] == 4.0
    assert ba["applied_stage"] == {"effective_date": "2026-01-15",
                                   "top_height": 10.0}


def test_live_payload_keeps_base_height_and_lists_stages():
    payload = _bundle_payload(_scene_row(), [_wall_row()], [_point_row()])
    b = payload["buildings"][0]
    assert "resolved_for_date" not in payload
    assert b["top_height"] == 4.0            # 实时视图保持原高度
    assert b["applied_stage"] is None
    assert len(b["height_stages"]) == 1


def test_building_without_stages_keeps_existing_behavior():
    plain = _wall_row(stages=[])
    for as_of in (None, date(2026, 1, 15)):
        payload = _bundle_payload(_scene_row(), [plain], [_point_row()],
                                  as_of=as_of)
        b = payload["buildings"][0]
        assert b["top_height"] == 4.0
        assert b["applied_stage"] is None
        assert b["height_stages"] == []


# ---------- 生效日前后：同一窗点采用不同高度 ----------

def test_geom_uses_stage_height_after_effective_date():
    """固定太阳向量（高度角 30°、正南）：4 m 墙够不着，10 m 墙必遮挡。"""
    d = enu_to_model(sun_vector_enu(30.0, 180.0), 0.0)
    origin = np.array([0.0, -0.01, 1.2])
    built_before, _ = _geom_from_bundle([_wall_row()], [_point_row()],
                                        as_of=date(2026, 1, 14))
    built_after, _ = _geom_from_bundle([_wall_row()], [_point_row()],
                                       as_of=date(2026, 1, 15))
    assert cast_sun_ray(built_before, origin, d) is None
    hit = cast_sun_ray(built_after, origin, d)
    assert hit is not None and hit["occluder"] == "wall_south"


def test_same_point_differs_across_effective_date():
    """验收口径：同一窗点，运行日期在生效日前后 → 采用不同高度 → 结果不同。

    冬季正午最大高度角约 27°：4 m 墙（临界 15.6°）全天遮不到该点；
    10 m 墙（临界 41.4°）在正午前后持续遮挡，生效日后的连续口径日照
    分钟数严格更少（相邻两日白昼时长差仅约 1 分钟，差异由高度主导）。
    """
    kw = dict(latitude=39.9, longitude=116.4, tz="Asia/Shanghai",
              north_offset_deg=0.0, step_minutes=10)
    built_before, pts = _geom_from_bundle([_wall_row()], [_point_row()],
                                          as_of=date(2026, 1, 14))
    built_after, _ = _geom_from_bundle([_wall_row()], [_point_row()],
                                       as_of=date(2026, 1, 16))
    r_before = analyze_point(built_before, pts[0], date="2026-01-14", **kw)
    r_after = analyze_point(built_after, pts[0], date="2026-01-16", **kw)
    s_before = r_before["summary"]["sunlit_minutes"]
    s_after = r_after["summary"]["sunlit_minutes"]
    assert s_after < s_before
    # 生效日后的遮挡样本必须指认该墙
    shaded = [s for s in r_after["fine_samples"] if s["status"] == "shaded"]
    assert shaded and all(s["occluder"] == "wall_south" for s in shaded)
