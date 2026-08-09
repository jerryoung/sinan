#!/usr/bin/env python3
"""
一次性种子化 CLI:把 ../data/ 三张全市场 CSV 灌入 DataStore(计划 Task 2)。

用法:
    python3 scripts/bootstrap_from_csv.py --types etf,cb
    python3 scripts/bootstrap_from_csv.py --types stock --start 2015
    python3 scripts/bootstrap_from_csv.py --types calendar   # 仅当不种子化个股时才需要

说明:
- --start 支持年份(2015)或日期(2015-06-01),按 trade_date 过滤;
- bootstrap_stock 会顺带写 trade_calendar(同一次全表扫描),
  所以 types 含 stock 时无需再加 calendar;
- --store-root 缺省取 config/settings.yaml 的 store_root(项目根下 store/)。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from trend.config import load_settings  # noqa: E402
from trend.data import bootstrap as bs  # noqa: E402
from trend.data.store import DataStore  # noqa: E402

FILES = {
    "stock": "stock_market_data.csv",
    "etf": "etf_market_data.csv",
    "cb": "bond_market.csv",
    "calendar": "stock_market_data.csv",  # 日历取自个股表 distinct trade_date
}
RUNNERS = {
    "stock": bs.bootstrap_stock,
    "etf": bs.bootstrap_etf,
    "cb": bs.bootstrap_cb,
    "calendar": bs.bootstrap_calendar,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全市场 CSV 种子化入库")
    ap.add_argument("--types", default="stock,etf,cb",
                    help="逗号分隔: stock,etf,cb(可加 calendar,仅在不含 stock 时需要)")
    ap.add_argument("--start", default=None,
                    help="起始过滤:年份(如 2015)或日期(如 2015-06-01),默认全量")
    ap.add_argument("--data-dir", default="/Users/jaryoung/project/trader/data",
                    help="三张 CSV 所在目录")
    ap.add_argument("--store-root", default=None,
                    help="DataStore 根目录,默认取 config/settings.yaml 的 store_root")
    args = ap.parse_args(argv)

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    unknown = set(types) - set(FILES)
    if unknown:
        ap.error(f"未知类型: {sorted(unknown)},可选 {sorted(FILES)}")

    root = Path(args.store_root) if args.store_root else load_settings().store_root
    store = DataStore(root)
    logger.info("store_root = {}", root)

    for t in types:
        csv_path = Path(args.data_dir) / FILES[t]
        if not csv_path.exists():
            logger.error("源文件不存在: {}", csv_path)
            return 1
        RUNNERS[t](store, csv_path, start=args.start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
