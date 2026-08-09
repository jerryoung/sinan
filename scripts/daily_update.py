#!/usr/bin/env python3
"""
每日数据更新入口(17:00 cron,方案 §5.2)。

流程:增量拉取当日行情(akshare 主源,tushare 备源)→ 入库 → 质检;
质检不通过则告警并以退出码 1 结束——绝不触发后续信号计算。

用法:
    python3 scripts/daily_update.py            # 默认今天
    python3 scripts/daily_update.py --date 2026-08-04
"""
import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="每日增量数据更新(17:00 cron)")
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="更新日期,默认今天")
    args = ap.parse_args()

    import pandas as pd

    from trend.config import load_settings
    from trend.data.sources.akshare_source import AkshareSource
    from trend.data.store import DataStore
    from trend.data.update import run_daily
    from trend.live.notify import notify

    settings = load_settings()
    store = DataStore(settings.store_root)

    def notify_fn(msg, **kw):
        notify(msg, webhook=settings.wecom_webhook, **kw)

    # 主源 akshare;tushare 备源可选(token 缺失等不可用时降级为单源)
    sources = [AkshareSource()]
    try:
        from trend.data.sources.tushare_source import TushareSource
        sources.append(TushareSource())
    except Exception as e:  # noqa: BLE001
        notify_fn(f"tushare 备源不可用,仅用主源: {e}", level="warning")

    day = pd.Timestamp(args.date)
    try:
        res = run_daily(store, sources, day, notify_fn)
    except Exception as e:  # noqa: BLE001
        notify_fn(f"[数据更新] {args.date} 异常终止: {e}", level="error")
        return 1
    ok = getattr(res, "ok", bool(res))   # 兼容 UpdateResult 与裸 bool 两种返回
    if not ok:
        notify_fn(f"[数据更新] {args.date} 质检未通过,不触发信号计算",
                  level="error")
        return 1
    notify_fn(f"[数据更新] {args.date} 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
