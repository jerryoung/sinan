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
    account: dict = field(default_factory=dict)
    trade_query: dict = field(default_factory=dict)


def _token(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _field(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _account_ready_status(status: str) -> bool:
    """空状态兼容旧 QMT；明确的未就绪状态必须拒绝。"""
    text = str(status or "").strip()
    bad = ("准备登录", "未登录", "登录失败", "未连接", "断开", "离线")
    return not text or not any(word in text for word in bad)


def verify_qmt_rpc(rpc: QmtRpcCfg, token_path: Path | None = None) -> QmtRpcReadiness:
    """分层验证网络、协议、行情、账号与交易查询；绝不产生交易副作用。"""
    endpoint = f"{rpc.host}:{rpc.port}"
    token = _token(token_path or Path.home() / ".qmt_rpc_token")
    client = qmt_sdk._Client()
    try:
        client.connect(rpc.host, rpc.port, token, rpc.timeout)
    except (OSError, TimeoutError, ConnectionError) as ex:
        client.close()
        return QmtRpcReadiness(False, "tcp", f"TCP 连接失败：{ex}", endpoint)
    try:
        try:
            health = client.call("rpc.health")
        except qmt_sdk.QmtRpcError as ex:
            detail = str(ex)
            if "rpc.health" in detail and "无此函数" in detail:
                detail = "QMT 脚本版本不支持健康协议，请替换为最新版 sinan_qmt.py"
            return QmtRpcReadiness(
                False, "health", f"鉴权或健康协议失败：{detail}", endpoint
            )
        capabilities = health.get("capabilities") or []
        if (health.get("service") != "sinan-qmt-rpc"
                or health.get("protocol") != 2
                or "qmt_api_queue" not in capabilities):
            return QmtRpcReadiness(
                False, "health", "服务标识、协议版本或能力不匹配",
                endpoint, health=health,
            )

        try:
            name = client.call("C.get_stock_name", "510300.SH")
            tick = client.call("C.get_full_tick", ["510300.SH"]) or {}
            row = tick.get("510300.SH") or {}
            if not row:
                raise qmt_sdk.QmtRpcError("510300.SH 未返回实时行情")
        except (OSError, TimeoutError, ConnectionError, qmt_sdk.QmtRpcError) as ex:
            return QmtRpcReadiness(
                False, "quote", f"行情验证失败：{ex}", endpoint, health=health
            )
        quote = {"symbol": "510300.SH", "name": name,
                 "last_price": row.get("lastPrice")}

        account_id = str(health.get("account") or "")
        account_type = str(health.get("account_type") or "STOCK")
        try:
            accounts = client.call(
                "get_trade_detail_data", account_id, account_type, "account"
            ) or []
            if not account_id or not accounts:
                raise qmt_sdk.QmtRpcError("QMT 未返回绑定账号")
            matched = next((item for item in accounts
                            if str(_field(item, "m_strAccountID", account_id))
                            == account_id), None)
            if matched is None:
                raise qmt_sdk.QmtRpcError("返回账号与模型绑定账号不一致")
            status = str(_field(matched, "m_strStatus", "") or "")
            if not _account_ready_status(status):
                raise qmt_sdk.QmtRpcError(f"账号状态未就绪：{status}")
        except (OSError, TimeoutError, ConnectionError, qmt_sdk.QmtRpcError) as ex:
            return QmtRpcReadiness(
                False, "account", f"账号验证失败：{ex}", endpoint,
                health=health, quote=quote,
            )
        account = {"id": account_id, "status": status or "未提供"}

        try:
            orders = client.call(
                "get_trade_detail_data", account_id, account_type, "order"
            ) or []
            deals = client.call(
                "get_trade_detail_data", account_id, account_type, "deal"
            ) or []
        except (OSError, TimeoutError, ConnectionError, qmt_sdk.QmtRpcError) as ex:
            return QmtRpcReadiness(
                False, "trade_query", f"委托/成交查询失败：{ex}", endpoint,
                health=health, quote=quote, account=account,
            )
        trade_query = {"orders": len(orders), "deals": len(deals)}
        message = ("RPC 已准备就绪" if health.get("trade_mode") != "unknown"
                   else "RPC 已准备就绪；QMT 模式不可自动检测")
        return QmtRpcReadiness(
            True, "ready", message, endpoint, health=health, quote=quote,
            account=account, trade_query=trade_query,
        )
    finally:
        client.close()
