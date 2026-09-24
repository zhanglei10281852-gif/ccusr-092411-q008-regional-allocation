"""领域错误。"""
from __future__ import annotations


class AllocationError(Exception):
    """调拨规则被违反。"""


class ValidationError(AllocationError):
    """输入数据不合法。"""


class NotFoundError(AllocationError):
    """聚合或实体不存在。"""


class ConflictError(AllocationError):
    """当前状态不允许该操作（包括重复幂等请求）。"""


class AuthorizationRequired(AllocationError):
    """紧急需求超出保留池，必须提供授权才能挤占未发运额度。"""
