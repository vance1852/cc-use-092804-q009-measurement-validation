"""国产电子部件批次和测量记录的 SQLite 结构、迁移与事务辅助。"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .contracts import (
    FREQUENCY_MAX_HZ,
    FREQUENCY_MIN_HZ,
    INSTRUMENT_PATTERN,
    NOISE_MAX,
    NOISE_MIN,
    RESPONSE_MAX,
    RESPONSE_MIN,
)

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 unit_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 signal_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, sample_id TEXT, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 CHECK(typeof(signal_frequency_hz)='real'),
 CHECK(typeof(response)='real'),
 CHECK(typeof(noise)='real'),
 CHECK(signal_frequency_hz BETWEEN {f_lo} AND {f_hi}),
 CHECK(response BETWEEN {r_lo} AND {r_hi}),
 CHECK(noise BETWEEN {n_lo} AND {n_hi}),
 CHECK(length(instrument) BETWEEN 1 AND 64),
 UNIQUE(lot_id,measurement_id));
CREATE UNIQUE INDEX IF NOT EXISTS measurement_sample_id_uniq
 ON measurements(lot_id,sample_id) WHERE sample_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS measurement_point_uniq
 ON measurements(lot_id,signal_frequency_hz,response,noise,instrument);
CREATE TABLE IF NOT EXISTS registered_instruments(
 instrument TEXT PRIMARY KEY, vendor TEXT NOT NULL DEFAULT '',
 model TEXT NOT NULL DEFAULT '', registered_by TEXT NOT NULL,
 registered_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
CREATE TABLE IF NOT EXISTS measurement_quarantine(
 quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
 original_measurement_id TEXT NOT NULL, lot_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, reasons_json TEXT NOT NULL,
 source TEXT NOT NULL CHECK(source IN ('scan','schema_migration')),
 status TEXT NOT NULL DEFAULT 'pending'
  CHECK(status IN ('pending','discarded','released')),
 detected_by TEXT NOT NULL, detected_at TEXT NOT NULL,
 resolved_by TEXT, resolved_at TEXT, resolution_note TEXT);
CREATE INDEX IF NOT EXISTS quarantine_lot_idx ON measurement_quarantine(lot_id);
CREATE TABLE IF NOT EXISTS measurement_idempotency(
 scope TEXT NOT NULL, key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 response_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope,key));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS quality_analyses(
 analysis_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 input_sha256 TEXT NOT NULL, result_json TEXT NOT NULL,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id,input_sha256));
""".format(
    f_lo=FREQUENCY_MIN_HZ,
    f_hi=FREQUENCY_MAX_HZ,
    r_lo=RESPONSE_MIN,
    r_hi=RESPONSE_MAX,
    n_lo=NOISE_MIN,
    n_hi=NOISE_MAX,
)

LEGACY_COLUMNS = (
    "measurement_id",
    "lot_id",
    "signal_frequency_hz",
    "response",
    "noise",
    "instrument",
    "operator",
    "measured_at",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_json_dumps(payload: Any) -> str:
    """序列化审计/隔离载荷；任何非有限数值都转成字符串标记，绝不输出 Infinity。"""

    def sanitize(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else f"<non-finite:{value!s}>"
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitize(item) for item in value]
        return value

    return json.dumps(sanitize(payload), sort_keys=True, ensure_ascii=False, allow_nan=False)


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
        (lot_id, event_type, actor, safe_json_dumps(payload), utcnow()),
    )


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # HTTP 服务使用线程服务器：写入全部经 BEGIN IMMEDIATE 串行化，配合 busy_timeout 允许跨线程共用连接。
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    if path != ":memory:":
        db.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(db)
    db.commit()
    return db


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_schema(db: sqlite3.Connection) -> None:
    """建表；遇到 v1 结构的 measurements 时先重命名，再迁移合法数据、隔离非法数据。"""

    legacy = _columns(db, "measurements") >= set(LEGACY_COLUMNS) and "sample_id" not in _columns(
        db, "measurements"
    )
    if legacy:
        db.execute("ALTER TABLE measurements RENAME TO measurements_legacy")
    db.executescript(SCHEMA)
    if legacy:
        _migrate_legacy_measurements(db)


def _legacy_reasons(row: sqlite3.Row) -> list[str]:
    reasons: list[str] = []
    numeric_rules = (
        ("signal_frequency_hz", FREQUENCY_MIN_HZ, FREQUENCY_MAX_HZ),
        ("response", RESPONSE_MIN, RESPONSE_MAX),
        ("noise", NOISE_MIN, NOISE_MAX),
    )
    for name, low, high in numeric_rules:
        value = row[name]
        if not isinstance(value, float) or not math.isfinite(value):
            reasons.append(f"{name} 违反 finite: 不是有限数值（实际 {value!r}）")
        elif not low <= value <= high:
            reasons.append(f"{name} 违反 range: 超出可接受量程 [{low}, {high}]（实际 {value}）")
    instrument = row["instrument"]
    if not isinstance(instrument, str) or not instrument.strip() or not INSTRUMENT_PATTERN.match(instrument):
        reasons.append("instrument 违反 identity: 缺失或不符合仪器身份格式")
    return reasons


def _migrate_legacy_measurements(db: sqlite3.Connection) -> None:
    """把 v1 测量分流：合法记录进新表，非法记录进隔离区并写审计事件。"""

    now = utcnow()
    # 历史数据中出现过、且身份格式合法的仪器自动补登记，避免后续注册校验误伤历史合法测量。
    legacy_rows = db.execute("SELECT * FROM measurements_legacy ORDER BY rowid").fetchall()
    for row in legacy_rows:
        instrument = row["instrument"]
        if isinstance(instrument, str) and INSTRUMENT_PATTERN.match(instrument):
            db.execute(
                "INSERT OR IGNORE INTO registered_instruments(instrument,vendor,model,registered_by,"
                "registered_at,active) VALUES(?,?,?, 'system', ?,1)",
                (instrument, "(legacy migration)", "", now),
            )
    seen_points: set[tuple] = set()
    for row in legacy_rows:
        payload = {key: row[key] for key in LEGACY_COLUMNS}
        reasons = _legacy_reasons(row)
        point = (row["lot_id"], row["signal_frequency_hz"], row["response"], row["noise"], row["instrument"])
        if not reasons and point in seen_points:
            reasons.append("sample 违反 duplicate_sample: 批次内重复采样")
        if reasons:
            db.execute(
                "INSERT INTO measurement_quarantine(original_measurement_id,lot_id,payload_json,"
                "reasons_json,source,status,detected_by,detected_at) VALUES(?,?,?,?,?, 'pending', ?,?)",
                (
                    row["measurement_id"],
                    row["lot_id"],
                    safe_json_dumps(payload),
                    safe_json_dumps(reasons),
                    "schema_migration",
                    "system",
                    utcnow(),
                ),
            )
            event(
                db,
                row["lot_id"],
                "measurement.quarantined",
                "system",
                {
                    "measurement_id": row["measurement_id"],
                    "reasons": reasons,
                    "source": "schema_migration",
                },
            )
            continue
        seen_points.add(point)
        db.execute(
            "INSERT INTO measurements(measurement_id,lot_id,signal_frequency_hz,response,noise,"
            "instrument,sample_id,operator,measured_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                row["measurement_id"],
                row["lot_id"],
                float(row["signal_frequency_hz"]),
                float(row["response"]),
                float(row["noise"]),
                row["instrument"],
                None,
                row["operator"],
                row["measured_at"],
            ),
        )
    db.execute("DROP TABLE measurements_legacy")


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
