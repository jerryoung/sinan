"""
目标仓位文件(方案 §8.1 / §8.3 / §9.5)——执行层与信号层之间的唯一契约。

流程:14:35 run_signal 生成 targets_YYYYMMDD.json → 14:45 QMT 薄壳读取执行。
两个进程只通过这个文件说话,所以文件必须自带三重防御:

  1. apply_risk        执行层强制风控:信号层的输出在这里被视为"不可信输入",
                       依次裁剪 单标的上限 → 总仓位(等比缩) → 单日调仓额(按比例
                       缩 Δ) → 流动性(逐标的截断)。
  2. build_payload     运行留痕(§9.5):生成时间戳、数据截止、参数指纹、校验和,
                       任何一天的决策可完整复现。
  3. load_and_validate 时效校验(§8.3):非当日 / 被篡改 / 超时效的文件一律
                       抛 TargetsInvalid 拒绝执行——宁可空仓,不执行错单。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..risk import limit_positions   # 与回测引擎共用的持仓数上限实现

_EPS = 1e-12   # 浮点比较容差:等比缩放后 Σw 会带 1e-17 级噪声,不应误触发风控


class TargetsInvalid(Exception):
    """targets 文件校验失败(异常消息含具体原因),调用方必须拒绝执行。"""


# --------------------------------------------------------------------------
# 校验和与参数指纹:写入侧(这里)与执行侧(QMT 薄壳)各持一份同算法拷贝
# --------------------------------------------------------------------------
def _canon(obj) -> str:
    """规范化 JSON:键排序 + 紧凑分隔符,保证同一内容永远得到同一字节串。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def targets_checksum(targets: dict) -> str:
    """对 targets 内容的 sha256。"""
    return hashlib.sha256(_canon(targets).encode("utf-8")).hexdigest()


def params_fingerprint_of(params: dict) -> str:
    """参数 dict 的 sha256 前 12 位:参数任何改动都会改变指纹,与键序无关。"""
    return hashlib.sha256(_canon(params).encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# 执行层强制风控(§8.3):不信任信号层
# --------------------------------------------------------------------------
def apply_risk(
    weights: dict,
    *,
    current_weights: dict,
    total_asset: float,
    adv20: dict[str, float] | None,
    settings,
) -> tuple[dict, list[str]]:
    """
    对信号层输出的目标权重依次施加四道裁剪,返回 (裁剪后权重, 触发说明列表)。

    目标全集 = 信号给出的标的 ∪ 当前持仓标的:持仓中未被信号提及的标的
    显式给目标 0(清仓腿)——否则 QMT 薄壳只按 targets 里的标的算差额,
    残留持仓永远卖不掉。

    裁剪次序(每一步都在上一步结果上继续):
      1. 单标的上限 max_weight_per_symbol —— 逐标的截到 cap
      2. 总仓位上限 max_total_weight     —— Σw 超限则全体等比缩(保持相对比例)
      3. 单日调仓额 max_daily_turnover   —— Σ|Δw| 超限则按比例缩 Δ,
         即向当前持仓收缩(减速调仓),而不是砍绝对权重
      4. 流动性 liquidity_pct_adv20      —— |Δw|×total_asset ≤ adv20×pct,
         逐标的截断单笔调仓金额(分多日建/清仓)
    """
    risk = settings.risk
    msgs: list[str] = []
    cur = {str(s): float(w) for s, w in (current_weights or {}).items()}
    tgt = {str(s): float(weights.get(s, 0.0))
           for s in set(map(str, weights)) | set(cur)}

    # 0) 仅做多:负权重直接裁 0(信号层不应给出,但执行层不信任它)
    for s, w in tgt.items():
        if w < 0:
            msgs.append(f"负权重裁剪: {s} {w:.4f}→0")
            tgt[s] = 0.0

    # 0.5) 持仓数上限:先定"哪些标的在场",再做权重裁剪
    tgt, m = limit_positions(tgt, cur, risk.max_positions)
    msgs += m

    # 1) 单标的上限
    cap = risk.max_weight_per_symbol
    for s, w in tgt.items():
        if w > cap + _EPS:
            msgs.append(f"单标的上限: {s} {w:.4f}→{cap:.4f}")
            tgt[s] = cap

    # 2) 总仓位上限:等比缩(缩后单标的必然仍 ≤ cap)
    tot = sum(tgt.values())
    if tot > risk.max_total_weight + _EPS:
        k = risk.max_total_weight / tot
        msgs.append(f"总仓位上限: Σw {tot:.4f}→{risk.max_total_weight:.4f}"
                    f"(等比×{k:.4f})")
        tgt = {s: w * k for s, w in tgt.items()}

    # 3) 单日调仓额:缩 Δ 而非缩 w —— 买腿少买、卖腿少卖,次日信号重发会继续逼近
    delta = {s: tgt[s] - cur.get(s, 0.0) for s in tgt}
    turnover = sum(abs(d) for d in delta.values())
    if turnover > risk.max_daily_turnover + _EPS:
        k = risk.max_daily_turnover / turnover
        msgs.append(f"单日调仓额: Σ|Δw| {turnover:.4f}→"
                    f"{risk.max_daily_turnover:.4f}(Δ×{k:.4f})")
        tgt = {s: cur.get(s, 0.0) + delta[s] * k for s in tgt}

    # 4) 流动性:单笔调仓金额 ≤ 近 20 日均成交额 × pct;缺 adv20 数据的标的跳过
    if adv20 and total_asset > 0:
        for s in tgt:
            adv = adv20.get(s)
            if adv is None or adv <= 0:
                continue
            dw = tgt[s] - cur.get(s, 0.0)
            max_dw = adv * risk.liquidity_pct_adv20 / total_asset
            if abs(dw) > max_dw + _EPS:
                msgs.append(f"流动性截断: {s} |Δw| {abs(dw):.4f}→{max_dw:.4f}")
                tgt[s] = cur.get(s, 0.0) + (max_dw if dw > 0 else -max_dw)

    # 消除等比缩放引入的浮点噪声,并保证非负
    tgt = {s: round(max(w, 0.0), 8) for s, w in tgt.items()}
    return tgt, msgs


def build_ref_orders(
    weights: dict,
    *,
    current_weights: dict,
    capital: float,
    prices: dict,
    lot_sizes: dict,
    band: float = 0.0,
) -> list[dict]:
    """
    参考委托单(影子模式/人工执行用):按名义本金与参考价把权重差换算成
    整手股数。**仅供参考**——实盘 QMT 薄壳仍按执行时点的真实账户资产与
    行情自算差额;参考价是数据截止日收盘,与次日 14:45 执行价有隔夜偏差。
    |Δw| < band 的标的跳过(与引擎再平衡带宽同语义)。
    """
    orders = []
    for s in sorted(set(weights) | set(current_weights)):
        tw = float(weights.get(s, 0.0))
        cw = float(current_weights.get(s, 0.0))
        dw = tw - cw
        if abs(dw) < max(band, 1e-9):
            continue
        px = prices.get(s)
        if px is None or not (px := float(px)) > 0:
            continue
        lot = int(lot_sizes.get(s, 100))
        qty = int(abs(dw) * capital / px // lot * lot)
        if qty <= 0:
            continue
        orders.append({
            "symbol": str(s), "side": "buy" if dw > 0 else "sell",
            "target_w": round(tw, 6), "current_w": round(cw, 6),
            "delta_w": round(dw, 6), "ref_price": round(px, 4),
            "lot_size": lot, "qty": qty, "est_amount": round(qty * px, 2),
        })
    return orders


# --------------------------------------------------------------------------
# payload 生成 / 落盘 / 校验
# --------------------------------------------------------------------------
def build_payload(
    weights: dict,
    *,
    strategy_name: str,
    date,
    data_cutoff,
    params_fingerprint,
) -> dict:
    """
    组装 targets 文件内容(§9.5 运行留痕)。

    date 是**执行日**(QMT 薄壳当日校验、当日执行的那一天),由调用方显式给出;
    data_cutoff 是信号所依据的数据截止时点。生产节奏下两者相差一天
    (17:00 日更 → 次日 14:35 出信号、14:45 执行),与回测"t 收盘信号、
    t+1 执行"的时滞结构严格对齐——若把 date 取成 data_cutoff,薄壳按
    当日找文件会永远校验失败。
    params_fingerprint 既可直接传参数 dict(自动取 sha256 前 12 位),
    也可传现成指纹串(调用方已自行计算时)。
    """
    fp = (params_fingerprint if isinstance(params_fingerprint, str)
          else params_fingerprint_of(dict(params_fingerprint)))
    targets = {str(s): round(float(w), 6) for s, w in sorted(weights.items())}
    return {
        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_cutoff": str(data_cutoff),
        "strategy": strategy_name,
        "params_fingerprint": fp,
        "targets": targets,
        "checksum": targets_checksum(targets),
    }


def targets_path(targets_dir, strategy: str, date) -> Path:
    """targets 文件路径 —— 文件名契约的唯一定义处(写入/校验/对账共用)。"""
    ymd = pd.Timestamp(date).strftime("%Y%m%d")
    return Path(targets_dir) / f"targets_{strategy}_{ymd}.json"


def save_targets(payload: dict, targets_dir) -> Path:
    """写 targets_{策略名}_{YYYYMMDD}.json —— 多策略影子跟踪互不覆盖。"""
    d = Path(targets_dir)
    d.mkdir(parents=True, exist_ok=True)
    fp = targets_path(d, payload["strategy"], payload["date"])
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    return fp


def load_and_validate(targets_dir, date, settings, strategy: str) -> dict:
    """
    §8.3 时效校验:文件存在、date/strategy 匹配、checksum 一致、
    generated_at 距今 ≤ risk.targets_max_age_hours,任一不满足抛 TargetsInvalid。

    非当日文件拒绝执行——旧信号在新行情下执行等于随机下单。
    """
    day = pd.Timestamp(date).strftime("%Y-%m-%d")
    fp = targets_path(targets_dir, strategy, date)
    if not fp.exists():
        raise TargetsInvalid(f"targets 文件不存在: {fp}")

    try:
        payload = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise TargetsInvalid(f"targets 文件无法解析: {e}") from e

    if payload.get("date") != day:
        raise TargetsInvalid(
            f"targets 日期不符: 文件内为 {payload.get('date')},期望 {day}")

    if payload.get("strategy") != strategy:
        raise TargetsInvalid(
            f"targets 策略不符: 文件内为 {payload.get('strategy')},期望 {strategy}")

    if targets_checksum(payload.get("targets", {})) != payload.get("checksum"):
        raise TargetsInvalid("targets 校验和不符,文件可能被篡改或写入损坏")

    try:
        gen = datetime.fromisoformat(str(payload.get("generated_at")))
    except (TypeError, ValueError) as e:
        raise TargetsInvalid(f"generated_at 无法解析: {e}") from e
    age_h = (datetime.now() - gen).total_seconds() / 3600.0
    max_h = settings.risk.targets_max_age_hours
    if age_h > max_h:
        raise TargetsInvalid(
            f"targets 超出时效: 生成于 {payload['generated_at']},"
            f"已 {age_h:.1f}h > 上限 {max_h}h")
    return payload
