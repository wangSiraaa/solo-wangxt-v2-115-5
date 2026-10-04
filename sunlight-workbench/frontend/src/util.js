/* 坐标映射：模型 ENU(x=东, y=北, z=上) → three.js(x, y=上, z)。
   (x,y,z)_model → (x, z, -y)_three 是行列式为 +1 的旋转，手性不变。 */
export const toThree = ([x, y, z]) => [x, z, -y]

export const STATUS_COLOR = {
  sunlit: '#f6c453',
  shaded: '#4a6fa5',
  night: '#666',
}

export function fmtMin(m) {
  const h = Math.floor(m / 60)
  const r = m % 60
  return h ? `${h}h${r ? `${r}m` : ''}` : `${r}m`
}

export function fmtTime(iso) {
  return iso.slice(11, 16)
}

/** 高度阶段：返回 date（YYYY-MM-DD）已生效的最新阶段，无则 null。
    与后端 app/stages.py 同一口径：effective_date <= date 中取最新；
    早于首个阶段返回 null（采用建筑原高度）。 */
export function effectiveStage(building, date) {
  const stages = building?.height_stages
  if (!stages?.length || !date) return null
  let best = null
  for (const s of stages) {
    if (s.effective_date <= date && (!best || s.effective_date > best.effective_date)) best = s
  }
  return best
}

/** 实时场景按所选日期解析各建筑实际顶高；快照（含 resolved_for_date）
    已在后端按运行日期解析，直接透传，保证旧运行体量可追查。 */
export function resolvePayloadForDate(payload, date) {
  if (!payload || payload.resolved_for_date) return payload
  return {
    ...payload,
    buildings: payload.buildings.map((b) => {
      const st = effectiveStage(b, date)
      return st ? { ...b, top_height: st.top_height, applied_stage: st } : b
    }),
  }
}
