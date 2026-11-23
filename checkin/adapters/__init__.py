"""适配器包。"""
from .base import BaseAdapter, CheckinOutcome, ContractDriftError, get_path, validate_shape
from .http import ContractHttpAdapter
from .registry import AdapterRegistry, ContractError

__all__ = [
    "BaseAdapter", "CheckinOutcome", "ContractDriftError",
    "ContractHttpAdapter", "AdapterRegistry", "ContractError",
    "get_path", "validate_shape",
]
