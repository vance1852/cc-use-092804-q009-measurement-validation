"""部件质量服务的可观察错误类型。"""

from __future__ import annotations

from typing import Any, Sequence


class ServiceError(RuntimeError):
    code = "service_error"
    status = 400


class NotFound(ServiceError):
    code = "not_found"
    status = 404


class Conflict(ServiceError):
    code = "conflict"
    status = 409


class Forbidden(ServiceError):
    code = "forbidden"
    status = 403


class InvalidState(ServiceError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ServiceError):
    """携带字段级违规清单的校验失败；violations 元素需实现 to_dict()。"""

    code = "validation_failed"
    status = 422

    def __init__(self, violations: Sequence[Any] | None = None, message: str | None = None):
        self.violations: Sequence[Any] = list(violations or [])
        text = message if message is not None else "; ".join(str(item) for item in self.violations)
        super().__init__(text)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.violations]
