"""高度阶段接口级验收测试（TestClient + 轻量假 DB，不依赖 PostgreSQL）。

覆盖验收口径：
- PUT 保存阶段、空列表清除、整组替换语义
- 重复日期 / 非法高度被拒绝（400/422），未知建筑 404
- GET 场景带 ?date 时早于首阶段用原高度、生效日起采用新阶段
"""
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from geoalchemy2.shape import from_shape
from shapely.geometry import Point, Polygon

from app import models
from app.db import get_db
from app.geometry import box_footprint
from app.main import app
from app.seed import LAT, LON, TZ


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter_by(self, **kw):
        return FakeQuery([r for r in self.rows
                          if all(getattr(r, k, None) == v for k, v in kw.items())])

    def all(self):
        return list(self.rows)

    def delete(self):
        n = len(self.rows)
        for r in self.rows:
            r._deleted = True
        return n

    def count(self):
        return len(self.rows)


class FakeDb:
    """只实现本文件用到的接口；行用 SimpleNamespace 模拟 ORM 对象。"""
    def __init__(self):
        self.rows = {}
        self._ids = {}
        self.added = []

    def register(self, model, obj):
        self.rows.setdefault(model, []).append(obj)
        if getattr(obj, "id", None) is None:
            self._ids[model] = self._ids.get(model, 0) + 1
            obj.id = self._ids[model]
        return obj

    def get(self, model, pk):
        for r in self.rows.get(model, []):
            if getattr(r, "_deleted", False):
                continue
            if r.id == pk:
                return r
        return None

    def query(self, model):
        return FakeQuery([r for r in self.rows.get(model, [])
                          if not getattr(r, "_deleted", False)])

    def add(self, obj):
        self.added.append(obj)
        model = type(obj)
        self.rows.setdefault(model, [])
        if getattr(obj, "id", None) is None:
            self._ids[model] = self._ids.get(model, 0) + 1
            obj.id = self._ids[model]
        self.rows[model].append(obj)
        # 模拟双向关系：RunPointResult.run / Run.results
        if model is models.RunPointResult:
            run = next((r for r in self.rows.get(models.Run, [])
                        if r.id == obj.run_id), None)
            if run is not None:
                obj.run = run
                run.results = [*getattr(run, "results", []), obj]

    def flush(self):
        pass

    def refresh(self, obj):
        # 模拟 ORM 关系加载：把未删除的 HeightStage 挂回建筑
        if isinstance(obj, NS):
            obj.height_stages = sorted(
                [r for r in self.rows.get(models.HeightStage, [])
                 if not getattr(r, "_deleted", False)
                 and r.building_id == obj.id],
                key=lambda x: x.effective_date)

    def commit(self):
        # 物理删除标记行，并为所有建筑刷新关系
        for model, lst in self.rows.items():
            self.rows[model] = [r for r in lst if not getattr(r, "_deleted", False)]
        for b in self.rows.get(models.Building, []):
            self.refresh(b)

    def close(self):
        pass


@pytest.fixture()
def client():
    db = FakeDb()
    scene = NS(id=1, name="S", description="", latitude=LAT, longitude=LON,
               timezone=TZ, north_offset_deg=0.0,
               unmodeled_occluders="树木未建模")
    db.register(models.Scene, scene)
    fp = from_shape(Polygon(box_footprint(35, -15, 18, 18)), srid=0)
    tower = NS(id=2, scene_id=1, name="B2_东南塔楼", kind="building",
               color="#7e93a8", footprint=fp, base_height=0.0,
               top_height=45.0, height_stages=[])
    db.register(models.Building, tower)
    # 正南墙（宽 30 m，11.5 m 远）+ 3 m 高南窗点：墙加高可立即改变遮挡
    wall_fp = from_shape(Polygon(box_footprint(0, -11.5, 30, 3)), srid=0)
    wall = NS(id=3, scene_id=1, name="B1_南侧板楼", kind="building",
              color="#8ea3b8", footprint=wall_fp, base_height=0.0,
              top_height=15.0, height_stages=[])
    db.register(models.Building, wall)
    point = NS(id=1, scene_id=1, building_id=None, name="W1",
               window_id="W1", geom=from_shape(Point(0, -7, 3), srid=0),
               normal=[0, -1, 0])
    db.register(models.MeasurePoint, point)

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    yield TestClient(app), db, tower
    app.dependency_overrides.clear()


def _put(client, bid, stages):
    return client.put(f"/api/buildings/{bid}/height-stages",
                      json={"stages": stages})


def test_put_stages_and_date_preview(client):
    c, db, tower = client
    r = _put(c, tower.id, [
        {"effective_date": "2026-06-01", "top_height": 60, "note": "塔楼加高"},
        {"effective_date": "2027-01-01", "top_height": 75, "note": ""},
    ])
    assert r.status_code == 200, r.text
    assert [s["effective_date"] for s in r.json()["stages"]] == \
        ["2026-06-01", "2027-01-01"]

    def eff(on_date):
        r = c.get(f"/api/scenes/1?date={on_date}")
        assert r.status_code == 200, r.text
        b = next(x for x in r.json()["buildings"] if x["id"] == tower.id)
        return b

    # 早于首个阶段：原高度、active_stage=null
    b = eff("2026-01-15")
    assert b["effective_top_height"] == 45.0 and b["active_stage"] is None
    # 生效日当天采用新阶段
    b = eff("2026-06-01")
    assert b["effective_top_height"] == 60.0
    assert b["active_stage"]["note"] == "塔楼加高"
    # 最新阶段生效
    assert eff("2027-06-01")["effective_top_height"] == 75.0
    # 不带日期：普通场景视图保持原行为（无 effective 字段）
    r = c.get("/api/scenes/1")
    b0 = next(x for x in r.json()["buildings"] if x["id"] == tower.id)
    assert "effective_top_height" not in b0
    assert r.json()["height_resolution"] is None


def test_replace_and_clear_semantics(client):
    c, db, tower = client
    assert _put(c, tower.id, [
        {"effective_date": "2026-06-01", "top_height": 60}]).status_code == 200
    # 整组替换：旧阶段被删除，只留新阶段
    r = _put(c, tower.id, [
        {"effective_date": "2028-03-01", "top_height": 80}])
    assert r.status_code == 200
    assert [s["effective_date"] for s in r.json()["stages"]] == \
        ["2028-03-01"]
    assert len(db.rows.get(models.HeightStage, [])) == 1
    # 空列表清除
    r = _put(c, tower.id, [])
    assert r.status_code == 200 and r.json()["stages"] == []
    assert db.rows.get(models.HeightStage, []) == []


def test_duplicate_date_rejected_without_partial_write(client):
    c, db, tower = client
    r = _put(c, tower.id, [
        {"effective_date": "2026-06-01", "top_height": 60},
        {"effective_date": "2026-06-01", "top_height": 70}])
    assert r.status_code == 400
    assert "重复" in r.json()["detail"]
    # 拒绝后不残留任何阶段
    assert db.rows.get(models.HeightStage, []) == []


def test_illegal_height_rejected(client):
    c, db, tower = client
    # schema 层（非正数、NaN）→ 422
    for h in (0, -10):
        r = _put(c, tower.id, [{"effective_date": "2026-06-01", "top_height": h}])
        assert r.status_code == 422, h
    # 低于 base_height → 业务校验 400
    tower.base_height = 50.0
    r = _put(c, tower.id, [{"effective_date": "2026-06-01", "top_height": 40}])
    assert r.status_code == 400 and "底高" in r.json()["detail"]
    # 非法日期格式
    r = _put(c, tower.id, [{"effective_date": "2026/06/01", "top_height": 60}])
    assert r.status_code == 422
    assert db.rows.get(models.HeightStage, []) == []


def test_unknown_building_404(client):
    c, _, _ = client
    r = _put(c, 999, [{"effective_date": "2026-06-01", "top_height": 60}])
    assert r.status_code == 404


def test_run_before_after_effective_date_and_snapshot_audit(client):
    """端到端验收：同一窗点在生效日前后运行采用不同高度、得到不同结果；
    之后修改阶段，旧运行的快照仍固定原体量。"""
    c, db, tower = client
    wall = db.get(models.Building, 3)
    assert _put(c, wall.id, [
        {"effective_date": "2026-06-01", "top_height": 40,
         "note": "板楼加高"}]).status_code == 200

    def run(on_date):
        r = c.post("/api/analysis/run",
                   json={"scene_id": 1, "date": on_date, "step_minutes": 5})
        assert r.status_code == 200, r.text
        return r.json()

    before = run("2026-05-31")   # 阶段尚未生效 → 原 15 m
    after = run("2026-06-01")    # 生效日当天 → 40 m
    # 运行回执带高度口径
    wall_res_before = next(x for x in before["height_resolution"]
                           if x["building_id"] == wall.id)
    wall_res_after = next(x for x in after["height_resolution"]
                          if x["building_id"] == wall.id)
    assert wall_res_before["effective_top_height"] == 15.0
    assert wall_res_before["active_stage"] is None
    assert wall_res_after["effective_top_height"] == 40.0
    assert wall_res_after["active_stage"]["effective_date"] == "2026-06-01"
    # 同一窗点结果不同
    rb = c.get(f"/api/analysis/{before['run_id']}").json()
    ra = c.get(f"/api/analysis/{after['run_id']}").json()
    sb = rb["results"][0]["summary"]["sunlit_minutes"]
    sa = ra["results"][0]["summary"]["sunlit_minutes"]
    assert sa < sb, (sb, sa)
    assert ra["height_resolution"] is not None

    # 修改阶段（40 → 55 m）后，旧快照仍是当时的 40 m 原体量
    assert _put(c, wall.id, [
        {"effective_date": "2026-06-01", "top_height": 55}]).status_code == 200
    old_snap = c.get(f"/api/snapshots/{after['snapshot_id']}").json()["payload"]
    old_wall = next(x for x in old_snap["buildings"] if x["id"] == wall.id)
    assert old_wall["effective_top_height"] == 40.0
    assert old_wall["active_stage"]["top_height"] == 40.0
    # 旧快照连阶段配置也固定为当时的 40 m（不可变 JSON，完整追溯）
    assert old_wall["height_stages"][0]["top_height"] == 40.0
    assert old_snap["height_resolution_date"] == "2026-06-01"
    # 当前场景则已是修改后的 55 m
    live = c.get("/api/scenes/1?date=2026-06-01").json()
    live_wall = next(x for x in live["buildings"] if x["id"] == wall.id)
    assert live_wall["height_stages"][0]["top_height"] == 55.0
    assert live_wall["effective_top_height"] == 55.0
