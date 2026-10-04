"""建筑高度阶段：校验与按日期解析（纯逻辑，不依赖 ORM，便于单测）。

规则（全项目唯一口径）：
- 每条阶段 = (生效日期 effective_date, 顶高 top_height)，自该本地日期
  （含当日）起生效；
- 同一建筑生效日期必须唯一；顶高必须是有限数值且大于建筑基座高度；
- 解析时取 on_date 已生效（effective_date <= on_date）的最新阶段；
  早于首个阶段的日期返回 None（调用方回落到建筑原高度）。
"""
from __future__ import annotations

import math
from datetime import date, datetime


def as_date(value) -> date:
    """接受 date 或 'YYYY-MM-DD' 字符串，统一为 date。"""
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def validate_height_stages(entries: list[tuple[date, float]],
                           base_height: float) -> None:
    """校验一组 (生效日期, 顶高)；非法时抛 ValueError（API 层转 400）。

    - 生效日期不得重复；
    - 顶高必须为有限数值且大于基座高度（否则拉伸体几何退化）。
    """
    seen: set[date] = set()
    for eff, height in entries:
        eff = as_date(eff)
        if eff in seen:
            raise ValueError(f"生效日期重复：{eff.isoformat()}")
        seen.add(eff)
        if not isinstance(height, (int, float)) or isinstance(height, bool) \
                or not math.isfinite(height):
            raise ValueError(f"阶段 {eff.isoformat()} 的高度须为有限数值")
        if height <= base_height:
            raise ValueError(
                f"阶段 {eff.isoformat()} 的高度 {height} 须大于基座高度 "
                f"{base_height}")


def resolve_height_stage(stages, on_date) -> object | None:
    """返回 on_date 已生效的最新阶段对象（无则 None）。

    stages 为任意带 effective_date / top_height 属性的对象序列
    （ORM 行或简单命名空间均可）；effective_date 可为 date 或字符串。
    """
    target = as_date(on_date)
    effective = [s for s in (stages or []) if as_date(s.effective_date) <= target]
    if not effective:
        return None
    return max(effective, key=lambda s: as_date(s.effective_date))


def resolve_top_height(base_top_height: float, stages, on_date,
                       ) -> tuple[float, object | None]:
    """返回 (实际采用顶高, 采用阶段 or None)。早于首个阶段用原高度。"""
    stage = resolve_height_stage(stages, on_date)
    if stage is None:
        return base_top_height, None
    return float(stage.top_height), stage
