import React, { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart, { hexA, useChartTheme } from "./EChart";
import type { TeacherRankItem } from "../api/types";

/**
 * 老师胜率排行图。
 */
export default function WinRateChart({
  items,
  height = 380,
}: {
  items: TeacherRankItem[];
  height?: number;
}) {
  const { palette, axisCommon, baseTooltip } = useChartTheme();
  const option = useMemo<EChartsOption>(() => {
    const sorted = [...items].sort((a, b) => a.win_rate - b.win_rate).slice(-15);
    return {
      backgroundColor: "transparent",
      tooltip: {
        ...baseTooltip,
        trigger: "axis",
        axisPointer: { type: "shadow" },
        valueFormatter: (v) => `${Number(v).toFixed(1)}%`,
      },
      grid: { left: 90, right: 40, top: 20, bottom: 30 },
      xAxis: {
        type: "value",
        max: 100,
        ...axisCommon(),
        splitLine: { lineStyle: { color: palette.grid, type: "dashed" } },
      },
      yAxis: {
        type: "category",
        data: sorted.map((i) => i.teacher),
        ...axisCommon(),
      },
      series: [
        {
          name: "胜率",
          type: "bar",
          data: sorted.map((i) => ({
            value: i.win_rate,
            itemStyle: { color: hexA(palette.ACCENT, 0.9), borderRadius: [0, 999, 999, 0] },
          })),
          barMaxWidth: 22,
          label: {
            show: true,
            position: "right",
            color: palette.text,
            fontSize: 11,
            formatter: (p: unknown) => `${(p as { value: number }).value.toFixed(1)}%`,
          },
        },
      ],
    };
  }, [items, palette, axisCommon, baseTooltip]);

  return <EChart option={option} height={height} />;
}
