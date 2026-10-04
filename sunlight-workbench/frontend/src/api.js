const BASE = '/api'

async function req(path, options) {
  const r = await fetch(BASE + path, options)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json()
}

export const api = {
  scenes: () => req('/scenes'),
  seed: () => req('/scenes/seed', { method: 'POST' }),
  scene: (id, onDate) =>
    req(`/scenes/${id}` + (onDate ? `?date=${onDate}` : '')),
  saveStages: (buildingId, stages) =>
    req(`/buildings/${buildingId}/height-stages`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stages }),
    }),
  sunpath: (id, date) => req(`/scenes/${id}/sunpath?date=${date}`),
  run: (scene_id, date, step_minutes = 5) =>
    req('/analysis/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scene_id, date, step_minutes }),
    }),
  runResult: (runId) => req(`/analysis/${runId}`),
  trace: (runId, pointId, time) =>
    req(`/analysis/${runId}/points/${pointId}/trace` + (time ? `?time=${time}` : '')),
  snapshot: (id) => req(`/snapshots/${id}`),
}
