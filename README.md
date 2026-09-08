# sol-ai-trading-system · 独立 PAPER 交易 agent

已接通 **Binance 公共实时行情 → 指标 → 四策略评分 → 两次风控 → 模拟成交/持仓保护 → 独立账本 → 规则化复盘**。只交易模拟 SOLUSDT，BTCUSDT 作为做多过滤，ETHUSDT 提供辅助行情与完整性检查。所有成交都是模拟，不能据此推断实盘收益。

**默认 `dry_run: true`、`live.enabled: false`，当前生产实盘入口仍硬关闭；即使手动改成实盘也会拒绝启动。** 本模块不启动旧跟单服务、执行桥或旧数据库，不读取旧凭据/账户/订单，不创建交易所验证订单。原系统保持不动。

## 安装与启动

Python 3.12。在独立服务器只复制本目录的源码与配置模板，排除 `.env`、`.venv`、`node_modules`、`dist`、`logs`、`reports`、`trades` 中的运行数据；不得复制原仓库的 `server/` 或任何旧数据库。

```sh
git clone --branch phase-04-scorecard --single-branch https://github.com/RoeKai/SOL_TradingAI.git sol-ai-trading-system
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

第二阶段新增 `app/setups/models.py` 的不可变、版本化 `TradeSetup` 和 `app/setups/adapters.py` 的单向 `adapt_legacy_signal`。该阶段仅交付描述与序列化层；第三阶段的独立 RR 计算和第四阶段的旁路评分见下节。新等级准入、移动止损和新 Paper 接入仍未实现。上文现有四策略/风控/止盈仍按原流程运行，`Signal`、下单、账本及所有配置不变。

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

本阶段无需启动主程序、行情订阅、数据库或执行桥。评分规则、缺项语义、调用范例与实测结果见 [STAGE_04_REPORT.md](STAGE_04_REPORT.md)。第四阶段发布到独立分支后暂停，等待验收，不自动进入第五阶段。

详细边界见 [架构隔离](docs/ARCHITECTURE_ISOLATION.md)、[历史隔离验收](docs/ISOLATION_ACCEPTANCE.md)、[Paper 闭环验收与文件清单](docs/PAPER_ACCEPTANCE.md)。未连接或部署任何新/旧服务器，未改原前端或重启旧服务。
