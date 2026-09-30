"""海洋测绘保护区穿越判定 HTTP API。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .analysis import AnalysisError, analyze
from .geopoly import GeometryError
from .schemas import AnalysisRequest

app = FastAPI(
    title="海洋保护区穿越判定 API",
    description="提交保护区（UTC 生效区间 + GeoJSON 多边形）与带时间戳的测线，"
    "返回进入/离开时刻与累计停留秒数。",
    version="1.0.0",
)


def _validation_body(exc) -> dict:
    details = []
    for err in exc.errors():
        msg = err.get("msg", "")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        details.append({
            "field": ".".join(str(x) for x in err.get("loc", []) if x != "body") or "body",
            "type": err.get("type"),
            "message": msg,
        })
    return {"error": "请求参数无效", "details": details}


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content=_validation_body(exc))


@app.exception_handler(ValidationError)
async def _pydantic_handler(request: Request, exc: ValidationError):
    return JSONResponse(status_code=400, content=_validation_body(exc))


@app.exception_handler(AnalysisError)
async def _analysis_handler(request: Request, exc: AnalysisError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(GeometryError)
async def _geometry_handler(request: Request, exc: GeometryError):
    return JSONResponse(status_code=400, content={"error": f"无效几何: {exc}"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/v1/analyze")
async def analyze_track(request: Request):
    # 先解析原始 JSON（非 JSON / 非对象 -> 400），再做 Pydantic 结构校验，
    # 最后做几何与时间的语义校验；任一失败均整体拒绝并指出原因。
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": f"请求体不是合法 JSON: {exc}"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象"})

    req = AnalysisRequest.model_validate(payload)
    if len(req.track) < 2:
        raise AnalysisError("track 至少需要 2 个按时间排列的点")
    result = analyze(req.areas, req.track)
    return {"results": result}
