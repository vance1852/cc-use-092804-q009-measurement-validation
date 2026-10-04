"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .jsonio import JsonDataError, loads
from .service import ComponentService


class Handler(BaseHTTPRequestHandler):
    service = ComponentService()

    def _json(self, status: int, body: dict) -> None:
        # allow_nan=False：响应中也不允许出现 Infinity/NaN。
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not raw:
            return {}
        try:
            value = loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, JsonDataError) as exc:
            field = getattr(exc, "key", None)
            violations = [{"field": field or "$", "rule": "json.syntax", "message": str(exc)}]
            raise ValidationFailed("请求体不是合法 JSON", violations)
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象", [
                {"field": "$", "rule": "type.object", "message": "请求体必须是 JSON 对象"}
            ])
        return value

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, ServiceError):
            body: dict = {"error": {"code": exc.code, "message": str(exc)}}
            if exc.violations:
                body["error"]["violations"] = exc.violations
            return self._json(exc.status, body)
        if isinstance(exc, PermissionError):
            return self._json(403, {"error": {"code": "forbidden", "message": str(exc)}})
        if isinstance(exc, KeyError):
            field = exc.args[0] if exc.args else "$"
            return self._json(422, {"error": {
                "code": "validation_failed",
                "message": "请求缺少必填字段",
                "violations": [{"field": str(field), "rule": "required",
                                "message": f"{field} 为必填字段"}],
            }})
        return self._json(400, {"error": {"code": "bad_request", "message": str(exc)}})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = path.strip("/").split("/")
        query = parse_qs(parsed.query)
        if path == "/health":
            return self._json(200, {"status": "ok", "service": "component-qualification"})
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            # /lots/{id}
            if len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(token, parts[1]))
            # /lots/{id}/audit
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "audit":
                return self._json(200, {"events": self.service.audit(token, parts[1])})
            # /lots/{id}/quality-report
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "quality-report":
                return self._json(200, self.service.quality_report(token, parts[1]))
            # /quarantine?lot_id=...
            if path == "/quarantine":
                lot_id = query.get("lot_id", [None])[0]
                return self._json(200, {"quarantined": self.service.list_quarantine(token, lot_id)})
            return self._json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except Exception as exc:
            return self._error(exc)

    def do_POST(self):
        try:
            body = self._body()
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            parts = urlparse(self.path).path.strip("/").split("/")
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            if self.path == "/instruments":
                return self._json(201, self.service.register_instrument(
                    token, body["instrument_id"], body["model"], body["vendor"]))
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(
                    token, body["lot_id"], body["product"], body["process_rev"], body["unit_count"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                if "measurements" in body:
                    result = self.service.write_measurements(
                        token, parts[1], body["measurements"],
                        self.headers.get("Idempotency-Key"),
                    )
                else:
                    result = self.service.add_measurement(
                        token, parts[1], body["signal_frequency_hz"], body["response"],
                        body["noise"], body["instrument"], body.get("sample_key"),
                    )
                return self._json(201, result)
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.analyze(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "quarantine-scan":
                return self._json(200, self.service.scan_quarantine(token, parts[1]))
            if parts[0] == "quarantine" and len(parts) == 3 and parts[2] == "resolve":
                return self._json(200, self.service.resolve_quarantine(
                    token, int(parts[1]), body["decision"], body["note"]))
            return self._json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except Exception as exc:
            return self._error(exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    Handler.service = ComponentService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
