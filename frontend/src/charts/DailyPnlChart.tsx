import React, { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart, { useChartTheme } from "./EChart";
import type { DailyPnlItem } from "../api/types";

/**
 * 每日盈亏柱状图。
 */
export default function DailyPnlChart({
  items,
  height = 320,
}: {
  items: DailyPnlItem[];
  height?: number;
}) {
  const { palette, axisCommon, baseTooltip } = useChartTheme();
  const option = useMemo<EChartsOption>(() => {
    const labels = items.map((i) => i.date);
    const values = items.map((i) => i.pnl);
    return {
      backgroundColor: "transparent",
      tooltip: {
        ...baseTooltip,
        trigger: "axis",
        formatter: (params: unknown) => {
          const arr = params as Array<{
            axisValue: string;
            data: number;
          }>;
          const first = arr[0];
          const idx = labels.indexOf(first?.axisValue);
          const item = items[idx];
          if (!item) return "";
          return [
            `<b>${item.date}</b>`,
            `盈亏: <span style="color:${item.pnl >= 0 ? palette.UP : palette.DOWN}">${item.pnl >= 0 ? "+" : ""}${item.pnl.toFixed(2)} USDT</span>`,
            `交易数: ${item.count}（胜 ${item.wins} / 负 ${item.losses}）`,
          ].join("<br/>");
        },
      },
      grid: { left: 60, right: 20, top: 30, bottom: 30 },
      xAxis: { type: "category", data: labels, ...axisCommon() },
      yAxis: {
        type: "value",
        ...axisCommon(),
        splitLine: { lineStyle: { color: palette.grid, type: "dashed" } },
      },
      series: [
        {
          name: "每日盈亏",
          type: "bar",
          data: values.map((v) => ({
            value: v,
            itemStyle: { color: v >= 0 ? palette.UP : palette.DOWN, borderRadius: 999 },
          })),
          barMaxWidth: 20,
          barCategoryGap: "35%",
        },
      ],
    };
  }, [items, palette, axisCommon, baseTooltip]);

  return <EChart option={option} height={height} />;
}
