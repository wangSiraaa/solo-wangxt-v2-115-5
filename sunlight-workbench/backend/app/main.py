"""FastAPI 入口：场景 / 建筑高度阶段 / 测点 / 分析运行 / 快照 / 单点追查。"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from . import models, seed
from .stages import (StageValidationError, parse_date, resolve_height,
                     validate_stages)
from .analysis import MeasurePointGeom, analyze_point
from .db import Base, engine, get_db
from .geometry import BuildingGeom, build_scene
from .solar import enu_to_model, sun_vector_enu, solar_positions
from .geometry import cast_sun_ray
import pandas as pd

app = FastAPI(title="日照分析工作台（合成场景·示例口径）")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

DISCLAIMER = ("合成场景 + 示例评价口径输出，未建模遮挡（树木等）见场景说明；"
              "逐时采样≠连续日照时长；本结果不构成规划合规结论。")


@app.exception_handler(StageValidationError)
def _stage_validation_handler(request, exc: StageValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)


# ---------- 场景 ----------

class SceneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    latitude: float
    longitude: float
    timezone: str
    north_offset_deg: float
    unmodeled_occluders: str


def _scene_json(s: models.Scene) -> dict:
    return SceneOut.model_validate(s).model_dump()


@app.get("/api/scenes")
def list_scenes(db: Session = Depends(get_db)):
    return [_scene_json(s) for s in db.query(models.Scene).all()]


@app.post("/api/scenes/seed")
def seed_scenes(db: Session = Depends(get_db)):
    if db.query(models.Scene).count():
        raise HTTPException(409, "已有场景，拒绝重复种子")
    return {"scene_ids": seed.seed_database(db)}


def _load_scene_bundle(db: Session, scene_id: int):
    s = db.get(models.Scene, scene_id)
    if not s:
        raise HTTPException(404, "场景不存在")
    buildings = db.query(models.Building).filter_by(scene_id=scene_id).all()
    points = db.query(models.MeasurePoint).filter_by(scene_id=scene_id).all()
    return s, buildings, points


def _building_json(b, on_date=None) -> dict:
    """建筑 JSON。on_date 给定时按高度阶段解析当日实际顶高。

    - height_stages 为全部阶段配置（始终输出，便于编辑与追溯）；
    - effective_top_height / active_stage 为该日期实际采用的高度与阶段，
      早于首个阶段时 active_stage=null，采用建筑原顶高。
    """
    rows = getattr(b, "height_stages", None) or []
    stage_list = [{
        "effective_date": str(st.effective_date),
        "top_height": st.top_height, "note": st.note or "",
    } for st in sorted(rows, key=lambda x: x.effective_date)]
    out = {
        "id": b.id, "name": b.name, "kind": b.kind, "color": b.color,
        "footprint": list(to_shape(b.footprint).exterior.coords)[:-1],
        "base_height": b.base_height, "top_height": b.top_height,
        "height_stages": stage_list,
    }
    if on_date is not None:
        r = resolve_height(b.top_height, rows, on_date)
        out["effective_top_height"] = r["effective_top_height"]
        out["active_stage"] = r["active_stage"]
    return out


def _bundle_payload(s, buildings, points, on_date=None) -> dict:
    """场景完整 JSON（即快照内容，前端渲染也用它）。

    on_date（运行日期）给定时，建筑 top_height 之外再写入当日生效高度，
    快照因此固定运行时实际体量；height_resolution 汇总每栋楼采用的阶段。
    """
    bjson = [_building_json(b, on_date) for b in buildings]
    height_resolution = None
    if on_date is not None:
        height_resolution = [{
            "building_id": bj["id"], "building_name": bj["name"],
            "original_top_height": bj["top_height"],
            "effective_top_height": bj["effective_top_height"],
            "active_stage": bj["active_stage"],
        } for bj in bjson]
    return {
        "scene": _scene_json(s),
        "buildings": bjson,
        "points": [{
            "id": p.id, "name": p.name, "window_id": p.window_id,
            "position": list(to_shape(p.geom).coords[0]), "normal": p.normal,
        } for p in points],
        "height_resolution_date": (on_date.isoformat()
                                   if on_date is not None else None),
        "height_resolution": height_resolution,
    }


@app.get("/api/scenes/{scene_id}")
def get_scene(scene_id: int, date: str | None = None,
              db: Session = Depends(get_db)):
    """场景包；带 ?date=YYYY-MM-DD 时按该日期预览高度阶段生效后的体量。"""
    s, buildings, points = _load_scene_bundle(db, scene_id)
    on_date = parse_date(date) if date is not None else None
    return _bundle_payload(s, buildings, points, on_date)


# ---------- 建筑高度阶段 ----------

class HeightStageIn(BaseModel):
    effective_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    top_height: float = Field(..., gt=0)
    note: str = ""


class HeightStagesRequest(BaseModel):
    stages: list[HeightStageIn]


@app.put("/api/buildings/{building_id}/height-stages")
def replace_height_stages(building_id: int, req: HeightStagesRequest,
                          db: Session = Depends(get_db)):
    """整组替换某栋建筑的高度阶段（空列表 = 清除，建筑恢复恒定原高度）。

    拒绝：重复生效日期、非有限/非正高度、低于建筑 base_height 的高度。
    """
    b = db.get(models.Building, building_id)
    if not b:
        raise HTTPException(404, "建筑不存在")
    incoming = [{"effective_date": st.effective_date,
                 "top_height": st.top_height, "note": st.note}
                for st in req.stages]
    # validate_stages 抛 StageValidationError → 全局处理器返回 400
    normalized = validate_stages(incoming, base_height=b.base_height)
    db.query(models.HeightStage).filter_by(building_id=b.id).delete()
    for st in normalized:
        db.add(models.HeightStage(
            building_id=b.id, effective_date=st["effective_date"],
            top_height=st["top_height"], note=st["note"]))
    db.commit()
    db.refresh(b)
    return {"building_id": b.id,
            "stages": _building_json(b)["height_stages"]}


# ---------- 分析 ----------

class RunRequest(BaseModel):
    scene_id: int
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    step_minutes: int = Field(5, ge=1, le=60)
    point_ids: list[int] | None = None  # 缺省 = 场景全部测点


def _geom_from_bundle(buildings, points, effective_heights=None):
    """从 ORM 行重建几何；effective_heights 按 building_id 覆盖顶高
    （键不存在时回退建筑原顶高）。"""
    geoms = []
    for b in buildings:
        top = (effective_heights or {}).get(b.id, b.top_height)
        geoms.append(BuildingGeom(
            name=b.name,
            footprint=[tuple(c) for c in
                       to_shape(b.footprint).exterior.coords][:-1],
            base_height=b.base_height, top_height=top))
    built = build_scene(geoms)
    pts = [MeasurePointGeom(id=str(p.id), name=p.name,
                            position=tuple(to_shape(p.geom).coords[0]),
                            normal=tuple(p.normal))
           for p in points]
    return built, pts


@app.post("/api/analysis/run")
def run_analysis(req: RunRequest, db: Session = Depends(get_db)):
    s, buildings, points = _load_scene_bundle(db, req.scene_id)
    if req.point_ids:
        points = [p for p in points if p.id in req.point_ids]
        if not points:
            raise HTTPException(400, "point_ids 无匹配测点")
    run_date = parse_date(req.date)  # 场景本地日期；格式错误 → 400
    # 高度阶段在运行日期解析一次：几何与快照共用同一份高度口径
    resolutions = {b.id: resolve_height(
        b.top_height, getattr(b, "height_stages", None) or [], run_date)
        for b in buildings}
    effective_heights = {bid: r["effective_top_height"]
                         for bid, r in resolutions.items()}
    # 1) 快照先行：结果关联快照，场景/阶段后续被改动也不影响追溯
    payload = _bundle_payload(s, buildings, points, on_date=run_date)
    snap = models.Snapshot(scene_id=s.id, payload=payload)
    db.add(snap)
    db.flush()
    run = models.Run(scene_id=s.id, snapshot_id=snap.id,
                     run_date=run_date,
                     step_minutes=req.step_minutes,
                     params={"point_ids": [p.id for p in points],
                             "height_resolution": [{
                                 "building_id": bid,
                                 "building_name": next(
                                     b.name for b in buildings if b.id == bid),
                                 "original_top_height": next(
                                     b.top_height for b in buildings
                                     if b.id == bid),
                                 "effective_top_height":
                                     r["effective_top_height"],
                                 "active_stage": r["active_stage"],
                             } for bid, r in resolutions.items()]},
                     disclaimer=DISCLAIMER)
    db.add(run)
    db.flush()
    # 2) 分析几何采用运行日期生效高度（而非 building 表当前原顶高）
    built, pts = _geom_from_bundle(buildings, points, effective_heights)
    for pt in pts:
        r = analyze_point(built, pt, latitude=s.latitude, longitude=s.longitude,
                          tz=s.timezone, date=req.date,
                          north_offset_deg=s.north_offset_deg,
                          step_minutes=req.step_minutes)
        db.add(models.RunPointResult(
            run_id=run.id, point_id=int(pt.id),
            hourly_samples=r["hourly_samples"],
            continuous_intervals=r["continuous_intervals"],
            fine_samples=r["fine_samples"], summary=r["summary"]))
    db.commit()
    return {"run_id": run.id, "snapshot_id": snap.id,
            "date": run_date.isoformat(), "disclaimer": DISCLAIMER,
            "height_resolution": payload["height_resolution"]}


@app.get("/api/analysis/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.Run, run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    return {
        "run_id": run.id, "scene_id": run.scene_id,
        "snapshot_id": run.snapshot_id, "date": str(run.run_date),
        "step_minutes": run.step_minutes, "disclaimer": run.disclaimer,
        "height_resolution": (run.params or {}).get("height_resolution"),
        "results": [{
            "point_id": r.point_id,
            "summary": r.summary,
            "hourly_samples": r.hourly_samples,
            "continuous_intervals": r.continuous_intervals,
            "fine_samples": r.fine_samples,
        } for r in run.results],
    }


@app.get("/api/analysis/{run_id}/points/{point_id}/trace")
def trace_point(run_id: int, point_id: int, time: str | None = None,
                db: Session = Depends(get_db)):
    """单点追查：返回该点逐样本遮挡物；给定 time 时只返回该时刻。"""
    r = (db.query(models.RunPointResult)
         .filter_by(run_id=run_id, point_id=point_id).first())
    if not r:
        raise HTTPException(404, "结果不存在")
    samples = r.fine_samples
    if time:
        samples = [s for s in samples if s["time"].startswith(time)]
        if not samples:
            raise HTTPException(404, "该时刻无采样（注意步长与本地时区）")
    shaded = [s for s in samples if s["status"] == "shaded"]
    return {
        "point_id": point_id,
        "queried": len(samples), "shaded": len(shaded),
        "occluders": sorted({s["occluder"] for s in shaded}),
        "samples": samples,
        "note": "遮挡物名称来自场景快照几何；未建模遮挡（树木等）见场景说明。",
    }


# ---------- 快照 ----------

@app.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    snap = db.get(models.Snapshot, snapshot_id)
    if not snap:
        raise HTTPException(404, "快照不存在")
    return {"snapshot_id": snap.id, "created_at": str(snap.created_at),
            "payload": snap.payload}


# ---------- 太阳路径（前端可视化） ----------

@app.get("/api/scenes/{scene_id}/sunpath")
def sunpath(scene_id: int, date: str, db: Session = Depends(get_db)):
    s = db.get(models.Scene, scene_id)
    if not s:
        raise HTTPException(404, "场景不存在")
    times = pd.date_range(pd.Timestamp(date, tz=s.timezone),
                          periods=96, freq="15min")
    pos = solar_positions(s.latitude, s.longitude, s.timezone, times)
    out = []
    for t, row in zip(times, pos.itertuples()):
        if row.apparent_elevation <= 0:
            continue
        d = enu_to_model(sun_vector_enu(row.apparent_elevation, row.azimuth),
                         s.north_offset_deg)
        out.append({"time": t.isoformat(), "dir": [round(float(v), 5) for v in d],
                    "elevation": round(float(row.apparent_elevation), 2),
                    "azimuth": round(float(row.azimuth), 2)})
    return {"date": date, "timezone": s.timezone, "points": out}
