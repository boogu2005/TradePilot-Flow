import React, { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart, { hexA, useChartTheme } from "./EChart";
import type { EquityPoint } from "../api/types";

/**
 * 账户资金曲线：equity + balance。
 */
export default function EquityChart({
  points,
  height = 320,
}: {
  points: EquityPoint[];
  height?: number;
}) {
  const { palette, axisCommon, baseTooltip } = useChartTheme();
  const option = useMemo<EChartsOption>(() => {
    const labels = points.map((p) => {
      const raw = p.time ?? p.date ?? "";
      return raw.slice(0, 16).replace("T", " ");
    });
    const equity = points.map((p) => p.equity ?? p.cumulative ?? 0);
    const balance = points.map((p) => p.balance ?? p.pnl ?? 0);
    return {
      backgroundColor: "transparent",
      tooltip: { ...baseTooltip, valueFormatter: (v) => `${Number(v).toFixed(2)} USDT` },
      grid: { left: 50, right: 20, top: 30, bottom: 30 },
      legend: {
        data: ["权益", "余额"],
        textStyle: { color: palette.text },
        top: 0,
      },
      xAxis: {
        type: "category",
        data: labels,
        boundaryGap: false,
        ...axisCommon(),
      },
      yAxis: {
        type: "value",
        scale: true,
        ...axisCommon(),
        splitLine: { lineStyle: { color: palette.grid, type: "dashed" } },
      },
      series: [
        {
          name: "权益",
          type: "line",
          data: equity,
          smooth: 0.3,
          symbol: "none",
          lineStyle: { color: palette.ACCENT, width: 2 },
          areaStyle: {
            color: {
              type: "linear",
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: hexA(palette.ACCENT, 0.25) },
                { offset: 1, color: hexA(palette.ACCENT, 0) },
              ],
            },
          },
        },
        {
          name: "余额",
          type: "line",
          data: balance,
          smooth: 0.3,
          symbol: "none",
          lineStyle: { color: palette.SECONDARY, width: 1.5 },
        },
      ],
    };
  }, [points, palette, axisCommon, baseTooltip]);

  return <EChart option={option} height={height} />;
}
