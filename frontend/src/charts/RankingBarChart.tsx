import React, { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart, { hexA, useChartTheme } from "./EChart";
import type { TeacherRankItem } from "../api/types";

/**
 * 老师收益排名柱状图。
 */
export default function RankingBarChart({
  items,
  height = 380,
}: {
  items: TeacherRankItem[];
  height?: number;
}) {
  const { palette, axisCommon, baseTooltip } = useChartTheme();
  const option = useMemo<EChartsOption>(() => {
    const sorted = [...items]
      .sort((a, b) => a.net_profit - b.net_profit)
      .slice(-15);
    return {
      backgroundColor: "transparent",
      tooltip: {
        ...baseTooltip,
        trigger: "axis",
        axisPointer: { type: "shadow" },
      },
      grid: { left: 90, right: 30, top: 20, bottom: 30 },
      xAxis: {
        type: "value",
        ...axisCommon(),
        splitLine: { lineStyle: { color: palette.grid, type: "dashed" } },
      },
      yAxis: {
        type: "category",
        data: sorted.map((i) => i.teacher),
        ...axisCommon(),
        axisLabel: { color: palette.text, fontSize: 11 },
      },
      series: [
        {
          name: "净收益",
          type: "bar",
          data: sorted.map((i) => ({
            value: i.net_profit,
            itemStyle: {
              color:
                i.net_profit >= 0
                  ? hexA(palette.UP, 0.9)
                  : hexA(palette.DOWN, 0.9),
              borderRadius: [0, 999, 999, 0], // 横向胶囊条:右端大圆角
            },
          })),
          barMaxWidth: 22,
          label: {
            show: true,
            position: "right",
            color: palette.text,
            fontSize: 11,
            formatter: (p: unknown) => {
              const v = (p as { value: number }).value;
              return `${v >= 0 ? "+" : ""}${v.toFixed(0)}`;
            },
          },
        },
      ],
    };
  }, [items, palette, axisCommon, baseTooltip]);

  return <EChart option={option} height={height} />;
}
