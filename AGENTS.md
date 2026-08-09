# AGENTS.md

本文件为 AI 编码工具(Claude Code / Codex / Cursor 等)在本仓库工作的指南;
Claude Code 经 CLAUDE.md 的 @AGENTS.md 导入。

## 项目定位

司南(SiNan):个人多策略仓位导航系统(研究用途,影子模式运行中,未实盘)。
**核心承诺:同一份 `generate_targets` 纯函数同时服务回测与实盘**——回测引擎逐日
重放它,影子/实盘每天调用它,任何改动都不能破坏这一对称性。用户是策略研究者,
分析与结论用中文;代码沿用中文 docstring + 推导注释风格。

文档分工:README.md 是用户手册(当前有效结论);docs/RESEARCH.md 是按主题沉淀的
实验档案(数字为当时口径)。回测逻辑或参数变更导致关键数字变化时,同步 README。

## 常用命令

无构建系统、无 lint 配置。依赖:`pip install pandas numpy duckdb pyarrow pydantic pyyaml loguru pytest streamlit plotly akshare`

```bash
python3 -m pytest tests/ -q                          # 全量测试(194 个)
python3 -m pytest tests/test_engine.py -q            # 单文件
python3 -m pytest tests/test_dca.py::test_strategy_yaml -q   # 单测试

python3 scripts/bootstrap_from_csv.py --types etf,cb # 种子化数据仓(读 ../data 的 CSV,仓库外)
python3 scripts/run_backtest.py --strategy config/strategies/combo_turtle_xsmom_x2.yaml \
    --start 2015-01-05 --end 2026-08-07
python3 scripts/shadow_update.py --strategy config/strategies/dca_cn_ndx_gold.yaml  # 拉数+质检+出 targets
python3 scripts/nightly_update.py                    # 夜间增量(策略池并集,失败写 update_log.json)
streamlit run app.py                                 # 操作面板(外层 .claude/launch.json 有预览配置)
```

## 架构(改代码前必须知道的大图)

```
var/store(parquet+DuckDB)→ SignalContext → generate_targets ┬→ backtest/engine(研究)
                                                             └→ live/targets → var/runtime/targets/*.json → QMT 薄壳
```

- **核心契约模块**(全线被 import,改签名全局波及):`sinan/config.py`(Settings/
  StrategyCfg,相对路径锚定项目根)、`calendar.py`、`universe/instruments.py`、
  `signal/base.py`(SignalContext + `@register` 策略注册表)、`data/store.py`、
  `backtest/result.py`。
- **策略调用约定**:引擎与 `run_signal` 都必须 `fn(ctx, **cfg.params, lookback=cfg.lookback)`
  ——漏传 lookback 会造成回测/实盘静默分叉(修过一次)。策略内部只能用 ≤ today
  的数据(无未来函数),执行一律 T+1。
- **引擎会计口径**(engine.py docstring 有推导,快照测试锁定):持仓记复权股数
  q_eff,成交按原始价整手,估值按后复权收盘,分红=红利再投;先卖后买、现金不透支、
  涨跌停/停牌/强赎全模拟。改口径必须重新生成 `tests/fixtures/snapshot_nav.csv`
  并说明原因。
- **targets/fills 契约**:`targets_{策略名}_{YYYYMMDD}.json`(`date`=执行日、
  `data_cutoff`=T−1;checksum 只覆盖权重;`qmt` 字段原样透传策略级账号/下单算法;
  `ref_orders` 仅供参考);薄壳回写 `fills_{策略名}_{YYYYMMDD}.json`(含
  trade_mode=sim/real 由 QMT 侧上报、total_asset、positions、fills)——
  看板与 run_signal 的持仓真相来源。qmt_shell 的 checksum 算法与
  `sinan/live/targets.py` 两侧各持一份拷贝,必须逐字节一致。策略净值统一由
  `sinan/live/ledger.py` 派生(有 fills 用账户真值,否则 targets 影子重放)。
- **风险层级**:策略参数 cap/x_risk → 引擎 Σ≤1 + `max_positions`(与 live 共用
  `limit_positions`:已持仓优先)→ live `apply_risk` 多重裁剪 → 单标的 34% 兜底。
- **配置解析优先级**:capital 为 CLI `--total-asset` > 策略 YAML > settings.capital;
  `rebalance_band` 策略级覆盖全局(定投小增量需 0.005 < 默认 0.02)。
- **特例语义**:dca 的 `params.start` 在回测中被引擎覆盖为窗口首日(配置值只锚定
  影子/实盘计划);xsmom 逐日定仓,是"入场时刻锁定权重"约定的唯一例外。
- **面板只做编排与展示**,不含策略/引擎逻辑:app.py 是 st.navigation 路由入口
  (左侧分组:量化策略/数据中心),页面在 ui/ 包、共享层 ui/common.py;
  回测页遵循"一次回测=一份报告"(HTML 归档 + .cfg.yaml 快照 + .result.json
  数据,统一由 show_report 渲染)。

## 关键约定与已知的坑

- **adj_factor 缺失语义是组内 ffill,永远不是 1.0**——曾因 fillna(1.0) 炸出
  ±264% 伪收益(bootstrap 与 store.write_bars 双层防护,回归测试锁定)。
- 成本:ETF 单边 5bp、个股 8bp;年化基准 `TRADING_DAYS=244`;蒙特卡洛固定 seed。
- 数据加载:优先 var/store 缓存,缺失才联网(sina 主用增量,tushare 备源需
  `~/.tushare_token`,token 严禁写入仓库/日志)。
- 测试风格:合成序列忌纯横盘(恰贴通道边界会连环触发,基底加微降漂移);策略
  测试用事件追踪(events 列表)断言到具体规则分支,并保住这些断言。
- Streamlit:所有页签每次 rerun 全部执行,一处异常全页面崩;表单控件 key 已绑定
  文件内容指纹(防止旧控件状态在保存时回写旧参数);st.dataframe 对 NaN 显示
  "None" 且 Styler na_rep 不生效——显示层预格式化字符串绕开。
- `var/` 为本机状态(数据仓/持仓痕迹/报告),git 忽略;书籍 PDF 与全市场 CSV
  在外层 trader 目录,不属于本仓库。
- 已知简化(有意为之,勿"修复"):止损按收盘价触发、仅做多、无资金动态再平衡、
  不处理停牌流水。
