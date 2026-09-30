"""拒绝条件与错误信息测试（整次计算均须 HTTP 400 并指出原因）。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SQUARE = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]
HOLE = [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]]


def req(areas, track):
    return client.post("/api/v1/analyze", json={"areas": areas, "track": track})


def area(aid="A1", geom=None, vf="2026-01-01T00:00:00Z", vt=None):
    a = {"id": aid, "valid_from": vf, "geometry": geom or {"type": "Polygon", "coordinates": SQUARE}}
    if vt:
        a["valid_to"] = vt
    return a


def track(points):
    return [{"time": t, "lon": lon, "lat": lat} for t, lon, lat in points]


GOOD_TRACK = track([
    ("2026-03-01T00:00:00Z", 0, 0),
    ("2026-03-01T00:00:10Z", 1, 1),
])


@pytest.mark.parametrize("lon,lat", [(180.0001, 0), (-180.5, 0), (0, 90.1), (0, -91)])
def test_coordinate_out_of_range(lon, lat):
    tr = track([("2026-03-01T00:00:00Z", 0, 0), ("2026-03-01T00:00:10Z", lon, lat)])
    r = req([area()], tr)
    assert r.status_code == 400
    assert "越界" in r.text


def test_non_increasing_time_equal():
    tr = track([
        ("2026-03-01T00:00:10Z", 0, 0),
        ("2026-03-01T00:00:10Z", 1, 1),
    ])
    r = req([area()], tr)
    assert r.status_code == 400
    assert "递增" in r.text


def test_non_increasing_time_backwards():
    tr = track([
        ("2026-03-01T00:00:10Z", 0, 0),
        ("2026-03-01T00:00:09Z", 1, 1),
    ])
    r = req([area()], tr)
    assert r.status_code == 400
    assert "递增" in r.text


def test_bad_timestamp():
    tr = track([("not-a-time", 0, 0), ("2026-03-01T00:00:10Z", 1, 1)])
    r = req([area()], tr)
    assert r.status_code == 400
    assert "无法解析" in r.text


def test_valid_to_before_from():
    a = area(vf="2026-03-01T00:00:20Z", vt="2026-03-01T00:00:10Z")
    r = req([a], GOOD_TRACK)
    assert r.status_code == 400
    assert "valid_to" in r.text


def test_track_too_short():
    r = req([area()], track([("2026-03-01T00:00:00Z", 0, 0)]))
    assert r.status_code == 400


def test_areas_empty():
    r = client.post("/api/v1/analyze", json={"areas": [], "track": GOOD_TRACK})
    assert r.status_code == 400


def test_missing_fields():
    r = client.post("/api/v1/analyze", json={"areas": [{}], "track": []})
    assert r.status_code == 400


def test_extra_fields_forbidden():
    payload = {"areas": [area()], "track": GOOD_TRACK, "unexpected": 1}
    r = client.post("/api/v1/analyze", json=payload)
    assert r.status_code == 400


def test_not_geojson_body():
    r = client.post("/api/v1/analyze", json=["nope"])
    assert r.status_code == 400


def test_geometry_unsupported_type():
    a = area(geom={"type": "Point", "coordinates": [1, 2]})
    r = req([a], GOOD_TRACK)
    assert r.status_code == 400
    assert "Polygon" in r.text


def test_geometry_ring_not_closed():
    a = area(geom={"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10]]]})
    r = req([a], GOOD_TRACK)
    assert r.status_code == 400
    assert "闭合" in r.text


def test_geometry_self_intersecting():
    bowtie = [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]]
    a = area(geom={"type": "Polygon", "coordinates": bowtie})
    r = req([a], GOOD_TRACK)
    assert r.status_code == 400
    assert "自交" in r.text


def test_geometry_duplicate_consecutive_points():
    a = area(geom={"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]})
    r = req([a], GOOD_TRACK)
    assert r.status_code == 400
    assert "重复点" in r.text


def test_hole_outside_outer():
    a = area(geom={"type": "Polygon", "coordinates": [SQUARE, [[20, 20], [20, 22], [22, 22], [22, 20], [20, 20]]]})
    r = req([a], GOOD_TRACK)
    assert r.status_code == 400
    assert "外环" in r.text


def test_geometry_coords_out_of_range():
    a = area(geom={"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 91], [0, 10], [0, 0]]]})
    r = req([a], GOOD_TRACK)
    assert r.status_code == 400
    assert "纬度越界" in r.text


def test_multipolygon_nested_parts():
    mp = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]],
    ]}
    r = req([area(geom=mp)], GOOD_TRACK)
    assert r.status_code == 400
    assert "内部" in r.text


def test_multipolygon_overlapping_edges():
    mp = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[5, 0], [15, 0], [15, 10], [5, 10], [5, 0]]],
    ]}
    r = req([area(geom=mp)], GOOD_TRACK)
    assert r.status_code == 400
    assert "重叠" in r.text


def test_duplicate_area_ids():
    r = req([area("X"), area("X")], GOOD_TRACK)
    assert r.status_code == 400
    assert "重复" in r.text


def test_multipolygon_point_touch_allowed():
    # 两部分仅在顶点 (10,5) 点接触：合法
    mp = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 5], [0, 10], [0, 0]]],
        [[[10, 5], [20, 0], [20, 10], [10, 5]]],
    ]}
    r = req([area(geom=mp)], GOOD_TRACK)
    assert r.status_code == 200


def test_multipolygon_transversal_cross_rejected():
    # 两个矩形横截交叉（面积相互侵入）：拒绝
    mp = {"type": "MultiPolygon", "coordinates": [
        [[[0, 2], [10, 2], [10, 8], [0, 8], [0, 2]]],
        [[[2, 0], [8, 0], [8, 10], [2, 10], [2, 0]]],
    ]}
    r = req([area(geom=mp)], GOOD_TRACK)
    assert r.status_code == 400
    assert "交叉或重叠" in r.text


def test_multipolygon_same_side_overlap_rejected():
    # 两部分共边但内部在同一侧（真实面积重叠）：拒绝
    mp = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[10, 0], [20, 0], [20, 10], [10, 10], [10, 0]]],
    ]}
    # 注意：两个矩形仅在 x=10 共边且内部分居两侧，这是合法接缝；
    # 改为同侧重叠的情形：
    mp2 = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[0, 0], [10, 0], [10, 5], [0, 5], [0, 0]]],
    ]}
    r = req([area(geom=mp)], GOOD_TRACK)
    assert r.status_code == 200
    r2 = req([area(geom=mp2)], GOOD_TRACK)
    assert r2.status_code == 400
    assert "交叉或重叠" in r2.text or "内部" in r2.text


def test_multipolygon_antimeridian_seam_join_allowed():
    # RFC 7946 在 ±180 切开、共享接缝边（内部异侧）：合法
    mp = {"type": "MultiPolygon", "coordinates": [
        [[[170, -10], [170, 10], [180, 10], [180, -10], [170, -10]]],
        [[[-180, -10], [-180, 10], [-170, 10], [-170, -10], [-180, -10]]],
    ]}
    r = req([area(geom=mp)], GOOD_TRACK)
    assert r.status_code == 200


def test_holes_transversal_cross_rejected():
    # 两个洞横截交叉（十字）：拒绝
    geom = {"type": "Polygon", "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[1, 4], [9, 4], [9, 6], [1, 6], [1, 4]],
        [[4, 1], [6, 1], [6, 9], [4, 9], [4, 1]],
    ]}
    r = req([area(geom=geom)], GOOD_TRACK)
    assert r.status_code == 400
    assert "洞" in r.text


def test_feature_wrapper_accepted():
    feature = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": SQUARE}}
    r = req([area(geom=feature)], GOOD_TRACK)
    assert r.status_code == 200
