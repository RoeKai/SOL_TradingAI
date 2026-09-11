# Stage 09 — 协议冻结与数据资格前置记录（不是完成回测）

| Strategy | Signals | Trades | Gross EV | Fee EV | Baseline EV | PF | Max DD | Stability | Rating |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| TREND_BREAKOUT | null | null | null | null | null | null | null | NOT_RUN | INSUFFICIENT_EVIDENCE |
| PULLBACK_ENTRY | null | null | null | null | null | null | null | NOT_RUN | INSUFFICIENT_EVIDENCE |
| PANIC_REBOUND | null | null | null | null | null | null | null | NOT_RUN | INSUFFICIENT_EVIDENCE |
| FAKE_BREAKOUT_REVERSE | null | null | null | null | null | null | null | NOT_RUN | INSUFFICIENT_EVIDENCE |

**本轮已完成：新版方法单独提交、可检索数据使用审计、原代码不变核对及现有回归。**
**尚未完成：Stage 09 研究适配器、正式 Alpha、三个成本口径的交易回测和策略有效性评级。**
表中 null 是未运行／证据不足，不是观察到零交易或负期望，也不是 C 级结论。
不能据本文件宣称已回答“四策略是否存在扣成本后的稳定优势”。

## 1. 冻结与版本关系

- 分支：`codex/stage09-strategy-backtest`。
- 原已验收代码：`506b33364f112480198caa57436253b0282ba86d`。
- 上一版协议提交：`19bd3088c73d14fd7b5fe85b8450c9e3f48cc03e`，原样保留。
- **Stage 09 协议独立提交：`18e83761db640abfddffe990abbb492cc28d1a3c`**。
- 协议文件：[docs/STAGE_09_STRATEGY_BACKTEST_PROTOCOL.md](docs/STAGE_09_STRATEGY_BACKTEST_PROTOCOL.md)。
- 协议 SHA-256：`39a08425397aba67e4f65dd3c5b6a6f7674b8033ab82a5e6fb597d28fb15e736`。
- 本轮用户原始要求 SHA-256：`beb14024ec49c08536ce3f0873656ee2c0b4f16071831505002738c9ad483fef`。

协议 commit 仅新增这一份协议文件，没有夹带研究代码、数据结果、策略参数或运行修改。
本报告及审计随后提交，不改变已冻结协议。它冻结已定义的方法及读出阻断，
**不是将未指定的退出比例／Runner规则宣称已经完全冻结**。

用户新版要求在收益读取前更改了区间、SHORT标签、去重及评级；因此另建Stage09
协议，没有覆盖旧协议或倒改旧实验。五月至六月正式收益未被本轮读取。

## 2. 五月至六月的数据使用检查

范围固定 `[2026-05-01T00:00:00Z, 2026-07-01T00:00:00Z)`，61UTC日。
结论严格为：**`NO_PRIOR_USE_FOUND_IN_AVAILABLE_RECORDS`**。
不是 `PROVEN_NEVER_SEEN`，也没有证据可以给本区间贴 `HOLDOUT_CONTAMINATED`。

审查方本次说明同样仅限其可检索记录：未发现该区间用于策略收益、设计或选参，
但不能排除其他人、聊天、电脑或外部工具的使用。本次将这一局限原样保留，
没有把它升级成绝对未见证明，也不要求审查方证明一个无法穷尽的命题。

本地实际核对：

| 检查面 | 实际发现 |
| --- | --- |
| 本地所有分支的提交描述、docs及阶段报告 | 无五月至六月历史收益／标签／选参记录；旧协议提及拟验证日期不是已使用记录 |
| validation | 8C/8D/8E/8FA数据清单及实验记录指向八月、八月首小时或七月31日预热 |
| research-runs | 仅`august-8fa-v1`旧研究；新stage09只有NOT_RUN前置状态 |
| historical-data | 仅`august-2026-v1`；aggTrades与funding；没有本地五月／六月归档、没有历史top20盘口 |
| historical-runs | 7份身份匹配的历史离线运行元数据，全部八月或八月首小时 |
| quantified-runs | 6份身份匹配的8D元数据，全部八月或八月首小时 |
| diagnostic-runs | 八月8E摘要／文件名，未发现五月至六月用途 |
| 旧工作副本SOL子项目的docs/reports/阶段报告 | 未发现五月至六月研究记录；没有读取旧跟单配置、账户或线上库 |

元数据源和13份run摘要收录于
[validation/stage09/preflight-audit.json](validation/stage09/preflight-audit.json)，
SHA-256 `b25ca8633f979bd028dc73463e4ee6dc3a4643d6dcbfeab996801f2f801eaddb`。
13个摘要已再次与对应元数据核对，实际输出 `METADATA_MATCHES=13`。
这不是13次策略验证，也不加入Python／Bridge测试总数。

### 2.1 保留的实际读出异常与边界

初次SQLite只读元数据查询5份成功、8份报 `unable to open database file (14)`。
未把该错误当成数据库损坏、未见数据或空仓证明，也未删除WAL／journal解锁。
对没有WAL和rollback journal、不是符号链接的8份冻结文件，改用
`mode=ro&immutable=1`做窄范围元数据读取；核对应用身份，并检查前后inode／大小／
修改时刻及日志仍不存在。8份完成，未观察到并发变化，最终元数据未读项为0。

所查字段只有`history_meta`中run的版本、评价／预热时间范围与内容摘要，未查询
订单、成交、权益、收益或历史标签。未运行Store、恢复／迁移、checkpoint或交易服务。
只读SQLite的控制侧文件不包含在“整个源目录逐字节未变”的声明中；本报告不作
该过度声明。元数据阴性检索也不能证明别处没有人工使用。

后续如发现正式区间使用记录，必须列出日期／用途并停止正式读出；不自己换月。
八月继续仅用于工程正确性，不能拿旧8FA结果给原四策略评级。

## 3. 当前两类实际前置缺项

### 3.1 原变量与正式历史资料

原指标买方压力包含前20档盘口，现有aggTrades不能恢复。趋势突破的`.55`及
假突破反向的失败盘口确认不能取消，也不能换成主动成交差或常数。
这两条目前是 `DATA_INSUFFICIENT`，不是原策略没有Alpha。

回踩／急跌核心价格Alpha可以单独设计只读适配，但五月至六月的输入归档当前不在
本地，研究代码尚未编写。原运行指标的通用ready还要求三标的新鲜盘口；不能把
ready强制为true来声称“原引擎直接复现”。新版协议明确研究核心与运行准入的差别。

正式Baseline还缺对应历史bid/ask、资金费mark及规则资料核对；声明费率每侧
`.0005`和滑点10bps不等于已校准真实成本。本轮没有下载、采购、采集或读取账户。

### 3.2 退出基准需要人工确认

原Signal是1R50%／2R50%，没有Runner；用户新要求是1R部分退出后保本、2R及Runner。
尚未明确TP1／TP2／Runner比例、Runner激活与跟踪距离、纯保本或成本覆盖。
因此已写入 **`EXIT_SPECIFICATION_INCOMPLETE`**，而不是默认为后来30/40/30。

已向用户请求确认一个候选规格：1R30%、2R40%、30%Runner，3R激活／1R跟踪，
TP1确认后纯entry保本。该候选还没有收到确认，不是本轮批准参数。
必须在任何正式Alpha／收益结果读取之前完成这个选择并单独冻结，不能先看
Alpha后再挑退出。没有选择前不运行交易层，也不以不完整协议宣称全方法已就绪。

## 4. 已固定的研究交付口径

协议保留五窗口1/5/**15主窗口**/30/60m，ALL与每15分钟最早信号去重两版，
匹配波动、UTC小时、SOL方向、BTC环境的背景，UTC日块不确定性及各窗口有效分母。
LONG为`future/entry−1`，SHORT为`entry/future−1`；SHORT标签不能误当线性合约收益。
先失效后的MFE不算原计划可实现机会；最高／最低价不是成交价。

交易层随后分别披露Gross、Fee-only、Baseline；同路径同数量做成本归因，
记录费用、价差、滑点／取整和资金费。没有退出／报价资料时，不用虚构路径补完表。

完整输出覆盖信号／交易数、方向、胜率、平均盈亏／盈亏比、PF、EV、净收益、
费用、回撤、连亏、持有期、交易频率、月收益及辅助Sharpe；按周／波动／方向
披露稳定性及最佳日／周／前5交易集中度。阈值、样本最低要求和分母已在协议记录。

A只代表可以申请后续完整模拟盘研究，不是开启模拟盘或实盘的授权；B仅保留
执行成本研究资格；C停止本策略版本；数据或样本不足为INSUFFICIENT_EVIDENCE。
当前没有正式Alpha或交易结果，因此不能给任何策略A、B或C。

## 5. 本轮实际测试与未运行项

使用项目既有Python3.12.13、Node24.18.0和固定依赖，没有升级依赖。
Python运行于禁止网络和原跟单系统访问的OS沙箱；Bridge mock只允许本机回环，
类型／构建仍禁网络。以下都是**本轮重新执行的开发方结果**，不是抄录8FA通过数。

| 项目 | 实际命令（离线沙箱内） | 实际结果 |
| --- | --- | --- |
| 现有Python全量 | `python -m pytest -q` | **2311 passed，2 warnings，220.69s** |
| Bridge mock | `node --import tsx --test tests/*.test.ts`，bridge目录 | **27 passed，0 failed/skipped，568.9965ms** |
| Bridge类型 | `node node_modules/typescript/bin/tsc --noEmit` | 退出码0 |
| Bridge构建 | `node scripts/build.mjs` | 退出码0；6个vendor、82个输入、standalone import通过 |
| Git空白检查 | `git diff --check` | 退出码0 |
| 源码不变 | 与`506b333...`对比app/main/config/admission/exit-policy/bridge/tests | 无差异 |
| 审计JSON | 13条完整摘要、64位格式、逐源metadata比对 | 通过，独立记录不累计为项目测试 |

两条warnings为现有Starlette/httpx及anyio弃用提示；本轮不扩范围升级修复。
Bridge测试打印的订单是合成mock，不是真实下单。没有用访问真实账户证明安全。

**没有运行：**Stage09新研究单元测试（尚无新研究代码）、正式期数据下载／内容
扫描、五月至六月Alpha、任何三成本回测、八月新研究工程回放、统计评级及交易部署。
全量回归通过只说明既有代码在当前测试下未退步，不证明本轮研究算法已经实现。
没有新增或删除既有断言，没有混入审查方25项／其他阶段的测试数。

## 6. 文件清单、输出与现有行为影响

| 新增文件 | 用途 |
| --- | --- |
| `docs/STAGE_09_STRATEGY_BACKTEST_PROTOCOL.md` | 独立方法冻结，明确未冻结退出不得读出 |
| `STAGE_09_STRATEGY_BACKTEST_REPORT.md` | 本前置记录，不冒充完成回测 |
| `validation/stage09/preflight-audit.json` | 脱敏时间范围及数据使用元数据审计 |
| `validation/stage09/test-results.json` | 本轮实际回归证据，不是历史研究结果 |

另建本地忽略文件`research-runs/stage09/preflight-v1/status.json`，明确NOT_RUN、
指标null和待解决项；没有收益工件。`research-runs/`已被gitignore覆盖，不上传。
没有修改原协议、策略／参数、数据库schema、执行／风控／账本／恢复、Bridge封禁、
main默认行为或旧实验。代码、配置及旧协议摘要均已复核相同。
远端main核对仍为`c3b474a4c5b6b8156fdcf48ad71bb352225a6e8a`，无合并或force push。

## 7. 当前结论与暂停点

目前**不能判定任何一条策略值得继续工程开发**：尚无这四条策略在正式区间的
完整证据，不是已经回测后发现四条策略均无效。

- 趋势突破、假突破反向：原盘口证据不足，不能用替代变量完成评级。
- 回踩承接、急跌反弹：核心价格研究尚待正式数据及研究适配；没有正式结果。
- 四条交易层：共同退出规格未确认，禁止先看收益再补规则。

先确认退出基准，并明确可用的历史盘口资料来源／缺失处理范围；之后才继续
已冻结的研究顺序。本轮在此前置点暂停，不启动任何策略功能、Paper、实盘或部署。
