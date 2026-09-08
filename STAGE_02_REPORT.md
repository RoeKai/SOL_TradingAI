# STAGE_02_REPORT — TradeSetup 数据结构与兼容方案

- 日期：2026-09-09（UTC+8）
- 仓库：https://github.com/RoeKai/SOL_TradingAI
- 唯一发布分支：`phase-02-tradesetup`
- 公开仓库基线：`c3b474a4c5b6b8156fdcf48ad71bb352225a6e8a`（初始化 main）
- 本阶段提交：包含本报告的提交；克隆后用 `git rev-parse HEAD` 获取完整 SHA，交付消息另列精确 SHA。报告不嵌入自己的提交 SHA，避免自引用。
- 发布状态与分支 SHA 以 GitHub ref 为准。发布不等于服务器部署，不启用任何交易服务。

## 1. 范围和发布关系

本阶段只增加不可变、版本化 TradeSetup 数据描述与旧 Signal 的单向兼容适配，以及文档和测试。不计算动态盈亏比、不计算八维评分、不实现等级准入、不实现新分批退出/移动止损，也不把 TradeSetup 接入 Paper 主链路。

这是独立公共仓库第一次导入：现有四策略、Paper、风控、日志、账本与纯代码复用桥作为既有基线一同提供，**不表示本阶段新实现了第三至第六阶段**。不导入原跟单仓库 Git 历史、原 server/client 工程、线上配置或状态。

现有链路仍是：

```text
四策略 → 原 Signal → 原 RiskEngine → 执行锁内二次检查 → PaperBroker → 独立账本
              └─ 显式调用 adapt_legacy_signal → TradeSetup 描述
                  （未接入运行时、无执行转换、无订单授权）
```

## 2. 文件变更

相对第二阶段开始前的独立模块：

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| app/setups/__init__.py | 新增 | 只导出 TradeSetup / adapt_legacy_signal |
| app/setups/models.py | 新增 | 嵌套数据契约、JSON 序列化、结构一致性校验 |
| app/setups/adapters.py | 新增 | 旧 Signal 单向旁路适配 |
| tests/test_trade_setups.py | 新增 | 70 项 Stage 2 契约/兼容/无副作用测试 |
| docs/STAGE2_TRADE_SETUP.md | 新增 | 完整字段、单位、约束和兼容方案 |
| docs/evidence/STAGE2-20260909.json | 新增 | 第二阶段原始验收摘要；其历史 branch/hash 不冒充本次发布 ref |
| STAGE_02_REPORT.md | 新增 | 本次公开发布与重新验收报告 |
| README.md | 修改 | Stage 2 边界、分支克隆方法、报告入口 |
| .gitignore | 修改 | 保留 GitHub 初始化的 Python 模板，补充秘密/状态/依赖/构建产物忽略规则 |

公开版本额外脱敏两份历史文档：`docs/ISOLATION_ACCEPTANCE.md` 的本机路径，以及 `docs/evidence/PAPER-20260909.json` 的本机路径和原始 paper 事件/请求/实例标签，仅保留匿名汇总。原始本地证据不覆盖。

相对目标仓库的初始化提交，首次发布共 **96 个文件：94 个新增、2 个修改（README.md、.gitignore）**。这是独立源码快照数量，不是新增交易功能数量；完整清单可用 `git show --stat HEAD` 查看。

运行代码、现有测试、配置和 vendor 快照与上一份已验收 Stage 2 源码包逐文件哈希一致。尤其 `app/models.py`、`app/strategies/engine.py`、`app/risk/engine.py`、`app/execution/*`、`app/paper/broker.py`、`app/portfolio/manager.py`、`app/runtime.py` 未改变。

## 3. TradeSetup 最终结构

顶层字段：

```text
schema_version, plan_version, setup_id, origin,
symbol, strategy_name, strategy_type, side,
structure_evidence, entry, invalidation_conditions, initial_stop, targets,
reward_risk, market_state, score, grade, grade_interpretation, confidence,
position_limit_advice, risk_budget, cost_assumptions, data_coverage,
stop_movement, created_at, data_as_of, valid_until,
rejection_reasons, compatibility_notes, admission_status, execution_authority
```

- `entry`：MARKET/LIMIT、参考价、入场区间、结构依据引用。
- `initial_stop` / `invalidation_conditions`：止损价格与依据，以及价格/结构/时间/数据等失效描述。
- `targets`：多个有来源标记的目标、相对初始总仓位的分批比例、每档理论和净 RR。
- `reward_risk`：整单毛/净 RR、计算方法说明；这里只储存未核验输入，不计算、不反推或改动止盈止损。
- `score`：趋势、量能、波动、大盘共振、结构、资金费率、清算区、消息面八维字段及旧分数；没有权重、求和、分级算法。
- `grade` 允许 S/A/B/C/None，只描述未来政策等级；不是胜率，不绑定目标或 RR。
- `confidence` 明确为证据置信度，**不是胜率**。
- `position_limit_advice` / `risk_budget`：仓位上限建议与预算，不读取账户、不决定下单数量。
- `cost_assumptions`：手续费率、滑点 bp、资金费、预期持仓秒数；缺失不是零成本。
- `data_coverage`：available/missing/stale/unverified/not_applicable、来源、时间、覆盖比及缺失项；默认缺失，不默认安全。
- `stop_movement`：仅保存规则，不监听成交、不触发移动。
- 时间为 UTC Unix 秒；未知有效期为 None，不代表永久有效。
- 固定 `schema_version=trade-setup/v1`、`admission_status=not_evaluated`、`execution_authority=none`。

模型拒绝未知字段、NaN/Infinity、数字型布尔、非法区间/多空几何、错误比例和悬空引用。低 RR/低分可以被描述，不等于允许交易；本阶段没有用 Schema 替代后续准入层。标准验证入口为构造器、model_validate、model_validate_json；不要以绕过验证的 model_construct 当作合法输入。

完整嵌套字段、默认值和单位见 [数据契约](docs/STAGE2_TRADE_SETUP.md)。

## 4. 旧 Signal 兼容

保留 `app.models.Signal` 原样。仅新增 `adapt_legacy_signal(signal)`，且运行时没有调用它：

1. 复制原身份、策略、方向、入场价、止损、目标及比例，不修改旧对象或共享可变目标列表。
2. 旧单点入场映射为相同上下界，标为 legacy_point；旧依据/目标标为未核验或 legacy_unspecified。
3. 原分数只进入 `score.legacy_score`；新分数、等级、置信度、RR、成本默认 None，不猜测补齐。
4. 不把 Signal 创建时间冒充行情时间，不杜撰有效期或结构止盈位。
5. 没有 TradeSetup → 可执行 Signal 的转换；没有账本迁移、运行接线或审批绕过。

因此本阶段**不改变现有下单行为**。原两次风控、执行锁、PaperBroker、账本、四策略与旧 1R/2R 各 50% 的模拟退出均保持不变。

## 5. 测试设计与本次实际结果

在独立临时源码副本执行，未加载原系统数据库或 .env。Python 测试使用 macOS 内核 sandbox 禁止所有网络，禁止读写原工作树和原仓库；Bridge 只放行 localhost 以测试临时随机端口的 HTTP 认证，外部网络与原仓库仍禁止。测试进程清空继承环境，只提供路径/语言等运行必需项。

| 验证 | 实际结果 |
| --- | --- |
| Stage 2 新测试（全量中的子集） | 70 passed |
| Python 全量 | 257 passed，0 failed，0 skipped |
| Bridge mock 全量 | 27 passed，0 failed，0 skipped |
| Bridge TypeScript check | PASS |
| Bridge 构建 + 独立 bundle 导入 | PASS；6 个 vendor 快照、82 个输入 |
| Python 静态隔离闭包 | PASS；35 个 Python 文件 |
| Dashboard JavaScript 语法 | PASS |
| main.py --check | dry_run=true、live_capability=false、network=none |

合计 **284 项测试通过**（70 项已包含于 257，不重复累计）。保留两项既有 Starlette/AnyIO 弃用警告。

首次 Bridge 全禁网运行得到 26/27，唯一失败为 HTTP 认证测试的 localhost 监听被内核以 EPERM 拒绝；只调整测试沙箱允许回环后，原测试及原交易代码不变，27/27 通过。没有为了通过测试放开交易所网络。

新增覆盖：JSON 往返、严格数值/不可变性、LONG/SHORT 几何、目标比例与引用、数据缺失、评分/RR 独立、移动规则仅描述、四策略旧信号适配、零配置/时钟/数据库/网络副作用、未接入运行时。原回归继续覆盖模拟成交、拒绝、止损、重复入场、持久化、熔断、日志/认证与实盘硬封禁。Mock 中的 FILLED/订单号均为合成数据。

复验命令（依赖仅安装于本模块）：

```sh
python -m pytest -q tests/test_trade_setups.py
python -m pytest -q
python scripts/verify_isolation.py
python main.py --check
node --check app/dashboard/assets/dashboard.js
cd bridge
npm run check
node --import tsx --test tests/*.test.ts
npm run build
```

上面是功能测试命令，不自动配置 OS 沙箱；验收时额外的内核禁网/文件拒绝策略需按实际机器路径配置，不能把普通 pytest 的通过称为内核隔离证明。

## 6. 公开发布和资金隔离

- 通过当前 GitHub 插件检查身份和仓库权限：RoeKai，push/admin；只创建并更新 phase-02-tradesetup。
- main 基线与提交父节点固定为上述初始化 SHA；不写 main/master，不 force push，不创建/合并 PR，不部署服务器。
- 白名单导出并审查文件内容，排除依赖、构建目录、Git 历史、数据库/WAL、日志、交易/仓位/运营状态和机器私有文件；历史日志片段不上传。
- .env.example 为全空凭据模板；config.yaml 为 paper 示例，保留 `dry_run: true`、`live.enabled: false`。
- .gitignore 只是辅助手段，发布树仍逐文件校验；测试里的 FAKE/sentinel 凭据为合成夹具，不是真实密钥。仓库不包含原交易账户标识。
- 发布树审计：96 个白名单文件；45 类秘密/状态/产物路径被忽略，13 类源码/模板路径保留；未发现真实凭据或未批准的运行代码变更。
- `isolation-policy.json` 保留 `live_runtime_allowed: false`；生产私有客户端/Bridge 继续硬封禁。手动改 YAML 也不能在当前阶段解除实盘封禁。
- 本轮未运行行情交易主进程、未发 Telegram、未查询真实账户、未创建任何真实或验证订单、未重启旧服务。

## 7. 结束与下一阶段边界

第二阶段完成即暂停。后续动态结构 RR、八维数据/评分、等级准入、分批退出/移动止损和 TradeSetup Paper 接线都需要另行确认。

实盘还缺独立服务器/账户与出口隔离验收、原生保护单生命周期与恢复、异步部分成交/撤单竞态、资金费和余额对账、异常 WAL 恢复与长期前向测试。本报告不解除任何实盘阻断。
