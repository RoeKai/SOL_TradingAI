# Isolated TradingSkill execution bridge

**Isolation-only work in progress — production live access is sealed.** Manual YAML live flags are necessary for future live use but are not sufficient in this release. Startup in live mode and every direct production private transport call fail with `LIVE_ISOLATION_NOT_ACCEPTED`. This cannot be unlocked by an environment variable, RPC or the policy marker. Independent exchange account identity, infrastructure isolation and native STOP child-order ownership acceptance remain incomplete. Do not deploy or describe this module as live-ready.

This is **not** the original copy-trading backend. Deploy it only beside the new Python service on the new server. It has its own `.env`, YAML config, submission journal and account log; no MySQL, original daemon or original portfolio database is imported.

From this folder: `npm ci && npm run check && npm test && npm run build && npm start` (Node 22+). Set a random, at least 32-character `SOL_BRIDGE_TOKEN` in `../.env`. Only localhost `127.0.0.1:8766` is bound. Do not expose this port through the unified management website or reverse proxy. The dashboard only connects to the separate Python control plane.

`GET /health` is unauthenticated and contains no account information. `POST /rpc` requires `Authorization: Bearer <SOL_BRIDGE_TOKEN>` and `{ "operation": "positions", "payload": {} }`.

In `dry_run: true`, **every private RPC including read-only account/position/order queries is rejected before any transport call** with `PRIVATE_RPC_DISABLED`; only public exchange rules are accessible. A paper-mode bridge has a public-only transport and does not construct a private Binance client. No test-order endpoint exists.

Mock-only regression paths require, on **every actual signed attempt**, strict YAML booleans `dry_run: false`, `live.enabled: true`, the exact confirmation string and `dedicated_account_confirmed: true`. Missing, string-coerced, corrupt or changed-in-flight configuration fails closed. Production retains the additional non-configurable live seal above. It never changes position mode or imports the original system's credentials.

## Reuse with isolation

`vendor/manifest.json` records original paths, SHA-256 of working-tree sources, output snapshots and the explicit patch manifest. `vendor/patches.json` records isolation-only changes to remove inherited logger/quota environment values and inject options explicitly. `npm run vendor:refresh` explicitly extracts audited transport declarations from `server/exchange/binance-adapter.ts` and applies these audited patches. It does not copy environment files. Normal builds verify already-fixed snapshots without needing the old repository. `scripts/check-imports.mjs` recursively allows only exact new source files, vendored utilities, Node built-ins and the environment-independent YAML browser ESM parser; dynamic loaders, external packages, parent imports and environment access are rejected. The final bundle is checked again. `dist/import-manifest.json` records every input and bundle SHA-256. Refreshing a snapshot requires review and tests; builds do not silently refresh it.

The old `BinanceAdapter` class is intentionally not imported: its `guarded-order → risk-gate` dependency reaches the original database. The new Python risk engine owns daily limits, consecutive-loss state, duplicate-entry reservation and strategy approval. The bridge independently checks fresh account exposure, stop direction, loss budget including fees/slippage, margin, leverage, minimums, account mode and small-capital limit before entry. Legacy pure risk arithmetic is reused; legacy shared risk-state is not.

## RPC contract

Read operations: `account`, `positions`, `position_mode`, `rules`, `open_orders`, `open_stops`, `order`, `query_stop`, `trades`.

Only `rules` is public. Query/cancel operations require an original client ID already owned by this instance's submission journal and the matching order/STOP kind. Merely sharing a `sol` prefix is not authority. `trades` requires `client_id` (not a bare exchange order ID or historical time range); the bridge queries that original owned order, then requests only its trades. A native STOP child order is **not** automatically adopted; that unaccepted ownership path remains a separate live blocker.

Write operations: `place_order`, `cancel_order`, `stop`, `cancel_stop`, `leverage`. Arbitrary paths and arbitrary symbols are never accepted. Order IDs must be deterministic `sol`-prefixed strings up to 36 characters.

Entry payload: `symbol`, `side` (`BUY`/`SELL`), `type` (`MARKET`/`LIMIT`), `quantity`, `price` for LIMIT, `client_id`, and `risk_context: {entry_price, stop_price, max_loss_usdt, equity, leverage}`. Exit payload uses `reduce_only: true`. A below-minimum exit is never increased to meet an entry minimum. Partial take-profit is a normal reduce-only LIMIT/MARKET order.

Stop payload: `symbol`, close `side`, exact `quantity`, `stop_price`, `client_id`. It uses the current Binance `/fapi/v1/algoOrder` `STOP_MARKET`, `reduceOnly=true`, `positionSide=BOTH`, `workingType=MARK_PRICE`; never `closePosition=true`. Stop prices round toward entry, not toward greater loss. Python must confirm the stop by original `clientAlgoId` and immediately request a reduce-only market close if protective placement cannot be confirmed.

Success: `{ok:true,result:<raw Binance response>}`. Failure: `{ok:false,error:{code,message,ambiguous}}`. A possible POST is permanently reserved in a fsync'd journal before dispatch. An ambiguous timeout is `UNKNOWN`; query the **same original client ID**. No blind POST retry, no automatic new ID, no duplicate entry on restart. The journal is not a trading history replacement: Python persists execution state separately. Never delete the journal to re-enable an uncertain order.

## Private files and instance state

The deployment root is derived from this module's installed file path, never from configuration or environment. A matching `../isolation-policy.json` marker is required. The explicit `.env` parser never searches parent directories, reads host credentials or expands `${...}`; interpolation is rejected. Owner, mode, canonical path, symlink, hardlink and traversal checks run before I/O, with `O_NOFOLLOW` file opens. `.env` and output state require private permissions. The deployment root itself must be an independently trusted, same-owner directory; separate OS user and filesystem/network controls still need real infrastructure acceptance.

State is partitioned as `trades/paper/` vs `trades/live/` and `logs/paper/` vs `logs/live/`. Each mode has `bridge-identity.json` containing application, schema version, `config.instance_id` and mode. Every journal/log record carries the same identity. Foreign state, unclaimed pre-existing files, wrong mode/schema/instance or linked state paths are rejected before new writes; no old single-file journal is auto-imported. The checked-in default is `instance_id: sol-ai-local`; deployments must choose their own reviewed identity.

Official endpoint reference, checked 2026-09-09: [Binance USD-M trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).

## Validation boundary

Tests inject fake exchange transport/fetch and do not contact Binance, Telegram or the old backend. Passing tests demonstrates local logic, not actual live exchange acceptance or guaranteed stop execution through gaps/outages.
