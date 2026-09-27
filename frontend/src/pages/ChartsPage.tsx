import React, { useEffect, useMemo, useState } from "react";
import { Radio, Skeleton } from "antd";
import EmptyState from "../components/EmptyState";
import EquityChart from "../charts/EquityChart";
import DailyPnlChart from "../charts/DailyPnlChart";
import RankingBarChart from "../charts/RankingBarChart";
import WinRateChart from "../charts/WinRateChart";
import ProfitFactorChart from "../charts/ProfitFactorChart";
import EChart, { hexA, useChartTheme } from "../charts/EChart";
import type { EChartsOption } from "echarts";
import { api } from "../api/client";
import type { EquityPoint, DailyPnlItem, TeacherRankItem } from "../api/types";

export default function ChartsPage() {
  const [days, setDays] = useState(30);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [dailyPnl, setDailyPnl] = useState<DailyPnlItem[]>([]);
  const [ranking, setRanking] = useState<TeacherRankItem[]>([]);
  const [distribution, setDistribution] = useState<{ bins: number[]; counts: number[] }>({ bins: [], counts: [] });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.getEquity(days),
      api.getDailyPnl(days),
      api.getRanking(30, "profit", 100),
      api.getRanking(30, "win_rate", 100),
      api.getRanking(30, "profit_factor", 100),
      api.getPnlDistribution(),
    ])
      .then(([eq, dp, profitRank, winRank, pfRank, dist]) => {
        setEquity(eq);
        setDailyPnl(dp);
        setRanking(profitRank.items);
        setWinRanking(winRank.items);
        setPfRanking(pfRank.items);
        setDistribution(dist);
      })
      .finally(() => setLoading(false));
  }, [days]);

  const [winRanking, setWinRanking] = useState<TeacherRankItem[]>([]);
  const [pfRanking, setPfRanking] = useState<TeacherRankItem[]>([]);

  const { palette, axisCommon, baseTooltip } = useChartTheme();
  const distOption: EChartsOption = useMemo(() => ({
    backgroundColor: "transparent",
    tooltip: {
      ...baseTooltip,
      trigger: "axis",
      axisPointer: { type: "shadow" },
    },
    grid: { left: 55, right: 20, top: 30, bottom: 45 },
    xAxis: {
      type: "category",
      data: distribution.bins.map((b) => b.toFixed(0)),
      ...axisCommon(),
      axisLabel: { color: palette.text, fontSize: 10, rotate: 0 },
    },
    yAxis: {
      type: "value",
      ...axisCommon(),
      splitLine: { lineStyle: { color: palette.grid, type: "dashed" } },
    },
    series: [
      {
        name: "交易笔数",
        type: "bar",
        data: distribution.counts,
        barMaxWidth: 26,
        barCategoryGap: "30%",
        itemStyle: {
          borderRadius: 999, // 胶囊柱条
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: hexA(palette.ACCENT, 0.9) },
              { offset: 1, color: hexA(palette.ACCENT, 0.3) },
            ],
          },
        },
      },
    ],
  }), [distribution, palette, axisCommon, baseTooltip]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-t2">图表分析</h2>
        <Radio.Group
          value={days}
          onChange={(e) => setDays(e.target.value)}
          optionType="button"
          buttonStyle="solid"
          options={[
            { label: "7 天", value: 7 },
            { label: "30 天", value: 30 },
            { label: "90 天", value: 90 },
          ]}
        />
      </div>

      {loading ? (
        <Skeleton active />
      ) : (
        <>
          <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div className="rounded-2xl border border-border bg-panel2 p-5">
              <h3 className="mb-2 text-sm font-semibold text-t1">账户资金曲线（近 {days} 天）</h3>
              {equity.length ? <EquityChart points={equity} height={320} /> : <div className="flex h-[320px] items-center justify-center"><EmptyState text="暂无数据" compact /></div>}
            </div>
            <div className="rounded-2xl border border-border bg-panel2 p-5">
              <h3 className="mb-2 text-sm font-semibold text-t1">每日收益柱状图（近 {days} 天）</h3>
              {dailyPnl.length ? <DailyPnlChart items={dailyPnl} height={320} /> : <div className="flex h-[320px] items-center justify-center"><EmptyState text="暂无数据" compact /></div>}
            </div>
          </section>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
            <div className="rounded-2xl border border-border bg-panel2 p-5">
              <h3 className="mb-2 text-sm font-semibold text-t1">老师收益排名 TOP15</h3>
              {ranking.length ? <RankingBarChart items={ranking} height={360} /> : <div className="flex h-[360px] items-center justify-center"><EmptyState text="暂无数据" compact /></div>}
            </div>
            <div className="rounded-2xl border border-border bg-panel2 p-5">
              <h3 className="mb-2 text-sm font-semibold text-t1">胜率排行 TOP15</h3>
              {winRanking.length ? <WinRateChart items={winRanking} height={360} /> : <div className="flex h-[360px] items-center justify-center"><EmptyState text="暂无数据" compact /></div>}
            </div>
            <div className="rounded-2xl border border-border bg-panel2 p-5">
              <h3 className="mb-2 text-sm font-semibold text-t1">盈亏比分布 TOP15</h3>
              {pfRanking.length ? <ProfitFactorChart items={pfRanking} height={360} /> : <div className="flex h-[360px] items-center justify-center"><EmptyState text="暂无数据" compact /></div>}
            </div>
          </section>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div className="rounded-2xl border border-border bg-panel2 p-5">
              <h3 className="mb-2 text-sm font-semibold text-t1">盈亏金额分布（全周期）</h3>
              {distribution.bins.length ? <EChart option={distOption} height={300} /> : <div className="flex h-[300px] items-center justify-center"><EmptyState text="暂无数据" compact /></div>}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
