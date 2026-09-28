import React, { useEffect, useMemo, useRef } from "react";
import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import { useTheme } from "../theme";
import type { ThemeMode } from "../theme";

interface EChartProps {
  option: EChartsOption;
  height?: number;
  loading?: boolean;
}

/** #rrggbb + alpha → rgba() 字符串（图表渐变用，随主题 accent 走）。 */
export function hexA(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

/** 白天/黑夜的图表配色，与 index.css 的 CSS 变量语义一致。 */
const THEMES: Record<
  ThemeMode,
  {
    ACCENT: string;
    SECONDARY: string;
    UP: string;
    DOWN: string;
    text: string;
    grid: string;
    tooltipBg: string;
    tooltipBorder: string;
    tooltipText: string;
    tooltipAxisPointer: string;
    series: string[];
  }
> = {
  light: {
    ACCENT: "#17171a",
    SECONDARY: "#8a8f98",
    UP: "#0a9c5e",
    DOWN: "#e5484d",
    text: "#6e6e73",
    grid: "#e7e7ea",
    tooltipBg: "#ffffff",
    tooltipBorder: "#e7e7ea",
    tooltipText: "#17171a",
    tooltipAxisPointer: "#aeaeb2",
    series: ["#17171a", "#8a8f98", "#c6cad1", "#4a4f58", "#0a9c5e", "#e5484d"],
  },
  dark: {
    ACCENT: "#f2f3f5",
    SECONDARY: "#8f959e",
    UP: "#30d158",
    DOWN: "#ff453a",
    text: "#9ba1aa",
    grid: "#2a2d33",
    tooltipBg: "#1f2228",
    tooltipBorder: "#2a2d33",
    tooltipText: "#e8eaed",
    tooltipAxisPointer: "#454a52",
    series: ["#e8eaed", "#8f959e", "#5a6069", "#c6cad1", "#30d158", "#ff453a"],
  },
};

export interface ChartTheme {
  palette: {
    UP: string;
    DOWN: string;
    ACCENT: string;
    SECONDARY: string;
    text: string;
    grid: string;
    series: string[];
  };
  axisCommon: () => {
    axisLine: { lineStyle: { color: string } };
    axisTick: { show: boolean };
    axisLabel: { color: string; fontSize: number };
    splitLine: { lineStyle: { color: string } };
  };
  baseTooltip: EChartsOption["tooltip"];
}

/** 随主题变化的图表配置；mode 切换后各 chart 的 option 会自动重建。 */
export function useChartTheme(): ChartTheme {
  const { mode } = useTheme();
  return useMemo(() => {
    const t = THEMES[mode];
    return {
      palette: {
        UP: t.UP,
        DOWN: t.DOWN,
        ACCENT: t.ACCENT,
        SECONDARY: t.SECONDARY,
        text: t.text,
        grid: t.grid,
        series: t.series,
      },
      axisCommon: () => ({
        axisLine: { lineStyle: { color: t.grid } },
        axisTick: { show: false },
        axisLabel: { color: t.text, fontSize: 11 },
        splitLine: { lineStyle: { color: t.grid } },
      }),
      baseTooltip: {
        trigger: "axis" as const,
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText, fontSize: 12 },
        axisPointer: { lineStyle: { color: t.tooltipAxisPointer } },
      },
    };
  }, [mode]);
}

export default function EChart({ option, height = 300, loading = false }: EChartProps) {
  const chartRef = useRef<ReactECharts>(null);

  // 容器尺寸变化（布局/横竖屏/侧栏折叠）时同步 canvas，防拉伸毛边
  useEffect(() => {
    const instance = chartRef.current?.getEchartsInstance();
    if (!instance) return;
    const el = instance.getDom();
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <ReactECharts
      ref={chartRef}
      option={option}
      style={{ height, width: "100%" }}
      notMerge
      showLoading={loading}
      opts={{ renderer: "canvas" }}
    />
  );
}
