#!/usr/bin/env python3
"""夜间增量更新:全部策略配置池的并集(ETF),部分失败不阻塞其余标的。

结果写 var/runtime/update_log.json(保留最近 30 次),数据更新页读取展示:
哪些标的、拉哪个时间窗、出错原因是什么,一目了然;失败非空时经企业微信
告警(webhook 配置了才推)并以退出码 1 结束(cron 邮件可感知)。

自动定时(20:00 后,交易日):
    crontab -e 加入一行——
    0 20 * * 1-5 cd /Users/jaryoung/project/trader/sinan && \
        /usr/bin/env python3 scripts/nightly_update.py >> var/runtime/nightly.log 2>&1

手动:python3 scripts/nightly_update.py [--symbols 510300,588000]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOG_PATH = ROOT / "var" / "runtime" / "update_log.json"
KEEP_RUNS = 30


def union_universe() -> list[str]:
    """全部策略配置(sec_type=etf)标的并集,按代码排序。"""
    import yaml
    syms: set[str] = set()
    for fp in sorted((ROOT / "config" / "strategies").glob("*.yaml")):
        d = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        if d.get("sec_type", "etf") == "etf":
            syms |= {str(s) for s in d.get("universe", [])}
    return sorted(syms)


def append_log(entry: dict) -> None:
    runs = []
    if LOG_PATH.exists():
        try:
            runs = json.loads(LOG_PATH.read_text(encoding="utf-8")).get("runs", [])
        except Exception:                      # noqa: BLE001 坏日志不阻塞更新
            runs = []
    runs.append(entry)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps({"runs": runs[-KEEP_RUNS:]},
                                   ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="夜间增量更新(策略池并集)")
    ap.add_argument("--symbols", default="",
                    help="逗号分隔的标的列表;缺省 = 全部策略池并集")
    args = ap.parse_args()

    from sinan.config import load_settings
    from sinan.data.sina_feed import fetch_incremental
    from sinan.data.store import DataStore
    from sinan.live.notify import notify

    settings = load_settings()
    store = DataStore(settings.store_root)
    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               or union_universe())
    print(f"增量更新 {len(symbols)} 只(策略池并集)...")
    result = fetch_incremental(store, symbols)
    result["symbols"] = len(symbols)
    append_log(result)

    n_fail = len(result["failed"])
    summary = (f"[数据更新] {result['run_at']} 共 {len(symbols)} 只:"
               f"更新 {len(result['updated'])} 只/{result['rows']} 行,"
               f"已最新 {len(result['up_to_date'])} 只,失败 {n_fail} 只,"
               f"数据截止 {result['latest']}")
    print(summary)
    if n_fail:
        lines = [f"{f['symbol']} {f['start']}→{f['end']}:{f['error']}"
                 for f in result["failed"]]
        notify(summary + "\n" + "\n".join(lines),
               webhook=settings.wecom_webhook, level="error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
