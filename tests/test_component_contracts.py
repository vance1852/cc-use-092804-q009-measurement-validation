from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from component_qualification.api import ComponentApplication
from component_qualification.contracts import validate_batch, validate_measurement
from component_qualification.errors import (
    Conflict,
    InvalidState,
    NotFound,
    ValidationFailed,
)
from component_qualification.jsonio import StrictJsonError, content_digest, strict_json_loads
from component_qualification.service import ComponentService


class ContractTests(unittest.TestCase):
    def row(self, **overrides):
        data = {
            "signal_frequency_hz": 500,
            "response": 0.9,
            "noise": 0.02,
            "instrument": "spectrometer-1",
        }
        data.update(overrides)
        return data

    def test_valid_measurement(self) -> None:
        item = validate_measurement(self.row())
        self.assertEqual(item.signal_frequency_hz, 500.0)
        self.assertEqual(item.instrument, "spectrometer-1")

    def test_non_finite_and_string_disguise_and_bool_are_rejected(self) -> None:
        cases = {
            "signal_frequency_hz": [float("inf"), float("-inf"), float("nan"), "500", True],
            "response": ["0.9", Decimal("NaN")],
            "noise": [None, b"x", []],
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaises(ValidationFailed) as caught:
                        validate_measurement(self.row(**{field: value}))
                    self.assertTrue(
                        any(v.field == field for v in caught.exception.violations),
                        caught.exception,
                    )

    def test_ranges_are_enforced_with_rule_names(self) -> None:
        with self.assertRaises(ValidationFailed) as caught:
            validate_measurement(self.row(signal_frequency_hz=0.0))
        self.assertEqual(caught.exception.violations[0].rule, "frequency_range")
        with self.assertRaises(ValidationFailed) as caught:
            validate_measurement(self.row(response=1.5))
        self.assertEqual(caught.exception.violations[0].rule, "response_range")
        with self.assertRaises(ValidationFailed) as caught:
            validate_measurement(self.row(noise=-0.1))
        self.assertEqual(caught.exception.violations[0].rule, "noise_range")
        # 边界值合法
        validate_measurement(self.row(signal_frequency_hz=1e9, response=1.0, noise=0.0))

    def test_instrument_identity_rules(self) -> None:
        for bad in ("", "  ", "bad id", "带中文", "a" * 65, 123):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValidationFailed) as caught:
                    validate_measurement(self.row(instrument=bad))
                self.assertEqual(caught.exception.violations[0].field, "instrument")

    def test_batch_collects_every_violation(self) -> None:
        rows = [
            self.row(signal_frequency_hz=float("inf"), response="x"),
            self.row(instrument=""),
            "not-an-object",
        ]
        result = validate_batch(rows)
        self.assertFalse(result.ok)
        fields = {(v.index, v.field) for v in result.violations}
        self.assertIn((0, "signal_frequency_hz"), fields)
        self.assertIn((0, "response"), fields)
        self.assertIn((1, "instrument"), fields)
        self.assertIn((2, "measurement"), fields)

    def test_duplicate_sample_by_id_and_by_fingerprint(self) -> None:
        result = validate_batch([
            self.row(sample_id="s-1"),
            self.row(signal_frequency_hz=501, sample_id="s-1"),
        ])
        self.assertTrue(any(v.rule == "duplicate_sample" for v in result.violations))
        result = validate_batch([self.row(), self.row()])
        self.assertTrue(any(v.rule == "duplicate_sample" for v in result.violations))
        # 测点指纹不同即视为不同采样
        result = validate_batch([self.row(), self.row(signal_frequency_hz=501)])
        self.assertTrue(result.ok)


class StrictJsonTests(unittest.TestCase):
    def test_constants_and_duplicate_keys_rejected(self) -> None:
        for text in ("Infinity", "-Infinity", "NaN", '{"a":1,"a":2}'):
            with self.subTest(text=text):
                with self.assertRaises(StrictJsonError):
                    strict_json_loads(text)

    def test_floats_keep_decimal_precision(self) -> None:
        value = strict_json_loads('{"x": 0.1}')
        self.assertEqual(value["x"], Decimal("0.1"))

    def test_canonical_digest_is_stable(self) -> None:
        rows = [{"b": 1, "a": [1, 2]}, {"a": [1, 2], "b": 1}]
        self.assertEqual(content_digest(rows[:1]), content_digest(rows[1:]))
        with self.assertRaises(ValueError):
            content_digest([{"x": float("inf")}])


def admin_service() -> tuple[ComponentService, str]:
    service = ComponentService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "component-admin")
    service.create_lot(token, "LOT-1", "CMOS image sensor", "P3.2", 10)
    service.register_instrument(token, "spectrometer-1")
    return service, token


def m(**overrides):
    data = {
        "signal_frequency_hz": 500,
        "response": 0.9,
        "noise": 0.02,
        "instrument": "spectrometer-1",
    }
    data.update(overrides)
    return data


class ServiceWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.token = admin_service()

    def test_single_and_batch_success(self) -> None:
        result = self.service.add_measurement(self.token, "LOT-1", m(sample_id="s1"))
        self.assertEqual(result["inserted"], 1)
        batch = self.service.add_measurements(
            self.token,
            "LOT-1",
            [m(signal_frequency_hz=510, sample_id="s2"), m(signal_frequency_hz=520, sample_id="s3")],
            idempotency_key="k1",
        )
        self.assertEqual(batch["inserted"], 2)
        self.assertEqual(len(self.service.list_measurements(self.token, "LOT-1")), 3)

    def test_invalid_single_leaves_no_row_and_no_audit(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.add_measurement(self.token, "LOT-1", m(response=float("inf")))
        self.assertEqual(self.service.list_measurements(self.token, "LOT-1"), [])
        self.assertEqual(
            [e for e in self.service.audit(self.token, "LOT-1") if e["event_type"] != "created"], []
        )

    def test_batch_failure_is_atomic(self) -> None:
        rows = [
            m(sample_id="ok-1"),
            m(signal_frequency_hz=510, response="0.99", sample_id="bad-1"),
            m(signal_frequency_hz=520, sample_id="ok-2"),
        ]
        with self.assertRaises(ValidationFailed) as caught:
            self.service.add_measurements(self.token, "LOT-1", rows, idempotency_key="k2")
        violations = caught.exception.violations
        self.assertEqual([v.field for v in violations], ["response"])
        self.assertEqual(violations[0].index, 1)
        self.assertEqual(self.service.list_measurements(self.token, "LOT-1"), [])
        self.assertEqual(
            [e for e in self.service.audit(self.token, "LOT-1") if e["event_type"] != "created"], []
        )

    def test_unregistered_instrument_rejected_at_boundary(self) -> None:
        with self.assertRaises(ValidationFailed) as caught:
            self.service.add_measurement(self.token, "LOT-1", m(instrument="unknown-rig"))
        self.assertEqual(caught.exception.violations[0].rule, "registered")
        self.assertEqual(self.service.list_measurements(self.token, "LOT-1"), [])

    def test_idempotent_replay_returns_stable_result(self) -> None:
        rows = [m(signal_frequency_hz=510, sample_id="s2"), m(signal_frequency_hz=520, sample_id="s3")]
        first = self.service.add_measurements(self.token, "LOT-1", rows, idempotency_key="k3")
        second = self.service.add_measurements(self.token, "LOT-1", rows, idempotency_key="k3")
        self.assertEqual(first, second)
        self.assertEqual(len(self.service.list_measurements(self.token, "LOT-1")), 2)
        events = self.service.audit(self.token, "LOT-1")
        self.assertEqual(sum(e["event_type"] == "measurements.imported" for e in events), 1)
        with self.assertRaises(Conflict):
            changed = [dict(rows[0]), m(signal_frequency_hz=600, sample_id="s4")]
            self.service.add_measurements(self.token, "LOT-1", changed, idempotency_key="k3")

    def test_duplicate_sample_against_persisted_data_conflicts(self) -> None:
        self.service.add_measurement(self.token, "LOT-1", m(sample_id="dup"))
        with self.assertRaises(Conflict):
            self.service.add_measurement(self.token, "LOT-1", m(sample_id="dup"))
        with self.assertRaises(Conflict):
            self.service.add_measurement(self.token, "LOT-1", m())

    def test_unknown_lot_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.add_measurement(self.token, "NOPE", m())

    def test_analysis_and_report_use_only_valid_traceable_rows(self) -> None:
        for freq in (450, 520, 650):
            self.service.add_measurement(
                self.token, "LOT-1", m(signal_frequency_hz=freq, sample_id=f"s-{freq}")
            )
        first = self.service.analyze(self.token, "LOT-1")
        second = self.service.analyze(self.token, "LOT-1")
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        self.assertEqual(first["valid_measurement_count"], 3)
        report = self.service.quality_report(self.token, "LOT-1")
        self.assertEqual(report["valid_measurement_count"], 3)
        self.assertEqual(len(report["valid_measurements"]), 3)
        with self.assertRaises(InvalidState):
            self.service.create_lot(self.token, "LOT-2", "x", "P1", 5)
            self.service.analyze(self.token, "LOT-2")


class QuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.token = admin_service()

    def _bypass_insert(self, measurement_id, *, response=0.9, instrument="ghost-rig",
                       frequency=500.0, noise=0.02):
        # 模拟绕过写入边界落库的存量记录（CHECK 只兜得住量程，未注册仪器仍能落表）。
        self.service.db.execute(
            "INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?)",
            (measurement_id, "LOT-1", frequency, response, noise, instrument, None,
             "admin", "2026-09-01T00:00:00+00:00"),
        )
        self.service.db.commit()

    def test_scan_quarantines_invalid_and_keeps_trail(self) -> None:
        self._bypass_insert("m-bad")
        self._bypass_insert("m-ok", response=0.7, frequency=460, instrument="spectrometer-1")
        result = self.service.scan_invalid_measurements(self.token, "LOT-1")
        self.assertEqual([item["measurement_id"] for item in result["quarantined"]], ["m-bad"])
        remaining = {row["measurement_id"] for row in self.service.list_measurements(self.token, "LOT-1")}
        self.assertEqual(remaining, {"m-ok"})
        items = self.service.list_quarantine(self.token, "LOT-1")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "pending")
        self.assertIn("registered", items[0]["reasons"][0])
        events = [e for e in self.service.audit(self.token, "LOT-1")
                  if e["event_type"] == "measurement.quarantined"]
        self.assertEqual(len(events), 1)
        json.loads(events[0]["payload"])

    def test_discard_resolution_leaves_history(self) -> None:
        self._bypass_insert("m-bad")
        self.service.scan_invalid_measurements(self.token, "LOT-1")
        qid = self.service.list_quarantine(self.token, "LOT-1")[0]["quarantine_id"]
        resolved = self.service.resolve_quarantine(self.token, qid, "discard", "台架量程溢出，废弃")
        self.assertEqual(resolved["status"], "discarded")
        item = self.service.list_quarantine(self.token, "LOT-1", "discarded")[0]
        self.assertEqual(item["resolved_by"], "admin")
        self.assertTrue(
            any(e["event_type"] == "measurement.quarantine.discarded"
                for e in self.service.audit(self.token, "LOT-1"))
        )
        with self.assertRaises(InvalidState):
            self.service.resolve_quarantine(self.token, qid, "discard", "再次处置")

    def test_release_requires_revalidation(self) -> None:
        from component_qualification.storage import safe_json_dumps

        # 迁移期隔离的 Infinity 记录：release 必须重新通过契约，不能直接回业务表。
        payload = {
            "measurement_id": "m-inf", "lot_id": "LOT-1", "signal_frequency_hz": 500,
            "response": float("inf"), "noise": 0.02, "instrument": "spectrometer-1",
            "sample_id": None, "operator": "admin", "measured_at": "2026-09-01T00:00:00+00:00",
        }
        cur = self.service.db.execute(
            "INSERT INTO measurement_quarantine(original_measurement_id,lot_id,payload_json,"
            "reasons_json,source,detected_by,detected_at) VALUES(?,?,?,?, 'schema_migration', 'system','t')",
            ("m-inf", "LOT-1", safe_json_dumps(payload), safe_json_dumps(["response 违反 finite"])),
        )
        self.service.db.commit()
        with self.assertRaises(ValidationFailed) as caught:
            self.service.resolve_quarantine(self.token, cur.lastrowid, "release", "想直接放回")
        self.assertTrue(any(v.field.startswith("payload.response") for v in caught.exception.violations))
        self.assertEqual(
            self.service.list_measurements(self.token, "LOT-1"), []
        )

    def test_scan_is_idempotent_when_nothing_invalid(self) -> None:
        for freq in (450, 520, 650):
            self.service.add_measurement(
                self.token, "LOT-1", m(signal_frequency_hz=freq, sample_id=f"s-{freq}")
            )
        result = self.service.scan_invalid_measurements(self.token, "LOT-1")
        self.assertEqual(result["quarantined"], [])


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_inf_rows_are_quarantined_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.sqlite3")
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE chip_lots(
                 lot_id TEXT PRIMARY KEY, product TEXT, process_rev TEXT, unit_count INTEGER,
                 status TEXT, owner TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE measurements(
                 measurement_id TEXT PRIMARY KEY, lot_id TEXT, signal_frequency_hz REAL, response REAL,
                 noise REAL, instrument TEXT, operator TEXT, measured_at TEXT);
                CREATE TABLE lot_events(
                 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT, event_type TEXT,
                 actor TEXT, payload TEXT, created_at TEXT);
                CREATE TABLE approvals(
                 lot_id TEXT, reviewer TEXT, decision TEXT, reason TEXT, created_at TEXT);
                """
            )
            db.execute(
                "INSERT INTO chip_lots VALUES('LOT-1','CMOS image sensor','P3.2',10,'engineering',"
                "'admin','t','t')"
            )
            db.execute(
                "INSERT INTO measurements VALUES('m-ok','LOT-1',500,0.9,0.02,'spec-1','admin','t')"
            )
            db.execute(
                "INSERT INTO measurements VALUES('m-inf','LOT-1',1e300,'Infinity',0.02,'spec-1','admin','t')"
            )
            # 字符串 Infinity 经旧 float() 转换后绑定
            db.execute(
                "INSERT INTO measurements VALUES('m-inf2','LOT-1',500,?,0.02,'spec-1','admin','t')",
                (float("inf"),),
            )
            db.commit()
            db.close()

            service = ComponentService(path)
            service.bootstrap_admin()
            token = service.auth.login("admin", "component-admin")
            measurements = service.list_measurements(token, "LOT-1")
            self.assertEqual([row["measurement_id"] for row in measurements], ["m-ok"])
            items = service.list_quarantine(token, "LOT-1")
            self.assertEqual({item["original_measurement_id"] for item in items}, {"m-inf", "m-inf2"})
            self.assertTrue(all(item["source"] == "schema_migration" for item in items))
            events = service.audit(token, "LOT-1")
            self.assertTrue(any(e["event_type"] == "measurement.quarantined" for e in events))
            # 历史合法仪器自动补登记，重复扫描不再隔离
            service.scan_invalid_measurements(token, "LOT-1")
            self.assertEqual(len(service.list_quarantine(token)), 2)
            service.db.close()


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.token = admin_service()
        self.app = ComponentApplication(self.service)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def request(self, method, path, body=None, raw: bytes | None = None, headers=None):
        if raw is None and body is not None:
            raw = json.dumps(body).encode()
        merged = dict(self.headers)
        merged.update(headers or {})
        return self.app.handle(method, path, merged, raw or b"")

    def test_field_level_violation_shape(self) -> None:
        response = self.request(
            "POST", "/lots/LOT-1/measurements",
            body={"signal_frequency_hz": 0, "response": 2.0, "noise": "x",
                  "instrument": "spectrometer-1"},
        )
        self.assertEqual(response.status, 422)
        error = response.body["error"]
        self.assertEqual(error["code"], "validation_failed")
        rules = {(v["field"], v["rule"]) for v in error["violations"]}
        self.assertIn(("signal_frequency_hz", "frequency_range"), rules)
        self.assertIn(("response", "response_range"), rules)
        self.assertIn(("noise", "type"), rules)

    def test_non_finite_json_rejected_at_boundary(self) -> None:
        response = self.request(
            "POST", "/lots/LOT-1/measurements",
            raw=b'{"signal_frequency_hz":Infinity,"response":0.9,"noise":0.02,'
            b'"instrument":"spectrometer-1"}',
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body["error"]["code"], "invalid_json")

    def test_batch_endpoint_atomic_and_idempotent(self) -> None:
        rows = [
            {"signal_frequency_hz": 450, "response": .71, "noise": .01,
             "instrument": "spectrometer-1", "sample_id": "a1"},
            {"signal_frequency_hz": 520, "response": .93, "noise": .02,
             "instrument": "spectrometer-1", "sample_id": "a2"},
        ]
        first = self.request(
            "POST", "/lots/LOT-1/measurements", body=rows,
            headers={"Idempotency-Key": "batch-1"},
        )
        self.assertEqual(first.status, 201)
        second = self.request(
            "POST", "/lots/LOT-1/measurements", body=rows,
            headers={"Idempotency-Key": "batch-1"},
        )
        self.assertEqual(second.body, first.body)
        bad = self.request(
            "POST", "/lots/LOT-1/measurements",
            body=[{"measurements": rows}], headers={"Idempotency-Key": "batch-1"},
        )
        # 数组元素必须是对象
        self.assertEqual(bad.status, 422)

    def test_quality_report_route(self) -> None:
        for freq in (450, 520, 650):
            self.request(
                "POST", "/lots/LOT-1/measurements",
                body={"signal_frequency_hz": freq, "response": .9, "noise": .01,
                      "instrument": "spectrometer-1", "sample_id": f"x{freq}"},
            )
        response = self.request("GET", "/lots/LOT-1/quality-report")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["valid_measurement_count"], 3)

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", self.headers)
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
