"""端到端 HTTP 测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
}
SQUARE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]],
    ],
}
DATELINE_BAND = {
    "type": "Polygon",
    "coordinates": [[[170, -10], [170, 10], [-170, 10], [-170, -10], [170, -10]]],
}


def req(areas, track):
    return client.post("/api/v1/analyze", json={"areas": areas, "track": track})


def area(aid, geom, vf="2026-01-01T00:00:00Z", vt=None):
    a = {"id": aid, "valid_from": vf, "geometry": geom}
    if vt:
        a["valid_to"] = vt
    return a


def track(points):
    return [{"time": t, "lon": lon, "lat": lat} for t, lon, lat in points]


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_basic_square_crossing():
    # 60s 内从 (-10,5) 到 (30,5)，速度 40 度/60s；进入 x=0 在 15s，离开 x=10 在 30s
    tr = track([
        ("2026-03-01T00:00:00Z", -10, 5),
        ("2026-03-01T00:01:00Z", 30, 5),
    ])
    r = req([area("A1", SQUARE)], tr)
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["id"] == "A1"
    assert res["inside"] is True
    assert res["total_duration_seconds"] == 15.0
    assert res["entries"] == [{
        "enter": "2026-03-01T00:00:15Z",
        "leave": "2026-03-01T00:00:30Z",
        "duration_seconds": 15.0,
    }]


def test_multi_segment_merge_touching():
    # 段1 从区外走到右上角 (10,10)：在 s=0.5（5s）进入，结尾为点接触；
    # 段2 从 (10,10) 走到区外：仅起点接触；段3 在远处，不接触。
    # 两次点接触都不计时长 -> 段1 内停留 5s，且不与其他片段相接。
    tr = track([
        ("2026-03-01T00:00:00Z", 0, 0),
        ("2026-03-01T00:00:10Z", 10, 10),
        ("2026-03-01T00:00:20Z", 11, 11),
        ("2026-03-01T00:00:30Z", 50, 50),
    ])
    r = req([area("A1", SQUARE)], tr)
    res = r.json()["results"][0]
    assert res["total_duration_seconds"] == 10.0
    assert len(res["entries"]) == 1
    assert res["entries"][0]["enter"] == "2026-03-01T00:00:00Z"
    assert res["entries"][0]["leave"] == "2026-03-01T00:00:10Z"


def test_segments_touching_inside_merge():
    # 段1 结尾在边界 (10,5)、段2 从同一点回到区内：[a,b) 与 [b,c) 相接合并
    tr = track([
        ("2026-03-01T00:00:00Z", 0, 5),
        ("2026-03-01T00:00:10Z", 10, 5),
        ("2026-03-01T00:00:20Z", 5, 5),
        ("2026-03-01T00:00:30Z", 5, 0),
    ])
    r = req([area("A1", SQUARE)], tr)
    res = r.json()["results"][0]
    assert res["total_duration_seconds"] == 30.0
    assert len(res["entries"]) == 1
    assert res["entries"][0]["enter"] == "2026-03-01T00:00:00Z"
    assert res["entries"][0]["leave"] == "2026-03-01T00:00:30Z"


def test_hole_interior_excluded_boundary_included():
    # 沿 y=5 横穿，60s 走 30 度；进入 x=0 在 20s，离开 x=10 在 40s；
    # 洞 [4,6]x[4,6]：进入洞边 x=4 在 28s、离开洞边 x=6 在 32s，
    # 洞边界本身计入、洞内严格区域 (28,32) 扣除。
    tr = track([
        ("2026-03-01T00:00:00Z", -10, 5),
        ("2026-03-01T00:01:00Z", 20, 5),
    ])
    r = req([area("A1", SQUARE_WITH_HOLE)], tr)
    res = r.json()["results"][0]
    assert res["total_duration_seconds"] == 16.0
    assert [e["enter"] for e in res["entries"]] == [
        "2026-03-01T00:00:20Z",
        "2026-03-01T00:00:32Z",
    ]
    assert [e["leave"] for e in res["entries"]] == [
        "2026-03-01T00:00:28Z",
        "2026-03-01T00:00:40Z",
    ]
    # 沿洞边界 y=4 行走应全程计入
    tr2 = track([
        ("2026-03-01T00:00:00Z", 4, 4),
        ("2026-03-01T00:00:10Z", 6, 4),
    ])
    r2 = req([area("A1", SQUARE_WITH_HOLE)], tr2)
    assert r2.json()["results"][0]["total_duration_seconds"] == 10.0


def test_single_point_touch_has_no_duration():
    # 两条线段均只在顶点 (10,0) 触碰多边形（该线 y=x-10 在 0<x<10 时 y<0）
    tr = track([
        ("2026-03-01T00:00:00Z", 11, 1),
        ("2026-03-01T00:00:10Z", 9, -1),
        ("2026-03-01T00:00:20Z", 11, 1),
    ])
    r = req([area("A1", SQUARE)], tr)
    res = r.json()["results"][0]
    assert res["inside"] is False
    assert res["total_duration_seconds"] == 0.0
    assert res["entries"] == []


def test_tangent_vertex_no_duration():
    tri = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [5, 5], [0, 0]]]}
    tr = track([
        ("2026-03-01T00:00:00Z", -5, 0),
        ("2026-03-01T00:00:10Z", 15, 0),
    ])
    r = req([area("A1", tri)], tr)
    res = r.json()["results"][0]
    # 沿底边 0..10 行走，边界计入，共 5s
    assert res["total_duration_seconds"] == 5.0

    # 仅在顶点 (5,5) 相切的水平线
    tr2 = track([
        ("2026-03-01T00:00:00Z", 0, 5),
        ("2026-03-01T00:00:10Z", 10, 5),
    ])
    r2 = req([area("A1", tri)], tr2)
    res2 = r2.json()["results"][0]
    assert res2["total_duration_seconds"] == 0.0
    assert res2["entries"] == []


def test_validity_window_half_open_clipping():
    # 区内窗口 15..30s；生效 [00:00:20, 00:00:26) -> 6s
    tr = track([
        ("2026-03-01T00:00:00Z", -10, 5),
        ("2026-03-01T00:01:00Z", 30, 5),
    ])
    a = area("A1", SQUARE, vf="2026-03-01T00:00:20Z", vt="2026-03-01T00:00:26Z")
    r = req([a], tr)
    res = r.json()["results"][0]
    assert res["total_duration_seconds"] == 6.0
    assert res["entries"][0]["enter"] == "2026-03-01T00:00:20Z"
    assert res["entries"][0]["leave"] == "2026-03-01T00:00:26Z"


def test_validity_window_clips_at_endpoint_half_open():
    # 离开时刻正好等于 valid_to：valid_to 不含，停留截到 00:00:30
    tr = track([
        ("2026-03-01T00:00:00Z", -10, 5),
        ("2026-03-01T00:01:00Z", 30, 5),
    ])
    a = area("A1", SQUARE, vf="2026-03-01T00:00:00Z", vt="2026-03-01T00:00:30Z")
    r = req([a], tr)
    res = r.json()["results"][0]
    assert res["total_duration_seconds"] == 15.0


def test_validity_window_split_and_merge():
    # 段1 在 6.667s 进入；窗口 [5s, 15s)：段2 在 13.333s 已离开，
    # 窗口未截断停留，总量 6.667s
    tr = track([
        ("2026-03-01T00:00:00Z", -10, 5),
        ("2026-03-01T00:00:10Z", 5, 5),
        ("2026-03-01T00:00:20Z", 20, 5),
    ])
    a = area("A1", SQUARE, vf="2026-03-01T00:00:05Z", vt="2026-03-01T00:00:15Z")
    r = req([a], tr)
    res = r.json()["results"][0]
    assert res["total_duration_seconds"] == pytest.approx(6.666667)
    assert len(res["entries"]) == 1
    assert res["entries"][0]["enter"] == "2026-03-01T00:00:06.666667Z"
    assert res["entries"][0]["leave"] == "2026-03-01T00:00:13.333333Z"

    # 长窗口覆盖全程：截后两段在段界 10s 处相接（[..,10)+[10,..)）-> 合并
    a2 = area("A1", SQUARE, vf="2026-03-01T00:00:00Z", vt="2026-03-01T00:00:30Z")
    r2 = req([a2], tr)
    res2 = r2.json()["results"][0]
    assert res2["total_duration_seconds"] == pytest.approx(6.666667)
    assert len(res2["entries"]) == 1

    # 窗口从段1 中部（8s）截到段2 中部（15s）：[8,10)+[10,15) 相接合并
    a3 = area("A1", SQUARE, vf="2026-03-01T00:00:08Z", vt="2026-03-01T00:00:15Z")
    r3 = req([a3], tr)
    res3 = r3.json()["results"][0]
    assert res3["total_duration_seconds"] == pytest.approx(5.333333)
    assert len(res3["entries"]) == 1
    assert res3["entries"][0]["enter"] == "2026-03-01T00:00:08Z"
    assert res3["entries"][0]["leave"] == "2026-03-01T00:00:13.333333Z"


def test_dateline_crossing():
    # 175 -> -175，20s，全程在 170..180 / -180..-170 带状区内
    tr = track([
        ("2026-03-01T00:00:00Z", 175, 0),
        ("2026-03-01T00:00:20Z", -175, 0),
    ])
    r = req([area("A1", DATELINE_BAND)], tr)
    res = r.json()["results"][0]
    assert res["total_duration_seconds"] == 20.0

    # 160 -> -160（向东短弧 40 度/40s）：进 170 在 10s，离开 -170 在 30s，
    # 过日界后由多边形 k=-1 框接续，片段相接合并为一次停留 20s
    tr2 = track([
        ("2026-03-01T00:00:00Z", 160, 0),
        ("2026-03-01T00:00:40Z", -160, 0),
    ])
    r2 = req([area("A1", DATELINE_BAND)], tr2)
    res2 = r2.json()["results"][0]
    assert res2["total_duration_seconds"] == 20.0
    assert len(res2["entries"]) == 1
    assert res2["entries"][0]["enter"] == "2026-03-01T00:00:10Z"
    assert res2["entries"][0]["leave"] == "2026-03-01T00:00:30Z"


def test_dateline_rfc7946_cut_polygon():
    # RFC 7946 切割表示：同一个带状区画成两部分（共享 ±180 接缝边）
    mp = {"type": "MultiPolygon", "coordinates": [
        [[[170, -10], [170, 10], [180, 10], [180, -10], [170, -10]]],
        [[[-180, -10], [-180, 10], [-170, 10], [-170, -10], [-180, -10]]],
    ]}
    tr = track([
        ("2026-03-01T00:00:00Z", 160, 0),
        ("2026-03-01T00:00:40Z", -160, 0),
    ])
    r = req([area("A1", mp)], tr)
    res = r.json()["results"][0]
    assert res["total_duration_seconds"] == 20.0
    assert len(res["entries"]) == 1


def test_multipolygon():
    mp = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
        [[[8, 0], [10, 0], [10, 2], [8, 2], [8, 0]]],
    ]}
    tr = track([
        ("2026-03-01T00:00:00Z", -2, 1),
        ("2026-03-01T00:00:12Z", 12, 1),
    ])
    r = req([area("M1", mp)], tr)
    res = r.json()["results"][0]
    # 速度 14/12 度每秒；块1：x 0..2 占 12/7 s；块2：x 8..10 占 12/7 s
    assert res["total_duration_seconds"] == pytest.approx(24 / 7)
    assert len(res["entries"]) == 2


def test_stationary_duplicate_points():
    # 同一位置停留：区内 10s 计入；区外 0s
    tr = track([
        ("2026-03-01T00:00:00Z", 5, 5),
        ("2026-03-01T00:00:05Z", 5, 5),
        ("2026-03-01T00:00:10Z", 5, 5),
    ])
    r = req([area("A1", SQUARE)], tr)
    assert r.json()["results"][0]["total_duration_seconds"] == 10.0

    tr2 = track([
        ("2026-03-01T00:00:00Z", 50, 50),
        ("2026-03-01T00:00:10Z", 50, 50),
    ])
    r2 = req([area("A1", SQUARE)], tr2)
    assert r2.json()["results"][0]["total_duration_seconds"] == 0.0


def test_multiple_areas_echo_all():
    tr = track([
        ("2026-03-01T00:00:00Z", -10, 5),
        ("2026-03-01T00:01:00Z", 30, 5),
    ])
    other = {"type": "Polygon", "coordinates": [[[40, 40], [50, 40], [50, 50], [40, 50], [40, 40]]]}
    r = req([area("A1", SQUARE), area(7, other)], tr)
    results = r.json()["results"]
    assert [x["id"] for x in results] == ["A1", 7]
    assert results[0]["total_duration_seconds"] == 15.0
    assert results[1] == {"id": 7, "inside": False, "total_duration_seconds": 0.0, "entries": []}


def test_naive_timestamp_assumed_utc():
    tr = track([
        ("2026-03-01T00:00:00", -10, 5),
        ("2026-03-01T00:01:00", 30, 5),
    ])
    r = req([area("A1", SQUARE, vf="2026-03-01T00:00:00")], tr)
    assert r.status_code == 200
    assert r.json()["results"][0]["entries"][0]["enter"] == "2026-03-01T00:00:15Z"


def test_offset_timestamp_normalized():
    tr = [
        {"time": "2026-03-01T01:00:00+01:00", "lon": -10, "lat": 5},
        {"time": "2026-03-01T01:01:00+01:00", "lon": 30, "lat": 5},
    ]
    r = req([area("A1", SQUARE)], tr)
    assert r.status_code == 200
    assert r.json()["results"][0]["entries"][0]["enter"] == "2026-03-01T00:00:15Z"
