"""几何核心的针对性单元测试。"""

from app.geopoly import (
    parse_geojson,
    segment_closed_intervals,
    wrap_delta,
    GeometryError,
)


def poly(coords, gtype="Polygon"):
    return parse_geojson({"type": gtype, "coordinates": coords})


SQUARE = [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]]


def test_wrap_delta_tie_breaks_positive():
    # (-180, 180]：180 与 -180 歧义时取 +180
    assert wrap_delta(180.0) == 180.0
    assert wrap_delta(-180.0) == 180.0
    assert wrap_delta(359.0) == -1.0
    assert wrap_delta(-359.0) == 1.0


def test_orientation_independence():
    ccw = poly(SQUARE)
    cw = poly([SQUARE[0][::-1]])
    assert segment_closed_intervals((-5, 5), (15, 5), ccw) == [(0.25, 0.75)]
    assert segment_closed_intervals((15, 5), (-5, 5), cw) == [(0.25, 0.75)]


def test_collinear_overlap_with_edge():
    p = poly(SQUARE)
    # 与底边 y=0 部分重叠：x 在 [0,10] 的部分计入
    assert segment_closed_intervals((-5, 0), (5, 0), p) == [(0.5, 1.0)]
    assert segment_closed_intervals((2, 0), (12, 0), p) == [(0.0, 0.8)]
    # 完全沿边
    assert segment_closed_intervals((0, 0), (10, 0), p) == [(0.0, 1.0)]


def test_trajectory_starting_inside_hole():
    p = poly([SQUARE[0], [(4, 4), (4, 6), (6, 6), (6, 4), (4, 4)]])
    # 起点洞内严格区域不计，穿出洞边（x=6）后开始计入直到外环边 x=10
    assert segment_closed_intervals((5, 5), (15, 5), p) == [(0.1, 0.5)]


def test_ending_on_hole_boundary():
    p = poly([SQUARE[0], [(4, 4), (4, 6), (6, 6), (6, 4), (4, 4)]])
    # 终点停在洞边 x=4：闭区间包含终点，计入
    assert segment_closed_intervals((-5, 5), (4, 5), p) == [(5 / 9, 1.0)]


def test_meridian_polygon_and_track():
    # 以 ±180 为边的环，测线沿缝行走
    seam = poly([[(170, -10), (170, 10), (180, 10), (180, -10), (170, -10)]])
    assert segment_closed_intervals((175, 0), (180, 0), seam) == [(0.0, 1.0)]
    assert segment_closed_intervals((180, 0), (175, 0), seam) == [(0.0, 1.0)]
    assert segment_closed_intervals((-180, 0), (-175, 0), seam) == []


def test_invalid_ring_must_have_four_positions():
    try:
        poly([[(0, 0), (10, 0), (10, 10), (0, 10)]])
    except GeometryError as e:
        assert "闭合" in str(e)
    else:
        raise AssertionError("应拒绝未闭合环")


def test_invalid_coordinate_in_ring():
    try:
        poly([[(0, 0), (10, 0), (10, 95), (0, 10), (0, 0)]])
    except GeometryError as e:
        assert "纬度越界" in str(e)
    else:
        raise AssertionError("应拒绝越界坐标")


def test_dateline_naive_ring_not_false_self_intersection():
    # 朴素表示的跨日界线环（含 170 -> -170 的长边），不应误判为自交
    ring = [(170, -10), (170, 10), (-170, 10), (-170, -10), (170, -10)]
    p = poly([ring])
    # 调用方负责按短弧展开终点经度
    end = (175 + wrap_delta(-175 - 175), 0)
    assert segment_closed_intervals((175, 0), end, p) == [(0.0, 1.0)]
    assert segment_closed_intervals((0, 0), (10, 0), p) == []


def test_dateline_rfc7946_split_ring():
    # RFC 7946 切割表示（两部分在 ±180 接缝上共边，合法 MultiPolygon）
    east = [(170, -10), (170, 10), (180, 10), (180, -10), (170, -10)]
    west = [(-180, -10), (-180, 10), (-170, 10), (-170, -10), (-180, -10)]
    mp = poly([[east], [west]], gtype="MultiPolygon")
    end = (160 + wrap_delta(-160 - 160), 0)
    assert segment_closed_intervals((160, 0), end, mp)

