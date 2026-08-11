"""数据更新页:增量更新(全部策略池并集)、手动批量同步、夜间自动更新状态。

第一性原理:系统真正需要新鲜数据的标的 = 全部策略配置池的并集;全市场
种子化属一次性 bootstrap,不在此页。失败必须可见:哪个标的、拉哪个时间窗、
错误原因,逐条列出(来自 var/runtime/update_log.json,夜间 cron 与此页
手动触发写同一份日志)。
"""
import json
import subprocess
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

from sinan.config import load_settings
from ui.common import ROOT, get_store
from ui.theme import metric_strip, page_header, section_title, workflow_bar

LOG_PATH = ROOT / "var" / "runtime" / "update_log.json"


def _union_universe() -> list[str]:
    import yaml
    syms: set[str] = set()
    for fp in sorted((ROOT / "config" / "strategies").glob("*.yaml")):
        d = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        if d.get("sec_type", "etf") == "etf":
            syms |= {str(s) for s in d.get("universe", [])}
    return sorted(syms)


def _last_run() -> dict | None:
    if not LOG_PATH.exists():
        return None
    try:
        runs = json.loads(LOG_PATH.read_text(encoding="utf-8")).get("runs", [])
        return runs[-1] if runs else None
    except Exception:                          # noqa: BLE001
        return None


def _show_run(run: dict) -> None:
    n_fail = len(run.get("failed", []))
    metric_strip([
        ("上次更新", run.get("run_at", "—")[:16].replace("T", " "), ""),
        ("更新标的", f"{len(run.get('updated', []))} 只", ""),
        ("入库行数", f"{run.get('rows', 0):,}", ""),
        ("已最新", f"{len(run.get('up_to_date', []))} 只", "success"),
        ("失败", f"{n_fail} 只", "danger" if n_fail else "success"),
    ])
    if n_fail:
        st.error(f"{n_fail} 只标的更新失败——逐条原因:")
        st.dataframe(pd.DataFrame(run["failed"])
                     .rename(columns={"symbol": "代码", "start": "起",
                                      "end": "止", "error": "出错原因"}),
                     width="stretch", hide_index=True)
    else:
        st.success(f"全部成功,数据截止 {run.get('latest', '—')}")


def page():
    page_header("数据更新", "按全部策略池并集增量同步，并公开每个失败原因",
                eyebrow="Data operations")
    workflow_bar("data")
    uni = _union_universe()
    st.caption(f"更新范围 = 全部策略配置池的并集(当前 {len(uni)} 只 ETF);"
               "全市场种子化/重建走命令行 scripts/bootstrap_from_csv.py。")

    # ---- 上次更新状态(夜间 cron 与手动触发共用同一份日志)---------------
    run = _last_run()
    if run:
        _show_run(run)
        try:
            age_h = (datetime.now()
                     - datetime.fromisoformat(run["run_at"])).total_seconds() / 3600
            if age_h > 30:
                st.warning(f"距上次更新已 {age_h:.0f} 小时——检查夜间任务是否在跑,"
                           "或点下方按钮手动更新。")
        except Exception:                      # noqa: BLE001
            pass
    else:
        st.info("暂无更新记录;点下方按钮手动更新一次,或配置夜间自动更新。")

    # ---- 手动触发 --------------------------------------------------------
    c1, c2 = st.columns([1, 2])
    if c1.button(f"增量更新全部（{len(uni)} 只）", type="primary"):
        with st.spinner(f"逐只增量拉取 {len(uni)} 只(约 {len(uni) * 0.4:.0f} 秒)…"):
            r = subprocess.run([sys.executable,
                                str(ROOT / "scripts" / "nightly_update.py")],
                               cwd=ROOT, capture_output=True, text=True)
        st.code((r.stdout + r.stderr)[-2000:] or "(无输出)")
        st.cache_data.clear()
        st.rerun()
    with c2:
        codes = st.text_input("手动同步指定标的(逗号/空格分隔;store 没有的自动全量补数)",
                              "", key="upd_codes", placeholder="例:588000 510880")
        if st.button("同步这些标的") and codes.strip():
            syms = [s.strip() for s in codes.replace(",", " ").split() if s.strip()]
            from sinan.data.ensure import ensure_bars
            from sinan.data.sina_feed import fetch_incremental
            logs: list[str] = []
            with st.spinner("补数/增量中…"):
                still = ensure_bars(get_store(), syms, "etf", log=logs.append)
                have = [s for s in syms if s not in still]
                result = (fetch_incremental(get_store(), have, log=logs.append)
                          if have else {"failed": []})
            for msg in logs:
                st.info(msg)
            fails = still + [f["symbol"] for f in result["failed"]]
            if fails:
                st.error(f"以下标的未能同步:{'、'.join(fails)}(原因见上方日志)")
            else:
                st.cache_data.clear()
                st.success("同步完成")

    # ---- 自动定时 --------------------------------------------------------
    st.divider()
    section_title("夜间自动更新（推荐）")
    st.caption("交易日 20:00 自动增量;失败会写入上方日志并经企业微信告警"
               "(设置页配置 webhook)。在终端执行 `crontab -e` 加入:")
    st.code(f"0 20 * * 1-5 cd {ROOT} && /usr/bin/env python3 "
            f"scripts/nightly_update.py >> var/runtime/nightly.log 2>&1",
            language="bash")
