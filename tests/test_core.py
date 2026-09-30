"""核心算法测试（仅依赖标准库）：python -m unittest discover -s tests -v"""

import unittest

from app.analysis import (
    RequestError,
    analyze_request,
)

T0 = "2026-01-01T00:00:00Z"


def square_poly(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[
        [x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def pt(lon, lat, sec, base=T0):
    # sec 为距 base 的秒数偏移，生成时间字符串
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return {"lon": lon, "lat": lat,
            "time": f"2026-01-01T{h:02d}:{m:02d}:{s:02d}Z"}


def area(aid="A", gf=T0, gt="2026-01-01T12:00:00Z", geometry=None):
    return {"id": aid, "valid_from": gf, "valid_to": gt,
            "geometry": geometry or square_poly(2.0, 2.0, 8.0, 8.0)}


class CrossingBasicTests(unittest.TestCase):
    def test_straight_crossing_half_speed(self):
        # 每秒 1 度，横穿方块 [2,8]：进入 t=2，离开 t=8，停留 6 秒
        track = [pt(0, 5, 0), pt(10, 5, 10)]
        res = analyze_request({"protected_areas": [area()], "track_points": track})
        visits = res["results"][0]["visits"]
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["entry_time"], "2026-01-01T00:00:02Z")
        self.assertEqual(visits[0]["leave_time"], "2026-01-01T00:00:08Z")
        self.assertEqual(visits[0]["dwell_seconds"], 6)
        self.assertEqual(res["results"][0]["total_dwell_seconds"], 6)

    def test_two_separate_visits(self):
        track = [pt(0, 5, 0), pt(10, 5, 10), pt(0, 5, 20)]
        res = analyze_request({"protected_areas": [area()], "track_points": track})
        visits = res["results"][0]["visits"]
        self.assertEqual(len(visits), 2)
        self.assertEqual([v["dwell_seconds"] for v in visits], [6, 6])
        self.assertEqual(res["results"][0]["total_dwell_seconds"], 12)

    def test_outer_boundary_counts(self):
        # 沿下边界 y=2 行走，整段都在边界上
        track = [pt(2, 2, 0), pt(8, 2, 6)]
        res = analyze_request({"protected_areas": [area()], "track_points": track})
        visits = res["results"][0]["visits"]
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["dwell_seconds"], 6)

    def test_single_touch_no_duration(self):
        # 射线在顶点 (2,2) 相切：只碰一个点，不计时长
        track = [pt(0, 2, 0), pt(4, 2, 4)]
        # 这里实际沿 y=2 边界进入，改测纯顶点相切：折线擦过 (2,2)
        track = [pt(2, 0, 0), pt(2, 2, 2), pt(2, 0, 4)]
        res = analyze_request({"protected_areas": [area()], "track_points": track})
        self.assertEqual(res["results"][0]["visits"], [])
        self.assertEqual(res["results"][0]["total_dwell_seconds"], 0)

    def test_touch_then_merge_adjacent(self):
        # 进区 -> 边界点 -> 继续区内：相接片段合并为一次
        # 方块 [2,8]^2；点序列：外(0,5,t0) -> 边(2,5,t2) -> 内(5,5,t5) -> 边(8,5,t8) -> 外(10,5,t10)
        track = [pt(0, 5, 0), pt(2, 5, 2), pt(5, 5, 5), pt(8, 5, 8), pt(10, 5, 10)]
        res = analyze_request({"protected_areas": [area()], "track_points": track})
        visits = res["results"][0]["visits"]
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["dwell_seconds"], 6)


class HoleTests(unittest.TestCase):
    def area_with_hole(self):
        return area(geometry={
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
                [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]],
            ],
        })

    def test_hole_interior_excluded(self):
        track = [pt(-1, 5, 0), pt(11, 5, 12)]
        res = analyze_request({"protected_areas": [self.area_with_hole()],
                               "track_points": track})
        visits = res["results"][0]["visits"]
        # 区内 [1,11]；洞内 [5,7] 扣除；洞边界 x=4 与 x=6 计入（单点）但无时长
        self.assertEqual(len(visits), 2)
        self.assertEqual(visits[0]["entry_time"], "2026-01-01T00:00:01Z")
        self.assertEqual(visits[0]["leave_time"], "2026-01-01T00:00:05Z")
        self.assertEqual(visits[1]["entry_time"], "2026-01-01T00:00:07Z")
        self.assertEqual(visits[1]["leave_time"], "2026-01-01T00:00:11Z")
        self.assertEqual(res["results"][0]["total_dwell_seconds"], 8)

    def test_hole_boundary_counts(self):
        # 沿洞边界 y=4 横穿
        track = [pt(0, 4, 0), pt(10, 4, 10)]
        res = analyze_request({"protected_areas": [self.area_with_hole()],
                               "track_points": track})
        visits = res["results"][0]["visits"]
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["dwell_seconds"], 10)

    def test_hole_tangent_to_outer_at_one_point(self):
        # 洞仅以单个顶点与外环相切，其余严格在内：合法
        geom = {"type": "Polygon", "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[5, 0], [7, 4], [3, 4], [5, 0]],
        ]}
        track = [pt(-1, 1, 0), pt(11, 1, 60)]
        res = analyze_request({"protected_areas": [area(geometry=geom)],
                               "track_points": track})
        # 区内 60s，三角形洞内 15s（y=1 横穿宽度 2，速度 12/60=0.2 度/秒）
        self.assertEqual(res["results"][0]["total_dwell_seconds"], 45)

    def test_identical_ring_hole_rejected(self):
        geom = {"type": "Polygon", "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        ]}
        payload = {"protected_areas": [area(geometry=geom)],
                   "track_points": [pt(0, 5, 0), pt(10, 5, 10)]}
        with self.assertRaises(RequestError) as ctx:
            analyze_request(payload)
        self.assertIn("几何无效", str(ctx.exception))


class ValidityWindowTests(unittest.TestCase):
    def test_clipping_half_open(self):
        # 实际区内 t∈[2,8]，生效区间 [00:00:05, 00:00:07)
        track = [pt(0, 5, 0), pt(10, 5, 10)]
        a = area(gf="2026-01-01T00:00:05Z", gt="2026-01-01T00:00:07Z")
        res = analyze_request({"protected_areas": [a], "track_points": track})
        v = res["results"][0]["visits"]
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["entry_time"], "2026-01-01T00:00:05Z")
        self.assertEqual(v[0]["leave_time"], "2026-01-01T00:00:07Z")
        self.assertEqual(v[0]["dwell_seconds"], 2)

    def test_window_excludes_everything(self):
        track = [pt(0, 5, 0), pt(10, 5, 10)]
        a = area(gf="2026-01-02T00:00:00Z", gt="2026-01-02T01:00:00Z")
        res = analyze_request({"protected_areas": [a], "track_points": track})
        self.assertEqual(res["results"][0]["visits"], [])

    def test_abutting_effective_pieces_merge_single_area(self):
        # 一次停留 6 秒被生效区间裁出两段、但二者相接：不应出现（单区单窗口），
        # 这里验证 leave 恰好等于窗口右界时无越界片段。
        track = [pt(0, 5, 0), pt(10, 5, 10)]
        a = area(gf="2026-01-01T00:00:02Z", gt="2026-01-01T00:00:08Z")
        res = analyze_request({"protected_areas": [a], "track_points": track})
        v = res["results"][0]["visits"]
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["dwell_seconds"], 6)


class AntimeridianTests(unittest.TestCase):
    def test_unwrap_track_and_polygon(self):
        # 保护区跨 180°：经度从 175 向东到 -175（宽 10 度）
        geom = {"type": "Polygon", "coordinates": [[
            [175, -5], [-175, -5], [-175, 5], [175, 5], [175, -5]]]}
        a = area(geometry=geom)
        # 轨迹向东跨 180：170(t0) -> -170(t10)，每秒 2 度
        track = [{"lon": 170, "lat": 0, "time": T0},
                 {"lon": -170, "lat": 0, "time": "2026-01-01T00:00:10Z"}]
        res = analyze_request({"protected_areas": [a], "track_points": track})
        v = res["results"][0]["visits"]
        # 展开坐标：区 x∈[175,185]；轨迹 x:170→190；t 进入=(175-170)/2=2.5，离开=(185-170)/2=7.5
        self.assertEqual(len(v), 1)
        self.assertAlmostEqual(v[0]["dwell_seconds"], 5)
        self.assertEqual(v[0]["entry_time"], "2026-01-01T00:00:02.500Z")
        self.assertEqual(v[0]["leave_time"], "2026-01-01T00:00:07.500Z")

    def test_narrow_antimeridian_strip_excludes_zero(self):
        # 跨 180 的 10° 窄条：经度 0 不在其中
        geom = {"type": "Polygon", "coordinates": [[
            [-175, -5], [175, -5], [175, 5], [-175, 5], [-175, -5]]]}
        a = area(geometry=geom)
        track = [{"lon": -5, "lat": 0, "time": T0},
                 {"lon": 5, "lat": 0, "time": "2026-01-01T00:00:10Z"}]
        res = analyze_request({"protected_areas": [a], "track_points": track})
        self.assertEqual(res["results"][0]["visits"], [])

    def test_westward_crossing_through_strip(self):
        # 西向穿越窄条：起点 -170，向西走（原始经度增大方向回绕）到 170
        # 展开轨迹 x：-170 -> -190；条环的 k=-1 副本为 [-185,-175]
        geom = {"type": "Polygon", "coordinates": [[
            [-175, -5], [175, -5], [175, 5], [-175, 5], [-175, -5]]]}
        a = area(geometry=geom)
        track = [{"lon": -170, "lat": 0, "time": T0},
                 {"lon": 170, "lat": 0, "time": "2026-01-01T00:00:10Z"}]
        res = analyze_request({"protected_areas": [a], "track_points": track})
        v = res["results"][0]["visits"]
        self.assertEqual(len(v), 1)
        self.assertAlmostEqual(v[0]["dwell_seconds"], 5)
        self.assertEqual(v[0]["entry_time"], "2026-01-01T00:00:02.500Z")
        self.assertEqual(v[0]["leave_time"], "2026-01-01T00:00:07.500Z")

    def test_narrow_antimeridian_strip_excludes_zero(self):
        # 同样的原始角点、另一种环序表示跨 180 的 10° 窄条：经度 0 不在其中
        geom = {"type": "Polygon", "coordinates": [[
            [-175, -5], [175, -5], [175, 5], [-175, 5], [-175, -5]]]}
        a = area(geometry=geom)
        track = [{"lon": -5, "lat": 0, "time": T0},
                 {"lon": 5, "lat": 0, "time": "2026-01-01T00:00:10Z"}]
        res = analyze_request({"protected_areas": [a], "track_points": track})
        self.assertEqual(res["results"][0]["visits"], [])


class MultiPolygonTests(unittest.TestCase):
    def test_two_disjoint_polygons(self):
        geom = {"type": "MultiPolygon", "coordinates": [
            [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
            [[[8, 0], [10, 0], [10, 2], [8, 2], [8, 0]]],
        ]}
        a = area(geometry=geom)
        track = [pt(-1, 1, 0), pt(11, 1, 12)]
        res = analyze_request({"protected_areas": [a], "track_points": track})
        visits = res["results"][0]["visits"]
        self.assertEqual(len(visits), 2)
        self.assertEqual(visits[0]["dwell_seconds"], 2)
        self.assertEqual(visits[1]["dwell_seconds"], 2)


class ValidationTests(unittest.TestCase):
    def _analyze_expect_error(self, payload, fragment):
        with self.assertRaises(RequestError) as ctx:
            analyze_request(payload)
        self.assertIn(fragment, str(ctx.exception))

    def test_lon_out_of_range(self):
        payload = {"protected_areas": [area()],
                   "track_points": [pt(0, 5, 0), pt(181, 5, 10)]}
        self._analyze_expect_error(payload, "经度")

    def test_lat_out_of_range(self):
        payload = {"protected_areas": [area()],
                   "track_points": [pt(0, 91, 0), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "纬度")

    def test_time_not_increasing(self):
        payload = {"protected_areas": [area()],
                   "track_points": [pt(0, 5, 10), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "严格递增")

    def test_naive_time_rejected(self):
        payload = {"protected_areas": [area()],
                   "track_points": [{"lon": 0, "lat": 5, "time": "2026-01-01T00:00:00"},
                                    {"lon": 10, "lat": 5, "time": "2026-01-01T00:00:10Z"}]}
        self._analyze_expect_error(payload, "时区")

    def test_self_intersecting_ring(self):
        # 非零面积的蝴蝶结（边 0-1 与边 2-3 交叉）
        bowtie = {"type": "Polygon", "coordinates": [[
            [0, 0], [10, 10], [10, 2], [0, 10], [0, 0]]]}
        payload = {"protected_areas": [area(geometry=bowtie)],
                   "track_points": [pt(0, 5, 0), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "自相交")

    def test_unclosed_ring(self):
        geom = {"type": "Polygon", "coordinates": [[
            [0, 0], [10, 0], [10, 10], [0, 10]]]}
        payload = {"protected_areas": [area(geometry=geom)],
                   "track_points": [pt(0, 5, 0), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "闭合")

    def test_hole_outside(self):
        geom = {"type": "Polygon", "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]],
        ]}
        payload = {"protected_areas": [area(geometry=geom)],
                   "track_points": [pt(0, 5, 0), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "洞")

    def test_duplicate_area_id(self):
        payload = {"protected_areas": [area("A"), area("A")],
                   "track_points": [pt(0, 5, 0), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "重复")

    def test_bad_time_window(self):
        payload = {"protected_areas": [area(gf=T0, gt=T0)],
                   "track_points": [pt(0, 5, 0), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "valid_to")

    def test_unsupported_geometry_type(self):
        geom = {"type": "Point", "coordinates": [1, 2]}
        payload = {"protected_areas": [area(geometry=geom)],
                   "track_points": [pt(0, 5, 0), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "几何类型")

    def test_single_point_track(self):
        payload = {"protected_areas": [area()], "track_points": [pt(5, 5, 0)]}
        self._analyze_expect_error(payload, "至少需要 2")

    def test_multipolygon_overlap_rejected(self):
        geom = {"type": "MultiPolygon", "coordinates": [
            [[[0, 0], [6, 0], [6, 6], [0, 6], [0, 0]]],
            [[[4, 4], [10, 4], [10, 10], [4, 10], [4, 4]]],
        ]}
        payload = {"protected_areas": [area(geometry=geom)],
                   "track_points": [pt(0, 5, 0), pt(10, 5, 10)]}
        self._analyze_expect_error(payload, "几何无效")


class StationaryTests(unittest.TestCase):
    def test_dwell_inside(self):
        track = [pt(5, 5, 0), pt(5, 5, 10)]
        res = analyze_request({"protected_areas": [area()], "track_points": track})
        v = res["results"][0]["visits"]
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["dwell_seconds"], 10)

    def test_stationary_outside(self):
        track = [pt(50, 50, 0), pt(50, 50, 10)]
        res = analyze_request({"protected_areas": [area()], "track_points": track})
        self.assertEqual(res["results"][0]["visits"], [])


if __name__ == "__main__":
    unittest.main()
