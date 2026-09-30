# 海洋测绘保护区穿越分析 HTTP API

给定若干**保护区**（编号、UTC 生效区间、GeoJSON `Polygon` / `MultiPolygon`）
与一条**按时间排列的经纬度测线**，计算测线每次进入/离开各保护区的 UTC
时刻与累计停留秒数。

- 相邻测线点之间：**时间匀速、纬度线性、经度沿较短方向（≤180°）插值**
- 保护区：**外环边界计入**；**洞的内部不计入，洞边界计入**
- 穿越结果与生效区间按 **左闭右开 `[valid_from, valid_to)`** 裁切
- 时间上**相接的有效片段合并**为一次停留
- **单点触碰（时长为 0）不计时长、不返回**
- 坐标越界 / 时间不递增 / 无效几何：**拒绝整次计算**（HTTP 400，中文原因）
- 正确处理跨 180° 经线（保护区与测线均支持）

---

## 1. 一键启动（推荐：Docker Compose）

```bash
docker compose up --build -d
```

启动后：

| 用途 | 地址 |
| --- | --- |
| 分析接口 | `http://localhost:8080/api/v1/analyze` |
| 健康检查 | `http://localhost:8080/health` |
| 交互式文档（Swagger） | `http://localhost:8080/docs` |
| OpenAPI 描述 | `http://localhost:8080/openapi.json` |

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down
```

### 仅用 Docker

```bash
docker build -t marine-zone-crossing .
docker run --rm -p 8080:8080 --name marine-zone-crossing marine-zone-crossing
```

### 修改端口

- Compose：启动前设置宿主机端口 `HOST_PORT=9000 docker compose up --build -d`
  （即 `http://localhost:9000`）
- 容器内监听端口由环境变量 `API_PORT` 控制，监听地址由 `API_HOST` 控制

构建阶段会自动执行单元测试（`tests/`），几何核心不通过则镜像构建失败。

---

## 2. 无 Docker / 无第三方依赖时本地运行

几何/时间核心 `app/geometry.py`、`app/analysis.py` **仅使用 Python 3.11+
标准库**；另附一个零依赖的 HTTP 后备服务，可直接运行：

```bash
# 仅标准库，无需 pip install
API_PORT=8080 python3 -m app.stdlib_server
```

生产模式（自动生成 OpenAPI 文档）使用 FastAPI：

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080
# 或：python3 -m app.main
```

运行测试（仅标准库）：

```bash
python3 -m unittest discover -s tests -v
```

---

## 3. 请求

`POST /api/v1/analyze`，`Content-Type: application/json`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `protected_areas` | 数组，必填非空 | 保护区列表 |
| `protected_areas[].id` | 字符串/数字 | 保护区编号，请求内唯一 |
| `protected_areas[].valid_from` | ISO 8601 | UTC 生效开始，**闭区间起点**，必须带时区（`Z` 或 `+08:00` 等） |
| `protected_areas[].valid_to` | ISO 8601 | UTC 生效结束，**开区间终点**，必须晚于 `valid_from` |
| `protected_areas[].geometry` | GeoJSON | 仅 `Polygon` 或 `MultiPolygon`；外环 + 洞均为合法简单线性环 |
| `track_points` | 数组，至少 2 点 | 测线点，**必须已按时间严格递增排列** |
| `track_points[].longitude` | number | 经度，`[-180, 180]`（别名 `lon`/`lng`/`long`） |
| `track_points[].latitude` | number | 纬度，`[-90, 90]`（别名 `lat`） |
| `track_points[].time` | ISO 8601 | 带时区的 UTC 时刻（别名 `timestamp`/`t`/`utc`） |

### 规则细节

- **插值**：相邻点时间段内位置按时间匀速：纬度对时间线性；经度先沿较短
  方向展开（东/西取经度差绝对值不超过 180° 的方向），再对时间线性。
- **洞**：在洞的内部不计停留；位于外环边界或洞边界上计入。
- **裁切**：某段停留与 `[valid_from, valid_to)` 取交（右端点排除），
  裁切后时间相接的片段合并；若仅剩一个零长度端点（如恰在 `valid_to`
  离开），不产生停留记录。
- **单点触碰**：测线只在保护区边界上相切一个时刻而不进入（含 V 形擦过
  顶点），停留时长为 0，不返回该次访问；但如果该点前后都是有效停留，
  会作为相接点把两段合并为一次。
- **无效输入**（整次请求拒绝，不做部分计算）：
  - 经纬度缺失、非有限数或越界
  - 测线时间相等/倒流、不带时区、无法解析
  - 几何不是 Polygon/MultiPolygon、环不闭合、点数不足、连续重复点、
    自相交/自接触、零面积、洞不在外环内、洞洞嵌套、MultiPolygon 外环
    交叉/重叠等
  - 保护区 `id` 重复、生效区间非正长度

### curl 示例

```bash
curl -sS -X POST http://localhost:8080/api/v1/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/request.json | python3 -m json.tool
```

跨 180° 经线示例：

```bash
curl -sS -X POST http://localhost:8080/api/v1/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/request_antimeridian.json | python3 -m json.tool
```

---

## 4. 响应

`200 OK`：

```json
{
  "track_point_count": 4,
  "results": [
    {
      "area_id": "PA-001",
      "effective_from": "2026-01-01T00:00:00Z",
      "effective_to": "2026-12-31T23:59:59Z",
      "visits": [
        {
          "entry_time": "2026-06-01T00:01:00Z",
          "leave_time": "2026-06-01T00:02:00Z",
          "dwell_seconds": 60.0
        }
      ],
      "total_dwell_seconds": 60.0
    },
    {
      "area_id": "PA-002-HOLE",
      "effective_from": "2026-06-01T00:00:00Z",
      "effective_to": "2026-06-30T23:59:59Z",
      "visits": [
        {"entry_time": "2026-06-01T00:04:45Z", "leave_time": "2026-06-01T00:06:33Z", "dwell_seconds": 108.0},
        {"entry_time": "2026-06-01T00:07:20Z", "leave_time": "2026-06-01T00:08:40Z", "dwell_seconds": 80.0}
      ],
      "total_dwell_seconds": 188.0
    }
  ]
}
```

- `visits` 按进入时刻升序；`entry_time` 为闭端点、`leave_time` 为开端点
  （即解释为 `[entry_time, leave_time)`）。
- 无停留时 `visits` 为空数组、`total_dwell_seconds` 为 0。
- 所有输出时间统一为 UTC（`...Z`）；输入中带任意时区偏移的时间会先转 UTC。

`400 Bad Request`（任何校验失败）：

```json
{
  "error": "RequestError",
  "detail": "测线点[3]：时间必须严格递增（2026-06-01T00:07:00Z 未晚于前一点）"
}
```

其他示例错误信息：

```json
{"error": "RequestError", "detail": "测线点[1]：经度 181.0 越界（允许 [-180, 180]）"}
{"error": "RequestError", "detail": "保护区 PA-1：Polygon 外环：环自相交（边 0 与边 2）"}
{"error": "RequestError", "detail": "保护区 PA-1：Polygon 洞[0] 不在外环内部"}
{"error": "RequestError", "detail": "保护区 PA-1：valid_to 必须晚于 valid_from"}
{"error": "RequestError", "detail": "track_points：单个点无法构成测线，至少需要 2 个点"}
```

---

## 5. 配置（环境变量）

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `API_HOST` | `0.0.0.0` | 服务监听地址 |
| `API_PORT` | `8080` | 服务监听端口（容器内） |
| `HOST_PORT` | `8080` | 仅 Compose 使用：宿主机映射端口 |

服务无状态、无数据库，无需初始化数据；镜像构建时即完成依赖安装与
算法自检。

---

## 6. 项目结构

```
.
├── app/
│   ├── geometry.py        # GeoJSON 校验、经度展开、点-环关系（纯标准库）
│   ├── analysis.py        # 时间/轨迹校验、分段穿越、裁切与合并（纯标准库）
│   ├── main.py            # FastAPI 应用（生产 HTTP 层）
│   └── stdlib_server.py   # 零依赖 HTTP 后备服务
├── tests/test_core.py     # 30 条算法/校验单元测试
├── examples/              # 普通请求与跨 180° 请求示例
├── requirements.txt       # fastapi / uvicorn / pydantic
├── Dockerfile             # 依赖安装 + 构建期测试自检
└── docker-compose.yml     # 一键启动、端口映射、健康检查
```
