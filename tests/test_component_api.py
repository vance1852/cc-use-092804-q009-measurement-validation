from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from component_qualification.api import Handler
from component_qualification.service import ComponentService


def valid_sample(key: str = "S-001", **overrides) -> dict:
    row = {
        "sample_key": key,
        "signal_frequency_hz": 450e9,
        "response": 0.9,
        "noise": 0.02,
        "instrument": "spectrometer-1",
    }
    row.update(overrides)
    return row


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = ComponentService(":memory:")
        cls.service.bootstrap_admin()
        Handler.service = cls.service
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = cls.service.auth.login("admin", "component-admin")
        cls.service.register_instrument(cls.token, "spectrometer-1", "GS-FR", "国产平台")
        cls.service.create_lot(cls.token, "LOT-1", "sensor", "P1", 10)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str, body=None, headers=None):
        data = None
        req_headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode()
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=req_headers, method=method
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def raw_request(self, path: str, raw: bytes, headers=None):
        req_headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=raw, headers=req_headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health(self) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/health")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)

    def test_json_infinity_is_rejected_with_field_and_rule(self) -> None:
        raw = (
            b'{"sample_key":"S-INF","signal_frequency_hz":450000000000,'
            b'"response":Infinity,"noise":0.02,"instrument":"spectrometer-1"}'
        )
        status, body = self.raw_request("/lots/LOT-1/measurements", raw)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_failed")
        violations = body["error"]["violations"]
        self.assertEqual(violations[0]["field"], "measurements[0].response")
        self.assertEqual(violations[0]["rule"], "finite")

    def test_json_nan_is_rejected(self) -> None:
        raw = (
            b'{"sample_key":"S-NAN","signal_frequency_hz":450000000000,'
            b'"response":0.9,"noise":NaN,"instrument":"spectrometer-1"}'
        )
        status, body = self.raw_request("/lots/LOT-1/measurements", raw)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["violations"][0]["field"], "measurements[0].noise")
        self.assertEqual(body["error"]["violations"][0]["rule"], "finite")

    def test_numeric_string_is_rejected_at_type_rule(self) -> None:
        status, body = self.request("POST", "/lots/LOT-1/measurements",
                                    valid_sample("S-STR", response="0.9"))
        self.assertEqual(status, 422)
        violation = body["error"]["violations"][0]
        self.assertEqual(violation["field"], "measurements[0].response")
        self.assertEqual(violation["rule"], "type.number")

    def test_duplicate_json_key_is_syntax_error(self) -> None:
        raw = (
            b'{"sample_key":"S-DUPKEY","signal_frequency_hz":450000000000,'
            b'"response":0.9,"response":0.8,"noise":0.02,"instrument":"spectrometer-1"}'
        )
        status, body = self.raw_request("/lots/LOT-1/measurements", raw)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["violations"][0]["rule"], "json.syntax")

    def test_batch_is_atomic_and_replay_is_stable(self) -> None:
        rows = [valid_sample("S-B1"), valid_sample("S-B2", signal_frequency_hz=500e9)]
        status, first = self.request(
            "POST", "/lots/LOT-1/measurements", {"measurements": rows},
            headers={"Idempotency-Key": "batch-1"},
        )
        self.assertEqual(status, 201)
        status, second = self.request(
            "POST", "/lots/LOT-1/measurements", {"measurements": rows},
            headers={"Idempotency-Key": "batch-1"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(first, second)

        # 一条非法 → 整批失败。
        bad = [valid_sample("S-B3"), valid_sample("S-B4", response=9)]
        status, body = self.request(
            "POST", "/lots/LOT-1/measurements", {"measurements": bad},
            headers={"Idempotency-Key": "batch-2"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["violations"][0]["field"], "measurements[1].response")
        count = self.service.db.execute(
            "SELECT count(*) FROM measurements WHERE sample_key IN ('S-B3','S-B4')"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_batch_without_idempotency_header(self) -> None:
        rows = [valid_sample("S-NK1"), valid_sample("S-NK2", signal_frequency_hz=500e9)]
        status, body = self.request("POST", "/lots/LOT-1/measurements", {"measurements": rows})
        self.assertEqual(status, 422)

    def test_unknown_instrument_rejected(self) -> None:
        status, body = self.request("POST", "/lots/LOT-1/measurements",
                                    valid_sample("S-UK", instrument="rogue-1"))
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["violations"][0]["rule"], "instrument.registered")
        self.assertEqual(body["error"]["violations"][0]["field"], "measurements[0].instrument")

    def test_quarantine_scan_and_report_routes(self) -> None:
        # 三条有效测量后分析可用，并带可追溯证据。
        for key, freq, response in (("S-Q1", 450e9, 0.9), ("S-Q2", 500e9, 0.95), ("S-Q3", 550e9, 0.85)):
            status, _ = self.request("POST", "/lots/LOT-1/measurements",
                                     valid_sample(key, signal_frequency_hz=freq, response=response))
            self.assertEqual(status, 201)
        status, body = self.request("POST", "/lots/LOT-1/analysis")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["input_sha256"]), 64)
        status, report = self.request("GET", "/lots/LOT-1/quality-report")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(report["valid_measurement_count"], 3)
        self.assertIn("valid_measurement_ids", report["analysis"])
        status, scan = self.request("POST", "/lots/LOT-1/quarantine-scan")
        self.assertEqual(status, 200)
        self.assertEqual(scan["count"], 0)
        status, listing = self.request("GET", "/quarantine?lot_id=LOT-1")
        self.assertEqual(status, 200)
        self.assertEqual(listing["quarantined"], [])


if __name__ == "__main__":
    unittest.main()
