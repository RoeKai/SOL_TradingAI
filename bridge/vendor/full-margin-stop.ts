import { parseOptionalMaxSymbolCumulativeLossPercent } from "./symbol-loss-policy";

const MIN_PERCENT = 0.01;
const MAX_PERCENT = 100;

export type FullMarginStopSide = "long" | "short";

export interface FullMarginStopInput {
  side: FullMarginStopSide;
  entryPrice: number;
  positionQty: number;
  pnlQuantityMultiplier: number;
  accountEquitySnapshot: number;
  stopPercent: number;
  /** Current mark, used only to validate which side a liquidation boundary is on. */
  markPrice?: number | null;
  /** Exchange liquidation boundary for the current combined position. */
  liquidationPrice?: number | null;
}

export interface FullMarginStopResult {
  maxLossAmount: number;
  effectiveQty: number;
  priceDistance: number;
  rawStopPrice: number;
}

export type ExecutableFullMarginStopResolution =
  | (FullMarginStopResult & {
      nativeStopRequired: true;
      stopPrice: number;
    })
  | Extract<FullMarginStopResolution, { nativeStopRequired: false }>;

export type FullMarginStopResolution =
  | (FullMarginStopResult & {
      nativeStopRequired: true;
    })
  | {
      nativeStopRequired: false;
      maxLossAmount: number;
      effectiveQty: number;
      priceDistance: number;
      rawStopPrice: null;
      reason: string;
    };

function decimalPlaces(value: number): number {
  const text = value.toString().toLowerCase();
  if (text.includes("e-")) {
    return Number(text.split("e-")[1] ?? 0);
  }
  return (text.split(".")[1] ?? "").length;
}

function assertPositive(label: string, value: number): void {
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`${label}必须大于 0`);
  }
}

/**
 * Optional stop percentage whose base is the complete cross-account equity,
 * not position margin ROI. Empty means disabled.
 */
export function parseOptionalFullMarginStopPercent(
  value: string | number | null | undefined,
): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && value.trim() === "") return null;

  const parsed = typeof value === "number" ? value : Number(value.trim());
  if (
    !Number.isFinite(parsed)
    || parsed < MIN_PERCENT
    || parsed > MAX_PERCENT
  ) {
    throw new Error("止损位全仓保证金百分比必须在 0.01% 到 100% 之间");
  }
  return Number(parsed.toFixed(4));
}

export function normalizeOptionalFullMarginStopPercent(
  value: string | number | null | undefined,
): string | null {
  const parsed = parseOptionalFullMarginStopPercent(value);
  return parsed === null ? null : parsed.toFixed(2);
}

/**
 * The current-position full-margin stop and the lifecycle cumulative-loss
 * budget are intentionally mutually exclusive.
 */
export function shouldAttachFullMarginStop(input: {
  fullMarginStopPercent: string | number | null | undefined;
  maxSymbolCumulativeLossPercent: string | number | null | undefined;
}): boolean {
  if (
    parseOptionalMaxSymbolCumulativeLossPercent(
      input.maxSymbolCumulativeLossPercent,
    ) !== null
  ) {
    return false;
  }
  return (
    parseOptionalFullMarginStopPercent(input.fullMarginStopPercent) !== null
  );
}

/**
 * Convert a full-account equity loss budget into the trigger price for the
 * position being opened.
 *
 * Example: account equity 1000U and 30% means a fixed 300U loss budget.
 * Leverage is deliberately absent from this formula.
 */
export function resolveFullMarginStop(
  input: FullMarginStopInput,
): FullMarginStopResolution {
  assertPositive("开仓参考价格", input.entryPrice);
  assertPositive("开仓数量", input.positionQty);
  assertPositive("盈亏数量换算系数", input.pnlQuantityMultiplier);
  assertPositive("全仓账户权益快照", input.accountEquitySnapshot);

  const stopPercent = parseOptionalFullMarginStopPercent(input.stopPercent);
  if (stopPercent === null) {
    throw new Error("止损位全仓保证金百分比未启用");
  }

  const maxLossAmount =
    input.accountEquitySnapshot * (stopPercent / 100);
  const effectiveQty = input.positionQty * input.pnlQuantityMultiplier;
  assertPositive("有效盈亏数量", effectiveQty);

  const priceDistance = maxLossAmount / effectiveQty;
  const rawStopPrice =
    input.side === "long"
      ? input.entryPrice - priceDistance
      : input.entryPrice + priceDistance;
  if (!Number.isFinite(rawStopPrice)) {
    throw new Error("按全仓保证金亏损预算计算出的止损价格无效");
  }
  if (input.side === "long" && rawStopPrice <= 0) {
    return {
      nativeStopRequired: false,
      maxLossAmount,
      effectiveQty,
      priceDistance,
      rawStopPrice: null,
      reason:
        "小仓暂不可挂：当前多仓即使价格跌至 0，最大理论亏损仍不超过冻结风险预算；" +
        "后续加仓、减仓或成交均价变化时自动重算",
    };
  }
  if (rawStopPrice <= 0) {
    throw new Error(
      "按全仓保证金亏损预算计算出的止损价格无效；该仓位名义价值小于配置的亏损预算",
    );
  }

  const markPrice = Number(input.markPrice);
  const liquidationPrice = Number(input.liquidationPrice);
  const liquidationReferencePrice =
    Number.isFinite(markPrice) && markPrice > 0
      ? markPrice
      : input.entryPrice;
  const liquidationOnLossSide =
    input.side === "long"
      ? liquidationPrice <= liquidationReferencePrice
      : liquidationPrice >= liquidationReferencePrice;
  const stopNotBeforeLiquidation =
    input.side === "long"
      ? rawStopPrice <= liquidationPrice
      : rawStopPrice >= liquidationPrice;
  if (
    Number.isFinite(liquidationPrice)
    && liquidationPrice > 0
    && liquidationOnLossSide
    && stopNotBeforeLiquidation
  ) {
    return {
      nativeStopRequired: false,
      maxLossAmount,
      effectiveQty,
      priceDistance,
      rawStopPrice: null,
      reason:
        "小仓暂不可挂：按当前冻结预算计算出的止损价不早于有效强平边界；" +
        "仓位数量、成交均价或强平边界变化后自动重算",
    };
  }

  return {
    nativeStopRequired: true,
    maxLossAmount,
    effectiveQty,
    priceDistance,
    rawStopPrice,
  };
}

export function computeFullMarginStop(
  input: FullMarginStopInput,
): FullMarginStopResult {
  const resolved = resolveFullMarginStop(input);
  if (!resolved.nativeStopRequired) {
    throw new Error(
      "按全仓保证金亏损预算计算出的止损价格无效；该仓位名义价值小于配置的亏损预算",
    );
  }
  return {
    maxLossAmount: resolved.maxLossAmount,
    effectiveQty: resolved.effectiveQty,
    priceDistance: resolved.priceDistance,
    rawStopPrice: resolved.rawStopPrice,
  };
}

/**
 * Round toward the entry side so exchange tick normalization cannot make the
 * configured price-loss budget wider.
 */
export function roundFullMarginStopPriceToTick(input: {
  side: FullMarginStopSide;
  rawStopPrice: number;
  entryPrice: number;
  tickSize: number;
}): number {
  assertPositive("止损原始价格", input.rawStopPrice);
  assertPositive("开仓均价", input.entryPrice);
  assertPositive("价格最小变动单位", input.tickSize);

  const units =
    input.side === "long"
      ? Math.ceil(input.rawStopPrice / input.tickSize - 1e-10)
      : Math.floor(input.rawStopPrice / input.tickSize + 1e-10);
  const precision = Math.min(12, decimalPlaces(input.tickSize));
  const stopPrice = Number((units * input.tickSize).toFixed(precision));

  if (
    !Number.isFinite(stopPrice)
    || stopPrice <= 0
    || (input.side === "long" && stopPrice >= input.entryPrice)
    || (input.side === "short" && stopPrice <= input.entryPrice)
  ) {
    throw new Error("止损价格没有位于持仓亏损方向");
  }
  return stopPrice;
}

/**
 * Resolve, normalize and validate one exchange-executable full-margin stop.
 *
 * A missing liquidation price is deliberately not an exemption: Binance may
 * omit it for a small cross-margin leg, while a positive stop on the correct
 * side can still be accepted. A reported, valid loss-side liquidation price
 * remains a hard boundary.
 *
 * Tick collapse and a trigger already crossed by mark price are configuration
 * errors, not small-position exemptions. The exposure-increasing caller must
 * reject before submitting the entry and ask for a wider loss percentage.
 */
export function resolveExecutableFullMarginStop(input: FullMarginStopInput & {
  tickSize: number;
}): ExecutableFullMarginStopResolution {
  const resolved = resolveFullMarginStop(input);
  if (!resolved.nativeStopRequired) return resolved;

  let stopPrice: number;
  try {
    stopPrice = roundFullMarginStopPriceToTick({
      side: input.side,
      rawStopPrice: resolved.rawStopPrice,
      entryPrice: input.entryPrice,
      tickSize: input.tickSize,
    });
  } catch (error: any) {
    throw new Error(
      "止损比例过小：价格按 Binance tickSize 规整后等于或越过开仓均价，" +
      `请提高止损比例；${error?.message || String(error)}`,
    );
  }

  const markPrice = Number(input.markPrice);
  if (
    Number.isFinite(markPrice)
    && markPrice > 0
    && (
      (input.side === "long" && stopPrice >= markPrice)
      || (input.side === "short" && stopPrice <= markPrice)
    )
  ) {
    throw new Error(
      `止损比例过小：规范化止损价 ${stopPrice} 已被当前标记价 ` +
      `${markPrice} 穿越或会立即触发，请提高止损比例`,
    );
  }

  const liquidationPrice = Number(input.liquidationPrice);
  const liquidationReferencePrice =
    Number.isFinite(markPrice) && markPrice > 0
      ? markPrice
      : input.entryPrice;
  const liquidationOnLossSide =
    input.side === "long"
      ? liquidationPrice <= liquidationReferencePrice
      : liquidationPrice >= liquidationReferencePrice;
  if (
    Number.isFinite(liquidationPrice)
    && liquidationPrice > 0
    && liquidationOnLossSide
    && (
      (input.side === "long" && stopPrice <= liquidationPrice)
      || (input.side === "short" && stopPrice >= liquidationPrice)
    )
  ) {
    return {
      nativeStopRequired: false,
      maxLossAmount: resolved.maxLossAmount,
      effectiveQty: resolved.effectiveQty,
      priceDistance: resolved.priceDistance,
      rawStopPrice: null,
      reason:
        "小仓暂不可挂：止损价按 Binance tickSize 规整后不早于有效强平边界；" +
        "仓位数量、成交均价或强平边界变化后自动重算",
    };
  }

  return { ...resolved, stopPrice };
}
