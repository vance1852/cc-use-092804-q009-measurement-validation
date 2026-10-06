"""测量写入契约：在数据进入业务表与审计链之前完成全部校验。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from .errors import ValidationFailed


@dataclass(frozen=True, slots=True)
class Violation:
    field: str
    rule: str
    reason: str
    index: int | None = None

    def __str__(self) -> str:
        prefix = f"[{self.index}] " if self.index is not None else ""
        return f"{prefix}{self.field} 违反 {self.rule}: {self.reason}"

    def to_dict(self) -> dict[str, str | int]:
        data: dict[str, str | int] = {"field": self.field, "rule": self.rule, "reason": self.reason}
        if self.index is not None:
            data["index"] = self.index
        return data


# 可接受量程：国产传感接口芯片测试台的工程边界。
FREQUENCY_MIN_HZ = 1.0
FREQUENCY_MAX_HZ = 1.0e9
RESPONSE_MIN = 0.0
RESPONSE_MAX = 1.0
NOISE_MIN = 0.0
NOISE_MAX = 1.0
INSTRUMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MEASUREMENT_FIELDS = ("signal_frequency_hz", "response", "noise", "instrument")


@dataclass(frozen=True, slots=True)
class Measurement:
    """一条通过契约的测量；数值一律 float，仪器身份非空且已规范化。"""

    signal_frequency_hz: float
    response: float
    noise: float
    instrument: str
    sample_id: str | None = None


@dataclass(slots=True)
class BatchValidationResult:
    measurements: list[Measurement] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    sample_keys: set[tuple[Any, ...]] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.violations


def _bounded_number(
    value: Any, path: str, rule: str, low: float, high: float, violations: list[Violation],
    index: int | None,
) -> float | None:
    """拒绝布尔、字符串伪装、非有限数值和超出可接受量程的值。"""

    if isinstance(value, bool):
        violations.append(Violation(path, "type", "必须是数值，不能是布尔值", index))
        return None
    if isinstance(value, float) and not math.isfinite(value):
        violations.append(Violation(path, "finite", "必须是有限数值（不接受 Infinity/-Infinity/NaN）", index))
        return None
    if isinstance(value, float):
        number = value
    elif isinstance(value, Decimal):
        if not value.is_finite():
            violations.append(Violation(path, "finite", "必须是有限数值（不接受 Infinity/-Infinity/NaN）", index))
            return None
        try:
            number = float(value)
        except OverflowError:
            violations.append(
                Violation(path, "range", f"数值超出浮点可表示范围，必须位于 [{low}, {high}]", index)
            )
            return None
        if not math.isfinite(number):
            violations.append(
                Violation(path, "range", f"数值超出浮点可表示范围，必须位于 [{low}, {high}]", index)
            )
            return None
    elif isinstance(value, int):
        number = float(value)
    elif isinstance(value, str):
        violations.append(Violation(path, "type", "必须是数值，不接受字符串伪装的数字", index))
        return None
    else:
        violations.append(Violation(path, "type", "必须是数值", index))
        return None
    if not (low <= number <= high):
        violations.append(
            Violation(path, rule, f"超出可接受量程，必须位于 [{low}, {high}]（实际 {number}）", index)
        )
        return None
    return number


def _instrument(value: Any, violations: list[Violation], index: int | None) -> str | None:
    if not isinstance(value, str) or isinstance(value, bool):
        violations.append(Violation("instrument", "type", "必须是字符串形式的仪器身份", index))
        return None
    identity = value.strip()
    if not identity:
        violations.append(Violation("instrument", "required", "仪器身份不能为空", index))
        return None
    if not INSTRUMENT_PATTERN.match(identity):
        violations.append(
            Violation(
                "instrument",
                "format",
                "只允许 1-64 位字母、数字或 . _ -，且须以字母或数字开头",
                index,
            )
        )
        return None
    return identity


def _sample_id(value: Any, violations: list[Violation], index: int | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or isinstance(value, bool) or not value.strip():
        violations.append(Violation("sample_id", "type", "若提供必须是非空字符串", index))
        return None
    identity = value.strip()
    if len(identity) > 128:
        violations.append(Violation("sample_id", "format", "长度不能超过 128", index))
        return None
    return identity


def validate_measurement(raw: Mapping[str, Any], *, index: int | None = None) -> Measurement:
    """校验单条测量；聚合该条内的全部违规后一次性抛出。"""

    violations: list[Violation] = []
    if not isinstance(raw, Mapping):
        raise ValidationFailed([Violation("measurement", "type", "必须是 JSON 对象", index)])
    measurement = _validate_fields(raw, violations, index, sample_keys=None)
    if violations:
        raise ValidationFailed(violations)
    return measurement


def _validate_fields(
    raw: Mapping[str, Any],
    violations: list[Violation],
    index: int | None,
    sample_keys: set[tuple[Any, ...]] | None,
) -> Measurement | None:
    frequency = _bounded_number(
        raw.get("signal_frequency_hz"), "signal_frequency_hz", "frequency_range",
        FREQUENCY_MIN_HZ, FREQUENCY_MAX_HZ, violations, index,
    )
    response = _bounded_number(
        raw.get("response"), "response", "response_range",
        RESPONSE_MIN, RESPONSE_MAX, violations, index,
    )
    noise = _bounded_number(
        raw.get("noise", 0.0), "noise", "noise_range",
        NOISE_MIN, NOISE_MAX, violations, index,
    )
    instrument = _instrument(raw.get("instrument"), violations, index)
    sample_id = _sample_id(raw.get("sample_id"), violations, index)
    # 重复采样：同批次内同一次物理采样不能出现两次（sample_id 优先，否则用测点指纹）。
    if sample_keys is not None and not violations:
        key: tuple[Any, ...]
        if sample_id is not None:
            key = ("id", sample_id)
        else:
            key = ("point", frequency, response, noise, instrument)
        if key in sample_keys:
            description = f"sample_id={sample_id}" if sample_id is not None else f"测点指纹 {key[1:]}"
            violations.append(Violation("sample", "duplicate_sample", f"批次内重复采样：{description}", index))
            return None
        sample_keys.add(key)
    if violations:
        return None
    return Measurement(frequency, response, noise, instrument, sample_id)  # type: ignore[arg-type]


def validate_batch(raw_rows: Any) -> BatchValidationResult:
    """校验一批测量：聚合全部条目和字段的违规，并拦截批次内重复采样。"""

    result = BatchValidationResult()
    if not isinstance(raw_rows, (list, tuple)) or isinstance(raw_rows, (str, bytes)):
        result.violations.append(Violation("measurements", "type", "必须是 JSON 数组", None))
        return result
    if not raw_rows:
        result.violations.append(Violation("measurements", "required", "至少包含一条测量", None))
        return result
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping):
            result.violations.append(Violation("measurement", "type", "必须是 JSON 对象", index))
            continue
        item_violations: list[Violation] = []
        measurement = _validate_fields(raw, item_violations, index, result.sample_keys)
        result.violations.extend(item_violations)
        if measurement is not None:
            result.measurements.append(measurement)
    return result
