"""请求分析编排：测线插值、逐线段求交、时间区间合并与生效区间裁切。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .geopoly import EPS, Poly, segment_closed_intervals, union_closed, wrap_delta


class AnalysisError(ValueError):
    """请求语义错误（坐标越界、时间不递增等），映射为 400。"""


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def parse_utc(value, label: str) -> datetime:
    """解析 ISO 8601 时间字符串；无时区的朴素时间按 UTC 处理。"""
    if not isinstance(value, str):
        raise AnalysisError(f"{label} 必须是 ISO 8601 时间字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AnalysisError(f"{label} 无法解析为时间: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    """格式化为带 Z 的 UTC ISO 8601（秒级，不足秒带小数）。"""
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 区间工具（闭区间 -> 左闭右开时间区间）
# ---------------------------------------------------------------------------

def to_half_open(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """丢弃零长/点区间，其余视为 [a, b)。"""
    return [(a, b) for a, b in intervals if b - a > EPS]


def clip_half_open(
    intervals: list[tuple[float, float]], lo: float, hi: float
) -> list[tuple[float, float]]:
    """左闭右开区间与 [lo, hi) 裁切。"""
    out: list[tuple[float, float]] = []
    for a, b in intervals:
        c, d = max(a, lo), min(b, hi)
        if d - c > EPS:
            out.append((c, d))
    return out


def merge_half_open(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并相交或端点相接（[a,b) 与 [b,c) 相接）的左闭右开区间。"""
    if not intervals:
        return []
    items = sorted(intervals)
    merged: list[tuple[float, float]] = []
    ca, cb = items[0]
    for a, b in items[1:]:
        if a <= cb + EPS:
            if b > cb:
                cb = b
        else:
            merged.append((ca, cb))
            ca, cb = a, b
    merged.append((ca, cb))
    return merged


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------

def _validate_effective(valid_from: datetime, valid_to: datetime | None, label: str) -> float | None:
    if valid_to is not None and valid_to <= valid_from:
        raise AnalysisError(f"{label} 的 valid_to 必须晚于 valid_from")
    return None


def analyze(areas, points) -> list[dict]:
    """执行完整分析。

    areas: 已由 Pydantic 解析的 ProtectedArea 列表
    points: 已解析的 TrackPoint 列表
    """
    if len(points) < 2:
        raise AnalysisError("测线至少需要 2 个时间点（单条线无法定义穿越）")

    times: list[datetime] = []
    coords: list[tuple[float, float]] = []
    for i, pt in enumerate(points):
        times.append(parse_utc(pt.time, f"track[{i}].time"))
        coords.append((pt.lon, pt.lat))

    for i in range(len(times) - 1):
        if times[i + 1] <= times[i]:
            raise AnalysisError(
                f"track[{i + 1}].time 必须严格晚于 track[{i}].time（时间必须严格递增）"
            )

    # 预解析保护区几何与生效区间
    from .geopoly import parse_geojson

    area_specs: list[tuple[object, list[Poly], datetime, datetime | None]] = []
    seen_ids: set[str] = set()
    for idx, area in enumerate(areas):
        aid = area.id
        if aid in seen_ids:
            raise AnalysisError(f"areas[{idx}].id 重复: {aid!r}")
        seen_ids.add(aid)
        vf = parse_utc(area.valid_from, f"areas[{idx}].valid_from")
        vt = parse_utc(area.valid_to, f"areas[{idx}].valid_to") if area.valid_to else None
        _validate_effective(vf, vt, f"areas[{idx}]")
        polys = parse_geojson(area.geometry, label=f"areas[{idx}]")
        area_specs.append((aid, polys, vf, vt))

    t0 = times[0]
    tN = times[-1]
    t_rel = [(t - t0).total_seconds() for t in times]

    # 为每个保护区累积左闭右开的相对秒区间
    area_intervals: dict[int, list[tuple[float, float]]] = {i: [] for i in range(len(area_specs))}

    for seg in range(len(times) - 1):
        lon1, lat1 = coords[seg]
        lon2, lat2 = coords[seg + 1]
        lon2_unwrapped = lon1 + wrap_delta(lon2 - lon1)
        p1 = (lon1, lat1)
        p2 = (lon2_unwrapped, lat2)
        seg_t0, seg_t1 = t_rel[seg], t_rel[seg + 1]

        for ai, (_, polys, _, _) in enumerate(area_specs):
            closed = segment_closed_intervals(p1, p2, polys)
            closed = union_closed(closed)
            for s0, s1 in to_half_open(closed):
                area_intervals[ai].append((seg_t0 + s0 * (seg_t1 - seg_t0),
                                           seg_t0 + s1 * (seg_t1 - seg_t0)))

    results: list[dict] = []
    for ai, (aid, _, vf, vt) in enumerate(area_specs):
        intervals = merge_half_open(area_intervals[ai])

        lo = max(0.0, (vf - t0).total_seconds())
        hi = None if vt is None else (vt - t0).total_seconds()
        if hi is not None:
            intervals = clip_half_open(intervals, lo, hi)
        else:
            intervals = clip_half_open(intervals, lo, float("inf"))
        intervals = merge_half_open(intervals)

        entries = []
        total = 0.0
        for a, b in intervals:
            total += b - a
            entries.append({
                "enter": format_utc(t0 + timedelta(seconds=a)),
                "leave": format_utc(t0 + timedelta(seconds=b)),
                "duration_seconds": round(b - a, 6),
            })

        results.append({
            "id": aid,
            "inside": bool(intervals),
            "total_duration_seconds": round(total, 6),
            "entries": entries,
        })

    return results
