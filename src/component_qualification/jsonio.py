"""写入边界使用的严格 JSON 解析与规范化摘要。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Iterable


class StrictJsonError(ValueError):
    """请求体不是合法的严格 JSON（语法错误、非有限常量或重复键）。"""


def _reject_constant(value: str) -> None:
    raise StrictJsonError(f"JSON 不允许非有限数值常量 {value}")


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"JSON 对象含重复键 {key}")
        result[key] = value
    return result


def strict_json_loads(raw: bytes | str) -> Any:
    """解析 UTF-8 JSON：数值保持十进制精度，拒绝非有限常量与重复键。"""

    try:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
        else:
            text = raw
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_pairs_hook,
        )
    except StrictJsonError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrictJsonError(f"请求体不是合法的 UTF-8 JSON: {exc}") from exc


def _default(value: Any) -> str:
    if isinstance(value, Decimal):
        # 1E+3 -> 1000，拒绝任何非有限尾数
        if not value.is_finite():
            raise ValueError("不能序列化非有限 Decimal")
        return format(value, "f")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """生成跨重放一致的紧凑 JSON；遇到非有限数值直接失败而不是写出 Infinity。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    )


def content_digest(values: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
