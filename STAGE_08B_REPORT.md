# Stage 8B — 版本化退出计划、条件情景与普通离线 Paper 闭环

## 1. 基线、范围与结论边界

- 仓库：`RoeKai/SOL_TradingAI`；分支：`phase-08b-admitted-paper`。
- 唯一父基线：`3ed71b94a73ee3938e7b5f1be40c16636a94848a`。保留全部历史，无 main 合并、无 force push。
- 只实现本轮 8B；未接实时公共行情、测试网、真实账户、Telegram、服务器或私有 Bridge。
- Live 始终硬关闭；不读真实 `.env`、不读取原跟单配置/数据库、不改旧入口。
- 本报告对应的完整交付 commit 是本文件所在的分支提交，通过 GitHub 分支及 `git rev-parse HEAD` 可核对；不是把报告自包含哈希当作授权。

| 能力/证据 | 本轮结果 |
| --- | --- |
| 单个计算/契约模块 | 有独立结构与测试 |
| 普通新开仓：从零持仓到审批、成交、退出、恢复 | LONG/SHORT 均通过真实业务函数；`allow_fixtures=false` |
| 已有仓位 fixture 链路 | 原 8A 保留，原回归未删；不计作普通入口成功 |
| 本实例合成证据归属/可重放 | 已实现；不是外部市场/交易所真实性认证 |
| 整个 Runner 政策的统计期望/胜率 | **未知，未实现**；旧完整政策估值仍 UNSUPPORTED |
| 实时或实盘 | **未接通，不能使用** |

## 2. 新增与修改文件

新增 18 个文件：

| 文件 | 职责 |
| --- | --- |
| `app/admitted_paper/__init__.py` | 无启动副作用的包入口 |
| `app/admitted_paper/models.py` | 显式 8B 设置、输入、证据、Candidate、ExitPlan、Scenario 模型 |
| `app/admitted_paper/storage.py` | schema 2 新实例及固定供应器注册；复用旧事务边界 |
| `app/admitted_paper/provider.py` | 输入链、三点结构、过去 180 秒大盘参考、规则置信描述、可重放计划供应 |
| `app/admitted_paper/plans.py` | 结构目标与实际退出计划的独立映射、内容/能力/成本检查 |
| `app/admitted_paper/scenarios.py` | 用原 Exit reducer 及其记账驱动 S0–S3 条件路径 |
| `app/admitted_paper/gate.py` | 原 Admission + 原完整契约诊断 + 新用途共享检查 + 情景准入 |
| `app/admitted_paper/engine.py` | 持久审批、锁内复核、风险预留/消费、Broker 验证、实际成交与恢复适配 |
| `app/admitted_paper/demo.py` | 明确标记的合成历史输入、普通开仓/退出与拒绝演示 |
| `app/admitted_paper/cli.py` | 独立 demo/reject/recover/status/review 命令 |
| `app/admitted_paper/review.py` | 只读账本复盘，普通与 fixture 分组统计 |
| `app/offline_paper/pricing.py` | 从原 Broker 提取的同一向不利方向取整报价函数；不改原公式 |
| `examples/admitted-paper/main.yaml` | 独立合成实例模板，默认风险不放宽 |
| `examples/admitted-paper/manifest.yaml` | 独立实例及已验收版本映射 |
| `tests/test_admitted_paper.py` | 普通业务闭环、情景与拒绝、成交及回执一致性 |
| `tests/test_admitted_recovery.py` | 并发、真实子进程崩溃、SQLite 失败、恢复与污染隔离 |
| `tests/test_admitted_boundaries.py` | schema/环境/实盘封禁、原件碰撞、CLI/来源边界 |
| `STAGE_08B_REPORT.md` | 本报告 |

修改 10 个文件：

| 文件 | 精确变化 |
| --- | --- |
| `app/configuration/contracts.py` | 新显式 `conditional_paper_8b` 用途分派和共享输入校验；原 `validate_contract` 默认行为、UNSUPPORTED、旧结果不变 |
| `app/offline_paper/storage.py` | Store 的身份/schema/目录/设置类型成为类属性；默认仍 schema 1/8A，增加精确新表名枚举 |
| `app/offline_paper/broker.py` | 提取报价函数和 ENTRY 授权钩子；默认 8A 钩子仍拒绝普通 ENTRY，只有新子类验证事务审批 |
| `tests/test_dynamic_rr.py` | 仅增加精确新组合文件的导入白名单 |
| `tests/test_scorecard.py` | 同上，无评分断言变化 |
| `tests/test_admission.py` | 同上，无业务风控断言变化 |
| `tests/test_exit_policy.py` | 同上，无退出断言变化 |
| `tests/test_config_cli_isolation.py` | 同上，无配置解析断言变化 |
| `.gitignore` | 排除 `offline-admitted-runs/`，原敏感数据规则保留 |
| `README.md` | 追加 8B 运行命令、边界与限制；前阶段历史说明保留 |

基线其余 152 个跟踪文件内容不变。特别包括 `main.py`、旧 runtime/Paper/账户账本、原 `app/offline_paper/engine.py`（8A R1 修复）、RR、Scorecard、Admission、Exit reducer/runner/R1/R2/R3、政策模板、Bridge、依赖、隔离脚本及所有原件审查测试。没有另写第二套账本/盈亏引擎或退出状态机。

## 3. 版本与新旧契约关系

| 对象 | 版本/范围 |
| --- | --- |
| 新实例 | `sol-admitted-paper/8b`，SQLite application ID 1397705785，schema 2 |
| 输入及供应器 | `SYNTHETIC_OFFLINE` / `synthetic-swing-provider/v1` |
| 证据 | `paper-evidence/v1` |
| Candidate | `paper-candidate/v1`；TradeSetup 本身仍 `trade-setup/v1` |
| ExitPlan | `paper-exit-plan/v1` / plan_version `1` |
| 情景结果 | `conditional-exit-scenarios/v1` |
| 组合准入 | `paper-scenario-admission/v1` |
| 资金费范围 | `synthetic-no-funding/v1`；本模拟合约定义无周期资金费，最多 3600 秒 |
| 原模块 | RR `linear-usdt-rr/v1`、评分 `plan-quality/v1`、Admission `paper-risk-admission/v1`、Exit `paper-exit-policy/v2` 均不改 |

原 `validate_contract` 每次正常审批仍实际调用并原样保存诊断。原 50/50 与下游 30/40/30 不一致、完整 Runner 估值未建模的结果依然存在。**没有按 reason-code 字符串筛掉错误再转 PASS。**

新用途明确分离两组检查：共享的配置、身份/内容、原决策重放、时效、成本下限、模式/规则/证据元数据检查仍运行；“原静态分配必须直接等于整个下游政策”的旧假设只属于旧用途。8B 在执行前额外建立独立 ExitPlan、结构走廊映射、限定资金费模型、条件情景及新的组合准入。未建模的 runner 类型、缺成本、映射冲突、未知版本依然拒绝。新用途不能为旧完整政策估值、实盘或其他未知用途提供 PASS。

## 4. ExitPlan 与证据结构

### ExitPlan 最终字段

- `schema_version / plan_id / plan_version`：完整内容摘要与版本，不是权限令牌。
- `setup_digest / evidence_digest / bundle_digest`：绑定完整原交易计划、证据及配置。
- `symbol / side / entry_method / reference_entry / entry_lower / entry_upper / geometry`：成交前参考几何，不改成交后冻结 R。
- `initial_stop / stop_evidence_ids`：原结构止损，不为 RR 调价。
- `triggers[]`：TP1/TP2 名称、R 倍数、**原始数量**比例、参考价、结构走廊依据和明确映射类型。
- `runner_fraction / runner_reference_evidence_id / runner_reference_price`：参考位置只是条件路径输入，不是挂单或必达目标。
- `policy / policy_digest`：完整 ExitPolicy，含保本方式、退出费率、滑点、固定 R 激活/跟踪、时间/趋势退出、时效与重试配置。
- `rules / rules_digest`：与真实实现对应的合成 Broker 能力和数量/价格规则。
- `costs / funding_model / holding_seconds / evidence_version / created_at / valid_until`：完整成本、模型范围、时效。

原结构目标仍按 50/50 用原 RR 计算；实际 ExitPlan 默认 30/40/30、1R/2R、3R 激活与回撤 1R，来自未改动的 `exit-policy.yaml`。TP1/TP2 是原结构走廊中的提前减仓触发，不宣称恰好等于原结构目标。映射缺失不补造结构。所有原计划内容不可反向修改。

S3 从决策时已存在的全部有效盈利侧结构依据中，筛选满足当前 Runner 激活条件的价格，取距离入场最近者。原静态两个目标不限制额外已存在结构候选；未存在的数据不会被补入。没有合格位置则 S3 UNAVAILABLE。

### 证据供应与防未来信息

`MarketInput` 只有严格的序号、observed_at、available_at、SOL/BTC/ETH 价格、成交量和固定合成 scope，不接受 `verified=true/source/账户余额` 等额外字段。供应器只能在逻辑时钟已经到达 available_at 后持久化输入；链中序号连续、双时间不倒退，每条绑定前一摘要和本实例。

结构点采用严格三点 Swing：左、中、右都已 available，才使用中点；证据保存三条序号及右侧确认时点。入场使用过去确实观察到、现在重新越过的价位；止损用最近的有效亏损侧 Swing，原静态目标用最近两个盈利侧 Swing。只有这一条受控供应路径，没有修改四类旧策略或新增 AI 推理。

方向描述是五条可重算布尔规则的通过比例：近期四条同向、BTC 同向、ETH 同向、量能增加、重获既有价位。不等于胜率，不默认 100；例如 BTC/ETH 不动时结果为 0.6。`btc_return_3m_pct/eth_return_3m_pct` 必须有恰好 180 秒前的输入基准，不能拿更短窗口改名；缺失直接拒绝。波动描述为最近三条观察的高低范围/当前价，属于显式合成 tick 窗口，不宣称真实市场周期指标。

本实例显式注册供应器与无资金费模拟模型，候选内容必须由已持久输入重放得到。登记名字、哈希或 `verified` 本身都不认证外部事实；普通入口只接收库内 Candidate ID。历史批准计划绑定其原输入前缀和原配置；后来新增的输入或供应器失效不会重写旧 seed/policy。

## 5. 情景定义、公式、数量和成本

`ScenarioEvaluation` 包含：version/scope/evaluation_id、ExitPlan/Setup 摘要、精确 quantity、保守初始风险、四个 Scenario、原 static_rr_digest、未覆盖能力；`full_policy_expected_return / win_probability` 固定未知。

每个 Scenario 包含 scenario_id、SUPPORTED/UNAVAILABLE/UNSUPPORTED、assumptions、reason_codes、quantity、entry_quote、分腿数量/报价/成交价/现金流/手续费/时点/事件顺序、毛盈亏、费用、净盈亏、初始风险、情景净 RR、冻结 R、最终保护位、S3 参考结构位置。

| 情景 | 明确条件顺序 |
| --- | --- |
| S0 | 按入场区间各边界及参考报价模拟初始成交→确认初始固定止损→先穿越初始止损→按可成交报价退出；取损失最大的一条 |
| S1 | TP1 触发→可分两笔确认完成→保本/成本覆盖替换确认→回撤到当前保护→退出 |
| S2 | S1 后 TP2 按同样确认规则完成→按当时已确认保护回撤退出 |
| S3 | TP1、TP2 完成→假定到达最近合格结构位置→原 fixed-R policy 收紧止损→确认替换→回撤到该止损→按不利报价退出 |

情景用原退出 reducer 发动作、接合成接受/成交/撤单确认并取得原累计盈亏，没有第二套仓位/退出算法。价格触及不会直接改变 TP 完成状态；TP 可以两笔在相同明确报价成交，第一笔未足量时止损不变。初始入场情景假设完整数量在同一模型报价成交，不覆盖所有未完成开仓与价格变化路径。实际 Broker 单独验证部分开仓、漏通知、乱序、跳价、未知和崩溃。

所有金额/数量累加采用 Decimal（事务/情景 50 位精度）。原 Stage 3 的 float 输入只在能由十进制表示无损往返时使用，不能表示就拒绝。报价函数与 Broker 共用：买入乘 `1+slippage_bps/10000` 后向上取 tick，卖出乘 `1-slippage_bps/10000` 后向下取 tick；手续费为确认数量×实际成交价×费率。

```text
R_frozen = abs(first_confirmed_fill_price - original_stop)
q1 = floor(q_original * tp1_fraction / quantity_step) * quantity_step
q2 = floor(q_original * tp2_fraction / quantity_step) * quantity_step
q_runner = q_original - q1 - q2

LONG  gross_pnl = total_exit_notional - total_entry_notional
SHORT gross_pnl = total_entry_notional - total_exit_notional
net_pnl = gross_pnl - confirmed_entry_fees - confirmed_exit_fees
risk_S0 = max(-S0.net_pnl over permitted reference entry quotes)
scenario_net_RR = scenario.net_pnl / risk_S0   # denominator is the ENTIRE quantity
```

分腿与余量来自原 reducer 的原始实际数量规则，不递归乘剩余量；不能满足精度/最小退出量的条件路径拒绝，不能修改比例或虚构尾差成交。原 Broker 只宣告已实现的固定数量、原子替换、reduce-only 最小名义额豁免；动态整仓/离网精确尾差能力仍为 false。

S3 极值达到 3R 只激活跟踪，不是退出。固定 R 回撤距离从极值扣除，再计滑点、tick 与手续费。S0 是有界报价模型风险，不是跳空损失绝对上限；实际 gap 示例按更差报价成交。各情景没有发生概率，绝不平均成期望值。

## 6. 原准入与新组合准入

1. 先由真实输入生成原 RR/八维 Scorecard，再调用原 `admit_trade`。配置/证据/日风险/单维/评分/RR 等原检查一项不省。
2. 原 Admission 给出数量后，仅再跑一次同精确数量的 RR、Scorecard 和 Admission；数量不稳定则拒绝，不循环搜目标或政策。
3. 新 ExitPlan 内容及政策必须重建一致，按该数量生成 S0–S3。
4. 原 REJECT 永不翻转；S3 不可用或净 RR 小于原 decision.required_net_rr 就拒绝。该门槛保留原等级与市场取严规则。
5. S0 风险与原已计算风险取较大者作为事务预留。重要字段区分：原 `allowed_risk_budget_usdt` 是其精确数量的**模型损失**；原 `policy_risk_ceiling_usdt` 才是各硬/软约束合成的**预算上限**。tick 与实际计费产生的小额差异只能占用这个原上限以内的余额，不增加数量、不提高政策预算；超原预算/日余额直接拒绝。
6. 同时按实际模拟入场价格核验保证金和实际手续费预留，受原账户与政策最严上限约束；高评分不增加杠杆。
7. S0/S1/S2 用于路径描述与风险核验，不要求全部达到正 RR；不把某段 Runner 的小额剩余风险换作整单分母。
8. `require_paper_admission` 在锁内实际重算核对，同一消费必须与完整输入、版本、时效和精确数量一致。

默认仍为单笔风险 5 USDT、日累计损失 20、每日 3 次、连续亏损 2、最多 1 仓、保证金比例 20%、杠杆 5。所有账户/原政策上限继续来自独立模板及原配置；没有为了成功案例放宽原政策。

## 7. 持久审批、事务与 Broker 边界

新数据库目录 `offline-admitted-runs/<run-id>/ledger.sqlite3`，身份由运行目录、run-id、instance-id、application-id、schema 和完整表集合共同核对。没有迁移或接管旧 8A 数据库。initial_balance 仅在显式 create 使用；已有库/坏库不重建、不补资金。

沿用原账户/订单/成交/仓位/费用/风险预留/inbox/outbox 表，新增：

| 表 | 内容 |
| --- | --- |
| `paper_providers` | 固定版本本实例供应器与模型注册 |
| `paper_inputs` | 有时点与前驱摘要的输入链 |
| `paper_candidates` | 可由已知输入重建的不可变候选 |
| `paper_approvals` | 原 Admission、完整 PlanInputs/config binding、ExitPlan/Scenario、原诊断、证据、账户版本、数量、杠杆、风险、保证金、费用、报价、有效期、请求 ID 及内容摘要 |
| `paper_consumptions` | 审批一次性消费、原 action/position ID、批准/不可变预留/意图摘要、预留后账户版本 |

`prepare` 只签发库内计划审批记录，不下单；拒绝结果也保留。`submit` 接受的只有该实例的审批 ID，裸 Signal/TradeSetup/JSON/哈希没有权限。同请求 ID 同内容返回原结果，不同内容拒绝。

`BEGIN IMMEDIATE` 是跨进程执行锁。锁内读取当前账本快照，核对时效、报价、配置、账户版本，重新跑完整计算/准入和内容一致性；然后原子提交风险、保证金与名额预留、审批消费、执行意图、请求结果和账户版本。Broker 不会在此事务提交前创建订单。

Broker 还要从本库核验供应/审批/原决策/ExitPlan/情景、消费记录、对应预留与精确意图。仅换 origin 为 NORMAL 没有效果。派发前账户版本/报价/配置/有效期再变，原 ID 产生明确 REJECTED/0，不扩大风险或暗中重签。

模拟成交与回执继续经 8A 持久 Broker：接受不等于成交；每个 fill_id 唯一。Broker 事实先保存，消费者去重、仓位/费用/盈亏/剩余成本、预留更新及下一动作在同一事务。执行后回执未入库按原 ID 对账，不换 ID 重发。SQLite 唯一键、事务、版本与幂等共同实现恢复，不宣称跨进程天然 exactly-once。

## 8. 实际成交、退出与恢复

- 原批准数量、已确认开仓总量、未成交批准余量分列。只有 Broker 确认明细改变仓位/费用/盈亏；回执累计量不是成交。
- 第一笔真实模拟成交产生带**真实非 REJECT 原审批快照**的 ExitSeed；首笔 R 冻结，随后数量/成本更新不改历史 R。
- 初始保护确认后，追加开仓成交要再次核对覆盖量。ARM/MOVE 提议不等于 ACTIVE；原固定数量保护和覆盖版本规则保留。
- 真实成交超出审批价差几何，保留事实、暂停新开仓、取消原订单未成交余量，按原保护/应急规则管理已成交仓位。不回滚、不加仓补救、不改原止损。
- 未封口、部分取消改变确认数量或实际价格偏离时，原条件结果标记 `REFERENCE_ONLY_*`，不把旧数量 RR 说成新数量下已重算的结果；已存在仓位保护不等待重新准入。
- EntrySealed 继续要求原开仓腿终态、累计高水位与明细归集一致。保留 8A 的原 ID 反向核对、初始/后续漏通知、终态冲突隔离和未结清风险不释放。
- 保留 Exit R1/R2/R3 的固定覆盖、剩余成本、终态锁存、高水位、None/0、迟到明细幂等记账、UNKNOWN 不盲重发。
- 恢复先检查库内审批/消费/不可变绑定和原政策/输入前缀，再调用原 8A 恢复与检查点重放。新增供应器失效只暂停新开仓，已确认仓位仍按原批准前缀/seed/policy 保护；篡改原历史证据、账本或检查点则保留数据并隔离。
- 没有后台热更新。改本地 YAML 不会自动切换已有实例政策；新配置需要新实例/新审批。不会将旧审批字段改成匹配新配置。
- 尚未被 Broker 接受的意图若恢复引起账户版本变化，会在原 ID 明确拒绝并结清零成交，必须由新请求重新申请。已经接受/成交的订单则恢复原事实，不重新开仓。

## 9. 可复制命令与实际演示

安装见 README：独立 Python 依赖环境，不需要 .env、旧交易服务或任何 API Key。在模块根目录执行：

```bash
python -m app.admitted_paper.cli demo --workspace . --run-id stage08b-long --side LONG
python -m app.admitted_paper.cli demo --workspace . --run-id stage08b-short --side SHORT
python -m app.admitted_paper.cli recover --workspace . --run-id stage08b-long
python -m app.admitted_paper.cli review --workspace . --run-id stage08b-long
python -m app.admitted_paper.cli demo --workspace . --run-id gap-long --side LONG --path gap
python -m app.admitted_paper.cli reject --workspace . --run-id bad-evidence --reason evidence
python -m app.admitted_paper.cli reject --workspace . --run-id bad-risk --reason risk
python -m app.admitted_paper.cli reject --workspace . --run-id bad-scenario --reason scenario
```

运行 ID 已存在就拒绝，演示再次运行需选择新 ID；恢复只读/核对原实例，不重放 demo 脚本重开交易。JSON 包含 Candidate、config、Admission、ExitPlan、Scenario、Approval ID 和全部拒绝原因/最终账本。

本次实际运行的普通入口（不是 fixture）：

| 项 | LONG | SHORT |
| --- | --- | --- |
| allow_fixtures / fixture 仓位 | false / 0 | false / 0 |
| 普通仓位 | 1 | 1 |
| 订单 / 确认成交明细 | 9 / 5 | 9 / 5 |
| 最终状态 / 剩余数量 | CLOSED / 0 | CLOSED / 0 |
| 首笔冻结 R | 10.10 | 10.10 |
| 毛盈亏 USDT | 12.24515 | 12.20657 |
| 费用 USDT | 0.054771175 | 0.042248315 |
| 净盈亏 USDT | 12.190378825 | 12.164321685 |
| 现金 USDT | 512.190378825 | 512.164321685 |
| 剩余预留 / 待对账 | 0 / 0 | 0 / 0 |
| 重启前后订单/成交/费用/现金 | 不变 | 不变 |
| S3 条件净 RR（规划场景） | 约 2.370631 | 约 2.368417 |

实际 demo 的 TP 报价路径为 1.1R/2.1R，规划情景为 1R/2R，因此二者净盈亏不被冒充同一结果。以上合成盈利只证明流程和守恒，不证明市场策略盈利能力。

另一次 CLI 反例实际输出：原 Admission `APPROVE`，新组合结果 `REJECT`，原因 `S3_NET_RR_BELOW_ORIGINAL_CONTEXT_FLOOR`；订单 0、成交 0、预留 0、现金 500、`normal_entry_complete=false`。没有用全部 REJECT 的演示冒充闭环，也没有用 fixture 成功冒充普通准入成功。

## 10. 测试、故障结果及未运行项

最终完整回归实际结果：**1961 passed，2 warnings，116.58 秒**。其中原基线 1810 项全部保留，8B 新增 151 项包含在此次全量中，不另行重复累计。两条警告来自既有 Starlette/httpx 与 AnyIO 的弃用提示；没有失败、跳过或 xfail。

| 实际检查 | 最终结果 |
| --- | --- |
| Python 完整回归 | 1961 passed，2 warnings，116.58 秒 |
| 新增 8B 用例（已包含在上述全量） | 普通流程/契约 86 项、故障恢复 26 项、隔离/CLI 39 项，共 151 项通过 |
| Bridge mock | 27 passed，0 failed，0 skipped；528.22175 ms |
| Bridge TypeScript | `tsc --noEmit`，退出码 0 |
| vendor 快照校验 | 6 个隔离快照一致 |
| Bridge 构建及独立导入 | 82 inputs；standalone import passed |
| Python 静态隔离 | `ISOLATION_SOURCE_PASS: 82 Python files` |
| Git 补丁格式 | `git diff --check`，退出码 0 |

独立专项实际执行：

```bash
python -m pytest -q tests/test_admitted_paper.py tests/test_admitted_recovery.py tests/test_admitted_boundaries.py
python -m pytest -q
python scripts/verify_isolation.py
cd bridge
node --import tsx --test tests/*.test.ts
node node_modules/typescript/bin/tsc --noEmit
node scripts/vendor.mjs --verify
node scripts/build.mjs
```

这些测试使用普通 Store.create/供应器/完整依赖，没有 mock `validate_contract`、`admit_trade`、`require_paper_admission`、情景准入或风险预留。新测试可测试纯模型或注入明确合成传输故障，但普通成功例不直接注入仓位。发布前补充的“首笔成交穿越原止损/恰好位于原止损”多空 4 项亦全部通过：不等待下一笔行情即进入应急退出；它们已计入 86 项普通流程用例和最终全量，不与开发期专项运行重复相加。

| 故障/反例 | 实际断言结果 |
| --- | --- |
| 两并发审批争同额度 | 只有 1 份预留/消费/开仓；另一请求账户版本失效拒绝 |
| 意图提交前进程死亡 | 0 订单、0 预留、0 消费，无半事务 |
| 意图提交后消费前死亡 | 原 ID 执行严格版本复核，旧版本明确 REJECTED/0；不重签、不重复下单 |
| Broker 执行后回执未消费死亡 | 恢复原接受事实，仍保留风险，只有 1 个 ENTRY |
| 成交入库确认返回前死亡 | inbox/fill 去重，恢复不重复仓位和费用 |
| 首次/后续成交通知双丢失 | 从原开仓腿/Broker 成交反向核对，补真实明细与保护；多空及双恢复通过 |
| 撤掉剩余开仓/实际偏价 | 已发生成交不抹掉，取消原剩余腿、保护确认数量、冻结 R 不变 |
| 首笔成交已在止损外/恰好位于止损 | 多空均立即生成既有应急退出；0R 事实不改成虚构正 R，无需再来一笔行情才保护 |
| TP/Stop 竞态、终态先于明细 | 总退出不超剩余量，迟到真实明细正常幂等入账 |
| ProtectionLost None/0/已知累计、旧 ACK | 高水位不倒退，缺明细不伪造成交、不解除未完成对账 |
| ENTRY 终态0后较大ACK | 风险重新保留，持久化隔离/原ID对账，无假成交 |
| SQLite 写失败 | 整个原子单元回滚，Store 失败锁存，不继续新开仓 |
| 审批/输入/消费/仓位数据损坏 | 历史保留，拒绝恢复/隔离，不删 WAL 或重建状态 |
| 当前供应器不可用 | 暂停新开仓，原已确认仓位仍可正常止损退出 |
| 配置点号键碰撞、未知字段/版本、伪造来源、未来数据 | 拒绝，无普通开仓/fixture回退 |

原 Stage 7 审查原件仍 4770 字节，SHA-256 `757ec0b46741fd22f51c5e181a431bf9196943ce6040ed311c3ce11b9f27c8e3`。未删除、跳过或放宽任何旧业务断言；仅列出的精确新模块静态导入例外增加。

Python/CLI/静态检查/类型/构建实际运行于 OS 沙箱：禁止全部网络与原跟单目录访问；Bridge mock 仅允许本机回环，所有订单回执都是其合成 mock。第一次用 tsx CLI 启动被该沙箱禁止其额外 Unix IPC，改用 `node --import tsx --test` 对同一 27 项运行通过，没有扩大外网权限。真实账户、实时行情、测试网、服务器部署和策略有效性测试均**未运行且未授权**。

保留 8A 自己的 1810 Python + 27 Bridge 历史记录和审查方独立 36 项组件复验记录，不与本轮或彼此重复相加。

## 11. 剩余限制与暂停

1. 只有明确标记的合成证据供应器；外部可信市场、结构证据及账户规则来源仍未接通。
2. 只支持本模拟无资金费、最多 3600 秒的组合；真实 Binance 资金费/持仓期/强平、流动性/深度与真实手续费不支持。
3. 条件估值仅 fixed_r；Swing/ATR 模型及任意部分开仓/时间/趋势/跳价联合路径的完整估值未实现，不能把四场景当作全集。
4. 原完整政策收益仍 UNSUPPORTED、统计期望/胜率未知；S3 结构位置不保证会到达，不能用此结果宣称策略有效。
5. 实际数量或成交几何偏离后只保留批准情景作为参考；未来如需新的描述性实际仓位情景，应独立编号重算，不能回写历史审批/R/RR。
6. 旧审批的时点/报价/账户版本变化采取拒绝并要求新请求；不自动反复寻优，不实现长时跨版本审批或热更新。
7. 已接受/已成交原订单可恢复；无法调和的累计/终态/损坏历史继续隔离，未实现自动人工解锁或账本迁移。
8. 本地文件所有者和独立数据库是模拟信任边界；内容哈希不是签名，不提供针对可任意篡改整库的管理员攻击的密码学证明。
9. 不改旧 main、实时系统或 Bridge 的封禁。任何后续接真实行情/扩展政策/实盘都须另行授权与验收。

本轮完成后停止推进，仅推送独立分支交付，等待 8B 验收。
