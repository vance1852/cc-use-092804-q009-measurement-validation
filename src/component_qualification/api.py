"""无第三方依赖的 JSON HTTP API：在写入边界解析严格 JSON 并回传字段级违规。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .jsonio import StrictJsonError, strict_json_loads
from .service import ComponentService


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: dict[str, Any]


class ComponentApplication:
    """把 HTTP 路由映射到领域服务，便于不使用网络套接字进行测试。"""

    def __init__(self, service: ComponentService) -> None:
        self.service = service

    @staticmethod
    def _error(code: str, message: str, status: int, violations: list | None = None) -> Response:
        body: dict[str, Any] = {"error": {"code": code, "message": message}}
        if violations:
            body["error"]["violations"] = violations
        return Response(status, body)

    @staticmethod
    def _bearer(headers: Mapping[str, str]) -> str:
        return headers.get("authorization", "").removeprefix("Bearer ").strip()

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        query = parse_qs(urlparse(target).query)
        parts = [part for part in path.split("/") if part]
        token = self._bearer(normalized)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "component-qualification"})
            payload: Any = None
            if method in {"POST", "PUT", "PATCH"}:
                try:
                    payload = strict_json_loads(body) if body else {}
                except StrictJsonError as exc:
                    return self._error("invalid_json", str(exc), 400)
            if method == "POST" and path == "/login":
                return Response(
                    200,
                    {"token": self.service.auth.login(payload["user_id"], payload["password"])},
                )
            if method == "POST" and path == "/instruments":
                return Response(
                    201,
                    self.service.register_instrument(
                        token, payload["instrument"], payload.get("vendor", ""), payload.get("model", "")
                    ),
                )
            if method == "POST" and path == "/lots":
                return Response(
                    201,
                    self.service.create_lot(
                        token, payload["lot_id"], payload["product"],
                        payload["process_rev"], payload["unit_count"],
                    ),
                )
            if method == "GET" and len(parts) == 2 and parts[0] == "lots":
                return Response(200, self.service.get_lot(token, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return Response(200, {"measurements": self.service.list_measurements(token, parts[1])})
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                idem = normalized.get("idempotency-key", "").strip()
                if isinstance(payload, list):
                    result = self.service.add_measurements(token, parts[1], payload, idem)
                elif isinstance(payload, dict) and isinstance(payload.get("measurements"), list):
                    result = self.service.add_measurements(
                        token, parts[1], payload["measurements"], idem
                    )
                else:
                    result = self.service.add_measurement(token, parts[1], payload, idem or None)
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return Response(200, self.service.analyze(token, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "quality-report":
                return Response(200, self.service.quality_report(token, parts[1]))
            if method == "POST" and len(parts) == 4 and parts[0] == "lots" and parts[2] == "quarantine" and parts[3] == "scan":
                return Response(200, self.service.scan_invalid_measurements(token, parts[1]))
            if method == "GET" and len(parts) == 1 and parts[0] == "quarantine":
                lot_id = query.get("lot_id", [None])[0]
                status = query.get("status", [None])[0]
                return Response(200, {"items": self.service.list_quarantine(token, lot_id, status)})
            if method == "POST" and len(parts) == 3 and parts[0] == "quarantine" and parts[2] == "resolve":
                return Response(
                    200,
                    self.service.resolve_quarantine(
                        token, int(parts[1]), payload["decision"], payload.get("note", "")
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "lots" and parts[2] == "approval":
                return Response(
                    200,
                    self.service.approve(token, parts[1], payload["decision"], payload["reason"]),
                )
            return self._error("route_not_found", "接口不存在", 404)
        except ValidationFailed as exc:
            return self._error("validation_failed", str(exc), exc.status, exc.to_dicts())
        except ServiceError as exc:
            return self._error(exc.code, str(exc), exc.status)
        except (KeyError, TypeError) as exc:
            return self._error("invalid_request", f"请求字段缺失或类型错误: {exc}", 422)


def make_handler(application: ComponentApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ComponentQualification/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="启动国产电子部件质量服务")
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    service = ComponentService(args.database)
    service.bootstrap_admin()
    application = ComponentApplication(service)
    ThreadingHTTPServer((args.host, args.port), make_handler(application)).serve_forever()


if __name__ == "__main__":
    main()
