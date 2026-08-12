# 司南(SiNan)—— 轻量化多策略量化系统

> 每天基于规则给出目标仓位的个人量化系统:趋势、动量、定投都只是不同的
> "定向算法",统一输出同一份 targets——司南给方向,不预测风浪。
> 原名"轻量化趋势交易系统(trend/)",2026-08 更名。

**核心承诺:同一份 `generate_targets` 纯函数同时服务回测与实盘**,从架构上
消除回测–实盘偏差。回测引擎逐日重放它,影子模式/实盘每天 17:00 后调用它,
两边喂同一数据仓、走同一风控,输出可互相校验。

```
数据仓(parquet+DuckDB)→ 信号(generate_targets)→ ┬ 回测引擎(研究)
                                                    └ targets 文件 → QMT 薄壳(执行)
```

## 快速开始

```bash
cd sinan
pip install pandas numpy duckdb pyarrow pydantic pyyaml loguru pytest streamlit plotly

# 1) 种子化数据仓(用 ../data/ 的三张全市场 CSV,离线完成,~350MB)
python3 scripts/bootstrap_from_csv.py --types etf,cb
python3 scripts/bootstrap_from_csv.py --types stock --start 2015

# 2) 回测(输出报告三件套到 var/reports/)
python3 scripts/run_backtest.py --strategy config/strategies/combo_turtle_xsmom_x2.yaml \
    --start 2015-01-05 --end 2026-08-07

# 3) 影子模式:拉数 → 质检 → 生成当日目标仓位(var/runtime/targets/)
python3 scripts/shadow_update.py --strategy config/strategies/combo_turtle_xsmom_x2.yaml

# 4) 操作面板(数据 → 回测 → 实盘研究工作台)
streamlit run app.py

# 测试(277 个,含回测快照、事件追踪与实盘配置接线测试)
python3 -m pytest tests/ -q
```

## 目录结构

```
sinan/             Python 包(核心代码,与项目同名)
  ├ data/          DataStore(parquet 按年分区 + DuckDB)· bootstrap · 日更 · 质检
  │                └ sources/  数据源适配层:DataSource 抽象 + 注册表,
  │                  akshare / tushare / qmt 各自单文件适配器,新源即插即用
  ├ universe/      交易规则推断(板块/T+0/涨跌幅)· 转债条款与强赎事件
  ├ signal/        SignalContext + 策略注册表 + strategies/(全部策略实现)
  ├ backtest/      engine(逐日循环)· execution_model · costs · report(指标+HTML)
  ├ live/          targets(风控裁剪/留痕/校验)· profiles(实盘配置引用/删除保护)
  │                · broker · reconcile · notify
  └ risk.py        组合层风险原语(limit_positions),回测与实盘共用的中立模块
config/            settings.yaml(本金/路径/执行/风控)· live_profiles.yaml(实盘配置)
                   · rules.yaml(品种规则)· strategies/*.yaml(含 live_profile 引用)
scripts/           bootstrap / daily_update / shadow_update / run_signal / run_backtest / 研究脚本
qmt_shell/         sinan_qmt(ECS 统一脚本,唯一必填 SHARE_DIR:执行全部策略 targets
                   + 备注「策略ID#日期#序号」归因 + 策略虚拟账本 + fills 回写 + RPC
                   转发;账号/模拟实盘从 QMT 绑定关系直读)· qmt_sdk(本地 SDK,
                   与内置 API 同名同形,任意 API 经通用转发覆盖)
app.py + ui/       Streamlit 操作面板(app.py 路由入口,ui/ 页面模块)
docs/RESEARCH.md   研究档案:全部实验结论表与决策记录
var/               本机状态,git 忽略:store(数据仓)· runtime(targets/fills)· reports
```

## 策略清单(sinan/signal/strategies/)

| 策略 | 思想 | 定位 |
|---|---|---|
| `turtle_s1` | 海龟快系统:20/10 突破 + 盈利过滤器 + 55 日兜底 + 2N 止损 | **主力**,单策略最优 Calmar |
| `xsmom` | 12-1 截面动量 top5 + 绝对动量过滤 | **主力互补**(与海龟 ρ≈0.57) |
| `combo` | 多策略资金加权合成,单配置/单 targets | **当前跟踪配置的载体** |
| `dca` | 定投计划的虚拟账户重放(周/月/季,可下跌加码) | 独立现金流策略,影子可跟踪 |
| `donchian` | 55 日突破 + 3×ATR 吊灯止损(海龟慢系统变体) | S2 基准 + 回测快照锚 |
| `livermore` | 《股票作手操盘术》市场要诀状态机 + 试探加码 | 低换手互补槽(~1 次/年) |

> 2026-08-09 精简:`ma_cross`/`tsmom`(对照组垫底)、`supertrend`(与海龟
> ρ=0.86 同源)、`rebalance`(基准结论已沉淀)、`cadence`(结论"勿降频")
> 验证后移除,结论与数字保留在 [docs/RESEARCH.md](docs/RESEARCH.md),
> 代码可从 git 历史找回。配置只保留每族最优(6 份)。

## 当前跟踪配置(影子模式)

| 配置 | 内容 | 全期回测(2015→2026) |
|---|---|---|
| `combo_turtle_xsmom_x2` | 海龟×2 + 截面动量×2 各半资金,26-ETF 混合池 | 年化 11.1% / 回撤 −16.9% / 夏普 1.05 |
| `dca_cn_ndx_gold` | 沪深300+纳指+黄金 月定投 4000 元,跌破年线加码 2× | 同窗口 IRR ~15%,最大浮亏 −9% |

配套风险配置:DD_tol=30%,`risk.max_positions=12`(引擎与实盘同一 `limit_positions`
语义),执行层 apply_risk 四重裁剪 + 单标的 34% 兜底。

## 三模块风险框架(全系统的定仓与停止依据)

1. **信号存活门槛** `W/L > (1−p)/p`,期望 `EV = p·W − (1−p)·L`
2. **风险优先定仓** 每笔风险 `x ≈ DD_tol ÷ 最坏连亏次数 ÷ 预算持仓数`;
   组合层用 `n_eff = n·f`、`f = √[(1+(M−1)ρ)/M]` 修正——**低相关新增才有增益**
3. **策略死亡诊断** 连亏重现周期 + 置信带停止线 / SPRT 序贯检验

## 关键工程约定(改回测逻辑时必须保持)

- **无未来函数**:信号只用 ≤ 当日数据,执行一律 T+1;targets 的
  `date`=执行日、`data_cutoff`=T−1。
- **复权会计**:持仓记复权股数 q_eff,成交按原始价整手,市值按后复权收盘,
  分红=红利再投;**adj_factor 缺失语义是组内 ffill,永远不是 1.0**(事故记录见档案)。
- **targets 契约**:`targets_{策略名}_{YYYYMMDD}.json`,多策略互不覆盖;
  checksum 只覆盖权重契约,`live_profile` 记录实盘配置 ID,解析后的 `qmt`
  保持薄壳兼容;`ref_orders` 参考委托区仅供影子/人工执行参考。
- **策略调用**:引擎与 run_signal 一律走 `call_strategy(cfg, ctx)`(调用约定的
  唯一实现);ctx 可见列由 `SignalContext` 统一裁到后复权 OHLCV,两侧恒等。
  dca 的计划起始日在回测中以回测窗口起点为准(配置 `start` 只锚定影子/实盘),
  该语义由策略在 `@register` 自声明,引擎不硬编码策略名。
- 成本:ETF 单边 5bp、个股 8bp;年化基准 244 交易日;蒙特卡洛固定 seed。

## 操作面板(app.py + ui/)

`streamlit run app.py`。平台使用统一深色研究工作台；策略看板顶部直接展示
“策略更新 → 回测验证 → 影子/实盘运行”状态链。左侧分组导航(st.navigation 多页架构,页面相互隔离、
按需执行);"当前策略(全局)"选择器只在量化策略模块的页面右上角出现,
切换页面选择保持:

- **数据**:行情查询(覆盖数据仓实际存在的 ETF/个股/转债 + K 线)·数据仓概况·数据更新。
- **回测与实盘**:策略看板(影子/实盘统一入口:收益统计、净值曲线、持仓详情、
  交易记录,targets 详情可展开;有 QMT fills 即实盘口径,否则影子重放)·
  回测(一次回测=一份存档报告,配置快照可 diff)。
- **策略与设置**:策略配置(表单/YAML双模式,只引用命名实盘配置)·设置(“系统设置/实盘配置”双页签;实盘配置
  支持新增、编辑、设为默认和删除,默认或被策略引用时禁止删除并列出策略)
- **数据源链**:设置页按优先级选择 `sina`、`akshare`、`tushare`、`qmt`;
  单源不可用时自动降级到下一项,空链、重复项和空名称在保存前直接拒绝。
  QMT RPC 连接参数在每份“实盘配置”中维护；QMT 数据源使用默认实盘配置。

## 研究档案

全部实验结论表(策略族对照、池规模、v3 选样与滚动重选、定投矩阵、数据源
评测、数据质量事故……)沉淀在 **[docs/RESEARCH.md](docs/RESEARCH.md)**,
按时间与主题组织;README 只保留当前有效的结论。

## 已知边界(有意为之)

- 不处理停牌流水(缺 K 线视同停牌顺延);止损按收盘价触发;仅做多;
  组合无资金动态再平衡。
- 影子模式的"每日信号 vs 回测同期输出"自动比对(M3)未实现,机件已齐备。
- 对账仅告警不阻断:账实不符会在看板与推送里显眼提示,但不会自动停掉
  次日执行(是否升级为硬阻断是风控决定,见 sinan/live/reconcile.py)。
- 数据源:akshare 主用、tushare 备源(token 缺失自动降级跳过),链路在
  `settings.data.sources` 配置、按序逐调用切换;交易机可把 `qmt` 源
  (RPC 直连行情)加在首位。新增数据源只需在 `sinan/data/sources/` 加一个
  `{名字}_source.py` 并 `@register_source` 注册,调用方零改动。
  baostock/yahoo 实测不合格(详见档案)。

> 本系统为研究用途,不构成投资建议。实盘前请完成影子验证与程序化交易报备。

---

## 关于作者 · 交流与服务

司南是我从零打造并公开演进的个人量化系统:回测–实盘一致性架构、多策略
影子跟踪、QMT 自动化执行,每一步的实验数据与踩坑记录都沉淀在
[研究档案](docs/RESEARCH.md) 里。如果它对你有启发,欢迎关注交流:

| 公众号「可转债量化实盘」 | 个人微信(JerryWu) |
|:---:|:---:|
| <img src="docs/img/wechat_mp.jpeg" width="220" alt="公众号二维码"> | <img src="docs/img/wechat_me.jpeg" width="220" alt="个人微信二维码"> |
| 可转债/ETF 量化实盘记录、策略研究与复盘 | 备注「司南」,拉你进量化交流群 |

**可提供的服务**(欢迎微信勾兑):

- 🛠 **量化系统定制开发**:数据管道 → 回测引擎 → 实盘对接的全链路搭建,
  或在司南基础上按你的策略与券商环境定制;
- 📊 **策略回测与验证**:你出想法,我出严谨的回测(无未来函数、真实费用/
  手数/涨跌停模拟)与诚实的结论——包括"这个策略不行";
- 🤖 **QMT/miniQMT 自动化**:targets 桥接、下单算法、多策略共账号归因、
  远程安全访问,少走三个月弯路;
- 💬 **一对一咨询**:量化工程架构、数据治理、从手工交易到程序化的迁移路径。

> 三年可转债/ETF 实盘,系统化方法论 + 全部公开可验证的工程实现——
> 不卖课、不荐股、不代客理财,只做技术与方法的交付。
