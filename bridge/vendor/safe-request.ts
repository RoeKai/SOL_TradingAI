/**
 * 稳定性基础设施：可靠的对外 HTTP 调用封装（P0-①）
 * 提供：超时控制 + 指数退避重试(仅对可重试错误) + 每交易所限流排队。
 *
 * 设计原则：
 *  - 只对「网络错误 / 超时 / 5xx / 429 限频」重试；对业务错误(4xx/余额不足/参数非法)不重试，直接抛出。
 *  - 重试带指数退避 + 抖动，避免雪崩与同步重试风暴。
 *  - 限流：每个 key(如交易所名) 维护最小调用间隔，串行排队，避免触发交易所 429。
 *  - 不改变调用方的返回语义：成功返回 Response，失败抛出最后一次错误。
 */

export interface SafeRequestOptions {
  timeoutMs?: number;      // 单次请求超时，默认 8000ms
  retries?: number;        // 最大重试次数(不含首次)，默认 3
  baseDelayMs?: number;    // 退避基准，默认 400ms
  maxDelayMs?: number;     // 退避上限，默认 4000ms
  rateLimitKey?: string;   // 限流分组键(如 "binance"/"okx"/"hyperliquid")
  label?: string;          // 日志标识
  /** Runs after rate-limit waiting and immediately before each actual fetch. */
  beforeAttempt?: () => Promise<void>;
}

// ===== 熔断器 Circuit Breaker：按 rateLimitKey(交易所) × 读写方向 隔离故障域 =====
// R-08a 修复内容：
// 1) 读写分域——熔断 key 追加 :r / :w 后缀。监控/行情读请求的失败风暴不再阻断
//    平仓、止损等写请求（旧行为：一个坏的只读端点会把整个交易所的下单一起熔断 30s）。
// 2) 半开单探测——冷却期结束后只放行一个探测请求（probeInFlight CAS），其余并发
//    继续快速失败；旧实现把 halfOpen 置 true 后放行全部并发，且 openedAt 从不清零，
//    熔断在 30 秒后实际退化为完全无保护。
// 3) 429/418 计入熔断失败（见 fetchWithRetry），持续限频不再反向复位失败计数。
const CB_FAIL_THRESHOLD = 6;       // 连续失败次数阈值
const CB_OPEN_MS = 30 * 1000;      // 熔断打开持续时间
type CBState = { failures: number; openedAt: number; halfOpen: boolean; probeInFlight: boolean };
const breakers: Record<string, CBState> = {};
// 熔断状态变化回调(供告警接入，避免 safe-request 直接依赖 alert 模块)
let onBreakerChange: ((key: string, open: boolean, info: string) => void) | null = null;
export function setBreakerListener(fn: (key: string, open: boolean, info: string) => void) { onBreakerChange = fn; }

/** 写请求(POST/PUT/DELETE)与读请求(GET等)使用独立熔断域。 */
export function breakerKeyFor(rateLimitKey: string, method?: string): string {
  const m = String(method || "GET").toUpperCase();
  const isWrite = m === "POST" || m === "PUT" || m === "DELETE" || m === "PATCH";
  return `${rateLimitKey}:${isWrite ? "w" : "r"}`;
}

function cbGet(key: string): CBState {
  if (!breakers[key]) breakers[key] = { failures: 0, openedAt: 0, halfOpen: false, probeInFlight: false };
  return breakers[key];
}
/** 返回是否应该“快速失败”(熔断打开且未到冷却期；半开期只放一个探测) */
function cbShouldBlock(key: string): boolean {
  const s = cbGet(key);
  if (s.openedAt === 0) return false;
  const elapsed = Date.now() - s.openedAt;
  if (elapsed >= CB_OPEN_MS) {
    if (s.probeInFlight) return true;       // 半开期已有探测在途，其余继续快速失败
    s.probeInFlight = true;                 // CAS：本请求成为唯一探测
    s.halfOpen = true;
    return false;
  }
  return true;
}
function cbOnSuccess(key: string): void {
  const s = cbGet(key);
  if (s.openedAt !== 0 || s.failures > 0) {
    if (onBreakerChange && s.openedAt !== 0) onBreakerChange(key, false, "探测成功，熔断关闭");
  }
  s.failures = 0; s.openedAt = 0; s.halfOpen = false; s.probeInFlight = false;
}
function cbOnFailure(key: string): void {
  const s = cbGet(key);
  s.failures++;
  if (s.halfOpen) { // 半开探测又失败 → 重新打开(冷却期重新计时)
    s.openedAt = Date.now(); s.halfOpen = false; s.probeInFlight = false;
    if (onBreakerChange) onBreakerChange(key, true, "半开探测失败，熔断重新打开");
    return;
  }
  if (s.failures >= CB_FAIL_THRESHOLD && s.openedAt === 0) {
    s.openedAt = Date.now();
    if (onBreakerChange) onBreakerChange(key, true, `连续${s.failures}次失败，熔断打开${CB_OPEN_MS / 1000}s`);
  }
}
/**
 * 查询熔断状态。兼容旧调用方：传入裸交易所名(如 "binance")时聚合读写两个
 * 子域——任一打开即视为打开，failures 取两者较大值。
 */
export function getBreakerState(key: string): { open: boolean; failures: number; readOpen?: boolean; writeOpen?: boolean } {
  const isOpen = (s: CBState) => s.openedAt !== 0 && !s.halfOpen;
  if (breakers[key]) {
    const s = breakers[key];
    return { open: isOpen(s), failures: s.failures };
  }
  const r = breakers[`${key}:r`];
  const w = breakers[`${key}:w`];
  const rOpen = r ? isOpen(r) : false;
  const wOpen = w ? isOpen(w) : false;
  return {
    open: rOpen || wOpen,
    failures: Math.max(r?.failures ?? 0, w?.failures ?? 0),
    readOpen: rOpen,
    writeOpen: wOpen,
  };
}

// ===== 依赖延迟统计(按 key 保留最近 N 次耗时与成败) =====
type LatStat = { samples: number[]; ok: number; fail: number };
const latStats: Record<string, LatStat> = {};
const LAT_WINDOW = 50;
function recordLatency(key: string, ms: number, ok: boolean): void {
  if (!key) return;
  const s = latStats[key] || (latStats[key] = { samples: [], ok: 0, fail: 0 });
  s.samples.push(ms); if (s.samples.length > LAT_WINDOW) s.samples.shift();
  if (ok) s.ok++; else s.fail++;
}
export function getLatencyStats(): Array<{ key: string; avgMs: number; p95Ms: number; ok: number; fail: number; failRate: number }> {
  return Object.keys(latStats).map((key) => {
    const s = latStats[key];
    const arr = s.samples.slice().sort((a, b) => a - b);
    const avg = arr.length ? Math.round(arr.reduce((x, y) => x + y, 0) / arr.length) : 0;
    const p95 = arr.length ? arr[Math.min(arr.length - 1, Math.floor(arr.length * 0.95))] : 0;
    const total = s.ok + s.fail;
    return { key, avgMs: avg, p95Ms: Math.round(p95), ok: s.ok, fail: s.fail, failRate: total ? Math.round((s.fail / total) * 1000) / 10 : 0 };
  });
}

const DEFAULTS = {
  timeoutMs: 8000,
  retries: 3,
  baseDelayMs: 400,
  maxDelayMs: 4000,
};

// ===== 每交易所限流：最小调用间隔(ms) =====
/**
 * F5（需求 13）：交易所配额用量计量。
 *
 * Binance 在每个响应头里回传本分钟已用权重（`x-mbx-used-weight-1m`）
 * 与已用下单数（`x-mbx-order-count-1m`）。这两项此前**一次都没被读过**，
 * 系统只能等被 429 拒绝后才知道自己超了。
 *
 * 现在：成功响应就记录用量；`passRateLimit` 在逼近上限时主动加大间隔。
 * 这是"被动挨打"到"主动退让"的关键一步——它不改变任何业务语义，
 * 只是让限流从"按次数猜"变成"按交易所告诉我们的真实用量"。
 */
export interface ExchangeQuotaUsage {
  usedWeight1m: number | null;
  orderCount1m: number | null;
  observedAt: number;
}

const quotaUsage = new Map<string, ExchangeQuotaUsage>();

/** Binance 期货默认每分钟 2400 权重；留 20% 余量后开始退让。 */
export let BINANCE_WEIGHT_LIMIT_1M = 2400;
export function configureRequestRuntime(options: { weightLimit1m: number }): void {
  if (!Number.isInteger(options.weightLimit1m) || options.weightLimit1m < 1 || options.weightLimit1m > 2400) throw new Error("Invalid explicit weight limit");
  BINANCE_WEIGHT_LIMIT_1M = options.weightLimit1m;
}
export const QUOTA_SOFT_LIMIT_RATIO = 0.8;

function headerNumber(res: any, name: string): number | null {
  try {
    const raw = res?.headers?.get?.(name);
    if (raw === null || raw === undefined || raw === "") return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
  } catch {
    return null;
  }
}

export function recordExchangeQuotaUsage(rateLimitKey: string, res: any): void {
  const usedWeight1m = headerNumber(res, "x-mbx-used-weight-1m");
  const orderCount1m = headerNumber(res, "x-mbx-order-count-1m");
  if (usedWeight1m === null && orderCount1m === null) return;
  quotaUsage.set(rateLimitKey, {
    usedWeight1m,
    orderCount1m,
    observedAt: Date.now(),
  });
}

export function getExchangeQuotaUsage(
  rateLimitKey: string,
): ExchangeQuotaUsage | null {
  const usage = quotaUsage.get(rateLimitKey);
  if (!usage) return null;
  // 观测超过 70 秒就当作过期：权重是按分钟窗口滚动的
  if (Date.now() - usage.observedAt > 70_000) return null;
  return usage;
}

/**
 * 逼近权重上限时返回额外的等待毫秒数。
 * 刻意做成**渐进**而不是硬闸门：突然完全停止会让紧急平仓也发不出去，
 * 而逐步拉长间隔能在保住配额的同时让关键请求仍有机会通过。
 */
/**
 * 退让的**硬上限**。
 *
 * J2（复核发现）：原实现在 `ratio >= 1` 时返回"等到下一个分钟窗口"，
 * 最长约 60 秒。而所有 Binance 请求共用一条串行队列、没有优先级——
 * 排在几个监控读之后的**紧急平仓**会被推迟到分钟级。
 * 高杠杆下这段时间足以把"按阈值止损"变成"爆仓"。
 *
 * 限流的目的是少挨 429，不是替交易所拒绝我们自己的止损单。
 * 宁可吃一个 429（它会被熔断器和重试正确处理），也不能把平仓压住一分钟。
 */
export const QUOTA_BACKOFF_MAX_MS = 2_000;

export function quotaBackoffMs(rateLimitKey: string, now = Date.now()): number {
  const usage = getExchangeQuotaUsage(rateLimitKey);
  if (!usage || usage.usedWeight1m === null) return 0;
  const ratio = usage.usedWeight1m / Math.max(1, BINANCE_WEIGHT_LIMIT_1M);
  if (ratio < QUOTA_SOFT_LIMIT_RATIO) return 0;
  if (ratio >= 1) {
    // 已经超了：退让到上限，但绝不无限等待下一个分钟窗口。
    // 权重窗口是按交易所墙钟分钟滚动的，我们的观测时刻与它并不对齐，
    // 按观测时刻推算"还剩多久"本身就不准（原实现还会在 elapsed 跨过
    // 60 秒时跳回一整分钟，越旧的观测反而等越久）。
    return QUOTA_BACKOFF_MAX_MS;
  }
  // 0.8 → 0ms，1.0 → 上限，线性放大
  const over = (ratio - QUOTA_SOFT_LIMIT_RATIO) / (1 - QUOTA_SOFT_LIMIT_RATIO);
  return Math.min(QUOTA_BACKOFF_MAX_MS, Math.round(over * QUOTA_BACKOFF_MAX_MS));
}

export function __resetExchangeQuotaUsageForTests(): void {
  quotaUsage.clear();
}

const MIN_INTERVAL_MS: Record<string, number> = {
  binance: 120,      // ~8 req/s 保守
  okx: 120,
  bybit: 120,        // R-08a：此前未定义，落到 default 100ms，偏激进
  bitget: 120,       // R-08a：同上
  hyperliquid: 150,
  default: 100,
};
// 记录每个 key 上一次调用时间，串行排队
const lastCallAt: Record<string, number> = {};
const queueTail: Record<string, Promise<void>> = {};

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** 通过限流闸门：保证同 key 的调用间隔 >= MIN_INTERVAL_MS */
async function passRateLimit(key: string): Promise<void> {
  const k = key || "default";
  const interval = MIN_INTERVAL_MS[k] ?? MIN_INTERVAL_MS.default;
  // 串行链：每个调用排在上一个之后
  const prev = queueTail[k] || Promise.resolve();
  let release!: () => void;
  const mine = new Promise<void>((r) => (release = r));
  queueTail[k] = prev.then(() => mine);
  await prev;
  const now = Date.now();
  // F5：在固定间隔之上叠加"权重逼近上限时的主动退让"。
  // 交易所回传的真实用量比我们按次数的估算准得多。
  const effectiveInterval = interval + quotaBackoffMs(k, now);
  const wait = Math.max(0, (lastCallAt[k] || 0) + effectiveInterval - now);
  if (wait > 0) await sleep(wait);
  lastCallAt[k] = Date.now();
  // 立即释放让下一个排队者进入(间隔已由 lastCallAt 控制)
  release();
}

/** 判断错误/响应是否可重试 */
function isRetriableStatus(status: number): boolean {
  return status === 429 || status === 408 || (status >= 500 && status <= 599);
}
function isRetriableError(err: any): boolean {
  const msg = String(err?.message || err || "").toLowerCase();
  return (
    err?.name === "AbortError" ||           // 超时
    msg.includes("timeout") ||
    msg.includes("network") ||
    msg.includes("fetch failed") ||
    msg.includes("econnreset") ||
    msg.includes("econnrefused") ||
    msg.includes("etimedout") ||
    msg.includes("socket hang up") ||
    msg.includes("eai_again")
  );
}

function backoffDelay(attempt: number, base: number, max: number): number {
  const exp = Math.min(max, base * Math.pow(2, attempt));
  const jitter = Math.random() * exp * 0.3; // 30% 抖动
  return Math.floor(exp * 0.7 + jitter);
}

/**
 * 可靠 fetch：超时 + 退避重试(仅可重试错误) + 限流。
 * 成功(含业务 4xx，由调用方解析 body 判定业务错误)返回 Response；
 * 仅在「网络/超时/5xx/429」重试，重试用尽抛出最后错误。
 */
export async function fetchWithRetry(
  url: string,
  init: RequestInit = {},
  options: SafeRequestOptions = {}
): Promise<Response> {
  const timeoutMs = options.timeoutMs ?? DEFAULTS.timeoutMs;
  const retries = options.retries ?? DEFAULTS.retries;
  const baseDelay = options.baseDelayMs ?? DEFAULTS.baseDelayMs;
  const maxDelay = options.maxDelayMs ?? DEFAULTS.maxDelayMs;
  const label = options.label || options.rateLimitKey || "http";

  // R-08a：熔断域 = 交易所 × 读写方向。写路径(下单/撤单)不被读风暴拖垮。
  const cbKey = options.rateLimitKey
    ? breakerKeyFor(options.rateLimitKey, (init as any)?.method)
    : undefined;
  // 熔断检查：若该域熔断打开且未到冷却期，直接快速失败(不发请求)
  if (cbKey && cbShouldBlock(cbKey)) {
    throw new Error(`[safeRequest] ${label} 熔断打开中(${cbKey})，快速失败`);
  }

  let lastErr: any = null;
  const _t0 = Date.now();
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (options.rateLimitKey) {
      try { await passRateLimit(options.rateLimitKey); } catch { /* 限流失败不阻断 */ }
    }
    // Authorization failures are intentional local blocks, not transport
    // failures, so run this outside the fetch/circuit-breaker catch.
    await options.beforeAttempt?.();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...init, signal: controller.signal });
      clearTimeout(timer);
      // Real fetch responses always expose status. Keeping a defensive 200
      // fallback also makes proxy/polyfill responses safe to consume.
      const responseStatus = typeof (res as any)?.status === "number" ? (res as any).status : 200;
      // 5xx/429/408 → 可重试
      if (isRetriableStatus(responseStatus) && attempt < retries) {
        const delay = backoffDelay(attempt, baseDelay, maxDelay);
        console.warn(`[safeRequest] ${label} HTTP ${responseStatus}，第${attempt + 1}次重试(延迟${delay}ms)`);
        await sleep(delay);
        continue;
      }
      // R-08a：重试用尽后的 5xx、429(限频)、418(IP ban) 都计入熔断失败——
      // 被交易所限频正是最需要集体退避的场景，旧行为(走 cbOnSuccess)不但不
      // 熔断还会复位已累积的失败计数。其余(含 4xx 业务响应)视为连通成功。
      if (cbKey) {
        if (responseStatus >= 500 || responseStatus === 429 || responseStatus === 418) cbOnFailure(cbKey);
        else cbOnSuccess(cbKey);
      }
      if (cbKey) recordLatency(cbKey, Date.now() - _t0, responseStatus < 500);
      // F5（需求 13）：读取交易所回传的权重用量。
      // 此前系统对"还剩多少配额"完全无感知，只能被 429 打脸后才反应；
      // 而 Binance 各端点权重差异极大（下单 1、账户 5、全量挂单 40），
      // 按"每 120ms 一次"这种按次数的限流对权重严重失真。
      if (options.rateLimitKey) {
        recordExchangeQuotaUsage(options.rateLimitKey, res);
      }
      return res;
    } catch (err: any) {
      clearTimeout(timer);
      lastErr = err;
      if (isRetriableError(err) && attempt < retries) {
        const delay = backoffDelay(attempt, baseDelay, maxDelay);
        console.warn(`[safeRequest] ${label} ${err?.name === "AbortError" ? "超时" : "网络错误"}(${err?.message})，第${attempt + 1}次重试(延迟${delay}ms)`);
        await sleep(delay);
        continue;
      }
      if (cbKey) { cbOnFailure(cbKey); recordLatency(cbKey, Date.now() - _t0, false); } // 重试用尽的网络/超时失败
      throw err;
    }
  }
  if (cbKey) { cbOnFailure(cbKey); recordLatency(cbKey, Date.now() - _t0, false); }
  throw lastErr || new Error(`[safeRequest] ${label} 重试${retries}次后仍失败`);
}

/**
 * 通用重试包装器：用于非 fetch 的可能瞬时失败操作(如 HL SDK 下单)。
 * 仅对可重试错误重试。
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  options: { retries?: number; baseDelayMs?: number; maxDelayMs?: number; label?: string; rateLimitKey?: string } = {}
): Promise<T> {
  const retries = options.retries ?? DEFAULTS.retries;
  const baseDelay = options.baseDelayMs ?? DEFAULTS.baseDelayMs;
  const maxDelay = options.maxDelayMs ?? DEFAULTS.maxDelayMs;
  const label = options.label || "op";
  let lastErr: any = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (options.rateLimitKey) { try { await passRateLimit(options.rateLimitKey); } catch {} }
    try {
      return await fn();
    } catch (err: any) {
      lastErr = err;
      if (isRetriableError(err) && attempt < retries) {
        const delay = backoffDelay(attempt, baseDelay, maxDelay);
        console.warn(`[withRetry] ${label} 可重试错误(${err?.message})，第${attempt + 1}次重试(延迟${delay}ms)`);
        await sleep(delay);
        continue;
      }
      throw err;
    }
  }
  throw lastErr || new Error(`[withRetry] ${label} 重试${retries}次后仍失败`);
}
