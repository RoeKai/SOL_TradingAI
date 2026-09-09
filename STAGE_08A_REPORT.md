# Stage 8A：独立离线 Paper、事务账本、事件消费与故障恢复

日期：2026-09-10。范围仅为合成离线 8A，不进入 8B。

- 仓库：`RoeKai/SOL_TradingAI`
- 唯一开发基线：`98107391af8bc9f0a10ff9a7435b22a15cfe6a82`
- 独立分支：`phase-08a-offline-paper`；完整发布 SHA 以本报告所在提交和交付回执为准。
- 从干净的独立 Git 副本创建分支。没有修改/合并 main、force push、丢弃其他工作或部署。

## 1. 结论与不能混用的验收范围

| 事项 | 本轮实际结果 |
| --- | --- |
| 既有纯计算/决策模块 | 保留原实现，全部旧测试继续通过 |
| A：普通计划入口 | 实际执行配置校验、RR、Scorecard、Admission 和锁内绑定检查，记录完整拒绝结果；没有任何获准新开仓 |
| B：合成已有仓位/开仓腿 fixture | 实际模拟订单接受、部分成交、撤单、止损替换、TP、尾仓、费用、事务记账和恢复 |
| 持久化及故障恢复 | 独立 SQLite、收件箱/出站队列、原 ID 对账、真实子进程中断及并发测试通过 |
| 完整新开仓准入→执行 | **仍未打通**：完整 Runner 政策 RR 未建模、结构/成本证据缺失，不能放行 |
| 实时行情、交易所测试网、真实账户、实盘 | **未接入，未访问，未启用** |

这不是所有请求立即返回 FILLED 的演示。Broker 有持久化的订单生命周期和独立成交事实；接受请求不改变持仓，价格穿越不代表成交，只有确认成交明细才改变现金、费用和仓位。

fixture 必须在显式 `--allow-fixtures` 的独立实例运行。其开仓腿是测试场景的重构，不是通过准入的新交易。原始计划快照标记 `admission_result=REJECT`，RR/评分摘要使用明确的 fixture 非审批标识，不伪造 APPROVE、结构目标或方向置信度。普通入口没有失败后回退旧 Signal 或 fixture 的分支。

## 2. 对第七阶段报告第 9 节逐项处理

| 前置项 | 8A 实现 | 仍未解决 |
| --- | --- | --- |
| 1 可信计划/结构/置信声明 | 普通示例保留缺失与 unverified；拒绝信息持久化 | 真实或独立可信计划供应者没有接入 |
| 2 风险快照/风险日口径 | 从本实例确认成交、持仓、预留生成 Stage 5 PaperRiskSnapshot；明确 UTC 日界 | 不是旧账本或真实账户适配 |
| 3 规则/成交适配 | 本地固定合成规则，实际 Broker 部分成交、reduce-only、原子换止损 | 非真实交易所能力；动态整仓/非步长尾差不支持 |
| 4 成交 R/偏差 | 沿用 Stage 6 首笔成交冻结 R、剩余成本；实际初始风险超预留时暂停新增并撤销未成交腿 | 完整获准计划的成交偏差准入契约未接通 |
| 5 静态 RR/Runner | 继续保留 UNSUPPORTED/INCOMPLETE 和分配冲突，不新建收益预测器 | Runner、时间/趋势退出的整单收益口径未建模 |
| 6 配置/审批绑定 | 保存完整不可变 bundle、来源摘要、计划内容、账户版本和申请期限；错配拒绝 | 不把旧审批自动换绑新政策；无热更新 |
| 7 锁内复核/预留 | SQLite BEGIN IMMEDIATE 串行化；fixture 原子风险/名额预留经过双进程竞争测试 | 普通入场缺少可通过的上游合同，不接到执行 |
| 8 事务事件/幂等 | 已实现 inbox、outbox、Broker 命令/订单/成交/回执及确认账本 | 不是跨服务分布式事务或无限负载队列 |
| 9 启动对账 | 重放检查点，核对成交/现金/意图/政策，按原 ID 恢复，损坏隔离 | 没有修复损坏数据、历史迁移或自动解锁工具 |
| 10 新 Paper 专门验收 | 本报告分开统计普通拒绝与 fixture 退出/恢复 | 不等于完整新开仓或实时 Paper 全链路验收 |

## 3. 新增/修改文件及原行为影响

新增 15 个文件：

| 文件 | 用途 |
| --- | --- |
| `app/offline_paper/__init__.py` | 无启动副作用的独立包 |
| `app/offline_paper/models.py` | RunSettings、FixtureEntry、Quote 等显式合成输入 |
| `app/offline_paper/storage.py` | 独立身份、SQLite 事务、唯一键、冻结配置及隔离记录 |
| `app/offline_paper/risk.py` | 账本生成风险快照、原子 fixture 风险/名额预留 |
| `app/offline_paper/broker.py` | 有状态合成 Broker、成交/回执、原 ID 对账 |
| `app/offline_paper/engine.py` | 独立组合、普通拒绝、事件消费、退出动作、恢复 |
| `app/offline_paper/fixtures.py` | 明确标记的合成场景及独立模板读取边界 |
| `app/offline_paper/review.py` | 从确认账本生成 JSON/Markdown 复盘，非策略有效性评估 |
| `app/offline_paper/cli.py` | init / run / resume / status / review / crash-probe |
| `examples/offline-paper/main.yaml` | 独立合成实例模板，不是生产配置 |
| `examples/offline-paper/manifest.yaml` | 合成实例 bundle 版本/作用域 |
| `tests/test_offline_paper.py` | 模拟成交、退出、乱序、保护、成本守恒 |
| `tests/test_offline_recovery.py` | 真实进程故障、并发、数据库失败和恢复隔离 |
| `tests/test_offline_boundaries.py` | 普通拒绝、配置/实例、精度、隔离与确定性 |
| `STAGE_08A_REPORT.md` | 本报告 |

修改 8 个文件：

- `.gitignore`：新增 `offline-runs/`，排除数据库、sidecar 和生成输出。
- `README.md`：新增置顶 8A 独立运行说明；旧实时路径标明为保留文档，避免误启动。
- `scripts/verify_isolation.py`：仅给新 `storage.py` / `cli.py` 精确增加 SQLite 导入许可。
- `tests/test_dynamic_rr.py`、`tests/test_scorecard.py`、`tests/test_admission.py`、`tests/test_exit_policy.py`、`tests/test_config_cli_isolation.py`：仅对明确的新离线组合文件扩展精确导入白名单，未删除或放宽业务断言。

相对基线，其余 137 个原有跟踪文件内容不变。包括 `main.py`、旧 runtime/Paper/账本/风控/策略、Bridge、TradeSetup、RR、Scorecard、Admission、Exit Policy、配置解析实现、原政策模板及全部原始审查测试。原交易行为未改变；只有显式调用新入口才会使用新模拟状态。

## 4. 数据、单位与可信来源边界

- 独立数据库身份：application=`sol-offline-paper/8a`、schema=1、mode=`synthetic_offline`、instance=`offline-paper-demo`、独立 run ID、bundle digest；SQLite application_id/user_version 和表集合同时校验。
- 路径固定为显式模块根下 `offline-runs/<run>/ledger.sqlite3`。复用原纯路径隔离工具检查 marker、所有者、权限、软/硬链接和 sidecar；不搜索父配置、不接收任意数据库路径。
- `init` 是唯一显式建库/初始资金步骤；既有目录/数据库一律不能重新初始化。初始余额必须与合成主配置一致。重新打开使用 `mode=rw`，未知库不会自动建表。恢复先核对现金守恒，不以初始余额覆盖异常状态。
- 金额/价格/数量在 SQLite JSON 中保存十进制字符串；计算使用 50 位 Decimal 上下文。不使用浮点累计费用或现金。Stage 2/4 原有 float 描述字段保持原契约，不用来累计账本金额。
- 数量单位 SOL，金额/名义/保证金 USDT，杠杆倍数；费率 `.0005=0.05%`，滑点 `10 bps=0.1%`。时间为显式 UTC Unix 秒，不能倒退；无系统时钟驱动行情、策略或成交。
- 合成规则：数量步长 `.001`、价格 tick `.01`、最小数量 `.001`、入场最小名义 `5`、单单最大数量 `1000`。入场/退出按相应冻结成本假设分别收费用/不利滑点；买价向上、卖价向下按 tick 取整。
- 默认初始资金 500；单笔风险 5、日限额 20、每日 3 次、连续亏损 2、同时 1 仓、杠杆 5、保证金比例 `.2`；额外合成账户绝对保证金上限 100、名义上限 500、数量上限 100。账户同类上限不能比冻结主配置更松，低于政策时仍取严格约束。
- 本次模拟没有资金费、强平、借贷或真实市场冲击事件。没有把缺失资金费补成零交给准入；普通计划仍因该项不完整被拒绝。fixture 复盘明确只统计本次已模拟并确认的费用，不能冒充真实净收益。
- Broker 的固定保护及原子替换已实现并测试；dynamic_full_position_stop=false，exact_close_remainder=false。按步长生成的尾仓可完整 reduce-only 退出，小额退出豁免最小名义金额已测试；非步长精确清仓没有假设支持，不加大数量凑单。
- 所有 `source/verified` 描述只指本实例合成实现，不是交易所认证。普通 API 不接收外部账户 JSON；fill 必须能在本 Broker 的确认成交事实表中逐项核对。fixture 故障注入不能伪造成交或余额。
- 新入口不读取 `.env` 或继承宿主密钥，不导入旧执行路径、网络客户端或 Telegram。Pydantic 自身插件开关不构成交易配置源；隔离测试阻断网络/凭据访问，并对其内部插件查询提供固定禁用值。

## 5. 风险快照和事务设计

风险快照从本库生成，不接受外部自证：

| 字段/概念 | 本轮口径 |
| --- | --- |
| 现金 | 初始资金 + 确认退出毛盈亏 − 已确认全部开/平费用 |
| 权益 | 现金 + 当前有效 bid/ask 标记的浮盈亏 |
| 已用保证金 | 当前剩余入场成本 / 配置杠杆；不是名义金额 |
| 可用保证金 | 权益 − 已用保证金 − 未成交腿的保证金/入场费用预留，最低为 0 |
| 日累计损失 | UTC 日内各确认退出的负净结果之和；分配对应开仓费，盈利不抵减损失 |
| 浮亏 | 当前负浮动结果，独立于日已实现损失；报价缺失时为未知而非 0 |
| 预留风险 | 未终结头寸/开仓腿的完整初始风险预算，分批退出后不提前释放 |
| 次数/名额 | 当日出现确认开仓成交的不同持仓 + 独立待开仓名额；部分成交不重复计次 |
| 连续亏损 | 完整结束持仓的净结果序列，不把 TP 分档当成多笔交易 |
| 未归集回执/隔离 | 快照 reconciliation_clear=false，不能据此新增 fixture 预留 |

费用在入场就扣现金；日损失的“负净退出结果”与现金净变动不是同一口径。尚未退出部分的费用包含在持仓现金和保守风险预留中，不将旧净现金字段重命名成累计损失。只有 EntrySealed、确认平仓且无未解决故障才释放风险预留。

表均有唯一主键，负载为版本化 JSON：`identity/account/configurations/requests/reservations/positions/fills/inbox/outbox/broker_commands/broker_orders/broker_fills/broker_events/quarantine`。

| 原子单元 | 同一 BEGIN IMMEDIATE / COMMIT 中的变化 |
| --- | --- |
| fixture 申请 | 版本/配置/时效/上限复核 → 风险/名额预留 → request 去重 → 开仓腿 outbox 意图 → 账户版本 |
| 合成 Broker 接受动作 | 原 action_id 去重 → 订单/撤单/原子替换 → 持久化回执 → command 消费记录 |
| 合成流动性成交 | 原 fill_id 去重 → Broker 累计量/终态 → Broker 成交事实 → 待投递回执/明细 |
| 事件消费 | inbox 去重 → Exit reducer / 确认 fills / 剩余成本 / 费用 → 预留变化 → 下一动作 outbox → 现金/版本 → 投递已消费 |

SQLite 使用 WAL + synchronous=FULL。Broker 执行与业务回执消费故意是两个事务，确实可以复现“已执行但还未入账”。不是假装一次跨边界提交。

投递是可重复的，不宣称跨进程天然 exactly-once。保证来自单库写锁、唯一 ID、内容冲突拒绝、持久化 command/fill 记录、检查点重放和相同原 ID 查询。出站状态为 PENDING→INFLIGHT→SUBMITTED→CONFIRMED；已有确认不会被另一个消费者迟到的 SUBMITTED 回写覆盖。

进程可能在 Broker 执行之后死亡：启动读取 INFLIGHT，先按原 action ID 查询。已有命令只重发其回执/明细，不新建订单；本地同一数据库明确没有命令时才消费原有持久化意图。UNKNOWN 不换 ID 盲目重发。

SQLite 写入异常回滚整个单元并使当前 Store 失败锁存，不能继续新增交易。重开后仍须恢复校验。损坏检查点、现金/成交/意图/政策内容不一致保留原始记录并隔离，没有删 WAL、删历史或重新生成订单解锁。正常 SQLite 自身 WAL 恢复/检查点不是手工删除恢复证据。

## 6. Broker、退出和恢复规则

1. 普通路径先做 Stage 7 预检，再保留原 RR/评分/Admission 诊断结果，锁内检查申请内容、配置摘要、账户版本和期限；全部结果落 requests。因为完整 Runner 合同不支持，所有普通入口最终 REJECT，不调用 Broker、不预留。
2. fixture 路径标记 B，额外需要实例级显式 opt-in；Broker 自身也检查该开关，不能只靠 intent 的 fixture 字符串解锁。它重放合成开仓腿，绝不声称曾通过 Admission。
3. 接受订单不是成交。只有显式 `Broker.fill` 合成流动性事件能按有效报价、数量能力和成本产生唯一成交事实。没有有效双边报价、行情过旧、未触发止损时不能伪造成交。OHLC 没有盘中顺序时直接拒绝，不能选有利路径。
4. confirmed EntryFill 初始化原 ExitSeed；首笔 R 冻结，新增成交更新剩余成本，未确认保护不能标 ACTIVE。固定数量覆盖增加时先发新保护/原子替换，不预先撤掉旧有效单造成空窗。
5. 原子替换在 Broker 单事务内建立新保护及退役旧单；先投递携带两项确认的 canonical replacement ACK，旧订单独立快照是后续佐证。旧终态不覆写为新非终态；迟到成交仍按原 ID 入账。
6. 开仓腿累计量采用高水位、终态锁存、明细归集；终态与真实明细数量完全吻合后才 EntrySealed。实际初始止损风险（含费用/退出成本预算）超预留，保留已发生成交，暂停新增并取消剩余开仓；已成交部分继续建立保护。
7. TP1 30%、TP2 40%、Runner 30%，以原实际开仓数量计算，沿用原 ExitPolicy 的配置和精度规则。到价只产生意图；TP1 完整确认才推进成本覆盖止损，TP2 完整确认才进入 Runner。原 R 不改变、止损不放宽。
8. 默认 Runner 3R 激活、1R 回撤跟踪；3R 不是确定成交价。止损跳价按实际合成可成交价及不利成本处理，可超过预估风险预算，不保证止损价成交。
9. 减仓数量由 Broker 当前确认库存限制。TP 与 Stop 竞态不会重复退出同一数量。部分成交、终态先到、明细晚到、重复投递按 Stage 6 R1/R2/R3 原规则归集；ProtectionLost 的 None 与 0 原样区分，不通过回执伪造成交。
10. 启动先重放检查点，核对现金/成交/不可变意图/政策，再恢复 pending actions 和原 ID 对账。恢复动作、成交、费用均持久化。矛盾故障不自动清空，必要时保持隔离及风险占用。
11. 暂停新开仓、日熔断、Admission REJECT 和评分缺失是独立信息，不停止已有持仓退出。已有头寸的 seed/policy 使用库内冻结版本，不读取新文件切换政策。报价缺失时持久化保护义务，但没有有效报价不能制造市价成交。

## 7. 可复制命令及实际输出

在独立项目目录、项目自己的依赖环境中运行，不配置任何真实 Key，不启动旧服务：

```sh
python -m app.offline_paper.cli init --run admission-demo --compact
python -m app.offline_paper.cli run --run admission-demo --scenario admission-rejection --compact

python -m app.offline_paper.cli init --run exit-demo --allow-fixtures --compact
python -m app.offline_paper.cli run --run exit-demo --scenario tp-runner --side LONG --compact
python -m app.offline_paper.cli resume --run exit-demo --compact
python -m app.offline_paper.cli status --run exit-demo --compact
python -m app.offline_paper.cli review --run exit-demo
```

不加 `--compact` 输出完整 JSON，含订单、确认成交、各持仓状态、费用、保护覆盖、待对账动作、账本快照、预留和拒绝诊断。review 直接从确认账本输出 Markdown，不调用大模型。

每个场景使用新的 run ID：

| 场景 | 来源 | 作用 |
| --- | --- | --- |
| admission-rejection | A 普通入口 | 保留缺失证据和 RR/分配冲突，拒绝，无订单 |
| partial-cover | B fixture | `.2 + .3` 分次成交，`.2` 保护扩到 `.5` 后重新确认 |
| partial-cancel | B fixture | `.2` 成交，撤销剩余 `.3`，终态/明细齐备才封口 |
| tp-runner | B fixture | TP1/成本保本/TP2/Runner/跟踪触发最终退出 |
| stop-gap | B fixture | 跨过止损，不利可成交价及成本记账 |
| unknown-reconcile | B fixture | 保护 UNKNOWN，按原 stop ID 查询恢复，不新 ID 下单 |

两类均有 LONG/SHORT 场景验证。手工实际运行 CLI 的 A 示例：`origin=A_NORMAL_ADMISSION`，orders=0、fills=0、cash=500、fees=0；拒绝包括 `NET_RR_INCOMPLETE`、`EXIT_ALLOCATION_MISMATCH`、`STRUCTURE_SUPPORT_INCOMPLETE`、`RUNNER_FULL_POLICY_VALUATION_UNSUPPORTED`、`FUNDING_HORIZON_INCOMPLETE`，以及最终 `NORMAL_ENTRY_FULL_EXIT_CONTRACT_UNSUPPORTED_8A`。

手工实际运行 CLI 的 B LONG tp-runner 示例及随后 resume，两次结果一致：

```json
{
  "orders": 9,
  "confirmed_ledger_fills": 5,
  "cash_usdt": "504.78503125",
  "fees_usdt": "0.05246875",
  "remaining_quantity": "0.00",
  "phase": "CLOSED",
  "pending_actions": 0,
  "pending_reconciliation": 0,
  "normal_entry_complete": false,
  "live_allowed": false
}
```

这是预先构造路径下的模拟现金结果，不是策略盈利证据。CLOSED 后 protection_status=MISSING、覆盖=0 表示没有剩余仓位需要保护，不表示丢失活动仓位保护。

真实子进程故障演示（crash-probe 预期退出码 **91**，然后单独运行 resume）：

```sh
python -m app.offline_paper.cli init --run crash-demo --allow-fixtures --compact
python -m app.offline_paper.cli crash-probe --run crash-demo --point after_broker_execution --side SHORT
python -m app.offline_paper.cli resume --run crash-demo --compact
```

`--point` 还支持 before_intent_commit、after_intent_commit、after_receipt_commit；每次必须新 run。参数/配置/状态错误退出 2；普通准入拒绝是成功处理的业务结果，退出 0 且 JSON 明确 REJECT，不是获准交易。

## 8. 实际测试、故障结果及证据限制

最终 Python 3.12.13、项目现有依赖环境、Node 24。没有升级依赖，未删/跳过/放宽既有业务断言。

| 检查 | 最终实际结果 |
| --- | --- |
| Python 全量 | **1768 passed，0 failed，0 skipped；44.90 秒** |
| 其中本轮新增 | **108 项**，已包含在全量中，不重复累计 |
| A 普通拒绝/绑定/无回退 | 10 项，正常新开仓数为 0 |
| B fixture 成交/退出/预留/恢复 | 72 项；含多空、并发、故障与费用守恒，不作为 A 成功数 |
| 通用配置/类型/隔离/身份边界 | 26 项 |
| 原 Stage 7 审查文件 | 原 18 项包含在全量中，原件 4770 字节、哈希保持一致 |
| Bridge 本机 mock | **27 passed，0 failed，0 skipped** |
| Bridge 类型、vendor、构建 | tsc --noEmit 通过；6 份 vendor 快照匹配；独立构建及导入通过 |
| Python 静态隔离 | ISOLATION_SOURCE_PASS: 69 Python files |
| 原入口只读检查 | config_valid=true、dry_run=true、live_capability=false、network=none |
| Dashboard JS 语法 | node --check 通过 |
| git diff --check | 通过 |

合并独立套件的计数为 **1768 Python + 27 Bridge = 1795**。不再加此前 99/105/107 项开发中复跑或原件 18 项。保留两个既有 Starlette/httpx、anyio 依赖弃用警告，不为消除警告改依赖。

```sh
python -m pytest -q
python -m pytest -q tests/test_offline_paper.py tests/test_offline_recovery.py tests/test_offline_boundaries.py
python scripts/verify_isolation.py
python main.py --check
node --check app/dashboard/assets/dashboard.js
cd bridge
node node_modules/typescript/bin/tsc --noEmit
node --import tsx --test tests/*.test.ts
node scripts/build.mjs
```

实际 Python/CLI/类型/构建在 OS 沙箱下禁止所有网络及原跟单目录读写；Bridge mock 只允许本机回环，外网仍被禁止。独立副本没有 `.env`，原 `main.py --check` 只做配置验证，不启动循环。mock 中的 FILLED/订单 ID 均为合成，不是账户成交。

故障断言结果：

| 故障/竞态 | 断言及结果 |
| --- | --- |
| 意图提交前进程退出 | 预留/请求/订单不存在，恢复不凭空重建意图 |
| 意图提交后、消费前退出 | 原 ID 恢复为一张合成开仓单，风险仍占用 |
| Broker 已接受、回执未消费退出 | 查询原 ID，没有重复订单 |
| 成交已入账、返回确认前退出 | 入场及退出均用真实子进程测试；不重复成交、扣费或平仓 |
| 两线程及两个真实进程抢额度 | 同版本申请只有一个预留成功，另一个 ACCOUNT_VERSION_CHANGED |
| SQLite 注入写入失败 | 整个消费单元回滚，Broker 已有事实和原风险预留保留，Store 锁存失败 |
| 检查点/现金/成交/意图/政策/配置损坏 | 明确隔离、原始记录不删、不释放未知风险 |
| 回执先到、明细后到 | 不按累计量伪造成交；查询补齐后才完成 TP/封口 |
| ProtectionLost UNKNOWN 的数量/None/0 | 高水位保留，较小旧 ACK 不解除义务；矛盾终态不放行全退 |
| 晚到开仓与退出 | 冻结 R 保持；最终毛盈亏=按方向计算的确认金额差，净盈亏再扣实际费用 |
| 同输入重放 | 多空完整场景跨两个独立 run 的结果逐项相同 |

原配置碰撞测试 SHA-256：`757ec0b46741fd22f51c5e181a431bf9196943ce6040ed311c3ce11b9f27c8e3`。原件及全部 Stage 6/R1/R2/R3 测试内容保持不变；新 CLI 同样在建库前拒绝字面点号键碰撞。

未运行/不宣称：真实硬盘掉电、文件系统损坏修复、跨主机事务、长时间高负载压测、实时行情、测试网、真实账户/订单、部署和策略有效性回测。SQLite trigger 写入异常测试不冒充物理磁盘耗尽；os._exit 子进程恢复不冒充断电全部场景验收。

## 9. 剩余限制与 8B 具体前置方案（本轮不实施）

1. **ExitPlan/估值口径需先确认。** 新增单独版本化的执行分配/触发/Runner/成本绑定契约，保留 A 原始结构目标、B 实际退出规则、C 假设下 RR 场景三个对象。30/40/30 不能冒用 50/50 的审批。
2. **Runner 仍不能估成确定 3R。** 下一子阶段先确定哪些可复用 calculate_rr 的静态场景，哪些必须标 UNSUPPORTED/路径依赖；单独输出，不覆盖历史 RR。若需要用新保守口径决定准入，必须先获批其业务语义与政策版本，不能本轮隐式降门槛。
3. **可信计划供应接口。** 定义独立 Plan/EvidenceSnapshot 来源能力、结构证据、方向置信声明、数据版本/TTL、失效检查与费用/资金费/持仓期输入。描述字段或哈希不是可信认证，不能为通过测试补造来源。
4. **真正的普通入场消费。** 只有上一项和第七阶段合同可完整通过后，才能在同一执行事务中调用原 require_paper_admission、重新生成本账本快照、复核绑定及原子预留，产出专用已审批执行意图。当前 Broker 明确不消费普通 ENTRY，fixture 不能转正。
5. **实际成交适配。** 用原 approved-plan 绑定入口创建种子，按实际报价/精度/手续费重新检查审批偏差；保留首笔冻结 R、原止损/目标、已确认现金流。8A 只验证 fixture 偏差取消/保护组件，不声称完整实际审批转换已接通。
6. **独立规则能力验收。** 当前仅有固定合成市场订单/退出、原子固定止损替换；限价撮合、动态整仓、非步长精确尾差、深度/排队、资金费/强平、真实 venue 规则未实现。不得复制这些能力标志到真实交易所。
7. **恢复/事务运行质量。** 后续需容量/保留政策、长时间压力、更多 I/O 故障、受审的损坏修复流程及启动对账可观测性；不清历史解锁，不热迁移旧账本。每次状态/意图/风险变化仍必须保持现有原子边界。
8. **接线验收与授权。** 单独验收“普通新开仓→确认成交→退出→恢复”，再讨论实时只读行情；无论下一步结果如何，都不自动开放 Bridge 私有能力、真实账户或实盘。

只提交源码、测试、文档和明确的合成模板。`offline-runs` 数据库/WAL/运行状态、依赖构建产物和日志不上传。推送本分支后立即暂停，等待 8A 验收；不进入 8B、第九阶段或实盘开发。
