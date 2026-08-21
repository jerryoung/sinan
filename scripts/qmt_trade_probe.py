#!/usr/bin/env python3
"""显式仿真报单探针：一次限价委托，按唯一备注确认，能撤则立即撤。"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qmt_shell import qmt_sdk  # noqa: E402
from sinan.config import load_live_profiles  # noqa: E402

_TERMINAL = {53: "canceled", 54: "canceled", 56: "filled", 57: "rejected"}
_CANCELABLE = {48, 49, 50, 51, 52, 55}


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _remark() -> str:
    # QMT 投资备注要求小于 24 字符；短随机尾避免同秒重复探针碰撞。
    return "sp#%s#%s" % (time.strftime("%H%M%S"), uuid.uuid4().hex[:6])


def run_trade_probe(
    client,
    confirmed_account: str,
    symbol: str,
    qty: int,
    limit_price: float,
    *,
    timeout: float = 15.0,
    poll_interval: float = 1.0,
    clock=time.monotonic,
    sleep=time.sleep,
    remark: str | None = None,
) -> dict:
    """执行一次有副作用探针；超时/异常后绝不自动重新调用 passorder。"""
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(symbol or "").upper()):
        raise ValueError("symbol 必须形如 510300.SH")
    if isinstance(qty, bool) or int(qty) <= 0:
        raise ValueError("qty 必须大于 0")
    if not math.isfinite(float(limit_price)) or float(limit_price) <= 0:
        raise ValueError("limit-price 必须为正数")

    health = client.call("rpc.health")
    account = str(health.get("account") or "")
    account_type = str(health.get("account_type") or "STOCK")
    if str(confirmed_account) != account:
        raise ValueError("账号确认不一致，拒绝报单")
    if not health.get("allow_trade"):
        raise ValueError("RPC 交易转发未允许")
    if health.get("protocol") != 2:
        raise ValueError("RPC 协议版本不匹配")

    probe_remark = remark or _remark()
    if not probe_remark or len(probe_remark) >= 24:
        raise ValueError("探针备注必须小于 24 字符")
    try:
        client.call(
            "passorder", 23, 1101, account, symbol.upper(), 11,
            float(limit_price), int(qty), "sinan_probe", 2,
            probe_remark, "__C__",
        )
    except Exception as exc:
        # 副作用请求异常无法证明柜台是否已收到，最多一次原则禁止重报。
        return {"status": "uncertain", "remark": probe_remark,
                "reason": "%s: %s" % (type(exc).__name__, exc)}

    deadline = clock() + float(timeout)
    cancel_requested = False
    while clock() < deadline:
        orders = client.call(
            "get_trade_detail_data", account, account_type, "order"
        ) or []
        matched = next((order for order in orders
                        if str(_field(order, "m_strRemark", "")) == probe_remark),
                       None)
        if matched is None:
            sleep(float(poll_interval))
            continue
        raw_status = int(_field(matched, "m_nOrderStatus", -1))
        sys_id = str(_field(matched, "m_strOrderSysID", "") or "")
        if raw_status in _TERMINAL:
            deals = client.call(
                "get_trade_detail_data", account, account_type, "deal"
            ) or []
            deal_count = sum(
                str(_field(deal, "m_strRemark", "")) == probe_remark
                for deal in deals
            )
            return {"status": _TERMINAL[raw_status], "remark": probe_remark,
                    "order_sys_id": sys_id, "order_status": raw_status,
                    "deal_count": int(deal_count)}
        if raw_status in _CANCELABLE and sys_id and not cancel_requested:
            try:
                client.call("cancel", sys_id, account, account_type, "__C__")
            except Exception as exc:
                return {"status": "uncertain", "remark": probe_remark,
                        "order_sys_id": sys_id,
                        "reason": "撤单结果不确定:%s: %s"
                                  % (type(exc).__name__, exc)}
            cancel_requested = True
        sleep(float(poll_interval))
    return {"status": "uncertain", "remark": probe_remark,
            "reason": ("撤单后未确认终态" if cancel_requested
                       else "等待唯一备注委托超时；未自动重报")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="QMT 仿真账号显式报单探针（会产生一笔真实委托）"
    )
    parser.add_argument("--confirm-simulation-account", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--qty", required=True, type=int)
    parser.add_argument("--limit-price", required=True, type=float)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    profiles = load_live_profiles()
    rpc = profiles.profiles[profiles.default].qmt.rpc
    token_path = Path.home() / ".qmt_rpc_token"
    token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
    client = qmt_sdk._Client()
    try:
        client.connect(rpc.host, rpc.port, token, rpc.timeout)
        result = run_trade_probe(
            client, args.confirm_simulation_account, args.symbol, args.qty,
            args.limit_price, timeout=args.timeout,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] != "uncertain" else 2
    except Exception as exc:
        print("探针拒绝或失败:%s" % exc, file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
