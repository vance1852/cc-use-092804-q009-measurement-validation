"""严格的 JSON 输入输出：拒绝非有限数值、重复键，并提供规范化摘要。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


class JsonDataError(ValueError):
    """JSON 缺失、损坏或不符合最外层语法契约。"""

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


class _NonFinite:
    """parse_constant 产生的哨兵，携带原始文本（NaN/Infinity/-Infinity）。"""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"<non-finite {self.text}>"


NON_FINITE = _NonFinite  # 供契约层用 isinstance 识别


def _capture_constant(value: str) -> _NonFinite:
    return _NonFinite(value)


def loads(data: str | bytes) -> Any:
    """解析 JSON 对象文本；非有限常量变为哨兵，重复键直接报错。"""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JsonDataError(f"JSON 对象含重复键: {key}", key=key)
            result[key] = value
        return result

    try:
        return json.loads(data, parse_constant=_capture_constant, object_pairs_hook=pairs_hook)
    except json.JSONDecodeError as exc:
        raise JsonDataError(f"不是有效 JSON: {exc.msg}") from exc


def _json_default(value: object) -> object:
    if isinstance(value, _NonFinite):
        raise ValueError(f"非有限数值不能序列化: {value.text}")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def canonical_json(value: object) -> str:
    """跨平台一致的紧凑 JSON；allow_nan=False 保证不输出 Infinity/NaN。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
