"""协调认证、仪器台账、批次、测量写入边界、隔离复核与质量报告的应用服务。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping, Sequence

from .analytics import confidence_interval, summarize_signal_profile, yield_rate
from .auth import Auth
from .contracts import Measurement, evaluate_stored, validate_batch
from .errors import Conflict, InvalidState, NotFound, ServiceError, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import connect, event, transaction, utcnow

_NAMESPACE = uuid.UUID("6f3f2d17-9b4a-4c3e-8f2a-1c5d7e9b0a12")


class ComponentService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    # ------------------------------------------------------------------ 基础
    def bootstrap_admin(self, user_id: str = "admin", password: str = "component-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def _lot_row(self, lot_id: str) -> Mapping[str, Any]:
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(f"批次不存在: {lot_id}")
        return row

    def _active_instruments(self) -> frozenset[str]:
        return frozenset(
            row[0]
            for row in self.db.execute(
                "SELECT instrument_id FROM instruments WHERE status='active'"
            ).fetchall()
        )

    def _known_instruments(self) -> frozenset[str]:
        """台账中存在的全部仪器（含已停用），用于库内记录复核。"""

        return frozenset(
            row[0] for row in self.db.execute("SELECT instrument_id FROM instruments").fetchall()
        )

    def register_instrument(
        self, token: str, instrument_id: str, model: str, vendor: str
    ) -> dict:
        actor = self.auth.require(token, "submit")
        instrument_id = (instrument_id or "").strip()
        if not instrument_id or not model.strip() or not vendor.strip():
            raise ValidationFailed("instrument_id、model、vendor 均不能为空")
        now = utcnow()
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO instruments VALUES(?,?,?,?,?,?)",
                    (instrument_id, model.strip(), vendor.strip(), "active", actor.user_id, now),
                )
        except Exception as exc:
            raise Conflict(f"仪器已登记: {instrument_id}") from exc
        return {"instrument_id": instrument_id, "status": "active", "model": model.strip()}

    def retire_instrument(self, token: str, instrument_id: str) -> dict:
        actor = self.auth.require(token, "submit")
        with transaction(self.db):
            cursor = self.db.execute(
                "UPDATE instruments SET status='retired' WHERE instrument_id=? AND status='active'",
                (instrument_id,),
            )
            if cursor.rowcount != 1:
                raise NotFound(f"在用仪器不存在: {instrument_id}")
        return {"instrument_id": instrument_id, "status": "retired"}

    # ------------------------------------------------------------------ 批次
    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, unit_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if (
            not isinstance(unit_count, int)
            or isinstance(unit_count, bool)
            or unit_count <= 0
            or not lot_id.strip()
            or not process_rev.strip()
        ):
            raise ValidationFailed("lot_id、process_rev 不能为空，unit_count 必须为正整数")
        now = utcnow()
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)",
                    (lot_id.strip(), product, process_rev.strip(), unit_count,
                     "engineering", actor.user_id, now, now),
                )
                event(self.db, lot_id.strip(), "created", actor.user_id,
                      {"product": product, "process_rev": process_rev.strip()})
        except Exception as exc:
            raise Conflict(f"批次已存在: {lot_id}") from exc
        return self.get_lot(token, lot_id.strip())

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        return dict(self._lot_row(lot_id))

    # ------------------------------------------------------------ 测量写入
    @staticmethod
    def _measurement_id(lot_id: str, item: Measurement) -> str:
        return uuid.uuid5(_NAMESPACE, f"measurement:{lot_id}:{item.sample_key}").hex

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict | None:
        row = self.db.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同的测量内容")
        return json.loads(row["response_json"])

    def write_measurements(
        self,
        token: str,
        lot_id: str,
        rows: Sequence[Mapping[str, Any]],
        idempotency_key: str | None = None,
    ) -> dict:
        """把一整个测量请求原子写入；任一条不合法则整批不落库、不写审计。"""

        actor = self.auth.require(token, "measure")
        lot = self._lot_row(lot_id)

        rows = tuple(rows)
        # 契约前移：任何摘要、幂等记录或审计事件之前完成结构、量程与批内去重校验。
        # 单条写入允许缺省 sample_key，由内容稳定派生；批量必须显式声明。
        instruments = self._active_instruments()
        parsed, violations = validate_batch(rows, instruments, auto_sample_key=(len(rows) == 1))
        if violations:
            # 写入边界拒绝：业务表与审计链都不留痕。
            raise ValidationFailed("测量未通过写入契约", violations)

        if lot["status"] in {"released", "rejected"}:
            raise InvalidState(f"批次状态 {lot['status']} 不再接受测量")

        # 摘要只基于通过契约的规范记录，保证非有限值永远无法进入幂等链。
        records = [item.as_record() for item in parsed]
        request_digest = content_digest(records)
        scope = f"measurements:{lot_id}"
        if not idempotency_key or not str(idempotency_key).strip():
            if len(records) == 1:
                # 单条写入：以内容派生稳定幂等键，等价重放自然返回同一结果。
                idempotency_key = request_digest
            else:
                raise ValidationFailed("批量写入必须提供 Idempotency-Key")
        else:
            idempotency_key = str(idempotency_key).strip()

        # 先识别等价重放：重放内容的采样键本就已在库中，不能按重复采样拒绝。
        replayed = self._idempotent_response(scope, idempotency_key, request_digest)
        if replayed is not None:
            return replayed

        # 非重放请求：采样键不得与库内既有测量重复。
        existing_keys = {
            row[0]
            for row in self.db.execute(
                "SELECT sample_key FROM measurements WHERE lot_id=?", (lot_id,)
            ).fetchall()
        }
        collisions = sorted({item.sample_key for item in parsed if item.sample_key in existing_keys})
        if collisions:
            raise ValidationFailed("测量未通过写入契约", [
                {
                    "field": "measurements.sample_key",
                    "rule": "duplicate",
                    "message": f"采样编号在库内已存在: {', '.join(collisions)}",
                    "value": key,
                }
                for key in collisions
            ])

        measurement_ids = [self._measurement_id(lot_id, item) for item in parsed]
        response = {
            "lot_id": lot_id,
            "inserted": len(parsed),
            "measurement_ids": measurement_ids,
            "request_sha256": request_digest,
        }
        now = utcnow()
        try:
            with transaction(self.db):
                for item, measurement_id in zip(parsed, measurement_ids):
                    record = item.as_record()
                    self.db.execute(
                        "INSERT INTO measurements(measurement_id,lot_id,sample_key,"
                        "signal_frequency_hz,response,noise,instrument,operator,measured_at,content_sha256) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            measurement_id, lot_id, item.sample_key,
                            item.signal_frequency_hz, item.response, item.noise,
                            item.instrument, actor.user_id, now,
                            content_digest([record]),
                        ),
                    )
                self.db.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), now),
                )
                event(self.db, lot_id, "measurements.imported", actor.user_id, response)
        except Exception as exc:
            raise Conflict("采样编号重复或幂等键并发冲突，整批未写入") from exc
        return response

    def add_measurement(
        self, token: str, lot_id: str, signal_frequency_hz: float,
        response: float, noise: float, instrument: str,
        sample_key: str | None = None,
    ) -> dict:
        """兼容单条录入；sample_key 缺省时由契约按内容稳定派生，等价调用可幂等重放。"""

        record: dict[str, Any] = {
            "signal_frequency_hz": signal_frequency_hz,
            "response": response,
            "noise": noise,
            "instrument": instrument,
        }
        if sample_key is not None:
            record["sample_key"] = sample_key
        result = self.write_measurements(token, lot_id, [record])
        return {"measurement_id": result["measurement_ids"][0], "lot_id": lot_id}

    # ------------------------------------------------------------ 隔离处置
    def scan_quarantine(self, token: str, lot_id: str | None = None) -> dict:
        """复跑写入契约识别库内非法记录，移动到隔离表并在审计链留痕。"""

        actor = self.auth.require(token, "analyze")
        self._lot_row(lot_id) if lot_id else None
        instruments = self._known_instruments()
        query = (
            "SELECT * FROM measurements"
            + (" WHERE lot_id=?" if lot_id else "")
            + " ORDER BY measurement_id"
        )
        params: tuple = (lot_id,) if lot_id else ()
        offenders: list[tuple] = []
        for row in self.db.execute(query, params).fetchall():
            reasons = evaluate_stored(row, instruments)
            if reasons:
                offenders.append((row, reasons))

        quarantined: list[dict] = []
        with transaction(self.db):
            for row, reasons in offenders:
                entry = self._quarantine_row(row, reasons, "runtime_scan", actor.user_id, now=utcnow())
                quarantined.append(entry)
        return {"scanned_lot": lot_id, "quarantined": quarantined,
                "count": len(quarantined)}

    def _quarantine_row(self, row, reasons: list[dict], source: str, actor: str, now: str | None = None) -> dict:
        from .storage import _json_safe_snapshot

        snapshot = _json_safe_snapshot(row)
        now = now or utcnow()
        cursor = self.db.execute(
            "INSERT INTO quarantined_measurements(measurement_id,lot_id,sample_key,"
            "signal_frequency_hz,response,noise,instrument,operator,measured_at,"
            "snapshot_json,reasons_json,source,status,detected_by,detected_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["measurement_id"], row["lot_id"], row["sample_key"],
                row["signal_frequency_hz"], row["response"], row["noise"],
                row["instrument"], row["operator"], row["measured_at"],
                canonical_json(snapshot), canonical_json(reasons),
                source, "quarantined", actor, now,
            ),
        )
        self.db.execute("DELETE FROM measurements WHERE measurement_id=?", (row["measurement_id"],))
        event(self.db, row["lot_id"], "measurement.quarantined", actor, {
            "measurement_id": row["measurement_id"],
            "quarantine_id": cursor.lastrowid,
            "source": source,
            "reasons": reasons,
        })
        return {
            "measurement_id": row["measurement_id"],
            "lot_id": row["lot_id"],
            "reasons": reasons,
        }

    def list_quarantine(self, token: str, lot_id: str | None = None) -> list[dict]:
        self.auth.require(token, "read")
        if lot_id:
            rows = self.db.execute(
                "SELECT * FROM quarantined_measurements WHERE lot_id=? ORDER BY quarantine_id",
                (lot_id,),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM quarantined_measurements ORDER BY quarantine_id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(item.pop("reasons_json"))
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
            # 原始数值列可能是无穷大（隔离的根因），输出时以标记表示，
            # 完整证据见 snapshot，绝不让 Infinity 出现在 JSON 响应里。
            for field in ("signal_frequency_hz", "response", "noise"):
                value = item.get(field)
                if isinstance(value, float):
                    if value != value:
                        item[field] = "__nan__"
                    elif value == float("inf"):
                        item[field] = "__infinity__"
                    elif value == float("-inf"):
                        item[field] = "__-infinity__"
            result.append(item)
        return result

    def resolve_quarantine(
        self, token: str, quarantine_id: int, decision: str, note: str
    ) -> dict:
        """人工复核隔离记录：discard 保留处置痕迹；release 验证后移回业务表。"""

        actor = self.auth.require(token, "approve")
        if decision not in {"release", "discard"} or not note.strip():
            raise ValidationFailed("decision 必须是 release/discard，且 note 不能为空")
        row = self.db.execute(
            "SELECT * FROM quarantined_measurements WHERE quarantine_id=?", (quarantine_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"隔离记录不存在: {quarantine_id}")
        if row["status"] != "quarantined":
            raise InvalidState(f"隔离记录已处置: {row['status']}")
        now = utcnow()
        stored_status = {"release": "released", "discard": "discarded"}[decision]
        event_name = {"release": "measurement.released", "discard": "measurement.discarded"}[decision]
        with transaction(self.db):
            if decision == "release":
                instruments = self._known_instruments()
                candidate = {
                    "measurement_id": row["measurement_id"],
                    "lot_id": row["lot_id"],
                    "sample_key": row["sample_key"],
                    "signal_frequency_hz": row["signal_frequency_hz"],
                    "response": row["response"],
                    "noise": row["noise"],
                    "instrument": row["instrument"],
                }
                if evaluate_stored(candidate, instruments):
                    raise InvalidState("记录仍不满足写入契约，不能解除隔离")
                clash = self.db.execute(
                    "SELECT 1 FROM measurements WHERE lot_id=? AND sample_key=?",
                    (row["lot_id"], row["sample_key"]),
                ).fetchone()
                if clash:
                    raise Conflict("同批次已存在相同采样编号")
                self.db.execute(
                    "INSERT INTO measurements(measurement_id,lot_id,sample_key,"
                    "signal_frequency_hz,response,noise,instrument,operator,measured_at,content_sha256) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["measurement_id"], row["lot_id"], row["sample_key"],
                        row["signal_frequency_hz"], row["response"], row["noise"],
                        row["instrument"], row["operator"], row["measured_at"],
                        content_digest([{
                            "sample_key": row["sample_key"],
                            "signal_frequency_hz": row["signal_frequency_hz"],
                            "response": row["response"],
                            "noise": row["noise"],
                            "instrument": row["instrument"],
                        }]),
                    ),
                )
            self.db.execute(
                "UPDATE quarantined_measurements SET status=?,resolved_by=?,resolved_at=?,"
                "resolution_note=? WHERE quarantine_id=? AND status='quarantined'",
                (stored_status, actor.user_id, now, note.strip(), quarantine_id),
            )
            event(self.db, row["lot_id"], event_name, actor.user_id, {
                "measurement_id": row["measurement_id"],
                "quarantine_id": quarantine_id,
                "decision": decision,
                "note": note.strip(),
            })
        return {"quarantine_id": quarantine_id, "measurement_id": row["measurement_id"],
                "status": stored_status}

    # ------------------------------------------------------------ 分析报告
    def _valid_rows(self, lot_id: str) -> list:
        rows = self.db.execute(
            "SELECT measurement_id,signal_frequency_hz,response,noise,instrument "
            "FROM measurements WHERE lot_id=? ORDER BY signal_frequency_hz,measurement_id",
            (lot_id,),
        ).fetchall()
        instruments = self._known_instruments()
        dirty: list[dict] = []
        clean: list = []
        for row in rows:
            reasons = evaluate_stored(row, instruments)
            if reasons:
                dirty.append({"measurement_id": row["measurement_id"], "reasons": reasons})
            else:
                clean.append(row)
        if dirty:
            # 绝不在读取时悄悄丢弃：要求先扫描隔离再出质量结论。
            violations = [
                {
                    "field": f"measurement[{item['measurement_id']}]",
                    "rule": "stored.contract",
                    "reasons": item["reasons"],
                }
                for item in dirty
            ]
            raise InvalidState(
                "存在未隔离的非法测量，请先运行 quarantine scan",
                violations,
            )
        return clean

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        self._lot_row(lot_id)
        rows = self._valid_rows(lot_id)
        if len(rows) < 3:
            raise ValidationFailed("形成质量结论至少需要三条有效测量")
        frequencies = [r["signal_frequency_hz"] for r in rows]
        responses = [r["response"] for r in rows]
        summary = summarize_signal_profile(frequencies, responses)
        rates = yield_rate(
            self._lot_row(lot_id)["unit_count"],
            sum(1 for value in responses if value >= 0.8),
            0,
        )
        ci = confidence_interval(responses)
        evidence = [{
            "measurement_id": r["measurement_id"],
            "signal_frequency_hz": r["signal_frequency_hz"],
            "response": r["response"],
            "noise": r["noise"],
            "instrument": r["instrument"],
        } for r in rows]
        return {
            "lot_id": lot_id,
            "signal_profile": summary.__dict__,
            "yield": rates,
            "response_ci": ci,
            "valid_measurement_ids": [r["measurement_id"] for r in rows],
            "input_sha256": content_digest(evidence),
        }

    def quality_report(self, token: str, lot_id: str) -> dict:
        """质量报告：只使用业务表中可追溯的有效测量，隔离记录仅作旁证列出。"""

        self.auth.require(token, "analyze")
        lot = dict(self._lot_row(lot_id))
        valid_count = self.db.execute(
            "SELECT count(*) FROM measurements WHERE lot_id=?", (lot_id,)
        ).fetchone()[0]
        quarantine = self.list_quarantine(token, lot_id)
        try:
            analysis = self.analyze(token, lot_id)
        except ServiceError as exc:
            analysis = {"unavailable": str(exc), "violations": exc.violations}
        return {
            "lot": lot,
            "valid_measurement_count": valid_count,
            "quarantined": [
                {
                    "quarantine_id": item["quarantine_id"],
                    "measurement_id": item["measurement_id"],
                    "status": item["status"],
                    "reasons": item["reasons"],
                    "detected_by": item["detected_by"],
                    "resolved_by": item["resolved_by"],
                    "resolution_note": item["resolution_note"],
                }
                for item in quarantine
            ],
            "analysis": analysis,
        }

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValidationFailed("decision 和 reason 为必填")
        with transaction(self.db):
            self.db.execute(
                "INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)",
                (lot_id, actor.user_id, decision, reason.strip(), utcnow()),
            )
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute(
                "UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?",
                (status, utcnow(), lot_id),
            )
            event(self.db, lot_id, "approval", actor.user_id,
                  {"decision": decision, "reason": reason.strip()})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)
            ).fetchall()
        ]
