"""建筑高度阶段（height stages）解析与校验。

每栋建筑可维护若干「生效日期 + 顶高」记录，用于模拟塔楼按场景本地日期
加高/改建后的遮挡变化：

- 生效日期在同一建筑内必须唯一；
- 顶高必须是有效数值（有限、> 0），且不得低于建筑 base_height；
- 分析日期 d 取「effective_date <= d」中的最新阶段（日期相等即当日生效）；
- 早于首个阶段的日期没有任何阶段命中，继续采用建筑原 top_height。

阶段配置与解析结果都会写入运行快照 payload：结果永远关联运行时实际采用
的体量，阶段后续被修改/删除不影响旧运行追溯。
"""
from __future__ import annotations

import math
from datetime import date, datetime


class StageValidationError(ValueError):
    """阶段输入非法（重复日期 / 非法高度等）。"""


def parse_date(s) -> date:
    """YYYY-MM-DD → date（输入必须是 10 位严格格式）。"""
    if not isinstance(s, str):
        raise StageValidationError("日期必须是 YYYY-MM-DD 字符串")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise StageValidationError(f"日期格式非法：{s!r}（应为 YYYY-MM-DD）")


def validate_stage_height(value, base_height: float = 0.0) -> float:
    """阶段顶高校验：必须为有限数、> 0 且 >= base_height。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StageValidationError("高度必须是数字")
    v = float(value)
    if not math.isfinite(v):
        raise StageValidationError("高度必须是有限数值")
    if v <= 0:
        raise StageValidationError(f"高度必须为正数，收到 {v}")
    if v < float(base_height):
        raise StageValidationError(
            f"顶高 {v} 低于建筑底高 {base_height}，体量无效")
    return v


def stage_dicts(stages) -> list[dict]:
    """归一化阶段行：接受 ORM 对象或纯 dict，按生效日期升序输出。"""
    out = []
    for st in stages or []:
        if isinstance(st, dict):
            d, h, note = st.get("effective_date"), st.get("top_height"), \
                st.get("note", "")
        else:
            d, h, note = st.effective_date, st.top_height, getattr(st, "note", "")
        if isinstance(d, str):
            d = parse_date(d)
        if not isinstance(d, date):
            raise StageValidationError("阶段缺少合法生效日期")
        out.append({"effective_date": d, "top_height": float(h),
                    "note": note or ""})
    return sorted(out, key=lambda x: x["effective_date"])


def validate_stages(stages, base_height: float = 0.0) -> list[dict]:
    """整体校验一组阶段：日期唯一、高度有效；按日期升序返回。"""
    norm = stage_dicts(stages)
    seen = set()
    for st in norm:
        if st["effective_date"] in seen:
            raise StageValidationError(
                f"生效日期重复：{st['effective_date'].isoformat()}")
        seen.add(st["effective_date"])
        validate_stage_height(st["top_height"], base_height)
    return norm


def resolve_height(original_top_height: float, stages, on_date) -> dict:
    """选取 on_date 已生效的最新阶段。

    返回 {effective_top_height, active_stage}；早于首个阶段时
    active_stage=None 且 effective_top_height 为建筑原顶高。
    stages 可以是 ORM 行列表或 validate_stages() 的输出（dict）。
    """
    if isinstance(on_date, str):
        on_date = parse_date(on_date)
    active = None
    for st in stage_dicts(stages):  # 已按日期升序
        if st["effective_date"] <= on_date:
            active = st
        else:
            break
    if active is None:
        return {"effective_top_height": float(original_top_height),
                "active_stage": None}
    return {"effective_top_height": float(active["top_height"]),
            "active_stage": {
                "effective_date": active["effective_date"].isoformat(),
                "top_height": float(active["top_height"]),
                "note": active.get("note", "")}}
