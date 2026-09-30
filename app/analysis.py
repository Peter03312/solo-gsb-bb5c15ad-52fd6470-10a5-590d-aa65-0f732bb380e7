"""穿越分析：时间/轨迹校验 + 保护区内停留区间计算。

时间区间在内部一律表示为 UTC epoch 秒上的闭区间 [a, b]（b > a），
输出时按左闭右开语义解释为 [entry, leave)。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .geometry import (
    EPS,
    GeometryError,
    Point,
    Ring,
    parse_geojson_geometry,
    point_on_ring_boundary,
    point_strict_inside,
    ring_bbox,
    shifted,
)

TIME_EPS = 1e-7              # 时长下限（秒），低于此视为单点接触
F_EPS = 1e-10                # 分段参数去重容差


class RequestError(ValueError):
    """请求数据无效（错误信息可直接返回给调用方）。"""


# --------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------

def parse_iso_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RequestError(f"{field}：必须为 ISO 8601 时间字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise RequestError(f"{field}：无法解析的时间 {value!r}")
    if dt.tzinfo is None:
        raise RequestError(f"{field}：时间必须带时区（如 Z 或 +00:00）")
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        frac = f"{dt.microsecond:06d}".rstrip("0")
        if len(frac) < 3:
            frac = frac.ljust(3, "0")
        return f"{base}.{frac}Z"
    return base + "Z"


# --------------------------------------------------------------------------
# 轨迹校验与展开
# --------------------------------------------------------------------------

class TrackPoint:
    __slots__ = ("x", "lat", "t", "lon0")

    def __init__(self, x: float, lat: float, t: float, lon0: float):
        self.x = x          # 沿较短方向展开后的经度（度）
        self.lat = lat
        self.t = t          # UTC epoch 秒
        self.lon0 = lon0    # 原始经度（回显用）


_LON_KEYS = ("longitude", "lon", "lng", "long")
_LAT_KEYS = ("latitude", "lat")
_TIME_KEYS = ("time", "timestamp", "t", "utc")


def _pick(p: dict, keys: tuple, label: str, idx: int):
    for k in keys:
        if k in p:
            return p[k]
    raise RequestError(f"测线点[{idx}]：缺少 {label} 字段（接受 {('/'.join(keys))}）")


def _finite_num(v, label: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RequestError(f"{label}：必须为数值")
    f = float(v)
    if not math.isfinite(f):
        raise RequestError(f"{label}：必须为有限数值")
    return f


def validate_track(points: Any) -> List[TrackPoint]:
    if not isinstance(points, list):
        raise RequestError("track_points：必须为数组")
    if len(points) == 0:
        raise RequestError("track_points：至少需要 2 个按时间排列的点（当前 0 个）")
    if len(points) < 2:
        raise RequestError("track_points：单个点无法构成测线，至少需要 2 个点")

    result: List[TrackPoint] = []
    prev_x = None
    prev_t: Optional[float] = None
    for i, p in enumerate(points):
        if not isinstance(p, dict):
            raise RequestError(f"测线点[{i}]：必须为对象")
        lon = _finite_num(_pick(p, _LON_KEYS, "经度", i), f"测线点[{i}]经度")
        lat = _finite_num(_pick(p, _LAT_KEYS, "纬度", i), f"测线点[{i}]纬度")
        if not (-180.0 <= lon <= 180.0):
            raise RequestError(f"测线点[{i}]：经度 {lon} 越界（允许 [-180, 180]）")
        if not (-90.0 <= lat <= 90.0):
            raise RequestError(f"测线点[{i}]：纬度 {lat} 越界（允许 [-90, 90]）")
        dt = parse_iso_time(_pick(p, _TIME_KEYS, "时间", i), f"测线点[{i}]时间")
        t = dt.timestamp()
        if prev_t is not None and not (t > prev_t):
            raise RequestError(
                f"测线点[{i}]：时间必须严格递增（{format_utc(datetime.fromtimestamp(t, timezone.utc))} "
                f"未晚于前一点）")

        if prev_x is None:
            x = lon
        else:
            d = lon - (prev_x % 360.0)
            while d > 180.0 + EPS:
                d -= 360.0
            while d < -180.0 - EPS:
                d += 360.0
            if d < -180.0:
                d += 360.0
            x = prev_x + d
        result.append(TrackPoint(x, lat, t, lon))
        prev_x, prev_t = x, t
    return result


# --------------------------------------------------------------------------
# 保护区
# --------------------------------------------------------------------------

class Area:
    def __init__(self, area_id: str, valid_from: float, valid_to: float,
                 geometry_type: str, polygons: list):
        self.id = area_id
        self.valid_from = valid_from
        self.valid_to = valid_to
        self.geometry_type = geometry_type
        self.polygons = polygons


def validate_areas(areas: Any) -> List[Area]:
    if not isinstance(areas, list) or len(areas) == 0:
        raise RequestError("protected_areas：必须为非空数组")
    seen = set()
    result: List[Area] = []
    for i, a in enumerate(areas):
        if not isinstance(a, dict):
            raise RequestError(f"保护区[{i}]：必须为对象")
        aid = a.get("id")
        if aid is None or (isinstance(aid, str) and not aid.strip()):
            raise RequestError(f"保护区[{i}]：缺少非空 id")
        aid = str(aid)
        if aid in seen:
            raise RequestError(f"保护区 id {aid!r} 重复")
        seen.add(aid)

        vf = parse_iso_time(a.get("valid_from"), f"保护区 {aid} valid_from").timestamp()
        vt = parse_iso_time(a.get("valid_to"), f"保护区 {aid} valid_to").timestamp()
        if not (vt > vf):
            raise RequestError(f"保护区 {aid}：valid_to 必须晚于 valid_from")

        geometry = a.get("geometry")
        try:
            gtype, polygons = parse_geojson_geometry(geometry)
        except GeometryError as exc:
            raise RequestError(f"保护区 {aid}：{exc}")
        result.append(Area(aid, vf, vt, gtype, polygons))
    return result


# --------------------------------------------------------------------------
# 点与保护区（含 360° 平移副本）
# --------------------------------------------------------------------------

def _point_shift_range(ring: Ring, p: Point) -> List[int]:
    minx, miny, maxx, maxy = ring_bbox(ring)
    x, y = p
    if y < miny - EPS or y > maxy + EPS:
        return []
    k_lo = math.ceil((x - EPS - maxx) / 360.0 - 1e-12)
    k_hi = math.floor((x + EPS - minx) / 360.0 + 1e-12)
    return list(range(k_lo, k_hi + 1))


def _segment_shift_range(ring: Ring, x0: float, y0: float,
                         x1: float, y1: float) -> List[int]:
    minx, miny, maxx, maxy = ring_bbox(ring)
    sx0, sx1 = min(x0, x1), max(x0, x1)
    sy0, sy1 = min(y0, y1), max(y0, y1)
    if sy1 < miny - EPS or sy0 > maxy + EPS:
        return []
    k_lo = math.ceil((sx0 - EPS - maxx) / 360.0 - 1e-12)
    k_hi = math.floor((sx1 + EPS - minx) / 360.0 + 1e-12)
    return list(range(k_lo, k_hi + 1))


def point_protected(p: Point, polygons: list) -> bool:
    """外环边界/内部计入；洞严格内部不计入，洞边界计入。"""
    x, y = p
    for poly in polygons:
        outer = poly["outer"]
        inside_outer = False
        for k in _point_shift_range(outer, p):
            rk = shifted(outer, k)
            if point_on_ring_boundary(p, rk):
                return True
            if point_strict_inside(p, rk):
                inside_outer = True
        if not inside_outer:
            continue
        in_hole = False
        for hole in poly["holes"]:
            for k in _point_shift_range(hole, p):
                hk = shifted(hole, k)
                if point_on_ring_boundary(p, hk):
                    return True  # 洞边界计入保护区
                if point_strict_inside(p, hk):
                    in_hole = True
        if not in_hole:
            return True
    return False


# --------------------------------------------------------------------------
# 单段测线：求参数 u ∈ [0,1] 上处于保护区的闭区间并集
# --------------------------------------------------------------------------

def _intersect_with_edge(p0: Point, p1: Point, q0: Point, q1: Point,
                         events: List[float], overlaps: List[Tuple[float, float]]):
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    ex, ey = q1[0] - q0[0], q1[1] - q0[1]
    den = dx * ey - dy * ex
    c0 = dx * (q0[1] - p0[1]) - dy * (q0[0] - p0[0])  # orient(p0,p1,q0)
    slen = math.hypot(dx, dy)
    elen = math.hypot(ex, ey)
    tol = EPS * max(1.0, slen, elen)

    if abs(den) > tol:
        # 常规相交：u = orient(p0,q0,q1) / den
        num = (q0[0] - p0[0]) * ey - (q0[1] - p0[1]) * ex
        u = num / den
        if -F_EPS <= u <= 1.0 + F_EPS:
            ix = p0[0] + u * dx
            iy = p0[1] + u * dy
            # 确认交点确实落在边与线段上
            if (min(q0[0], q1[0]) - EPS <= ix <= max(q0[0], q1[0]) + EPS
                    and min(q0[1], q1[1]) - EPS <= iy <= max(q0[1], q1[1]) + EPS):
                events.append(min(1.0, max(0.0, u)))
        return

    if abs(c0) <= tol:
        # 共线：投影端点到段参数
        d2 = dx * dx + dy * dy
        if d2 <= EPS:
            return
        u0 = ((q0[0] - p0[0]) * dx + (q0[1] - p0[1]) * dy) / d2
        u1 = ((q1[0] - p0[0]) * dx + (q1[1] - p0[1]) * dy) / d2
        lo = max(0.0, min(u0, u1))
        hi = min(1.0, max(u0, u1))
        if hi - lo > F_EPS:
            overlaps.append((lo, hi))


def _dedup_sorted(values: List[float]) -> List[float]:
    values.sort()
    out = []
    for v in values:
        if not out or abs(v - out[-1]) > F_EPS:
            out.append(v)
    return out


def _merge_closed(intervals: List[Tuple[float, float]],
                  tol: float = F_EPS) -> List[Tuple[float, float]]:
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for a, b in intervals[1:]:
        if a <= merged[-1][1] + tol:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def segment_protected_intervals(p0: TrackPoint, p1: TrackPoint,
                                polygons: list) -> List[Tuple[float, float]]:
    # 空间静止段：位置恒定，只做一次点判定
    if abs(p1.x - p0.x) <= EPS and abs(p1.lat - p0.lat) <= EPS:
        return [(0.0, 1.0)] if point_protected((p0.x, p0.lat), polygons) else []

    A: Point = (p0.x, p0.lat)
    B: Point = (p1.x, p1.lat)
    events: List[float] = [0.0, 1.0]
    overlaps: List[Tuple[float, float]] = []

    for poly in polygons:
        for ring in [poly["outer"], *poly["holes"]]:
            for k in _segment_shift_range(ring, A[0], A[1], B[0], B[1]):
                rk = shifted(ring, k)
                n = len(rk) - 1
                for i in range(n):
                    _intersect_with_edge(A, B, rk[i], rk[i + 1], events, overlaps)

    grid = _dedup_sorted(events)
    intervals: List[Tuple[float, float]] = list(overlaps)
    for i in range(len(grid) - 1):
        u0, u1 = grid[i], grid[i + 1]
        if u1 - u0 <= F_EPS:
            continue
        um = (u0 + u1) * 0.5
        mpt = (A[0] + (B[0] - A[0]) * um, A[1] + (B[1] - A[1]) * um)
        if point_protected(mpt, polygons):
            intervals.append((u0, u1))

    # 段端点本身落在边界上（单点，不产生时长，但可能与相邻片段相接）
    if point_protected(A, polygons):
        intervals.append((0.0, 0.0))
    if point_protected(B, polygons):
        intervals.append((1.0, 1.0))

    return _merge_closed(intervals)


# --------------------------------------------------------------------------
# 整条测线 × 单个保护区
# --------------------------------------------------------------------------

def _merge_time_intervals(intervals: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    return _merge_closed(intervals, tol=TIME_EPS)


def crossing_intervals(track: List[TrackPoint], area: Area) -> List[Tuple[float, float]]:
    raw: List[Tuple[float, float]] = []
    for i in range(len(track) - 1):
        a, b = track[i], track[i + 1]
        for u0, u1 in segment_protected_intervals(a, b, area.polygons):
            if u1 - u0 > F_EPS:
                raw.append((a.t + u0 * (b.t - a.t), a.t + u1 * (b.t - a.t)))
            else:
                raw.append((a.t + u0 * (b.t - a.t), a.t + u0 * (b.t - a.t)))
    merged = _merge_time_intervals(raw)

    # 按左闭右开 [valid_from, valid_to) 裁切
    vs, ve = area.valid_from, area.valid_to
    clipped: List[Tuple[float, float]] = []
    for t0, t1 in merged:
        c0 = max(t0, vs)
        c1 = min(t1, ve)
        if c1 - c0 > TIME_EPS:
            clipped.append((c0, c1))
    return _merge_time_intervals(clipped)


# --------------------------------------------------------------------------
# 顶层分析
# --------------------------------------------------------------------------

def analyze_request(payload: dict) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestError("请求体必须为 JSON 对象")
    areas = validate_areas(payload.get("protected_areas"))
    track = validate_track(payload.get("track_points"))

    results = []
    for area in areas:
        intervals = crossing_intervals(track, area)
        visits = []
        for t0, t1 in intervals:
            visits.append({
                "entry_time": format_utc(datetime.fromtimestamp(t0, timezone.utc)),
                "leave_time": format_utc(datetime.fromtimestamp(t1, timezone.utc)),
                "dwell_seconds": round(t1 - t0, 6),
            })
        # 各段先四舍五入到微秒再求和，避免二进制浮点尾数
        total = round(sum(v["dwell_seconds"] for v in visits), 6)
        results.append({
            "area_id": area.id,
            "effective_from": format_utc(datetime.fromtimestamp(area.valid_from, timezone.utc)),
            "effective_to": format_utc(datetime.fromtimestamp(area.valid_to, timezone.utc)),
            "visits": visits,
            "total_dwell_seconds": total,
        })
    return {"track_point_count": len(track), "results": results}
