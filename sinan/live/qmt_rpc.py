"""QMT RPC 就绪验证：无交易副作用，逐层返回可诊断结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from qmt_shell import qmt_sdk
from sinan.config import QmtRpcCfg


@dataclass(frozen=True)
class QmtRpcReadiness:
    ready: bool
    stage: str
    message: str
    endpoint: str
    health: dict = field(default_factory=dict)
    quote: dict = field(default_factory=dict)


def _token(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _call(rpc: QmtRpcCfg, token: str, fn: str, *args):
    client = qmt_sdk._Client()
    try:
        client.connect(rpc.host, rpc.port, token, rpc.timeout)
        return client.call(fn, *args)
    finally:
        client.close()


def verify_qmt_rpc(rpc: QmtRpcCfg, token_path: Path | None = None) -> QmtRpcReadiness:
    """验证 TCP、健康协议和行情；绝不调用下单/撤单函数。"""
    endpoint = f"{rpc.host}:{rpc.port}"
    token = _token(token_path or Path.home() / ".qmt_rpc_token")
    try:
        health = _call(rpc, token, "rpc.health")
    except (OSError, TimeoutError, ConnectionError) as ex:
        return QmtRpcReadiness(False, "tcp", f"TCP 连接失败：{ex}", endpoint)
    except qmt_sdk.QmtRpcError as ex:
        detail = str(ex)
        if "rpc.health" in detail and "无此函数" in detail:
            detail = "QMT 脚本版本不支持健康协议，请替换为最新版 sinan_qmt.py"
        return QmtRpcReadiness(False, "health", f"鉴权或健康协议失败：{detail}",
                               endpoint)

    if health.get("service") != "sinan-qmt-rpc" or health.get("protocol") != 1:
        return QmtRpcReadiness(False, "health", "服务标识或协议版本不匹配",
                               endpoint, health=health)
    try:
        name = _call(rpc, token, "C.get_stock_name", "510300.SH")
        tick = _call(rpc, token, "C.get_full_tick", ["510300.SH"]) or {}
        row = tick.get("510300.SH") or {}
        if not row:
            raise qmt_sdk.QmtRpcError("510300.SH 未返回实时行情")
    except (OSError, TimeoutError, ConnectionError, qmt_sdk.QmtRpcError) as ex:
        return QmtRpcReadiness(False, "quote", f"行情验证失败：{ex}", endpoint,
                               health=health)

    quote = {"symbol": "510300.SH", "name": name,
             "last_price": row.get("lastPrice")}
    return QmtRpcReadiness(True, "ready", "RPC 已准备就绪", endpoint,
                           health=health, quote=quote)
