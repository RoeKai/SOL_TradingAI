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

清单内共享`execution_model.funding_model`仍为原8C模型标签，用于保留
原供应器／A/B的固定预算语义；它不覆盖8D的数量成本函数。
本实例的有效预算由`run.cost_function_version`、`run.quantification`、
审批中的CostFunction与最终ExitPlan.funding_model共同绑定。
本轮冻结两边rate=.001、events=2，不下调、不引入资金费收入抵扣。
实际历史结算是另一份事实，既不是旧1USDT预算也不是F(q)的改写。

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
另新增 `scripts/attribute_8d_parallel.py` 及
`tests/test_stage08d_attribution_driver.py`，仅用于有界只读验证。
新增本报告、`docs/STAGE_08D_CONTRACT.md`；实验脱敏汇总在`validation/stage08d/`。
修改README、.gitignore、`app/admission/engine.py`（默认兼容提取），
`scripts/verify_isolation.py`以及五处精确静态导入白名单测试
（admission/config_cli_isolation/dynamic_rr/exit_policy/scorecard）。
未改原策略、配置模板、main、Exit、Broker账本、Bridge或旧实验。

源码／测试／文档共24个新增路径、9个修改路径（不含实验汇总JSON）。
按Git文件模式及blob逐项比较开发基线与代码冻结提交：原237个路径中，
明确修改的上述9个之外，**228个既有文件字节不变**，无删除。
机器可读核对见`validation/stage08d/compatibility-verification.json`。
这证明源码范围，不把它等同于策略收益或真实交易安全证明。
本阶段相对开发基线的完整新增／修改路径、字节数和SHA-256见
`validation/stage08d/file-manifest.json`（清单自身不纳入自己的哈希）。
只包括源码、测试、文档和脱敏汇总，没有原始大行情、数据库、
WAL、完整运行日志、真实配置或账户凭据。

## 7. 测试设计与开发期记录

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

## 8. 同一八月候选归因与连续运行设计

固定原数据与预热，原8C实验文件保持原版本；新代码/配置/价格/
费用/求解器先冻结新RunManifest。A旧价旧费、B新价旧费、C旧价新费、
D新价新费共享同一只读500USDT空账本反事实快照，没有交易副作用，
不是四个独立样本。之后D使用独立可变连续账户。

先固定首小时和带仓合成故障检查，再整月基准／原压力模型。
基准总价差2bps、滑点10bps、接受延迟1000ms；压力滑点20bps、
延迟3000ms；其他费率、量、日期和门槛不调整。
实际命令见README及第11节；输出不允许覆盖旧文件。四组归因实际
结果见第12节，连续账户结果单独记录。没有完成的月份不能借首小时
或旧代码的整月结果补记为完成。

## 9. 边界与暂停

8D只支持已建模的离线市场价模型和数量资金费预算。Runner完整政策
估值仍UNSUPPORTED；概率／统计期望仍null。价格锚不是成交保证，
Pbudget不是无限未来价格保证；参数不是已核实的真实历史交易所规则。
未建模真实盘口、队列、强平/ADL、维持保证金、真实资金费供给和
实时账户能力。当前受控正例不证明策略有效或真实历史可盈利。
实际历史是否成交必须看独立连续实验结果，不看纯计算通过数。

本轮结束后暂停，不部署、不进入下一阶段。

## 10. 首轮代码冻结与真实验证发现

首轮代码：`3fa243db8661785d0e232f4ad8c35a7346eae6f4`，app/config内容摘要
`5d698a043124a831854376e094ea74fa00dceafdb625fccbbf62d3ad6032cd3e`。

* 全量实际执行：**2183 passed，2 warnings，213.81s**。
  其中新增105项；不能再把专项106（含1项旧隔离测试）叠加到总数。
* Bridge mock：**27 passed，0 failed，863.233291ms**；类型检查和构建/import退出0。
  隔离检查116个Python源文件通过。
* 首小时：41,901条，15候选，0订单／成交，现金500，1.517545s。
  两次新进程恢复0.054783s、0.034875s，输入摘要、数量、费用和现金不变。
* 首小时A/B/C/D均15 REJECT。A/C有15个旧单点价冲突；B/D无此冲突，
  但15个在数量一致的线性净RR上有整域不达标证明。
* 首轮全月归因在已报告2000个候选后遇到
  `EXECUTION_QUOTE_STALE_OR_FUTURE`退出2，没有生成完整归因结果，
  精确失败序号未记录；仅保留可证实的最后进度，不伪造完成数。
  `validation/stage08d/v1-interruption.json`保留此事实。
* v1 baseline/stress只有冻结清单和独立实例初始化；没有完成、也未开始
  D整月连续回放。清单不是执行证据，不能用它们替代v2结果。

新增多空复现先执行 **2 failed，24 deselected**，根因是只读比较在
生成价格描述时调用了可执行新鲜度入口，导致合法历史拒绝样本中断批处理。
修复把“已可见历史价格描述”和“可用于新审批的价格”分开：
`describe_prices`可描述过期但已可见的快照，不能引用未来；
`prices`和新准入仍拒绝过期。B/D先作数量无关时效拒绝，C仍保留
旧价格检查。未知映射明确UNSUPPORTED，不过滤成PASS；程序错误仍抛出。
原断言保留，专项复验 **2 passed，24 deselected，0.74s**。

为控制整月只读归因资源，增加独立验证脚本
`scripts/attribute_8d_parallel.py`：最多4个进程，每批128个相同候选，
每个仍调用原 `compare`，主进程按原顺序归集。Worker无数据库、
Broker或可变账户输入。不是新准入算法；工作进程数量不改变样本。
新增 `tests/test_stage08d_attribution_driver.py`验证串／并行结果等价。
所有新实验使用v2名称重新冻结；v1首小时／中断记录不得改标。

串／并行驱动实际测试：**2 passed，7.47s**（1／2个worker）；
8D专项在加入该驱动前：**107 passed，41.03s**。
以上均为专项，不叠加到随后全量回归。
再次隔离检查116文件通过；Bridge mock **27 passed，0 failed，652.07625ms**，
类型检查、构建／独立import均退出0。测试中的交易回执为mock，非联网下单。

第二轮代码冻结前，全量实际执行：
`python -m pytest -q` → **2187 passed，2 warnings，229.07s**。
为原2078项加本轮109项，不把专项或此前全量重复累计。
两个warning为既有Starlette/httpx、anyio弃用提示，没有跳过测试。
同一源树下Bridge27项单独列示。新版本历史实验结果在下节追加。
机器可读开发方执行记录见`validation/stage08d/verification-summary.json`；
不是审查方独立复验，也不是测试已开启真实交易的证明。
实际运行环境：Python 3.12.13、Node v24.18.0；项目独立依赖环境，
Python与回放使用OS禁网沙箱，Bridge mock仅允许本机回环。

实际收集的本轮109项分类（属于上述2187项，不另加）：

| 测试文件后缀 | 项数 | 核心断言 |
|---|---:|---|
| contracts |20|多空报价逐步算术、非零价差的普通准入正例、线性独立不等式|
| quantification |26|固定／比例成本、probe失败而合法q通过、同q重算、账户上限、并发／伪造／旧schema拒绝、过期描述|
| execution |20|正常审批到部分成交／保护／退出／资金费、漏通知恢复、报价漂移／真实成交偏差、8个真实子进程故障|
| boundaries |40|成本单调约束、非单调求解预算耗尽、整域证明、原等级、未来数据、竞争退出、首次模型越界、旧AST／新版本拒绝|
| cli |1|独立CLI在明确合成历史格式数据上初始化／运行／恢复，旧实例不变|
| attribution_driver |2|只读逐条计算与1／2worker结果逐项相同|

普通正例不调用fixture_entry，allow_fixtures=false；测试通过完整供应、
定量、签发、事务预留及Broker，而非把准入mock为APPROVE。
评分分档输入、线性不等式及部分故障注入是明确组件检查，
不将这些独立组件数当成真实历史开仓数。

## 11. v2冻结与首小时验证

实验代码提交：`b488b0e282ef49cfbcceeb4bf84ca5aeb2d7e806`。
app／已绑定模板内容摘要：
`c14f4fe322af622c9e04bd658bf0feff5aec38659a5ab6b83f2943c0a3b710b4`。
本报告最终文档提交与实验代码冻结提交分别记录，不用文档SHA改标实验。

| 清单 | 完整内容摘要 |
|---|---|
| engineering-v2-manifest.json | `86b48ab9228fd751bf0938f600bca3d64cfbac56e4fcb9fe16b40372d9da170e` |
| baseline-v2-manifest.json | `4814773e5b96237582512da461358ae837100362d4ba16d7aa7906e725bdf8bc` |
| stress-v2-manifest.json | `b57ee97c7727dc996a200aa2261a9f5b86b5ed2be77c32ab309652dc33c8d772` |

三者复用原dataset摘要：
`2bd4ab5b4cdca36938dbb32aa01214d8cf5cfd236f153734b25f400d4e7fb098`。
新配置基准／压力摘要分别：
`9810bbb0f0576924df7c1d8d9b8a06f2e7e787a206424d604aff6bc2925a3897`、
`528be3a6218679ae059bc359c8cddf417b949653fe9264ec25d9ccc361d13dd3`。

首小时先完成四组只读归因，再运行D连续实例：
41,901条（BTC18,480、ETH16,051、SOL7,370），240采样，15候选（多11／空4），
15拒绝记录、0消费审批、0模拟订单／成交／完整交易。
现金／权益500，费用／资金费0，胜率与盈亏比为null，不作盈利结论。
实际回放1.368074秒，峰值RSS52,445,184字节，数据库及sidecar2,883,584字节。
两次全新进程恢复0.026848／0.033039秒；自动比较counts、metrics、
游标、prefix和reconciliation_clear完全相等。
已到固定范围末端 `1785545999960`，finished=true，不是整月结果。

首小时归因A/C有15个旧单点价冲突，B/D无此冲突；
B/C/D均有15个静态净RR整域不达标证明。拒绝原因可同时出现，
不能把评分不足和净RR不足相加成更多交易，或把尚未执行的S3算成已通过。

### 可复制的独立离线命令

在项目独立依赖环境、禁止网络与旧系统访问的边界下执行。
不启动main、Bridge服务或下载器。以下是本轮实际命令形式；
相同run或输出已存在时必须换新的实验名称，不能删除数据库解锁。

```sh
python -m pytest -q
python scripts/verify_isolation.py
# bridge/ 内（mock用本机回环隔离环境）
node --import tsx --test tests/*.test.ts
node node_modules/typescript/bin/tsc --noEmit
node scripts/build.mjs

# 工程清单；整月将kind/run/output改为baseline或stress及独立名称。
python -m app.execution_costs.cli freeze --workspace . \
  --dataset historical-data/august-2026-v1 --kind engineering \
  --run-id august-8d-engineering-v2 \
  --code-commit b488b0e282ef49cfbcceeb4bf84ca5aeb2d7e806 \
  --manifest validation/stage08d/engineering-v2-manifest.json
python -m app.execution_costs.cli attribute --workspace . \
  --dataset historical-data/august-2026-v1 --source-run august-baseline-v2 \
  --manifest validation/stage08d/engineering-v2-manifest.json \
  --output validation/stage08d/engineering-v2-attribution.json
python -m app.execution_costs.cli init --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-engineering-v2 \
  --manifest validation/stage08d/engineering-v2-manifest.json
python -m app.execution_costs.cli run --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-engineering-v2 \
  --output validation/stage08d/engineering-v2-result.json
python -m app.execution_costs.cli recover --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-engineering-v2 \
  --output validation/stage08d/engineering-v2-recovery-1.json
# 再次在新进程调用recover，输出recovery-2.json。

# 整月同候选只读归因，最多4个worker，无可变账户或订单。
python -m scripts.attribute_8d_parallel --workspace . \
  --source-run august-baseline-v2 --workers 4 \
  --manifest validation/stage08d/baseline-v2-manifest.json \
  --output validation/stage08d/baseline-v2-attribution.json
```

## 12. 整月同候选四组归因：实际完成

`baseline-v2-attribution.json`：37,141个同源候选，4个只读worker，
898.878037秒，最后候选时刻1788220665000，固定八月范围内全候选已检查。
按候选计37,141，绝不是148,564个独立样本。与原8C同源多18,655／空18,486。
归因工具未创建订单。归集摘要：
`0c2716fa305df59350ab7577ca86279d0bfdd02c4147a28801fd0318aa4a66be`。

| 组 | 价格／成本 | REJECT | UNRESOLVED | 获准 |
|---|---|---:|---:|---:|
| A |旧价格、原诊断入口、原整单1USDT|37141|0|0|
| B |新价格、有界数量求解、保留原整单1USDT|37140|1|0|
| C |旧价格、数量函数及求解|37141|0|0|
| D |新价格、数量函数及求解|37140|1|0|

A用于保留旧诊断事实；B/C/D运行显式定量器。B仍保留整单固定预算，
不把B与A的整条输出差异全归为“只改价格”的单变量效应。
各价格检查、成本与求解轨迹分别可查，不过滤旧REJECT。

* 旧A的37,141个候选均出现单点报价冲突和诊断净RR不足，两个原因共存。
  C有37,135个旧价格冲突，另6个已在BTC硬检查提前拒绝；不是声称那6个
  旧价位兼容。B/D均消除此字段混用，但仍需时效与全部其他检查。
* B/D分别37,107个在允许域上有静态净RR不达标证明；27个报价过期、
  6个BTC禁止做多先行拒绝；1个达到32次预算仍未得到一致解。
  **不能将该1个计为已证明全域无解，也不能给它S3通过。**
* D在被评估数量上的资金费预算范围0.24921248—1.02340920USDT；
  C为0.41181592—1.02340920；B始终整单1。相对B，D范围差额
  -0.75078752至+0.02340920。它们是试算值，不是已批准或已扣费用；
  金额下降不证明相同保守程度。33个前置拒绝样本没有物化候选q。
* D有18,765个方向单维不足、37,107个RR单维不足；这些与上述原因重叠。
  grade上限及风险试算拒绝数也重叠，不能说成新增漏单数。
* 没有仅靠更换成本就获得普通审批的历史样本。37,141个旧probe都使用
  原固定1USDT；这说明旧口径覆盖范围，不等于证明37,141笔都只因成本
  错配而漏单。真正可见的改善与仍存限制见下述特例。

### 唯一未定量完成的候选：不得用高静态RR掩盖退出限制

只读重算定位SHORT、时刻1787375640、结构入场93.70、止损93.72、
结构目标93.33／88.81。旧probe=0.054时静态净RR约-0.85923。
新D在q=5.329时净RR=4.04396289665、等级S，仍然不获准。
实际模型入场93.59，冻结R=.13。原退出状态机确认TP1=1.598后，
因成本覆盖止损已被当前报价穿越，产生
`PROPOSED_STOP_ALREADY_CROSSED`应急全退意图（剩3.731），
而非TP2。状态为STOP_PENDING，TP2并未成交。
因此原条件路径的TP2假设不可完成，返回
`SCENARIO_EXPECTED_ACTION_MISSING:TP2`；有界32个q未找到一致方案。
允许域5,276格点未完全枚举，保留UNRESOLVED。

`unresolved-diagnostic.json`保存两候选只读重算及此条件事件事实。
该诊断没有模拟Broker订单、没有账本写入，不算历史成交。
另一个LONG候选的单位数量静态净RR约2.19165，但已触发BTC做多禁止；
未放行到后续场景。没有为上述例子移动止损、跳过TP1保护、修改退出
政策、增大目标、延长预算或改日期。本轮不扩建替代路径收益预测器。

## 13. 连续D账户实验与恢复命令

本轮基准与压力是两个独立schema4实例；使用同一数据但不同的原声明
执行模型。每组先运行1,000,000条并正常退出，再在新进程继续剩余月份。
这验证已提交游标的分段续跑；合成带仓的强制崩溃验证另见第10节，
不能把正常分段退出称为真实历史带仓崩溃。

清单分别按第11节的freeze/init命令使用`--kind baseline`／`--kind stress`，
run-id和输出名分别为`august-8d-baseline-v2`／`august-8d-stress-v2`。
基准与压力的run实际分别启动，重叠运行；时长不代表独占机器基准性能。

```sh
# 首段：两组各一次，数据库、游标和余额保留。
python -m app.execution_costs.cli run --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-baseline-v2 \
  --max-events 1000000 --output validation/stage08d/baseline-v2-segment-1.json
python -m app.execution_costs.cli run --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-stress-v2 \
  --max-events 1000000 --output validation/stage08d/stress-v2-segment-1.json

# 新进程续跑到原固定范围末端；不是重新初始化。
python -m app.execution_costs.cli run --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-baseline-v2 \
  --output validation/stage08d/baseline-v2-result.json
python -m app.execution_costs.cli run --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-stress-v2 \
  --output validation/stage08d/stress-v2-result.json

# 完成后每次独立新进程恢复；i=1和i=2生成不同输出，不覆盖。
for stage08d_kind in baseline stress; do
  for stage08d_recovery in 1 2; do
    python -m app.execution_costs.cli recover --workspace . \
      --dataset historical-data/august-2026-v1 \
      --run-id "august-8d-${stage08d_kind}-v2" \
      --output "validation/stage08d/${stage08d_kind}-v2-recovery-${stage08d_recovery}.json"
  done
done
```

首段两组均384候选、多185／空199，0模拟订单／成交，现金500；
各消费1,000,000条，游标1785627867270，finished=false。
基准36.307481秒、压力36.079962秒；两组峰值RSS均51,380,224字节。
对应数据库及sidecar为59,908,096／59,998,208字节。
首段输出保留原值，不能被后续整月输出覆盖。

### 实际整月完成结果

两组命令均退出0，`finished=true`。各消费**76,496,930条**，
结束于`1788220799967`，覆盖原固定八月和预热输入，显式丢弃0条。
BTC35,677,521、ETH33,011,157、SOL7,808,252；31天、178,560个采样。
两组完整有序输入前缀均为：
`098ed3de25806dc4661c965cc18d36f447f54a5f56222e2fbbd2624e17b75f73`。
它们共享同一历史样本，不计为两份独立统计证据。

| 实际指标 | 基准v2 | 压力v2 |
|---|---:|---:|
| 候选／计算审批记录 |37141／37141|37141／37141|
| 消费审批／模拟订单／确认成交／完整持仓交易 |0／0／0／0|0／0／0／0|
| 期末现金／权益（USDT） |500／500|500／500|
| 交易费／资金费净现金流（USDT） |0／0|0／0|
| 实际累计回放秒数（两段之和） |3389.611275|3387.088782|
| 末段秒数（75,496,930条） |3353.303794|3351.008819|
| 进程峰值RSS（字节，macOS口径） |51675136|50626560|
| 数据库及sidecar（字节） |4968062976|4971622400|
| 首次／再次恢复核心耗时（秒） |0.213930／0.125062|0.111116／0.240999|

37141条是已记录的计算结果，不是37141个可消费的APPROVE。
两组各37107个静态净RR整域拒绝、27个过期报价拒绝、6个BTC做多拒绝、
1个有界求解未完成；没有通过后再因流动性或接受延迟未成交的订单。
最后一项仍为`UNRESOLVED`计算，执行入口拒绝消费，不改成全域无解。
多空候选各18655／18486，源信号与只读归因一致。

按最终记录的重叠原因：基准有7676个`FINAL_RISK_OUT_OF_BOUNDS`、
37067个等级／账户数量上限试算拒绝；压力相应为33602、37086。
两组均有18765个方向单维不足。它们与净RR拒绝共存，不能相加，
也不能描述为已获批后因实际账户耗尽或流动性不足而漏掉的成交。
旧probe成本问题影响多少笔“本可成交”不能仅从共存原因推断；
可复核的旧probe→一致q静态RR改善、随后仍因S3拒绝的特例见第12节。

完整工件：`baseline-v2-result.json`、`stress-v2-result.json`及各自
`recovery-1.json`／`recovery-2.json`；文件均在`validation/stage08d/`。
实际执行Node严格深比较：每次恢复的**整个result对象**与恢复前相等，
包含费用、现金、counts、positions、原因、statistics、游标、性能与模型状态，
不只是比较余额。原始输出哈希和检查结论保存在
`completion-conservation.json`，该文件是开发方实际核对，不是审查方复验。

另经只读SQL检查：各有93个资金费结算事实、净现金流0，预留、
出站动作、Broker订单／成交、账本成交和持仓均为0，对账清晰。
`model_limit=null`、资金费预算越界0；由于没有持仓，这不能证明
真实历史带仓的模型保证金、资金费预算或退出保护已经受过压力验证。
胜率、利润因子和平均持仓时间均为null；零回撤／零收益不代表安全或有效。

### 实现、运行与证据范围

| 项目 | 本轮实际状态 |
|---|---|
| 价格分层、同q成本／RR／评分／风险定量、版本绑定和原子消费 |已实现，受控多空正常入口回归通过|
| 普通审批→部分成交→保护／退出→资金费→恢复 |合成历史格式数据实际通过；allow_fixtures=false，不mock准入|
| 4个真实进程故障点×多空 |8个专项用例通过，计入2187项，不重复累计|
| 固定首小时、同候选全月A/B/C/D、D连续基准／压力 |均实际完成，版本与结果单独保留|
| 本版本整月结束后两次新进程恢复 |两组均通过，完整输出深比较一致|
| 真实历史产生持仓后的执行／退出／资金费压力验证 |未验证，本次真实历史为零订单|
| 策略有效性、完整Runner期望、样本外表现 |未验证／未建模|
| 实时、测试网、真实账户、私有Bridge、Telegram、部署 |未启用／未运行，实盘硬关闭|

代码／测试自`b488b0e…`冻结后未改动；最后收尾只追加本报告与脱敏
验证工件，不以文档提交SHA改标旧实验。Python、Bridge及隔离检查见
第10节，专项和先后两轮全量均未叠加累计。无未运行但标为通过的检查。

## 14. 未完成／未验证及后续需单独授权事项

* 原完整Runner政策估值仍为`UNSUPPORTED`，统计期望和胜率未知。
  S0—S3只是声明路径的条件计算；本轮不对路径分配概率。
* 首期数量预算仅支持显式无整单固定收费的模型。旧整单1USDT仅用于
  原版本与A/B对照；新增真实固定收费需要独立收费日程和成本契约，
  不从缺失资料推断0，不把旧1USDT除以probe数量后冒充比例费率。
* 32次有界求解不保证遍历整个数量域。唯一未完成候选涉及TP1后的
  成本覆盖保护已被穿越，原退出引擎要求应急退出。若后续扩展条件
  情景以表达该路径，需独立版本、现金流与风险断言；不能在本轮
  跳过保护、虚构TP2成交或放宽止损来得到S3。
* 本轮不是策略寻优；精确重获、时间窗口、目标选择、评分、净RR、
  退出与风险门槛都保留。不授权更换有利日期或自行选择样本外月份。
* 受控多空普通路径及强制崩溃测试使用合成历史格式数据；它们证明
  组合代码行为，不替代真实历史产生持仓后的执行证据。历史实验
  是否完成及是否有成交，只由本次冻结清单对应的实际结果决定。
* 未实现真实盘口队列、强平／ADL及维持保证金表。模型保证金失效
  继续在成交前锁存；后续诊断现金流不能包装成模型支持收益。
* 真实历史资金费事实可以结算模拟持仓，但规则、tick、费率、滑点、
  流动性参与率及预算上界仍是声明假设，不是历史账户能力认证。
* 没有实时数据、测试网、真实账户、私有Bridge、Telegram或部署。
  本轮退出后停止推进，不修改main，不自动进入下一阶段。
