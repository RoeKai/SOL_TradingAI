# Stage 8D — 执行价格分层与数量一致成本

开发基线：`efd9acc207519240af7d737a5faf9007bc7c01fc`。
独立分支：`phase-08d-execution-cost-contracts`。不合并main。

本文件随本阶段追加验证记录；代码冻结与实验结果分开标注。
未标为实际完成的项目不能据此视为验收通过。

## 1. 仅两项授权语义变化

1. `execution-price-layers/v1`：原始结构单点 P 保留；同一可见快照生成
   方向对手价Q，再计算滑点和tick后的M。额外不利报价漂移为0bps。
2. `quantity-funding-budget/v1` 与 `quantity-consistent-admission/v1`：
   显式数量函数成本，先受硬上限约束，再逐个候选q重算原RR、原评分、
   条件S0—S3和风险，不再用最小诊断q一票决定其他q。

没有改动原供应器的精确重获、15秒采样、300秒窗口、180秒参考、
最近止损、最近两结构目标和去重；没有变更任何评分、净RR、S3、
风险或退出规则。未联网、未部署、未访问账户、未调用私有Bridge。
无实时数据、测试网、实盘或Telegram。原main入口保持原字节。

## 2. 版本、隔离及复用

| 对象 | 8D版本／规则 | 旧行为 |
|---|---|---|
| 结构供应 | historical-exact-reclaim-provider/v1 | 原代码直接复用 |
| 证据审核 | historical-prefix-review/v1 | 原前缀重算算法，新schema精确访问器 |
| RR | linear-usdt-rr/v1 | 原calculate_rr；新派生输入独立ID |
| 评分 | 原Scorecard规则 | 原函数、原分档，无胜率推断 |
| 新准入 | quantity-consistent-admission/v1 | 不伪装原AdmissionDecision |
| 退出计划／场景 | quantified-exit-plan/v1 / quantity-consistent-scenarios/v1 | 原PathExperiment、SpreadPath、Exit reducer |
| 成交与账本 | 原OfflinePaper / Broker | 无第二套盈亏记账或退出状态机 |
| 运行 | quantified-historical-run/v1 | 新应用ID1397705787、schema4、quantified-runs |
| 旧实例 | schema1/2/3 | 不迁移、不接管、不补余额 |

通用放宽旧schema访问器的尝试被隔离审查拒绝，未落地。
采用新8D精确身份适配器，旧8C源码不变。复制的薄调度器有AST等价
断言覆盖 `_fills`、`_model_limit`、`trade`、`run_stream`，而现金、
开仓终态／高水位、检查点重放仍调用原模块。

## 3. 价格及每条现金流成本

完整预先设计见 `docs/STAGE_08D_CONTRACT.md`。
方向d=+1 LONG/-1 SHORT，总价差b bps，滑点s bps：

* Qentry=Pmarket×(1+d×b/20000)
* Mentry=BUY向上／SELL向下tick取整(Qentry×(1+d×s/10000))
* Qexit=X×(1-d×b/20000)，Mexit同样按退出方向不利取整。
* 每腿手续费=q×M×fee_rate；资金费单独结算，不叠加扣第二遍价差。

参考100、总价差2bps、滑点10bps、tick.01、费率.0005：

| 方向 | Q | 未取整M | 最终M | 每SOL入场费 |
|---|---:|---:|---:|---:|
| LONG |100.0100|100.1100100|100.12|.050060|
| SHORT|99.9900|99.8900100|99.89|.049945|

旧静态RR保持结构P，entry_slippage_bps映射为实际M与P之差的
向上整数bps预算；exit用原止损及目标各腿总不利bps的最大值再向上取整。
因此这里只把价差、滑点、tick合并一次，不替换P后再加全套成本。
`lineage.exit_mapping` / `per_unit_entry_price_excess` 保存原精确腿、
保守映射腿及差值。它不是精确成交预测；无法无损传入旧float接口
的值拒绝映射。静态目标50/50不改为执行30/40/30。

实际执行计划单独以M建立参考几何；SpreadPath仍从市场P开始，
添加一次价差／滑点；首笔确认成交才冻结真实初始R。
原结构证据不是确定可成交价，也不保证Runner成交于3R。

提交前重新计算；接受前核验账本经济状态、暂停／隔离、有效期、
新鲜对手价及原结构，按最终q重新核验资金、净RR、S0/S3。
不利报价超过锚点拒绝；报价变好也要求新请求，不改旧审批。
确认后实际成交偏离M：保留成交，取消剩余开仓并保护已成交数量。

## 4. 数量成本和求解

F(q)=0 + q×Pbudget×.001×2。
固定0来自声明的模拟手续费模型无整单固定收费，不是把缺失费用填0。
旧模型整单1USDT在A/B始终保留。新接口暂不支持新增非零整单固定
收费计划（需要独立实际收费日程）；旧固定资金费对照仍完整保留。

Pbudget取所有当前可见入场、对手价、成交价、初始止损、已验证价格
结构点及其双方模型quote/fill的最大值。包括SHORT风险侧和Runner
参考路径；不读未来费率、标记价格或文件尾。它不是未来价格上限。
旧1USDT与F(q)差额逐组显示；金额降低不证明同等保守性。

实际资金费保留原符号和历史毫秒顺序入账；累计实际扣款超冻结预算
后锁存暂停新增。收入不抵消已触发的预算越界，不回写RR或审批，
已有保护不停止。原日亏损口径继续包含独立资金费扣款。

求解器先检查原账户／结构／版本／范围，取严格的账户、主配置、
Admission上限。在步长格点上建立线性止损风险上界和各等级上界，
按等级上界轮转枚举向下邻近q，最多32个不同q。每个q重新计算费用、
原RR、原评分、等级上限及S0/S3。不是对非单调通过条件二分。

数量上限同时受请求／计划／单笔／剩余日风险、权益风险比例、
可用保证金、已有保证金占用、等级名义额、绝对名义额、币数量及
交易所min/step/max约束。评分不调高杠杆。

只有完整一致的q才APPROVE/REDUCE。枚举未覆盖整个域且无独立证明
时是UNRESOLVED，不冒称无解。选定q不合格仅称该q拒绝。
静态线性成本可证明整域净RR不合格时单独记录证明；不外推为
非单调S3的证明。独立算术检查：
`(g*q-f0)/(l*q+f0)>=k ⇔ (g-k*l)*q >= (1+k)*f0`，L>0。

## 5. 原规则映射与审批消费

| 旧检查 | 新用途实现 | 是否改变数值规则 |
|---|---|---|
| _check_account / _check_structure | 原函数，另收紧主配置重复上限 | 否 |
| _check_plan_data | 原函数；仅价格／成本分量检查为显式回调 | 仅授权的价格含义变化 |
| 原成本完整性与上下界 | 原完整性；模型费率／滑点分量对原上下限 | 否；合并bps不误当纯滑点 |
| 总分／单维／覆盖／等级 | soft_checks逐项相同方程 | 否 |
| _size各风险及仓位上限 | caps_for原方程；资金费反推改为q函数 | 仅授权的数量含义变化 |
| 原净RR门槛 | max(全局、市场、实际等级) | 否 |
| S0 / S3 | 原条件路径引擎；最终同q；S0取严格风险，S3原门槛 | 否 |
| 原admit_trade / require_paper_admission | 旧调用默认分支不变 | 不将旧REJECT过滤成通过 |

正式记录绑定：原Candidate/TradeSetup、PriceContract、CostFunction、
materialization、derived_setup、rr、scorecard、exit_plan、scenarios、
domain/试算轨迹、risk_ceiling、精确q/leverage/margin/fee_reserve、
账户实例/版本/快照、配置/政策/数据/运行摘要、request_id及有效期。
哈希本身不授予权限：必须存在本实例持久化记录，重算相等，且在
同一事务核验并写入reservations/outbox/consumptions。重复消费返回
原结果，同ID变内容拒绝。Broker只读取该意图，不接受裸Signal或fixture。
原审批与原静态RR保留历史；新result有独立版本及ID。

## 6. 文件清单

新增 `app/execution_costs/`：`__init__.py`、`models.py`、`prices.py`、
`plans.py`、`scenarios.py`、`gate.py`、`storage.py`、`configuration.py`、
`evidence.py`、`funding.py`、`engine.py`、`replay.py`、`report.py`、
`cli.py`、`attribution.py`。
新增测试 `tests/test_stage08d_contracts.py`、`test_stage08d_quantification.py`、
`test_stage08d_execution.py`、`test_stage08d_boundaries.py`、`test_stage08d_cli.py`。
新增本报告、`docs/STAGE_08D_CONTRACT.md`；实验脱敏汇总在`validation/stage08d/`。
修改README、.gitignore、`app/admission/engine.py`（默认兼容提取），
`scripts/verify_isolation.py`以及五处精确静态导入白名单测试
（admission/config_cli_isolation/dynamic_rr/exit_policy/scorecard）。
未改原策略、配置模板、main、Exit、Broker账本、Bridge或旧实验。

## 7. 实际测试记录（最终结果在本阶段收尾追加）

新普通路径：allow_fixtures=false，从0仓开始，由原供应器生成受控
历史格式样本、真实新准入签发/消费，覆盖LONG/SHORT、部分成交、
扩保护、TP/Runner、成本守恒、资金费和真实子进程崩溃。
没有mock准入或修改门槛。组件评分分档测试单独标注，不冒充正常审批。

开发期失败保留：
* 首批新20项通过；随后组合2失败286通过为新增模块未加入精确
  静态白名单，修正只允许新路径，没有放开网络/runtime导入。
* 新执行测试驱动最初违反事件序号／当前流动性／恢复后审批版本
  前置，修正驱动后仍有首次漏通知两项失败。核实原因是原历史
  模型保留对账延迟：已持久化RECONCILE且reconciliation_clear=false。
  加入未就绪断言和显式时钟推进后，20项全部通过，未修改恢复逻辑。
* 新边界测试4失败27通过：成本翻倍后原最大q已超硬域，应拒绝，
  用同一合法q比较两组RR；原受控信号综合100分，不能假定一定先
  试错一个较低等级。改为明确评分组件输入测试等级边界，保留
  最终q上限断言。一次模块导入拼写错导致收集失败，修正路径。
* 一次隔离脚本在Bridge目录执行导致找不到文件；从根目录重跑
  `ISOLATION_SOURCE_PASS: 116 Python files`，不将启动错误算通过。

## 8. 同一八月候选归因与连续运行（待追加实际结果）

固定原数据与预热，原8C实验文件保持原版本；新代码/配置/价格/
费用/求解器先冻结新RunManifest。A旧价旧费、B新价旧费、C旧价新费、
D新价新费共享同一只读500USDT空账本反事实快照，没有交易副作用，
不是四个独立样本。之后D使用独立可变连续账户。

先固定首小时和带仓合成故障检查，再整月基准／原压力模型。
基准总价差2bps、滑点10bps、接受延迟1000ms；压力滑点20bps、
延迟3000ms；其他费率、量、日期和门槛不调整。
实际命令见README；输出不允许覆盖旧文件。没有完成的月份不能
借首小时或旧代码的整月结果补记为完成。

## 9. 边界与暂停

8D只支持已建模的离线市场价模型和数量资金费预算。Runner完整政策
估值仍UNSUPPORTED；概率／统计期望仍null。价格锚不是成交保证，
Pbudget不是无限未来价格保证；参数不是已核实的真实历史交易所规则。
未建模真实盘口、队列、强平/ADL、维持保证金、真实资金费供给和
实时账户能力。当前受控正例不证明策略有效或真实历史可盈利。
实际历史是否成交必须看独立连续实验结果，不看纯计算通过数。

本轮结束后暂停，不部署、不进入下一阶段。
