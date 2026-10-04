import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

/** 高度阶段维护：为单个建筑编辑若干"生效日期＋高度"记录，保存时整体替换。
    客户端做提示性预检，权威校验在服务端（重复日期/非法高度返回 400）。 */
export default function StageEditor({ buildings, onSaved }) {
  const [bid, setBid] = useState('')
  const [rows, setRows] = useState([])
  const [msg, setMsg] = useState(null)

  const building = buildings.find((b) => b.id === +bid)

  useEffect(() => {
    setRows((building?.height_stages ?? []).map((s) => ({ ...s })))
    setMsg(null)
  }, [bid, building?.height_stages])

  const setRow = (i, patch) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  const precheck = () => {
    const dates = rows.map((r) => r.effective_date)
    if (dates.some((d) => !d)) return '存在未填写生效日期的记录'
    if (new Set(dates).size !== dates.length) return '生效日期重复，请修正后再保存'
    for (const r of rows) {
      const h = parseFloat(r.top_height)
      if (!Number.isFinite(h)) return `阶段 ${r.effective_date} 的高度须为数值`
      if (building && h <= building.base_height)
        return `阶段 ${r.effective_date} 的高度须大于基座高度 ${building.base_height} m`
    }
    return null
  }

  const save = async () => {
    setMsg(null)
    const err = precheck()
    if (err) { setMsg({ ok: false, text: err }); return }
    try {
      await api.saveStages(+bid, rows.map((r) => ({
        effective_date: r.effective_date,
        top_height: parseFloat(r.top_height),
      })))
      setMsg({ ok: true, text: '已保存，新分析将按阶段解析高度' })
      onSaved?.()
    } catch (e) {
      setMsg({ ok: false, text: `保存被拒绝：${e.message}` })
    }
  }

  return (
    <div className="stage-editor">
      <label>建筑高度阶段（按场景本地日期生效）</label>
      <select value={bid} onChange={(e) => setBid(e.target.value)}>
        <option value="" disabled>选择建筑</option>
        {buildings.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
      </select>
      {building && (
        <>
          <div className="muted small">
            原高度 {building.top_height} m（基座 {building.base_height} m）；
            早于首个阶段的日期仍按原高度。
          </div>
          {rows.map((r, i) => (
            <div key={i} className="stage-row">
              <input type="date" value={r.effective_date}
                onChange={(e) => setRow(i, { effective_date: e.target.value })} />
              <input type="number" step="0.5" min="0" value={r.top_height}
                onChange={(e) => setRow(i, { top_height: e.target.value })} />
              <span className="muted small">m</span>
              <button onClick={() => setRows(rows.filter((_, j) => j !== i))}>删</button>
            </div>
          ))}
          <div className="stage-actions">
            <button onClick={() => setRows([...rows, { effective_date: '', top_height: '' }])}>
              ＋阶段
            </button>
            <button onClick={save}>保存阶段</button>
          </div>
        </>
      )}
      {msg && <div className={msg.ok ? 'muted small' : 'error'}>{msg.text}</div>}
    </div>
  )
}
