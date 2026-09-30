"""仅依赖标准库的 HTTP 后备服务器。

用法：python -m app.stdlib_server
环境变量：API_HOST（默认 0.0.0.0）、API_PORT（默认 8080）。
生产环境建议使用 FastAPI（见 app/main.py 与 Dockerfile）。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .analysis import RequestError, analyze_request

API_VERSION = "1.0.0"


class Handler(BaseHTTPRequestHandler):
    server_version = f"marine-zone-crossing/{API_VERSION}"

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("", ):
            self._send_json(200, {"service": "marine-zone-crossing",
                                  "version": API_VERSION,
                                  "endpoint": "/api/v1/analyze"})
        elif self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "NotFound", "detail": f"未知路径 {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/v1/analyze":
            self._send_json(404, {"error": "NotFound", "detail": f"未知路径 {self.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "RequestError", "detail": "请求体不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "RequestError", "detail": "请求体必须为 JSON 对象"})
            return
        try:
            result = analyze_request(payload)
        except RequestError as exc:
            self._send_json(400, {"error": "RequestError", "detail": str(exc)})
            return
        self._send_json(200, result)

    def log_message(self, fmt: str, *args) -> None:
        # 保持简洁访问日志
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"marine-zone-crossing {API_VERSION} listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
