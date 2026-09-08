# STAGE_04_REPORT — 八维计划质量评分

- 日期：2026-09-09（UTC+8）
- 仓库：https://github.com/RoeKai/SOL_TradingAI
- 独立分支：`phase-04-scorecard`
- 已验收父阶段：`phase-03-dynamic-rr`
- 父提交：`c151ee3db5a35b85ceee6fe380b21f4d89fb22c6`
- 本阶段提交：包含本报告的提交；交付消息提供完整 SHA。报告不嵌入自己的 SHA。
- 工作方式：从第三阶段建立独立 Git 工作副本，切换到第四阶段分支。不在原跟单工作树开发，不改 main/master，不合并，不部署。

## 1. 交付边界

按本轮最终确认的八维定义实施，**不是趋势、成交量、RSI、资金费率、消息等市场因子评分器**。

本阶段只提供纯函数：

```python
score_trade_setup(setup, rr, *, evaluated_at, max_data_age_seconds=300) -> Scorecard
```

输入是第二阶段 TradeSetup 与同一计划的第三阶段 RRCalculation。没有文件、配置、环境变量、数据库、账户、网络或系统时钟读取；不自行取得行情。只输出可解释、版本化的计划描述，不具有交易权限。

没有准入判断、买卖指令、仓位生成、目标/止损修改、分批退出或移动止损执行，没有接入策略候选排序、Paper、风控、执行锁或账本。低分、0 分、负净 RR 正常返回评分描述，**不会变成交易拒绝**。

`admission_status=not_evaluated`、`execution_authority=none` 固定不变。实盘入口继续硬关闭；未读取真实账户，未创建真实或验证订单，未发送 Telegram，未启动或重启任何交易服务。

## 2. 文件清单

| 文件 | 变更 | 职责 |
| --- | --- | --- |
| app/setups/scorecard_models.py | 新增 | Scorecard、七维/整体描述、检查项、缺失问题、显式评估上下文 |
| app/setups/scorecard.py | 新增 | 纯函数、七项描述评分及第八项等权汇总 |
| tests/test_scorecard.py | 新增 | 126 个评分、兼容、隔离与边界测试 |
| STAGE_04_REPORT.md | 新增 | 本报告、规则与验收证据 |
| README.md | 修改 | 第四阶段入口、调用边界及分支安装命令 |

相对第三阶段：**4 个新增、1 个修改**；完整发布树 104 个代码/文档/模板文件，其余 99 个既有文件哈希不变。未改依赖、配置、隔离策略、模型、RR 实现或任何旧测试。

重点保持不变：`app/models.py`、`app/setups/models.py`、`adapters.py`、`rr.py`、`rr_models.py`、`app/setups/__init__.py`、`main.py`、全部四策略/风控/Paper/执行/账本/连接模块、`config.yaml`、`isolation-policy.json`、全部 bridge/vendor 代码。

## 3. Scorecard 最终结构

顶层包含身份与版本（schema/rubric/plan/setup/symbol/side）、明确的 `context`、输入数据截止/有效期、RR 版本/完成状态，以及恰好以下八维：

| 中文维度 | 字段 |
| --- | --- |
| 方向置信度 | direction_confidence |
| 入场质量 | entry_quality |
| 止损质量 | stop_loss_quality |
| 止盈质量 | take_profit_quality |
| 盈亏比质量 | rr_quality |
| 仓位质量 | position_quality |
| 执行清晰度 | execution_clarity |
| 整体交易质量 | overall_trade_quality |

每维共同字段：

- `score`：0–100，保留两位小数；有必需输入缺失时为 `null`，不是 0。
- `label`：excellent / good / fair / poor / invalid。
- `explanation`：文字说明。
- `status`：complete / partial / unavailable，描述**这项评分输入**的完整程度，不是交易状态。
- `known_points`：已知检查项贡献；缺项情况下不是最终分、不是补零结果或安全分。
- `coverage`：可评价检查项权重 / 总权重。
- `issues`：`code / field / explanation`，标记 missing、stale、unverified、not_applicable 或 unsupported。

前七维另有 `checks`：每个检查项含 `code / max_points / points / explanation / inputs / issues`，便于追溯。

第八维另有 `summary` 和固定汇总口径 `equal_mean_first_seven_no_missing_imputation`。

额外保留 `input_missing_items`、原计划 `recorded_rejection_codes`、明确的解释/验证范围、固定无执行权限标记和限制说明。原拒绝记录只复制代码，不重新裁决、不清除。不新增 approved、eligible、allow_trade、order_quantity 等准入或下单字段。

## 4. 描述性规则 v1

规则是**未校准的启发式计划描述**，不是盈利预测、策略回测结论或风控标准。分数从输入算出，绝不为了分数修改输入。规则版本为 `plan-quality/v1`。

### 4.1 方向置信度

仅使用调用方显式提供的 `confidence.value × 100`，并要求相关引用依据及 structure 覆盖项具有明确可用状态、来源和本次时间窗口内的时间戳。没有 confidence 时不从 LONG/SHORT、策略名、旧 score、等级或高 RR 猜测置信度。

TradeSetup 的 confidence 语义仍是 **证据置信声明，不是胜率**。本阶段没有新方向预测算法，无法独立判断方向对错，也不认证调用方的声明是否真实。

### 4.2 入场质量

- 40 分：声明 `structure_zone`；引用结构价格位于给定入场区间的比例。无依据/不可用依据为未知，而非满分。
- 40 分：`clamp(1 - 市场价距区间距离 / R, 0, 1)`。在区间内距离为零；市场快照及 market_price 覆盖项必须有可用状态、来源和有效时间戳，快照不会覆盖掉缺失标记。LIMIT 也只描述当前距离，不预测成交。
- 20 分：`clamp(1 - 区间宽度 / R, 0, 1)`，描述价格区间精确程度。

`R = abs(参考入场价 - 初始止损价)`。窄区间不表示易成交；不引入 RSI、ATR 或新的指标数据。

### 4.3 止损质量

- 20 分：经过原 TradeSetup 验证的亏损侧几何位置。
- 50 分：引用结构位的合理方向关系；多仓引用 support/swing_low/range_boundary，空仓引用 resistance/swing_high/range_boundary，止损在亏损侧结构位以外或相等。依据无法解释时为未知。
- 30 分：价格失效条件方向与触发价一致；LONG 使用 lt/lte、触发价位于止损与入场下界之间，SHORT 镜像。冲突为已知低分，缺少价格失效条件为未知。

不构造/移动止损；没有噪声、波动、跳空或清算充分性模型。

### 4.4 止盈质量

- 50 分：按原始分批权重检查结构目标是否被盈利侧结构位支持。多仓目标不越过所引用的 resistance/swing_high/range_boundary，空仓镜像使用 support/swing_low/range_boundary。
- 20 分：原模型已验证的盈利方向排序及区间外几何位置。
- 20 分：原始比例用 Decimal 检查精确和为 1；不偷偷归一化。
- 10 分：在参考入场场景下净目标收益为正的分批权重，依赖完整净收益及有来源/时间的成本假设。

没有目标不补造目标。没有目标到达概率，不模拟“先止盈再止损”的路径。

### 4.5 盈亏比质量

使用第三阶段已算出的整单**净 RR**，参考入场值占 50%，入场区间两端最小净 RR 占 50%。不取最远目标冒充整单，不用毛 RR 替代未知净 RR。

每个净 RR 场景的描述分档：

| 净 RR | 场景描述分 |
| --- | --- |
| ≤0 | 0 |
| (0, 1) | 25 |
| [1, 1.5) | 45 |
| [1.5, 2) | 60 |
| [2, 3) | 80 |
| ≥3 | 100 |

3R 以上不加额外分，避免仅拉远目标就不断提高这项分数；其他维度仍独立描述真实结构依据。以上不是准入门槛，1.49R、0.1R、负净 RR 仍返回描述，不下单、不拒单。方向置信度和旧 score/grade 不参与 RR 计算。

缺失费用、滑点、资金费、成本来源/时间、有效净风险分母或完整区间净 RR 时，相关检查项为未知。第三阶段仍可保留已算出的毛 RR，不被评分覆盖。

### 4.6 仓位质量

只比较**显式计算用数量**及其三入场场景金额与调用方给定的计划上限：

- 35 分：计算用数量与 `max_quantity` 的一致性。
- 35 分：三场景最大含滑点入场名义额与 `max_notional_usdt` 的一致性。
- 30 分：三场景最大含成本初始止损损失与 `max_loss_usdt` 的一致性。

完整数据下，计算值不超过声明上限给该项满分，超过给该项 0；这只是计划内部描述，不产生准入结果。零上限不会被忽略。未给数量/上限则标记未知，禁止用 max_quantity 反推实际数量。

不读取/推断实际余额、实际持仓、逐仓/全仓、实际杠杆、当日损失或交易次数。`remaining_daily_loss_usdt`、杠杆上限、保证金比例不会偷偷成为本阶段准入条件。不校准、生成或修改仓位建议。

### 4.7 执行清晰度

五项各 20 分：明确的入场方式/区间，明确有效的数据截止/有效期，完整有来源和时间的成本声明，精确分批比例，以及与初始止损一致的价格失效条件。缺失为未知；过期计划的时间项为 0，但仍生成描述。

不执行撤单、止盈、移动止损或过期阻断；不要求未开发的第六阶段规则存在，也不保证交易所可以执行给定计划。

### 4.8 整体交易质量与标签

前七维完整时：`overall.score = round(sum(七维 score) / 7, 2)`，不把第八维重复加入。

任一评分项缺失则整体 `score=null / label=invalid`，保留已知贡献和覆盖比例。**不对剩余维度重新归一化，不以未知=0 伪装最终综合分。** 已知完整的低分可以是 0/poor，与未知不同。

标签：85–100 excellent，70–<85 good，50–<70 fair，0–<50 poor，未知 invalid。标签不是 S/A/B/C 交易准入等级，更不是胜率。每项按 Decimal 计算、两位小数 ROUND_HALF_EVEN 展示；整体对已展示的七维分数取均值。

综合 summary 列出七维分数/未知状态并给出文字结论。全部评分输入完整，也可能仍有不参与本版评分的原始市场数据缺项；它们继续放在 `input_missing_items` 中，不因高分消失。

## 5. 输入绑定、时间与兼容

1. 对 TradeSetup / RRCalculation 要求精确类型，重新验证不可变输入副本，防止 `model_copy` 绕过原有几何/类型约束。
2. 用接受的 Stage 3 纯函数及 RR 中显式的计算用数量重新计算并比对完整结果，防止其他计划、旧价格、错误成本、篡改目标/区间/比例或 RR 数值混入。
3. 类型/记录不一致抛出输入契约错误，不构造“交易拒绝”；缺失评分资料返回结构化未知，而不是异常或隐式安全值。
4. `evaluated_at` 必须调用方明确传入且不早于计划创建；默认 `max_data_age_seconds=300`，可以只对此次描述显式调整，结果记录上下文。不读取系统时钟，不改 config.yaml，也不写运行状态。
5. `available` 只是调用方状态声明，叠加本次元数据年龄检查，不认证数据来源。缺数据不能默认安全。
6. 全部结果是独立旁路，不回写 TradeSetup。`TradeSetup.score` 保留第二阶段原市场因子 ScoreBreakdown，`Signal.score` 保留旧流程语义；两者均未被重命名、升级或用于新评分。新八维在 `Scorecard` 命名空间。
7. `adapt_legacy_signal` 单向兼容不变；旧 Signal 可能没有结构依据、置信度、成本、有效期等，新 Scorecard 如实标记未知，不能把 legacy_score 当新分数。
8. 不导出执行转换，不在 `app/setups/__init__.py` 或主流程自动导入评分模块；调用方必须显式选择这个纯函数。

## 6. 调用与运行

使用已有描述对象，不需要连接任何外部服务：

```python
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup

# setup 是调用方已构造的 TradeSetup；以下 quantity 只是算术假设。
rr = calculate_rr(setup, quantity=1.0)
card = score_trade_setup(setup, rr, evaluated_at=setup.created_at,
                        max_data_age_seconds=300)
print(card.model_dump_json(indent=2))
```

完整可运行的合成输入在 `tests/test_scorecard.py` 中的 `raw_plan/plan`；不包含真实账号、订单或行情记录。默认样例七维分数为 **90 / 96 / 100 / 100 / 80 / 100 / 100**，综合 **95.14**；这是人工构造的单元测试案例，不能当作实盘信号或盈利证明。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
.venv/bin/python -m pytest -q tests/test_scorecard.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_isolation.py
.venv/bin/python main.py --check
# 只校验配置，不启动 main.py / --headless / 行情或私有连接。
```

## 7. 实际测试与验收

本阶段在独立源码副本实测，非仅计划执行：

| 检查 | 实际结果 |
| --- | --- |
| Stage 4 单元测试 | 126 passed |
| Python 全量，含原 Stage 2 / Stage 3 / Paper / 风控 / 隔离回归 | 476 passed，0 failed，0 skipped |
| TypeScript bridge mock 测试 | 27 passed，0 failed，0 skipped |
| 两套测试总计 | 503 passed |
| Python 源码依赖隔离扫描 | ISOLATION_SOURCE_PASS，39 个 Python 文件 |
| `main.py --check` | ok=true、dry_run=true、live_capability=false、network=none |
| TypeScript `tsc --noEmit` | 通过 |
| bridge 构建/导入检查 | 6 个隔离 vendor 快照、82 个 bundle 输入，无原跟单运行时/DB |
| dashboard JS 语法检查 | 通过；未修改该文件 |

Python 仍有 2 条既有 Starlette/AnyIO 测试依赖弃用警告；没有为本阶段改动依赖或连接逻辑。

覆盖：

- LONG/SHORT、MARKET/LIMIT、逐项手算分数、前七维等权、两位小数与标签边界。
- 缺少置信度、结构/市场/成本资料、元数据过期、未验证来源、缺有效期和缺计算用数量；未知与已知低分严格区分。
- 止损结构/失效方向冲突、目标结构与原始分批权重、空目标、比例微小残差。
- 0.1/1/1.49/1.5/2/3/4/8R、负净 RR、区间最差端点、成本缺项、资金费导致净分母非正、非 USDT 标的。
- 仓位零上限、精确边界、超出声明上限、不从建议推导数量，不使用日亏损/实际杠杆做准入。
- 错计划身份、价格、成本、数量、区间、比例或伪造 RR 绑定错误，模型校验绕过、非法时钟输入。
- JSON 往返、不可变结果、不修改旧 Signal/TradeSetup/RR，不使用旧 score/grade，不清除原拒绝记录。
- 明确的无 I/O/配置/数据库/网络/时钟毒丸测试；静态检查新模块纯依赖且没有被原运行链路导入。
- 外部 Decimal 精度/舍入/trap 不改变结果；重复调用完全确定。

### 内核级隔离

全量 Python 测试在 macOS `sandbox-exec` 中运行：拒绝所有网络，并拒绝读取/写入原跟单仓库及其工作树。bridge 测试同样拒绝所有外网和旧仓库，只允许 localhost 用于一个已有的合成 HTTP 测试。清空继承环境，只放行工具 PATH 和语言设置。

bridge 输出中 `FILLED/NEW` 等是 fake transport 的合成回执，**不是交易所受理或真实成交**。没有通过访问真实账户“查询零订单”来证明隔离。

证明链为：原运行文件与配置哈希未变 → 新模块无运行接线 → 毒丸测试禁止外部能力 → 全量回归在拒绝外网/旧仓库的内核沙箱通过 → 生产配置与代码硬封禁仍有效。

## 8. GitHub 与敏感信息边界

只发布独立模块源码、测试、文档、空凭据模板和安全配置。复用已发布第三阶段的脱敏基础树，不上传原跟单历史或本机运行记录。现有 `.gitignore` 继续排除 `.env`/密钥/账号文件/数据库及 WAL/日志/运行状态/构建产物/依赖目录；本阶段不放宽规则。

发布前按完整文件白名单扫描敏感模式、验证空 `.env.example` 与实盘硬关闭配置，比较 99 个未改基础文件哈希；推送后核对完整远端树的 blob SHA、实际分支 SHA、提交父链和报告。仅推进 `phase-04-scorecard`，不更新 main、第二或第三阶段分支，不创建合并提交。

## 9. 仍不能实盘 / 暂停点

第四阶段到此为止。该模块没有第五阶段准入，也没有第六阶段退出引擎或新 Paper 接线；评分未经回测校准，没有胜率含义。计划结构、行情来源、成本和置信声明也未认证。

既有生产入口硬封禁继续保留。独立服务器/账户身份与出口 ACL、原生保护单和真实异步成交对账、异常 WAL 恢复、长期压力/故障及前向 paper 验收等原缺项仍未完成；本报告不替代这些验收。

完成本阶段源码与报告 GitHub 同步后**暂停，等待用户验收**。不自动开发第五阶段，不连接实盘，不合并或部署。
