import React, { useEffect, useMemo, useState } from "react";
import { Radio, Skeleton, Table, Tabs } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link } from "react-router-dom";
import EmptyState from "../components/EmptyState";
import PnlText from "../components/PnlText";
import RankingBarChart from "../charts/RankingBarChart";
import WinRateChart from "../charts/WinRateChart";
import ProfitFactorChart from "../charts/ProfitFactorChart";
import { api } from "../api/client";
import type { TeacherRankItem } from "../api/types";

type RankType = "profit" | "profit_factor" | "win_rate" | "roi" | "risk_adjusted";

export default function RankingPage() {
  const [period, setPeriod] = useState(30);
  const [type, setType] = useState<RankType>("risk_adjusted");
  const [items, setItems] = useState<TeacherRankItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .getRanking(period, type, 100)
      .then((res) => setItems(res.items))
      .finally(() => setLoading(false));
  }, [period, type]);

  const columns = useMemo<ColumnsType<TeacherRankItem>>(() => {
    const common = [
      {
        title: "排名",
        dataIndex: "rank",
        width: 70,
        align: "center" as const,
        render: (v: number) => (
          <span
            className={`inline-flex h-7 w-7 items-center justify-center rounded-full font-mono text-xs font-bold shadow-sm ${
              v === 1
                ? "bg-[#17171a] text-white"
                : v === 2
                  ? "bg-[#6e7480] text-white"
                  : v === 3
                    ? "bg-[#b8bdc5] text-[#17171a]"
                    : "bg-panel text-t2 shadow-none"
            }`}
          >
            {v}
          </span>
        ),
      },
      {
        title: "老师名称",
        dataIndex: "teacher",
        render: (v: string) => (
          <Link to={`/teachers/${encodeURIComponent(v)}`} className="text-accent hover:underline">
            {v}
          </Link>
        ),
      },
    ];

    if (type === "profit") {
      return [
        ...common,
        {
          title: "交易次数",
          dataIndex: "total_trades",
          align: "right" as const,
        },
        {
          title: "盈利次数",
          dataIndex: "wins",
          align: "right" as const,
          render: (v: number) => <span className="text-up">{v}</span>,
        },
        {
          title: "亏损次数",
          dataIndex: "losses",
          align: "right" as const,
          render: (v: number) => <span className="text-down">{v}</span>,
        },
        {
          title: "总收益 (USDT)",
          dataIndex: "net_profit",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} suffix=" USDT" />,
        },
        {
          title: "收益率",
          dataIndex: "roi",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} suffix="%" />,
        },
        {
          title: "胜率",
          dataIndex: "win_rate",
          align: "right" as const,
          render: (v: number) => <span className="font-mono">{v.toFixed(1)}%</span>,
        },
        {
          title: "最大盈利",
          dataIndex: "max_win",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} />,
        },
        {
          title: "最大亏损",
          dataIndex: "max_loss",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} />,
        },
      ] as ColumnsType<TeacherRankItem>;
    }

    if (type === "profit_factor") {
      return [
        ...common,
        {
          title: "盈亏比",
          dataIndex: "profit_factor",
          align: "right" as const,
          render: (v: number | null) => (
            <span className={`font-mono font-bold ${(v ?? 999) >= 1 ? "text-up" : "text-down"}`}>
              {v === null ? "∞" : v.toFixed(2)}
            </span>
          ),
        },
        {
          title: "平均盈利",
          dataIndex: "avg_win",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} />,
        },
        {
          title: "平均亏损",
          dataIndex: "avg_loss",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} />,
        },
        {
          title: "交易次数",
          dataIndex: "total_trades",
          align: "right" as const,
        },
        {
          title: "总收益",
          dataIndex: "net_profit",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} suffix=" USDT" />,
        },
      ] as ColumnsType<TeacherRankItem>;
    }

    if (type === "risk_adjusted") {
      return [
        ...common,
        {
          title: "风险调整得分",
          dataIndex: "risk_adjusted_score",
          align: "right" as const,
          render: (v: number, r) =>
            r.eligible ? (
              <span className={`font-mono font-bold ${v >= 0 ? "text-up" : "text-down"}`}>
                {v > 0 ? "+" : ""}
                {v.toFixed(2)}
              </span>
            ) : (
              <span className="text-xs text-t3">未达标</span>
            ),
        },
        {
          title: "平均收益率",
          dataIndex: "mean_ratio",
          align: "right" as const,
          render: (v: number) => (
            <span className="font-mono">{(v * 100).toFixed(1)}%</span>
          ),
        },
        {
          title: "收益波动率",
          dataIndex: "std_ratio",
          align: "right" as const,
          render: (v: number) => (
            <span className="font-mono">{(v * 100).toFixed(1)}%</span>
          ),
        },
        {
          title: "交易次数",
          dataIndex: "ratio_trades",
          align: "right" as const,
        },
        {
          title: "总收益 (USDT)",
          dataIndex: "net_profit",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} suffix=" USDT" />,
        },
      ] as ColumnsType<TeacherRankItem>;
    }

    if (type === "roi") {
      return [
        ...common,
        {
          title: "收益率",
          dataIndex: "roi",
          align: "right" as const,
          render: (v: number) => (
            <span className={`font-mono font-bold ${v >= 0 ? "text-up" : "text-down"}`}>
              {v > 0 ? "+" : ""}
              {v.toFixed(2)}%
            </span>
          ),
        },
        {
          title: "总收益 (USDT)",
          dataIndex: "net_profit",
          align: "right" as const,
          render: (v: number) => <PnlText value={v} suffix=" USDT" />,
        },
        {
          title: "交易次数",
          dataIndex: "total_trades",
          align: "right" as const,
        },
        {
          title: "胜率",
          dataIndex: "win_rate",
          align: "right" as const,
          render: (v: number) => <span className="font-mono">{v.toFixed(1)}%</span>,
        },
        {
          title: "盈亏比",
          dataIndex: "profit_factor",
          align: "right" as const,
          render: (v: number | null) => (
            <span className={`font-mono font-bold ${(v ?? 999) >= 1 ? "text-up" : "text-down"}`}>
              {v === null ? "∞" : v.toFixed(2)}
            </span>
          ),
        },
        {
          title: "平均持仓",
          dataIndex: "avg_hold_hours",
          align: "right" as const,
          render: (v: number) => (
            <span className="text-xs text-t2">{v.toFixed(1)}h</span>
          ),
        },
      ] as ColumnsType<TeacherRankItem>;
    }

    return [
      ...common,
      {
        title: "胜率",
        dataIndex: "win_rate",
        align: "right" as const,
        render: (v: number) => (
          <span className={`font-mono font-bold ${v >= 50 ? "text-up" : "text-down"}`}>
            {v.toFixed(1)}%
          </span>
        ),
      },
      {
        title: "盈利次数",
        dataIndex: "wins",
        align: "right" as const,
        render: (v: number) => <span className="text-up">{v}</span>,
      },
      {
        title: "亏损次数",
        dataIndex: "losses",
        align: "right" as const,
        render: (v: number) => <span className="text-down">{v}</span>,
      },
      {
        title: "总交易次数",
        dataIndex: "total_trades",
        align: "right" as const,
      },
      {
        title: "平均收益",
        dataIndex: "net_profit",
        align: "right" as const,
        render: (_, r) => (
          <PnlText value={r.total_trades ? r.net_profit / r.total_trades : 0} suffix=" USDT" />
        ),
      },
      {
        title: "总收益",
        dataIndex: "net_profit",
        align: "right" as const,
        render: (v: number) => <PnlText value={v} suffix=" USDT" />,
      },
    ] as ColumnsType<TeacherRankItem>;
  }, [type]);

  // 月度锁定档位列（数据来自机器人每月 1 号快照，与实时排名解耦）
  const tierColumns: ColumnsType<TeacherRankItem> = [
    {
      title: "本月锁定档位",
      dataIndex: "tier_band",
      width: 110,
      render: (v: string | null) =>
        v ? <span className="text-xs text-t1">{v}</span> : <span className="text-xs text-t3">—</span>,
    },
    {
      title: "本月单笔仓位比例",
      dataIndex: "position_ratio",
      width: 120,
      align: "right",
      render: (v: number | null) =>
        v != null ? (
          <span className="font-mono font-semibold text-t1">
            {(v * 100).toFixed(0)}%
          </span>
        ) : (
          <span className="text-xs text-t3">—</span>
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-t2">带单老师排行榜</h2>
        <Radio.Group
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
          optionType="button"
          buttonStyle="solid"
          options={[
            { label: "7 天", value: 7 },
            { label: "30 天", value: 30 },
            { label: "90 天", value: 90 },
          ]}
        />
      </div>

      <div className="rounded-2xl border border-border bg-panel2">
        <Tabs
          activeKey={type}
          onChange={(k) => setType(k as RankType)}
          items={[
            { key: "risk_adjusted", label: "动态仓位分配榜" },
            { key: "profit", label: "收益排行榜" },
            { key: "roi", label: "收益率排行榜" },
            { key: "profit_factor", label: "盈亏比排行榜" },
            { key: "win_rate", label: "胜率排行榜" },
          ]}
          className="px-4 pt-2"
        />
        {loading ? (
          <div className="p-4"><Skeleton active /></div>
        ) : items.length === 0 ? (
          <EmptyState text="该周期内暂无已平仓交易" />
        ) : (
          <Table<TeacherRankItem>
            rowKey="teacher"
            columns={[...columns, ...tierColumns]}
            dataSource={items}
            size="small"
            pagination={{ pageSize: 15, showSizeChanger: false }}
            scroll={{ x: 1100 }}
          />
        )}
      </div>

      {/* 排行图表 */}
      <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="rounded-2xl border border-border bg-panel2 p-5">
          <h3 className="mb-2 text-sm font-semibold text-t1">收益排名 TOP15</h3>
          {items.length ? <RankingBarChart items={items} height={360} /> : <div className="flex h-[360px] items-center justify-center text-sm text-t2">暂无数据</div>}
        </div>
        <div className="rounded-2xl border border-border bg-panel2 p-5">
          <h3 className="mb-2 text-sm font-semibold text-t1">胜率排行 TOP15</h3>
          {items.length ? <WinRateChart items={items} height={360} /> : <div className="flex h-[360px] items-center justify-center text-sm text-t2">暂无数据</div>}
        </div>
        <div className="rounded-2xl border border-border bg-panel2 p-5">
          <h3 className="mb-2 text-sm font-semibold text-t1">盈亏比分布 TOP15</h3>
          {items.length ? <ProfitFactorChart items={items} height={360} /> : <div className="flex h-[360px] items-center justify-center text-sm text-t2">暂无数据</div>}
        </div>
      </section>
    </div>
  );
}
