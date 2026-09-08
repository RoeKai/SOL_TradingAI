# 隔离阻断项修复与验收报告

日期：2026-09-09

分支：`codex/sol-ai-trading-system`

## 结论与范围

本轮仅修复 [架构文档](ARCHITECTURE_ISOLATION.md) 第九节的隔离阻断项，未恢复策略功能开发，未部署、重启原服务或创建验证订单。

**已证明当前隔离验收入口可以在操作系统禁止全部网络、禁止原仓库读写时独立完成合成模拟交易。** 默认不加载任何私有客户端；生产 Python BridgeClient、Node 私有传输和 live 启动仍硬拒绝。即使手动填写全部实盘开关，也不能解除本轮代码封禁。

这不是“完整交易系统已完成”，也不是“实盘账户隔离和远程部署已验收”。

## 第九节逐项对应

| 项 | 修复/验收措施 | 结果与证据边界 |
| --- | --- | --- |
| 1 独立包运行 | 仅复制新模块到全新临时目录；Python 使用独立 venv，Node 使用本模块已安装依赖；内核拒绝原两个仓库与全部网络 | 独立导入、148 项测试、模拟启动、Node 构建/导入通过。依赖此前已独立安装并通过 pip check；本次内核禁网阶段不在线安装包 |
| 2 依赖隔离 | Python 全模块 AST 和允许名单；Node 递归图、纯 ESM bundle AST、输入 SHA、vendor 补丁来源核验 | Python 31 个文件通过；Node 82 个输入通过；传递/动态/外部/未使用旧依赖注入回归通过 |
| 3 凭据/路径哨兵 | 显式配置和 .env，无环境回退或插值；校验路径、所有者、权限、符号链接、硬链接；禁用宿主 HTTP 代理继承 | 假旧凭据、父目录 .env、外部状态/日志链接均拒绝；哨兵文件哈希不变 |
| 4 默认无私有请求 | main 仅 offline smoke，无网络模块/密钥加载；engine 禁止 paper 注入 live；bridge 所有 private RPC 在 paper 下拒绝 | 注入假 Key 和实盘环境变量仍完成 1 笔模拟；网络客户端 0、私有请求 0、真实订单 0；IPv4/IPv6 被内核拒绝 |
| 5 独立模式及恢复 | paper/live 分目录；数据库身份写前核验；未知数据库不建表；实例/mode 不符拒绝；订单 journal 同样验证身份 | 错误实例、复制模式、未知库、日志/journal 归属及重启保持自己的风险状态测试通过；非空 WAL 保留且拒启，不自动恢复或清除 |
| 6 实盘门禁 | 严类型配置；缺项/损坏拒绝；实际提交前再次核验；生产入口额外硬关闭 | mock 覆盖 string/bool/number、环境覆盖、中途关闭和损坏；所有生产私有路径拒绝 |
| 7 订单归属及止损 | 原 client ID 与本实例 ledger/journal provenance 绑定；UNKNOWN 不换 ID 重发；外部仓位/订单不接管；确认自有成交先保护后查费用 | 30 项 Python 执行隔离 mock 测试通过，覆盖止损失败后的 reduce-only 市价退出；这不是实际成交证明。原生 STOP 子单未验收，故硬停止 |
| 8 基础设施隔离 | 独立用户/路径模板；oneshot offline smoke；PrivateNetwork、地址族限制和 deny 策略；bridge 模板不可启动；不发布网关路由 | 本机 macOS 内核隔离已验证；**新服务器的 Linux systemd、ACL、防火墙、网关、升级回滚未验收**，没有主机信息，不冒充完成 |
| 9 前端入口隔离 | 独立 token/session；根路径仅自身或 /sol-ai；不代理旧交易 API；现有原前端没有本轮改动 | mock 认证、cookie 路径、旧 token 拒绝、跨路由拒绝测试通过。实际管理网关未部署 |
| 10 上线边界 | 隔离验收入口不是常驻服务；生产 live 固定封禁，不自动申请/执行部署 | 保持阻断。第 8 项及账户身份/完整实盘生命周期未过前，不得放开实盘 |

## 实际测试结果

- `python -m pytest -q`：**148 passed**。两条 Starlette/AnyIO 的弃用警告，无测试失败。
- `python scripts/verify_isolation.py`：**ISOLATION_SOURCE_PASS: 31 Python files**。
- `npm run check && npm test && npm run build`：TypeScript 通过，**27/27 mock 测试通过**；6 份 vendor 来源快照与隔离补丁哈希通过；82 个 bundle 输入通过；独立 bundle 导入通过。
- `python -m pip check`：**No broken requirements found**。
- 将最终代码复制至 `/tmp/isolated-acceptance/sol-ai-trading-system`，在下述内核策略下再次执行全部 Python 测试：**148 passed**。
- 同一内核策略下执行复制包的 `node scripts/build.mjs`：**82 个输入，独立构建及 bundle 导入通过**，没有读取原源码。
- 最终默认入口探针：**1 笔合成模拟交易，private_requests=0，live_orders=0**。测试日志中的 FILLED/STOP 是模拟接口的固定返回，不代表交易所订单。

原已跟踪文件在本轮开始和结束比较 SHA-256：**本轮改动 0 个原文件**。既有未提交修改全部保留，没有 git reset、旧服务部署或重启。

### 内核隔离策略（本机实际使用）

```scheme
(version 1)
(allow default)
(deny network*)
(deny file-read* file-write*
  (subpath "/srv/legacy-copy-worktree")
  (subpath "/srv/legacy-copy-repository"))
```

验收前复制的是新模块源码和其独立依赖，不复制原 `server/`、真实 `.env`、数据库、交易日志或运行数据。复制目录的新 .env、父目录 .env、进程环境仅放置 `FAKE_...` 哨兵。

在该策略中，探针先实际尝试本机 IPv4、IPv6 连接及读取原仓库非敏感 `package.json`，必须得到权限拒绝，再运行独立模拟子进程；策略缺失时探针直接失败，不会把“连接拒绝”误当“内核禁止”。

```json
{
  "os_sandbox": {
    "ipv4": "PERMISSION_DENIED",
    "ipv6": "PERMISSION_DENIED"
  },
  "old_source_denied": true,
  "result": {
    "ok": true,
    "dry_run": true,
    "mode": "paper",
    "synthetic_market": true,
    "network_clients_created": 0,
    "private_requests": 0,
    "live_orders": 0,
    "trades_created": 1
  }
}
```

计数并非唯一依据：同时依赖“入口不含私有客户端能力”“mock 传输调用计数断言”和“内核禁止网络”三层证据。机器可读摘要与代码哈希见 [验收证据](evidence/ISOLATION-20260909.json)。

## 重要剩余边界

1. 本轮没有新服务器地址，无法证明远程 Linux ACL、实际网络策略或网页网关已生效。本机 sandbox 不能替代这项实机验收。
2. 专用交易所账户 UID/子账户身份尚未验证。两个 Key 不等于两个独立账户；不能复用原实盘资产。因此生产私有入口仍硬关闭，没有配置或环境变量解锁后门。
3. 原生 STOP 子订单归属、完整部分成交/撤单竞态、缺失成交均价恢复仍待后续专项实盘前验收；本轮不扩展这些功能，相关未验收路径拒绝自动导入。
4. 非空 WAL 一律拒绝自动启动，保留原文件供隔离恢复审计；不允许通过删除 WAL、journal、账本或改身份消除阻断。正常关闭后重启已验证。
5. 路径保护的部署前提是独立可信 UID、不可被其他用户改写的目录。O_NOFOLLOW/规范路径与 OS 权限不构成对“已持有服务 UID 且能改程序的攻击者”的防御承诺。
6. 当前 main 是离线验收程序，不运行实时公共行情，更不是完整生产交易守护进程。公共行情出口白名单及长运行仍未执行验收。

## 本轮修改文件

下列路径均相对于 `sol-ai-trading-system/`；M 为本轮修改，A 为本轮新增。原跟单目录没有本轮修改。

```text
M app/alerts/telegram.py
M app/config.py
M app/dashboard/server.py
M app/data/service.py
M app/execution/bridge_client.py
M app/execution/engine.py
M app/portfolio/manager.py
M app/utils/audit.py
M app/utils/locking.py
M bridge/README.md
M bridge/scripts/build.mjs
M bridge/scripts/vendor.mjs
M bridge/src/core.ts
M bridge/src/server.ts
M bridge/tests/bridge.test.ts
M bridge/vendor/logger.ts
M bridge/vendor/manifest.json
M bridge/vendor/safe-request.ts
M config.yaml
M deploy/README.md
M deploy/nginx-sol-ai.conf.example
M deploy/sol-ai-bridge.service
M deploy/sol-ai.service
M deploy/unified-console.html
M docs/ARCHITECTURE_ISOLATION.md
M requirements.txt
M tests/test_dashboard_review.py
A README.md
A app/bootstrap.py
A app/utils/paths.py
A bridge/scripts/check-imports.mjs
A bridge/src/config.ts
A bridge/src/isolation.ts
A bridge/src/yaml-browser.d.ts
A bridge/tests/imports.test.ts
A bridge/tests/isolation.test.ts
A bridge/vendor/patches.json
A isolation-policy.json
A main.py
A requirements.lock.txt
A scripts/verify_isolation.py
A tests/os_isolation_probe.py
A tests/test_dependency_isolation.py
A tests/test_execution_isolation.py
A tests/test_python_isolation.py
A docs/ISOLATION_ACCEPTANCE.md
A docs/evidence/ISOLATION-20260909.json
```

后续只应先完成剩余隔离/实盘前验收，不因为本报告自动恢复功能开发或执行部署。
