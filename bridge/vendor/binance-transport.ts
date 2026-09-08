// Generated exact declaration bodies from TradingSkill; see manifest.json.
import crypto from "node:crypto";
import {fetchWithRetry,withRetry} from "./safe-request";
interface ExchangeCredentials {apiKey:string;secretKey:string}
const BINANCE_FUTURES_URL = "https://fapi.binance.com";

let binanceIpBanUntilMs = 0;

let binanceServerTimeOffsetMs = 0;

let binanceServerTimeOffsetValidUntilMs = 0;

let binanceTimeSyncPromise: Promise<void> | null = null;

export function resetBinanceTimeOffsetForTests(): void {
  binanceServerTimeOffsetMs = 0;
  binanceServerTimeOffsetValidUntilMs = 0;
  binanceTimeSyncPromise = null;
}

export function parseBinanceBanUntil(message: unknown): number {
  const match = String(message || "").match(/banned until\s+(\d{10,})/i);
  const value = Number(match?.[1] || 0);
  return Number.isFinite(value) && value > Date.now() ? value : 0;
}

function sign(queryString: string, secret: string): string {
  return crypto.createHmac("sha256", secret).update(queryString).digest("hex");
}

function buildSignedUrl(path: string, params: Record<string, string>, creds: ExchangeCredentials): string {
  const timestamp = Math.trunc(Date.now() + binanceServerTimeOffsetMs).toString();
  params.timestamp = timestamp;
  params.recvWindow = "5000";
  const queryString = new URLSearchParams(params).toString();
  const signature = sign(queryString, creds.secretKey);
  return `${BINANCE_FUTURES_URL}${path}?${queryString}&signature=${signature}`;
}

async function syncBinanceServerTime(force = false): Promise<void> {
  if (!force && binanceServerTimeOffsetValidUntilMs > Date.now()) return;
  if (binanceTimeSyncPromise) return binanceTimeSyncPromise;

  binanceTimeSyncPromise = (async () => {
    const startedAt = Date.now();
    const res = await fetchWithRetry(
      `${BINANCE_FUTURES_URL}/fapi/v1/time`,
      { method: "GET" },
      {
        retries: 1,
        rateLimitKey: "binance",
        label: "binance server time",
      }
    );
    const body = await res.json();
    const completedAt = Date.now();
    const serverTime = Number(body?.serverTime);
    if (!Number.isFinite(serverTime) || serverTime <= 0) {
      throw new Error("Binance server time response is invalid");
    }
    // Signed writes must never be ahead of Binance. Using the response
    // completion time (plus a small behind-bias) is intentionally conservative:
    // being a few hundred milliseconds behind is covered by recvWindow, while
    // being more than one second ahead is rejected synchronously with -1021.
    // Midpoint compensation can overshoot under asymmetric proxy latency.
    binanceServerTimeOffsetMs = serverTime - completedAt - 250;
    binanceServerTimeOffsetValidUntilMs = completedAt + 5 * 60_000;
  })();

  try {
    await binanceTimeSyncPromise;
  } finally {
    binanceTimeSyncPromise = null;
  }
}

function getHeaders(creds: ExchangeCredentials) {
  return {
    "X-MBX-APIKEY": creds.apiKey,
    "Content-Type": "application/x-www-form-urlencoded",
  };
}

export async function request(
  creds: ExchangeCredentials,
  method: string,
  path: string,
  params: Record<string, string> = {},
  opts?: { retries?: number; beforeWriteAttempt?: () => Promise<void> },
): Promise<any> {
  if (binanceIpBanUntilMs > Date.now()) {
    throw new Error(`Binance IP cooldown active until ${binanceIpBanUntilMs}`);
  }
  const headers = getHeaders(creds);
  // 稳定性(P0)：超时+退避重试+限流。
  // 关键：Binance 签名含 timestamp/recvWindow(5s)，每次重试必须【重新生成签名 url】，
  // 否则重试时会报 timestamp 过期。故用 withRetry 包裹“重新签名 + 单次带超时 fetch”。
  //
  // P0-②：写操作（下单/撤单/algo 单，method !== GET）默认 retries=0 ——
  // 超时 ≠ 失败，盲重试会产生重复订单；下单的"超时后确认"由 placeOrder
  // 按 origClientOrderId 查单完成。读操作保持 3 次重试。
  const isWrite = method !== "GET";
  const retries = opts?.retries ?? (isWrite ? 0 : 3);
  const execute = () => withRetry(async () => {
    const url = buildSignedUrl(path, { ...params }, creds); // 每次重新签名(新 timestamp)
    const res = await fetchWithRetry(url, { method, headers }, {
      retries: 0,
      rateLimitKey: "binance",
      label: `binance ${method} ${path}`,
      beforeAttempt: isWrite ? opts?.beforeWriteAttempt : undefined,
    });
    const body = await res.json();
    // 可重试的服务端错误：服务忙(-1001)、超时(-1007)。
    // 限频(-1003) 必须立即返回给上层冷却/调度，不得在同一请求内放大重试。
    if (body && typeof body.code === "number" && body.code !== 200) {
      const c = body.code;
      const reportedBanUntil = parseBinanceBanUntil(body.msg);
      if (reportedBanUntil > binanceIpBanUntilMs) binanceIpBanUntilMs = reportedBanUntil;
      // An explicit IP ban must never be retried. Retrying a banned request can
      // extend the ban window and starve protective position monitoring.
      const explicitlyBanned = /banned until|way too many requests/i.test(String(body.msg || ""));
      const retriable = !explicitlyBanned && (c === -1001 || c === -1007 || c === -1000);
      const err: any = new Error(body.msg || `Binance API error: ${c}`);
      (err as any).binanceCode = c;
      if (retriable && !isWrite) err.message = `network ${err.message}`; // 仅读操作打可重试标记
      throw err;
    }
    return body;
  }, { rateLimitKey: undefined, label: `binance ${method} ${path}`, retries });

  try {
    return await execute();
  } catch (error: any) {
    if (Number(error?.binanceCode) !== -1021) throw error;
    // Binance explicitly rejected the request before matching-engine
    // acceptance, so replaying the same business request is safe. Prefer the
    // host's NTP clock first: a stale/cached /time response must not poison the
    // shared offset of this long-lived process and break every later signed
    // request. If the host clock is genuinely skewed, fall back to an explicit
    // server-time sync after the direct-clock retry is also rejected.
    binanceServerTimeOffsetMs = 0;
    binanceServerTimeOffsetValidUntilMs = 0;
    try {
      return await execute();
    } catch (directClockError: any) {
      if (Number(directClockError?.binanceCode) !== -1021) throw directClockError;
      await syncBinanceServerTime(true);
      return execute();
    }
  }
}

function decimalsFromIncrement(increment: string): number {
  const normalized = String(increment || "").toLowerCase();
  if (normalized.includes("e-")) return Number(normalized.split("e-")[1] || 0);
  const dot = normalized.indexOf(".");
  if (dot < 0) return 0;
  return normalized.slice(dot + 1).replace(/0+$/, "").length;
}

export function floorBinanceQuantityToStep(
  quantity: number,
  stepSize: string
): string {
  const step = Number(stepSize);
  if (!(quantity > 0) || !(step > 0)) return "0";
  const precision = decimalsFromIncrement(stepSize);
  const units = Math.floor(
    (quantity + Number.EPSILON * Math.max(1, quantity)) / step
  );
  if (!(units > 0)) return "0";
  return (units * step).toFixed(precision);
}

export function roundBinanceStopPriceToTick(
  price: number,
  tickSize: string,
  positionSide: "long" | "short",
): string {
  const tick = Number(tickSize);
  if (!(price > 0) || !(tick > 0)) return "0";
  const precision = decimalsFromIncrement(tickSize);
  const rawUnits = price / tick;
  const units =
    positionSide === "long"
      ? Math.ceil(rawUnits - 1e-10)
      : Math.floor(rawUnits + 1e-10);
  if (!(units > 0)) return "0";
  return (units * tick).toFixed(precision);
}
