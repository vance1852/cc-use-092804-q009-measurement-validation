"""国产电子部件批次、测量、仪器台账与隔离记录的 SQLite 结构及事务辅助。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


# 进程内串行锁：HTTP 多线程共享同一连接时，防止并发 BEGIN 互相打断。
# 本平台每进程只服务一个 SQLite 数据库，全局锁足够且无死锁风险。
_BEGIN_LOCK = threading.RLock()


SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta(
 key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 unit_count INTEGER NOT NULL CHECK(unit_count > 0),
 status TEXT NOT NULL CHECK(status IN ('engineering','released','hold','rejected')),
 owner TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS instruments(
 instrument_id TEXT PRIMARY KEY, model TEXT NOT NULL, vendor TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','retired')),
 registered_by TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 sample_key TEXT NOT NULL,
 signal_frequency_hz REAL NOT NULL
   CHECK(typeof(signal_frequency_hz)='real' AND signal_frequency_hz > 0 AND signal_frequency_hz <= 1e12),
 response REAL NOT NULL
   CHECK(typeof(response)='real' AND response >= 0 AND response <= 2),
 noise REAL NOT NULL
   CHECK(typeof(noise)='real' AND noise >= 0 AND noise < 1),
 instrument TEXT NOT NULL REFERENCES instruments(instrument_id),
 operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
 UNIQUE(lot_id, sample_key));

CREATE TABLE IF NOT EXISTS quarantined_measurements(
 quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
 measurement_id TEXT, lot_id TEXT, sample_key TEXT,
 signal_frequency_hz REAL, response REAL, noise REAL,
 instrument TEXT, operator TEXT, measured_at TEXT,
 snapshot_json TEXT NOT NULL, reasons_json TEXT NOT NULL,
 source TEXT NOT NULL CHECK(source IN ('legacy_scan','runtime_scan')),
 status TEXT NOT NULL CHECK(status IN ('quarantined','released','discarded')),
 detected_by TEXT NOT NULL, detected_at TEXT NOT NULL,
 resolved_by TEXT, resolved_at TEXT, resolution_note TEXT);

CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));

CREATE TABLE IF NOT EXISTS idempotency_keys(
 scope TEXT NOT NULL, key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 response_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope,key));
"""

_DDL_STATEMENTS = [stmt.strip() for stmt in SCHEMA.split(";") if stmt.strip()]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False：HTTP 服务用多线程处理请求；写入由 _BEGIN_LOCK
    # 串行化（同一连接不能并发 BEGIN），DB 层 busy_timeout 处理多进程锁等待。
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    initialize(db)
    return db


def initialize(db: sqlite3.Connection) -> None:
    """创建当前模式，并把旧版测量表原子迁移到带契约约束的新结构。"""

    with _BEGIN_LOCK:
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _DDL_STATEMENTS:
                db.execute(statement)
            _migrate_measurements(db)
            db.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _json_safe_snapshot(row: sqlite3.Row) -> dict:
    """把库内行复制成可 JSON 序列化快照；非有限 REAL 以标记字符串保留证据。"""

    snapshot = {key: row[key] for key in row.keys()}
    for key in ("signal_frequency_hz", "response", "noise"):
        value = snapshot.get(key)
        if isinstance(value, float):
            if value != value:
                snapshot[key] = "__nan__"
            elif value == float("inf"):
                snapshot[key] = "__infinity__"
            elif value == float("-inf"):
                snapshot[key] = "__-infinity__"
    return snapshot


def _migrate_measurements(db: sqlite3.Connection) -> None:
    """旧表缺少 sample_key 与量程约束：合法行继承新约束，非法行进隔离表。

    调用方已持有事务；整个迁移在同一事务内完成，失败即全部回滚。
    """

    cols = _table_columns(db, "measurements")
    if not cols or "sample_key" in cols:
        return

    from .contracts import evaluate_stored

    legacy = db.execute(
        "SELECT measurement_id,lot_id,signal_frequency_hz,response,noise,instrument,operator,measured_at "
        "FROM measurements"
    ).fetchall()
    db.execute("ALTER TABLE measurements RENAME TO measurements_legacy")
    for statement in _DDL_STATEMENTS:
        db.execute(statement)

    now = utcnow()
    # 旧测量引用的仪器自动登记，保持历史记录的身份可追溯。
    for instrument_id in {row["instrument"] for row in legacy if row["instrument"]}:
        db.execute(
            "INSERT OR IGNORE INTO instruments VALUES(?,?,?,?,?,?)",
            (instrument_id, "legacy-import", "unknown", "active", "system", now),
        )
    instruments = frozenset(
        row[0] for row in db.execute(
            "SELECT instrument_id FROM instruments WHERE status='active'"
        ).fetchall()
    )

    for row in legacy:
        reasons = evaluate_stored(row, instruments)
        if reasons:
            snapshot = _json_safe_snapshot(row)
            db.execute(
                "INSERT INTO quarantined_measurements(measurement_id,lot_id,sample_key,"
                "signal_frequency_hz,response,noise,instrument,operator,measured_at,"
                "snapshot_json,reasons_json,source,status,detected_by,detected_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["measurement_id"], row["lot_id"], None,
                    row["signal_frequency_hz"], row["response"], row["noise"],
                    row["instrument"], row["operator"], row["measured_at"],
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(reasons, ensure_ascii=False),
                    "legacy_scan", "quarantined", "system", now,
                ),
            )
            db.execute(
                "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
                (
                    row["lot_id"], "measurement.quarantined", "system",
                    json.dumps(
                        {
                            "measurement_id": row["measurement_id"],
                            "source": "legacy_scan",
                            "reasons": reasons,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    now,
                ),
            )
            continue
        sample_key = f"legacy-{row['measurement_id']}"
        digest = hashlib.sha256(
            json.dumps(
                {
                    "lot_id": row["lot_id"],
                    "sample_key": sample_key,
                    "signal_frequency_hz": row["signal_frequency_hz"],
                    "response": row["response"],
                    "noise": row["noise"],
                    "instrument": row["instrument"],
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        db.execute(
            "INSERT INTO measurements(measurement_id,lot_id,sample_key,signal_frequency_hz,response,noise,"
            "instrument,operator,measured_at,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                row["measurement_id"], row["lot_id"], sample_key,
                row["signal_frequency_hz"], row["response"], row["noise"],
                row["instrument"], row["operator"], row["measured_at"], digest,
            ),
        )
    db.execute("DROP TABLE measurements_legacy")


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    with _BEGIN_LOCK:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
        (lot_id, event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), utcnow()),
    )
