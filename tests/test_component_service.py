from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from component_qualification.errors import Conflict, InvalidState, ValidationFailed
from component_qualification.service import ComponentService


LEGACY_SCHEMA = """
CREATE TABLE chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 unit_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL,
 signal_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
"""


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComponentService(":memory:")
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "component-admin")
        self.service.register_instrument(self.token, "spectrometer-1", "GS-FR", "国产平台")
        self.service.create_lot(self.token, "LOT-1", "sensor", "P1", 10)

    def sample(self, key: str = "S-001", **overrides) -> dict:
        row = {
            "sample_key": key,
            "signal_frequency_hz": 450e9,
            "response": 0.9,
            "noise": 0.02,
            "instrument": "spectrometer-1",
        }
        row.update(overrides)
        return row

    def seed_three(self) -> None:
        self.service.write_measurements(self.token, "LOT-1", [
            self.sample("S-001"),
            self.sample("S-002", response=0.95, signal_frequency_hz=500e9),
            self.sample("S-003", response=0.85, signal_frequency_hz=550e9),
        ], idempotency_key="seed-1")


class WriteBoundaryTests(ServiceTestBase):
    def test_batch_success(self) -> None:
        result = self.service.write_measurements(self.token, "LOT-1",
                                                 [self.sample("S-001")], idempotency_key="k1")
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(len(result["request_sha256"]), 64)

    def test_one_bad_row_rolls_back_whole_batch_and_leaves_no_audit(self) -> None:
        rows = [self.sample("S-001"), self.sample("S-002", response="0.95")]
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.write_measurements(self.token, "LOT-1", rows, idempotency_key="k1")
        self.assertEqual(ctx.exception.violations[0]["field"], "measurements[1].response")
        self.assertEqual(ctx.exception.violations[0]["rule"], "type.number")
        count = self.service.db.execute("SELECT count(*) FROM measurements").fetchone()[0]
        self.assertEqual(count, 0)
        events = self.service.audit(self.token, "LOT-1")
        self.assertEqual([e["event_type"] for e in events], ["created"])

    def test_non_finite_and_out_of_range_rejected_at_boundary(self) -> None:
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(self.token, "LOT-1", 450e9, float("inf"), 0.02,
                                         "spectrometer-1", "S-INF")
        self.assertEqual(ctx.exception.violations[0]["rule"], "finite")
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.add_measurement(self.token, "LOT-1", 450e9, 3.0, 0.02,
                                         "spectrometer-1", "S-RANGE")
        self.assertEqual(ctx.exception.violations[0]["rule"], "range")
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM measurements").fetchone()[0], 0)

    def test_batch_without_idempotency_key_is_rejected_after_validation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.write_measurements(self.token, "LOT-1",
                                            [self.sample("S-001"), self.sample("S-002")])

    def test_duplicate_sample_key_is_rejected(self) -> None:
        self.service.write_measurements(self.token, "LOT-1", [self.sample("S-001")],
                                        idempotency_key="k1")
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.write_measurements(self.token, "LOT-1", [self.sample("S-001")],
                                            idempotency_key="k2")
        self.assertEqual(ctx.exception.violations[0]["rule"], "duplicate")

    def test_unregistered_instrument_rejected(self) -> None:
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.write_measurements(self.token, "LOT-1",
                                            [self.sample("S-001", instrument="ghost-9")],
                                            idempotency_key="k1")
        self.assertEqual(ctx.exception.violations[0]["rule"], "instrument.registered")


class IdempotencyTests(ServiceTestBase):
    def test_batch_replay_returns_stored_response(self) -> None:
        rows = [self.sample("S-001"), self.sample("S-002", signal_frequency_hz=500e9)]
        first = self.service.write_measurements(self.token, "LOT-1", rows, idempotency_key="k1")
        second = self.service.write_measurements(self.token, "LOT-1", rows, idempotency_key="k1")
        self.assertEqual(first, second)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM measurements").fetchone()[0], 2)

    def test_same_key_different_content_conflicts(self) -> None:
        self.service.write_measurements(self.token, "LOT-1", [self.sample("S-001")],
                                        idempotency_key="k1")
        with self.assertRaises(Conflict):
            self.service.write_measurements(self.token, "LOT-1",
                                            [self.sample("S-002")], idempotency_key="k1")

    def test_single_write_equivalent_replay_is_stable(self) -> None:
        first = self.service.add_measurement(self.token, "LOT-1", 450e9, 0.9, 0.02,
                                             "spectrometer-1", "S-001")
        second = self.service.add_measurement(self.token, "LOT-1", 450e9, 0.9, 0.02,
                                              "spectrometer-1", "S-001")
        self.assertEqual(first, second)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM measurements").fetchone()[0], 1)


class AnalysisTests(ServiceTestBase):
    def test_analysis_is_traceable(self) -> None:
        self.seed_three()
        result = self.service.analyze(self.token, "LOT-1")
        self.assertEqual(len(result["valid_measurement_ids"]), 3)
        self.assertEqual(len(result["input_sha256"]), 64)
        # 同样输入重复分析，摘要稳定。
        self.assertEqual(self.service.analyze(self.token, "LOT-1")["input_sha256"],
                         result["input_sha256"])

    def test_analysis_refuses_when_illegal_rows_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = build_legacy_db(directory, [
                ("m-good-1", 450e9, 0.9, 0.02),
                ("m-good-2", 500e9, 0.95, 0.02),
                ("m-good-3", 550e9, 0.85, 0.02),
            ])
            service = ComponentService(path)
            service.bootstrap_admin()
            token = service.auth.login("admin", "component-admin")
            # 迁移完成后，用旁路连接（foreign_keys=OFF）补入仪器身份缺失的记录。
            raw = sqlite3.connect(path)
            raw.execute(
                "INSERT INTO measurements(measurement_id,lot_id,sample_key,signal_frequency_hz,"
                "response,noise,instrument,operator,measured_at,content_sha256) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("m-ghost", "LOT-OLD", "S-GHOST", 450e9, 0.9, 0.02, "ghost-instrument",
                 "admin", "2026-09-03T00:00:00+00:00", "d" * 64),
            )
            raw.commit()
            raw.close()
            with self.assertRaises(InvalidState) as ctx:
                service.analyze(token, "LOT-OLD")
            self.assertTrue(any("m-ghost" in v["field"] for v in ctx.exception.violations))
            service.db.close()

    def test_quality_report_excludes_quarantined_but_records_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = build_legacy_db(directory, DEFAULT_LEGACY_ROWS)
            service = ComponentService(path)
            service.bootstrap_admin()
            token = service.auth.login("admin", "component-admin")
            self.assertEqual(service.db.execute(
                "SELECT count(*) FROM measurements").fetchone()[0], 1)
            report = service.quality_report(token, "LOT-OLD")
            self.assertEqual(report["valid_measurement_count"], 1)
            self.assertIn("unavailable", report["analysis"])
            self.assertEqual({q["measurement_id"] for q in report["quarantined"]},
                             {"m-inf", "m-range"})
            self.assertTrue(all(q["reasons"] for q in report["quarantined"]))
            service.db.close()

    def test_scan_is_idempotent_after_cleanup(self) -> None:
        self.seed_three()
        self.assertEqual(self.service.scan_quarantine(self.token, "LOT-1")["count"], 0)
        self.assertEqual(self.service.scan_quarantine(self.token, "LOT-1")["count"], 0)


class QuarantineResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        # 先迁移一个干净的旧库，再用旁路连接（foreign_keys=OFF）植入仪器身份缺失的记录，
        # 模拟迁移完成后库内仍存在/新混入的非法数据。
        path = build_legacy_db(self.directory.name, [
            ("m-good-1", 450e9, 0.9, 0.02),
            ("m-good-2", 500e9, 0.95, 0.02),
            ("m-good-3", 550e9, 0.85, 0.02),
        ])
        self.service = ComponentService(path)
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "component-admin")
        raw = sqlite3.connect(path)
        raw.execute(
            "INSERT INTO measurements(measurement_id,lot_id,sample_key,signal_frequency_hz,"
            "response,noise,instrument,operator,measured_at,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("m-ghost", "LOT-OLD", "S-GHOST", 450e9, 0.9, 0.02, "ghost-instrument",
             "admin", "2026-09-03T00:00:00+00:00", "d" * 64),
        )
        raw.commit()
        raw.close()
        self.path = path
        scan = self.service.scan_quarantine(self.token, "LOT-OLD")
        self.assertEqual(scan["count"], 1)
        self.quarantine_id = self.service.list_quarantine(self.token, "LOT-OLD")[0]["quarantine_id"]

    def tearDown(self) -> None:
        self.service.db.close()
        self.directory.cleanup()

    def test_discard_keeps_full_trail(self) -> None:
        result = self.service.resolve_quarantine(self.token, self.quarantine_id, "discard",
                                                 "仪器身份无法核实，作废该采样")
        self.assertEqual(result["status"], "discarded")
        item = self.service.list_quarantine(self.token, "LOT-OLD")[0]
        self.assertEqual(item["status"], "discarded")
        self.assertEqual(item["resolved_by"], "admin")
        self.assertIn("无法核实", item["resolution_note"])
        events = [e["event_type"] for e in self.service.audit(self.token, "LOT-OLD")]
        self.assertIn("measurement.quarantined", events)
        self.assertIn("measurement.discarded", events)
        payload = json.loads(self.service.audit(self.token, "LOT-OLD")[-1]["payload"])
        self.assertEqual(payload["measurement_id"], "m-ghost")

    def test_release_before_identity_repair_is_refused(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.resolve_quarantine(self.token, self.quarantine_id, "release", "尝试恢复")
        item = self.service.list_quarantine(self.token, "LOT-OLD")[0]
        self.assertEqual(item["status"], "quarantined")

    def test_release_succeeds_after_instrument_registered(self) -> None:
        self.service.register_instrument(self.token, "ghost-instrument", "GS-X", "国产平台")
        result = self.service.resolve_quarantine(self.token, self.quarantine_id, "release",
                                                 "仪器补登在册，采样数值有效")
        self.assertEqual(result["status"], "released")
        ids = {r[0] for r in self.service.db.execute(
            "SELECT measurement_id FROM measurements").fetchall()}
        self.assertIn("m-ghost", ids)
        events = [e["event_type"] for e in self.service.audit(self.token, "LOT-OLD")]
        self.assertIn("measurement.released", events)


def build_legacy_db(directory: str, rows: list[tuple]) -> str:
    """按旧版（无 sample_key、无量程约束）模式构造数据库文件。"""

    path = str(Path(directory) / "legacy.sqlite3")
    db = sqlite3.connect(path)  # 默认 foreign_keys=OFF，复现历史连接方式
    db.executescript(LEGACY_SCHEMA)
    db.execute(
        "INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)",
        ("LOT-OLD", "sensor", "P1", 10, "engineering", "admin",
         "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
    )
    for measurement_id, freq, response, noise in rows:
        db.execute(
            "INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)",
            (measurement_id, "LOT-OLD", freq, response, noise, "old-instrument",
             "admin", "2026-09-02T00:00:00+00:00"),
        )
    db.commit()
    db.close()
    return path


# 典型历史脏数据：量程溢出被测试台写成无穷大（1e999 → REAL Infinity）。
DEFAULT_LEGACY_ROWS = [
    ("m-good-1", 450e9, 0.9, 0.02),
    ("m-inf", 450e9, 1e999, 0.02),
    ("m-range", 450e9, 5.0, 0.02),
]


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_illegal_rows_are_quarantined_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = build_legacy_db(directory, DEFAULT_LEGACY_ROWS)
            service = ComponentService(path)
            service.bootstrap_admin()
            token = service.auth.login("admin", "component-admin")
            valid = service.db.execute(
                "SELECT measurement_id FROM measurements ORDER BY measurement_id"
            ).fetchall()
            self.assertEqual([r[0] for r in valid], ["m-good-1"])
            quarantined = service.list_quarantine(token, "LOT-OLD")
            by_id = {q["measurement_id"]: q for q in quarantined}
            self.assertEqual(set(by_id), {"m-inf", "m-range"})
            for item in quarantined:
                self.assertEqual(item["source"], "legacy_scan")
                self.assertEqual(item["status"], "quarantined")
                self.assertTrue(item["reasons"])
            self.assertEqual(by_id["m-inf"]["reasons"][0]["rule"], "finite")
            self.assertEqual(by_id["m-inf"]["snapshot"]["response"], "__infinity__")
            self.assertEqual(by_id["m-range"]["reasons"][0]["rule"], "range")
            events = service.audit(token, "LOT-OLD")
            self.assertEqual(sum(1 for e in events if e["event_type"] == "measurement.quarantined"), 2)
            # 审计链上不出现未隔离测量被导入的事件。
            self.assertFalse(any(e["event_type"] == "measurements.imported" for e in events))
            # 旧仪器被自动登记，合法历史测量的身份仍可追溯。
            self.assertTrue(service.db.execute(
                "SELECT 1 FROM instruments WHERE instrument_id='old-instrument'").fetchone())
            service.db.close()

            # 重复打开不产生重复隔离（迁移幂等）。
            service2 = ComponentService(path)
            service2.bootstrap_admin()
            token2 = service2.auth.login("admin", "component-admin")
            self.assertEqual(len(service2.list_quarantine(token2, "LOT-OLD")), 2)
            self.assertEqual(service2.db.execute(
                "SELECT count(*) FROM measurements").fetchone()[0], 1)
            service2.db.close()

    def test_legacy_row_count_is_conserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = build_legacy_db(directory, DEFAULT_LEGACY_ROWS)
            service = ComponentService(path)
            total_valid = service.db.execute("SELECT count(*) FROM measurements").fetchone()[0]
            total_quarantine = service.db.execute(
                "SELECT count(*) FROM quarantined_measurements"
            ).fetchone()[0]
            self.assertEqual(total_valid + total_quarantine, 3)

    def test_legacy_valid_rows_keep_identity_and_gain_sample_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = build_legacy_db(directory, [
                ("m-good-1", 450e9, 0.9, 0.02),
                ("m-good-2", 500e9, 0.95, 0.02),
                ("m-good-3", 550e9, 0.85, 0.02),
            ])
            service = ComponentService(path)
            service.bootstrap_admin()
            token = service.auth.login("admin", "component-admin")
            result = service.analyze(token, "LOT-OLD")
            self.assertEqual(len(result["valid_measurement_ids"]), 3)
            keys = {r[0] for r in service.db.execute(
                "SELECT sample_key FROM measurements").fetchall()}
            self.assertEqual(keys, {"legacy-m-good-1", "legacy-m-good-2", "legacy-m-good-3"})


if __name__ == "__main__":
    unittest.main()
