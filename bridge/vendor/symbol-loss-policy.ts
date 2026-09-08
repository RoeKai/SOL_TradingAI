const MIN_PERCENT = 0.01;
const MAX_PERCENT = 100;

function assertFinite(label: string, value: number): void {
  if (!Number.isFinite(value)) {
    throw new Error(`${label}必须是有效数字`);
  }
}

/**
 * Parse the optional per-symbol lifecycle loss limit.
 *
 * Empty input deliberately means "disabled"; zero is not treated as disabled
 * because accepting it would make a newly opened position immediately eligible
 * for forced closure.
 */
export function parseOptionalMaxSymbolCumulativeLossPercent(
  value: string | number | null | undefined
): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && value.trim() === "") return null;

  const parsed = typeof value === "number" ? value : Number(value.trim());
  if (
    !Number.isFinite(parsed) ||
    parsed < MIN_PERCENT ||
    parsed > MAX_PERCENT
  ) {
    throw new Error("单币种最大累计亏损比例必须在 0.01% 到 100% 之间");
  }
  return Number(parsed.toFixed(4));
}

/**
 * Decimal columns are written as strings by the existing configuration path.
 */
export function normalizeOptionalMaxSymbolCumulativeLossPercent(
  value: string | number | null | undefined
): string | null {
  const parsed = parseOptionalMaxSymbolCumulativeLossPercent(value);
  return parsed === null ? null : parsed.toFixed(4);
}

export function calculateSymbolMaxLossAmount(
  equitySnapshot: number,
  maxLossPercent: number
): number {
  assertFinite("账户权益快照", equitySnapshot);
  if (equitySnapshot <= 0) {
    throw new Error("账户权益快照必须大于 0");
  }

  const normalizedPercent =
    parseOptionalMaxSymbolCumulativeLossPercent(maxLossPercent);
  if (normalizedPercent === null) {
    throw new Error("单币种最大累计亏损比例未启用");
  }
  return equitySnapshot * (normalizedPercent / 100);
}

export function calculateSymbolCumulativePnl(
  realizedPnl: number,
  unrealizedPnl: number
): number {
  assertFinite("已实现盈亏", realizedPnl);
  assertFinite("未实现盈亏", unrealizedPnl);
  return realizedPnl + unrealizedPnl;
}

export interface SymbolLossPolicyEvaluation {
  maxLossAmount: number;
  cumulativePnl: number;
  remainingUnrealizedLossCapacity: number;
  breached: boolean;
}

/**
 * Evaluate one account/config/source/symbol/direction lifecycle.
 *
 * The threshold is inclusive: when cumulative PnL reaches exactly the negative
 * loss budget, the position must be closed.
 */
export function evaluateSymbolLossPolicy(input: {
  equitySnapshot: number;
  maxLossPercent: number;
  realizedPnl: number;
  unrealizedPnl: number;
}): SymbolLossPolicyEvaluation {
  const maxLossAmount = calculateSymbolMaxLossAmount(
    input.equitySnapshot,
    input.maxLossPercent
  );
  const cumulativePnl = calculateSymbolCumulativePnl(
    input.realizedPnl,
    input.unrealizedPnl
  );

  return {
    maxLossAmount,
    cumulativePnl,
    // Example: 150U budget + 50U realized profit permits 200U remaining
    // unrealized loss; a -50U realized loss leaves 100U.
    remainingUnrealizedLossCapacity: maxLossAmount + input.realizedPnl,
    breached: cumulativePnl <= -maxLossAmount,
  };
}
