import React, { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart, { hexA, useChartTheme } from "./EChart";
import type { TeacherRankItem } from "../api/types";

/**
 * 老师盈亏比分布图。
 */
export default function ProfitFactorChart({
  items,
  height = 380,
}: {
  items: TeacherRankItem[];
  height?: number;
}) {
  const { palette, axisCommon, baseTooltip } = useChartTheme();
  const option = useMemo<EChartsOption>(() => {
    const sorted = [...items]
      .filter((i) => i.total_trades > 0)
      .sort((a, b) => (a.profit_factor ?? 999) - (b.profit_factor ?? 999))
      .slice(-15);
    return {
      backgroundColor: "transparent",
      tooltip: {
        ...baseTooltip,
        trigger: "axis",
        axisPointer: { type: "shadow" },
        valueFormatter: (v) => (v === null || v === undefined ? "∞" : `${Number(v).toFixed(2)}`),
      },
      grid: { left: 90, right: 40, top: 20, bottom: 30 },
      xAxis: {
        type: "value",
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
          name: "盈亏比",
          type: "bar",
          data: sorted.map((i) => ({
            value: i.profit_factor === null ? 999 : i.profit_factor,
            itemStyle: {
              color:
                (i.profit_factor ?? 0) >= 1
                  ? hexA(palette.UP, 0.9)
                  : hexA(palette.DOWN, 0.9),
              borderRadius: [0, 999, 999, 0],
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
              return v >= 999 ? "∞" : v.toFixed(2);
            },
          },
        },
      ],
    };
  }, [items, palette, axisCommon, baseTooltip]);

  return <EChart option={option} height={height} />;
}
