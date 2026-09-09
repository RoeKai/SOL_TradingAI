# STAGE 07 — Configuration Bundle / Cross-Module Contract Validation

**审查状态：第七阶段暂缓验收。第 1–9 节保留原始交付的范围与历史测试记录；本轮修复及最新实测见第 10 节，不将原始提交或历史通过数重复当作修复交付。**

## 1. 交付范围与基线

- 仓库：`RoeKai/SOL_TradingAI`。
- 唯一父提交：`34a6847e76d2fb5fb46ab7e8acab2ddfabb0c8d2`（已验收 Stage 6 R3）。
- 独立分支：`phase-07-config-contracts`；保留历史，不修改/合并 main，不 force push。
- 本阶段交付可运行的配置编译、内容绑定、跨模块检查、离线 CLI 和测试，不是只交设计。
- 不接入现有 Paper/Live，不改四策略、RR/评分算法、Admission 算法、Exit 状态机、Bridge 私有接口封禁或原跟单系统。
- 不实现新订单/账户适配器、数据库事务、出站队列、热更新、启动调度、真实账户读取、真实订单或部署。

**结果不是下单授权，不替代 AdmissionDecision。配置合法、计划一致、元数据齐全、可信来源已接通、执行已接通五件事明确分开。**

## 2. 文件清单与行为影响

新增 15 个文件，修改 6 个；基线其余 122 个文件内容保持不变。

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| app/configuration/__init__.py | 新增 | 离线模块边界说明，无自动初始化 |
| app/configuration/models.py | 新增 | 不可变 bundle、来源、问题、版本、绑定和校验结果 |
| app/configuration/encoding.py | 新增 | 严格 YAML、Decimal/canonical JSON、内容摘要，无 IO |
| app/configuration/registry.py | 新增 | 逐字段主配置目录、原默认值、别名与单位 |
| app/configuration/compiler.py | 新增 | 四份显式文本编译、版本/范围/交集检查、来源记录 |
| app/configuration/inputs.py | 新增 | 显式 PlanInputs、可选原 seed/policy/state 只读输入 |
| app/configuration/contracts.py | 新增 | 完整内容绑定、原 RR/评分/历史审批核对、退出差异与元数据检查 |
| app/configuration/examples.py | 新增 | 最小、故意不补齐资料的合成计划例子 |
| app/configuration/check.py | 新增 | 窄文件边界 CLI，摘要/JSON/非零退出码 |
| configuration.yaml | 新增 | 脱敏的版本、模式、实例与标的声明模板 |
| docs/STAGE_07_PARAMETER_SOURCES.md | 新增 | 先建立的来源表及最终 235 行参数目录 |
| tests/test_config_bundle.py | 新增 | 严格解析、默认、单位、来源、摘要、配置冲突测试 |
| tests/test_config_contracts.py | 新增 | 多空内容替换、旧审批、RR/退出口径、TTL/缺资料测试 |
| tests/test_config_cli_isolation.py | 新增 | CLI、输入路径、凭据隔离、无主流程接线测试 |
| STAGE_07_REPORT.md | 新增 | 本报告 |
| .gitignore | 修改 | 忽略本地 plan.json、bundle/检查导出和私有政策覆盖文件 |
| README.md | 修改 | 第七阶段分支、CLI、结果语义与安全说明 |
| tests/test_dynamic_rr.py | 修改 | 仅精确扩展 4 个新增离线文件的静态引用名单 |
| tests/test_scorecard.py | 修改 | 仅精确扩展 5 个新增离线文件的静态引用名单 |
| tests/test_admission.py | 修改 | 仅精确扩展 5 个新增离线文件的静态引用名单 |
| tests/test_exit_policy.py | 修改 | 仅精确扩展 3 个新增离线文件的静态引用名单 |

四个旧测试文件没有删除、跳过或放宽业务断言；仅按用户允许扩展精确离线模块名单，新 AST 测试反向禁止其他原模块引用 `app.configuration`。Stage 6 R1/R2/R3 所有测试保留。

`config.yaml`、`admission.yaml`、`exit-policy.yaml`、`app/config.py`、整个原运行/策略/风控/Paper/执行/退出模块均未修改。旧 Paper 仍用原 50%/50% 退出逻辑；没有换成新 ExitPolicy。配置目录不影响旧服务正在运行的设置。

## 3. 参数来源与单位

完整逐字段表：[STAGE_07_PARAMETER_SOURCES.md](docs/STAGE_07_PARAMETER_SOURCES.md)。实现前先区分来源权威，随后核对原配置、模型和策略回退；不能将名字相近的字段归成同一值。

| 参数类别 | 默认/单位 | 权威与覆盖方式 |
| --- | --- | --- |
| 主配置风险声明 | 单笔 5 USDT、杠杆 5x、保证金/权益 .2、持仓数 1 | 与同口径 Admission ceiling 取 min，保留两份来源；不是账户已确认限制 |
| 主配置日风控 | 20 USDT、3 次、连续亏损 2 次 | 原账本净现金日盈亏+负浮盈；ENTRY 计数，原口径不改 |
| Admission 日风控 | 20 USDT、3 次、连续亏损 2 次 | 累计负净交易结果+浮亏+预留；次数还涉及待开仓，不能与上行机械合并 |
| 运行时账户硬限制 | 每项默认 None | 需独立可信供应者；配置和 paper.initial_balance 不等于该快照 |
| Admission 其他上限 | 保证金100 USDT、名义额500 USDT、BASE数量100、权益风险比例.01、最小预算.5 USDT | 原政策范围与账户约束保留；不把数量当名义额/保证金 |
| 单笔申请 | risk_budget_usdt、leverage、margin_mode 等显式值 | 不是全局上限，也不是最终可用数量；不从评分猜值 |
| 规则快照 | quantity_step/min/max 是 BASE；price_tick 是 USDT/BASE；min_notional 是 USDT | entry/exit 规则必须匹配声明作用域；能力 flags 不是认证 |
| 原策略分数 | 四策略 min_score 默认60 | 旧启发式信号分数，不是新八维总分，不互相覆盖 |
| 新评分与净 RR 门槛 | 总分60、关键单维55/65/80/75/50/50/80；global净RR1.5 | 评分不改变 RR；七单维+第八综合，沿用原 rubric |
| 等级/市场 | S85/1/A75/.75/B65/.5/C55/.25；trend风险1、range .5，high_volatility/unknown默认禁用 | 有效净RR下限为 global/tier/market 最大值；无高分降RR底线 |
| 退出政策 | 1R/.3、2R/.4、Runner .3；3R激活/1R跟踪；最长3600秒 | 比例精确和1，保留原首笔确认成交冻结R规则，文件不热替换旧仓政策 |

显式同义别名：`drop_pct=2` 是跌幅幅度，与 `drop_threshold_pct=-2` 相容；若填 3 和 -2 则报 `SEMANTIC_ALIAS_CONFLICT`。`btc_max_drop_pct` 同理。两个等价值只记录来源，不最后值覆盖。

同口径单笔风险、杠杆、保证金比例和持仓数是数值上限，采用更小值。BTC 做多禁入是“3m 百分数 <= 负阈值”谓词，两个阈值用 **max** 组合才更严格，例如 -.8 与 -1.2 取 -.8；不会机械 min。策略自己的过滤器仍独立保留。

日口径差异始终报告 `DISTINCT_DAILY_ACCOUNTING_NOT_MERGED`；识别差异不等于已完成账本适配。未引入“净现金日盈亏”和“累计负交易结果”互转公式。

### 类型和安全约束

- `.01 fraction = 1 percent_points`；`.0005 fee_rate = .05 percent_points`；`10 bps = .001 fraction = .1 percent_points`。`unit_convert` 只允许这四类明确的无量纲换算。
- USDT 损失预算、USDT 名义额、USDT 保证金、BASE 数量、价格、杠杆、秒、UTC 秒时间戳不参加比例换算。数量/价格/资金比较均使用 Decimal，不改变原模型定义。
- 金额/比例接受十进制数或明确的十进制字符串；整数必须真正是 int，布尔不作数值，布尔只允许 YAML `true/false`。拒绝 `yes/on/TRUE`、八/十六进制及 YAML 六十进制时分式整数记法。
- 拒绝 NaN/Infinity、非法范围、未知字段（包括空映射）、重复键、merge键、alias/anchor/tag、模板/环境插值、非数字时间戳、过度长度/嵌套/指数。
- 四份文本单份限 1MB，最多30000 YAML tokens，嵌套最多24；数值最多50位有效数、指数绝对值不超过1000。
- 所有原政策仍先经原模型检查，包括比例精确和1、R节点顺序、Runner参数、重试正整数、TTL正数、等级顺序/覆盖集合/滑点区间等。第七阶段额外拒绝原 float 字段不能精确表示的数值；不静默舍入或改成最新版执行。
- 主配置 instance、dry_run、live.enabled 和7项关键风险值必须显式提供；各政策 version/mode、manifest安全/版本作用域必须显式。其他默认值逐项记下实际采用值和 default_applied，缺安全运行时资料仍为 None/INCOMPLETE。
- 主配置必须 `dry_run=true/live.enabled=false/confirmation=''`，不把 dedicated_account_confirmed 设为真；manifest固定 paper_only/live_allowed=false。环境和CLI没有解除开关。
- 本离线主目录支持 SOLUSDT、Binance USDT-M linear USDT，固定SOL/BTC/ETH行情集合；时区当前支持UTC和Asia/Kuala_Lumpur，Bridge声明限制本机8766。超出是组装范围限制，不修改旧运行代码。

## 4. 版本映射与不可变快照

| 定义 | 版本 |
| --- | --- |
| ConfigBundle / BundleManifest.schema_version | config-bundle/v1 |
| 目录/主配置契约 | config-catalog/v1 / main-config-contract/v1 |
| 模板修订标签 | sol-paper-config/v1（只是标签，不能替代摘要） |
| TradeSetup | trade-setup/v1 |
| RRCalculation.calculation_version | linear-usdt-rr/v1 |
| Scorecard / rubric | trade-scorecard/v1 / plan-quality/v1 |
| AdmissionPolicy / AdmissionDecision | paper-risk-admission/v1 / paper-admission/v1 |
| ExitPolicy / ExitState / ExitCheckpoint | paper-exit-policy/v2 / position-exit/v2 / exit-checkpoint/v2 |
| PlanConfigurationBinding | plan-config-binding/v1 |
| ContractValidationResult | config-contract-result/v1 |

版本文字必须与支持目录匹配；原 Admission/Exit 的自由文本版本字段在 bundle 边界收紧为明确已支持版本。不会见到 unknown/v99 就套用最新版。

### 最终数据结构

```text
ConfigBundle (frozen)
  schema_version
  manifest: BundleManifest
    bundle_revision, catalog_version, main_contract_version
    setup_schema_version, rr_calculation_version
    scorecard_schema_version, scoring_rule_version
    admission_policy_version, exit_policy_version
    exit_state_version, exit_checkpoint_version
    mode, instance_id, exchange, contract_type, trade_symbol, quote_currency
    score_context_max_age_seconds, live_allowed=false
  sources: tuple[SourceSnapshot]
    name, raw_text, raw_digest, effective_json, effective_digest
  parameters: tuple[ParameterSource]
    path, value_json, unit, category, authority
    source, source_path, source_digest, default_applied, constraint, override_rule
  comparable_limits: tuple[ComparableLimit]
    semantic, unit, effective_value, sources, rule, authority
  freshness_rules: tuple[FreshnessRule]
    field_path, ttl_seconds, owner, invalidates
  bundle_digest, mode=paper_only, execution_authority=none, live_allowed=false

ConfigCompilation
  parsing, consistency, bundle (may be None), issues

PlanConfigurationBinding (frozen, declared content only)
  schema_version, bundle_digest, instance_id
  setup_digest, rr_digest, scorecard_digest (may be None)
  admission_policy_digest, exit_policy_digest, declared_at
  authority=declared_content_only_not_signature

PlanInputs (each absent field remains absent)
  setup
  rr?, scorecard?, admission?, configuration_binding?
  account?, exchange?, request?, exit_rules?
  bound_position?: {seed, policy, state}

ContractValidationResult (frozen)
  schema_version, bundle_digest?, evaluated_at?
  config_parsing, config_consistency, plan_consistency, runtime_metadata
  runtime_trust=INCOMPLETE, execution=NOT_INTEGRATED
  issues: tuple[ContractIssue]
    reason_code, severity, field_path, source, actual, expected, suggestion
  freshness: tuple[FreshnessObservation]
    field_path, status, observed_at?, ttl_seconds, expires_at?, invalidates
  exit_comparison?: ExitComparison
    reference_entry, initial_stop, reference_initial_r
    tp_trigger_prices, original_targets, proposed_allocations
    runner_activation_price, runner_exit_price=None, full_policy_net_rr=None
    valuation=UNSUPPORTED, interpretation=reference_geometry_not_actual_fills_or_expected_return
  execution_authority=none, admission_replacement=false, live_allowed=false
  existing_position_rule=continue_original_bound_policy_independent_of_new_config
```

对象内部只存冻结记录、tuple与不可变字符串；`main_values` 返回的新字典不影响 bundle。`verify_bundle` 从保留的四份原始文本严格重编译并比较**整个对象**，不是只检查调用者自称的哈希。Decimal context在函数内固定，不污染或依赖外部精度/舍入环境。

内容摘要区分原文与有效配置：注释/空白改变只改变 raw hash 与整体 bundle hash；关键内容不改版本仍改变有效摘要。刻意采用严格的原文绑定，注释变化后也不能原样复用旧 bundle 审批。依赖锁固定解析/模型版本；跨依赖版本应重新核对摘要，不假定格式永远不变。

本次四个脱敏模板的 bundle_digest：

`88bfb9aff742aa99cefd8c8aa2a96ce1eadd8973ffaca1e6e033bc8d27c5c982`

哈希仅证明内容相等，不证明结构目标真实、账户安全、交易所支持动态整仓止损或数据来源可信。

## 5. 计划、RR、评分、审批和退出政策一致性

顺序为 **配置编译 → 显式计划/场景/历史记录校验 → 问题输出**；没有执行箭头。

1. 验证完整 TradeSetup（包括方向、标的、入场区间/方式、止损、真实目标/比例/证据引用和全部其他内容），不只比 setup_id/plan_version。
2. 复用 `calculate_rr(setup, quantity=原场景数量)` 比对完整原 RR。实际审批数量的 `final_rr` 再用同一函数核对，不用最远TP或毛RR顶替。Stage3的float数量接口不能无损表达某个Decimal数量时返回UNSUPPORTED，不默默取整。
3. 用原 `score_trade_setup` 和原ScoringContext重算核对描述，不改变八维算法；策略说明变化可能不改变RR，但完整计划hash仍须改变，旧审批不可复用。
4. 检查配置绑定中完整bundle/setup/RR/card/两政策内容摘要、实例和时序；声明必须晚于输入形成、早于或等于审批。缺少历史绑定是INCOMPLETE，不自动追认；自行填哈希/回填时间不构成可信签发。
5. 检查原AdmissionDecision的自身内容摘要、完整绑定、plan_terms、政策版本和精确最终数量RR；全部历史输入齐全时，在原 evaluated_at 调用原 `admit_trade` 比较整个结果。这是历史核对，不是新的交易准入。
6. 额外比对历史审批与bundle更严格的主配置风险/杠杆/保证金/BTC约束；超出只报冲突，不缩仓、更改审批或调整杠杆。主策略交易方式与原规则查询方式不匹配也报冲突。
7. entry/exit规则分别显式提供，核对标的、合约、订单类型、BASE数量上下限/步长、price tick与USDT最小名义额。例外/原子能力不从普通数值规则推断；验证标签仍不是认证。

### A / B / C 三种口径

| 概念 | 保留内容 | 禁止混用 |
| --- | --- | --- |
| A 原市场结构 | TradeSetup原目标、证据与分配 | 不能把1R/2R触发位改写成原结构目标 |
| B 拟用退出政策 | TP1/TP2比例、成本保护、Runner/时间/趋势退出 | 不能静默把50/50换成30/40/30 |
| C 静态RR场景 | 第三阶段按显式入场/目标/数量/成本算出的三种入场场景 | 不是整个B的保证收益、概率期望或路径模拟 |

配置比例精确和1；计划比例额外用Decimal严格核对，不归一化。前两档参考触发价格分别为 `entry ± initial_distance × tp_r`，LONG/SHORT镜像；与原结构目标不同则报差异。负/零触发价报不可行。已有止损移动声明如果与TP1保本方式不符报冲突；无法表示的固定价/缓冲/后续移动路径返回UNSUPPORTED，不替换原声明。

第三档不是固定TP：即使原目标和激活价都恰好为3R，也始终保留 `runner_exit_price=null/full_policy_net_rr=null`。当前Stage6政策要求三个正比例、必有Runner，第三阶段静态目标模型无法估整个退出路径。因此本阶段**没有一个完整Runner政策被冒称为已估值通过**；可以配置合法，但计划层仍为UNSUPPORTED或INCOMPLETE。

参考入场价/R仅作规划比较；未来首笔确认成交另冻结Stage6的真实R。成交适配仍需重新核对实际数量、成交价、原止损方向、目标/成本假设和场景适用范围。不得回写原TradeSetup、历史RR、已实现盈亏或已冻结R。

### 成本兼容

逐条腿/单位比较：入场费率与滑点不等于退出费率与滑点，取各预算要求的最大下界，不能重复加总同一成本。计划预算可保守高于下界，但不得越过原Admission滑点上界；退出成本下界高于准入上界时配置层直接冲突。

资金费沿用第三阶段**整单指定数量、有符号USDT总额、按比例分配的静态假设**。None不是0，不能当费率或每单位成本使用。时长必须覆盖拟用Exit最长时长且不超Admission最大假设时长。即使静态预算合法，分批/持仓路径资金费仍未完整建模，明确UNSUPPORTED，不说已经解决。

## 6. 新鲜度、运行时证据和旧仓保护

| 项 | 默认秒数 | 失效影响 |
| --- | --- | --- |
| 原主流程行情 / Admission行情 | 15 / 15 | 原就绪/新计划与审批，保持不同所有者 |
| 结构原记录 / 结构确认 | 300 / 300 | 结构适用性与审批 |
| 失效条件确认 | 15 | 当前计划/审批 |
| 账户风险快照 | 5 | 审批、风险预留；日界另显式核对 |
| 交易所规则 | 3600 | 数量与规则适用性 |
| 成本假设 | 300 | 净RR场景的现实适用性 |
| 评分内部数据窗口 / 评分结果 | 300 / 30 | 原描述有效性/审批引用 |
| AdmissionDecision | 5及其更早的原valid_until | 资格消费截止 |
| 原Exit市场 / Runner结构输入 | 5 / 30 | 原绑定Exit策略检查，非新开仓就绪 |

新检查使用半开有效窗口：`observed_at <= now < observed_at + TTL`；计划/审批必须 `now < valid_until`。过期/未来时间为FAIL，缺失/未验证声明为INCOMPLETE。历史原函数核对仍按其原时间和原算法，不改已验收代码。TTL到界时不延长原审批。

第五阶段data_coverage的非structure项沿用其market TTL（包括funding_rate**覆盖声明**）；CostAssumptions里的费率/滑点/资金费预算用独立300秒。没有偷偷改动旧算法以伪造更细粒度数据源；后续供应者必须满足这两类不同契约。

即使元数据来源名、时间、verified和内容hash均符合，`runtime_trust`仍是INCOMPLETE。没有供应者认证、实际交易所能力验证、可信账户/结构数据接入。合成单元测试可以构造完整声明，不表示这些真实组件已连接；公开CLI例子故意留空评分、账户、确认和资金费，不补造安全性。

已有持仓只核对其原seed/policy绑定，不用当前新bundle替换旧policy；无任何状态写入、actions字段或取消保护指令。配置错误最多阻止新组装。原检查点完整重放仍是Stage6恢复权威；没有自动迁移/清空旧账本，旧v2语义不一致继续拒绝。

## 7. 离线运行和结果

安装锁定依赖后，在本模块根目录运行（无需.env，无需main.py）：

```sh
python -m app.configuration.check
python -m app.configuration.check --example config-valid --json --parameters
python -m app.configuration.check --example synonym-conflict --json
python -m app.configuration.check --example allocation-mismatch --at 1800000000 --json
python -m app.configuration.check --example runner-unmodeled --at 1800000000 --json
```

| 实际示例 | 配置解析 | 配置一致 | 计划一致 | 元数据 / 可信运行时 | 退出码 |
| --- | --- | --- | --- | --- | --- |
| config-valid | PASS | PASS（有日口径警告） | NOT_EVALUATED | NOT_EVALUATED / INCOMPLETE | 0 |
| synonym-conflict | FAIL | NOT_EVALUATED | NOT_EVALUATED | NOT_EVALUATED / INCOMPLETE | 2 |
| allocation-mismatch | PASS | PASS | FAIL，EXIT_ALLOCATION_MISMATCH | INCOMPLETE / INCOMPLETE | 3 |
| runner-unmodeled | PASS | PASS | UNSUPPORTED，RUNNER_FULL_POLICY_VALUATION_UNSUPPORTED | INCOMPLETE / INCOMPLETE | 3 |

有效配置摘要实际输出：

```text
Config parse: PASS; config consistency: PASS
Plan: NOT_EVALUATED; runtime metadata: NOT_EVALUATED; trusted runtime: INCOMPLETE
Bundle: 88bfb9aff742aa99cefd8c8aa2a96ce1eadd8973ffaca1e6e033bc8d27c5c982
NOT_INTEGRATED; execution_authority=none; live_allowed=false; no account or order access
```

Runner示例JSON节选（其余问题/资料缺失也会列出）：

```json
{
  "config_parsing": "PASS",
  "config_consistency": "PASS",
  "plan_consistency": "UNSUPPORTED",
  "runtime_metadata": "INCOMPLETE",
  "runtime_trust": "INCOMPLETE",
  "execution": "NOT_INTEGRATED",
  "execution_authority": "none",
  "live_allowed": false,
  "exit_comparison": {
    "reference_entry": "100.0",
    "reference_initial_r": "5.0",
    "runner_activation_price": "115.0",
    "runner_exit_price": null,
    "full_policy_net_rr": null,
    "valuation": "UNSUPPORTED"
  }
}
```

CLI仅读四份指定模块内文件（或`examples/configuration/<case>/`下对应固定文件名）及现有`isolation-policy.json`标记；`--plan`只能读相同边界内的`plan.json`。没有root参数、父目录扫描、.env/宿主密钥/旧配置回退，也不启动任何交易、Paper、Bridge、Dashboard、Telegram进程。路径与类型错误返回结构化问题、退出码2，值和宿主路径隐去。

核心函数只接受显式对象/文本；没有“找不到文件就用主配置”逻辑。`--at`不填且需要计划校验时返回INCOMPLETE；配置-only无需时钟。机器结果可JSON序列化，无需持久化服务。

## 8. 实际测试与隔离证据

最终本地执行环境：Python3.12、锁定项目依赖、Node24。没有为测试放开真实交易网络。

| 检查 | 实际结果 |
| --- | --- |
| Stage7新增合成用例 | 269通过 |
| 全量Python（含原1279项与全部Stage6/R1/R2/R3） | 1548通过、0失败、0跳过；22.43秒 |
| Bridge原mock回归 | 27通过、0失败、0跳过 |
| Python静态隔离 | ISOLATION_SOURCE_PASS: 60 Python files |
| 原main.py --check | config_valid=true, dry_run=true, live_capability=false, network=none |
| Bridge类型检查 | tsc --noEmit 通过 |
| Bridge构建/独立导入 | 6个vendor快照、82个输入核对通过；standalone import通过 |
| Dashboard JS语法 | node --check 通过 |

**合计1575项Python+Bridge测试通过。** 保留两个既有依赖弃用警告（Starlette/httpx、anyio BlockingPortal），未为消除警告升级或改动已验收依赖。

发布审计逐文件核对GitHub基线128个blob和本次143个源文件：15新增、6修改、其余122个字节不变；只允许21个已列差异。公开包凭据/账户/运行数据扫描未发现匹配项；52类私有或运行文件路径被gitignore拦截，源码/文档/模板白名单可提交。原跟单工作树及其模块镜像未写入；实现和测试均在独立Git副本完成。

新增覆盖：确定性/同版本内容变化；默认与235行来源；同义冲突与不同日口径；比例/百分数/bps；金额/数量/名义额/保证金/杠杆分离；类型/非法范围/重复YAML/未知版本/非有限数；无环境污染/旧配置回退；多TTL与确认资料；同ID替换止损/目标/比例/成本/方向/策略/审批；保守成本与冲突成本；RR与Exit比例/触发差异；Runner激活非成交；旧审批换bundle；原退出状态不变；裸Signal不能成为配置执行契约；Live封禁与CLI路径/符号链接/硬链接。

复验命令：

```sh
python -m pytest -q tests/test_config_bundle.py tests/test_config_contracts.py tests/test_config_cli_isolation.py
python -m pytest -q
python scripts/verify_isolation.py
python main.py --check
node --check app/dashboard/assets/dashboard.js
cd bridge
node node_modules/typescript/bin/tsc --noEmit
node --import tsx --test tests/*.test.ts
node scripts/build.mjs
```

Python/CLI/类型/构建在操作系统沙箱下禁止全部网络及原跟单目录读写；Bridge测试仅允许本机HTTP mock回环，外网和原跟单目录仍禁止。mock日志中的FILLED是合成应答，不是实盘成交。没有通过访问真实账户验证“零订单”。新纯函数还用IO/时钟/账户访问毒丸与AST静态白名单测试其副作用边界。

未运行：真实行情/真实账户/真实订单/服务器部署、完整新Paper主链路或生产执行验证；这些明确不在本阶段范围，不能写成通过。

## 9. 第八阶段前置清单与尚未解决项

1. 可信TradeSetup/市场结构目标/方向置信声明供应者；来源认证、证据版本和失效条件事实，不能靠补满测试字段。
2. 可信独立账户风险快照与相同风险日边界；主流程净现金损失与Admission累计损失、旧ENTRY计数与新预留口径适配。
3. 独立交易所规则与合成成交适配；精度、最小额、reduce-only例外、原子换保护/动态整仓能力需验证，不能靠flags。
4. 参考计划与首笔实际成交冻结R的适配/偏差复核；晚到成交和剩余成本继续按Stage6原契约。
5. 静态结构RR与30/40/Runner实际退出的未建模项：路径、保本/移动止损、时间/趋势退出、费用/资金费与持仓时长。当前只识别并阻断不兼容，不是已经有完整退出收益预测器。
6. 配置摘要在计划创建/审批签发/实际消费中的可信绑定；不可倒签旧审批、不可将新政策附会给旧仓。
7. 执行锁内新鲜度及所有约束复核、原子风险/名额预留，防检查后状态变化。
8. 事务账本与事件持久化、幂等消费、原动作ID/成交ID确认、崩溃恢复与出站意图生命周期。
9. 启动对账与旧检查点完整重放；UNKNOWN不盲目重发，矛盾资料不清库。已有仓保护始终高于新增信号/新配置失败。
10. 新Paper全链路的专门集成验收与故障/竞态压力测试；不得直接将此校验结果当Execution许可。

上述事项未接入。没有改变任何已验收交易语义来让配置“看起来能用”。本阶段只上传源码、测试、文档、脱敏模板；不上传.env、真实配置/账户、账本、日志或运行快照。

**推送第七阶段独立分支后暂停，等待验收；不进入第八阶段，不合并main，实盘继续硬关闭。**

## 10. 审查修订 R1：嵌套键与字面点号键碰撞

### 10.1 基线、复现与证据范围

- 修复父提交：`3960dda922f7ddc31a53babf1b5f9111f6f7f56e`，分支 `phase-07-config-contracts`。
- 只追加修复提交，保留全部历史，不 force push，不修改/合并 main，不进入第八阶段。
- 本轮可访问文件中未找到审查方提到的 `test_stage07_semantic_key_collisions.py`，已请求重新附上。**没有原样运行该附件，也不声称重跑了附件的断言。**
- 在修改生产实现前，自行建立 `tests/test_config_semantic_collisions.py` 的首批 27 项合成回归：9 种输入各测两种根键顺序，共 18 项 `compile_bundle`；另有 9 项通过实际文件读取、argparse 和编译器的离线 CLI 子进程测试。
- 在上述原始基线中实际结果：**27 failed，3.24 秒**。18 项编译测试错误返回 `parsing=PASS`；9 项 CLI 错误返回退出码 0 并生成非空 bundle，均与拒绝断言不符。后续修复没有删除或放宽这些断言，也没有修改任何原有测试文件。

最小复现（在正常模板的 risk 节内将原值改成 true，再在根部添加字面点号键）：

```yaml
risk:
  max_loss_per_trade: true
  # 其余必填风险字段保持正常模板值
risk.max_loss_per_trade: 5
```

其他复现包含：5 与 4 冲突；非法字符串/0 被 5 遮盖；`live.enabled=true` 被字面键 false 遮盖；杠杆99被5遮盖；策略节内局部点号键；根部点号节；非法旧字段别名被另一种写法遮盖。所有配置均为合成文本，错误接受 `live.enabled` 的测试未构建任何私有客户端或执行订单，不能混同于发生过实盘访问。

### 10.2 根因与修复

旧 `prepare_main.keys()` 使用字符串拼接核对字段路径，没有区分一个原始键段与多个嵌套键段；`flatten()` 再将二者编码为同一个路径。`dict(flatten(raw))` 因而静默保留后一个值，真正的原值在类型/安全约束检查前已经丢失。flatten 还会排序键，所以交换 YAML 的填写顺序并不能消除漏洞。旧别名来源代码第二次构造该字典，也沿用了歧义。

修复选择用户允许的**仅支持嵌套 YAML**方案，不增加新的覆盖语言：

1. `encoding.validate_mapping_key()` 限定原始键为非空、不含点号的字符串，merge 键不允许。`_Loader.construct_mapping()` 在构建原始映射时执行检查，早于 flatten、默认值、政策模型和来源追踪。
2. 字面点号键统一报 `LITERAL_DOTTED_KEY_FORBIDDEN`；相同值、只写点号键、引号/转义写法、列表中的映射同样拒绝。不会先对碰撞值做归一化或选更严格值，因为这会掩盖非法原始输入。
3. `prepare_main()` 与 `flatten()` 共享键段验证，为直接提供 mapping 的内部调用补同一防线。后续 `dict(flatten(...))` 不会收到由含点号/空键段造成的歧义路径。
4. 来源追踪使用一次已校验的 `supplied_paths` 集合，不再重建原始路径字典。正式嵌套别名的符号转换、等值双来源记录和 `SEMANTIC_ALIAS_CONFLICT` 保持不变。
5. 内部生成的点号来源路径、版本文本、URL/普通字符串值不受限制；本轮限制的是原始字段键，不是所有含点号字符串。

实际入口错误契约不变：`compile_bundle` 返回 `CONFIG_INPUT_INVALID`，source/field_path 标明错误配置角色，suggestion 包含上述明确语法原因，实际值隐去；`parsing=FAIL`、`consistency=NOT_EVALUATED`、`bundle=None`。不返回可用快照，也不触发任何执行行为。

### 10.3 本轮文件清单与行为影响

| 文件 | 本轮变化 |
| --- | --- |
| app/configuration/encoding.py | 新增共享原始键段校验；YAML 构建入口及 flatten 调用 |
| app/configuration/registry.py | 主配置 mapping 入口同规则；复用已校验原始路径集记录别名来源 |
| tests/test_config_semantic_collisions.py | 新增 94 项回归，含首批 27 项复现及真实离线 CLI 集成 |
| README.md | 说明嵌套语法、拒绝点号键、别名与 CLI 失败结果 |
| docs/STAGE_07_PARAMETER_SOURCES.md | 明确表中语义路径并非 YAML 点号写法，保留来源和覆盖规则 |
| STAGE_07_REPORT.md | 保留历史报告，追加本修订记录与未运行项 |

共 **1 新增、5 修改**；父提交其余 **138 个文件字节不变**。所有既有测试文件/断言、参数模板、导入白名单、TradeSetup、RR、评分、Admission、Exit/R1/R2/R3、Paper/Live 主流程和 Bridge 均不变。

正常模板的 bundle 摘要保持：`88bfb9aff742aa99cefd8c8aa2a96ce1eadd8973ffaca1e6e033bc8d27c5c982`。回归同时核对其 235 项有效参数、来源、各配置内容摘要和 CLI JSON 完全一致。没有改变参数默认值、政策版本或交易业务规则。过去错误接受的点号键配置现在拒绝，这是本次唯一有意的解析行为变化；原始数据须改写为合法嵌套结构后重新校验，不能以旧摘要/版本号绕过 `verify_bundle` 的原文重新编译。

### 10.4 测试设计与实际结果

新增覆盖：冲突及非法原值、两个键顺序、隐藏实盘开关/杠杆、嵌套/根部点号节、非法别名；拒绝发生在默认/标量校验之前；直接 mapping 不能绕过；等值/单独/引号/转义/列表键；四份配置共享解析入口及错误值脱敏；全部 5 个正式别名的独立/等值/冲突语义和精确来源；正常模板原摘要；字符串值/内部来源路径中的点号；空键/非字符串/merge 段。

离线 CLI 集成使用临时的独立源码副本和四份合成配置，保留实际安全文件读取、解析、编译及返回码，不 mock `parse_text`、`compile_bundle` 或 CLI 结果，不修改仓库中的模板。

| 检查 | 本轮实际结果 |
| --- | --- |
| 修复前原始基线的首批合成复现 | 27 failed（预期复现漏洞），3.24 秒 |
| 修复后新增专项回归 | 94 passed，8.23 秒 |
| Python 全量，保留全部旧回归 | **1642 passed，0 failed，0 skipped；40.10 秒** |
| Bridge 本机 mock 回归 | **27 passed，0 failed，0 skipped** |
| Python 静态隔离 | `ISOLATION_SOURCE_PASS: 60 Python files` |
| 原 `main.py --check` | `config_valid=true, dry_run=true, live_capability=false, network=none` |
| Bridge 类型检查 | `tsc --noEmit` 通过 |
| Bridge 构建与独立导入 | 6 个 vendor 快照、82 个输入核对通过；standalone import 通过 |
| Dashboard JS 语法 | `node --check` 通过 |

合计 **1669 项 Python + Bridge 测试通过**。Python 保留两个既有依赖弃用警告（Starlette/httpx、anyio BlockingPortal），未改依赖以消除警告。补测试时，通用 dumper 对整值 Decimal 生成了已禁止的 `!!float` 标签；测试夹具改用既有契约允许的十进制字符串，未放开生产标签校验或修改通过/拒绝断言。

复验命令（在独立项目目录、现有依赖环境中；不启动交易服务）：

```sh
python -m pytest -q tests/test_config_semantic_collisions.py
python -m pytest -q
python scripts/verify_isolation.py
python main.py --check
python -m app.configuration.check --json --parameters
node --check app/dashboard/assets/dashboard.js
cd bridge
node node_modules/typescript/bin/tsc --noEmit
node --import tsx --test tests/*.test.ts
node scripts/build.mjs
```

对上述冲突配置，真实 CLI 集成已验证返回码 **2**，机器结果关键字段为：

```json
{
  "bundle": null,
  "validation": {
    "config_parsing": "FAIL",
    "config_consistency": "NOT_EVALUATED",
    "execution": "NOT_INTEGRATED",
    "execution_authority": "none",
    "live_allowed": false
  }
}
```

此处为结果字段摘录；完整输出另含 `CONFIG_INPUT_INVALID` 和 `LITERAL_DOTTED_KEY_FORBIDDEN` 提示，不回显原始配置值。

### 10.5 隔离、未运行项与暂停

本轮实现/测试在独立 Git 副本进行，没有写入原跟单目录或其模块镜像。Python/CLI/类型/构建在 OS 沙箱中禁止所有网络和原系统目录访问；Bridge mock 仅允许本机回环，外网仍被禁止。没有通过账户查询来证明“无订单”。

未运行：当前未提供可访问原件的 `test_stage07_semantic_key_collisions.py`；真实账户、真实行情/订单、部署、Paper/Live 全链路接入。这些不能冒充通过。收到审查附件后仍需原样补跑，当前 94 项自行编写用例不代表已覆盖该附件所有断言。

第 9 节前置清单及 RR/Runner 未建模项仍未解决；本轮没有扩展准入/执行/事务/调度能力。实盘继续硬关闭；只上传脱敏源码、测试和文档。**追加推送后立即暂停，等待第七阶段复验，不进入第八阶段。**
