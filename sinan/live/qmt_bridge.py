"""本地到 QMT 的显式执行桥：发布 targets、查询状态、拉取 fills。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from qmt_shell import qmt_sdk
from sinan.config import QmtRpcCfg


@dataclass(frozen=True)
class PublishResult:
    status: str
    strategy: str
    date: str
    checksum: str
    filename: str


def _safe_identity(strategy: str, day: str) -> tuple[str, str]:
    strategy = str(strategy or "")
    if (not strategy or strategy in {".", ".."}
            or any(ord(ch) < 32 for ch in strategy)
            or any(ch in '\\/:*?"<>|' for ch in strategy)):
        raise ValueError(f"策略名称不安全:{strategy!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day or "")):
        raise ValueError("日期必须为 YYYY-MM-DD")
    parsed = datetime.strptime(day, "%Y-%m-%d")
    if parsed.strftime("%Y-%m-%d") != day:
        raise ValueError(f"日期无效:{day}")
    return strategy, day


class QmtRpcBridge:
    """每次操作使用短连接，副作用请求从不在客户端盲重试。"""

    def __init__(
        self,
        rpc: QmtRpcCfg,
        token_path: Path | None = None,
        client_factory: Callable[[], qmt_sdk._Client] = qmt_sdk._Client,
    ) -> None:
        self.rpc = rpc.model_copy(deep=True)
        self.token_path = token_path or Path.home() / ".qmt_rpc_token"
        self.client_factory = client_factory

    def _token(self) -> str:
        if not self.token_path.exists():
            return ""
        return self.token_path.read_text(encoding="utf-8").strip()

    def _call(self, fn: str, *args):
        client = self.client_factory()
        try:
            client.connect(
                self.rpc.host, self.rpc.port, self._token(), self.rpc.timeout
            )
            return client.call(fn, *args)
        finally:
            client.close()

    def publish(self, payload: dict) -> PublishResult:
        result = self._call("rpc.publish_targets", payload)
        return PublishResult(
            status=str(result["status"]),
            strategy=str(result["strategy"]),
            date=str(result["date"]),
            checksum=str(result["checksum"]),
            filename=str(result["filename"]),
        )

    def execution_status(self, strategy: str, date: str) -> dict:
        strategy, date = _safe_identity(strategy, date)
        result = self._call("rpc.execution_status", strategy, date)
        if not isinstance(result, dict):
            raise qmt_sdk.QmtRpcError("rpc.execution_status 返回格式错误")
        return result

    def pull_fills(
        self, strategy: str, date: str, fills_dir: Path
    ) -> Path | None:
        strategy, date = _safe_identity(strategy, date)
        status = self.execution_status(strategy, date)
        fills = status.get("fills")
        if not isinstance(fills, dict):
            return None
        directory = Path(fills_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"fills_{strategy}_{date.replace('-', '')}.json"
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(fills, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)
        finally:
            if tmp.exists():
                tmp.unlink()
        return path
