# Stage 7 参数来源与兼容规则

开发基线：`34a6847e76d2fb5fb46ab7e8acab2ddfabb0c8d2`。此表先于校验实现建立；不修改三个原配置文件或原运行模块。配置快照只是声明，不能认证账户或交易所能力。

## 权威层次（不做无说明覆盖）

| 类别 | 字段位置 / 默认 | 单位与约束 | 使用者 / 权威来源 | 覆盖规则 |
| --- | --- | --- | --- | --- |
| 主配置实例、模式和标的 | config.yaml: instance_id=sol-ai-local, dry_run=true, trade_symbol=SOLUSDT, symbols=SOL/BTC/ETH USDT | 独立实例名、布尔、标的集合；实盘封禁不能解除 | app/config.py；独立主配置声明，不是账户身份认证 | 显式配置；跨 manifest 身份必须一致，不取最后值 |
| 主配置风险上限 | risk.max_loss_per_trade=5, max_leverage=5, max_margin_ratio=0.2, max_positions=1 | USDT 止损预算、倍、权益比例、持仓数 | app/config.py / app/risk/engine.py | 与同口径准入上限取更严格值；不是运行时 AccountHardLimits |
| 主配置日损失 | risk.daily_loss_limit=20 | USDT；本地风险日净现金盈亏加负浮盈口径 | app/risk/engine.py / portfolio.metrics | 与 Admission 的累计负净交易结果不是同一口径，不机械合并；未来需两个指标和日界证据 |
| 主配置次数 | risk.max_trades_per_day=3, max_consecutive_losses=2 | 本地日 ENTRY 订单数；已平交易连续亏损数 | 原账本/风控代码 | 不把新准入的已用次数加待开仓名额当成同一计数；分别保留 |
| 运行时账户硬限制 | PaperRiskSnapshot.limits.* | USDT、币数量、名义额、保证金、倍、比例、计数，默认 None | 将来的可信独立 Paper 账本供应者 | 必须显式提供、认证、新鲜；与同口径政策取更严格值，不能由配置或 paper.initial_balance 伪造 |
| 准入政策 | admission.yaml / AdmissionPolicy | policy v1；单笔 5、日损失 20、杠杆 5、保证金比例 .2、绝对保证金 100、名义额 500、数量 100、权益风险比例 .01、最小预算 .5 | app/admission/policy.py | 配置可在契约范围内声明；不得覆盖账户硬限制或把 USDT/数量混为一谈 |
| 单笔申请及建议 | AdmissionRequest.risk_budget_usdt/leverage；TradeSetup.risk_budget/position_limit_advice | 损失预算不是下单金额；BASE 数量不是 USDT 名义额 | 单笔计划/请求提供方，仅声明 | 无默认账户授权；不能覆盖政策、运行时硬限制和已审批精确数量 |
| 交易所规则 | ExchangeConstraints / ExitVenueRules | BASE 步长/最小/最大量、USDT 最小额、价格 tick、倍、合约类型及能力 | 将来的可信规则与成交适配器 | 非配置默认；symbol、exchange、linear USDT、精度和原子保护能力需核对；verified/hash 不构成认证 |
| 退出政策 | exit-policy.yaml: 1R/30%、2R/40%、Runner30%；cost_covered；3R 激活、1R 回撤；持仓 3600 秒 | 比例精确和 1；R 距离基于首笔确认成交冻结锚点；参数只收紧/按已绑定政策 | app/exits/policy.py / Stage 6 纯状态机 | 当前仓位继续绑定原 seed/policy；文件变化只形成新快照，不热替换、不迁移历史 |
| RR 描述 | RRCalculation.calculation_version=linear-usdt-rr/v1 | 整单净 RR=净收益/含成本初始止损损失；quantity 为假设 BASE 数量 | app/setups/rr.py calculate_rr | 不覆盖目标或评分；新假设产生独立结果；静态目标场景不是退出路径收益预测 |
| 评分描述 | trade-scorecard/v1, plan-quality/v1, ScoringContext.max_data_age_seconds=300 | 七原始维度和第八综合描述，0–100 不是胜率 | app/setups/scorecard.py | 不改评分算法；内容重算比对，不用高分覆盖契约冲突 |
| 风险分层 | admission.tiers / markets / dimension_floors | 总分阈值、各维下限、等级与市场的 RR 下限/风险比例 | AdmissionPolicy | 有效 RR 下限取 global/tier/market 最大值；不是修改 RR 计算或放宽 global 下限 |
| 成本 | main.risk.taker_fee_rate=.0005, slippage_bps=10；admission 双边最低费率 .0005、最低滑点10/最高30；exit 退出费率 .0005、滑点10 | 小数费率、bps；.0005=.05%，10bps=.1% | 各自模块预算口径 | 只比较同一条腿/同一单位；保守预算可高于下界，不求全值相等，不重复加总费用 |
| 资金费与时长 | TradeSetup.cost_assumptions.funding_cost_usdt / assumed_holding_seconds 默认 None；admission 最大假设86400；exit 最长3600 | 有符号 USDT 总额；秒 | 单笔显式情景假设 | 缺失不是0；静态 pro-rata 资金费不覆盖分批路径，Runner 完整净收益建模返回 UNSUPPORTED |

主配置还有行情连接、Dashboard、Alerts、Review、runtime、四策略参数：只冻结其显式值/既有默认值，不调用对应服务。各叶字段的最终来源、单位、默认应用标记、约束及覆盖规则由配置编译器完整列出；下面的逐字段目录在实现完成后生成并核对原模型与原策略默认值。

## 新鲜度与失效责任

| 数据 | 已验收窗口默认（秒） | 谁检查 / 影响 |
| --- | --- | --- |
| 主流程行情 | main.data.stale_after_seconds=15 | 原 RiskEngine；不被新配置替换 |
| 准入行情/覆盖非结构项/失效确认 | admission.max_market_age_seconds=15 | 新组装检查与后续 Admission；计划/准入有效性失效 |
| 结构及结构独立确认 | max_structure_age_seconds=300 | 结构供应者、Admission；不等于认证通过 |
| 账户与日界 | max_account_age_seconds=5，显式 day_started_at/day_ends_at | 可信账户供应者/锁内复核；审批到期不能复用 |
| 交易所规则 | max_exchange_age_seconds=3600 | 规则适配与下单锁内复核 |
| 费率、滑点与资金费假设 | max_cost_age_seconds=300 | 预算假设检查；不能把资金费缺失填0 |
| Scorecard 结果 | max_scorecard_age_seconds=30；其内部原始输入窗口另为300 | 结果年龄与内部证据年龄分别检查 |
| AdmissionDecision | decision_ttl_seconds=5，且受全部关联数据截止约束 | 后续准入消费；配置快照没有无限执行权限 |
| 退出报价 / Swing、ATR、趋势失效证据 | exit.market_max_age_seconds=5 / evidence_max_age_seconds=30 | Stage 6 / 未来退出适配；不影响旧仓绑定政策的必要保护 |

不能将上述 TTL 压成一个统一值；配置合法性不宣称运行时已供应任何这些数据。

## 版本与消费顺序

新 manifest 显式声明 bundle/schema、main 契约、Admission v1、Exit v2、TradeSetup v1、RR v1、Scorecard/rubric v1、ExitState/checkpoint v2。未知版本拒绝；同版本内容变更仍产生不同摘要。各原文件继续独立存在。

新计划和审批要绑定完整 bundle 摘要、各政策内容摘要、setup/RR/card 内容及实例；旧审批没有 bundle 绑定时是 INCOMPLETE，不自动追认。内容哈希不是签名。已经存在的 ExitState 仍由原 seed/policy 重放，新配置错误只阻止新组装，绝不发出停止旧仓保护的指令。

## 逐字段有效参数目录（本次脱敏模板）

以下 235 行由严格编译结果与原模型默认目录核对。有效值是本次模板值，不是账户现状。D=模型/既有策略默认；E=显式文件；派生项注明计算来源。JSON 字符串形式的十进制不是金额/单位互换许可。完整逐字段约束、来源摘要与覆盖规则也可由 --json --parameters 获取。

主配置安全必填字段即使有历史默认值也必须显式存在；manifest 除 score_context_max_age_seconds 外均须显式声明（live_allowed 也必须显式 false）。主配置时区离线目录当前仅支持 Asia/Kuala_Lumpur、UTC，Bridge 声明仅支持本机 8766；这是新组装支持范围，不修改旧运行模块。

策略 enabled 的默认依原实现区分：整个 strategies 省略时四类开启；显式空节/未列策略时仅 panic_rebound 默认开启。策略 order_type 默认继承 execution.entry_order_type，已启用策略显式不同会冲突。旧 drop_pct/btc_max_drop_pct 是正数跌幅幅度，转换为负百分数后必须与新字段一致。

### main

| 字段 | 本次有效值 | 省略时默认 / 必填 | 单位 | 来源 / 默认是否采用 | 约束 |
| --- | --- | --- | --- | --- | --- |
| main.alerts.enabled | false | false | boolean | alerts.enabled / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.dashboard.host | "127.0.0.1" | "127.0.0.1" | host | dashboard.host / E | text; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.dashboard.port | 8765 | 8765 | count | dashboard.port / E | integer; lower=1024; lower_inclusive=True; upper=65535; choices=() |
| main.dashboard.require_auth | true | true | boolean | dashboard.require_auth / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.dashboard.root_path | "" | "" | path | dashboard.root_path / E | text; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.dashboard.title | "SOL AI \u72ec\u7acb\u4ea4\u6613\u53f0" | "SOL AI \u72ec\u7acb\u4ea4\u6613\u53f0" | text | dashboard.title / E | text; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.data.rest_base_url | "https://fapi.binance.com" | "https://fapi.binance.com" | url | data.rest_base_url / E | text; lower=None; lower_inclusive=True; upper=None; choices=('https://fapi.binance.com',) |
| main.data.stale_after_seconds | "15" | "15" | seconds | data.stale_after_seconds / E | decimal; lower=2; lower_inclusive=True; upper=120; choices=() |
| main.data.warmup_candles | 120 | 120 | count | data.warmup_candles / E | integer; lower=20; lower_inclusive=True; upper=240; choices=() |
| main.data.ws_base_url | "wss://fstream.binance.com" | "wss://fstream.binance.com" | url | data.ws_base_url / E | text; lower=None; lower_inclusive=True; upper=None; choices=('wss://fstream.binance.com',) |
| main.dry_run | true | true；必填不可省略 | boolean | dry_run / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.execution.entry_order_type | "MARKET" | "MARKET" | order_type | execution.entry_order_type / E | text; lower=None; lower_inclusive=True; upper=None; choices=('MARKET', 'LIMIT') |
| main.execution.leverage | 5 | 5 | leverage_multiple | execution.leverage / E | integer; lower=1; lower_inclusive=True; upper=5; choices=() |
| main.execution.limit_expiry_seconds | "30" | "30" | seconds | execution.limit_expiry_seconds / E | decimal; lower=1; lower_inclusive=True; upper=300; choices=() |
| main.instance_id | "sol-ai-local" | "sol-ai-local"；必填不可省略 | instance | instance_id / E | text; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.live.bridge_url | "http://127.0.0.1:8766" | "http://127.0.0.1:8766" | url | live.bridge_url / E | text; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.live.confirmation | "" | "" | text | live.confirmation / E | text; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.live.dedicated_account_confirmed | false | false | boolean | live.dedicated_account_confirmed / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.live.enabled | false | false；必填不可省略 | boolean | live.enabled / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.live.max_account_equity_usdt | "500" | "500" | USDT | live.max_account_equity_usdt / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.live.order_timeout_seconds | "8" | "8" | seconds | live.order_timeout_seconds / E | decimal; lower=0; lower_inclusive=False; upper=30; choices=() |
| main.live.reconcile_interval_seconds | "10" | "10" | seconds | live.reconcile_interval_seconds / E | decimal; lower=5; lower_inclusive=True; upper=60; choices=() |
| main.paper.initial_balance | "500" | "500" | USDT | paper.initial_balance / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.review.daily_hour | 0 | 0 | count | review.daily_hour / E | integer; lower=0; lower_inclusive=True; upper=23; choices=() |
| main.review.daily_minute | 5 | 5 | count | review.daily_minute / E | integer; lower=0; lower_inclusive=True; upper=59; choices=() |
| main.risk.btc_crash_pct | "-0.8" | "-.8" | percent_points | risk.btc_crash_pct / E | decimal; lower=-100; lower_inclusive=False; upper=0; choices=() |
| main.risk.daily_loss_limit | "20" | "20"；必填不可省略 | USDT_net_cash_day_loss | risk.daily_loss_limit / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.risk.max_consecutive_losses | 2 | 2；必填不可省略 | count | risk.max_consecutive_losses / E | integer; lower=1; lower_inclusive=True; upper=None; choices=() |
| main.risk.max_leverage | 5 | 5；必填不可省略 | leverage_multiple | risk.max_leverage / E | integer; lower=1; lower_inclusive=True; upper=5; choices=() |
| main.risk.max_loss_per_trade | "5" | "5"；必填不可省略 | USDT_loss_budget | risk.max_loss_per_trade / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.risk.max_margin_ratio | "0.2" | ".2"；必填不可省略 | equity_fraction | risk.max_margin_ratio / E | decimal; lower=0; lower_inclusive=False; upper=.2; choices=() |
| main.risk.max_positions | 1 | 1；必填不可省略 | count | risk.max_positions / E | integer; lower=1; lower_inclusive=True; upper=1; choices=() |
| main.risk.max_trades_per_day | 3 | 3；必填不可省略 | count | risk.max_trades_per_day / E | integer; lower=1; lower_inclusive=True; upper=None; choices=() |
| main.risk.slippage_bps | "10" | "10" | bps | risk.slippage_bps / E | decimal; lower=0; lower_inclusive=True; upper=100; choices=() |
| main.risk.taker_fee_rate | "0.0005" | ".0005" | fee_rate | risk.taker_fee_rate / E | decimal; lower=0; lower_inclusive=True; upper=.01; choices=() |
| main.runtime.heartbeat_seconds | "1" | "1" | seconds | runtime.heartbeat_seconds / E | decimal; lower=.2; lower_inclusive=True; upper=5; choices=() |
| main.runtime.max_event_loop_lag_seconds | "5" | "5" | seconds | runtime.max_event_loop_lag_seconds / E | decimal; lower=1; lower_inclusive=True; upper=15; choices=() |
| main.runtime.scoring_interval_seconds | "0.5" | ".5" | seconds | runtime.scoring_interval_seconds / E | decimal; lower=.1; lower_inclusive=True; upper=5; choices=() |
| main.strategies.fake_breakout_reverse.breakout_buffer_pct | "0.05" | ".05" | percent_points | strategies.fake_breakout_reverse.breakout_buffer_pct / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.fake_breakout_reverse.btc_crash_pct | "-0.8" | "-.8" | percent_points | strategies.fake_breakout_reverse.btc_crash_pct / D | decimal; lower=-100; lower_inclusive=False; upper=0; choices=() |
| main.strategies.fake_breakout_reverse.cooldown_seconds | "300" | "300" | seconds | strategies.fake_breakout_reverse.cooldown_seconds / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.fake_breakout_reverse.enabled | true | 依 strategies 节是否省略；见上文 | boolean | strategies.fake_breakout_reverse.enabled / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.strategies.fake_breakout_reverse.max_stop_distance_pct | "5" | "5" | percent_points | strategies.fake_breakout_reverse.max_stop_distance_pct / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.fake_breakout_reverse.min_score | "60" | "60" | score_points | strategies.fake_breakout_reverse.min_score / D | decimal; lower=0; lower_inclusive=False; upper=100; choices=() |
| main.strategies.fake_breakout_reverse.observation_seconds | "300" | "300" | seconds | strategies.fake_breakout_reverse.observation_seconds / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.fake_breakout_reverse.order_type | "MARKET" | 继承 execution.entry_order_type | order_type | execution.entry_order_type / D | text; lower=None; lower_inclusive=True; upper=None; choices=('MARKET', 'LIMIT') |
| main.strategies.panic_rebound.btc_crash_pct | "-0.8" | "-.8" | percent_points | strategies.panic_rebound.btc_crash_pct / E | decimal; lower=-100; lower_inclusive=False; upper=0; choices=() |
| main.strategies.panic_rebound.cooldown_seconds | "180" | "300" | seconds | strategies.panic_rebound.cooldown_seconds / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.panic_rebound.drop_threshold_pct | "-2" | "-2" | percent_points | strategies.panic_rebound.drop_threshold_pct / E | decimal; lower=-100; lower_inclusive=False; upper=0; choices=() |
| main.strategies.panic_rebound.enabled | true | 依 strategies 节是否省略；见上文 | boolean | strategies.panic_rebound.enabled / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.strategies.panic_rebound.max_stop_distance_pct | "5" | "5" | percent_points | strategies.panic_rebound.max_stop_distance_pct / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.panic_rebound.min_score | "60" | "60" | score_points | strategies.panic_rebound.min_score / D | decimal; lower=0; lower_inclusive=False; upper=100; choices=() |
| main.strategies.panic_rebound.observation_seconds | "300" | "300" | seconds | strategies.panic_rebound.observation_seconds / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.panic_rebound.order_type | "MARKET" | 继承 execution.entry_order_type | order_type | execution.entry_order_type / D | text; lower=None; lower_inclusive=True; upper=None; choices=('MARKET', 'LIMIT') |
| main.strategies.panic_rebound.rebound_fraction | "0.25" | ".25" | fraction | strategies.panic_rebound.rebound_fraction / E | decimal; lower=0; lower_inclusive=False; upper=1; choices=() |
| main.strategies.panic_rebound.stop_buffer_pct | "0.15" | ".15" | percent_points | strategies.panic_rebound.stop_buffer_pct / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.pullback_entry.bounce_pct | "0.2" | ".2" | percent_points | strategies.pullback_entry.bounce_pct / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.pullback_entry.btc_crash_pct | "-0.8" | "-.8" | percent_points | strategies.pullback_entry.btc_crash_pct / D | decimal; lower=-100; lower_inclusive=False; upper=0; choices=() |
| main.strategies.pullback_entry.cooldown_seconds | "300" | "300" | seconds | strategies.pullback_entry.cooldown_seconds / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.pullback_entry.enabled | true | 依 strategies 节是否省略；见上文 | boolean | strategies.pullback_entry.enabled / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.strategies.pullback_entry.max_stop_distance_pct | "5" | "5" | percent_points | strategies.pullback_entry.max_stop_distance_pct / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.pullback_entry.min_score | "60" | "60" | score_points | strategies.pullback_entry.min_score / D | decimal; lower=0; lower_inclusive=False; upper=100; choices=() |
| main.strategies.pullback_entry.min_trend_pct | "0.8" | ".8" | percent_points | strategies.pullback_entry.min_trend_pct / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.pullback_entry.observation_seconds | "300" | "300" | seconds | strategies.pullback_entry.observation_seconds / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.pullback_entry.order_type | "MARKET" | 继承 execution.entry_order_type | order_type | execution.entry_order_type / D | text; lower=None; lower_inclusive=True; upper=None; choices=('MARKET', 'LIMIT') |
| main.strategies.pullback_entry.pullback_pct | "0.3" | ".3" | percent_points | strategies.pullback_entry.pullback_pct / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.trend_breakout.breakout_buffer_pct | "0.05" | ".05" | percent_points | strategies.trend_breakout.breakout_buffer_pct / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.trend_breakout.btc_crash_pct | "-0.8" | "-.8" | percent_points | strategies.trend_breakout.btc_crash_pct / D | decimal; lower=-100; lower_inclusive=False; upper=0; choices=() |
| main.strategies.trend_breakout.cooldown_seconds | "300" | "300" | seconds | strategies.trend_breakout.cooldown_seconds / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.trend_breakout.enabled | true | 依 strategies 节是否省略；见上文 | boolean | strategies.trend_breakout.enabled / E | boolean; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.strategies.trend_breakout.max_stop_distance_pct | "5" | "5" | percent_points | strategies.trend_breakout.max_stop_distance_pct / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.trend_breakout.min_buy_pressure | "0.55" | ".55" | fraction | strategies.trend_breakout.min_buy_pressure / E | decimal; lower=0; lower_inclusive=False; upper=1; choices=() |
| main.strategies.trend_breakout.min_score | "60" | "60" | score_points | strategies.trend_breakout.min_score / D | decimal; lower=0; lower_inclusive=False; upper=100; choices=() |
| main.strategies.trend_breakout.min_trend_pct | "0.5" | ".5" | percent_points | strategies.trend_breakout.min_trend_pct / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.trend_breakout.min_volume_ratio | "1.2" | "1.2" | volume_multiple | strategies.trend_breakout.min_volume_ratio / E | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.trend_breakout.observation_seconds | "300" | "300" | seconds | strategies.trend_breakout.observation_seconds / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.strategies.trend_breakout.order_type | "MARKET" | 继承 execution.entry_order_type | order_type | execution.entry_order_type / D | text; lower=None; lower_inclusive=True; upper=None; choices=('MARKET', 'LIMIT') |
| main.strategies.trend_breakout.stop_distance_pct | "0.8" | ".8" | percent_points | strategies.trend_breakout.stop_distance_pct / D | decimal; lower=0; lower_inclusive=False; upper=None; choices=() |
| main.symbols | ["SOLUSDT","BTCUSDT","ETHUSDT"] | ["SOLUSDT","BTCUSDT","ETHUSDT"] | symbol_set | symbols / E | symbols; lower=None; lower_inclusive=True; upper=None; choices=() |
| main.timezone | "Asia/Kuala_Lumpur" | "Asia/Kuala_Lumpur" | timezone | timezone / E | text; lower=None; lower_inclusive=True; upper=None; choices=('Asia/Kuala_Lumpur', 'UTC') |
| main.trade_symbol | "SOLUSDT" | "SOLUSDT" | symbol | trade_symbol / E | text; lower=None; lower_inclusive=True; upper=None; choices=('SOLUSDT',) |
### admission

| 字段 | 本次有效值 | 省略时默认 / 必填 | 单位 | 来源 / 默认是否采用 | 约束 |
| --- | --- | --- | --- | --- | --- |
| admission.allowed_evidence_verifiers.0 | "paper-structure-review/v1" | "paper-structure-review/v1" | text_or_enum | allowed_evidence_verifiers.0 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.allowed_exchanges.0 | "BINANCE_USDT_M" | "BINANCE_USDT_M" | text_or_enum | allowed_exchanges.0 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.allowed_risk_sources.0 | "paper-ledger-snapshot/v1" | "paper-ledger-snapshot/v1" | text_or_enum | allowed_risk_sources.0 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.allowed_symbols.0 | "SOLUSDT" | "SOLUSDT" | text_or_enum | allowed_symbols.0 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.btc_crash_3m_pct | -0.8 | -0.8 | percent_points | btc_crash_3m_pct / E | {"maximum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.daily_loss_limit_usdt | "20" | "20" | USDT_cumulative_losing_outcomes | daily_loss_limit_usdt / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.decision_ttl_seconds | 5.0 | 5.0 | seconds | decision_ttl_seconds / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.dimension_floors.0.dimension | "direction_confidence" | "direction_confidence" | text_or_enum | dimension_floors.0.dimension / E | {"enum":["direction_confidence","entry_quality","stop_loss_quality","take_profit_quality","rr_quality","position_quality","execution_clarity"],"type":"string"}; plus accepted model cross-field validators |
| admission.dimension_floors.0.minimum | 55.0 | 55.0 | score_points | dimension_floors.0.minimum / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.dimension_floors.1.dimension | "entry_quality" | "entry_quality" | text_or_enum | dimension_floors.1.dimension / E | {"enum":["direction_confidence","entry_quality","stop_loss_quality","take_profit_quality","rr_quality","position_quality","execution_clarity"],"type":"string"}; plus accepted model cross-field validators |
| admission.dimension_floors.1.minimum | 65.0 | 65.0 | score_points | dimension_floors.1.minimum / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.dimension_floors.2.dimension | "stop_loss_quality" | "stop_loss_quality" | text_or_enum | dimension_floors.2.dimension / E | {"enum":["direction_confidence","entry_quality","stop_loss_quality","take_profit_quality","rr_quality","position_quality","execution_clarity"],"type":"string"}; plus accepted model cross-field validators |
| admission.dimension_floors.2.minimum | 80.0 | 80.0 | score_points | dimension_floors.2.minimum / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.dimension_floors.3.dimension | "take_profit_quality" | "take_profit_quality" | text_or_enum | dimension_floors.3.dimension / E | {"enum":["direction_confidence","entry_quality","stop_loss_quality","take_profit_quality","rr_quality","position_quality","execution_clarity"],"type":"string"}; plus accepted model cross-field validators |
| admission.dimension_floors.3.minimum | 75.0 | 75.0 | score_points | dimension_floors.3.minimum / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.dimension_floors.4.dimension | "rr_quality" | "rr_quality" | text_or_enum | dimension_floors.4.dimension / E | {"enum":["direction_confidence","entry_quality","stop_loss_quality","take_profit_quality","rr_quality","position_quality","execution_clarity"],"type":"string"}; plus accepted model cross-field validators |
| admission.dimension_floors.4.minimum | 50.0 | 50.0 | score_points | dimension_floors.4.minimum / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.dimension_floors.5.dimension | "position_quality" | "position_quality" | text_or_enum | dimension_floors.5.dimension / E | {"enum":["direction_confidence","entry_quality","stop_loss_quality","take_profit_quality","rr_quality","position_quality","execution_clarity"],"type":"string"}; plus accepted model cross-field validators |
| admission.dimension_floors.5.minimum | 50.0 | 50.0 | score_points | dimension_floors.5.minimum / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.dimension_floors.6.dimension | "execution_clarity" | "execution_clarity" | text_or_enum | dimension_floors.6.dimension / E | {"enum":["direction_confidence","entry_quality","stop_loss_quality","take_profit_quality","rr_quality","position_quality","execution_clarity"],"type":"string"}; plus accepted model cross-field validators |
| admission.dimension_floors.6.minimum | 80.0 | 80.0 | score_points | dimension_floors.6.minimum / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.enabled | true | true | boolean | enabled / E | {"type":"boolean"}; plus accepted model cross-field validators |
| admission.markets.0.allowed | true | true | boolean | markets.0.allowed / E | {"type":"boolean"}; plus accepted model cross-field validators |
| admission.markets.0.minimum_net_rr | "2" | "2" | net_R_multiple | markets.0.minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.markets.0.regime | "trend" | "trend" | text_or_enum | markets.0.regime / E | {"enum":["trend","range","high_volatility","unknown"],"type":"string"}; plus accepted model cross-field validators |
| admission.markets.0.risk_fraction | "1" | "1" | fraction | markets.0.risk_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.markets.1.allowed | true | true | boolean | markets.1.allowed / E | {"type":"boolean"}; plus accepted model cross-field validators |
| admission.markets.1.minimum_net_rr | "1.5" | "1.5" | net_R_multiple | markets.1.minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.markets.1.regime | "range" | "range" | text_or_enum | markets.1.regime / E | {"enum":["trend","range","high_volatility","unknown"],"type":"string"}; plus accepted model cross-field validators |
| admission.markets.1.risk_fraction | "0.5" | "0.5" | fraction | markets.1.risk_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.markets.2.allowed | false | false | boolean | markets.2.allowed / E | {"type":"boolean"}; plus accepted model cross-field validators |
| admission.markets.2.minimum_net_rr | "2" | "2" | net_R_multiple | markets.2.minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.markets.2.regime | "high_volatility" | "high_volatility" | text_or_enum | markets.2.regime / E | {"enum":["trend","range","high_volatility","unknown"],"type":"string"}; plus accepted model cross-field validators |
| admission.markets.2.risk_fraction | "0.25" | "0.25" | fraction | markets.2.risk_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.markets.3.allowed | false | false | boolean | markets.3.allowed / E | {"type":"boolean"}; plus accepted model cross-field validators |
| admission.markets.3.minimum_net_rr | "2" | "2" | net_R_multiple | markets.3.minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.markets.3.regime | "unknown" | "unknown" | text_or_enum | markets.3.regime / E | {"enum":["trend","range","high_volatility","unknown"],"type":"string"}; plus accepted model cross-field validators |
| admission.markets.3.risk_fraction | "0" | "0" | fraction | markets.3.risk_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.max_account_age_seconds | 5.0 | 5.0 | seconds | max_account_age_seconds / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_consecutive_losses | 2 | 2 | count | max_consecutive_losses / E | {"minimum":1,"type":"integer"}; plus accepted model cross-field validators |
| admission.max_cost_age_seconds | 300.0 | 300.0 | seconds | max_cost_age_seconds / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_entry_deviation_bps | 50.0 | 50.0 | bps | max_entry_deviation_bps / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_exchange_age_seconds | 3600.0 | 3600.0 | seconds | max_exchange_age_seconds / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_holding_assumption_seconds | 86400.0 | 86400.0 | seconds | max_holding_assumption_seconds / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_leverage | 5 | 5 | leverage_multiple | max_leverage / E | {"minimum":1,"type":"integer"}; plus accepted model cross-field validators |
| admission.max_loss_per_trade_usdt | "5" | "5" | USDT_loss_budget | max_loss_per_trade_usdt / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.max_margin_ratio | "0.2" | "0.2" | equity_fraction | max_margin_ratio / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.max_margin_usdt | "100" | "100" | USDT_margin | max_margin_usdt / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.max_market_age_seconds | 15.0 | 15.0 | seconds | max_market_age_seconds / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_position_notional_usdt | "500" | "500" | USDT_notional | max_position_notional_usdt / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.max_position_quantity | "100" | "100" | base_quantity | max_position_quantity / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.max_positions | 1 | 1 | count | max_positions / E | {"minimum":1,"type":"integer"}; plus accepted model cross-field validators |
| admission.max_risk_fraction_of_equity | "0.01" | "0.01" | equity_fraction | max_risk_fraction_of_equity / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.max_scorecard_age_seconds | 30.0 | 30.0 | seconds | max_scorecard_age_seconds / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_spread_bps | 20.0 | 20.0 | bps | max_spread_bps / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_structure_age_seconds | 300.0 | 300.0 | seconds | max_structure_age_seconds / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.max_trades_per_day | 3 | 3 | count | max_trades_per_day / E | {"minimum":1,"type":"integer"}; plus accepted model cross-field validators |
| admission.max_volatility_pct | 3.0 | 3.0 | percent_points | max_volatility_pct / E | {"exclusiveMinimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.maximum_entry_slippage_bps | "30" | "30" | bps | maximum_entry_slippage_bps / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.maximum_exit_slippage_bps | "30" | "30" | bps | maximum_exit_slippage_bps / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_data_coverage | "0.8" | "0.8" | fraction | minimum_data_coverage / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_entry_fee_rate | "0.0005" | "0.0005" | fee_rate | minimum_entry_fee_rate / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_entry_slippage_bps | "10" | "10" | bps | minimum_entry_slippage_bps / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_exit_fee_rate | "0.0005" | "0.0005" | fee_rate | minimum_exit_fee_rate / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_exit_slippage_bps | "10" | "10" | bps | minimum_exit_slippage_bps / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_net_rr | "1.5" | "1.5" | net_R_multiple | minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_risk_budget_usdt | "0.5" | "0.5" | USDT_loss_budget | minimum_risk_budget_usdt / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_score_coverage | "1" | "1" | fraction | minimum_score_coverage / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.minimum_total_score | 60.0 | 60.0 | score_points | minimum_total_score / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.mode | "paper_only" | "paper_only" | text_or_enum | mode / E | {"const":"paper_only","type":"string"}; plus accepted model cross-field validators |
| admission.policy_version | "paper-risk-admission/v1" | "paper-risk-admission/v1" | text_or_enum | policy_version / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.required_data.0 | "market_price" | "market_price" | text_or_enum | required_data.0 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.required_data.1 | "structure" | "structure" | text_or_enum | required_data.1 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.required_data.2 | "volatility" | "volatility" | text_or_enum | required_data.2 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.required_data.3 | "btc_reference" | "btc_reference" | text_or_enum | required_data.3 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.required_data.4 | "eth_reference" | "eth_reference" | text_or_enum | required_data.4 / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| admission.tiers.0.max_notional_equity_ratio | "1" | "1" | equity_multiple | tiers.0.max_notional_equity_ratio / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.0.minimum_net_rr | "2" | "2" | net_R_multiple | tiers.0.minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.0.minimum_total | 85.0 | 85.0 | score_points | tiers.0.minimum_total / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.tiers.0.name | "S" | "S" | text_or_enum | tiers.0.name / E | {"enum":["S","A","B","C"],"type":"string"}; plus accepted model cross-field validators |
| admission.tiers.0.risk_fraction | "1" | "1" | fraction | tiers.0.risk_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.1.max_notional_equity_ratio | "0.75" | "0.75" | equity_multiple | tiers.1.max_notional_equity_ratio / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.1.minimum_net_rr | "2" | "2" | net_R_multiple | tiers.1.minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.1.minimum_total | 75.0 | 75.0 | score_points | tiers.1.minimum_total / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.tiers.1.name | "A" | "A" | text_or_enum | tiers.1.name / E | {"enum":["S","A","B","C"],"type":"string"}; plus accepted model cross-field validators |
| admission.tiers.1.risk_fraction | "0.75" | "0.75" | fraction | tiers.1.risk_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.2.max_notional_equity_ratio | "0.5" | "0.5" | equity_multiple | tiers.2.max_notional_equity_ratio / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.2.minimum_net_rr | "1.5" | "1.5" | net_R_multiple | tiers.2.minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.2.minimum_total | 65.0 | 65.0 | score_points | tiers.2.minimum_total / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.tiers.2.name | "B" | "B" | text_or_enum | tiers.2.name / E | {"enum":["S","A","B","C"],"type":"string"}; plus accepted model cross-field validators |
| admission.tiers.2.risk_fraction | "0.5" | "0.5" | fraction | tiers.2.risk_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.3.max_notional_equity_ratio | "0.25" | "0.25" | equity_multiple | tiers.3.max_notional_equity_ratio / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.3.minimum_net_rr | "1.5" | "1.5" | net_R_multiple | tiers.3.minimum_net_rr / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| admission.tiers.3.minimum_total | 55.0 | 55.0 | score_points | tiers.3.minimum_total / E | {"maximum":100,"minimum":0,"type":"number"}; plus accepted model cross-field validators |
| admission.tiers.3.name | "C" | "C" | text_or_enum | tiers.3.name / E | {"enum":["S","A","B","C"],"type":"string"}; plus accepted model cross-field validators |
| admission.tiers.3.risk_fraction | "0.25" | "0.25" | fraction | tiers.3.risk_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
### exit

| 字段 | 本次有效值 | 省略时默认 / 必填 | 单位 | 来源 / 默认是否采用 | 约束 |
| --- | --- | --- | --- | --- | --- |
| exit.atr_multiple | "2" | "2" | ATR_multiple | atr_multiple / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.break_even_mode | "cost_covered" | "cost_covered" | text_or_enum | break_even_mode / E | {"enum":["entry_price","cost_covered"],"type":"string"}; plus accepted model cross-field validators |
| exit.evidence_max_age_seconds | "30" | "30" | seconds | evidence_max_age_seconds / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.expected_exit_fee_rate | "0.0005" | "0.0005" | fee_rate | expected_exit_fee_rate / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.expected_exit_slippage_bps | "10" | "10" | bps | expected_exit_slippage_bps / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.market_max_age_seconds | "5" | "5" | seconds | market_max_age_seconds / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.max_control_attempts | 3 | 3 | count | max_control_attempts / E | {"exclusiveMinimum":0,"type":"integer"}; plus accepted model cross-field validators |
| exit.max_holding_seconds | "3600" | "3600" | seconds | max_holding_seconds / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.max_known_zero_fill_failures | 3 | 3 | count | max_known_zero_fill_failures / E | {"exclusiveMinimum":0,"type":"integer"}; plus accepted model cross-field validators |
| exit.mode | "paper_only" | "paper_only" | text_or_enum | mode / E | {"const":"paper_only","type":"string"}; plus accepted model cross-field validators |
| exit.runner_activation_r | "3" | "3" | frozen_R_multiple | runner_activation_r / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.runner_fraction | "0.3" | "0.3" | original_quantity_fraction | runner_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.runner_strategy | "fixed_r" | "fixed_r" | text_or_enum | runner_strategy / E | {"enum":["fixed_r","swing","atr"],"type":"string"}; plus accepted model cross-field validators |
| exit.runner_trail_r | "1" | "1" | frozen_R_multiple | runner_trail_r / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.swing_buffer_r | "0" | "0" | frozen_R_multiple | swing_buffer_r / E | {"anyOf":[{"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.tp1_fraction | "0.3" | "0.3" | original_quantity_fraction | tp1_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.tp1_r | "1" | "1" | frozen_R_multiple | tp1_r / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.tp2_fraction | "0.4" | "0.4" | original_quantity_fraction | tp2_fraction / E | {"anyOf":[{"maximum":1.0,"minimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.tp2_r | "2" | "2" | frozen_R_multiple | tp2_r / E | {"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}]}; plus accepted model cross-field validators |
| exit.trend_exit_enabled | true | true | boolean | trend_exit_enabled / E | {"type":"boolean"}; plus accepted model cross-field validators |
| exit.version | "paper-exit-policy/v2" | "paper-exit-policy/v2" | text_or_enum | version / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
### manifest

| 字段 | 本次有效值 | 省略时默认 / 必填 | 单位 | 来源 / 默认是否采用 | 约束 |
| --- | --- | --- | --- | --- | --- |
| manifest.admission_policy_version | "paper-risk-admission/v1" | 无默认；显式必填 | text_or_enum | admission_policy_version / E | {"const":"paper-risk-admission/v1","type":"string"}; plus accepted model cross-field validators |
| manifest.bundle_revision | "sol-paper-config/v1" | 无默认；显式必填 | text_or_enum | bundle_revision / E | {"maxLength":4096,"minLength":1,"pattern":"\\S","type":"string"}; plus accepted model cross-field validators |
| manifest.catalog_version | "config-catalog/v1" | 无默认；显式必填 | text_or_enum | catalog_version / E | {"const":"config-catalog/v1","type":"string"}; plus accepted model cross-field validators |
| manifest.contract_type | "linear_usdt" | 无默认；显式必填 | text_or_enum | contract_type / E | {"const":"linear_usdt","type":"string"}; plus accepted model cross-field validators |
| manifest.exchange | "BINANCE_USDT_M" | 无默认；显式必填 | text_or_enum | exchange / E | {"const":"BINANCE_USDT_M","type":"string"}; plus accepted model cross-field validators |
| manifest.exit_checkpoint_version | "exit-checkpoint/v2" | 无默认；显式必填 | text_or_enum | exit_checkpoint_version / E | {"const":"exit-checkpoint/v2","type":"string"}; plus accepted model cross-field validators |
| manifest.exit_policy_version | "paper-exit-policy/v2" | 无默认；显式必填 | text_or_enum | exit_policy_version / E | {"const":"paper-exit-policy/v2","type":"string"}; plus accepted model cross-field validators |
| manifest.exit_state_version | "position-exit/v2" | 无默认；显式必填 | text_or_enum | exit_state_version / E | {"const":"position-exit/v2","type":"string"}; plus accepted model cross-field validators |
| manifest.instance_id | "sol-ai-local" | 无默认；显式必填 | text_or_enum | instance_id / E | {"pattern":"^[a-z][a-z0-9_-]{2,47}$","type":"string"}; plus accepted model cross-field validators |
| manifest.live_allowed | false | false；仍必填 | boolean | live_allowed / E | {"const":false,"type":"boolean"}; plus accepted model cross-field validators |
| manifest.main_contract_version | "main-config-contract/v1" | 无默认；显式必填 | text_or_enum | main_contract_version / E | {"const":"main-config-contract/v1","type":"string"}; plus accepted model cross-field validators |
| manifest.mode | "paper_only" | 无默认；显式必填 | text_or_enum | mode / E | {"const":"paper_only","type":"string"}; plus accepted model cross-field validators |
| manifest.quote_currency | "USDT" | 无默认；显式必填 | text_or_enum | quote_currency / E | {"const":"USDT","type":"string"}; plus accepted model cross-field validators |
| manifest.rr_calculation_version | "linear-usdt-rr/v1" | 无默认；显式必填 | text_or_enum | rr_calculation_version / E | {"const":"linear-usdt-rr/v1","type":"string"}; plus accepted model cross-field validators |
| manifest.schema_version | "config-bundle/v1" | 无默认；显式必填 | text_or_enum | schema_version / E | {"const":"config-bundle/v1","type":"string"}; plus accepted model cross-field validators |
| manifest.score_context_max_age_seconds | "300" | "300" | seconds | score_context_max_age_seconds / E | {"anyOf":[{"type":"number"},{"pattern":"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$","type":"string"}],"gt":0}; plus accepted model cross-field validators |
| manifest.scorecard_schema_version | "trade-scorecard/v1" | 无默认；显式必填 | text_or_enum | scorecard_schema_version / E | {"const":"trade-scorecard/v1","type":"string"}; plus accepted model cross-field validators |
| manifest.scoring_rule_version | "plan-quality/v1" | 无默认；显式必填 | text_or_enum | scoring_rule_version / E | {"const":"plan-quality/v1","type":"string"}; plus accepted model cross-field validators |
| manifest.setup_schema_version | "trade-setup/v1" | 无默认；显式必填 | text_or_enum | setup_schema_version / E | {"const":"trade-setup/v1","type":"string"}; plus accepted model cross-field validators |
| manifest.trade_symbol | "SOLUSDT" | 无默认；显式必填 | text_or_enum | trade_symbol / E | {"const":"SOLUSDT","type":"string"}; plus accepted model cross-field validators |
### derived

| 字段 | 本次有效值 | 省略时默认 / 必填 | 单位 | 来源 / 默认是否采用 | 约束 |
| --- | --- | --- | --- | --- | --- |
| derived.btc_long_block_at_or_below_pct | "-0.8" | 按来源计算，不覆盖原值 | percent_points | risk.btc_crash_pct + admission.btc_crash_3m_pct / E | LONG only; return_3m_pct <= max(two negative thresholds) |
| derived.minimum_net_rr.A.high_volatility | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.A + markets.high_volatility / E | maximum of three RR floors |
| derived.minimum_net_rr.A.range | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.A + markets.range / E | maximum of three RR floors |
| derived.minimum_net_rr.A.trend | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.A + markets.trend / E | maximum of three RR floors |
| derived.minimum_net_rr.A.unknown | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.A + markets.unknown / E | maximum of three RR floors |
| derived.minimum_net_rr.B.high_volatility | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.B + markets.high_volatility / E | maximum of three RR floors |
| derived.minimum_net_rr.B.range | "1.5" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.B + markets.range / E | maximum of three RR floors |
| derived.minimum_net_rr.B.trend | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.B + markets.trend / E | maximum of three RR floors |
| derived.minimum_net_rr.B.unknown | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.B + markets.unknown / E | maximum of three RR floors |
| derived.minimum_net_rr.C.high_volatility | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.C + markets.high_volatility / E | maximum of three RR floors |
| derived.minimum_net_rr.C.range | "1.5" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.C + markets.range / E | maximum of three RR floors |
| derived.minimum_net_rr.C.trend | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.C + markets.trend / E | maximum of three RR floors |
| derived.minimum_net_rr.C.unknown | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.C + markets.unknown / E | maximum of three RR floors |
| derived.minimum_net_rr.S.high_volatility | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.S + markets.high_volatility / E | maximum of three RR floors |
| derived.minimum_net_rr.S.range | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.S + markets.range / E | maximum of three RR floors |
| derived.minimum_net_rr.S.trend | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.S + markets.trend / E | maximum of three RR floors |
| derived.minimum_net_rr.S.unknown | "2" | 按来源计算，不覆盖原值 | net_R_multiple | minimum_net_rr + tiers.S + markets.unknown / E | maximum of three RR floors |

### 目录权威与覆盖说明

main 行来自 app/config.py 及 app/strategies/engine.py / app/runtime.py 的既有回退；admission 行来自 app/admission/policy.py；exit 行来自 app/exits/policy.py；manifest 来自本阶段显式版本目录。策略旧启发式 min_score 不是八维 Scorecard 门槛；两个 60 的数值不能当作同一语义覆盖。

所有行均保留原始来源摘要和有效值。四个 comparable_limits 另保留两个来源路径；derived.minimum_net_rr 是 global/tier/market 三个下限的最大值，未放宽 global。derived.btc_long_block_at_or_below_pct 是主风控与准入相同负百分数比较谓词的更严格组合；策略自己的过滤条件仍独立保留。

allowed_risk_sources / allowed_evidence_verifiers 是可接受来源标签的政策名单，不是签名或可信供应者实现。PlanInputs 中的账户快照、交易所规则、申请、结构确认、失效确认都须显式提供；None 与已确认 0、空持仓不是同一回事。声明完整不等于来源已认证。
