"""协调认证、批次、测量契约、隔离复核与质量报告的应用服务。"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from typing import Any, Mapping, Sequence

from .analytics import confidence_interval, summarize_signal_profile, yield_rate
from .auth import Auth
from .contracts import (
    FREQUENCY_MAX_HZ,
    FREQUENCY_MIN_HZ,
    INSTRUMENT_PATTERN,
    Measurement,
    NOISE_MAX,
    NOISE_MIN,
    RESPONSE_MAX,
    RESPONSE_MIN,
    Violation,
    validate_batch,
    validate_measurement,
)
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .jsonio import content_digest
from .storage import connect, event, safe_json_dumps, transaction, utcnow

ANALYTICS_VERSION = "signal-profile-1"


class ComponentService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "component-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # ---------------------------------------------------------------- 批次

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, unit_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if (
            not isinstance(lot_id, str) or not lot_id.strip()
            or not isinstance(product, str) or not product.strip()
            or not isinstance(process_rev, str) or not process_rev.strip()
            or isinstance(unit_count, bool) or not isinstance(unit_count, int) or unit_count <= 0
        ):
            raise ValidationFailed([Violation("lot", "fields", "lot_id/product/process_rev 必须为非空字符串，unit_count 必须为正整数")])
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, unit_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(f"批次不存在: {lot_id}")
        return dict(row)

    # ------------------------------------------------------------- 仪器身份

    def register_instrument(self, token: str, instrument: str, vendor: str = "", model: str = "") -> dict:
        actor = self.auth.require(token, "measure")
        if not isinstance(instrument, str) or not INSTRUMENT_PATTERN.match(instrument.strip()):
            raise ValidationFailed([Violation("instrument", "format", "仪器身份不符合格式要求")])
        instrument = instrument.strip()
        now = utcnow()
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO registered_instruments(instrument,vendor,model,registered_by,registered_at) "
                    "VALUES(?,?,?,?,?)",
                    (instrument, vendor.strip(), model.strip(), actor.user_id, now),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"仪器已注册: {instrument}") from exc
        return {"instrument": instrument, "vendor": vendor.strip(), "model": model.strip(), "active": True}

    def _require_active_instruments(self, instruments: set[str]) -> list[Violation]:
        if not instruments:
            return []
        placeholders = ",".join("?" for _ in instruments)
        rows = self.db.execute(
            f"SELECT instrument FROM registered_instruments WHERE active=1 AND instrument IN ({placeholders})",
            tuple(instruments),
        ).fetchall()
        known = {row[0] for row in rows}
        return [
            Violation("instrument", "registered", f"仪器身份未注册或已停用: {name}")
            for name in sorted(instruments - known)
        ]

    # ------------------------------------------------------------- 测量写入

    def add_measurement(
        self, token: str, lot_id: str, payload: Mapping[str, Any], idempotency_key: str | None = None
    ) -> dict:
        if not isinstance(payload, Mapping):
            raise ValidationFailed([Violation("measurement", "type", "必须是 JSON 对象")])
        return self._write_measurements(token, lot_id, [dict(payload)], idempotency_key)

    def add_measurements(
        self, token: str, lot_id: str, payloads: Sequence[Mapping[str, Any]], idempotency_key: str
    ) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationFailed([Violation("idempotency_key", "required", "批量写入必须提供幂等键")])
        return self._write_measurements(token, lot_id, [dict(item) for item in payloads], idempotency_key.strip())

    def _write_measurements(
        self, token: str, lot_id: str, raw_rows: list[dict[str, Any]], idempotency_key: str | None
    ) -> dict:
        actor = self.auth.require(token, "measure")
        # 契约校验全部在写入边界之外完成：任何一条不合规，整批拒绝且不留下任何痕迹。
        result = validate_batch(raw_rows)
        if result.violations:
            raise ValidationFailed(result.violations)
        measurements: list[Measurement] = result.measurements
        request_digest = content_digest(raw_rows)
        scope = f"measurements:{lot_id}"
        try:
            with transaction(self.db):
                if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                    raise NotFound(f"批次不存在: {lot_id}")
                instrument_violations = self._require_active_instruments({item.instrument for item in measurements})
                if instrument_violations:
                    raise ValidationFailed(instrument_violations)
                if idempotency_key is not None:
                    stored = self.db.execute(
                        "SELECT request_sha256,response_json FROM measurement_idempotency WHERE scope=? AND key=?",
                        (scope, idempotency_key),
                    ).fetchone()
                    if stored is not None:
                        if stored["request_sha256"] != request_digest:
                            raise Conflict("同一幂等键对应了不同的测量内容")
                        return json.loads(stored["response_json"])
                now = utcnow()
                measurement_ids: list[str] = []
                for item in measurements:
                    measurement_id = uuid.uuid4().hex
                    self.db.execute(
                        "INSERT INTO measurements(measurement_id,lot_id,signal_frequency_hz,response,noise,"
                        "instrument,sample_id,operator,measured_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            measurement_id, lot_id, item.signal_frequency_hz, item.response, item.noise,
                            item.instrument, item.sample_id, actor.user_id, now,
                        ),
                    )
                    measurement_ids.append(measurement_id)
                    event(
                        self.db, lot_id, "measurement", actor.user_id,
                        {
                            "measurement_id": measurement_id,
                            "signal_frequency_hz": item.signal_frequency_hz,
                            "response": item.response,
                            "noise": item.noise,
                            "instrument": item.instrument,
                            **({"sample_id": item.sample_id} if item.sample_id else {}),
                        },
                    )
                if len(measurements) > 1:
                    event(
                        self.db, lot_id, "measurements.imported", actor.user_id,
                        {"inserted": len(measurements), "measurement_ids": measurement_ids,
                         "request_sha256": request_digest, **({"idempotency_key": idempotency_key} if idempotency_key else {})},
                    )
                response = {
                    "lot_id": lot_id,
                    "inserted": len(measurements),
                    "measurement_ids": measurement_ids,
                    "request_sha256": request_digest,
                }
                if idempotency_key is not None:
                    self.db.execute(
                        "INSERT INTO measurement_idempotency(scope,key,request_sha256,response_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (scope, idempotency_key, request_digest, safe_json_dumps(response), actor.user_id, now),
                    )
                return response
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次内存在重复采样或幂等键并发冲突") from exc

    def list_measurements(self, token: str, lot_id: str) -> list[dict]:
        """返回进入业务表的有效测量；隔离区记录不会混在这里。"""

        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT measurement_id,lot_id,signal_frequency_hz,response,noise,instrument,sample_id,"
            "operator,measured_at FROM measurements WHERE lot_id=? ORDER BY signal_frequency_hz,measurement_id",
            (lot_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------- 隔离与处置

    def scan_invalid_measurements(self, token: str, lot_id: str | None = None) -> dict:
        """识别业务表中的非法测量并隔离；每条隔离都写审计事件，绝不静默丢弃。"""

        actor = self.auth.require(token, "analyze")
        sql = (
            "SELECT m.*, r.instrument AS known_instrument FROM measurements m "
            "LEFT JOIN registered_instruments r ON r.instrument=m.instrument AND r.active=1 WHERE 1=1"
        )
        params: list[Any] = []
        if lot_id is not None:
            sql += " AND m.lot_id=?"
            params.append(lot_id)
        rules = (
            ("signal_frequency_hz", FREQUENCY_MIN_HZ, FREQUENCY_MAX_HZ),
            ("response", RESPONSE_MIN, RESPONSE_MAX),
            ("noise", NOISE_MIN, NOISE_MAX),
        )
        quarantined: list[dict] = []
        with transaction(self.db):
            now = utcnow()
            for row in self.db.execute(sql, params).fetchall():
                reasons: list[str] = []
                for name, low, high in rules:
                    value = row[name]
                    if not isinstance(value, float) or not math.isfinite(value):
                        reasons.append(f"{name} 违反 finite: 不是有限数值（实际 {value!r}）")
                    elif not low <= value <= high:
                        reasons.append(f"{name} 违反 range: 超出可接受量程 [{low}, {high}]（实际 {value}）")
                instrument = row["instrument"]
                if not isinstance(instrument, str) or not INSTRUMENT_PATTERN.match(instrument):
                    reasons.append("instrument 违反 format: 不符合仪器身份格式")
                elif row["known_instrument"] is None:
                    reasons.append(f"instrument 违反 registered: 仪器身份未注册或已停用: {instrument}")
                if not reasons:
                    continue
                payload = {
                    "measurement_id": row["measurement_id"], "lot_id": row["lot_id"],
                    "signal_frequency_hz": row["signal_frequency_hz"], "response": row["response"],
                    "noise": row["noise"], "instrument": row["instrument"],
                    "sample_id": row["sample_id"], "operator": row["operator"],
                    "measured_at": row["measured_at"],
                }
                cursor = self.db.execute(
                    "INSERT INTO measurement_quarantine(original_measurement_id,lot_id,payload_json,"
                    "reasons_json,source,detected_by,detected_at) VALUES(?,?,?,?, 'scan', ?,?)",
                    (
                        row["measurement_id"], row["lot_id"], safe_json_dumps(payload),
                        safe_json_dumps(reasons), actor.user_id, now,
                    ),
                )
                self.db.execute("DELETE FROM measurements WHERE measurement_id=?", (row["measurement_id"],))
                event(
                    self.db, row["lot_id"], "measurement.quarantined", actor.user_id,
                    {"measurement_id": row["measurement_id"], "quarantine_id": cursor.lastrowid,
                     "reasons": reasons, "source": "scan"},
                )
                quarantined.append(
                    {"measurement_id": row["measurement_id"], "lot_id": row["lot_id"], "reasons": reasons}
                )
        return {"scanned": "measurements", "quarantined": quarantined}

    def list_quarantine(self, token: str, lot_id: str | None = None, status: str | None = None) -> list[dict]:
        self.auth.require(token, "read")
        if status is not None and status not in {"pending", "discarded", "released"}:
            raise ValidationFailed([Violation("status", "allowed", "只能是 pending/discarded/released")])
        sql = "SELECT * FROM measurement_quarantine WHERE 1=1"
        params: list[Any] = []
        if lot_id is not None:
            sql += " AND lot_id=?"
            params.append(lot_id)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY quarantine_id"
        items = []
        for row in self.db.execute(sql, params).fetchall():
            item = dict(row)
            item["reasons"] = json.loads(item.pop("reasons_json"))
            item["payload"] = json.loads(item.pop("payload_json"))
            items.append(item)
        return items

    def resolve_quarantine(self, token: str, quarantine_id: int, decision: str, note: str) -> dict:
        """处置隔离记录：discard 留痕结案；release 必须重新通过契约并写回业务表。"""

        actor = self.auth.require(token, "approve")
        if decision not in {"discard", "release"} or not isinstance(note, str) or not note.strip():
            raise ValidationFailed([Violation("resolution", "required", "decision 必须是 discard/release 且需填写处置说明")])
        with transaction(self.db):
            row = self.db.execute(
                "SELECT * FROM measurement_quarantine WHERE quarantine_id=?", (quarantine_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"隔离记录不存在: {quarantine_id}")
            if row["status"] != "pending":
                raise InvalidState(f"隔离记录已处置：{row['status']}")
            now = utcnow()
            if decision == "discard":
                self.db.execute(
                    "UPDATE measurement_quarantine SET status='discarded',resolved_by=?,resolved_at=?,"
                    "resolution_note=? WHERE quarantine_id=?",
                    (actor.user_id, now, note.strip(), quarantine_id),
                )
                event(
                    self.db, row["lot_id"], "measurement.quarantine.discarded", actor.user_id,
                    {"measurement_id": row["original_measurement_id"], "quarantine_id": quarantine_id,
                     "note": note.strip()},
                )
                return {"quarantine_id": quarantine_id, "status": "discarded"}
            payload = json.loads(row["payload_json"])
            try:
                measurement = validate_measurement(payload)
            except ValidationFailed as exc:
                raise ValidationFailed(
                    [Violation(f"payload.{v.field}", v.rule, v.reason) for v in exc.violations]
                ) from exc
            instrument_violations = self._require_active_instruments({measurement.instrument})
            if instrument_violations:
                raise ValidationFailed(instrument_violations)
            exists = self.db.execute(
                "SELECT 1 FROM measurements WHERE lot_id=? AND signal_frequency_hz=? AND response=? "
                "AND noise=? AND instrument=?",
                (row["lot_id"], measurement.signal_frequency_hz, measurement.response,
                 measurement.noise, measurement.instrument),
            ).fetchone()
            if exists:
                raise Conflict("业务表中已存在等价测点，不能重复放回")
            self.db.execute(
                "INSERT INTO measurements(measurement_id,lot_id,signal_frequency_hz,response,noise,"
                "instrument,sample_id,operator,measured_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    row["original_measurement_id"], row["lot_id"], measurement.signal_frequency_hz,
                    measurement.response, measurement.noise, measurement.instrument,
                    payload.get("sample_id"), payload.get("operator") or actor.user_id,
                    payload.get("measured_at") or now,
                ),
            )
            self.db.execute(
                "UPDATE measurement_quarantine SET status='released',resolved_by=?,resolved_at=?,"
                "resolution_note=? WHERE quarantine_id=?",
                (actor.user_id, now, note.strip(), quarantine_id),
            )
            event(
                self.db, row["lot_id"], "measurement.quarantine.released", actor.user_id,
                {"measurement_id": row["original_measurement_id"], "quarantine_id": quarantine_id,
                 "note": note.strip()},
            )
            return {"quarantine_id": quarantine_id, "status": "released",
                    "measurement_id": row["original_measurement_id"]}

    # ---------------------------------------------------------------- 分析

    def _valid_rows(self, lot_id: str) -> list:
        return self.db.execute(
            "SELECT * FROM measurements WHERE lot_id=? ORDER BY signal_frequency_hz,measurement_id",
            (lot_id,),
        ).fetchall()

    def analyze(self, token: str, lot_id: str) -> dict:
        actor = self.auth.require(token, "analyze")
        rows = self._valid_rows(lot_id)
        if not rows and not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise NotFound(f"批次不存在: {lot_id}")
        if len(rows) < 3:
            raise InvalidState("至少需要三条有效测量才能形成质量结论")
        # 输入快照与摘要绑定：相同有效测量集合必然得到相同结论（等价重放稳定）。
        snapshot = [
            {
                "measurement_id": row["measurement_id"],
                "signal_frequency_hz": row["signal_frequency_hz"],
                "response": row["response"],
                "noise": row["noise"],
                "instrument": row["instrument"],
            }
            for row in rows
        ]
        input_digest = content_digest([snapshot])
        stored = self.db.execute(
            "SELECT analysis_id,result_json FROM quality_analyses WHERE lot_id=? AND input_sha256=?",
            (lot_id, input_digest),
        ).fetchone()
        if stored is not None:
            result = json.loads(stored["result_json"])
            result["analysis_id"] = stored["analysis_id"]
            return result
        summary = summarize_signal_profile([row["signal_frequency_hz"] for row in rows], [row["response"] for row in rows])
        lot = self.db.execute("SELECT unit_count FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        rates = yield_rate(lot["unit_count"], sum(1 for row in rows if row["response"] >= 0.8), 0)
        ci = confidence_interval([row["response"] for row in rows])
        result = {
            "lot_id": lot_id,
            "signal_profile": summary.__dict__,
            "yield": rates,
            "response_ci": ci,
            "valid_measurement_count": len(rows),
            "measurement_ids": [row["measurement_id"] for row in rows],
            "input_sha256": input_digest,
            "analytics_version": ANALYTICS_VERSION,
        }
        now = utcnow()
        with transaction(self.db):
            cursor = self.db.execute(
                "INSERT INTO quality_analyses(lot_id,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (lot_id, input_digest, safe_json_dumps(result), actor.user_id, now),
            )
            result["analysis_id"] = cursor.lastrowid
            self.db.execute(
                "UPDATE quality_analyses SET result_json=? WHERE analysis_id=?",
                (safe_json_dumps(result), cursor.lastrowid),
            )
            event(
                self.db, lot_id, "analysis.generated", actor.user_id,
                {"analysis_id": cursor.lastrowid, "input_sha256": input_digest,
                 "valid_measurement_count": len(rows), "analytics_version": ANALYTICS_VERSION},
            )
        return result

    def quality_report(self, token: str, lot_id: str) -> dict:
        """质量报告只引用业务表中可追溯的有效测量，并单列隔离区处置情况。"""

        self.auth.require(token, "read")
        lot_row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot_row is None:
            raise NotFound(f"批次不存在: {lot_id}")
        measurements = [
            {
                "measurement_id": row["measurement_id"],
                "signal_frequency_hz": row["signal_frequency_hz"],
                "response": row["response"],
                "noise": row["noise"],
                "instrument": row["instrument"],
                "sample_id": row["sample_id"],
                "operator": row["operator"],
                "measured_at": row["measured_at"],
            }
            for row in self._valid_rows(lot_id)
        ]
        quarantine_rows = self.db.execute(
            "SELECT status,count(*) AS n FROM measurement_quarantine WHERE lot_id=? GROUP BY status",
            (lot_id,),
        ).fetchall()
        analysis_row = self.db.execute(
            "SELECT analysis_id,result_json,created_at FROM quality_analyses WHERE lot_id=? "
            "ORDER BY analysis_id DESC LIMIT 1",
            (lot_id,),
        ).fetchone()
        return {
            "lot": dict(lot_row),
            "valid_measurements": measurements,
            "valid_measurement_count": len(measurements),
            "quarantine": {row["status"]: row["n"] for row in quarantine_rows},
            "analysis": None if analysis_row is None else json.loads(analysis_row["result_json"]),
        }

    # ---------------------------------------------------------------- 审批

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValidationFailed([Violation("decision", "required", "decision 必须是 release/hold/reject 且需填写理由")])
        with transaction(self.db):
            exists = self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not exists:
                raise NotFound(f"批次不存在: {lot_id}")
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
