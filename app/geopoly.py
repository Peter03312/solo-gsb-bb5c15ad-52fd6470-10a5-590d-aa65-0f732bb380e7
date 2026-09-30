"""精确几何计算：GeoJSON 解析校验 + 线段/多边形求交。

坐标模型
--------
* 所有几何坐标均为经纬度，经度范围 [-180, 180]、纬度范围 [-90, 90]。
* 线段（测线相邻点）纬度按参数 s 线性插值；经度沿较短方向
  （增量规整到 (-180, 180]）插值，因此线段经度可以超出 [-180, 180]
  （穿越日界线时）。
* 为处理日界线，每个多边形环整体平移 360*k（k ∈ {-1, 0, 1}）生成
  三个“坐标框”；横跨日界线的环边在每个框内按短弧展开。这样线段与
  多边形边的求交是普通平面线段求交，无需球面投影。

边界规则
--------
外环边界在保护区内；内环（洞）边界也在保护区内（洞内严格区域除外）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

EPS = 1e-9
SEG_EPS = 1e-9
# 叉积的绝对容差：经纬度坐标量级 <= 360，叉积量级可达约 1e5，
# 双精度绝对误差约为 1e-10，1e-7 足以容纳舍入误差而不会误判。
CROSS_EPS = 1e-7
TWO_PI_OFFSET = 360.0

Point = tuple[float, float]  # (lon, lat)


class GeometryError(ValueError):
    """无效几何（类型、坐标范围、自交、洞不合法等）。"""


def cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def wrap_delta(dlon: float) -> float:
    """把经度增量规整到 (-180, 180]（较短方向）。"""
    d = (dlon + 180.0) % 360.0 - 180.0
    if d == -180.0:
        d = 180.0
    return d


# ---------------------------------------------------------------------------
# 线段关系
# ---------------------------------------------------------------------------

def point_on_segment(p: Point, a: Point, b: Point) -> bool:
    """点 p 是否在线段 a-b 上（含端点，含共线重叠）。"""
    px, py = p
    ax, ay = a
    bx, by = b
    ex, ey = bx - ax, by - ay
    denom = ex * ex + ey * ey
    if denom <= EPS:
        return abs(px - ax) <= EPS and abs(py - ay) <= EPS
    t = ((px - ax) * ex + (py - ay) * ey) / denom
    if t < -EPS or t > 1.0 + EPS:
        return False
    # 点到直线距离用叉积判断，容差按线段长度缩放
    cx = ex * (py - ay) - ey * (px - ax)
    return abs(cx) <= CROSS_EPS


def _seg_params_for_overlap(a1: Point, a2: Point, b1: Point, b2: Point) -> tuple[float, float]:
    """共线情况下，b1/b2 在 a 段参数坐标上的最小/最大值。"""
    ex, ey = a2[0] - a1[0], a2[1] - a1[1]
    denom = ex * ex + ey * ey
    if denom <= EPS:
        t1 = t2 = 0.0
    else:
        t1 = ((b1[0] - a1[0]) * ex + (b1[1] - a1[1]) * ey) / denom
        t2 = ((b2[0] - a1[0]) * ex + (b2[1] - a1[1]) * ey) / denom
    return min(t1, t2), max(t1, t2)


def segment_edge_relation(a1: Point, a2: Point, b1: Point, b2: Point):
    """线段 a 与线段 b 的关系。

    返回:
        None                        —— 不相交
        ("point", (s, u))           —— 单个交点，s/u 为两线段上的参数
        ("range", (s0, s1))         —— 共线重叠，a 段参数区间
    """
    rx, ry = a2[0] - a1[0], a2[1] - a1[1]
    sx, sy = b2[0] - b1[0], b2[1] - b1[1]
    rxs = cross(rx, ry, sx, sy)
    qpx = b1[0] - a1[0]
    qpy = b1[1] - a1[1]

    if abs(rxs) > CROSS_EPS:
        s = cross(qpx, qpy, sx, sy) / rxs
        u = cross(qpx, qpy, rx, ry) / rxs
        if s >= -EPS and s <= 1.0 + EPS and u >= -EPS and u <= 1.0 + EPS:
            return "point", (min(1.0, max(0.0, s)), min(1.0, max(0.0, u)))
        return None

    # 平行：检查共线
    if abs(cross(qpx, qpy, rx, ry)) > CROSS_EPS:
        return None

    s0, s1 = _seg_params_for_overlap(a1, a2, b1, b2)
    if s1 < -EPS or s0 > 1.0 + EPS:
        return None
    s0 = min(1.0, max(0.0, s0))
    s1 = min(1.0, max(0.0, s1))
    if s1 - s0 <= EPS:
        return "point", (s1,)
    return "range", (s0, s1)


# ---------------------------------------------------------------------------
# 多边形
# ---------------------------------------------------------------------------

@dataclass
class Poly:
    """一个 Polygon：outer 为外环顶点（不含闭合点），holes 为内环列表。"""

    outer: list[Point]
    holes: list[list[Point]] = field(default_factory=list)
    _unwrap_cache: dict = field(default_factory=dict, repr=False, compare=False)

    def _unwrap_ring(self, vertices: list[Point]) -> list[Point]:
        """把环一致展开到一个连续经度平面。

        从首点出发，后续每个顶点经度按短弧增量累积，使得环的每条边
        都不跨越日界线（RFC 7946 切割环的接缝边也会被一致展开）。
        """
        cached = self._unwrap_cache.get(id(vertices))
        if cached is not None:
            return cached
        out = [vertices[0]]
        for p in vertices[1:]:
            prev = out[-1]
            out.append((prev[0] + wrap_delta(p[0] - prev[0]), p[1]))
        self._unwrap_cache[id(vertices)] = out
        return out

    def _frame_vertices(self, vertices: list[Point], k: int) -> list[Point]:
        base = self._unwrap_ring(vertices)
        if k:
            return [(lon + TWO_PI_OFFSET * k, lat) for lon, lat in base]
        return list(base)

    def edges(self, k: int) -> list[tuple[Point, Point]]:
        """坐标框 k 下全部边（外环在前，其后依次为各洞）；均为普通短边。"""
        cache = self.__dict__.setdefault("_edge_cache", {})
        if k in cache:
            return cache[k]
        frames: list[tuple[Point, Point]] = []
        for ring in [self.outer, *self.holes]:
            verts = self._frame_vertices(ring, k)
            n = len(verts)
            frames.extend((verts[i], verts[(i + 1) % n]) for i in range(n))
        cache[k] = frames
        return frames


def ring_wn(p: Point, edges: list[tuple[Point, Point]]) -> int:
    """绕数法判定：0 = 环外，非 0 = 环内（不区分边界）。"""
    x, y = p
    wn = 0
    for a, b in edges:
        ax, ay = a
        bx, by = b
        if ay <= y:
            if by > y and cross(bx - ax, by - ay, x - ax, y - ay) > CROSS_EPS:
                wn += 1
        elif by <= y:
            if cross(bx - ax, by - ay, x - ax, y - ay) < -CROSS_EPS:
                wn -= 1
    return wn


def _ring_slice(poly: Poly, hole: int) -> tuple[int, int]:
    """edges(k) 缓存中某一环对应的边切片（0=外环，1..=洞）。"""
    if hole == 0:
        return 0, len(poly.outer)
    start = len(poly.outer)
    for h in range(hole - 1):
        start += len(poly.holes[h])
    return start, start + len(poly.holes[hole - 1])


def _point_ring_status(p: Point, poly: Poly, k: int, hole: int) -> str:
    """点相对某个环（坐标框 k）的状态: outside / boundary / inside。"""
    s0, s1 = _ring_slice(poly, hole)
    edges = poly.edges(k)[s0:s1]
    for a, b in edges:
        if point_on_segment(p, a, b):
            return "boundary"
    return "inside" if ring_wn(p, edges) != 0 else "outside"


def _agg_ring_status(p: Point, poly: Poly, hole: int) -> str:
    """跨三个坐标框聚合：boundary 优先，其次 inside，否则 outside。"""
    result = "outside"
    for k in (-1, 0, 1):
        status = _point_ring_status(p, poly, k, hole)
        if status == "boundary":
            return "boundary"
        if status == "inside":
            result = "inside"
    return result


def ring_contains_inclusive(p: Point, poly: Poly, hole: int = 0) -> bool:
    """点在环内或环边界上（跨坐标框）。"""
    return _agg_ring_status(p, poly, hole) != "outside"


def point_in_polygon(p: Point, poly: Poly) -> bool:
    """保护区主规则：外环边界计入；洞边界计入；洞内严格区域不计。"""
    if _agg_ring_status(p, poly, 0) == "outside":
        return False
    for hi in range(1, len(poly.holes) + 1):
        if _agg_ring_status(p, poly, hi) == "inside":
            return False
    return True


def point_strict_in_polygon(p: Point, poly: Poly) -> bool:
    """点严格位于多边形内部（外环严格内、不在任何环边界、不在洞内）。"""
    if _agg_ring_status(p, poly, 0) != "inside":
        return False
    for hi in range(1, len(poly.holes) + 1):
        if _agg_ring_status(p, poly, hi) != "outside":
            return False
    return True


# ---------------------------------------------------------------------------
# 线段 -> 保护区内的闭参数区间
# ---------------------------------------------------------------------------

def _cluster(values: list[float]) -> list[float]:
    """把接近（<= EPS）的参数值聚成一个。"""
    values.sort()
    out: list[float] = []
    for v in values:
        if not out or v - out[-1] > EPS:
            out.append(v)
        else:
            out[-1] = (out[-1] + v) / 2.0
    return out


def segment_closed_intervals(p1: Point, p2: Point, polys: list[Poly]) -> list[tuple[float, float]]:
    """线段 p1->p2 落在多边形并集内的 s 区间（闭）。

    线段坐标使用展开经度（可超出 [-180,180]，由多边形的 k=-1/0/1
    坐标框覆盖）；调用方负责按短弧展开。洞边校验等内部调用应先按
    环展开端点（见 Poly._unwrap_ring）。
    边界（外环、洞边）计入；洞内严格区域不计。单点触碰表现为零长区间。
    """
    zero_len = abs(p2[0] - p1[0]) <= EPS and abs(p2[1] - p1[1]) <= EPS
    if zero_len:
        return [(0.0, 1.0)] if any(point_in_polygon(p1, poly) for poly in polys) else []

    cuts: set[float] = {0.0, 1.0}
    ranges: list[tuple[float, float]] = []
    sxmin, sxmax = min(p1[0], p2[0]), max(p1[0], p2[0])
    symin, symax = min(p1[1], p2[1]), max(p1[1], p2[1])

    for poly in polys:
        for k in (-1, 0, 1):
            for e1, e2 in poly.edges(k):
                # 包围盒预筛
                if (max(e1[0], e2[0]) < sxmin - EPS or min(e1[0], e2[0]) > sxmax + EPS
                        or max(e1[1], e2[1]) < symin - EPS or min(e1[1], e2[1]) > symax + EPS):
                    continue
                rel = segment_edge_relation(p1, p2, e1, e2)
                if rel is None:
                    continue
                if rel[0] == "point":
                    cuts.add(rel[1][0])
                else:
                    s0, s1 = rel[1]
                    ranges.append((s0, s1))
                    cuts.add(s0)
                    cuts.add(s1)

    nodes = _cluster([min(1.0, max(0.0, v)) for v in cuts])

    def inside(s: float) -> bool:
        lon = p1[0] + (p2[0] - p1[0]) * s
        lat = p1[1] + (p2[1] - p1[1]) * s
        return any(point_in_polygon((lon, lat), poly) for poly in polys)

    result: list[tuple[float, float]] = []
    for i in range(len(nodes) - 1):
        a, b = nodes[i], nodes[i + 1]
        if b - a <= EPS:
            continue
        if inside((a + b) / 2.0):
            result.append((a, b))

    result.extend(ranges)
    return union_closed(result)


def union_closed(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并相交或相接的闭区间；保持零长区间为点。"""
    if not intervals:
        return []
    items = sorted((min(a, b), max(a, b)) for a, b in intervals)
    merged: list[tuple[float, float]] = []
    cur_a, cur_b = items[0]
    for a, b in items[1:]:
        if a <= cur_b + EPS:
            if b > cur_b:
                cur_b = b
        else:
            merged.append((cur_a, cur_b))
            cur_a, cur_b = a, b
    merged.append((cur_a, cur_b))
    return merged


def _point_at(a: Point, b: Point, t: float) -> Point:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _ring_wn_strict(p: Point, verts: list[Point]) -> int:
    """单个展开环的绕数（调用前应保证 p 不在该环边界上）。"""
    return ring_wn(p, list(zip(verts, verts[1:] + verts[:1])))


def _point_on_ring(p: Point, verts: list[Point]) -> bool:
    for i in range(len(verts)):
        if point_on_segment(p, verts[i], verts[(i + 1) % len(verts)]):
            return True
    return False


def _shared_edge_joins_outside(e1: tuple[Point, Point], s01: tuple[float, float],
                               r1: list[Point], r2: list[Point]) -> bool:
    """共线重叠是否为合法“接缝共边”：两环内部位于公共线相反两侧。

    用于合法化 RFC 7946 在 ±180° 接缝处切开、共享接缝边的表示；
    两环内部在同一侧（真实面积重叠）时返回 False。
    """
    a1, a2 = e1
    m = _point_at(a1, a2, (s01[0] + s01[1]) / 2.0)
    dx, dy = a2[0] - a1[0], a2[1] - a1[1]
    length = math.hypot(dx, dy)
    if length <= EPS:
        return False
    nx, ny = -dy / length, dx / length  # 几何法向（与边方向无关）
    delta = 1e-6
    qp = (m[0] + nx * delta, m[1] + ny * delta)
    qm = (m[0] - nx * delta, m[1] - ny * delta)

    def sides(verts):
        ip = _ring_wn_strict(qp, verts) != 0 and not _point_on_ring(qp, verts)
        im = _ring_wn_strict(qm, verts) != 0 and not _point_on_ring(qm, verts)
        return ip, im

    a_p, a_m = sides(r1)
    b_p, b_m = sides(r2)
    return (a_p ^ a_m) and (b_p ^ b_m) and (a_p != b_p)


def edges_transversally_cross(e1: tuple[Point, Point], e2: tuple[Point, Point],
                              r1: list[Point], r2: list[Point]) -> bool:
    """两条分别属于不同简单环的边是否构成非法相交（横截交叉或面积重叠）。

    * 共线重叠：内部同侧（面积重叠）非法；内部异侧（接缝共边）合法。
    * 点接触：接触点两侧的微邻域真正穿入对方内部才算非法；
      外切/顶点接触合法。
    r1/r2 为两环一致展开后的顶点列表。
    """
    a1, a2 = e1
    b1, b2 = e2
    rel_ab = segment_edge_relation(a1, a2, b1, b2)
    if rel_ab is None:
        return False
    if rel_ab[0] == "range":
        return not _shared_edge_joins_outside(e1, rel_ab[1], r1, r2)

    s = rel_ab[1][0]
    u = segment_edge_relation(b1, b2, a1, a2)[1][0]
    near = 1e-7

    def enters(edge: tuple[Point, Point], t: float, other_verts: list[Point]) -> bool:
        ea, eb = edge
        for dt in (-near, near):
            tt = t + dt
            if tt <= EPS or tt >= 1.0 - EPS:
                continue  # 接触点外没有线段延伸的一侧跳过
            q = _point_at(ea, eb, tt)
            if _ring_wn_strict(q, other_verts) != 0:
                return True
        return False

    return enters(e1, s, r2) and enters(e2, u, r1)


# ---------------------------------------------------------------------------
# GeoJSON 解析与严格校验
# ---------------------------------------------------------------------------

def _is_finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def parse_ring(raw, label: str) -> list[Point]:
    if not isinstance(raw, (list, tuple)):
        raise GeometryError(f"{label} 必须是坐标数组")
    if len(raw) < 4:
        raise GeometryError(f"{label} 至少需要 4 个位置（首尾闭合）")
    ring: list[Point] = []
    for i, pos in enumerate(raw):
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            raise GeometryError(f"{label} 第 {i + 1} 个位置必须是 [经度, 纬度]")
        lon, lat = pos[0], pos[1]
        if not (_is_finite_number(lon) and _is_finite_number(lat)):
            raise GeometryError(f"{label} 第 {i + 1} 个位置坐标必须是有限数字")
        lon, lat = float(lon), float(lat)
        if not (-180.0 <= lon <= 180.0):
            raise GeometryError(f"{label} 第 {i + 1} 个位置经度越界: {lon}")
        if not (-90.0 <= lat <= 90.0):
            raise GeometryError(f"{label} 第 {i + 1} 个位置纬度越界: {lat}")
        ring.append((lon, lat))

    if abs(ring[0][0] - ring[-1][0]) > EPS or abs(ring[0][1] - ring[-1][1]) > EPS:
        raise GeometryError(f"{label} 首尾坐标不一致，环未闭合")
    ring = ring[:-1]  # 去掉闭合重复点

    n = len(ring)
    for i in range(n):
        a = ring[i]
        b = ring[(i + 1) % n]
        if abs(a[0] - b[0]) <= EPS and abs(a[1] - b[1]) <= EPS:
            raise GeometryError(f"{label} 存在相邻重复点（第 {i + 1} 个位置）")

    # 自交检查在“一致展开”的边上进行（与 Poly._unwrap_ring 同一规则），
    # 避免朴素表示中横跨日界线的长边造成误判。
    flat = [ring[0]]
    for p in ring[1:]:
        prev = flat[-1]
        flat.append((prev[0] + wrap_delta(p[0] - prev[0]), p[1]))
    edges = list(zip(flat, flat[1:] + flat[:1]))
    boxes = []
    for (ax, ay), (bx, by) in edges:
        boxes.append((min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)))
    for i in range(n):
        xmin_i, xmax_i, ymin_i, ymax_i = boxes[i]
        for j in range(i + 1, n):
            adjacent = (j == i + 1) or (i == 0 and j == n - 1)
            if adjacent:
                continue
            xmin_j, xmax_j, ymin_j, ymax_j = boxes[j]
            if (xmax_i < xmin_j - EPS or xmax_j < xmin_i - EPS
                    or ymax_i < ymin_j - EPS or ymax_j < ymin_i - EPS):
                continue
            rel = segment_edge_relation(edges[i][0], edges[i][1], edges[j][0], edges[j][1])
            if rel is not None:
                raise GeometryError(f"{label} 自交（边 {i + 1} 与边 {j + 1} 非法接触）")
    return ring


def build_poly(raw_rings, label: str) -> Poly:
    if not isinstance(raw_rings, (list, tuple)) or len(raw_rings) < 1:
        raise GeometryError(f"{label} 的 coordinates 必须至少包含一个外环")
    outer = parse_ring(raw_rings[0], f"{label} 外环")
    holes = [parse_ring(r, f"{label} 洞 {i + 1}") for i, r in enumerate(raw_rings[1:])]

    poly = Poly(outer=outer, holes=holes)

    # 每个洞必须严格位于外环内（允许洞边界与外环相接）：
    # 洞的每条边落在外环多边形内的 s 覆盖必须为整条边。
    outer_poly = Poly(outer=outer, holes=[])
    for hi, hole in enumerate(holes):
        hn = len(hole)
        unwrapped = outer_poly._unwrap_ring(hole)  # noqa: SLF001
        for i in range(hn):
            a = unwrapped[i]
            b = unwrapped[(i + 1) % hn]
            covered = segment_closed_intervals(a, b, [outer_poly])
            if not covered:
                raise GeometryError(f"{label} 洞 {hi + 1} 位于外环之外")
            (c0, c1), rest = covered[0], covered[1:]
            if rest or c0 > EPS or c1 < 1.0 - EPS:
                raise GeometryError(f"{label} 洞 {hi + 1} 未完全位于外环内")

    # 洞与洞：边不得横截交叉/重叠（切点接触允许）；不得包含
    m = len(holes)
    unwrapped_holes = [outer_poly._unwrap_ring(h) for h in holes]  # noqa: SLF001
    for i in range(m):
        ri, ri_flat = holes[i], unwrapped_holes[i]
        for j in range(i + 1, m):
            rj, rj_flat = holes[j], unwrapped_holes[j]
            for ei in range(len(ri_flat)):
                e1 = (ri_flat[ei], ri_flat[(ei + 1) % len(ri_flat)])
                for ej in range(len(rj_flat)):
                    e2 = (rj_flat[ej], rj_flat[(ej + 1) % len(rj_flat)])
                    if edges_transversally_cross(e1, e2, ri_flat, rj_flat):
                        raise GeometryError(
                            f"{label} 洞 {i + 1} 与洞 {j + 1} 边界交叉或重叠"
                        )
            # 包含关系：任一顶点严格在另一洞环内即非法
            for p in ri:
                if _agg_ring_status(p, poly, j + 1) == "inside":
                    raise GeometryError(f"{label} 洞 {i + 1} 与洞 {j + 1} 存在包含关系")
            for p in rj:
                if _agg_ring_status(p, poly, i + 1) == "inside":
                    raise GeometryError(f"{label} 洞 {i + 1} 与洞 {j + 1} 存在包含关系")
    return poly


def parse_geojson(geom, label: str = "保护区") -> list[Poly]:
    """解析 GeoJSON Polygon / MultiPolygon（也接受 Feature 包装）。"""
    if not isinstance(geom, dict):
        raise GeometryError(f"{label} 的 geometry 必须是 GeoJSON 对象")
    gtype = geom.get("type")
    if gtype == "Feature":
        return parse_geojson(geom.get("geometry"), label)
    coords = geom.get("coordinates")
    if gtype == "Polygon":
        return [build_poly(coords, label)]
    if gtype == "MultiPolygon":
        if not isinstance(coords, list) or len(coords) < 1:
            raise GeometryError(f"{label} MultiPolygon 必须至少包含一个 Polygon")
        polys = [build_poly(c, f"{label} 第 {i + 1} 部分") for i, c in enumerate(coords)]
        validate_multipolygon(polys, label)
        return polys
    raise GeometryError(f"{label} geometry.type 必须是 Polygon 或 MultiPolygon（收到 {gtype!r}）")


def validate_multipolygon(polys: list[Poly], label: str) -> None:
    def parts_cross(a: Poly, b: Poly) -> bool:
        a_flat = a._unwrap_ring(a.outer)  # noqa: SLF001
        b_flat = b._unwrap_ring(b.outer)  # noqa: SLF001
        for k in (-1, 0, 1):
            av = [(lon + TWO_PI_OFFSET * k, lat) for lon, lat in a_flat]
            bv = [(lon + TWO_PI_OFFSET * k, lat) for lon, lat in b_flat]
            for i in range(len(av)):
                e1 = (av[i], av[(i + 1) % len(av)])
                for j in range(len(bv)):
                    e2 = (bv[j], bv[(j + 1) % len(bv)])
                    if edges_transversally_cross(e1, e2, av, bv):
                        return True
        return False

    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if parts_cross(polys[i], polys[j]):
                raise GeometryError(
                    f"{label} MultiPolygon 的第 {i + 1}、{j + 1} 部分边界交叉或重叠"
                )
            # 点接触允许；一个部分完全落在另一个内部则非法
            for p in polys[j].outer:
                if point_strict_in_polygon(p, polys[i]):
                    raise GeometryError(
                        f"{label} MultiPolygon 的第 {j + 1} 部分位于第 {i + 1} 部分内部"
                    )
            for p in polys[i].outer:
                if point_strict_in_polygon(p, polys[j]):
                    raise GeometryError(
                        f"{label} MultiPolygon 的第 {i + 1} 部分位于第 {j + 1} 部分内部"
                    )
