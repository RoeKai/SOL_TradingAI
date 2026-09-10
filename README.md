# sol-ai-trading-system · 独立 PAPER 交易 agent

## 8D：独立历史离线价格／数量成本契约

本分支 `phase-08d-execution-cost-contracts` 只运行独立8D入口。
不启动下面保留的旧 `main.py`、公共实时行情、Bridge或Telegram；实盘硬关闭。
结构信号价、对手报价与模拟成交价分开；同一最终数量物化资金费预算、
调用原RR和评分、原退出路径模拟并重新核验原阈值。新配置／schema 4
仅使用 `quantified-runs/`，不接管8A/B/C数据库，不重置其资金。

```sh
# Python 3.12，先在独立虚拟环境安装 requirements.lock.txt。
# 复用已经校验的八月数据；以下命令不下载、不联网。
python -m pytest -q tests/test_stage08d_*.py
python scripts/verify_isolation.py

# <CODE_SHA> 必须替换为报告记录的40位代码冻结提交。
python -m app.execution_costs.cli freeze --workspace . \
  --dataset historical-data/august-2026-v1 --kind engineering \
  --run-id august-8d-engineering-v1 --code-commit <CODE_SHA> \
  --manifest validation/stage08d/engineering-manifest.json
# 四组只读归因：同一原8C候选、同一空账本反事实快照，不创建订单。
python -m app.execution_costs.cli attribute --workspace . \
  --dataset historical-data/august-2026-v1 --source-run august-baseline-v2 \
  --manifest validation/stage08d/engineering-manifest.json
python -m app.execution_costs.cli init --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-engineering-v1 \
  --manifest validation/stage08d/engineering-manifest.json
python -m app.execution_costs.cli run --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-engineering-v1
python -m app.execution_costs.cli recover --workspace . \
  --dataset historical-data/august-2026-v1 --run-id august-8d-engineering-v1
```

结果可用 `--output 新文件.json` 独占保存；不会覆盖旧实验。先创建输出父目录。
整月基准／压力分别用新run ID及 `--kind baseline` / `--kind stress` 冻结；
日期固定为八月，原压力参数保持20bps滑点、3000ms接受延迟。
`--max-events` 可分段，结果中的 `finished=false` 必须按未完成解释。
只读归因不是四份独立统计样本；受控多空正常开仓测试也不是历史盈利证明。
具体已运行范围、限制及报告见 [STAGE_08D_REPORT.md](STAGE_08D_REPORT.md)，
逐腿公式见 [8D契约](docs/STAGE_08D_CONTRACT.md)。

## 当前交付：8A 独立离线 Paper（不运行下方旧实时入口）

`phase-08a-offline-paper` 新增独立模拟 Broker、SQLite 事务账本、事件收件箱、动作出站队列和崩溃恢复。**只使用显式合成报价、成交流及逻辑时钟；没有行情连接、账户连接、Telegram、部署或实盘能力。** 不替换 `main.py`，不读取旧 Paper 账本。

普通新开仓入口实际调用第七阶段校验、原 RR、原 Scorecard、原 Admission，但仍因结构/成本缺项与完整 Runner 政策未建模而 **REJECT**；没有预留或下单，也没有旧 Signal 回退。以下退出演示属于 **B：显式 fixture 已有持仓/开仓腿重放**，不代表 **A：完整新开仓准入链路**已打通。

```sh
git clone --branch phase-08a-offline-paper --single-branch https://github.com/RoeKai/SOL_TradingAI.git sol-ai-offline
cd sol-ai-offline
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt

# A：普通准入入口，预期明确 REJECT；不会产生订单
.venv/bin/python -m app.offline_paper.cli init --run admission-demo --compact
.venv/bin/python -m app.offline_paper.cli run --run admission-demo --scenario admission-rejection --compact

# B：仅显式 fixture 实例允许合成已有持仓/开仓腿，非通过准入的新交易
.venv/bin/python -m app.offline_paper.cli init --run exit-demo --allow-fixtures --compact
.venv/bin/python -m app.offline_paper.cli run --run exit-demo --scenario tp-runner --side LONG --compact
.venv/bin/python -m app.offline_paper.cli resume --run exit-demo --compact
.venv/bin/python -m app.offline_paper.cli review --run exit-demo

.venv/bin/python -m pytest -q tests/test_offline_paper.py tests/test_offline_recovery.py tests/test_offline_boundaries.py
```

每个场景使用全新的 `--run`。同名实例不能重新初始化余额。可选场景：`admission-rejection`、`partial-cover`、`partial-cancel`、`tp-runner`、`stop-gap`、`unknown-reconcile`；多空均可用。完整 JSON 不加 `--compact`。状态仅在 `offline-runs/<run>/ledger.sqlite3` 及其 SQLite sidecar，已忽略且不得上传；`resume` 先重放检查点、按原动作 ID 对账，不换 ID 重发。未知数据库/版本/实例及损坏状态拒绝，不删 WAL 或历史解锁。

`review` 从确认成交账本输出 Markdown，明确 fixture 不是策略有效性证据。事务、故障点、限制和下一子阶段前置见 [STAGE_08A_REPORT.md](STAGE_08A_REPORT.md)。完成 8A 后暂停，不自动进入 8B。

## 以下为保留的旧运行路径说明

下面描述的是既有 `main.py` 公共行情 Paper 路径；8A 不启动、不替换、不接入这条路径。历史阶段的“未接入”说明保留其当时范围，新离线组合以本页上方及 8A 报告为准。

已接通 **Binance 公共实时行情 → 指标 → 四策略评分 → 两次风控 → 模拟成交/持仓保护 → 独立账本 → 规则化复盘**。只交易模拟 SOLUSDT，BTCUSDT 作为做多过滤，ETHUSDT 提供辅助行情与完整性检查。所有成交都是模拟，不能据此推断实盘收益。

**默认 `dry_run: true`、`live.enabled: false`，当前生产实盘入口仍硬关闭；即使手动改成实盘也会拒绝启动。** 本模块不启动旧跟单服务、执行桥或旧数据库，不读取旧凭据/账户/订单，不创建交易所验证订单。原系统保持不动。

## 安装与启动

Python 3.12。在独立服务器只复制本目录的源码与配置模板，排除 `.env`、`.venv`、`node_modules`、`dist`、`logs`、`reports`、`trades` 中的运行数据；不得复制原仓库的 `server/` 或任何旧数据库。

```sh
git clone --branch phase-07-config-contracts --single-branch https://github.com/RoeKai/SOL_TradingAI.git sol-ai-trading-system
cd sol-ai-trading-system
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
.venv/bin/python scripts/verify_isolation.py
.venv/bin/python main.py --check

# 不需要 Binance Key 或 .env：公共行情 + 模拟交易，Ctrl+C 正常停止
.venv/bin/python main.py --headless

# 有限时长公共行情验证（退出后保留本实例账本、日志和复盘）
.venv/bin/python main.py --headless --run-seconds 45
```

首次启动请求一次 `exchangeInfo` 和每币种一次 1m K 线预热；之后订阅 ticker、kline、aggTrade、depth20。断线指数退避重连；市场连接重连后最多每 60 秒一次历史补洞，**不以 REST 高频轮询行情**。公共规则、SOL/BTC/ETH 新鲜行情、连续 15 根已闭合分钟线和四周期收益缺一不可开仓。启动日志 `paper_readiness_changed: ready=true` 表示行情条件就绪，**不是已成交或盈利证明**。

当前路由与 [Binance 官方 WebSocket 迁移说明](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice) 一致：ticker/kline/aggTrade 走 `/market`，depth 走 `/public`，没有 `/private` 订阅。

### 带网页运行

```sh
cp -n .env.example .env
chmod 600 .env
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
# 将生成的值手动填到本模块 .env 的 SOL_DASHBOARD_TOKEN；交易所 Key 保持空白。
.venv/bin/python main.py
```

打开 `http://127.0.0.1:8765`，用自己的管理令牌登录。支持行情、四策略评分/原因、持仓、净盈亏、订单日志、交易记录、风险状态、暂停/恢复和今日复盘。网页无法启用实盘、清除损失计数或重发未知订单。不要同时运行 headless 与网页两个进程，共用账本会被实例锁拒绝。远程访问需要独立认证的 HTTPS 管理入口；本轮没有更改旧前端路由或部署真实反向代理。

### Telegram（默认关闭）

在新 `.env` 填写独立 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，并在新 `config.yaml` 设置 `alerts.enabled: true` 才会真实发消息。不复用旧通知队列。支持模拟开仓/减仓/平仓、止损、熔断、公共 API 错误和断线报警；有界队列与有限重试不阻塞保护处理。本轮只做模拟传输测试，没有真实发 TG。

## 四策略：均已实现、默认开启、独立配置

| 策略 | 入场条件与状态 | 方向 |
| --- | --- | --- |
| `trend_breakout` 趋势突破 | 15m 涨幅 ≥0.5%，突破前一状态 3m 高点 0.05%，量比 ≥1.2，买压 ≥0.55 | 多 |
| `pullback_entry` 回踩承接 | 15m 涨幅 ≥0.8%，1m 回踩 ≥0.3%，从观察低点回弹 ≥0.2% | 多 |
| `panic_rebound` 急跌反弹 | 3m 跌幅超过 2% 后观察；后续行情从新低反弹达到原跌幅的 25% | 多 |
| `fake_breakout_reverse` 假突破反向 | 先突破区间 0.05%，再退回区间并由盘口压力确认失败；不是仅见突破就反向 | 多 / 空 |

每种策略可用 `enabled: false` 单独关闭。配置键拼错、非有限数值或无效阈值会拒绝启动；修改策略参数需重启本模块。所有做多信号仍必须满足 BTC 3m 跌幅小于 0.8%。已有持仓/待成交入场时不能再次开仓，无马丁、不做亏损加仓。

每轮全部评估，再按分数选最多一个候选：确认形态 60 分、量能最多 15 分、方向买卖压最多 15 分、方向动能最多 10 分；未确认形态最多 59 分、永远不能执行。默认门槛 60，可设置各策略 `min_score`。同分固定按 panic → trend → pullback → fake 排序。**这是可解释启发式分数，不是胜率，不绕过任何风控。** 冷却/重置状态落入新账本；观察价不跨断线/重启继承。分钟线最终包最多 2 秒交付等待期保留观察，但禁止新增开仓；真缺线/过期行情清空观察。

## 风控、模拟成交与退出

- 两次审批：信号产生后一次，执行锁内重新读取行情/仓位/运营状态再一次。
- 默认单笔估算止损预算 5 USDT、日亏损上限 20、每天最多 3 次入场意图、连续亏损 2 笔停止新增；最多 1 仓、保证金 ≤权益的 20%、杠杆 ≤5 倍。亏损/次数计数不因重启或点击恢复而清空。
- 按止损距离、手续费与不利滑点计算数量，使用公共合约精度；不足交易最小值则明确拒绝，不加大风险凑单。
- 市价入场、限价入场/过期/撤单；限价成交前再次审批。挂单默认 30 秒失效，暂停/退出进程会撤销未成交模拟入场。
- 无合法止损不可开仓。在线 paper 止损先于止盈，价格跳空按当前不利可成交价模拟，不假设恰好在止损价成交。**跳空可能超过 5 USDT 预算，不能保证绝对最大实际亏损。**
- 默认 1R/2R 各 50% 分批退出，R 为信号价到止损价距离；最后一档关闭全部余量。数量太小的中间档仅标记延期，不伪造已成交；完成标记、数量、费用和现金同事务，避免重启重复减仓。
- 暂停/熔断只禁止新增，SOL 自身报价新鲜时照常模拟退出，不被 BTC/ETH 断线卡住。SOL 报价过期时不制造成交；停机期间没有交易所原生保护，恢复后按新行情处理并保留缺口事实。
- 模拟成本：默认双边每次成交 0.05% 手续费、不利滑点 10bp，保守买卖价。不是订单簿排队/真实深度成交模型；暂不含资金费、强平、借贷、流动性冲击、真实部分成交竞态。

## 状态与复盘

全部在当前模块：`trades/paper/ledger.sqlite3`、`logs/paper/events.jsonl`、`reports/paper/review-YYYY-MM-DD.md`。日志逐条记录行情、评分/候选、审批/拒绝、下单/撤单、成交、盈亏、错误；默认按 10MB 轮转、保留 5 份备份，需要长期原始行情审计时在新服务器独立归档，不挂载旧日志。

本地时区默认 `Asia/Kuala_Lumpur`。每天 00:05 生成前一日 Markdown；每次模拟全平、正常退出以及网页请求也会生成对应日报。四策略即使无成交也有零样本行；输出扣费胜率、盈亏比、利润因子、已实现最大回撤、平均持仓时间、评分/审批计数和收紧/暂停建议。额外报告有效 SOL 行情采样的账户含浮盈最大回撤（断线/停机不插值）。复盘不调用大模型、不自动改参数。

只打开当前身份的账本；外部路径、符号/硬链接、错误所有者/不安全权限、未知数据库、paper/live 或实例不匹配均拒绝。保留既有非空 WAL 离线核查阻断：异常断电留下非空 WAL 时，**不要删除 WAL、改身份或清库绕过**；需要恢复工具验收后再处理。这是当前尚未完成的无人值守恢复能力。

## 如何证明没有实盘订单

```sh
.venv/bin/python scripts/verify_isolation.py
.venv/bin/python -m pytest -q
```

查看 `/api/snapshot` 中 `safety`：`production_live_sealed=true`、`private_client_constructed=false`、`ledger_mode=paper`。`network_receipts` 只允许公共 `GET /fapi/v1/exchangeInfo`、`GET /fapi/v1/klines` 与两个公共 WS 路由；不带 API Key、签名、代理或重定向。**零计数不是独立证明**：需与源码闭包检查、私有调用毒丸测试、隔离副本的内核禁网/禁止读旧仓库测试一起看，证据见 [本轮验收](docs/PAPER_ACCEPTANCE.md)。没有访问真实账户做“零订单”查询，因为这本身就是禁止的私有调用。

保留离线命令 `main.py --isolation-smoke`，仅在单独验收副本运行：它会产生明确标记的合成 `isolation_smoke` 交易，会占用该副本交易次数，不能混入正式 paper 实例。

独立 TypeScript 复用桥仍只构建与 mock 验证，不运行：

```sh
cd bridge
npm ci --ignore-scripts
npm run check
npm test
npm run build
```

## 当前不能实盘的缺项

生产入口硬封禁；本轮没有解除。尚需独立服务器/账户 UID 与出口 ACL 验收、原生止损子订单完整归属和重启对账、真实异步部分成交/撤单竞态验证、资金费与强平/余额对账、异常 WAL 恢复、长时间压力/断网/磁盘故障运行验证，以及足够的样本外/前向 paper 统计。测试中的假成交、45 秒公共行情验证均不等于上述验收通过。后续必须完成这些项目并单独授权，再由操作人手动开实盘，禁止创建验证订单。

## 第二阶段：TradeSetup 结构（尚未接入交易）

第二阶段新增 `app/setups/models.py` 的不可变、版本化 `TradeSetup` 和 `app/setups/adapters.py` 的单向 `adapt_legacy_signal`。该阶段仅交付描述与序列化层；第三阶段的独立 RR 计算、第四阶段的旁路评分、第五阶段的纯准入决策及第六阶段的独立退出状态机见下节。新准入和新退出引擎都尚未接入 Paper。上文现有四策略/风控/止盈仍按原流程运行，`Signal`、下单、账本及运行配置不变。

旧信号可旁路映射为描述，但旧分数只保存到 `score.legacy_score`；结构依据、RR、等级、置信度、有效期及缺失数据不会被猜测补齐。所有新对象固定 `admission_status=not_evaluated`、`execution_authority=none`，没有 `TradeSetup → Signal` 执行转换。

完整字段、单位、兼容限制与验收命令见 [第二阶段数据契约](docs/STAGE2_TRADE_SETUP.md)；该阶段历史发布与验收见 [STAGE_02_REPORT.md](STAGE_02_REPORT.md)。

## 第三阶段：独立动态 RR 计算（尚未接入交易）

显式调用 `from app.setups.rr import calculate_rr`，传入 `TradeSetup`，返回不可变 `RRCalculation`。按既有入场、初始止损、目标与比例计算每档和整单毛/净 RR，并分别输出参考入场价和区间两端场景；不生成或调整止盈止损，不把评分换成 RR。

净 RR = 扣除成本后的目标收益 / 含成本的初始止损损失。滑点按不利方向作用于成交价，手续费按模拟成交名义金额计算，资金费明确区分支出与收入。缺失成本不按零处理：仍可算毛 RR，净 RR 标记未知。非零整单 USDT 资金费需要显式 `quantity=`（计算用标的数量，不是下单数量）；绝不使用仓位建议上限反推实际仓位。

结果为独立旁路描述，Decimal 数值在 JSON 中为字符串。`complete` 只表示算术输入完整，**不是风控通过**；新结果仍为 `admission_status=not_evaluated`、`execution_authority=none`。不回写 TradeSetup，不接入 Paper、账本、执行锁、风控或实盘。完整公式、比例约束、边界与实际测试见 [STAGE_03_REPORT.md](STAGE_03_REPORT.md)。第三阶段交付后暂停，等待验收，不自动进入评分或准入阶段。

## 第四阶段：八维计划评分描述（尚未接入交易）

显式调用 `from app.setups.scorecard import score_trade_setup`，传入 `TradeSetup`、同一计划的 `RRCalculation` 和明确的 `evaluated_at` 时间。返回独立、不可变的 `Scorecard`，**只包含这八个质量维度**：方向置信度、入场质量、止损质量、止盈质量、盈亏比质量、仓位质量、执行清晰度、整体交易质量。每维有 `score / label / explanation`，整体附 `summary`。

前七维等权平均为第八维，不重复计权。缺失、过期或未验证的必需输入返回 `score=null / label=invalid` 和结构化问题；不将可评分项重新归一化到 100 分，不猜测胜率。方向置信度只转述调用方的证据置信声明，不新增方向预测。RR 质量只描述既有净 RR，不调整价格或仓位，不生成 RR 准入阈值。

这是新的旁路结果，不是旧 `Signal.score` 或第二阶段保留的市场因子 `TradeSetup.score`；它们均保持原样。**没有评分准入、下单、新 Paper 接线、分批退出或移动止损执行**。未修改实盘硬封禁或原运行流程。低分和负净 RR 仍正常返回描述，不成为交易拒绝。

```sh
.venv/bin/python -m pytest -q tests/test_scorecard.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_isolation.py
.venv/bin/python main.py --check
```

第四阶段无需启动主程序、行情订阅、数据库或执行桥。评分规则、缺项语义、调用范例与实测结果见 [STAGE_04_REPORT.md](STAGE_04_REPORT.md)。该报告保留第四阶段历史边界；第五阶段经独立授权新增以下旁路决策，不修改评分器。

## 第五阶段：Admission / Risk Gate（纯决策，尚未接入 Paper）

显式调用 `from app.admission.engine import admit_trade`，输入同一计划的 `TradeSetup / RRCalculation / Scorecard`，以及调用方明确提供的 Paper 风险快照、交易所约束、申请、策略配置和评估时间，输出不可变 `AdmissionDecision`：

- `APPROVE`：全部硬条件满足，按正常计算后的风险预算具备 Paper 资格。
- `REDUCE`：计划成立，但评分、市场、剩余预算或数量/保证金上限要求缩仓；不修改方向、入场、止损和目标。
- `REJECT`：硬门槛、关键单维、覆盖度、总分或最小可用预算不满足；允许风险和数量均为零。

先检查结构、成本、净 RR、数据新鲜度及账户硬约束，再由七项单维和第八项综合分确定机会风险档位。S/A/B/C **不是胜率，也不决定杠杆**。数量由止损风险反推并取各约束最小值，按交易所步长向下取整，随后使用第三阶段原函数按最终数量再次计算整单净 RR；不能用最远 TP 或毛 RR 代替。缺失/未确认安全状态一律不能默认通过。

阈值模板为独立 [admission.yaml](admission.yaml)，通过 `parse_admission_policy(yaml_text)` 显式解析；它不读取文件或环境变量，`main.py` 不自动加载此文件。账户快照必须同时提供当前硬上限，最终取配置和账户上限中的较严值。

未来消费契约 `require_paper_admission` 拒绝旧 Signal、裸 TradeSetup、伪造/过期决策、变更的快照/配置及未经 RR 重算的数量。**本阶段没有执行入口，也没有把既有 Paper 主流程改成新准入流程。** 哈希只是内容绑定，不是数据真实性认证或一次性交易凭证；可信快照、执行锁内二次检查、原子风险预留、持久化幂等消费仍须后续接入和验收。

```sh
# 纯离线测试；不启动主程序、账户客户端或 Bridge 服务
.venv/bin/python -m pytest -q tests/test_admission.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_isolation.py
.venv/bin/python main.py --check
```

第五阶段历史实测新增 216 项，Python 全量 692 项、Bridge 27 项均通过。完整结构、硬/软规则、配置、计算口径及边界见 [STAGE_05_REPORT.md](STAGE_05_REPORT.md)。第六阶段经独立授权新增下列旁路模块，不修改第五阶段实现。

## 第六阶段：Exit Policy / Position Protection（尚未接入 Paper）

`app/exits/` 提供不可变退出记录、纯事件状态机、确定性尾仓规则和 JSON 检查点重放；不是订单客户端，也不读写数据库、文件、环境变量或时钟。

- 首次确认开仓成交即冻结 `frozen_initial_r` 和 R 的入场锚点，后续移动止损不重定义 R。多次开仓成交的实际均价另行记录，开仓腿确认结束后按实际原始总量分配。
- 默认 1R 计划减原始数量 30%，2R 再减原始数量 40%，余量归 Runner。触价只产生意图，数量、盈亏和 TP 完成标记只由确认成交更新。
- TP1 完成后才申请纯保本或成本覆盖止损；只有保护确认回执才更新当前止损。LONG 只提高，SHORT 只降低。
- 默认 Runner 从 3R 激活、以历史最有利可信报价回撤 1R 跟踪，不在 3R 强制全部卖出；另有确认 Swing、ATR 输入接口、时间退出及趋势失效退出。
- Stop 优先于 TP；部分成交按真实数量处理。UNKNOWN、撤单未结算或终态先于成交明细时只对账，不重复发退出。最小量不足不向上凑单；最终尾差只使用显式确认的精确全退能力，不支持则报告保护异常，不能伪造平仓。
- `restore_checkpoint` 完整重放验证历史并追加恢复阻断事件；未完成动作按原 ID 先对账，恢复本身不重新发 TP/止损订单。
- 审查修复：固定数量止损显式记录覆盖数量/仓位数量版本；新增成交后原子补保护，不把旧 ACK 当作整仓覆盖。动态整仓必须显式配置能力和对应确认契约。
- 冻结 R、历史开仓 VWAP、剩余持仓成本分别记录；最终毛盈亏独立核对全部确认成交金额差，净盈亏扣实际费用。
- CANCEL/RECONCILE 明确失败采用可配置有界重试；UNKNOWN 不重发退出单，目标订单的权威回执才解除控制动作等待。耗尽重试升级保护异常。
- R2 回执排序修复：终态锁存独立于 SETTLING；旧止损的迟到 ACCEPTED/UNKNOWN 不恢复保护身份。累计成交高水位不倒退，明细补齐前不解除原 ID 对账和恢复阻断；真实迟到成交仍按唯一 fill_id 入账。
- R3 ProtectionLost 入口修复：与 ActionReceipt 共用累计证据、终态和明细结算逻辑。UNKNOWN 携带的数量不丢失，None 不等于 0；旧 ACK 不能抹去待核对量，矛盾终态隔离并按原订单 ID 对账，不放行新的全退意图。只有 ExitFill 改变持仓、费用和盈亏。
- 退出状态、检查点和默认政策升级为 v2；旧 v1 检查点拒绝自动恢复，不能重建历史动作 ID 冒充安全迁移。
- R2/R3 不改 JSON 字段和动作 ID 算法，但收紧 v2 语义：旧快照必须与新规则完整重放一致才允许恢复；有差异时保留原记录并阻断，不自动清空/迁移。

独立阈值见 [exit-policy.yaml](exit-policy.yaml)，主程序不加载。所有结果固定 `execution_authority=none_until_paper_integration`，实盘继续硬关闭。**既有 Paper 的旧 50%/50% 退出仍保持原状，本阶段没有替换它。**

```sh
.venv/bin/python -m pytest -q tests/test_exit_policy.py tests/test_exit_recovery.py tests/test_stage06_review_regressions.py
.venv/bin/python -m pytest -q tests/test_stage06_r1_receipt_ordering.py tests/test_exit_receipt_invariants.py
.venv/bin/python -m pytest -q tests/test_stage06_r2_protection_lost.py tests/test_exit_protection_lost.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_isolation.py
.venv/bin/python main.py --check
```

第六阶段原有 145 项退出测试、R1 的 108 项和 R2 的 178 项测试原样保留。本轮 R3 原样收录审查方六项复现（SHA-256 不变）并补 150 项 ProtectionLost 证据/终态/补齐/恢复回归，新增 **156 项**。Python **1279 项**、Bridge **27 项**通过，共 **1306 项**；原样六项在 58fff0ab 基线上全部失败，修后通过。完整证据、状态转换与限制见 [STAGE_06_REPORT.md 第 12 节](STAGE_06_REPORT.md#12-审查修订-r3protectionlost-累计证据入口统一)。真实持久化事务、执行锁/出站队列和新 Paper 接线尚未实现；可重放状态不等于已完成交易所执行保证。

第六阶段 R3 已按独立决策与可重放恢复范围验收；第七阶段只新增下述离线配置契约，不接入执行。

## 第七阶段：配置快照与跨模块一致性（独立、离线）

`app/configuration/` 编译显式提供的 `config.yaml`、`admission.yaml`、`exit-policy.yaml` 和新的 [configuration.yaml](configuration.yaml) 版本/作用域声明，生成不可变 `ConfigBundle`。三个原配置文件、现有四策略、Paper、账本、Bridge 和退出状态机实现均未修改。

纯函数入口：`app.configuration.compiler.compile_bundle(...)` 接收四段 YAML **文本**；`app.configuration.contracts.validate_contract(bundle, inputs, evaluated_at=...)` 接收明确的 `PlanInputs` 和 UTC 秒。核心不读取文件、环境变量、时钟、账户或数据库。复用原 `calculate_rr` 和原评分函数核对描述；如显式给出全部历史审批输入，只在原时间重放原 `admit_trade` 核对记录，不产生新审批或订单资格。

不用创建 `.env`、运行 `main.py` 或启动任何服务，直接执行：

```sh
# 只检查配置：退出码 0，不代表计划、运行时或执行已就绪
.venv/bin/python -m app.configuration.check
.venv/bin/python -m app.configuration.check --example config-valid --json --parameters

# 同义参数矛盾：退出码 2；不采用最后值覆盖
.venv/bin/python -m app.configuration.check --example synonym-conflict --json

# 上游 50/50 与拟用 30/40/30 不匹配：退出码 3
.venv/bin/python -m app.configuration.check --example allocation-mismatch --at 1800000000 --json

# 即使比例匹配，Runner 的完整路径收益仍未建模：退出码 3 / UNSUPPORTED
.venv/bin/python -m app.configuration.check --example runner-unmodeled --at 1800000000 --json

# 仅在本模块的指定示例子目录读取本地显式资料；plan.json 默认被 Git 忽略
# 自行提供原始、独立的历史记录，不为通过校验编造评分/资金费/确认来源
.venv/bin/python -m app.configuration.check --plan examples/configuration/local/plan.json --at 1800000000 --json

.venv/bin/python -m pytest -q tests/test_config_bundle.py tests/test_config_contracts.py tests/test_config_cli_isolation.py
```

CLI 只读指定的本模块配置文件（或 `examples/configuration/<case>/` 下对应固定文件名），同时检查本模块 `isolation-policy.json`；无父目录扫描、外部根目录参数、`.env`、配置回退或环境插值。符号链接/硬链接被现有隔离边界拒绝。`--at` 是显式测试时间，不自动读系统时钟。不得把真实记录导出进 Public 仓库；公开例子是代码生成的缺资料合成说明，不含评分、账户或真实结构证据。

配置文件只支持嵌套 YAML：请写 `risk: {max_loss_per_trade: 5}`，不能写字面键 `risk.max_loss_per_trade: 5`。任何层级（包括列表中的映射）的点号键都在 flatten/defaults 前拒绝；即使值相等或没有另一种写法也不接受。内部来源路径可以含点号，字符串值/URL 中的点号不受影响。正式字段别名 `drop_pct`、`btc_max_drop_pct` 仍须写在对应策略的嵌套节中，保留符号转换、冲突检查和来源追踪；YAML anchor/alias 语法继续禁止。解析失败时 `bundle=null`，CLI 返回退出码 2。修复与复验记录见 [Stage 7 R1](STAGE_07_REPORT.md#10-审查修订-r1嵌套键与字面点号键碰撞)。

默认输出摘要：

```text
Config parse: PASS; config consistency: PASS
Plan: NOT_EVALUATED; runtime metadata: NOT_EVALUATED; trusted runtime: INCOMPLETE
NOT_INTEGRATED; execution_authority=none; live_allowed=false; no account or order access
```

JSON 独立记录 `config_parsing / config_consistency / plan_consistency / runtime_metadata / runtime_trust / execution`。错误带 `reason_code、severity、field_path、source、actual、expected、suggestion`。`runtime_metadata=PASS` 仅表示提供的声明完整且内部一致；`verified=true`、来源名称和内容哈希都不是真实性认证。`runtime_trust` 固定 `INCOMPLETE`，执行固定 `NOT_INTEGRATED / none`，没有订单权限。

- 对可比较的单笔风险、杠杆、权益保证金比例和仓位数取更严上限，保留每个来源；日净现金损失与累计负交易结果、已用次数与待开仓预留不能混算。BTC 负百分数阈值用相同 `<=` 谓词组合，不能机械取数值最小值。
- 费率 `.0005 = .05%`，`10 bps = .1%`；USDT 损失预算、BASE 数量、名义金额、保证金、杠杆分别记录。金额/比例/价格比较用 Decimal；原模型浮点字段不能精确表示的政策声明明确拒绝，不静默舍入。
- 同 ID/版本但止损、目标、比例、成本、评分、政策或其他计划内容变化，不能复用旧绑定/审批。新计划须在审批前声明完整配置摘要，真实签发与原子消费尚未接入；旧审批不自动追认。
- 原市场结构、参考 R 触发价、静态 RR 场景分别保留。Runner 到 3R 激活不等于 3R 成交；分批、移动止损、时间/趋势退出及资金费路径未完整估值，返回 `UNSUPPORTED`，不补造收益/资金费、不拉远目标。
- 已有持仓继续绑定原 seed/policy；新配置错误不停止旧仓必要保护。旧检查点仍必须按原契约完整重放一致，不清空历史、不迁移账本、不热更新政策。

字段/单位/默认值/覆盖规则见 [参数来源表](docs/STAGE_07_PARAMETER_SOURCES.md)；最终结构、例子、真实测试与第八阶段前置清单见 [STAGE_07_REPORT.md](STAGE_07_REPORT.md)。

**第七阶段交付后暂停等待验收；不进入第八阶段、不合并 main、不部署、不接入 Paper/Live。实盘继续硬关闭。**

详细边界见 [架构隔离](docs/ARCHITECTURE_ISOLATION.md)、[历史隔离验收](docs/ISOLATION_ACCEPTANCE.md)、[Paper 闭环验收与文件清单](docs/PAPER_ACCEPTANCE.md)。未连接或部署任何新/旧服务器，未改原前端或重启旧服务。

## 第八阶段 8B：普通审批的合成离线闭环

这是独立新入口，**不是 `main.py` 的新默认模式，也不启用任何实盘能力**。8A 入口和旧账本不变。8B 只接受本实例供应器生成的合成计划，不能传裸 Signal／JSON 审批绕过准入。默认 `allow_fixtures=false`，成功示例从空数据库开始，不调用 `fixture_entry`。

从本仓库根目录，在自己的依赖环境执行（不需要 `.env` 或 API Key）：

```bash
python -m app.admitted_paper.cli demo --workspace . --run-id demo-long --side LONG
python -m app.admitted_paper.cli demo --workspace . --run-id demo-short --side SHORT
python -m app.admitted_paper.cli recover --workspace . --run-id demo-long
python -m app.admitted_paper.cli review --workspace . --run-id demo-long

# 普通入口拒绝例：不会产生订单、预留或持仓
python -m app.admitted_paper.cli reject --workspace . --run-id reject-evidence --reason evidence
python -m app.admitted_paper.cli reject --workspace . --run-id reject-risk --reason risk
python -m app.admitted_paper.cli reject --workspace . --run-id reject-s3 --reason scenario

# 显式进程崩溃探针（预期退出码 91），随后按原订单 ID 恢复
python -m app.admitted_paper.cli demo --workspace . --run-id crash-demo --fault after_broker_execution
python -m app.admitted_paper.cli recover --workspace . --run-id crash-demo

python -m pytest -q tests/test_admitted_paper.py tests/test_admitted_recovery.py tests/test_admitted_boundaries.py
```

每个 `demo/reject` 必须使用尚未存在的 run-id；重复初始化直接拒绝，不重置余额。数据库身份为 `sol-admitted-paper/8b`、schema 2，固定在本模块的 `offline-admitted-runs/<run-id>/ledger.sqlite3`，不能接管 schema 1 的 8A 数据库。运行目录和数据库不上传。

数据链 → Candidate → 原 RR/Scorecard/Admission → **独立 ExitPlan 与 S0–S3 条件准入** → 锁内重算/预留/消费 → 原模拟 Broker → 确认成交 → 原退出状态机 → 事务账本/复盘/恢复。JSON 输出每个绑定 ID、原因、现金、费用、原始 R、剩余仓位和待对账项。

原第七阶段 `RUNNER_FULL_POLICY_VALUATION_UNSUPPORTED` **仍然存在**。8B 只建模指定条件路径；原 50/50 结构目标及其静态 RR 不会被改成执行的 30/40/30。S3 扣除 Runner 跟踪距离，使用最接近的合格既有结构位置，不把 3R 激活当作 3R 成交。`win_probability` 和 `full_policy_expected_return` 均未知，模拟盈利不能证明策略有效。

限制：只有明确的合成无资金费合约、固定 R Runner、指定报价和显式成交量；没有实时行情、真实流动性、真实资金费、强平系统、交易所账户或私有 Bridge。报价/版本改变后的旧审批拒绝，需用新请求重新申请；尚未被 Broker 接受的意图在重启改变账户版本后会以原 ID 拒绝结清，而非暗中重新授权。已接受订单/已有仓位继续恢复和保护。

完整模型、事务边界、测试记录与限制见 [STAGE_08B_REPORT.md](STAGE_08B_REPORT.md)。8B 交付后暂停验收，不合并 main，不自动进入后续阶段。

## 第八阶段 8C：真实历史观察、假设执行模型、离线模拟账户

新入口与 `main.py`、8A/8B 实例、真实 `.env`、私有 Bridge 完全分开。只执行 SOL；BTC/ETH 是参考。
固定评估 2026-08-01 至 2026-09-01（UTC，右端不含），预热 300 秒。官方历史 aggTrades **不是真实盘口**；价差、滑点、延迟、参与率、规则和账户均为明确的模型假设。不得以模拟结果宣传实盘收益。

版本提示：本节原 `baseline-v2` 命令记录旧代码 `88901b3…` 的实验流程，只能用该冻结代码重现；R1修复代码必须用下文独立的新run-id，不能接管或覆盖旧v2运行。

首次运行先准备本项目依赖环境。下列命令不需要 API Key：

```bash
# 唯一有公共历史网络权限的独立下载器（不启动交易循环）
mkdir -p historical-data
python -m app.historical_download.cli preflight
python -m app.historical_download.cli download --directory historical-data/august-2026-v1

# macOS 禁网；Linux 请在 --network none 容器/等效隔离环境中执行后续命令
offline() { sandbox-exec -f examples/historical-replay/offline.sb "$@"; }
offline python -m app.historical_replay.cli index --dataset historical-data/august-2026-v1

# 先冻结。此 commit 是本次正式回放实际使用的代码，不是结果后的文档提交。
offline python -m app.historical_replay.cli freeze --dataset historical-data/august-2026-v1 --manifest historical-data/august-2026-v1/baseline-v2.json --run-id august-baseline-v2 --kind baseline --code-commit 88901b3288ab21540917506f32f4759fdf72a1e5
offline python -m app.historical_replay.cli init --dataset historical-data/august-2026-v1 --manifest historical-data/august-2026-v1/baseline-v2.json --run-id august-baseline-v2
offline python -m app.historical_replay.cli run --dataset historical-data/august-2026-v1 --run-id august-baseline-v2
offline python -m app.historical_replay.cli recover --dataset historical-data/august-2026-v1 --run-id august-baseline-v2
offline python -m app.historical_replay.cli report --dataset historical-data/august-2026-v1 --run-id august-baseline-v2

offline python -m pytest -q
offline python scripts/verify_isolation.py
```

`--kind engineering` 固定首小时；`stress` 固定整月、滑点20bps/接受延迟3000ms；`missing_restart` 固定首小时、BTC缺失第10–12分钟。各用新 `--run-id` 和 manifest，再 `init/run`。故障实验 `run --fault scheduled_restart` 会在第15分钟提交游标后以91退出，随后先 `recover` 再不带 `--fault` 的 `run`。`--max-events` 可分段推进，不能当成整月完成。其他真实进程故障点为 `before_market_cursor_commit`、`after_market_cursor_commit`、`before_funding_cursor_commit`。

重复初始化、未知/其他实例数据库、配置或代码摘要变化、已存在清单/索引均拒绝；不会自动重置余额或迁移旧状态。新数据库仅在 `historical-runs/<run-id>/ledger.sqlite3`，application ID 1397705786 / schema 3。大行情、数据库、WAL 和日志不上传。代码与数据内容摘要是绑定信息，不是外部真实性签名。

**当前重要限制：原8B精确单点入场区间与本轮非零价差存在冲突。** 原 Admission 拒绝区间外可成交价；不扩大区间、不回退 fixture、不伪造通过。固定资金费预算也会影响诊断数量下的静态净RR。零成交不是策略安全或有效性的证据；没有发生的完整历史成交链路不能列为验证通过。

完整证据、实际范围、性能、故障对照、拒绝分布和限制见 [STAGE_08C_REPORT.md](STAGE_08C_REPORT.md)；可重取的公开来源及实验摘要在 [validation/stage08c](validation/stage08c)。实盘、实时行情、测试网、账户、Telegram 和服务器部署继续关闭。8C交付后暂停验收。

### 8C R1：竞争退出和模型越界

修复代码 `677d22d26b6c2f597aa1a2af5ee2606526e00886` 只修带仓调度与模型失效。每次模拟成交后重查后续订单、累计量、可减仓量；仍共享同一市场参与量。新可见报价先检查原有暴露，首次 `MODEL_LIMIT_EXCEEDED` 在同事务锁存，清仓和重启不能使其恢复为有效回放。

报告升级 `historical-report/v2`。若模型越界，`metrics=null`、`model_supported_metrics_available=false`；账务数字放入 `diagnostic_metrics`，其他方向/持仓汇总也仅供诊断，不代表模型支持的收益。`run` 返回 `model_invalid=true` 后不再推进普通回放；待保护/待核对状态保留，不删除账本来解锁。缺失性能计时保持null，不生成假计时。

已有归档只读复用，以下不用下载器、真实Key或旧服务。首次在新检出副本执行时应使用自己的新run-id；若这里的ID已存在，应直接查看该次报告，不能覆盖初始化。

```bash
offline() { sandbox-exec -f examples/historical-replay/offline.sb "$@"; }
offline python -m app.historical_replay.cli freeze --dataset historical-data/august-2026-v1 --manifest historical-data/august-2026-v1/engineering-r1-v3.json --run-id august-engineering-r1-v3 --kind engineering --code-commit 677d22d26b6c2f597aa1a2af5ee2606526e00886
offline python -m app.historical_replay.cli init --dataset historical-data/august-2026-v1 --manifest historical-data/august-2026-v1/engineering-r1-v3.json --run-id august-engineering-r1-v3
offline python -m app.historical_replay.cli run --dataset historical-data/august-2026-v1 --run-id august-engineering-r1-v3
offline python -m app.historical_replay.cli recover --dataset historical-data/august-2026-v1 --run-id august-engineering-r1-v3
offline python -m app.historical_replay.cli report --dataset historical-data/august-2026-v1 --run-id august-engineering-r1-v3
offline python -m pytest -q tests/test_stage08c_active_integration.py tests/test_stage08c_active_cli.py
```

新增测试的仓位是明确注入的合成组件状态，使用真实历史模拟Broker/退出消费者/SQLite，但没有普通历史审批。完整HistoricalPaper/CLI恢复必须拒绝将其伪装为正常入场。真实数据首小时仍零订单；原整月v2结果不改标为R1。本轮记录与限制见报告第10节和 [validation/stage08c-r1](validation/stage08c-r1)，不放宽精确入场/价差及诊断数量净RR契约。
