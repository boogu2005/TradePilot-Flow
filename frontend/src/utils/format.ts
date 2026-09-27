/** 数字/货币/百分比统一格式化。 */

/**
 * 行情价格显示：与 OKX 一致，低价币保留足够小数位。
 * 如 0.19433 → "0.19433"、1.1233 → "1.1233"、64000.123 → "64,000.12"
 * （toLocaleString 默认最多 3 位小数会把低价币截断，如 0.19433 → "0.194"）
 */
export function formatPrice(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString("en-US", {
    maximumSignificantDigits: 7,
  });
}

export function fmtUSD(value: number | null | undefined, digits = 2): string {
  return `$${fmtNum(value ?? 0, digits)}`;
}

export function fmtNum(value: number | null | undefined, digits = 2): string {
  return (value ?? 0).toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

export function fmtPct(value: number | null | undefined, digits = 2): string {
  return `${(value ?? 0).toFixed(digits)}%`;
}

export function fmtSigned(value: number | null | undefined, digits = 2): string {
  const v = value ?? 0;
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  })}`;
}
