# 部署边界：独立 PAPER 后端，生产实盘仍禁用

## 本轮更新

已经交付公共行情驱动的 PAPER 常驻入口 `python main.py`（认证网页）与 `python main.py --headless`；启动与隔离约束见 [README](../README.md)。本机公共行情验证已经执行，**未部署任何服务器或修改旧服务**。以下旧 unit 仍是离线验收模板，没有被偷换成可联网的常驻任务：`sol-ai.service` 保持 oneshot/禁网，`sol-ai-bridge.service` 继续不可启动。

新服务器要部署 PAPER 时，应创建专属用户、专属本地目录与独立网络出口策略，先按下方公共行情准入方案完成 OS 验收，再新增 PAPER 专用 service；不能直接放宽旧服务权限、共用旧 DB/凭据，也不能把仅适用于离线验收的 `PrivateNetwork=true` 模板当成行情服务。当前没有提供真实服务器地址或完成该验收，因此不宣称已部署。统一网页仍可用独立 `/sol-ai/` 代理到新服务器，但本轮未改旧前端/路由。

## 以下为上一轮离线模板说明（历史边界保留）

状态：本轮只补 `docs/ARCHITECTURE_ISOLATION.md` 第 9 节的隔离阻断项。完整交易调度入口尚未交付；本目录不代表模拟盘/实盘可部署。**未连接新服务器、旧服务器、数据库、交易所或 Telegram，未安装这些模板。**

## 唯一允许的本地验收入口

将本模块源码、固定 vendor 快照和依赖清单复制到单独临时目录；不得复制旧 `.env`、旧状态、原仓库 `server/`、生产数据或共享目录。测试依赖可事先在独立环境安装，随后断网验收：

```sh
python scripts/verify_isolation.py
python -m pytest -q
python main.py --isolation-smoke --cycles 1
```

`isolation-smoke` 是有限回合的合成行情/模拟状态验收，不启动网站、不连公共行情、不创建 live bridge、不读真实账户、不发 Telegram。`dry_run: false` 也会拒绝启动，错误为 `LIVE_ISOLATION_NOT_ACCEPTED`。不提供常驻交易服务或开启实盘的操作说明。

## 文件与服务隔离模板

- `sol-ai.service` 只定义 `Type=oneshot` 的离线检查：无网络依赖、无重启循环、无开机 enable 入口，使用独立 `sol-ai` 用户；`PrivateNetwork`、`IPAddressDeny=any`、仅 `AF_UNIX`，禁止网络访问旧 DB/执行服务。仅新模块 logs/trades/reports 可写，并显式隐藏常见旧数据库 socket/目录。
- `sol-ai-bridge.service` **不可启动**：`RefuseManualStart=true`、`ExecStart=/usr/bin/false`，无 enable 入口，不运行 Node，不读取交易密钥。默认模拟验收永远不依赖它。
- 所有目录必须是新实例专属本地目录，非符号链接、非共享挂载；不包含旧服务器凭据。源码路径检查不能证明 Linux mount namespace/ACL 已生效，真实服务器必须另行验收。

这些 unit 只用于未来经授权的隔离验收准备，当前不要复制到 `/etc/systemd/system`，不要执行 daemon-reload/start/enable，不要重启旧 service。不同 Linux/systemd 版本需要独立核验 directive 支持，未知或不支持的隔离指令必须阻止验收，而非忽略继续。

## 未来公共行情模拟盘的网络准入方案（尚未实施）

当前离线阶段默认拒绝所有 IP 网络。后续另行授权启用真实公共行情时，采用独立容器/网络命名空间与网关出口策略，出站默认拒绝：

1. 仅放行经过 DNS/FQDN 严格校验的 `fstream.binance.com:443` WSS 和 `fapi.binance.com:443` 公共行情读取。REST 仅允许明确的公共路径 `/fapi/v1/klines`、`/fapi/v1/exchangeInfo`、`/fapi/v1/time`，方法只读；必须阻断 `/fapi/v*/account`、`/balance`、`/positionRisk`、`/order`、`/algoOrder` 等所有私有路径。
2. 只按域名或目标 IP 放行 Binance **不足以证明不会请求私有接口**。需要应用层公共路径 allowlist、默认不创建签名客户端、假 Key 哨兵和抓包/代理日志联合验收。TLS 加密流量下，L3 防火墙不能检查 URL 路径；不得声称仅靠 iptables 已满足第 9.4 项。
3. 拒绝旧服务器全部地址、数据库/缓存端口与私有地址段出站；不挂载 DB socket、共享卷、旧配置或旧日志。旧系统具体 IP/网段清单由操作人在新服务器上提供并独立核验，不从旧数据库读取。
4. 不启动 bridge；不开放 8766；不放行 Telegram 或任意 HTTP_PROXY/HTTPS_PROXY。真实 Telegram 通道是后续单独授权的出口，绝不借用旧 TG 队列/凭据。
5. DNS、TLS 信任、隧道/VPN和管理入口亦需精确放行及审计；不能用 `0.0.0.0/0` 宽松放行代替验证。不存在“复制本模板即可保证安全”的结论。

## 统一网页隔离（仅本地 mock 路由测试）

`nginx-sol-ai.conf.example` 的路由全部注释，当前没有可部署的新网站后端。将来单独获准时，旧 `/` 与新 `/sol-ai/` 分别指向不同后端；新模块只接受自身令牌及会话，永不调用旧登录 DB 或旧下单 API。保留旧路由和权限不变。

`unified-console.html` 只是不可操作的文字示意，不替换旧主页。未建立真实管理隧道，未测试真实 Nginx/HTTPS 跨服务器访问，也未验收服务器 ACL。恢复常驻服务/公共行情/实盘部署必须另行授权，不能从本次离线测试自动推出。
