"""测试台测量写入边界的严格数据契约。

契约在任何业务表或审计事件写入之前执行，违反时一次性给出
``哪个字段`` 违反了 ``哪条规则``。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .jsonio import _NonFinite, canonical_json


class ContractViolation(ValueError):
    """单条测量不能满足领域契约（保留给直接调用方使用）。"""


# 可接受量程。修改量程即修改测试台契约。
@dataclass(frozen=True)
class Range:
    minimum: float
    minimum_inclusive: bool
    maximum: float
    maximum_inclusive: bool

    def contains(self, value: float) -> bool:
        if self.minimum_inclusive:
            if value < self.minimum:
                return False
        elif value <= self.minimum:
            return False
        if self.maximum_inclusive:
            if value > self.maximum:
                return False
        elif value >= self.maximum:
            return False
        return True

    def describe(self) -> str:
        left = "[" if self.minimum_inclusive else "("
        right = "]" if self.maximum_inclusive else ")"
        return f"{left}{self.minimum:g}, {self.maximum:g}{right}"


LIMITS: dict[str, Range] = {
    "signal_frequency_hz": Range(0.0, False, 1e12, True),
    "response": Range(0.0, True, 2.0, True),
    "noise": Range(0.0, True, 1.0, False),
}

NUMERIC_FIELDS = ("signal_frequency_hz", "response", "noise")
KNOWN_FIELDS = {"sample_key", "signal_frequency_hz", "response", "noise", "instrument"}
INSTRUMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class Measurement:
    """一条已通过契约的测量。"""

    sample_key: str
    signal_frequency_hz: float
    response: float
    noise: float
    instrument: str

    def as_record(self) -> dict[str, Any]:
        return {
            "sample_key": self.sample_key,
            "signal_frequency_hz": self.signal_frequency_hz,
            "response": self.response,
            "noise": self.noise,
            "instrument": self.instrument,
        }


def violation(field: str, rule: str, message: str, value: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"field": field, "rule": rule, "message": message}
    if isinstance(value, (str, int, bool)) or value is None:
        item["value"] = value
    elif isinstance(value, float):
        # 非有限值无法安全进入 JSON 响应，以 None 表示（详情见 message）。
        item["value"] = value if math.isfinite(value) else None
    return item


def _check_number(field: str, value: Any, violations: list[dict[str, Any]], path: str | None = None) -> float | None:
    label = path or field
    if isinstance(value, _NonFinite):
        violations.append(
            violation(label, "finite", f"{label} 必须是有限数值，收到非有限常量 {value.text}", value.text)
        )
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        violations.append(
            violation(label, "type.number", f"{label} 必须是 JSON 数值，不接受字符串或其他类型伪装", value)
        )
        return None
    number = float(value)
    if not math.isfinite(number):
        # 防御绕过 JSON 解析直接进入服务层的 inf/nan。
        violations.append(violation(label, "finite", f"{label} 必须是有限数值", number))
        return None
    limits = LIMITS[field]
    if not limits.contains(number):
        violations.append(
            violation(label, "range", f"{label} 超出仪器可接受量程 {limits.describe()}", number)
        )
        return None
    return number


def validate_measurement(
    raw: Any,
    path: str,
    instruments: frozenset[str],
    violations: list[dict[str, Any]],
    seen_keys: set[str],
    seen_content: set[tuple[Any, ...]],
    auto_sample_key: bool = False,
) -> Measurement | None:
    """校验单条测量；问题追加进 violations，全部合法时返回 Measurement 并登记去重身份。

    auto_sample_key=True 时允许缺省 sample_key，并由其余字段的内容摘要稳定派生，
    使等价的单条重放得到同一采样身份。
    """

    if not isinstance(raw, Mapping):
        violations.append(violation(path, "type.object", f"{path} 必须是 JSON 对象"))
        return None

    for key in raw:
        if key not in KNOWN_FIELDS:
            violations.append(violation(f"{path}.{key}", "unknown_field", f"{path} 含未知字段 {key}"))

    row_problems: list[dict[str, Any]] = []

    sample_key = raw.get("sample_key") if "sample_key" in raw else None
    has_sample_key = "sample_key" in raw and sample_key is not None
    sample_id: str | None = None
    if has_sample_key:
        if not isinstance(sample_key, str) or not sample_key.strip():
            row_problems.append(
                violation(f"{path}.sample_key", "type.string", f"{path}.sample_key 必须是非空字符串", sample_key)
            )
        else:
            sample_id = sample_key.strip()
            if sample_id in seen_keys:
                row_problems.append(
                    violation(
                        f"{path}.sample_key",
                        "duplicate",
                        f"{path}.sample_key {sample_id} 在本批或库内已存在，重复采样不能进入业务表",
                        sample_id,
                    )
                )
    elif not auto_sample_key:
        row_problems.append(
            violation(f"{path}.sample_key", "required", f"{path}.sample_key 为必填，用于测试台采样去重")
        )

    numbers: dict[str, float | None] = {}
    for field in NUMERIC_FIELDS:
        if field not in raw or raw.get(field) is None:
            row_problems.append(violation(f"{path}.{field}", "required", f"{path}.{field} 为必填字段"))
            numbers[field] = None
        else:
            numbers[field] = _check_number(field, raw[field], row_problems, f"{path}.{field}")

    instrument = raw.get("instrument") if "instrument" in raw else None
    instrument_id: str | None = None
    if instrument is None:
        row_problems.append(
            violation(f"{path}.instrument", "required", f"{path}.instrument 为必填字段")
        )
    elif not isinstance(instrument, str) or not instrument.strip():
        row_problems.append(
            violation(f"{path}.instrument", "type.string", f"{path}.instrument 必须是非空字符串", instrument)
        )
    else:
        instrument_id = instrument.strip()
        if not INSTRUMENT_PATTERN.match(instrument_id):
            row_problems.append(
                violation(
                    f"{path}.instrument",
                    "format",
                    f"{path}.instrument 只能包含字母数字及 ._-，长度 1-64 且以字母数字开头",
                    instrument_id,
                )
            )
        elif instrument_id not in instruments:
            row_problems.append(
                violation(
                    f"{path}.instrument",
                    "instrument.registered",
                    f"{path}.instrument {instrument_id} 未在仪器身份台账登记或已停用",
                    instrument_id,
                )
            )

    if row_problems:
        violations.extend(row_problems)
        return None

    assert instrument_id is not None
    if sample_id is None:
        # 数值已通过有限与量程校验，这里做内容派生是安全的。
        identity = {
            "signal_frequency_hz": numbers["signal_frequency_hz"],
            "response": numbers["response"],
            "noise": numbers["noise"],
            "instrument": instrument_id,
        }
        sample_id = "auto-" + hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:24]
        if sample_id in seen_keys:
            violations.append(
                violation(
                    f"{path}.sample_key",
                    "duplicate",
                    f"{path} 的采样内容在本批或库内已存在，重复采样不能进入业务表",
                )
            )
            return None

    item = Measurement(
        sample_key=sample_id,
        signal_frequency_hz=numbers["signal_frequency_hz"],  # type: ignore[arg-type]
        response=numbers["response"],  # type: ignore[arg-type]
        noise=numbers["noise"],  # type: ignore[arg-type]
        instrument=instrument_id,
    )
    fingerprint = (
        item.signal_frequency_hz,
        item.response,
        item.noise,
        item.instrument,
    )
    if fingerprint in seen_content:
        violations.append(
            violation(path, "duplicate", f"{path} 与本批另一条测量的采样内容完全相同，疑似重复采样")
        )
        return None

    seen_keys.add(sample_id)
    seen_content.add(fingerprint)
    return item


def validate_batch(
    rows: Any,
    instruments: frozenset[str],
    existing_keys: set[str] | None = None,
    auto_sample_key: bool = False,
) -> tuple[tuple[Measurement, ...], list[dict[str, Any]]]:
    """校验整批测量；返回（合法测量，全部违规）。两者不会同时非空。"""

    violations: list[dict[str, Any]] = []
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence) or isinstance(rows, Mapping):
        violations.append(violation("measurements", "type.array", "measurements 必须是 JSON 数组"))
        return (), violations
    if not rows:
        violations.append(violation("measurements", "required", "measurements 至少包含一条测量"))
        return (), violations

    seen_keys: set[str] = set(existing_keys or ())
    seen_content: set[tuple[Any, ...]] = set()
    parsed: list[Measurement] = []
    for index, raw in enumerate(rows):
        item = validate_measurement(
            raw, f"measurements[{index}]", instruments, violations, seen_keys, seen_content,
            auto_sample_key=auto_sample_key and len(rows) == 1,
        )
        if item is not None:
            parsed.append(item)
    if violations:
        return (), violations
    return tuple(parsed), []


def evaluate_stored(row: Mapping[str, Any], known_instruments: frozenset[str]) -> list[dict[str, Any]]:
    """对数据库中既有记录复跑契约（扫描隔离用），返回违规原因列表。

    仪器身份只要求在台账中存在（含已停用）——采样时仪器在册即保持可追溯；
    写入边界才要求仪器当前处于 active。
    """

    reasons: list[dict[str, Any]] = []
    for field in NUMERIC_FIELDS:
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            reasons.append(
                violation(
                    f"measurement.{field}",
                    "type.number",
                    f"库内 {field} 的存储类型为 {type(value).__name__}，不是数值",
                )
            )
            continue
        number = float(value)
        if not math.isfinite(number):
            reasons.append(
                violation(f"measurement.{field}", "finite", f"库内 {field} 是非有限数值 {number}")
            )
            continue
        limits = LIMITS[field]
        if not limits.contains(number):
            reasons.append(
                violation(
                    f"measurement.{field}",
                    "range",
                    f"库内 {field}={number:g} 超出可接受量程 {limits.describe()}",
                )
            )
    instrument = row["instrument"]
    if not isinstance(instrument, str) or not instrument.strip():
        reasons.append(violation("measurement.instrument", "type.string", "库内 instrument 不是非空字符串"))
    elif instrument.strip() not in known_instruments:
        reasons.append(
            violation(
                "measurement.instrument",
                "instrument.registered",
                f"库内 instrument {instrument} 不在仪器身份台账中",
            )
        )
    return reasons
