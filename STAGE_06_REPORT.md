# STAGE_06_REPORT — 分批退出与持仓保护

- 日期：2026-09-09（UTC+8）
- 仓库：https://github.com/RoeKai/SOL_TradingAI
- 独立分支：`phase-06-exit-policy`
- 已验收父阶段：`phase-05-risk-admission`
- 第五阶段验收起点：`45d7035421aef6dae436a7a9222d3629dfa5450d`
- 本轮审查/修复唯一父提交：`daa68658e4a0219fa8fb05122ebe637e9bf8c66e`（原第六阶段提交保留，不改写历史）
- 本阶段提交：包含本报告的提交，交付消息提供完整 SHA；本报告不嵌入自己的 SHA。
- 范围：纯退出决策模型、确定性规则、确认事件状态机、序列化/重放恢复契约及测试。没有接入 Paper/Live，没有部署或合并 main。

## 1. 交付结论与不可越界项

**审查修订 R1：第六阶段暂缓验收后的三项定向修复，修后等待复验。** 已先在上述原提交代码上重建审查事件并运行：三个问题的 LONG/SHORT 六项均失败。没有把历史 864 项通过当作这些边界安全的证明。本轮修复范围、证据和剩余限制见第 10 节。

新增 `app/exits/`，回答“已确认成交的仓位应减多少、何时申请收紧止损、何时退出余仓”。输入是合成/未来可信 Paper 成交和回执；输出是**待持久化的决策意图**，不是已发送订单或已执行保护。

```text
前五阶段只读历史描述 ── capture_plan ── PlanSnapshot
                                                │
首次确认开仓成交 + 明确规则 + 独立 ExitPolicy ── initialize_exit
                                                │
确认成交 / 回执 / 行情 / 恢复事件 ── apply_event ── 新 ExitState + 新动作意图
                                                │
                           journal + state ── checkpoint / restore_checkpoint
                                                ┊ 尚未接线
                                  后续独立 Paper 执行与事务账本
```

没有网络、账户、数据库、文件、环境变量或系统时钟读取/写入。JSON/YAML 由调用方明确提供文本。没有 API 请求、撤单、仓位操作或 TG 发送；没有启动交易服务。

**当前交易行为没有变化。** 原四策略、Signal、两次风控、执行锁、Paper Broker、账本、50%/50% 退出、Bridge 和生产实盘封禁原样保留。本阶段不把新 30%/40%/Runner 模型接入旧主流程。

TradeSetup、Dynamic RR、Scorecard、AdmissionDecision 均不回写。R 退出节点是独立退出政策的触发位，不是伪造的结构目标，不替换原 TP、不重算或美化原 RR。已有持仓的保护不因 Admission REJECT、新信号暂停、日熔断或评分缺失而停止。

## 2. 文件清单

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| app/exits/__init__.py | 新增 | 独立旁路包，不自动接线 |
| app/exits/models.py | 新增 | 不可变计划快照、成交/回执事件、状态、动作、结果 |
| app/exits/bindings.py | 新增 | 前五阶段历史内容与身份只读绑定 |
| app/exits/policy.py | 新增 | 可配置退出政策与严格 YAML 文本解析 |
| app/exits/runner.py | 新增 | 纯保本/成本覆盖公式、固定 R/Swing/ATR 建议接口 |
| app/exits/engine.py | 新增 | 事件归约、优先级、数量与保护确认、幂等意图 |
| app/exits/persistence.py | 新增 | 完整事件重放、JSON 检查点验证、重启恢复阻断 |
| exit-policy.yaml | 新增 | 独立退出配置模板，main.py 不加载 |
| tests/test_exit_policy.py | 新增 | 基础状态、镜像、成本、精度、隔离测试 |
| tests/test_exit_recovery.py | 新增 | 竞态、乱序、恢复、篡改、数量守恒矩阵测试 |
| tests/test_stage06_review_regressions.py | 本轮新增 | 六项原始复现及覆盖/成本守恒/控制重试扩展回归 |
| STAGE_06_REPORT.md | 新增 | 本报告 |
| README.md | 修改 | 第六阶段旁路入口、命令、边界、分支 |
| tests/test_dynamic_rr.py | 修改 | 只允许四个新增纯文件引用既有模型，不放行运行入口 |
| tests/test_scorecard.py | 修改 | 只允许新 bindings/models 旁路保留历史评分描述 |
| tests/test_admission.py | 修改 | 只允许新 bindings/models 旁路保留历史准入记录 |

相对第五阶段累计：**12 个新增、4 个修改；完整发布树 124 个文件，其余 108 个既有文件内容哈希不变。** 原第六阶段对三项旧测试的旁路白名单扩展保留，本轮未修改任何既有测试或放宽断言。相对本轮审查基线是 **1 个新增、8 个修改，其余 115 个文件内容不变**，精确清单见第 10 节。

未修改任何既有实现，包括 `app/setups/`、`app/admission/`、`app/models.py`、全部策略/风控/Paper/执行/行情/账本代码、`main.py`、`config.yaml`、隔离政策、全部 Bridge/vendor 和依赖版本。原跟单工作树已有修改未提交或发布。

## 3. 数据结构

### 3.1 输入与配置

所有模型不可变、额外字段禁止、布尔/计数严格校验、数值有限；金额、价格和数量用 Decimal，JSON 为字符串。运算使用固定 50 位精度和 ROUND_HALF_EVEN，数量舍入另明确向下，保护价另明确朝收紧方向舍入。

- `PlanSnapshot`：setup_id、plan_version、symbol、side、original_stop、original_targets，以及 setup/rr/scorecard/admission 四份完整内容摘要和历史 admission_result。
- `ExitVenueRules`：symbol、verified、quantity_step、price_tick、min_quantity、min_notional、max_quantity、reduce_only_min_quantity_exempt、reduce_only_min_notional_exempt、exact_close_remainder、atomic_stop_replace；新增 dynamic_full_position_stop（默认 false）和 dynamic_stop_contract_id（默认 null）。动态模式必须同时明确启用和提供契约 ID，不能默认交易所支持。
- `ExitSeed`：固定 mode=paper、position_id、PlanSnapshot、规则快照、first_fill。
- `ExitPolicy`：完整字段/默认值见第 7 节。政策内容与 seed 一同绑定当前仓位，不能热换配置重定义该仓位历史 R 或扩大追踪距离。

`capture_plan` 校验各阶段身份和已存在的 Admission 内容绑定，再复制历史字段；不重新准入、不把 REJECT 改成 APPROVE。已经存在的确认仓位无论历史准入标签如何，都需要退出保护。它不接受裸 Signal，也不能用来申请新开仓。

供应方标识、verified、确认事件和内容摘要都是调用方输入；**不是交易所认证、签名或事件真实性证明**。后续可信适配器必须负责来源、订单归属、方向、币种、成交唯一标识及 reduce-only 能力。此阶段没有验证任何真实账户/交易所能力。

### 3.2 事件

每种事件都有 `event_id / position_id / received_at`；接收时刻必须单调，业务发生时刻可以早于接收时刻。事件身份相同内容不同为契约错误。

| 事件 | 字段 / 语义 |
| --- | --- |
| EntryFill | entry_action_id、fill_id、quantity、price、fee_usdt、occurred_at、confirmed=true；唯一确认开仓成交事实 |
| EntrySealed | entry_action_id、total_filled_quantity；确认本次开仓腿已结束且所有成交数量对齐，才冻结原始总量分配 |
| MarketEvent | observed_at、bid、ask、confirmed、evidence；新信号暂停、日熔断、Admission 拒绝、评分可用性只作信息，不能阻断保护 |
| ActionReceipt | action_id、status、cumulative_filled_quantity；止损确认另需 stop_price、reduce_only_verified、coverage；旧 covers_remaining 只兼容该固定数量订单；替换还需 old_stop_retired 和 retired_stop_cumulative_filled |
| ExitFill | action_id、fill_id、quantity、price、fee_usdt、occurred_at、confirmed=true；只有它改变剩余仓位、已成交 TP 数量和退出盈亏 |
| ProtectionLost | action_id、CANCELED/FAILED/UNKNOWN，以及可选累计成交总量；总量不明时一律先对账 |
| RecoveryRequired | 独立恢复事件；根据持久化动作自动建立恢复阻断，不接受调用方随意清空待核对 ID |

`RunnerEvidence` 支持 swing_low / swing_high / atr / trend_invalid，含唯一 evidence_id、value 或 invalid、confirmed 和 observed_at。不是 AI 自由文本指令，不在引擎内计算或猜测 Swing/ATR。

终态回执不会直接制造成交。比如 FILLED 回执称 3 个已成交，而只收到 1 个真实成交明细时，仍是 SETTLING，仓位只减少 1，等待另外 2 的明细。

`StopCoverage`：mode（fixed_quantity/dynamic_position）、quantity、quantity_version、evidence_id、dynamic_contract_id。quantity 是该订单含已成交部分的总保护容量；固定模式剩余覆盖容量为 quantity 减该止损订单已确认成交量。版本绑定本持仓的确认数量序列，不是行情或一般状态版本。动态模式明确覆盖同一 position_id 的后续同腿成交，直到该保护单退役，必须由规则能力、动作模式与回执契约一致证明。纯模块只校验调用方的证据契约，来源认证仍须后续可信适配器完成。

### 3.3 ExitState 完整分组

| 字段 | 含义 |
| --- | --- |
| schema_version / mode / live_allowed | position-exit/v2 / paper / false，不能开启实盘 |
| seed_digest / policy_digest | 固定种子与政策完整内容绑定 |
| position_id / symbol / side / version / phase | 持仓身份、方向、状态版本和状态 |
| entry_action_id / opened_at / entry_sealed | 原开仓腿、最早已知确认成交发生时刻、是否完成开仓归集 |
| original_quantity / remaining_quantity | 当前已确认原始开仓总量（sealed 后固定）、确认成交剩余量 |
| actual_average_entry / entry_notional | 已知开仓成交的真实加权均价、累计成交金额 |
| remaining_entry_cost / remaining_average_entry | 尚未退出库存的成本金额与成本均价；平仓归零/None，与历史 VWAP 分离 |
| exit_notional / position_quantity_version | 已确认退出金额；每笔唯一开/平成交增加数量版本，重复成交不增加 |
| frozen_r_anchor_entry / frozen_initial_r | 首次确认时的真实成交均价锚点及绝对止损距离，一经建立不变 |
| original_stop / current_stop | 原始止损不可变、当前已确认保护价格只能收紧 |
| protection_status / protection_action_id | MISSING/PARTIAL/PENDING/ACTIVE/UNKNOWN 及当前保护身份；ACTIVE 必须覆盖当前全部余量，PENDING/UNKNOWN 不可展示为全仓已保护 |
| protection_covered_quantity / protection_coverage_version | 按已确认保护容量核验的覆盖量与确认时数量版本；补保护在途仍保留旧单覆盖证据，不冒充新覆盖已生效 |
| tp1_planned / tp2_planned / runner_planned | 以实际原始数量计算并向下取整后分配，尾差归 runner |
| tp1_filled / tp2_filled | 每档累计确认成交数量，不按触价或订单状态字样猜测 |
| tp1_complete / tp2_complete | 正数计划量已完整确认成交才为 true；0 数量延期不是已完成 |
| runner_quantity | TP2 完成后的实际剩余数量，随确认退出减少 |
| entry_fees / exit_fees | 已发生开仓和退出手续费 |
| realized_gross_pnl / realized_net_pnl | 确认退出毛盈亏；净口径为毛盈亏减截至当时全部已支出开仓/退出费，不含浮盈或假设收益 |
| favorable_extreme / last_market | 持仓以来最有利可信可退出报价、最后有效次序的行情记录；重启清除行情缓存等待新报价 |
| last_received_at / last_confirmation_event | 接收序列时间、最后确认事件 ID，可在 journal 查完整类型和内容 |
| event_receipts / fill_facts | event_id→内容摘要；每笔退出记录 entry_basis_price 及实际分摊 entry_basis_notional，按确认接收顺序核对剩余成本 |
| actions | 所有已形成意图及其回执状态、已成交量，不代表全部实际发送 |
| completed_action_ids | 已处于成交/撤销/拒绝终态的动作，不应误读成全是成交 |
| confirmed_fill_action_ids | 真正有已确认退出成交的动作 ID，与前项分开 |
| milestones | TP1_FILLED / TP2_FILLED 的持久化确认里程碑 |
| emergency_reason / faults | 首个需全退原因与异常隔离代码；矛盾事实不得静默抹平 |
| recovery_pending_action_ids | 重启后必须先按原 ID 对账的未结束/有效保护动作 |

所有仓位数量满足：`original_quantity - sum(已确认退出数量) = remaining_quantity`。状态复验独立重放成本分摊，并核对 `gross = direction_sign × (exit_notional - entry_notional + remaining_entry_cost)`、净盈亏扣费及完全平仓后的成交金额守恒；另核对覆盖数量/版本、TP、止损单调性、事件版本与动作 ID。

### 3.4 动作与结果

`ExitAction` 包含 action_id、position_id、sequence、kind、reason_code、quantity、stop_price、target_action_id、replaces_action_id、close_exact_remainder、固定 reduce_only=true、退出 side（LONG→SELL，SHORT→BUY）、status、filled_quantity、acknowledged_quantity、stop_confirmed、terminal_status/terminal_quantity。

R1 新增 position_quantity_version（形成意图时的数量版本）、protection_mode（固定或动态）、confirmed_coverage（已确认契约）、target_confirmed（控制动作已收到目标订单权威事实，而非仅收到控制 ACK）。后两项为回执状态，不反写意图身份。

kind 为 ARM_STOP / MOVE_STOP / TP1 / TP2 / TP_COMBINED / CLOSE_ALL / CANCEL / RECONCILE。动作 ID 由固定 seed/policy 摘要、序号和不可变动作内容确定性产生。

`ExitResult = state + 本次新增actions + reason_codes`；固定 `execution_authority=none_until_paper_integration`。重复事件返回相同 state 和空 actions，不把历史意图当作新指令再次返回。最终全退可以拆成不超过 max_quantity 的串行分块；CLOSE_ALL 表示全退目标，不承诺一笔就能退出全部。

## 4. Initial R、数量、保本与尾仓

### 4.1 冻结真实 R

```text
frozen_r_anchor_entry = 首次确认开仓成交记录的实际均价
frozen_initial_r = abs(frozen_r_anchor_entry - original_stop)
TP1 触发位 = frozen_r_anchor_entry ± tp1_r × frozen_initial_r
TP2 触发位 = frozen_r_anchor_entry ± tp2_r × frozen_initial_r
```

LONG 用加号，SHORT 用减号。单笔完整开仓确认中该均价就是实际开仓均价。

若同一开仓腿随后还有确认部分成交，`actual_average_entry` 按全部确认成交更新，但**冻结 R 及其触发锚点不移动**。EntrySealed 必须明确确认最终数量，才开始正常分批止盈。乱序到达的更早开仓成交可以修正最早持仓时间，但不事后重写首次确认时冻结的 R。后续不得把最终均价冒充最早冻结锚点；两字段专门分开披露。

初始保护从第一笔确认成交开始，不等 EntrySealed。实际成交已经穿越原止损或 R=0 时不制造有效 TP，直接产生异常全退意图。若应急退出期间原开仓腿仍未结束，要求取消原开仓腿；后续已在途的同腿成交仍入账并继续全退，不重置历史盈亏/R。开仓腿 sealed 后的额外开仓成交视为异常，隔离对账，不将其悄悄当作允许加仓。

### 4.2 原始数量分批与尾差

```text
TP1数量 = floor(实际原始总量 × tp1_fraction / step) × step
TP2数量 = floor(实际原始总量 × tp2_fraction / step) × step
Runner计划量 = 实际原始总量 - TP1数量 - TP2数量
```

默认 30% / 40% / 30%，不是递归减剩余仓位的 30%/40%。尾差归 Runner，部分成交后只补本档未完成的原始计划量。

本档不足最小数量/名义额：延期，不标记完成、不推进保本；到 TP2 条件可合并尚未完成的 TP1+TP2 为单个 TP_COMBINED。若部分退出会留下不符合约束的小仓位，则改为显式全余量退出目标 DUST_SWEEP_FULL_REMAINDER，不伪装成正常 TP1/TP2 全部已完成。

最终退出不向上凑量，不增加仓位，超过最大数量则串行分块。小于最小值或有 sub-step 尾差时，只有调用方明确提供 `exact_close_remainder=true` 才产生精确余量退出意图。否则输出 `UNMANAGEABLE_REMAINDER_REQUIRES_ADAPTER` 并对账，保留真实剩余数量，不能声称已平仓。这是未来执行适配器必须证明支持的能力，不是本模块已验证某家交易所支持的保证。

### 4.3 TP1 后保本和成本覆盖

触到 1R 只建立 TP1_PENDING。必须满足确认成交合计等于 TP1 计划数量，才有 TP1_FILLED；**移动止损意图仍不是已生效止损**。收到新保护的正确价格、reduce-only 和剩余仓位覆盖确认后，才更新 current_stop。

纯保本模式：申请 stop=remaining_average_entry。常规开仓归集后退出时，它与实际开仓 VWAP 相同；出现迟到开仓成交时必须使用真实剩余库存成本。

成本覆盖模式默认保守地把**已支出的全部开仓费 + 已发生退出费**分摊到剩余量；不以已实现毛利润抵扣费用、降低保护线。设 E=剩余持仓成本均价，C=上述已发生费用/剩余量，f=预计退出费率，s=预计退出不利滑点比例：

```text
LONG:  stop = (E + C) / ((1 - s) × (1 - f))
SHORT: stop = (E - C) / ((1 + s) × (1 + f))
```

LONG 向上按 tick 舍入，SHORT 向下。结果必须至少保护入场价，且比已确认止损更紧才形成 MOVE_STOP；永远不放宽。若新保护线已被当前可信可退出报价穿越，或成本覆盖价格无效，改为应急退出，不提交位于错误一侧的止损。

替换要求明确支持 atomic_stop_replace。旧止损保留到新止损确认；必须同时确认旧单已退役和其累计成交总量。总量比已收到成交明细多时，先 SETTLING/对账，不能把旧单漏掉的成交量当成仍可再次退出的仓位。不能证明原子替换能力时，不悄悄走“先撤再挂”空窗，转入明确应急退出流程。

成本覆盖是模型中的费用/滑点预算，不保证跳空、流动性冲击、实际费率变化或资金费后绝不亏损。本阶段不增加资金费/强平模型，不虚构账户净值。

### 4.4 TP2 与 Runner 接口

TP2 完整确认后保留 TP2_FILLED 里程碑并原子进入 RUNNER；剩余数量是真实 Runner 数量。规则只产生候选止损或全退原因，不改变原计划目标。

| 机制 | 确定性规则 |
| --- | --- |
| fixed_r（默认） | 最有利可信可退出报价达到至少 3R 才激活；LONG 使用最高报价减 1R，SHORT 使用最低报价加 1R。3R 本身不是全平价 |
| swing | 使用最近确认、有效时间窗内的 Swing Low（多）/Swing High（空），可配置 R 缓冲；不自行猜结构 |
| atr | 使用明确确认、有效时间窗内的正 ATR 值；最有利报价回撤 atr_multiple × ATR；不调用行情或指标服务 |
| 时间退出 | 达到 max_holding_seconds 触发全退，所有持仓阶段适用，不依赖评分、报价完整或新入场权限 |
| 趋势失效 | 启用时，确认且新鲜的 trend_invalid=true 触发全退；未确认/过期/未知不当作已确认失效 |

`RunnerRule` Protocol、`RunnerProposal(stop_price, reason_code)` 为扩展界面；配置只接受确定枚举，不加载外部代码或 AI 指令。Swing/ATR 缺项时保留已有确认止损，不默认安全，也不停止已有 Stop/时间退出。所有候选都经过方向单调性、价格 tick 和是否已经穿越的检查。

最有利报价从开仓以来累积，但 Runner 跟踪只在 TP2 确认后使用。如果成交等待期间曾有高点、后来回撤越过候选保护线，产生退出意图，而不是伪造成交在历史高点。重启必须等当前行情，不能拿恢复前缓存报价模拟新成交。

## 5. 完整状态机与转换表

`phase` 是业务阶段，`protection_status` 是保护状态，`ExitAction.status` 是动作执行/对账状态；三者不可混为一谈。价格只能使“持久化意图待执行”发生，不能使 TP_FILLED/CLOSED 发生。

| 当前状态/条件 | 输入或已确认前提 | 新状态 / 输出 |
| --- | --- | --- |
| 无状态 | 首次确认 EntryFill | OPEN，冻结 R；ARM_STOP 意图；错误止损/零 R 改应急全退 |
| OPEN、开仓归集中 | 后续同腿确认成交 | 更新实际均价/原始已成交量，R/锚点不变，继续保护，不正常 TP |
| 固定数量止损已确认 | 新开仓成交使余量超过覆盖 | PARTIAL；支持原子替换则 MOVE_STOP/PENDING，旧覆盖保留；不支持或补保护明确失败则 PROTECTION_REQUIRED，不先撤旧保护 |
| 补保护 PENDING | 又收到开仓成交 | 不并发重发替换；先确认在途替换实际容量，再核验新余量，必要时串行再次补保护 |
| 显式动态整仓保护 | 新增同持仓确认成交 | 仅在有效动态契约下扩展覆盖；重启仍须原订单对账，旧布尔 ACK 不授予此能力 |
| OPEN | EntrySealed 总量对齐 | 固定原始量 TP1/TP2/Runner 分配 |
| OPEN，保护明确 ACTIVE | 新鲜报价达到 TP1 且数量合法 | TP1_PENDING，唯一 TP1 意图；剩余数量和止损不变 |
| TP1_PENDING | 接受回执但无成交 | 仍待成交，不减少数量 |
| TP1_PENDING | 确认部分成交 | 仅减少实际成交量；不标记完成，不申请保本 |
| TP1_PENDING | TP1 确认量达到原计划量 | TP1_FILLED 里程碑，申请 MOVE_STOP；原已确认止损暂不变 |
| TP1_FILLED / protection PENDING | 新止损和旧单退役/数量确认 | 更新 current_stop/保护 ID，ACTIVE；如已越 TP2，可生成 TP2_PENDING |
| TP1 未达最小量 | 报价越 TP2 且合并数量合法 | TP1_PENDING + TP_COMBINED；按成交先分配未完成 TP1，再 TP2 |
| TP2_PENDING | 确认部分成交 | 只减少实际数量，不提前 Runner 完成 |
| TP2_PENDING | TP2 全部确认 | 保存 TP2_FILLED 里程碑并进入 RUNNER，真实剩余量成为 Runner |
| RUNNER | 合法且更紧的规则候选 | MOVE_STOP 意图，确认后才生效；触到 3R 不全平 |
| 任意未平状态 | Stop 穿越、时间/趋势退出、保护失败或尾差全退 | 锁存全退原因；优先取消/对账在途 TP 和旧保护，STOP_PENDING/PROTECTION_REQUIRED |
| 有在途 TP | Stop 触发 | 不再新发 TP；CANCEL 指向原 TP；确认结算数量后才处理余量 |
| 任意动作 UNKNOWN | UNKNOWN 回执或恢复未知 | PROTECTION_REQUIRED，只 RECONCILE 原 action_id，不重发同一退出 |
| 任意动作 | 终态累计数量大于已收到明细 | SETTLING，等待缺失成交；不补猜成交、不释放重复退出额度 |
| TP 已确认撤销/拒绝 | 累计量与成交明细一致 | 只对原档未完成量建立新 ID 的意图；明确失败重试有上限 |
| 当前保护 | 仅撤单请求 ACCEPTED | 不能宣称保护已撤/已替换；继续等待目标订单确认 |
| 控制 CANCEL/RECONCILE | 明确 REJECTED/CANCELED | 原目标 ID 不变；在配置预算内形成新控制尝试 ID，耗尽则 PROTECTION_REQUIRED + 原因码 |
| CANCEL 控制 | UNKNOWN | 不重复撤单/退出；只查询原目标订单 |
| RECONCILE 控制 | UNKNOWN 或只有 FILLED 字样而无目标事实 | 明确故障升级，不当作目标已撤/已成交，不盲目重发退出 |
| 控制动作待确认 | 原目标权威回执/成交已对齐 | 解析 target_confirmed；未来新 UNKNOWN 可再次对账，不被历史成功查询永久挡住 |
| 当前保护 | CANCELED/FAILED，但累计量未知 | UNKNOWN/对账；不当作有效保护，也不盲发另一笔可能重复的退出 |
| 当前保护 | 撤销/失败且累计量结算清楚 | MISSING；无未结算退出时立即给出市价全余量退出意图 |
| STOP_PENDING | 确认部分退出 | 按确认量减少，剩余继续串行全退；不视作 CLOSED |
| 任意持仓阶段 | 确认剩余量=0 且开仓腿 sealed | CLOSED；另产生遗留动作清理意图，CLOSED 不代表清理请求全已确认 |
| 剩余=0但开仓腿未结束 | 确认退出完成 | PROTECTION_REQUIRED，取消原开仓腿；迟到成交继续入账/全退 |
| CLOSED | 完全相同事件/成交重复投递 | 状态不变，不重复减仓 |
| 任意阶段 | 身份/数量/终态矛盾、无可用尾差能力等 | PROTECTION_REQUIRED + faults + 对账；不捏造零仓/利润 |
| 恢复仓位 | RecoveryRequired | 将所有未终态动作及有效保护加入恢复待核对 ID；仅新增 RECONCILE |
| 恢复阻断 | 原动作身份与数量得到权威确认 | 逐项解除阻断；全部对齐后回到确认事实推导的业务阶段，不重执行已完成 TP |

枚举保留 ERROR 给未来消费者的不可归约故障界面；本版可归约异常统一用 PROTECTION_REQUIRED，输入类型/完整性错误抛 ExitContractError。TP2_FILLED 是持久化确认里程碑，和 RUNNER 在同一事件原子产生，不人为要求第二条行情来“确认已成交”。

超出剩余量、终态相互矛盾或 fill_id 内容冲突不是可自动猜测修复的差异。原始事件保留在 journal/调用方事件队列，状态标记异常并要求归属/仓位对账；不按负数持仓继续运行，也不静默丢弃后当作成功。输入契约异常时调用方必须持久化异常并冻结消费，不能捕获后当作正常许可。

## 6. 幂等、乱序、持久化与恢复

### 6.1 三种身份

1. position_id + seed/policy 摘要固定生命周期，不能混用其他仓位或改政策。
2. event_id + 内容摘要去重；相同 ID 改内容拒绝。
3. fill_id + 原 action_id/数量/价格/费用/发生时刻去重；不同投递 event_id 的同一成交也不重复减仓。成交时剩余成本均价 entry_basis_price 和已消耗成本 entry_basis_notional 留档，晚到开仓确认不重写已经实现的历史盈亏，也不把历史全部开仓均价用于新余量。

动作 ID 确定性绑定原始种子、政策和动作内容/序号。意图一旦进入返回 state，后续行情只看到它在途，不会再次生成同档意图。重新计算同样事件流产生完全相同的 ID 和状态。已知明确撤销或拒绝后，才可按原档剩余量形成新 ID；UNKNOWN 永远不走这个分支。

同一行情跨多档：只形成 TP1；确认其成交后处理保本，确认保护后再处理 TP2。没有按历史价格穿越顺序凭空填单。迟到的 ACCEPTED 不能把已成交/退役动作复活；终态数量先到，明细后到时使用 SETTLING。

Stop 与 TP 竞态：保护/全退优先；取消在途 TP 的目标订单并核对其最终成交总量，确认余量后串行全退。原生止损确实发生的成交，即使先于/晚于 TP 回执到达，也按唯一成交事实扣量。所有退出意图 reduce_only=true；实际适配器仍必须证明它真的执行 reduce-only/当前余量限制，纯模型不能替代 Broker 的资金保护。

### 6.2 检查点

`ExitCheckpoint` 包含 schema_version=exit-checkpoint/v2、完整 seed、policy、journal、state、state_digest。v1 不能静默升级或直接恢复：本轮改变了成本和覆盖契约及动作内容，必须拒绝旧格式，保留原日志/动作 ID，待独立迁移验收。不得清空旧状态后重建意图冒充迁移。

- `checkpoint(seed, policy, journal)` 从首次确认成交完整重放，生成可 JSON 序列化检查点，不写文件。
- `restore_checkpoint(json_text, recovery_event=...)` 重新验证模型、完整重放并与保存 state/hash 比较；不一致拒绝，不能靠清掉 TP 标记继续。
- 恢复强制追加新的 RecoveryRequired；所有在途动作和有效保护先按原 ID 对账，清除行情缓存。恢复只新增 RECONCILE 意图，不返回旧 TP/Stop 为新可发送订单。
- 每次重启可以再次要求读取核对，但不能重复提交退出订单。只有正确原动作回执/成交使状态与累计量对齐，才解除对应恢复阻断。
- 控制动作明确失败次数保存在动作历史中，重放/重启不清零。已耗尽同目标 RECONCILE 预算时，恢复也不能绕开预算再次生成该查询。无明确失败的重启可以重新读取核对，不等于再次提交原退出订单。

**本阶段没有真实数据库、事务出站队列、文件 fsync、锁或自动重试服务。** “可持久化”是完整序列化/重放契约，不是已经有跨进程 exactly-once 执行保证。未来消费者必须原子保存新状态、事件和意图，再消费新动作；发送可能发生过的 INTENT 重启后必须 UNKNOWN/按原 ID 对账，而不是重发。

内容摘要用于检测保存状态与事件不一致，不是签名或可信日志认证；同时篡改整份事件和重新生成摘要不在它的防护范围内。可信持久化、访问控制、防重放和事件来源认证属于下一阶段接线的强制验收。

当前异常归属/事实矛盾采用隔离并请求对账，不自动删历史、补填盈亏或无条件解除 faults。安全的异常人工复核/修复事件和长期日志压缩/快照截断需要后续设计；本版恢复依赖完整 journal，不能任意截断。

## 7. 默认参数

模板：[exit-policy.yaml](exit-policy.yaml)。所有阈值只在独立政策中，不改原运行配置。解析拒绝重复键、别名/锚点、额外键、错误类型/数值和比例不等于 1。

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| version / mode | paper-exit-policy/v2 / paper_only | 版本；实盘模式不被模型接受 |
| tp1_r / tp2_r | 1 / 2 | 固定初始 R 的触发倍数，必须递增 |
| tp1_fraction / tp2_fraction / runner_fraction | 0.3 / 0.4 / 0.3 | 占实际原始总量；必须为正且精确和为 1 |
| break_even_mode | cost_covered | 可选 entry_price，均不得放宽当前止损 |
| expected_exit_fee_rate | 0.0005 | 预计退出手续费比例（0.05%） |
| expected_exit_slippage_bps | 10 | 预计退出不利滑点 bp |
| runner_strategy | fixed_r | 可选 swing / atr；只选择确定规则 |
| runner_activation_r | 3 | 至少达到此有利 R 才激活跟踪，不是强制卖出位 |
| runner_trail_r | 1 | 固定 R 跟踪回撤距离 |
| swing_buffer_r | 0 | 已确认 Swing 的额外 R 缓冲 |
| atr_multiple | 2 | 已确认 ATR 跟踪距离倍数 |
| max_holding_seconds | 3600 | 所有持仓阶段的最长时间退出触发 |
| trend_exit_enabled | true | 是否根据确认趋势失效触发全退 |
| market_max_age_seconds | 5 | 可用于触价/移动保护的报价最大年龄 |
| evidence_max_age_seconds | 30 | Swing/ATR/趋势失效确认年龄 |
| max_known_zero_fill_failures | 3 | 同类明确零成交拒绝/撤销次数上限；TP 达限保留保护，应急全退达限升级对账，不热循环无限重试 |
| max_control_attempts | 3 | 同 kind + 原 target ID，自上次权威目标确认后最多容忍的连续明确失败次数（含首次）；耗尽升级；不授权 UNKNOWN 退出重发 |

规则参数是未经收益校准的 Paper 政策，不是盈利承诺。最大持仓时间与超时驱动仍由未来调度器提供当前事件时间；纯函数本身不会睡眠或定时唤醒。

## 8. 运行与实际测试

无需启动行情、账户服务、主程序或 Bridge 服务：

```sh
.venv/bin/python -m pytest -q tests/test_exit_policy.py tests/test_exit_recovery.py tests/test_stage06_review_regressions.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_isolation.py
.venv/bin/python main.py --check
node --check app/dashboard/assets/dashboard.js
cd bridge
npm run check
node --import tsx --test tests/*.test.ts
npm run build
```

使用既有锁定依赖，Python 3.12。实际回归在独立源代码副本中运行，macOS 系统沙箱禁止外部网络并禁止读写原跟单仓库；Bridge HTTP mock 仅放行本机回环。没有为测试放开真实交易网络，也没有访问真实账户来查询“是否零订单”。

| 验收项 | 实际结果 |
| --- | --- |
| 原退出/恢复测试 | **145 项保留并通过，断言未修改** |
| 本轮审查回归 | **108 项通过；其中六项原始复现在基线上全部失败** |
| Python 全量 | **945 passed，2 个既有依赖弃用告警** |
| Bridge 测试 | **27 passed，0 failed** |
| 总计 | **972 项通过** |
| 静态隔离 | `ISOLATION_SOURCE_PASS: 51 Python files` |
| 启动前纯检查 | `ok=true, config_valid=true, dry_run=true, live_capability=false, network=none` |
| Bridge TypeScript / 构建 | 通过；6 个隔离复用快照，82 个构建输入，独立 bundle 导入通过 |
| 原 Dashboard 脚本语法 | 通过，未修改脚本 |

两个 Python 告警来自既有 Starlette/httpx 和 anyio 弃用，不是失败，本轮没有扩大范围改依赖。Bridge 日志中的 FILLED 来自原测试 mock，不能理解为实盘成交。

测试覆盖：

- LONG/SHORT 镜像全过程；触价不成交不保本；TP1/TP2 部分成交；TP2 完成和 Runner 真实余量。
- 保本/成本覆盖双方向公式及 inward tick；成本覆盖线已穿越时全退；已收紧保护不能放宽；R 和原始计划内容不变。
- 同一 event_id 与同一 fill_id 重复投递；事件冲突、状态/费用/盈亏/数量/动作 ID 篡改；确定性 Decimal 上下文。
- 单次跨多档、部分撤单后只补本档剩余量、Stop 与 TP 成交乱序/并发、终态先到成交后到、迟到接受回执、矛盾终态和超额成交隔离。
- UNKNOWN TP/MOVE_STOP 不重发；止损失败、未确认撤单、旧止损退役累计量缺失/落后明细；无原子替换能力不能制造无保护空窗。
- 30 组方向 × 原始数量 × 分配政策矩阵的数量守恒；最小订单、TP 合并、尾差全退、sub-step 尾差、最大量分块；无精确全退能力保持真实残仓并报告异常。
- 6 种阶段恢复、两次重启、恢复后旧 ID 对账解除阻断、已完成 TP 不重执行、CLOSED 恢复保持零仓、篡改/截断检查点拒绝。
- 部分开仓第一笔冻结 R、实际均价单独更新、乱序开仓发生时间、应急退出后迟到原开仓成交不重写历史已实现盈亏。
- 日熔断、新信号暂停、Admission REJECT、评分缺失均不阻断已有 Stop；无报价仍可时间退出；过期/未来/未验证行情不伪造成交。
- Swing/ATR/趋势失效的确认与新鲜度；可配置分配、阈值和有界明确失败重试。
- 文件、数据库、网络、时钟毒丸测试；静态无主流程接线；live 模式始终不被接受，原所有私有接口封禁测试仍通过。

基础调用方式：

```python
from app.exits.engine import initialize_exit, apply_event
from app.exits.persistence import checkpoint, restore_checkpoint

# seed/policy/confirmed_event/recovery_event 为调用方构造的合成或可信 Paper 记录。
initial = initialize_exit(seed, policy)
result = apply_event(seed, policy, initial.state, confirmed_event)
# result.actions 只是待持久化意图，本阶段没有发送函数。
saved = checkpoint(seed, policy, (confirmed_event,))
restored = restore_checkpoint(saved.model_dump_json(), recovery_event=recovery_event)
```

完整可运行的合成示例在测试 Harness，不属于生产账户适配器。不得在真实服务中直接以“加载测试 fixture”冒充可信状态。

## 9. 发布、卫生与未完成项

### 发布范围

本轮只追加第 10 节所列 9 个差异文件，逐文件核对审查基线完整树。保留 `.gitignore`，不上传 `.env`、API Key、真实账户、数据库、日志、运行报告、node_modules、dist、旧跟单系统 Git 历史或本机部署信息；`.env.example` 变量值继续为空。历史验收材料保持已发布脱敏版本。

本轮在现有 phase-06-exit-policy 上追加单父修复提交，父提交为审查基线 daa68658；不 rebase、不 force push、不改写此前历史。GitHub 发布树与本地审查树逐文件 hash 一致。main 和其他阶段分支保持不变，不合并，不运行旧服务。

### 进入 Paper 全链路之前还缺

1. 可信 Paper 成交/回执适配器：订单/仓位身份、同腿开仓 sealed、唯一 fill_id、累计成交终态、真实 reduce-only/余量限制。
2. Admission 与 Exit 的执行边界衔接：新开仓必须消费有效准入、执行锁内二次校验和原子预留；已开仓保护必须独立于新开仓熔断。
3. 独立事务账本、事件收件箱、状态/意图原子提交、CAS 版本锁、幂等出站消费；发送可能发生过必须原 ID 对账。当前 JSON 可重放不等于这一事务链已完成。
4. 把原子止损替换、旧单退役累计量、动态余量覆盖、最小规则豁免和精确尾差全退映射到真正 Paper Broker，并验证竞争条件；无法证明能力不能把布尔值写 true 绕过。
5. 调度/超时、断线、重启恢复阻断、后台告警与人工异常对账；行情缺失不能让已有保护停止。矛盾事实的修复必须有新权威证据，不能删历史解锁。
6. 当前规则快照/政策按仓位绑定；交易所规则变化、安全政策迁移、完整 journal 压缩/长期恢复需独立可追溯设计，不能重建初始 R 或回放重复下单。
7. Dashboard / Telegram / Review 对待执行、已确认成交、保护 ACTIVE/PARTIAL/PENDING/UNKNOWN、覆盖数量/数量版本、恢复阻断、尾差异常的准确呈现；本阶段未接线这些页面或队列。
8. 新全链路并发、崩溃、磁盘错误、长时间断网/行情缺失及前向 Paper 统计验证，不能用纯函数单测替代。

真实实盘仍额外缺独立账户/服务器身份、出口 ACL、真实交易所订单/原生保护/部分成交/资金费/强平/余额对账等验收；生产代码封禁本轮完全未解除。没有真实账户或验证订单，没有部署。

## 10. 审查修订 R1：三项阻断问题的复现、修复与证据

### 10.1 先复现，后修复

基线为 `daa68658e4a0219fa8fb05122ebe637e9bf8c66e`。独立 Git 工作副本 HEAD 与远端 phase-06-exit-policy 一致且源码干净；先只增加 `tests/test_stage06_review_regressions.py` 中最初六个用例，按审查消息提供的事件序列重建，不修改引擎后运行：

```sh
python -m pytest -q tests/test_stage06_review_regressions.py -k test_review_
# 基线实际结果：6 failed in 0.31s
```

独立复现方式：在干净的基线副本中仅加入当前新增测试文件，以上 `-k test_review_` 选择六个原始用例；它们不依赖新增覆盖类型。扩展用例只在修复版本执行。发布前再次使用基线源码副本验证同一测试文件的这六项，仍全部失败。

| 阻断项 | LONG 基线 | SHORT 基线 | 修复后 |
| --- | --- | --- | --- |
| 4 单位保护确认后追加成交 6 单位 | 10 单位仓位仍 ACTIVE，只有 4 单位保护确认 | 同样失败 | 原覆盖量保留 4，状态 PENDING；形成 10 单位原子替换意图，确认前不宣称 ACTIVE |
| 4@100 退出后迟到 1 单位开仓 | 毛盈亏 −49.8，正确应 −49 | 100 开空/110 平空，迟到 101 开空/110 平空也为 −49.8 | 两方向均 −49；有费用时再减全部确认费用 |
| 原保护 CANCEL 明确 REJECTED | 后续行情仍 STOP_PENDING，无新控制或故障 | 同样失败 | 按原目标 ID 有界新控制尝试；耗尽后明确异常及对账，不静默无限等待 |

这六个用例的原始断言完整保留，没有用改变预期、删除断言或忽略测试来消除失败。原 145 个退出测试及所有前阶段测试均未修改。历史 864 项测试没有覆盖以上关键断言；本轮补全后重新运行完整测试。

### 10.2 覆盖缺口的根因与修复

根因在原 `apply_event(EntryFill)`、`_receipt` 和 `_drive`：只更新数量/历史均价，`covers_remaining=true` 的一次确认没有容量或数量版本约束，`ACTIVE` 被沿用到后续成交。

修复规则：

1. 每个唯一 EntryFill/ExitFill 更新 position_quantity_version；Stop 意图记录形成时版本，确认记录容量、版本、证据标识。重复事件/成交不增加版本。
2. 默认 fixed_quantity。旧布尔回执最多证明该意图固定数量，不能隐式证明新增成交；固定 ACK 的总容量必须与订单意图一致。每次归约重新核验可覆盖容量。
3. 4→10 后，保留原止损 ID 及 4 的覆盖证据，形成 MOVE_STOP(quantity=10, stop=原价, replaces=原ID)。不先发 CANCEL，不放宽止损，不假定替换已完成。
4. 替换期间 10→12 时，不并发再发；10 的确认只能覆盖 10，核验后串行请求 12 的保护。原子替换 ACK 必须同时证明新保护和旧单退役累计成交量。
5. 动态模式只有显式能力 + contract ID + 对应动态动作 + 相同契约回执 + 当前数量版本/容量证明才能建立；语义明确包括同一 position_id 后续开仓成交。不匹配、缺失、过期数量版本或旧布尔确认拒绝建立动态保护。
6. 无原子替换能力、补保护明确失败：保留已有部分保护，PROTECTION_REQUIRED 并报告，不取消有效旧单制造空窗。补保护 UNKNOWN 只查原替换 ID；不能再挂一个可能重叠的退出。

关键原因码：PROTECTION_COVERAGE_INSUFFICIENT、STOP_COVERAGE_CONTRACT_INVALID、PROTECTION_COVERAGE_REPAIR_UNAVAILABLE、PROTECTION_COVERAGE_REPAIR_FAILED。

### 10.3 迟到成交记账的根因与修复

根因在原 `_apply_fill`：退出成本使用 actual_average_entry（全历史入场 VWAP），含已经消耗的仓位，后续新成交又被历史成本重复摊入。原测试只证明旧盈亏/R 不被重写，未校验最终现金流。

现在分离三个口径：

- frozen_r_anchor_entry / frozen_initial_r：首笔确认固定的政策锚点，绝不改写。
- actual_average_entry / entry_notional：全部已知开仓成交的历史 VWAP/金额，用于历史描述。
- remaining_entry_cost / remaining_average_entry：当前尚未退出库存的加权成本，用于下一次退出和保本计算。

按**确认接收顺序**归集事实：开仓只增加当前库存成本；部分退出按当前库存成本分摊，最后一笔退出消耗全部剩余成本，避免除法尾差滞留。每笔退出保留成本均价和精确分摊金额。occurred_at 可乱序并用于最早持仓时间，但不反向改写已入账成交的成本分摊；无法归属或超出已确认库存仍隔离对账，不猜测未确认仓位。

示例 LONG：400 成本全部退出后剩余成本为 0；晚到 1@99 后成本变 99（不是历史均价 99.8）；90 退出消耗这 99，合计 −40−9=−49。SHORT 完全镜像。

独立守恒校验不依赖引擎的平均价格：

```text
sign = LONG:+1 / SHORT:-1
任意时点 gross = sign × (全部确认退出金额 − 全部确认开仓金额 + 剩余库存成本)
全部平仓时 remaining_entry_cost = 0
全部平仓时 gross = sign × (全部确认退出金额 − 全部确认开仓金额)
net = gross − 全部实际开仓费用 − 全部实际退出费用
```

测试额外覆盖先退出 1/2/4 单位、迟到成交发生时间早于/晚于退出、带/不带费用、LONG/SHORT、非整除成本分配、完全平仓后重启。该修复作用于实际状态、成本事实和校验，而非展示层补差。

### 10.4 控制动作失败的根因与修复

原 `_control_once` 只判断历史上是否出现同 kind + target；已拒绝尝试也永久阻塞后续控制。

现在区分：

| 控制状态 | 处理 |
| --- | --- |
| INTENT / ACCEPTED / SETTLING | 在途；不重复生成，控制 ACK 不是目标订单确认 |
| REJECTED / CANCELED | 明确失败；相同原目标 ID、不同控制尝试 ID；受 max_control_attempts 限制 |
| CANCEL UNKNOWN | 不盲目重发 CANCEL；RECONCILE 原目标订单，目标未结算前不创建 CLOSE_ALL |
| RECONCILE UNKNOWN | 明确故障升级，不无限重发、不重发原退出订单 |
| 控制 FILLED 但没有目标事实 | CONTROL_RESULT_WITHOUT_TARGET_CONFIRMATION，不假定目标已经取消/成交 |
| 原目标权威状态/成交已对齐 | target_confirmed=true；解除对应等待；以后新未知事件仍可对该原目标重新对账 |
| 连续明确失败耗尽 | CANCEL_CONTROL_ATTEMPTS_EXHAUSTED 或 RECONCILE_CONTROL_ATTEMPTS_EXHAUSTED；PROTECTION_REQUIRED，保留真实仓位和原 ID |

重试计数来自持久化动作历史，自上次权威目标确认后统计，重启不清零。新控制尝试不等于新退出订单；UNKNOWN 的 TP/CLOSE/Stop 始终不能换 ID 盲发。重复拒绝事件、迟到控制 ACK、重复行情也不会超额创建重试。

### 10.5 本轮精确文件清单与实际结果

| 文件 | 本轮类型 | 修改内容 |
| --- | --- | --- |
| app/exits/models.py | 修改 | 固定/动态覆盖契约、覆盖数量版本、剩余成本/退出金额、控制目标确认、state v2 |
| app/exits/engine.py | 修改 | 覆盖核验/原子补保护、剩余成本归集与守恒、状态感知控制去重/有界失败处理、恢复计数 |
| app/exits/policy.py | 修改 | max_control_attempts、默认 policy v2 |
| app/exits/runner.py | 修改 | 保本/成本覆盖以当前剩余持仓成本为基础 |
| app/exits/persistence.py | 修改 | checkpoint v2，拒绝 v1 静默恢复 |
| exit-policy.yaml | 修改 | v2 及控制失败上限模板 |
| tests/test_stage06_review_regressions.py | 新增 | 108 项合成回归：原六项复现及相关边界矩阵 |
| STAGE_06_REPORT.md | 修改 | 复現、根因、契约、规则、结果、限制及版本兼容说明 |
| README.md | 修改 | 修复边界、测试命令、版本与测试数量 |

最终实际结果：**945 Python + 27 Bridge = 972 项通过**；其中本轮 108 项新增、原退出 145 项全部保留。静态隔离、main.py --check、Bridge 类型检查/构建/独立导入、Dashboard JS 语法检查通过。测试均为合成事件/mock；外部网络和原跟单目录被测试沙盒禁止。两个既有 Python 依赖弃用告警未扩大范围处理。

### 10.6 修复后的限制与下一步边界

- 这是纯决策层修复，不是已安装到 Broker 的保护功能。没有真实账户、订单、部署、Paper 主链路改动或旧跟单变更。
- 固定保护补齐依赖将来适配器真正实现原子替换。能力不足或失败时只是明确报告未完全保护并升级，不能声称剩余仓位已安全，也不会自动先撤有效旧单。
- 动态能力/evidence ID 是可核验的数据契约，**不是本模块连接交易所验证出的可信事实**。后续适配器必须认证、验证和绑定真实订单语义；不能随便填 true 或字符串。
- 控制重试是有界意图决策，没有后台定时器或退避发送服务。明确升级的 faults 不自动清除，仍需后续可信修复事件/人工处理链路；在途请求超时与告警调度也待接入。
- state/checkpoint v2 不兼容直接恢复旧 v1。原日志、原动作 ID 应保留；安全迁移需要独立核对覆盖容量和成本守恒，本轮不做自动迁移，不靠清空后重放生成新订单。
- 仍缺第 9 节的可信 Paper 适配、原子账本/出站队列、锁内复核、崩溃恢复与全链路竞争验收；不进入第七阶段。

**本轮追加修复提交后立即暂停，等待复验；不自动进入第七阶段，不合并 main。**
