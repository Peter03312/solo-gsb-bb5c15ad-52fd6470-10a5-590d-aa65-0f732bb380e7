"""几何核心：GeoJSON 校验、跨 180° 经线处理、点-环关系。

坐标约定：内部平面坐标 (x, y)，x 为可能被展开（unwrap）过的经度，
y 为纬度。经度展开规则：相邻点间沿最短方向（经度差绝对值 <= 180）。

环（ring）的内部表示：首点保持原始经度，其后每个顶点沿较短方向
相对前一点展开，因此跨 180° 经线的环在平面上是一条连续简单环。
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

Point = Tuple[float, float]
Ring = List[Point]

EPS = 1e-9                      # 坐标比较容差（度）
LON_MIN, LON_MAX = -180.0, 180.0
LAT_MIN, LAT_MAX = -90.0, 90.0
MIN_RING_AREA = 1e-12           # 环最小绝对面积（度^2）


class GeometryError(ValueError):
    """几何无效或坐标越界。"""


# --------------------------------------------------------------------------
# 基础数值工具
# --------------------------------------------------------------------------

def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def on_segment(p: Point, a: Point, b: Point, tol: float = EPS) -> bool:
    """p 是否落在线段 ab 上（含端点），tol 为坐标容差。"""
    if abs(orient(a, b, p)) > tol * max(1.0, _seg_len(a, b)):
        return False
    return (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol)


def _seg_len(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def ring_bbox(ring: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def ring_area(ring: Sequence[Point]) -> float:
    s = 0.0
    n = len(ring)
    for i in range(n - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(s) * 0.5


# --------------------------------------------------------------------------
# GeoJSON 环校验与展开
# --------------------------------------------------------------------------

def _check_lonlat(lon: float, lat: float, where: str) -> None:
    if not (_is_number(lon) and _is_number(lat)):
        raise GeometryError(f"{where}：经纬度必须为有限数值")
    lon, lat = float(lon), float(lat)
    if not (LON_MIN <= lon <= LON_MAX):
        raise GeometryError(f"{where}：经度 {lon} 越界（允许 [{LON_MIN}, {LON_MAX}]）")
    if not (LAT_MIN <= lat <= LAT_MAX):
        raise GeometryError(f"{where}：纬度 {lat} 越界（允许 [{LAT_MIN}, {LAT_MAX}]）")


def unwrap_coords(coords: Sequence, label: str) -> Ring:
    """校验一个 GeoJSON 线性环的原始坐标并展开经度。"""
    if not isinstance(coords, list) or len(coords) < 4:
        raise GeometryError(f"{label}：线性环至少需要 4 个点")

    pts: Ring = []
    prev_x = None
    for i, c in enumerate(coords):
        if not isinstance(c, (list, tuple)) or len(c) < 2:
            raise GeometryError(f"{label}：第 {i} 个点格式无效，应为 [lon, lat]")
        lon, lat = c[0], c[1]
        _check_lonlat(lon, lat, f"{label} 第 {i} 点")
        lon, lat = float(lon), float(lat)
        if prev_x is None:
            x = lon
        else:
            d = lon - (prev_x % 360.0)
            # 把 d 调整到 (-180, 180] 的最短方向
            while d > 180.0 + EPS:
                d -= 360.0
            while d < -180.0 - EPS:
                d += 360.0
            # 歧义情形 d 恰为 ±180：固定取 +180
            if d < -180.0:
                d += 360.0
            x = prev_x + d
        pts.append((x, lat))
        prev_x = x

    first, last = pts[0], pts[-1]
    if abs(first[0] - last[0]) > EPS or abs(first[1] - last[1]) > EPS:
        raise GeometryError(f"{label}：环未闭合（首末点必须相同）")
    # 统一闭合点（消除展开后首末经度差异）
    pts[-1] = (first[0], first[1])

    # 连续重复点
    for i in range(len(pts) - 1):
        if _seg_len(pts[i], pts[i + 1]) <= EPS:
            raise GeometryError(f"{label}：存在连续重复点（第 {i}、{i + 1} 点）")

    if ring_area(pts) < MIN_RING_AREA:
        raise GeometryError(f"{label}：环面积为零或过小，几何无效")

    _check_ring_self_intersection(pts, label)
    return pts


def _edge_pair_detail(p1: Point, p2: Point, p3: Point, p4: Point):
    """返回两条线段关系：

    ("cross", None)        严格穿越
    ("overlap", (u0,u1))   共线且正长度重叠（u 为 p1p2 上的参数区间）
    ("touch", [points...]) 仅在端点/T 接处相触
    ("none", None)
    """
    o1 = orient(p3, p4, p1)
    o2 = orient(p3, p4, p2)
    o3 = orient(p1, p2, p3)
    o4 = orient(p1, p2, p4)
    cross_tol = EPS
    s12 = max(1.0, _seg_len(p1, p2))
    s34 = max(1.0, _seg_len(p3, p4))

    if ((o1 > cross_tol * s34 and o2 < -cross_tol * s34)
            or (o1 < -cross_tol * s34 and o2 > cross_tol * s34)) \
            and ((o3 > cross_tol * s12 and o4 < -cross_tol * s12)
                 or (o3 < -cross_tol * s12 and o4 > cross_tol * s12)):
        return "cross", None

    # 共线情形
    if abs(o1) <= EPS * s34 and abs(o2) <= EPS * s34 and abs(o3) <= EPS * s12:
        ux, uy = p2[0] - p1[0], p2[1] - p1[1]
        denom = ux * ux + uy * uy
        if denom > EPS:
            us = []
            for q in (p3, p4):
                us.append(((q[0] - p1[0]) * ux + (q[1] - p1[1]) * uy) / denom)
            lo = max(0.0, min(us))
            hi = min(1.0, max(us))
            if hi - lo > EPS:
                return "overlap", (lo, hi)

    touches: List[Point] = []
    for q, o, a, b in ((p1, o1, p3, p4), (p2, o2, p3, p4),
                       (p3, o3, p1, p2), (p4, o4, p1, p2)):
        if abs(o) <= EPS * max(1.0, _seg_len(a, b)) and on_segment(q, a, b):
            if not any(abs(q[0] - t[0]) <= EPS and abs(q[1] - t[1]) <= EPS for t in touches):
                touches.append(q)
    if touches:
        return "touch", touches
    return "none", None


def _check_ring_self_intersection(ring: Ring, label: str) -> None:
    n = len(ring) - 1  # 闭合点重复，边数 = 点数 - 1
    for i in range(n):
        a1, a2 = ring[i], ring[i + 1]
        for j in range(i + 1, n):
            b1, b2 = ring[j], ring[j + 1]
            adjacent = (j == i + 1) or (i == 0 and j == n - 1)
            kind, data = _edge_pair_detail(a1, a2, b1, b2)
            if kind == "cross" or kind == "overlap":
                raise GeometryError(f"{label}：环自相交（边 {i} 与边 {j}）")
            if kind == "touch":
                if adjacent:
                    # 仅允许在两条边的共享顶点处接触
                    shared = ring[i + 1] if j == i + 1 else ring[0]
                    ok = all(abs(t[0] - shared[0]) <= EPS and abs(t[1] - shared[1]) <= EPS
                             for t in data)
                    if not ok:
                        raise GeometryError(f"{label}：环在相邻边的非共享顶点处接触（边 {i} 与边 {j}）")
                else:
                    raise GeometryError(f"{label}：环自接触（边 {i} 与边 {j}）")


# --------------------------------------------------------------------------
# 点-环查询
# --------------------------------------------------------------------------

def point_on_ring_boundary(p: Point, ring: Sequence[Point]) -> bool:
    n = len(ring) - 1
    for i in range(n):
        if on_segment(p, ring[i], ring[i + 1]):
            return True
    return False


def point_strict_inside(p: Point, ring: Sequence[Point]) -> bool:
    """射线法；边界点返回 False（调用前通常已单独判定边界）。"""
    x, y = p
    inside = False
    n = len(ring) - 1
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[i + 1]
        if (ay > y) != (by > y):
            x_cross = ax + (y - ay) * (bx - ax) / (by - ay)
            if x < x_cross - EPS:
                inside = not inside
    return inside


def point_covers_ring(p: Point, ring: Sequence[Point]) -> bool:
    """环内部或边界。"""
    if point_on_ring_boundary(p, ring):
        return True
    return point_strict_inside(p, ring)


# --------------------------------------------------------------------------
# 环与环关系（考虑 360° 经度平移副本）
# --------------------------------------------------------------------------

def shifted(ring: Sequence[Point], k: int) -> Ring:
    return [(p[0] + 360.0 * k, p[1]) for p in ring]


def _shift_range_overlapping(ring_a: Sequence[Point], ring_b: Sequence[Point],
                             pad: float = 1e-6) -> List[int]:
    """枚举使 ring_b 平移 360k 后 bbox 与 ring_a 相交的 k。"""
    aminx, miny_a, amaxx, maxy_a = ring_bbox(ring_a)
    bminx, miny_b, bmaxx, maxy_b = ring_bbox(ring_b)
    if miny_b > maxy_a + pad or miny_a > maxy_b + pad:
        return []
    k_lo = math.ceil((aminx - pad - bmaxx) / 360.0 - 1e-12)
    k_hi = math.floor((amaxx + pad - bminx) / 360.0 + 1e-12)
    return list(range(k_lo, k_hi + 1))


class RingRelation:
    def __init__(self):
        self.cross = False        # 严格穿越
        self.overlap = False      # 共线正长度重叠
        self.touch = False        # 仅点接触
        self.a_contains_b = False  # B 的内部点在 A 内（含边界）
        self.b_contains_a = False


def ring_relation(ring_a: Ring, ring_b: Ring) -> RingRelation:
    """在两环无交叉/共线重叠的前提下判定包含关系。

    包含关系用“对方顶点是否严格落在本环内部”判定（边界上的相切点
    不算）：存在 B 的顶点严格在 A 内即 B ⊆ A，反之亦然；两环相离时
    均为 False，完全重合时也均为 False（重合环本身按无效处理）。
    """
    rel = RingRelation()
    for k in _shift_range_overlapping(ring_a, ring_b):
        bk = shifted(ring_b, k)
        na = len(ring_a) - 1
        nb = len(bk) - 1
        for i in range(na):
            a1, a2 = ring_a[i], ring_a[i + 1]
            for j in range(nb):
                b1, b2 = bk[j], bk[j + 1]
                kind, _ = _edge_pair_detail(a1, a2, b1, b2)
                if kind == "cross":
                    rel.cross = True
                elif kind == "overlap":
                    rel.overlap = True
                elif kind == "touch":
                    rel.touch = True
        # B 的顶点严格在 A 内 => B ⊆ A；A 的顶点严格在平移后的 B 内 => A ⊆ B
        for j in range(nb):
            v = bk[j]
            if not point_on_ring_boundary(v, ring_a) and point_strict_inside(v, ring_a):
                rel.a_contains_b = True
                break
        for i in range(na):
            v = ring_a[i]
            if not point_on_ring_boundary(v, bk) and point_strict_inside(v, bk):
                rel.b_contains_a = True
                break
    return rel


# --------------------------------------------------------------------------
# GeoJSON Polygon / MultiPolygon 校验入口
# --------------------------------------------------------------------------

PolygonData = dict  # {"outer": Ring, "holes": List[Ring]}


def parse_geojson_geometry(geometry) -> Tuple[str, List[PolygonData]]:
    """校验 GeoJSON Polygon/MultiPolygon，返回展开后的平面表示。"""
    if not isinstance(geometry, dict):
        raise GeometryError("geometry 必须为 GeoJSON 对象")
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Polygon":
        if not isinstance(coords, list) or len(coords) < 1:
            raise GeometryError("Polygon coordinates 必须为非空数组（外环 + 可选洞）")
        polygons = [_build_polygon(coords, "Polygon")]
    elif gtype == "MultiPolygon":
        if not isinstance(coords, list) or len(coords) < 1:
            raise GeometryError("MultiPolygon coordinates 必须为非空数组")
        polygons = []
        for pi, poly_coords in enumerate(coords):
            if not isinstance(poly_coords, list) or len(poly_coords) < 1:
                raise GeometryError(f"MultiPolygon 第 {pi} 个多边形缺少外环")
            polygons.append(_build_polygon(poly_coords, f"MultiPolygon[{pi}]"))
    else:
        raise GeometryError(f"不支持的几何类型 {gtype!r}，仅允许 Polygon / MultiPolygon")

    _validate_polygon_topology(polygons)
    return gtype, polygons


def _build_polygon(coords: Sequence, label: str) -> PolygonData:
    outer = unwrap_coords(coords[0], f"{label} 外环")
    holes = []
    for hi, hc in enumerate(coords[1:]):
        holes.append(unwrap_coords(hc, f"{label} 洞[{hi}]"))
    return {"outer": outer, "holes": holes}


def _reject_bad_relation(rel: RingRelation, what_a: str, what_b: str,
                         forbid_a_contains_b: bool, forbid_b_contains_a: bool) -> None:
    if rel.cross:
        raise GeometryError(f"{what_a} 与 {what_b} 边界交叉，几何无效")
    if rel.overlap:
        raise GeometryError(f"{what_a} 与 {what_b} 边界共线重叠，几何无效")
    if forbid_a_contains_b and rel.a_contains_b:
        raise GeometryError(f"{what_b} 落入 {what_a} 内部，几何无效")
    if forbid_b_contains_a and rel.b_contains_a:
        raise GeometryError(f"{what_a} 落入 {what_b} 内部，几何无效")


def _validate_polygon_topology(polygons: List[PolygonData]) -> None:
    # 1) 每个洞必须位于其外环内部（允许边界相切）；洞洞不允许嵌套
    for pi, poly in enumerate(polygons):
        outer = poly["outer"]
        for hi, hole in enumerate(poly["holes"]):
            rel = ring_relation(outer, hole)
            # A=外环，B=洞：要求 B 在 A 内，禁止 A 在 B 内
            _reject_bad_relation(rel, f"多边形{pi}外环", f"洞[{hi}]",
                                 forbid_a_contains_b=False,
                                 forbid_b_contains_a=True)
            if not rel.a_contains_b:
                raise GeometryError(f"多边形{pi} 的洞[{hi}] 不在外环内部")
        for h1 in range(len(poly["holes"])):
            for h2 in range(h1 + 1, len(poly["holes"])):
                rel = ring_relation(poly["holes"][h1], poly["holes"][h2])
                # A=洞1, B=洞2：任意嵌套都不允许
                _reject_bad_relation(rel, f"多边形{pi}洞[{h1}]", f"洞[{h2}]",
                                     forbid_a_contains_b=True,
                                     forbid_b_contains_a=True)

    # 2) MultiPolygon：多边形之间不得重叠/交叉（允许相切）；
    #    洞不得越界落入其它多边形外环。
    for i in range(len(polygons)):
        for j in range(i + 1, len(polygons)):
            rel = ring_relation(polygons[i]["outer"], polygons[j]["outer"])
            _reject_bad_relation(rel, f"多边形{i}外环", f"多边形{j}外环",
                                 forbid_a_contains_b=True,
                                 forbid_b_contains_a=True)
            for hi, hole in enumerate(polygons[j]["holes"]):
                rh = ring_relation(polygons[i]["outer"], hole)
                if rh.cross or rh.overlap or rh.a_contains_b:
                    raise GeometryError(
                        f"多边形{j} 的洞[{hi}] 越界进入多边形{i}，几何无效")
            for hi, hole in enumerate(polygons[i]["holes"]):
                rh = ring_relation(polygons[j]["outer"], hole)
                if rh.cross or rh.overlap or rh.b_contains_a:
                    raise GeometryError(
                        f"多边形{i} 的洞[{hi}] 越界进入多边形{j}，几何无效")
