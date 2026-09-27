import React from "react";

interface PnlTextProps {
  value: number;
  suffix?: string;
  precision?: number;
  prefix?: string;
  className?: string;
}

/**
 * 盈亏数字：盈利用绿色、亏损用红色（Binance 风格）。
 */
export default function PnlText({
  value,
  suffix = "",
  precision = 2,
  prefix = "",
  className = "",
}: PnlTextProps) {
  const isNegative = value < 0;
  const isPositive = value > 0;
  const color = isPositive
    ? "text-up"
    : isNegative
      ? "text-down"
      : "text-t3";
  const sign = isNegative ? "" : isPositive ? "+" : "";
  return (
    <span className={`font-mono font-semibold ${color} ${className}`}>
      {sign}
      {prefix}
      {value.toFixed(precision)}
      {suffix}
    </span>
  );
}
