import React, { useMemo } from 'react'
import * as THREE from 'three'
import { Canvas } from '@react-three/fiber'
import { OrbitControls, Line, Html } from '@react-three/drei'
import { toThree } from '../util.js'

const RAY_LEN = 120
const SUN_R = 160

function extrude(b, topHeight) {
  const shape = new THREE.Shape()
  b.footprint.forEach(([x, y], i) => (i ? shape.lineTo(x, y) : shape.moveTo(x, y)))
  const g = new THREE.ExtrudeGeometry(shape, {
    depth: topHeight - b.base_height, bevelEnabled: false,
  })
  g.translate(0, 0, b.base_height)
  g.rotateX(-Math.PI / 2) // 模型 z-up → three y-up
  return g
}

function Building({ b, effectiveTop, highlighted, hasActiveStage, onClick }) {
  const geom = useMemo(() => extrude(b, effectiveTop),
    [b.footprint, b.base_height, effectiveTop])
  return (
    <mesh geometry={geom} onClick={(e) => { e.stopPropagation(); onClick?.(b.name) }}>
      <meshStandardMaterial
        color={b.color}
        emissive={highlighted ? '#ff5722' : hasActiveStage ? '#ff9800' : '#000'}
        emissiveIntensity={highlighted ? 0.7 : hasActiveStage ? 0.35 : 0}
        transparent opacity={highlighted ? 0.95 : 0.85}
      />
    </mesh>
  )
}

/** 原体量线框：高度阶段生效后的有效高度与建筑原顶高不同的时候，
    用线框标出加高/降低前的体量，便于对照遮挡变化。 */
function OriginalMassGhost({ b, originalTop, effectiveTop }) {
  const geom = useMemo(() => extrude(b, originalTop),
    [b.footprint, b.base_height, originalTop])
  if (Math.abs(originalTop - effectiveTop) < 1e-9) return null
  return (
    <mesh geometry={geom}>
      <meshBasicMaterial color="#ffd54f" wireframe transparent opacity={0.5} />
    </mesh>
  )
}

function MeasurePoint({ p, status, selected, onClick }) {
  const color = selected ? '#ff4081' : status === 'shaded' ? '#4a6fa5'
    : status === 'sunlit' ? '#f6c453' : '#999'
  return (
    <mesh position={toThree(p.position)}
      onClick={(e) => { e.stopPropagation(); onClick?.(p.id) }}>
      <sphereGeometry args={[0.45, 16, 16]} />
      <meshStandardMaterial color={color} />
    </mesh>
  )
}

/** 真北箭头：模型 +y 轴（= 场景声明的模型北），红色；真北由 north_offset 决定，
    场景页已统一口径，这里画模型北并标注偏角。 */
function NorthArrow() {
  return (
    <group position={[0, 0.2, 0]}>
      <Line points={[toThree([0, 0, 0]), toThree([0, 18, 0])]} color="#d32f2f" lineWidth={3} />
      <Html position={toThree([0, 20, 0])} center>
        <div className="north-label">模型北 ↑</div>
      </Html>
    </group>
  )
}

/** 阶段说明覆盖层：列出所选日期每栋建筑实际采用的高度与阶段。 */
function PhaseLegend({ resolution, date }) {
  if (!resolution) return null
  const changed = resolution.filter(
    (r) => Math.abs(r.effective_top_height - r.original_top_height) > 1e-9)
  return (
    <Html position={toThree([-78, 0.5, 52])} center style={{ pointerEvents: 'none' }}>
      <div className="phase-legend">
        <b>高度阶段（{date}）</b>
        {changed.length === 0
          ? <div className="muted">当日无阶段生效，全部采用建筑原高度</div>
          : changed.map((r) => (
            <div key={r.building_id}>
              {r.building_name}：{r.original_top_height}m →
              <b> {r.effective_top_height}m</b>
              <span className="muted">
                {' '}（{r.active_stage.effective_date} 生效
                {r.active_stage.note ? ` · ${r.active_stage.note}` : ''}）
              </span>
            </div>
          ))}
        <div className="muted small">黄线框 = 加高前原体量</div>
      </div>
    </Html>
  )
}

export default function SceneViewer({
  payload, sunpath, timeIdx, pointStatus, selectedPointId,
  highlightOccluder, onSelectPoint, onSelectBuilding,
}) {
  const sun = sunpath?.points?.[timeIdx]
  const sunPos = sun ? toThree(sun.dir.map((v) => v * SUN_R)) : null
  return (
    <Canvas camera={{ position: [60, 70, 90], up: [0, 1, 0], fov: 45 }}
      style={{ background: '#10141c' }}>
      <ambientLight intensity={0.5} />
      <directionalLight position={sunPos ?? [50, 80, 30]} intensity={1.2} />
      <gridHelper args={[200, 40, '#2a3242', '#1c2330']} rotation={[0, 0, 0]} />
      <NorthArrow />
      {payload?.buildings.map((b) => {
        // 选日期后的运行/预览：实体按当日生效高度；否则回退建筑原顶高
        const effectiveTop = b.effective_top_height ?? b.top_height
        return (
          <group key={b.id}>
            <Building b={b} effectiveTop={effectiveTop}
              hasActiveStage={!!b.active_stage}
              highlighted={highlightOccluder === b.name}
              onClick={onSelectBuilding} />
            <OriginalMassGhost b={b} originalTop={b.top_height}
              effectiveTop={effectiveTop} />
          </group>
        )
      })}
      {payload?.points.map((p) => (
        <MeasurePoint key={p.id} p={p}
          status={pointStatus?.[p.id]?.status}
          selected={p.id === selectedPointId}
          onClick={onSelectPoint} />
      ))}
      <PhaseLegend resolution={payload?.height_resolution}
        date={payload?.height_resolution_date} />
      {/* 太阳路径（当日，模型坐标系） */}
      {sunpath && (
        <Line points={sunpath.points.map((s) => toThree(s.dir.map((v) => v * SUN_R)))}
          color="#f6c453" dashed dashSize={2} gapSize={1} />
      )}
      {sunPos && (
        <mesh position={sunPos}>
          <sphereGeometry args={[3, 16, 16]} />
          <meshBasicMaterial color="#ffeb3b" />
        </mesh>
      )}
      {/* 测点→太阳 射线：绿=晒到，红=被遮挡（遮挡物见右侧面板/点击建筑高亮） */}
      {sun && payload?.points.map((p) => {
        const st = pointStatus?.[p.id]
        if (!st || st.status === 'night') return null
        const o = p.position
        const e = [o[0] + sun.dir[0] * RAY_LEN, o[1] + sun.dir[1] * RAY_LEN,
                   o[2] + sun.dir[2] * RAY_LEN]
        return (
          <Line key={p.id} points={[toThree(o), toThree(e)]}
            color={st.status === 'sunlit' ? '#7ce38b' : '#ef5350'} lineWidth={2} />
        )
      })}
      <OrbitControls makeDefault />
    </Canvas>
  )
}
