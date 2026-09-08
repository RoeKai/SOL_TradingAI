/**
 * 轻量结构化日志（P2-可观测性，零依赖）
 *
 * 目标：给日志加上 [时间 ISO] [级别] [模块] 前缀与可选结构化字段，
 * 让一笔信号(signalId/planId)可以在跨模块日志中被 grep 串联。
 * 不引入 pino 等新依赖（避免破坏现有安装/构建）；后续如迁移 pino，
 * 只需替换本文件实现，调用方不变。
 *
 * 用法：
 *   import { createLogger } from "./logger";
 *   const log = createLogger("GuardedOrder");
 *   log.info("下单确认", { signalId, exchange: "okx", ordId });
 *   log.warn("超时后查单确认成功", { clOrdId });
 */

export type LogLevel = "debug" | "info" | "warn" | "error";

const LEVEL_ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };



function fmtMeta(meta?: Record<string, unknown>): string {
  if (!meta) return "";
  try {
    const parts: string[] = [];
    for (const [k, v] of Object.entries(meta)) {
      if (v === undefined) continue;
      parts.push(`${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
    }
    return parts.length ? " " + parts.join(" ") : "";
  } catch {
    return "";
  }
}

export interface Logger {
  debug(msg: string, meta?: Record<string, unknown>): void;
  info(msg: string, meta?: Record<string, unknown>): void;
  warn(msg: string, meta?: Record<string, unknown>): void;
  error(msg: string, meta?: Record<string, unknown>): void;
}

export function createLogger(module: string, options: { minLevel: LogLevel }): Logger {
  const emit = (level: LogLevel, msg: string, meta?: Record<string, unknown>) => {
    if (LEVEL_ORDER[level] < LEVEL_ORDER[options.minLevel]) return;
    const line = `${new Date().toISOString()} [${level.toUpperCase()}] [${module}] ${msg}${fmtMeta(meta)}`;
    if (level === "error") console.error(line);
    else if (level === "warn") console.warn(line);
    else console.log(line);
  };
  return {
    debug: (m, x) => emit("debug", m, x),
    info: (m, x) => emit("info", m, x),
    warn: (m, x) => emit("warn", m, x),
    error: (m, x) => emit("error", m, x),
  };
}
