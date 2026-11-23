"""契约注册表：加载/校验 contracts/*.json，提供适配器工厂。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..models import Platform, Version
from .base import BaseAdapter
from .http import ContractHttpAdapter


class ContractError(ValueError):
    pass


class AdapterRegistry:
    def __init__(self, contracts_dir: str | Path, *, http_fn=None, simulate: bool = False):
        self.dir = Path(contracts_dir)
        self.http_fn = http_fn
        self.simulate = simulate
        self.contracts: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.errors: List[str] = []
        self.load()

    def load(self) -> None:
        self.contracts.clear()
        self.errors.clear()
        if not self.dir.exists():
            self.errors.append(f"契约目录不存在: {self.dir}")
            return
        for path in sorted(self.dir.glob("*.json")):
            try:
                contract = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.errors.append(f"{path.name}: 解析失败 ({exc})")
                continue
            try:
                self._validate(contract, path)
            except ContractError as exc:
                self.errors.append(str(exc))
                continue
            key = (contract["platform"], contract["version"])
            self.contracts[key] = contract

    @staticmethod
    def _validate(c: Any, path: Path) -> None:
        if not isinstance(c, dict):
            raise ContractError(f"{path.name}: 契约根节点必须是对象")
        p = str(c.get("platform", "")).lower()
        v = str(c.get("version", "")).lower()
        if p not in Platform.values():
            raise ContractError(f"{path.name}: 未知平台 {p}")
        if v not in Version.values():
            raise ContractError(f"{path.name}: 未知版本 {v}")
        expected_name = f"{p}.{v}.json"
        if path.name != expected_name:
            raise ContractError(f"{path.name}: 文件名应为 {expected_name}")
        if not c.get("steps"):
            raise ContractError(f"{path.name}: 缺少 steps")
        for i, step in enumerate(c["steps"]):
            if not isinstance(step, dict) or not step.get("id"):
                raise ContractError(f"{path.name}: steps[{i}] 缺少 id")
            if not step.get("method"):
                raise ContractError(f"{path.name}: steps[{i}] 缺少 method")

    def get(self, platform: str, version: str) -> Optional[BaseAdapter]:
        c = self.contracts.get((platform.lower(), version.lower()))
        if not c:
            return None
        return ContractHttpAdapter(c, http_fn=self.http_fn, simulate=self.simulate)

    def has(self, platform: str, version: str) -> bool:
        return (platform.lower(), version.lower()) in self.contracts

    def summary(self) -> List[Dict[str, str]]:
        rows = []
        for (p, v), c in sorted(self.contracts.items()):
            rows.append({
                "platform": p,
                "version": v,
                "name": str(c.get("name", "")),
                "contract_version": str(c.get("contract_version", "")),
                "status": "template" if c.get("template") else str(c.get("status", "active")),
                "steps": str(len(c.get("steps") or [])),
                "note": str(c.get("note", "")),
            })
        return rows
