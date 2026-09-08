# STAGE_03_REPORT — 动态盈亏比 RR 计算系统

- 日期：2026-09-09（UTC+8）
- 仓库：https://github.com/RoeKai/SOL_TradingAI
- 独立分支：`phase-03-dynamic-rr`
- 父阶段：`phase-02-tradesetup`
- 父提交：`1db9a4e34e5f6c8418556e26a4b49bb804de08ab`
- 本阶段提交：包含本报告的提交；交付消息提供完整 SHA，克隆后 `git rev-parse HEAD` 可核验。报告不嵌入自己的 SHA。
- 不更新 main/master 或第二阶段分支，不合并 PR，不部署或启动服务。

## 1. 本阶段边界

只实现基于既有 TradeSetup 输入的 **纯数学 RR 计算**：

1. LONG/SHORT、MARKET/LIMIT 的每档毛/净 RR 与按分批比例加权的整单毛/净 RR。
2. 参考入场价、入场下界、入场上界三个场景，保留原始止损、目标和比例。
3. 手续费、入场/退出不利滑点、明确给出的整单资金费，以及可选的计算用数量。
4. 缺失数据、无法定义的分母或不支持的计价方式，返回结构化计算问题。
5. 不可变、可 JSON 往返的独立 RRCalculation 结果。

**没有准入判断，没有最低 1.5R 门槛，没有八维评分/等级策略，没有仓位建议生成，没有分批退出或移动止损执行，没有 Paper 接线。** score/grade/confidence 不决定 RR；低 RR、负净 RR 不会在本模块变成交易拒绝。

现有四策略、Signal、两次风控、执行锁、PaperBroker、账本、交易所连接和下单代码均不变。实盘硬关闭，不访问真实账户，不创建真实或验证订单。

## 2. 文件清单

| 文件 | 变更 | 职责 |
| --- | --- | --- |
| app/setups/rr.py | 新增 | calculate_rr 纯函数及成本/场景计算 |
| app/setups/rr_models.py | 新增 | RRCalculation / RRScenario / RRTargetResult / RRCostBreakdown / RRIssue |
| tests/test_dynamic_rr.py | 新增 | 93 项 Stage 3 测试 |
| STAGE_03_REPORT.md | 新增 | 本阶段公式、约束、兼容与实测报告 |
| README.md | 修改 | 第三阶段入口、使用方式与边界；克隆默认指向第三阶段分支 |

相对第二阶段发布树，仅 **4 个新增、1 个修改**，总共 100 个源码/文档/模板文件。第二阶段其余 95 个既有文件逐文件哈希保持一致。尤其 `app/setups/models.py`、`app/setups/adapters.py`、`app/setups/__init__.py`、`app/models.py`、全部运行链路与配置均未修改；不升级或放宽 Stage 2 Schema。

## 3. 计算口径

### 3.1 单位与输入

- 仅描述线性、USDT 计价合约；不查询交易所合约目录，调用方仍负责确认合约类型。非 USDT 后缀标的返回 UNSUPPORTED_QUOTE，不做外汇、反向合约或 quanto 转换。
- E：该场景的入场价；S：既有初始止损价；Ti：第 i 档既有目标价。
- d：LONG 为 +1，SHORT 为 -1。
- wi：第 i 档占初始总仓位的比例，不是占剩余仓位的比例。
- Q：显式传入 `quantity=` 的计算用标的数量。例如 SOLUSDT 的 Q 单位是 SOL，**不是 USDT、保证金、杠杆或下单授权**。
- fe / fx：入场/退出手续费率，0.001 表示 0.1%，不是 1。
- be / bx：入场/退出滑点 bp，10 bp = 0.1%。
- F：TradeSetup.cost_assumptions.funding_cost_usdt，整笔假设仓位的资金费总额；正数为支出，负数为收入。
- 所有价格、目标、止损、比例都来自输入，绝不为了满足 RR 反推或改动。

### 3.2 毛 RR

```text
R = d × (E - S) > 0                 # 每单位标的的原始初始止损风险，USDT
gi = d × (Ti - E)                   # 每单位标的到目标 i 的毛收益
gross_rr_i = gi / R
weighted_gross_rr_i = wi × gross_rr_i
G = Σ(wi × gi)
gross_rr_total = G / R = Σ(weighted_gross_rr_i)
```

整单取比例加权，不取最远一档，也不简单平均。例如 E=100、S=98，目标 102/104/106，比例 20%/30%/50%，每档为 1R/2R/3R，整单是 **2.3R**，不是 3R 或 2R。

### 3.3 成本与净 RR

滑点作用于不利成交方向，手续费按滑点后的成交价名义金额计算：

```text
a = E × be / 10000                 # 每单位入场滑点损耗
b(X) = X × bx / 10000              # 每单位目标/止损退出滑点损耗
Ee = E + d × a                    # 不利入场成交价
Xe = X - d × b(X)                 # 不利退出成交价
f = F / Q                         # 每单位标的资金费；显式 F=0 时不需要 Q
C(X) = a + b(X) + Ee × fe + Xe × fx + f

Rnet = R + C(S)                   # 含成本的初始止损场景损失
ni = gi - C(Ti)                   # 扣成本的目标收益
net_rr_i = ni / Rnet              # 仅在 Rnet > 0 且所有必要成本已知时定义
weighted_net_rr_i = wi × net_rr_i
N = Σ(wi × ni)
net_rr_total = N / Rnet = Σ(weighted_net_rr_i)
```

净 RR 的分母为 **含成本的初始止损损失**，不是原始 R，也不是保证金占用。本版本显式标识 `net_reward_over_cost_adjusted_initial_stop_loss`，避免消费者混用口径。

资金费保留正负号，同一个明确给出的 F 用于目标/止损场景。若假设的资金费收入使 Rnet ≤ 0，则净 RR 无定义，返回 None 和 NON_POSITIVE_NET_STOP_LOSS，不给无穷 RR、不强行截断分母。资金费收入只是输入假设，不代表保证能收取。

手续费和资金费均先按每单位计算，再按 wi、Q 分配，整单入场手续费/资金费只分配一次，不为每个目标重复收取整单费用。退出手续费按各档不同退出价计算。未执行合约精度/最小名义额舍入，这里不是下单数量计算。

若 Q 已提供：

```text
quantity_i = Q × wi
gross_pnl_i = quantity_i × gi
net_pnl_i = quantity_i × ni
gross_pnl_total = Q × G
net_pnl_total = Q × N
gross_risk_usdt = Q × R
net_stop_loss_usdt = Q × Rnet
```

未提供 Q 时，比例、价格、每单位风险和（满足条件的）RR 仍可计算，但仓位数量与绝对 PnL 金额保持 None；不会偷偷假设买入 1 SOL。

### 3.4 入场区间

分别按 reference_price、lower_price、upper_price 计算，不根据实时行情选择价格，不改区间。LONG 的 `adverse_entry=upper`，SHORT 的 `adverse_entry=lower`，这是不利入场边界标签，不是自动挑选交易价或全市场最坏结果保证。

所有场景沿用同一初始止损和目标。并不根据高分选择有利区间端点、重新锚定止损或使用实时移动止损。

## 4. 缺失和异常规则

| 条件 | 算术结果 | 不会发生的行为 |
| --- | --- | --- |
| 任一手续费率/滑点/资金费为 None | 毛 RR 可用；净 RR None；MISSING_COST_ASSUMPTIONS 指明字段 | 不把未知成本当 0 |
| F 非零但 Q 未提供 | 毛 RR 可用；净 RR None；QUANTITY_REQUIRED_FOR_FUNDING | 不使用 max_quantity/风险预算/余额猜仓位 |
| F 明确为 0 | 无 Q 也可算净 RR（其他成本需已知） | 不生成绝对仓位或金额 |
| 无目标 | 不生成目标或总 RR；NO_TARGETS | 不补固定 1:3 目标 |
| 比例十进制和不严格为 1 | 保留每档 RR，整单 RR None；FRACTIONS_NOT_EXACTLY_ONE | 不归一化、不补尾仓或改比例 |
| 不利滑点产生非正成交价 | 保留毛 RR，相关净 RR None；NON_POSITIVE_EFFECTIVE_PRICE | 不假造合法成交价 |
| Rnet ≤ 0 | 保留毛 RR/成本分解，净 RR None | 不除零、不输出 Infinity |
| 净收益小于 0 | 真实保留负净 RR | 不裁剪为 0、不因低 RR 拒单 |
| 无效类型/NaN/Infinity/错误几何/明显比例错误 | 重新执行原 Schema 校验，抛 ValidationError | 不把不合法描述提交执行器 |

比例说明：Stage 2 Schema 允许 1e-9 的合计误差，Stage 3 不修改该兼容约束。但是本计算器不会把 `(1/3, 1/3, 1/3)` 自动变为完整资金分配；其浮点转十进制后的和为 0.9999999999999999，返回每档值及明确的整单不可算标记。需要整单结果时调用方应提供明确完整分配，例如 0.333333/0.333333/0.333334。这只是描述算术边界，不是风险准入拒绝，也不会改动旧主流程。

有明确数值但缺行情来源、新闻等元数据时，可完成条件算术，同时保留 `input_missing_items`，不认证数据新鲜度、目标真实性、有效期或市场安全。assumed_holding_seconds 仅作为原输入元数据保留；没有资金费率序列时不擅自从持仓秒数推算 F。

## 5. 输出结构与兼容

入口：

```python
from app.setups.rr import calculate_rr

result = calculate_rr(setup)               # 不查账户，不启动 runtime
result = calculate_rr(setup, quantity=2)   # 可选、明确的计算用 2 个标的单位
```

- RRCalculation：计算版本、setup_id/plan_version、标的方向、完整性状态、可选 Q、原比例和、原成本假设、三个 RRScenario、结构化 issues、原数据缺失项和时间。
- RRScenario：原入场/止损、不利成交价、原始风险、含成本初始止损损失、止损成本分解、目标列表、整单毛/净 RR、可选绝对金额。
- RRTargetResult：原 target_id/价格/比例/来源类型、每档毛/净收益、毛/净 RR、加权 RR、成本分解、可选分配数量与金额。
- RRCostBreakdown：每单位入场手续费、退出手续费、入场滑点、退出滑点、资金费和成本合计；未知组件保留 None。
- RRIssue：code、scope（inputs/reference/lower/upper）、message、相关 fields。**不是 risk rejection，也不写 TradeSetup.rejection_reasons。**
- 数值使用独立、固定 50 位 Decimal 上下文，ROUND_HALF_EVEN，不依赖或修改调用方 Decimal 精度；由输入浮点的十进制字符串转换，不假装恢复浮点输入之前已丢失的精度。JSON 中 Decimal 输出字符串，None 输出 null。
- `complete` 只代表各场景算术可计算；`partial` 有部分结果；`unavailable` 没有目标或不支持计价。三者都不是交易授权。

所有结果固定：

```text
calculation_version = linear-usdt-rr/v1
verification = arithmetic_only_not_market_verified
quantity_authority = hypothetical_only_not_order_quantity
admission_status = not_evaluated
execution_authority = none
```

不改 TradeSetup 或 Signal。旧对象的预填 RR 不作为计算输入，不覆盖旧 reward_risk.verification；本次新值从独立 RRCalculation 读取。没有反向 Signal 转换或审批凭证。

`adapt_legacy_signal` 仍单向、显式、未接入运行时。其旧目标保持 legacy_unspecified；有价格即可算毛 RR，因旧 Signal 无成本通常只能给出未知净 RR。不会伪造成本把旧对象“升级为已核验”。

## 6. 可离线运行的合成例子

此例不用 API Key/.env，所有值是测试数值；不创建任何订单：

```python
from app.setups import TradeSetup
from app.setups.rr import calculate_rr

setup = TradeSetup(
    plan_version="example/v1", setup_id="rr-example", symbol="SOLUSDT",
    strategy_name="synthetic", strategy_type="custom", side="LONG",
    entry={"order_type": "MARKET", "reference_price": 100, "lower_price": 100, "upper_price": 100},
    initial_stop={"price": 98, "basis": "Synthetic example only"},
    targets=[
        {"target_id": "t1", "price": 102, "fraction": .2, "basis": "Synthetic"},
        {"target_id": "t2", "price": 104, "fraction": .3, "basis": "Synthetic"},
        {"target_id": "t3", "price": 106, "fraction": .5, "basis": "Synthetic"},
    ],
    cost_assumptions={
        "entry_fee_rate": 0, "exit_fee_rate": 0,
        "entry_slippage_bps": 0, "exit_slippage_bps": 0, "funding_cost_usdt": 0,
    },
    created_at=1800000000,
)
result = calculate_rr(setup, quantity=2)
print(result.reference.gross_rr)      # Decimal("2.3")
print(result.reference.net_rr)        # Decimal("2.3"), only because costs explicitly zero
print(result.reference.gross_pnl_usdt)  # 9.20 USDT, hypothetical
print(result.model_dump_json(indent=2))
assert setup.reward_risk.gross_rr is None
assert result.execution_authority == "none"
```

双边非零成本手算校验：多头 E=100、S=95、T=110、Q=2，入场费率 0.001、退出费率 0.002、入场滑点 10bp、退出滑点 20bp、F=1。有效入场 100.1、目标退出 109.78、止损退出 94.81，每单位净收益 8.86034、净止损损失 6.07972，净 RR = 8.86034/6.07972；不能只扣一边费用或把分母继续当 5。空头对应手算也有独立回归。

## 7. 测试设计与实际结果

2026-09-09 在独立源码副本重新执行：

| 项目 | 实际结果 |
| --- | --- |
| Stage 3 专项 | 93 passed |
| Python 全量（包含 Stage 3） | 350 passed，0 failed，0 skipped |
| 未修改 Bridge mock 全量 | 27 passed，0 failed，0 skipped |
| Bridge TypeScript check | PASS |
| Bridge 构建及独立 bundle 导入 | PASS；6 个 vendor 快照、82 个输入 |
| Python 静态依赖隔离 | PASS；37 个 Python 文件 |
| main.py --check | dry_run=true，live_capability=false，network=none |
| Dashboard JS 语法 | PASS |

共 **377 项测试通过**；93 已计入 350，不重复相加。两项既有 Starlette/AnyIO 弃用警告保留，无新失败或跳过。

专项覆盖：多空/市价/限价、单档与非等权多档、成本手算、手续费按成交价、资金费正负及分摊、数量缩放、入场区间、每档/整单一致性、缺失成本、负/零净收益、净分母非正、比例残差、不合法与构造器绕过输入、超小止损、极端有限数量、确定性精度、JSON/不可变性、score/grade/置信度/预算不影响 RR、四策略旧 Signal、移动规则仅描述、零配置/网络/数据库/时钟副作用与无主流程接入。

Python 全量在 **OS 禁止所有网络、禁止读写原跟单工作树及原仓库** 的副本运行。Bridge mock 在同样禁止外部网络和原仓库的环境运行，只允许 localhost 临时随机端口用于原认证测试；没有启动生产 Bridge 或联系交易所。测试用环境清空继承变量，私有客户端仍由已有硬封禁与毒丸测试保护。

复验命令（依赖沿用锁文件，本阶段无依赖变更）：

```sh
python -m pytest -q tests/test_dynamic_rr.py
python -m pytest -q
python scripts/verify_isolation.py
python main.py --check
node --check app/dashboard/assets/dashboard.js
cd bridge
npm run check
node --import tsx --test tests/*.test.ts
npm run build
```

上述普通命令不自动安装 OS 沙箱；内核隔离测试需额外配置实际旧仓库路径和拒绝网络规则。测试通过不是行情真实性、策略盈利能力或实盘资格的证明。

## 8. 发布与实盘阻断

- 新提交只包含本阶段 5 个文件差异；其他基线源码与文档不重写。
- 继续使用第二阶段的 .gitignore，白名单检查完整发布树；不提交 .env、真实凭据、账号/仓位/订单记录、数据库、日志、运行状态、依赖和构建产物。
- 代码与报告通过检查后只同步独立阶段分支，交付时报告分支及 commit SHA，随后暂停等验收。
- `dry_run=true`、`live.enabled=false`、`live_runtime_allowed=false` 和生产私有入口封禁全部保持原样，修改 YAML 仍不能在本阶段开启实盘。
- 不连接或操作真实账户，不创建验证订单；不启动行情交易主进程、不发送 Telegram、不重启旧服务、不部署任何服务器。
- 后续仍缺评分与数据政策、准入规则、分批退出/移动止损状态机、新计划 Paper 接线，以及既有实盘隔离/原生保护单/恢复对账等验收。本阶段没有完成或解除这些项目。

**第三阶段结束后暂停，等待用户验收；不自动进入第四阶段。**
