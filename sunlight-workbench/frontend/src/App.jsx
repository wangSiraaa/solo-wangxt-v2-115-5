import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import SceneViewer from './components/SceneViewer.jsx'
import ResultsPanel from './components/ResultsPanel.jsx'
import HeightStagesPanel from './components/HeightStagesPanel.jsx'

const DATES = ['2026-01-15', '2026-03-20', '2026-06-02', '2026-07-15'] // 跨冬夏算例日期

export default function App() {
  const [scenes, setScenes] = useState([])
  const [sceneId, setSceneId] = useState(null)
  const [livePayload, setLivePayload] = useState(null)  // 可编辑的当前场景（按日期预览）
  const [snapshotPayload, setSnapshotPayload] = useState(null) // 运行后：固定快照
  const [date, setDate] = useState(DATES[0])
  const [sunpath, setSunpath] = useState(null)
  const [timeIdx, setTimeIdx] = useState(30)
  const [run, setRun] = useState(null)
  const [selectedPointId, setSelectedPointId] = useState(null)
  const [highlightOccluder, setHighlightOccluder] = useState(null)
  const [trace, setTrace] = useState(null)
  const [error, setError] = useState(null)

  // 视图优先渲染快照（旧运行可追溯原体量），否则渲染当前场景
  const payload = snapshotPayload ?? livePayload

  useEffect(() => { api.scenes().then(setScenes).catch((e) => setError(String(e))) }, [])

  useEffect(() => {
    if (!sceneId) return
    setRun(null); setSelectedPointId(null); setTrace(null); setSnapshotPayload(null)
    // 带日期加载：未配置阶段时返回内容与原来一致；配置后预览当日生效体量
    api.scene(sceneId, date).then(setLivePayload)
      .catch((e) => setError(String(e)))
    api.sunpath(sceneId, date).then(setSunpath)
  }, [sceneId, date])

  const doSeed = async () => { await api.seed(); setScenes(await api.scenes()) }

  const doRun = async () => {
    setError(null)
    const r = await api.run(sceneId, date, 5)
    const full = await api.runResult(r.run_id)
    setRun(full)
    // 结果关联快照：渲染切换到快照内容，固定运行时高度阶段与体量
    const snap = await api.snapshot(full.snapshot_id)
    setSnapshotPayload(snap.payload)
  }

  const backToLive = () => {
    setRun(null); setSelectedPointId(null); setTrace(null); setSnapshotPayload(null)
  }

  // 阶段保存后就地刷新当前场景的阶段配置，并按当前日期重新解析体量
  const onStagesSaved = async () => {
    setLivePayload(await api.scene(sceneId, date))
  }

  // 当前时刻各测点状态（取最近细样本）
  const pointStatus = useMemo(() => {
    if (!run || !sunpath) return null
    const t = sunpath.points[timeIdx]?.time
    if (!t) return null
    const out = {}
    for (const res of run.results) {
      let best = null
      for (const s of res.fine_samples) {
        if (!best || Math.abs(s.time.localeCompare(t)) < Math.abs(best.time.localeCompare(t))) best = s
      }
      out[res.point_id] = best
    }
    return out
  }, [run, sunpath, timeIdx])

  const selectedResult = run?.results.find((r) => r.point_id === selectedPointId)
  const changedStages = (run?.height_resolution ?? []).filter(
    (r) => Math.abs(r.effective_top_height - r.original_top_height) > 1e-9)

  const doTrace = async (pointId) => {
    setTrace(await api.trace(run.run_id, pointId))
  }

  return (
    <div className="layout">
      <header>
        <b>日照分析工作台</b>
        <span className="badge">合成场景 · 示例评价口径 · 非规划合规结论</span>
      </header>
      <aside>
        <label>场景（坐标基准统一挂在场景上）</label>
        <select value={sceneId ?? ''} onChange={(e) => setSceneId(+e.target.value)}>
          <option value="" disabled>选择场景</option>
          {scenes.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        {!scenes.length && <button onClick={doSeed}>初始化合成场景</button>}
        {payload && (
          <div className="meta">
            <div>经纬度 {payload.scene.latitude}, {payload.scene.longitude}</div>
            <div>时区 {payload.scene.timezone}</div>
            <div>模型北偏角 {payload.scene.north_offset_deg}°</div>
            <div className="warn">未建模遮挡：{payload.scene.unmodeled_occluders}</div>
          </div>
        )}
        <label>日期（场景本地日期，跨冬夏算例）</label>
        <select value={date} onChange={(e) => setDate(e.target.value)}
          disabled={!!snapshotPayload}>
          {DATES.map((d) => <option key={d}>{d}</option>)}
        </select>
        {snapshotPayload
          ? <button onClick={backToLive}>返回当前场景（退出快照视图）</button>
          : <button disabled={!sceneId} onClick={doRun}>运行当日分析（5min 步长）</button>}
        {run && (
          <div className="muted small">
            run #{run.run_id} · 快照 #{run.snapshot_id} · {run.date}
          </div>
        )}
        {run && (
          <div className="meta">
            {changedStages.length === 0
              ? <div>该日期无高度阶段生效：全部建筑采用原高度。</div>
              : changedStages.map((r) => (
                <div key={r.building_id}>
                  {r.building_name} 采用 <b>{r.effective_top_height}m</b>
                  （原 {r.original_top_height}m，
                  {r.active_stage.effective_date} 阶段生效
                  {r.active_stage.note ? ` · ${r.active_stage.note}` : ''}）
                </div>
              ))}
          </div>
        )}
        {error && <div className="error">{error}</div>}
        {sunpath && (
          <>
            <label>时刻 {sunpath.points[timeIdx]?.time.slice(11, 16)}</label>
            <input type="range" min={0} max={sunpath.points.length - 1}
              value={timeIdx} onChange={(e) => setTimeIdx(+e.target.value)} />
          </>
        )}
        {trace && (
          <div className="trace">
            <h4>遮挡物追查（测点 #{trace.point_id}）</h4>
            <div>遮挡物：{trace.occluders.join('、') || '无'}</div>
            <div className="muted small">{trace.note}</div>
            <button onClick={() => setTrace(null)}>关闭</button>
          </div>
        )}
      </aside>
      <main>
        <SceneViewer
          payload={payload} sunpath={sunpath} timeIdx={timeIdx}
          pointStatus={pointStatus} selectedPointId={selectedPointId}
          highlightOccluder={highlightOccluder}
          onSelectPoint={setSelectedPointId}
          onSelectBuilding={setHighlightOccluder} />
      </main>
      <aside className="right">
        {!snapshotPayload && livePayload && (
          <HeightStagesPanel buildings={livePayload.buildings}
            onSaved={onStagesSaved} />
        )}
        {snapshotPayload && (
          <div className="snapshot-note">
            正在查看运行 #{run?.run_id} 的快照体量（{run?.date} 实际生效高度）。
            阶段后续修改不影响本视图；点「返回当前场景」可继续编辑阶段。
          </div>
        )}
        <ResultsPanel
          run={run} result={selectedResult}
          onHoverInterval={(iv) => setHighlightOccluder(iv?.occluder ?? null)}
          onTrace={doTrace} />
      </aside>
    </div>
  )
}
