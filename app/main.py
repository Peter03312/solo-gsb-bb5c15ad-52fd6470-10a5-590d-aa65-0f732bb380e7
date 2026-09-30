"""FastAPI 入口：POST /api/v1/analyze 穿越保护区分析。"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .analysis import RequestError, analyze_request

APP_NAME = "marine-zone-crossing"
API_VERSION = "1.0.0"

app = FastAPI(
    title="海洋测绘保护区穿越分析 API",
    version=API_VERSION,
    description=(
        "提交带 UTC 生效区间与 GeoJSON Polygon/MultiPolygon 的保护区，"
        "以及按时间递增的经纬度测线点；返回每个保护区内的进入/离开时刻"
        "与累计停留秒数。"
    ),
)


class ErrorBody(BaseModel):
    error: str
    detail: str


def _error_response(exc: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


@app.exception_handler(RequestError)
async def request_error_handler(_: Request, exc: RequestError) -> JSONResponse:
    return _error_response(exc, 400)


@app.get("/", tags=["meta"])
async def root():
    return {"service": APP_NAME, "version": API_VERSION,
            "docs": "/docs", "endpoint": "/api/v1/analyze"}


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "service": APP_NAME, "version": API_VERSION}


@app.post(
    "/api/v1/analyze",
    tags=["analysis"],
    summary="分析测线在各保护区内的停留区间",
    responses={400: {"model": ErrorBody, "description": "请求数据无效"}},
)
async def analyze(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return _error_response(RequestError("请求体不是合法 JSON"), 400)
    if not isinstance(payload, dict):
        return _error_response(RequestError("请求体必须为 JSON 对象"), 400)
    try:
        return analyze_request(payload)
    except RequestError as exc:
        return _error_response(exc, 400)


def main() -> None:  # pragma: no cover
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8080"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
