# 第二阶段：TradeSetup 数据契约与兼容方案

范围：只定义数据、校验数据形状、序列化和单向兼容适配。本阶段不计算动态 RR、八维分数、等级、仓位或风控决策，不处理行情、不触发止盈止损、不接入 PaperRuntime，不开启实盘。

## 1. 文件与行为边界

新增：

- `app/setups/__init__.py`：只导出 `TradeSetup` 和 `adapt_legacy_signal`。
- `app/setups/models.py`：Pydantic v2 嵌套不可变数据结构和结构一致性校验。
- `app/setups/adapters.py`：显式、单向、旁路的旧信号描述转换。
- `tests/test_trade_setups.py`：数据契约、兼容与无接入测试。
- 本文及 `docs/evidence/STAGE2-20260909.json`：规则与实际验收摘要。

现有文件仅 `README.md` 增加阶段说明。`app/models.py`、四策略、runtime、风控、执行锁、PaperBroker、独立账本、配置、下单桥和 vendor 均不修改。不刷新旧系统源码快照、不迁移账本、不导入旧数据库、不启动任何账户查询或下单客户端。

## 2. 顶层最终字段

`TradeSetup` 是描述，不是订单请求，也不是风控审批凭证。未提供的估计默认 `None`，不默认 0、满分、安全或无限有效。

| 字段 | 类型 / 默认 | 定义 |
| --- | --- | --- |
| `schema_version` | 固定 `trade-setup/v1` | 数据格式版本；不接受未知版本 |
| `plan_version` | 非空字符串，必填 | 生成者/计划规则版本；适配器固定 `legacy-signal-adapter/v1` |
| `setup_id` | 非空字符串，必填 | 计划身份；不等于下单授权 |
| `origin` | `native_plan` / `legacy_signal` | 描述来源，不代表真实性或质量 |
| `symbol` | 大写字母/数字，必填 | 标的；Schema 能记录标的不代表运行时允许交易该标的 |
| `strategy_name` | 非空字符串，必填 | 原策略名称，不自动更名 |
| `strategy_type` | 四种现有策略、`custom` 或 `legacy_unknown` | 策略类型；未知旧名称不猜测归类 |
| `side` | `LONG` / `SHORT` | 持仓方向，不混用 BUY/SELL |
| `structure_evidence` | `Evidence[]`，默认空 | 结构依据、来源、时间、核验状态 |
| `entry` | `EntryPlan`，必填 | 入场方式、参考价、价格区间及依据 |
| `invalidation_conditions` | `InvalidationCondition[]`，默认空 | 价格/结构/时间/数据/人工失效条件，当前不执行 |
| `initial_stop` | `InitialStop`，必填 | 初始止损价与依据；不是随行情移动的当前止损 |
| `targets` | `TargetLevel[]`，默认空 | 提供的多个目标、比例、每档 RR；空表示尚未提供，不是允许无止盈下单 |
| `reward_risk` | `RewardRiskEstimates` | 整单毛/净 RR 与计算方法说明，未提供为未知 |
| `market_state` | `MarketSnapshot` | 提供的市场状态；默认 missing/unknown |
| `score` | `ScoreBreakdown` | 八维字段、分数及旧分数；本阶段没有权重/计算/分级算法 |
| `grade` | `S` / `A` / `B` / `C` / `None` | 未来准入与仓位政策等级；不决定止损或目标 |
| `grade_interpretation` | 固定 `policy_tier_not_win_probability` | 等级不是胜率 |
| `confidence` | `ConfidenceEstimate` | 证据置信度描述，不是胜率 |
| `position_limit_advice` | `PositionLimitAdvice` | 仓位上限建议，不是最终数量 |
| `risk_budget` | `RiskBudget` | 单笔/日剩余风险预算描述，不读取账户 |
| `cost_assumptions` | `CostAssumptions` | 手续费、滑点、资金费、持仓期假设 |
| `data_coverage` | `DataCoverage` | 输入状态、覆盖度和缺失项；默认全部缺失 |
| `stop_movement` | `StopMovementPlan` | 移动止损规则描述；默认无规则，没有执行器 |
| `created_at` | Unix 秒，必填 | 描述创建时间，由调用方提供；不自行读时钟 |
| `data_as_of` | Unix 秒 / `None` | 该描述采用的数据截止时间；未知不猜测 |
| `valid_until` | Unix 秒 / `None` | 显式有效期；`None` 表示未知，不表示永久有效 |
| `rejection_reasons` | `RejectionReason[]`，默认空 | 已提供的拒绝理由；空不表示通过 |
| `compatibility_notes` | 字符串列表 | 映射损失、未知项与来源解释 |
| `admission_status` | 固定 `not_evaluated` | Schema 合法不等于风控准入 |
| `execution_authority` | 固定 `none` | 不能由调用方声明 approved/live 等授权 |

## 3. 嵌套字段与单位

列表在内存中使用 tuple，嵌套记录均 frozen；标准 JSON 输出为数组。价格、比例、分数、时间禁止布尔冒充数字、字符串冒充数字、NaN/Infinity。类型校验允许普通整数用于浮点字段。所有时间都是 UTC Unix **秒**，不是毫秒。

| 结构 | 字段与含义 |
| --- | --- |
| `Evidence` | `evidence_id`、`kind`、`description`、`status`、`source`、`timeframe`、`observed_at`、可选 `price`。kind 包括支撑/阻力、摆动高低点、区间边界、指标、市场事件、旧来源不明、其他；available 必须有来源和时间，但本阶段不鉴定证据真实性 |
| `EntryPlan` | `order_type` MARKET/LIMIT；`reference_price`、`lower_price`、`upper_price`；`basis` structure_zone/legacy_point/unverified；`evidence_ids`。参考价必须在区间内；结构区间必须引用证据 |
| `InvalidationCondition` | `condition_id`、`kind` price/structure/time/data/operator、`description`、`operator` lt/lte/gt/gte/eq、可选 `price` / `at`、`evidence_ids`。价格条件必须有价格与比较符；时间条件必须有时间 |
| `InitialStop` | `price`、`basis`、`evidence_ids`。多头在整个入场区间下方，空头在区间上方；只校验几何，不计算合理止损 |
| `TargetLevel` | `target_id`、`price`、`fraction`、`basis`、`kind` structure/legacy_unspecified/unverified、`evidence_ids`、`theoretical_rr`、`net_rr`。目标按盈利方向有序，比例相对**初始总仓位**，总和为 1；标为 structure 必须有证据引用 |
| `RewardRiskEstimates` | `gross_rr`、`net_rr`、`calculation_method`、固定 `verification=not_calculated_or_verified_in_stage2`。毛 RR 非负，净 RR 可为负；所有值都是调用方提供的未核验估计，不会反算、调整或补全 |
| `MarketSnapshot` | `status`、`regime` unknown/trend/range/high_volatility、`observed_at`、`source`、`reference_price`、`bid`、`ask`；`return_1m_pct/3m_pct/5m_pct/15m_pct`、`btc_return_3m_pct`、`eth_return_3m_pct`、`volume_ratio`、`volatility_pct`、`buy_pressure`。收益/波动使用百分数点（2=2%），买压使用 0–1 |
| `ScoreComponent` | `dimension`、`status`、`points`、`max_points`、`evidence_ids`、`explanation`。点数/上限范围 0–100，未知为 None；不能给 missing/stale/not_applicable 项填分；unverified 可存未核验输入但不产生授权 |
| `ScoreBreakdown` | `total`、`components`、`method_version`、`legacy_score`、固定 `interpretation=heuristic_not_win_probability`。八维固定为 trend/volume/volatility/market_resonance/structure/funding_rate/liquidation_zones/news；必须各出现一次。默认每维点数和权重未知，不求和、不定等级、不按可用项拉满到 100 |
| `ConfidenceEstimate` | `value` 0–1 或 None、`basis`、固定 `interpretation=evidence_confidence_not_win_probability`。提供数值必须说明依据；不是经回测校准的获胜概率 |
| `PositionLimitAdvice` | `max_quantity`、`max_notional_usdt`、`max_margin_usdt`、`max_margin_fraction_of_equity`、`leverage_cap`、`basis`、固定 `authority=advice_only_not_order_quantity`。未知为 None，建议可以为 0。保证金比例为 0–1，不能把 20 当作 20%；Schema 不执行账户上限检查 |
| `RiskBudget` | `max_loss_usdt`、`remaining_daily_loss_usdt`、`max_risk_fraction_of_equity`、`basis`。未知为 None，预算 0 可用于记录不可交易候选；不分配资金 |
| `CostAssumptions` | `entry_fee_rate`、`exit_fee_rate`（0–1）；`entry_slippage_bps`、`exit_slippage_bps`（1 bp=0.01%）；`funding_cost_usdt`（正值成本、负值收入）；`assumed_holding_seconds`、`source`、`observed_at`。未提供资金费不视为 0；不从当前 config 自动读取成本 |
| `DataAvailability` | `name`、`status` available/missing/stale/unverified/not_applicable、`required`、`source`、`observed_at`、`note`。available 必须有来源和时间，但不自行判断新鲜度；required 不能标 not_applicable |
| `DataCoverage` | `items`、`coverage_ratio`、`missing_items`。覆盖基线含 market_price/trend/volume/volatility/btc_reference/eth_reference/structure/funding_rate/liquidation_zones/news；允许额外命名输入但不能重复。覆盖度是 available 项数/适用项数，不是加权评分或安全程度；全部不适用时为 None。missing_items 包含 missing/stale/unverified，具体状态看 items。构造器核验摘要一致，`from_items` 仅整理元数据 |
| `StopMoveRule` | `after_target_id`、`move_to` entry/cost_adjusted_entry/fixed_price/trailing、`fixed_price`、`trailing_distance_r`、`buffer_bps`、`requires_confirmed_fill` 固定为 true。目标必须存在，声明固定价/跟踪时必须给出所需参数；本阶段不监听触发、不移动止损、不计算成本保本价 |
| `StopMovementPlan` | `rules`、`allow_widening` 只能 false。默认无规则，不自动插入 1R/2R/3R 或 30/40/30 计划 |
| `RejectionReason` | `code`、`message`、`source`、`recorded_at`。仅存调用方提供的原因，不调用风控来生成它 |

证据引用、目标 ID、失效条件 ID 和移动止损触发目标不能重复或悬空。证据时间不能晚于明确的数据截止时间（未给截止时间时不晚于创建时间）。这里只校验相对时序，**不会把一份旧描述自动判为新鲜、有效或允许执行**。

## 4. 不绑定评分和 RR

- 不提供 `RR = f(score)`、`target = entry + grade_multiple × risk` 等算法。
- 改动 score、grade、confidence、仓位建议不会改变 stop、target 或 RR。
- 低 RR、低分、成本后负 RR、缺失成本都可以作为描述被保存；Schema 不承担第 5 阶段准入职责，记录成功不代表允许开仓。
- 即使显式填入 S、95 分、完整覆盖度和正 RR，`execution_authority` 仍是 none。
- 未来第 3 阶段负责真实结构目标和成本计算，第 4/5 阶段负责评分政策与准入；必须分别验收。本阶段不为“看起来完整”补造这些数据。

## 5. 单向兼容关系

```text
现有策略 → 原 Signal → 原 RiskEngine / 执行锁 / PaperBroker / 账本（完全不变）
               │
               └─ 显式调用 adapt_legacy_signal（未接入主流程）
                    → TradeSetup 描述（无准入、无执行转换）
```

映射规则：

1. 原 signal.id、策略名、方向、价格、止损、目标价和比例原样复制；Signal 本身不修改，目标列表不共享可变引用。
2. 旧入场只有单点价，因此 lower/reference/upper 相等并标记 legacy_point，不虚构区间。
3. 旧 reason 只作为 legacy_unspecified/unverified 说明，不认证其结构真实性。
4. 旧目标一律标 legacy_unspecified，不认证为结构目标，也不根据价格反算 RR。
5. 旧 score 只进入 `score.legacy_score`；新 `score.total`、八维点数、grade、confidence、costs、RR 仍未知。
6. Signal 没有市场数据时间/有效期/失效规则，因此 data_as_of、valid_until 保持 None；不把 created_at 冒充行情时间，不复制执行引擎的 30 秒期限。
7. 未知策略类型标 legacy_unknown；旧名称原样保留。
8. 非标准 Signal 类型、带额外未知目标字段、结构不合法的数据明确报错，不静默丢字段。因适配器未接入运行路径，不会改变原 Signal 的处理行为。
9. **没有 `TradeSetup → Signal` 转换**。反向转换现在会丢掉未来准入信息，必须等第 5/8 阶段设计明确审批凭证后再单独实现；不能直接把描述交给 ExecutionEngine。
10. 无账本写入和迁移。未来序列化到独立存储也必须保留 schema_version 和计划版本，不覆盖在途旧仓位的保护计划。

## 6. 独立查看与测试

安装环境沿用项目 README。以下示例只处理内存中的合成数据，不启动 runtime、不读 .env、不访问账户、不创建订单：

```python
from app.models import Signal
from app.setups import TradeSetup, adapt_legacy_signal

old = Signal('sol-example', 'panic_rebound', 'SOLUSDT', 'LONG', 100, 99,
             [{'price': 101, 'fraction': .5}, {'price': 102, 'fraction': .5}],
             1800000000.0, reason='Synthetic example only', score=72)
setup = adapt_legacy_signal(old)
print(setup.model_dump_json(indent=2))
assert TradeSetup.model_validate_json(setup.model_dump_json()) == setup
print(TradeSetup.model_json_schema())
```

使用构造器、`model_validate` 或 `model_validate_json` 做验证，不使用绕过验证的 `model_construct` / 未验证 `model_copy(update=...)` 当成合法输入。frozen 防止普通赋值，不是进程级安全沙箱；隔离与实盘封禁仍由原有模块独立负责。

```sh
python -m pytest -q tests/test_trade_setups.py
python scripts/verify_isolation.py
python -m pytest -q
# 独立 bridge 仅沿用原有 mock 检查，不启动服务
cd bridge
npm run check
npm test
npm run build
```

实际测试结果见 `docs/evidence/STAGE2-20260909.json`。新增覆盖含 JSON 往返、字段类型、不可变性、多空几何、数据缺失、八维字段、评分/RR 独立、移动规则仅描述、四策略旧信号兼容、无主流程接入、无配置/时钟/数据库/网络调用。**“低 RR Schema 可记录”不是“低 RR 获准交易”；真正拒绝场景属于后续准入阶段。**

## 7. 暂停边界 / 不能实盘的缺项

本阶段不新增服务器部署、账户接口或开关。`dry_run=true`、`live.enabled=false`、`live_runtime_allowed=false` 保持原样，Python 私有客户端及生产 Bridge 继续硬封禁。

下一阶段仍未完成：动态结构/RR、八维评分数据与政策、等级/逐仓准入、分批移动止损状态机、新计划 Paper 接入。既有实盘阻断项（独立账户/服务器隔离、原生止损生命周期与恢复等）也未解除。完成本阶段后暂停，等待用户明确确认第三阶段。
