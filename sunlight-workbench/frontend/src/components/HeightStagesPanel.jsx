import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

/** 单栋建筑的高度阶段维护：生效日期 + 高度（顶高，米）。
    日期重复 / 非法高度由后端拒绝（400），错误就地显示。 */
function BuildingStages({ building, disabled, onSaved }) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(null)   // [{effective_date, top_height, note}]
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => { setDraft(null); setError(null) }, [building.id])

  const stages = draft ?? building.height_stages ?? []
  const dirty = draft !== null

  const edit = () => {
    setDraft((building.height_stages ?? []).map(
      (s) => ({ ...s, top_height: String(s.top_height) })))
    setOpen(true)
  }
  const update = (i, key, value) =>
    setDraft(stages.map((s, j) => (j === i ? { ...s, [key]: value } : s)))
  const addRow = () =>
    setDraft([...stages, { effective_date: '2026-06-01', top_height: '', note: '' }])
  const dropRow = (i) => setDraft(stages.filter((_, j) => j !== i))

  const save = async () => {
    setBusy(true); setError(null)
    try {
      const payload = stages.map((s) => ({
        effective_date: s.effective_date,
        top_height: Number(s.top_height), note: s.note ?? '',
      }))
      const r = await api.saveStages(building.id, payload)
      setDraft(null)
      onSaved?.(r.stages)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stages-card">
      <div className="stages-head" onClick={() => setOpen(!open)}>
        <span>{open ? '▾' : '▸'} {building.name}</span>
        <span className="muted">原顶高 {building.top_height}m ·
          {' '}{(building.height_stages ?? []).length} 个阶段</span>
      </div>
      {open && (
        <div className="stages-body">
          {(building.height_stages ?? []).length === 0 && !dirty && (
            <div className="muted small">未配置阶段：任何日期均采用原高度（现有行为）。</div>
          )}
          {!dirty && (building.height_stages ?? []).map((s) => (
            <div key={s.effective_date} className="stage-row saved">
              {s.effective_date} 起 → <b>{s.top_height}m</b>
              {s.note ? <span className="muted">（{s.note}）</span> : null}
            </div>
          ))}
          {dirty && stages.map((s, i) => (
            <div key={i} className="stage-row">
              <input type="date" value={s.effective_date}
                disabled={disabled || busy}
                onChange={(e) => update(i, 'effective_date', e.target.value)} />
              <input type="number" step="0.1" placeholder="顶高m"
                value={s.top_height} disabled={busy}
                onChange={(e) => update(i, 'top_height', e.target.value)} />
              <input type="text" placeholder="说明（可选）" value={s.note ?? ''}
                disabled={busy}
                onChange={(e) => update(i, 'note', e.target.value)} />
              <button disabled={busy} onClick={() => dropRow(i)}>×</button>
            </div>
          ))}
          {dirty
            ? <div className="stages-actions">
                <button disabled={busy} onClick={addRow}>+ 阶段</button>
                <button disabled={busy} onClick={() => setDraft(null)}>取消</button>
                <button className="primary" disabled={busy} onClick={save}>
                  {busy ? '保存中…' : '保存'}
                </button>
              </div>
            : <button disabled={disabled} onClick={edit}>编辑阶段</button>}
          {error && <div className="error">{error}</div>}
        </div>
      )}
    </div>
  )
}

export default function HeightStagesPanel({ buildings, disabled, onSaved }) {
  if (!buildings) return null
  return (
    <div className="stages">
      <label>建筑高度阶段（按场景本地日期生效）</label>
      {buildings.map((b) => (
        <BuildingStages key={b.id} building={b} disabled={disabled}
          onSaved={(stages) => onSaved?.(b.id, stages)} />
      ))}
    </div>
  )
}
