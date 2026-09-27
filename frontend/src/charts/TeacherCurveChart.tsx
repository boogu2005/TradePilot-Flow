import React, { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart, { useChartTheme } from "./EChart";
import type { CurvePoint } from "../api/types";

/**
 * 老师累计收益曲线。
 */
export default function TeacherCurveChart({
  points,
  height = 320,
}: {
  points: CurvePoint[];
  height?: number;
}) {
  const { palette, axisCommon, baseTooltip } = useChartTheme();
  const option = useMemo<EChartsOption>(() => {
    const labels = points.map((p) => p.date ?? "");
    const values = points.map((p) => p.cumulative_pnl ?? 0);
    const last = values[values.length - 1] ?? 0;
    const color = last >= 0 ? palette.UP : palette.DOWN;
    return {
      backgroundColor: "transparent",
      tooltip: {
        ...baseTooltip,
        valueFormatter: (v) => `${Number(v).toFixed(2)} USDT`,
      },
      grid: { left: 55, right: 20, top: 30, bottom: 30 },
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
          name: "累计收益",
          type: "line",
          data: values,
          smooth: 0.2,
          symbol: "circle",
          symbolSize: 5,
          lineStyle: { color, width: 2 },
          itemStyle: { color },
          areaStyle: {
            color: {
              type: "linear",
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: `${color}40` },
                { offset: 1, color: `${color}00` },
              ],
            },
          },
        },
      ],
    };
  }, [points, palette, axisCommon, baseTooltip]);

  return <EChart option={option} height={height} />;
}
