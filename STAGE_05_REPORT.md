# STAGE_05_REPORT — 交易准入与风险决策层

- 日期：2026-09-09（UTC+8）
- 仓库：https://github.com/RoeKai/SOL_TradingAI
- 独立分支：`phase-05-risk-admission`
- 已验收父阶段：`phase-04-scorecard`
- 唯一父提交：`0c0cdb7d6dc0d2ce5aa1827749cec7486b6b6f70`
- 本阶段提交：包含本报告的提交，交付消息提供完整 SHA；报告不嵌入自己的 SHA。
- 工作方式：从已验收提交建立独立 Git 副本和第五阶段分支；不修改或合并 main，不部署服务器。

## 1. 本阶段结论与边界

新增纯决策函数：

```python
admit_trade(setup, rr, scorecard, *, account, exchange, request, policy, evaluated_at)
# -> AdmissionDecision: APPROVE / REDUCE / REJECT
```

回答的是“这份计划在明确的 Paper 风险上下文中是否具备资格、最多允许多少风险和数量”，不是预测涨跌或实际下单。

```text
TradeSetup ── calculate_rr ── Scorecard
                    │              │
                    └── Admission / Risk Gate ← 显式 Paper 状态、约束、配置、时间
                                  │
                           AdmissionDecision
                                  │
                    require_paper_admission（未来消费契约）
                                  ┊ 未接线
                          后续 Paper Execution
```

本阶段没有账户、数据库、文件、网络、系统时钟读取，没有风险预留、日志写入、订单生成或资金操作。配置解析函数只接受调用方提供的 YAML 文本。测试使用合成数据；没有启动真实行情或交易进程。

**现有交易行为完全不变。** `main.py` 仍运行既有 Signal → 两次风控 → Paper 流程，不自动加载 Admission。新的契约不是已经覆盖旧 Paper 的全局拦截器，不能声称旧流程现在已受本阶段保护。既有风控、执行锁、Paper Broker、账本、Bridge 私有接口封禁和实盘硬关闭全部保留。

本阶段不会修改、生成、拉远止盈止损，不实现第六阶段分批退出/移动止损。测试中原 Bridge mock 的 `FILLED` 仅是假返回，不是交易所成交。

## 2. 文件清单

| 文件 | 变更 | 职责 |
| --- | --- | --- |
| app/admission/__init__.py | 新增 | 旁路包声明，不自动接线 |
| app/admission/models.py | 新增 | 不可变输入上下文、确认记录、决策、内容绑定与约束 |
| app/admission/policy.py | 新增 | 配置模型、层级一致性验证、严格 YAML 文本解析 |
| app/admission/engine.py | 新增 | 硬门槛、软评分、风险反推数量、最终 RR 复验 |
| app/admission/contract.py | 新增 | 后续 Paper 消费契约，拒绝裸信号、篡改或过期决策 |
| admission.yaml | 新增 | 独立第五阶段策略阈值模板，主程序不加载 |
| tests/test_admission.py | 新增 | 216 项准入、风险、篡改、边界、纯函数与隔离测试 |
| STAGE_05_REPORT.md | 新增 | 本报告 |
| README.md | 修改 | 第五阶段说明、边界及独立分支命令 |
| tests/test_dynamic_rr.py | 修改 | 静态旁路检查精确允许四个新增纯准入文件引用 setups；未放行运行模块 |
| tests/test_scorecard.py | 修改 | 同上，精确扩展四个纯准入文件白名单，不允许主流程引用 |

相对父提交：**8 个新增、3 个修改；完整发布树 112 个源码/文档/模板文件，101 个既有文件内容哈希不变。** 两项既有测试各增加四行精确路径白名单，不移除断言；第五阶段另有主流程禁止接线检查。

未修改：`app/models.py`、全部 `app/setups/` 实现、旧 Signal 适配、RR/评分规则、`config.yaml`、`main.py`、行情连接、四策略、原风控、执行锁、Paper、账本、Dashboard、alerts、隔离策略及全部 Bridge/vendor 文件。没有复制原跟单线上状态。

## 3. 输入与数据权威

所有新模型沿用不可变 `Record`：拒绝额外字段，严格布尔/计数类型，有限数值校验；入口重新验证副本，不能以 `model_copy` 绕过约束。金额/数量使用 Decimal，JSON 输出字符串。时间为明确的 UTC Unix 秒，调用方必须传入，函数不读取当前时钟。

| 输入 | 关键字段和语义 |
| --- | --- |
| TradeSetup | 原计划、结构依据、价格/比例、失效条件、预算建议、成本、市场快照、覆盖项及版本；只读 |
| RRCalculation | 已验收第三阶段实际输出，含显式计算用数量；不以建议上限猜数量 |
| Scorecard | 已验收第四阶段八维输出；保留其原评分上下文，不改分数算法 |
| PaperRiskSnapshot | `instance_id / snapshot_revision / mode / status / source / observed_at`，当前风险日、权益、可用/已用保证金、已实现亏损、未实现亏损、已预留风险、次数、连续亏损、持仓/待入场清单、暂停/对账状态、逐仓/杠杆/自动追加/马丁状态和账户硬上限 |
| AccountHardLimits | 单笔风险、日亏损、次数、连亏、同时持仓、杠杆、保证金比例/金额、单仓名义额/数量等 10 个硬上限；必须逐项明确提供 |
| ExchangeConstraints | 交易所、标的、MARKET/LIMIT、线性 USDT 合约类型、确认状态/来源/时间、数量步长、最小/最大数量、最小名义额、价格精度、最大杠杆；本阶段不获取交易所规则 |
| AdmissionRequest | `request_id / risk_budget_usdt / action / leverage / margin_mode / auto_add_margin / loss_recovery_sizing / sizing_basis / confirmations / invalidation_review`；申请的是可亏损预算，不是想下多少本金 |
| AdmissionPolicy | 独立版本化配置，见第 7 节 |

关键未知一律保留 `None`，不能默认平仓、未暂停、无亏损、已逐仓。`positions=None` 是未知，`positions=()` 才表示已确认空仓；待入场同理。

`available_margin_usdt` 定义为可用于新增初始保证金和入场费的自由资金；已用保证金加可用金额不能超过权益。`day_realized_loss_usdt` 是风险日内各净亏损结果的损失总额，不用盈利回补风险额度。日剩余预算还扣除浮亏和现有/待成交风险预留，偏保守，可能重叠扣减但不会放大额度。后续账本适配必须严格实现这个口径，不能将净盈利或带符号盈亏直接传入。

每个被引用的结构依据必须有 `EvidenceConfirmation(evidence_id, evidence_digest, verified, verifier, checked_at)`，内容摘要匹配、确认方在允许列表、时间新鲜且不早于依据。全部失效条件另需 `InvalidationReview(setup_digest, all_conditions_clear, verifier, checked_at)`，绑定整份计划，且不早于计划创建。已触发的明确价格/时间条件不会被“全部清楚”的声明覆盖。

**来源名、确认记录和 SHA256 是内容一致性检查，不是签名、真实性认证或对市场结构的独立证明。** 本阶段不会把一段“目标真实”的文字认证成事实。后续必须由可信 Paper 状态和结构验证适配器提供这些记录，不能让不可信外部请求自称 confirmed。没有这一可信供应链，不得接执行。

## 4. AdmissionDecision 最终结构

| 字段 | 类型 / 说明 |
| --- | --- |
| schema_version | 固定 `paper-admission/v1` |
| decision_id | 64 位十六进制内容摘要；生成时将自身置零后计算，不是签名 |
| policy_version | 本次策略配置版本 |
| result | `APPROVE / REDUCE / REJECT` |
| evaluated_at / valid_until | 明确评估时间、最早失效时刻；拒绝通常没有有效期 |
| binding | setup、rr、scorecard、account、exchange、request、policy 七份完整输入的 SHA256，以及 instance_id / snapshot_revision；契约畸形拒绝时可为空 |
| plan_terms | setup_id、plan_version、symbol、side、order_type、entry_reference/lower/upper、initial_stop、全部 `(target_id, price, fraction)`；从输入原样复制 |
| hard_gates_passed | 硬门槛是否完整通过；仅因软评分拒绝可以为 true，不能单独作为允许执行标记 |
| opportunity_tier | S/A/B/C 或 null，仅是配置定义的风险档位 |
| tier_interpretation | 固定 `risk_budget_band_not_win_probability` |
| reason_codes | 去重且有序的机器原因代码，每种决策均非空 |
| reasons | `code / layer / explanation / fields`，layer 为 contract/hard/soft/sizing/information |
| policy_risk_ceiling_usdt | 应用各风险预算上限后的额度，尚未按数量精度/保证金等缩小 |
| allowed_risk_budget_usdt | 最终数量对应的保守风险额度，不大于前项 |
| max_quantity | 精确 RR 复算通过的最终数量上限，非执行订单 |
| max_notional_usdt | 三个入场场景中的最大含入场滑点名义额 |
| max_initial_margin_usdt | 前项除以显式、已确认且不变的杠杆 |
| entry_fee_reserve_usdt | 单独保留的入场手续费资金，不能把全部可用金额只分配给保证金 |
| modeled_stop_loss_usdt | 第三阶段输出的最坏场景净止损金额，保留真实资金费正负口径 |
| leverage | 原申请并已确认的杠杆，不根据评分生成；拒绝时 null |
| required_net_rr | 全局/市场/等级净 RR 底线的最大值 |
| final_min_net_rr | 最终数量下参考、入场区间两端整单净 RR 的最小值 |
| final_rr | 原第三阶段函数按最终数量返回的完整 RRCalculation，含每档和整单结果 |
| constraints | 每项 `name / quantity_cap / explanation`，追溯数量的各独立约束 |
| eligibility_scope | 固定 `paper_only` |
| execution_authority | 固定 `none_until_paper_integration` |
| live_allowed | 固定 false，配置不能打开 |
| limitations | 数据可信性、未预留/消费、需要锁内复核、缩仓需重算、跳空等限制 |

REJECT 的风险、数量、名义额、保证金和手续费额度全为 0，`leverage / final_rr` 均为空。不能把 rejected result 搭配正数 cap 伪装成允许交易。所有结果都有明确解释。

## 5. Hard Gate：不可由高分覆盖

以下规则先于软评分；任意失败直接 REJECT。边界类型不符（例如拿旧 Signal 直接调用）抛出 `AdmissionContractError`，不会返回可用额度。

| 硬规则 | 实施与主要 reason_codes |
| --- | --- |
| 同一计划/版本/原结果 | 重新验证模型，并调用原 `calculate_rr` 比对整份 RR，再按原上下文调用原评分器比对整份 Scorecard；不只比 setup_id。`INPUT_CONTRACT_INVALID / RR_PLAN_MISMATCH / SCORECARD_PLAN_MISMATCH / UPSTREAM_CALCULATION_INVALID` |
| 启用与标的范围 | policy.enabled 必须开启，标的在 allowlist。`ADMISSION_DISABLED / SYMBOL_NOT_ALLOWED` |
| 计划和数据时间 | 创建不能在未来，计划必须有有效期且未到期；data_as_of、市场、评分、账户、规则、成本、结构和确认均检查各自年龄。拒绝未来时间和无来源。`PLAN_EXPIRED_OR_UNTIMED / STALE_OR_FUTURE_DATA / UNCONFIRMED_DATA` |
| 必需数据/覆盖度 | 配置 required_data 与所有原计划 required=true 项取并集；必须 available，missing/stale/unverified/not_applicable 不能代替必需数据。整体覆盖率达标。`REQUIRED_DATA_MISSING / DATA_COVERAGE_LOW` |
| 必需市场状态 | reference_price、bid、ask、volatility_pct、BTC/ETH 3m 涨跌幅全部明确；超波动/价差、BTC 急跌做多禁止。`MARKET_STATE_UNKNOWN / ABNORMAL_VOLATILITY / ABNORMAL_SPREAD / BTC_CRASH_LONG_BLOCK` |
| 入场可执行性 | 参考价偏离受限；MARKET 的当前买入 ask/卖出 bid 必须在计划区间内，LIMIT 报价可在区间外但仍受最大偏离约束。不自动修改入场。`ENTRY_DEVIATION_LIMIT / EXECUTABLE_QUOTE_OUTSIDE_PLAN` |
| 结构入场 | 需要 native_plan + structure_zone 和价格在入场区间的依据；旧 Signal 的单向适配不构成认证。`ENTRY_STRUCTURE_UNCONFIRMED / ENTRY_STRUCTURE_INVALID` |
| 合法结构止损 | 原模型保证亏损侧几何位置；多仓引用 support/swing_low/range_boundary，空仓镜像；止损必须在对应结构外或相等。`STOP_STRUCTURE_MISSING / STOP_STRUCTURE_INVALID` |
| 真实结构目标 | 每个 TP 必须是 structure、有真实依据和价格，不越过盈利侧引用结构；原方向、排序、比例验证保留。`TARGETS_MISSING / TARGET_STRUCTURE_MISSING / TARGET_STRUCTURE_INVALID / STRUCTURE_PRICE_MISSING` |
| 确认证据 | 所有被引用依据有内容绑定、可信供应方声明和有效时间；确认不得早于数据。`EVIDENCE_NOT_CONFIRMED / EVIDENCE_CONFIRMATION_PREDATES_DATA` |
| 失效条件 | 有与方向/止损一致的价格失效条件，全部条件经当前计划绑定审查；价格和时间触发后直接拒绝。`STOP_INVALIDATION_INCONSISTENT / INVALIDATION_STATE_UNCONFIRMED / INVALIDATION_REVIEW_PREDATES_PLAN / PLAN_INVALIDATED` |
| 完整交易成本 | 双边费率、双边不利滑点已计入且不低于配置假设下界，滑点不能超过上界；资金费、时长、来源和时间必须明确，时长受限。`COSTS_UNKNOWN / COST_ASSUMPTION_OUT_OF_BOUNDS / FUNDING_HORIZON_UNKNOWN / COST_HORIZON_TOO_LONG` |
| 真实整单净 RR | 参考、区间下端、上端都必须 complete 且有整单净 RR；取最小值，先检查全局最低值，随后只可提高到市场/档位要求。无净 RR 不用毛值，不取最远 TP。`NET_RR_UNAVAILABLE / NET_RR_BELOW_HARD_FLOOR / NET_RR_BELOW_CONTEXT_FLOOR` |
| 预算/历史拒绝 | 计划有正数风险预算；未解除的原 rejection_reasons 不能被高分清空。`PLAN_RISK_BUDGET_UNKNOWN / PRIOR_REJECTION_UNRESOLVED` |
| 明确 Paper 状态 | mode=paper、确认状态/来源/时效满足，所有安全字段和 10 个账户上限明确；计数属于当前风险日。`PAPER_ONLY / ACCOUNT_STATE_UNKNOWN / ACCOUNT_HARD_LIMIT_UNKNOWN / REQUEST_STATE_UNKNOWN / RISK_DAY_MISMATCH` |
| 暂停/对账 | paused=false 且 reconciliation_clear=true。`ACCOUNT_HALTED_OR_UNRECONCILED` |
| 逐仓、禁止追加和追损 | 请求与账户均 ISOLATED、auto_add_margin=false、martingale=false，loss_recovery=false，依据 quality_risk_budget。仅 OPEN，任何 ADD 均拒绝。`ISOLATED_MARGIN_REQUIRED / AUTO_MARGIN_FORBIDDEN / MARTINGALE_OR_RECOVERY_FORBIDDEN / ADDING_FORBIDDEN` |
| 日内熔断 | 已实现亏损总额 + 浮亏达到日上限、连亏达到上限、已用次数 + 待入场达到上限均拒绝；剩余预算另扣所有预留风险。`DAILY_LOSS_LIMIT / CONSECUTIVE_LOSS_HALT / DAILY_TRADE_LIMIT` |
| 仓位/挂单冲突 | 持仓 + 待入场数量达到上限；同币种任何方向既有仓位/待入场均冲突，不允许借反向信号绕过。`MAX_POSITIONS_LIMIT / CONFLICTING_POSITION_OR_ORDER` |
| 单笔风险、杠杆 | 申请风险超过账户/配置最严单笔上限直接拒绝；杠杆取账户、配置、交易所、计划上限最严值，并与账户确认值一致。绝不按评分提杠杆。`SINGLE_TRADE_RISK_LIMIT / LEVERAGE_LIMIT_OR_MISMATCH` |
| 余额与既有保证金 | 已用 + 可用不得超过权益；既有保证金已越硬上限则拒绝，新保证金只能用剩余额度。`ACCOUNT_BALANCE_INCONSISTENT / EXISTING_MARGIN_LIMIT` |
| 交易所约束 | 必须为允许的交易所、相同标的/订单类型的线性 USDT；数量/价格规则完整、确认和新鲜。止损/所有目标及 LIMIT 入场符合价格 tick，不自动改价。`EXCHANGE_RULES_MISMATCH / EXCHANGE_RULES_UNKNOWN / EXCHANGE_RULES_INVALID / PRICE_PRECISION_INVALID` |
| 最终量复验 | 向下舍入后低于最小数量/名义额就拒绝；最终数值须无损传入原 RR 接口，重新算净 RR；风险不得越界。`EXCHANGE_MINIMUM_EXCEEDS_BUDGET / QUANTITY_NOT_REPRESENTABLE / RESIZED_NET_RR_BELOW_FLOOR / FINAL_RISK_OUT_OF_BOUNDS` |

预算为零、低于最小可用预算，或固定资金费吃完预算，分别输出 `RISK_BUDGET_EXHAUSTED / FIXED_COST_EXCEEDS_BUDGET`。本轮只做拒绝/缩量，不增加本金凑交易所最小单。

决策有效期取全部使用中数据窗口、计划到期、账户风险日结束和配置 TTL 的最早时刻；无剩余时长输出 `DECISION_LIFETIME_EXHAUSTED`。无关且未引用的旧结构注释不会错误缩短有效期，但必需覆盖项和真实使用的证据不会被忽略。

## 6. Soft Score、风险额度与数量

### 6.1 软评分

1. 八维沿用第四阶段，前七维及整体必须可评价，不将缺项归一化成安全分。
2. 检查整体覆盖度、综合分门槛以及**七个单维各自最低分**；止损质量不足不能被方向或 RR 高分补偿。
3. 由综合分匹配 S/A/B/C 风险档位，决定允许风险比例和名义额上限，不决定杠杆。
4. 明确市场 regime 的允许状态、风险比例及净 RR 底线。未知市场默认拒绝。
5. 有效 RR 底线为 `max(全局最低净RR, 市场最低净RR, 档位最低净RR)`。配置验证禁止高等级降低 RR 要求；高分也不能突破底线。

软拒绝原因：`SCORECARD_INCOMPLETE / TOTAL_SCORE_BELOW_MINIMUM / DIMENSION_SCORE_BELOW_MINIMUM / NO_ELIGIBLE_SCORE_TIER`。市场禁入为硬条件 `MARKET_REGIME_NOT_ALLOWED`。

此处“档位”不是第四阶段 excellent/good/fair/poor 标签，不是胜率。RR 数学和评分继续独立：配置只能比较 RR，不重新定义或修改 RR；原计划和原 Scorecard 完全不回写。

### 6.2 风险反推

先计算各预算上限，取最小：

```text
基础申请 = min(明确申请风险, 配置/账户单笔硬上限)
剩余日预算 = max(0, 日亏损硬上限 - 已实现亏损 - 浮亏 - 已预留风险)

预算 = min(
  申请风险, 单笔硬上限, 计划风险上限, 剩余日预算,
  权益 × 配置风险比例,
  可选计划剩余日预算、计划权益风险比例,
  基础申请 × 评分档位风险比例,
  基础申请 × 市场风险比例
)
```

评分/市场是两个独立上限取 min，不将二者相乘放大或隐式复合。任何账户硬上限都取 `min(AdmissionPolicy, PaperRiskSnapshot.limits)`，不能调宽新配置绕过现有账户风控。

三种入场场景的每单位亏损和费用均来自第三阶段原计算：

```text
基础每单位止损损失 = max(三场景中的
  毛止损距离 + 入场费 + 止损退出费 + 入场不利滑点 + 退出不利滑点)
保守固定资金费 = max(明确整单资金费, 0)
风险数量上限 = (预算 - 保守固定资金费) / 基础每单位止损损失
```

这是对第三阶段成本输出的风险数量反推，**不是另建净 RR 算法**。资金费收入不会为更大仓位提供额度；最终 RR 仍由原第三阶段函数按真实正负假设计算。保守允许风险和 `modeled_stop_loss_usdt` 可能因此不同。

保证金/数量约束：

```text
可用保证金额度 = min(
  明确可用自由资金,
  权益 × 最大保证金比例 - 已用保证金,
  最大保证金额 - 已用保证金,
  可选计划保证金额/权益比例
)
保证金数量上限 = 可用保证金额度 / (最坏入场价 / 明确杠杆 + 每单位入场费)

未舍入数量 = min(
  风险数量上限, 保证金数量上限,
  权益 × 档位名义额比例 / 最坏入场价,
  账户/配置单仓名义额上限 / 最坏入场价,
  账户/配置单仓数量上限, 交易所最大数量,
  可选计划数量/名义额上限
)
最终数量 = floor(未舍入数量 / 数量步长) × 数量步长
```

`最坏入场价` 为三场景含不利入场滑点价格最大值，用于保守名义额/保证金占用。最小名义额检查取计划入场区间的最低价格，不能因为当前高报价把不够最小名义额的订单放行。价格 tick 不符拒绝，不调整保护价格。

最终调用 **`calculate_rr(原TradeSetup, quantity=最终数量)`**，重新检查参考和区间两端整单净 RR。固定 USDT 资金费会在缩仓后提高每单位成本，可能令原先可用 RR 不再达标；此时 REJECT，不沿用缩仓前 RR。

最终保守风险为原模型净止损金额与“不用资金费收入抵扣的保守估算”较大者，不能超过预算且不得小于最小可用风险。它不是极端跳空或缺流动性下的绝对最大实际亏损保证。

### 6.3 三种结果

- APPROVE：全部满足，数量主要受正常风险预算反推；仅按步长向下取整不自动降级。代码包含 `STANDARD_PAPER_ELIGIBILITY`。
- REDUCE：某预算上限低于申请风险，或保证金/名义额/数量约束低于风险反推数量；输出 `RISK_REDUCED_<约束>`、`SIZE_REDUCED_<约束>` 和 `REDUCED_PAPER_ELIGIBILITY`。
- REJECT：前述任意硬问题、关键评分失败，或缩仓后无法满足最小预算/交易所规则/净 RR。所有可用额度为零。

合成夹具的实际结果（不是实盘账户/报价）：

| 条件 | 结果 | 预算上限 USDT | 最终保守风险 USDT | 数量 |
| --- | --- | --- | --- | --- |
| 高质量 S 档，申请风险 5 | APPROVE | 5 | 4.99957712325 | 0.863 |
| 同计划，日额度 20 中已有 18 预留 | REDUCE | 2 | 1.99867219875 | 0.345 |
| 同计划，日亏损达到 20 | REJECT / DAILY_LOSS_LIMIT | 0 | 0 | 0 |

前两行最终最小净 RR 均约 2.981418，所需底线 2；此夹具资金费为 0，改变数量不改变单位 RR。另有非零固定资金费导致缩仓后 RR 失败的专门测试。

## 7. 默认配置及含义

完整模板为 [admission.yaml](admission.yaml)。不改既有 `config.yaml`，不自动读取 `.env`。`parse_admission_policy(text)` 拒绝多余键、重复键、YAML 别名/锚点、非法类型/数值或倒序档位。以下都是可调整阈值，不以不可修改的 70/80 分或 1.5/2R 硬编码进判定逻辑。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| policy_version / mode / enabled | paper-risk-admission/v1 / paper_only / true | 版本与旁路启用；mode 只允许 paper_only |
| allowed_symbols | SOLUSDT | 新计划准入范围 |
| allowed_exchanges | BINANCE_USDT_M | 显式提供的规则范围 |
| allowed_risk_sources | paper-ledger-snapshot/v1 | 未来可信 Paper 状态适配器标识 |
| allowed_evidence_verifiers | paper-structure-review/v1 | 未来结构/失效审查供应方标识，不是密钥 |
| required_data | market_price, structure, volatility, btc_reference, eth_reference | 额外必需覆盖项；原计划 required=true 也强制要求 |
| max_market_age_seconds | 15 | 市场、数据截止、非结构必需覆盖项及失效审查最大年龄 |
| max_structure_age_seconds | 300 | 结构、引用确认与 structure 覆盖项年龄 |
| max_account_age_seconds | 5 | Paper 账户快照年龄 |
| max_exchange_age_seconds | 3600 | 合约规则年龄 |
| max_cost_age_seconds | 300 | 成本假设年龄 |
| max_scorecard_age_seconds | 30 | Scorecard 描述年龄 |
| decision_ttl_seconds | 5 | 决策最大寿命，还受所有输入更早到期约束 |
| max_holding_assumption_seconds | 86400 | 资金费估算持仓时长上限 |
| minimum_data_coverage / minimum_score_coverage | 0.8 / 1 | 原计划整体数据覆盖与综合评分覆盖最低值；必需未知仍直接拒绝 |
| minimum_total_score | 60 | 综合分最低值，还要逐项过单维门槛 |
| minimum_net_rr | 1.5 | 全局整单净 RR 最低值，市场/档位只能提高 |
| max_loss_per_trade_usdt | 5 | 单笔硬风险上限 |
| daily_loss_limit_usdt | 20 | 当前风险日最大损失预算 |
| max_trades_per_day / max_consecutive_losses / max_positions | 3 / 2 / 1 | 次数、连亏熔断、持仓与待入场上限 |
| max_leverage | 5 | 明确杠杆最大值，与评分无映射 |
| max_margin_ratio / max_margin_usdt | 0.2 / 100 | 总保证金权益比例及金额双重上限 |
| max_position_notional_usdt / max_position_quantity | 500 / 100 | 新单仓名义额及基础资产数量上限 |
| max_risk_fraction_of_equity | 0.01 | 单计划最多使用权益 1% 风险预算 |
| minimum_risk_budget_usdt | 0.5 | 剩余或最终风险额度低于此值拒绝，而非凑单 |
| btc_crash_3m_pct | -0.8 | BTC 3m 涨跌幅小于等于此值禁止做多 |
| max_volatility_pct / max_spread_bps / max_entry_deviation_bps | 3 / 20 / 50 | 异常波动、价差、入场偏离门槛；MARKET 报价另必须在区间内 |
| minimum_entry_fee_rate / minimum_exit_fee_rate | 0.0005 / 0.0005 | 双边费率假设下界；费率单位为比例 |
| minimum_entry_slippage_bps / minimum_exit_slippage_bps | 10 / 10 | 双边不利滑点假设下界 |
| maximum_entry_slippage_bps / maximum_exit_slippage_bps | 30 / 30 | 双边可接受滑点假设上界 |

七个单维最低分：

| direction_confidence | entry_quality | stop_loss_quality | take_profit_quality | rr_quality | position_quality | execution_clarity |
| --- | --- | --- | --- | --- | --- | --- |
| 55 | 65 | 80 | 75 | 50 | 50 | 80 |

档位默认值：

| 档位 | 综合分起点 | 基础申请风险比例 | 单仓名义额/权益上限 | 净 RR 底线 |
| --- | --- | --- | --- | --- |
| S | 85 | 1 | 1 | 2 |
| A | 75 | 0.75 | 0.75 | 2 |
| B | 65 | 0.5 | 0.5 | 1.5 |
| C | 55 | 0.25 | 0.25 | 1.5 |

C 的实际有效最低总分仍受全局 60 限制，即默认 60–<65。档位分数需严格降序，较高档位不能更低 RR 门槛，也不能有倒置的风险/名义额上限。

市场规则：

| 市场 | 默认允许 | 基础申请风险比例 | 净 RR 底线 |
| --- | --- | --- | --- |
| trend | 是 | 1 | 2 |
| range | 是 | 0.5 | 1.5 |
| high_volatility | 否 | 0.25 | 2 |
| unknown | 否 | 0 | 2 |

这些默认值是待校准的 Paper 风险政策，不是已证明盈利的参数。第 4 阶段描述性评分规则原样保留；第 5 阶段只将其结果与可配置准入阈值比较。

## 8. 防绕过契约及旧 Signal 兼容

`require_paper_admission(decision, *, setup, rr, scorecard, account, exchange, request, policy, evaluated_at, quantity)`：

1. 只接受严格 `AdmissionDecision`，旧 Signal、裸 TradeSetup、dict 或伪造类型均拒绝。
2. 重新验证决策；REJECT 不能消费；逐项绑定整个输入、策略版本和 Paper 快照修订。
3. 在原评估时刻重新执行纯准入，与整份决策比较，不能篡改结果、额度、理由或 ID。
4. 检查原决策尚未到期，再以消费方显式当前时间重评，拒绝风险状态过期。
5. 消费数量必须等于本次已完整 RR 重算的 `max_quantity`。**更小也不能直接绕过**，因固定资金费可能改变净 RR；需重新申请。
6. 返回结果依旧没有执行权，不提供 TradeSetup/Decision → 旧 Signal 的自动执行转换。

既有 `adapt_legacy_signal` 保持单向描述兼容。它不会补造结构确认、成本、覆盖度或安全状态；不能靠 legacy_score 获得新准入。Stage 2 TradeSetup 与 Stage 3 RR 的 `admission_status=not_evaluated` 不回写，准入是单独绑定的决策对象。

后续正确接线必须封闭新 Paper 的所有入口（含限价成交、重启恢复、手工 API 等），在现有执行锁内取得最新可信状态，完成二次准入与**原子风险/次数预留、持久化消费和幂等检查**后才能调用 Broker。不能把旧 Signal 入口作为回退路径。

**本阶段纯函数无法提供数据供应方认证、账户全局互斥、一次性消费或防重放。** 哈希不是加密授权令牌。同一快照可重复做描述计算；不能据此重复下单。这些执行层能力尚未实现，必须在后续 Paper 接线阶段验收，不能通过本阶段测试声称全局绕过已被阻断。

## 9. 调用与运行

当前不需要运行服务。按 README 安装依赖后，可在已有经过验证的描述和合成上下文上显式调用：

```python
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from app.admission.engine import admit_trade
from app.admission.policy import parse_admission_policy

# setup、paper_snapshot、exchange_constraints、request 均由调用方显式提供。
# yaml_text 是已读入内存的独立配置文本；引擎本身不读文件。
policy = parse_admission_policy(yaml_text)
rr = calculate_rr(setup, quantity=analysis_quantity)
scorecard = score_trade_setup(setup, rr, evaluated_at=now)
decision = admit_trade(
    setup, rr, scorecard,
    account=paper_snapshot, exchange=exchange_constraints,
    request=request, policy=policy, evaluated_at=now,
)
print(decision.model_dump_json(indent=2))  # 展示，不下单
```

`analysis_quantity` 是第三/四阶段计算描述所需的显式数量，不是先指定下单量；最终可用数量由第五阶段重新按风险计算。输入评分如果显示原计算量已有质量问题会如实评估，不替它改分。

无需私钥、API Key、`.env`、线上数据库或真实账户。完整可运行合成夹具在 `tests/test_admission.py::case`，不属于生产适配器。

## 10. 测试设计与实际结果

执行环境：Python 3.12、既有锁定依赖；独立源代码副本。在 macOS 系统沙箱中禁止全部外部网络并禁止读写旧跟单仓库；Bridge HTTP 测试另仅允许本机回环，仍禁止外部网络和旧仓库。临时测试目录不对外发布，测试身份均为合成实例。

| 验收项目 | 实际结果 |
| --- | --- |
| `python -m pytest -q tests/test_admission.py` | **216 passed** |
| `python -m pytest -q` | **692 passed**, 2 个既有依赖弃用告警 |
| `python scripts/verify_isolation.py` | `ISOLATION_SOURCE_PASS: 44 Python files` |
| `python main.py --check` | `ok=true, config_valid=true, dry_run=true, live_capability=false, network=none` |
| `node --check app/dashboard/assets/dashboard.js` | 退出码 0 |
| Bridge `npm run check` | TypeScript 检查通过 |
| Bridge `node --import tsx --test tests/*.test.ts` | **27 passed, 0 failed** |
| Bridge `npm run build` | 6 个复用快照验证通过，82 个构建输入，独立 bundle import 通过 |

合计 **719 项测试通过（Python 692 + Bridge 27）**。既有 Python 476 项全部保留；新增 216 项。依赖告警是 Starlette/httpx TestClient 和 anyio BlockingPortal 弃用，不是测试失败；本轮不为消除告警改稳定依赖。

覆盖清单：

- 高总分但净 RR 不足拒绝；高 RR 但止损/目标依据缺失拒绝；高总分但关键单维不足拒绝。
- missing/stale/unverified、未来时间、过期计划、未知账户所有关键字段、未知全部硬上限、失效条件触发均失败关闭。
- 达到日亏损、连续亏损、次数、持仓上限、同币种持仓或待入场冲突拒绝。
- 预算剩余不足按明确最小值 REDUCE 或 REJECT；保证金、数量、名义额、杠杆和费用上限不能被高评分覆盖。
- LONG/SHORT × MARKET/LIMIT 的高质量正常通过；27 组预算 × 杠杆 × 自由保证金组合检查最终损失/保证金不越限。
- 超单笔风险申请直接拒绝；缩仓不改方向、入场、止损、TP/比例或原始评分/RR。
- 固定正资金费导致最终缩仓 RR 不合格拒绝；资金费收入不放大风险数量；未知/过低成本、异常滑点、时长不明拒绝。
- 数量步长向下舍入、最小交易额失败不抬量、价格精度失败不改价、无法无损传入原 RR 的数量拒绝。
- 可成交 ask/bid 超计划或触发失效拒绝；不能只验证参考价；失效审查需绑定当前计划，假声明不能覆盖已触发条件。
- 计划/版本/原结果/评分/决策/请求/配置/快照篡改、老决策过期、旧 Signal、裸对象、减少或增加未经重算数量全部拒绝消费。
- RR 与 score 独立、改变风险档位不改变杠杆、不修改上游对象、JSON 往返、确定性 Decimal 上下文。
- 配置阈值修改会生效，重复键/别名/额外字段/非法类型或范围拒绝；不能通过配置开启 live。
- 对文件、数据库、网络、系统时钟和私有客户端设“毒丸”后仍可计算；静态检查没有新的主流程接线，既有私有接口封禁/隔离测试仍通过。

复现基础命令：

```sh
.venv/bin/python -m pytest -q tests/test_admission.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_isolation.py
.venv/bin/python main.py --check
node --check app/dashboard/assets/dashboard.js
cd bridge
npm ci --ignore-scripts
npm run check
node --import tsx --test tests/*.test.ts
npm run build
```

测试不需要启动主进程/Bridge 服务，不需要公开行情。`npm ci` 只是开发机安装依赖；本次使用既有安装依赖在禁外网环境中执行测试。隔离验证不通过时不得以临时放开私有网络来换取测试通过。

## 11. 发布卫生与不可变文件

- 只允许上述 11 个差异文件，逐文件对照第四阶段已发布 104 文件基线；全树仍只包含源代码、测试、文档和模板。
- 保留既有 `.gitignore`：排除 `.env`、凭据/密钥、账户状态、数据库及 WAL/SHM、日志、运行复盘、备份、node_modules、dist、压缩包和本机工具元数据。仅 `.env.example` 可发布且所有变量值为空。
- 全树做私钥、常见访问令牌、TG Token、带认证 URL、旧账户标识等扫描；不上传原跟单 Git 历史或任何实盘资料。
- 继续使用已发布的脱敏历史验收文档，不覆盖回本地原始运行证据。`config.yaml` 和 `isolation-policy.json` 的实盘封禁内容不变。
- GitHub 发布采用唯一第四阶段父提交，完整文件树做内容哈希核对；分支只更新 `phase-05-risk-admission`，不合并或更新 main/历史阶段分支。

## 12. 还缺什么，为什么现在不能实盘

### 新 Admission 到 Paper 全链路尚缺

1. 可信结构/失效审查供应方、版本化来源证明，不能信任外部自行填写 confirmed。
2. 独立 Paper 账本风险快照适配：损失口径、待入场/持仓风险预留、风险日归属、单调 revision 与一致快照。
3. 执行锁内最新状态复验，原子风险与次数预留、决策持久化消费、幂等和防重放；必须覆盖限价成交/撤单、重启/故障恢复和所有后门入口。
4. 保持旧 Broker/账本语义的正式准入接线、拒绝/缩仓日志及 Dashboard/Telegram/复盘呈现；本轮故意未改主流程。
5. 第六阶段新的分批退出与移动止损按独立授权实施，本轮没有实现，也不依赖虚构的未来保护能力。
6. 接线后的跨模块故障/并发/生命周期回归和长期 Paper 前向验证，不能用纯函数单测代替。

### 真实实盘尚缺且继续硬关闭

仍需独立账户/服务器身份和出口隔离验收、持久化订单意图、原 clientOrderId 对账、交易所原生止损及失败收敛、异步部分成交/撤单竞态、资金费/强平与账户对账、故障/异常 WAL 恢复、长时间压力验证和足够样本外/前向统计。第五阶段既不解除这些阻断，也不以任何测试为名创建验证订单。

**交付本阶段后立即暂停，等待第五阶段验收；不进入第六阶段，不部署，不合并 main。**
