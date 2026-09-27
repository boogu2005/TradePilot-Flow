import React, { useEffect, useState } from "react";
import { Button, Descriptions, Skeleton, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ArrowLeftOutlined } from "@ant-design/icons";
import { useNavigate, useParams } from "react-router-dom";
import dayjs from "dayjs";
import EmptyState from "../components/EmptyState";
import PnlText from "../components/PnlText";
import StatCard from "../components/StatCard";
import TeacherCurveChart from "../charts/TeacherCurveChart";
import { api } from "../api/client";
import type { TeacherDetail, TradeItem } from "../api/types";

function formatHold(seconds: number | null): string {
  if (seconds == null) return "-";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export default function TeacherDetailPage() {
  const { teacher } = useParams<{ teacher: string }>();
  const navigate = useNavigate();
  const decoded = teacher ? decodeURIComponent(teacher) : "";
  const [detail, setDetail] = useState<TeacherDetail | null>(null);
  const [trades, setTrades] = useState<{ items: TradeItem[]; total: number } | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!decoded) return;
    setLoading(true);
    Promise.all([
      api.getTeacherDetail(decoded, 0),
      api.getTeacherTrades(decoded, 1, 20),
    ])
      .then(([d, t]) => {
        setDetail(d);
        setTrades({ items: t.items, total: t.total });
      })
      .finally(() => setLoading(false));
  }, [decoded]);

  const loadTrades = async (p: number) => {
    const res = await api.getTeacherTrades(decoded, p, 20);
    setTrades({ items: res.items, total: res.total });
  };

  const columns: ColumnsType<TradeItem> = [
    { title: "币种", dataIndex: "pair", render: (v: string) => <span className="font-mono">{v}</span> },
    {
      title: "方向",
      dataIndex: "direction",
      width: 80,
      render: (v: string) => <Tag color={v === "LONG" ? "green" : "red"} className="font-mono">{v}</Tag>,
    },
    {
      title: "开仓时间",
      dataIndex: "open_time",
      width: 130,
      render: (v: string | null) => v ? <span className="text-xs text-t2">{dayjs(v).format("MM-DD HH:mm")}</span> : "-",
    },
    {
      title: "平仓时间",
      dataIndex: "close_time",
      width: 130,
      render: (v: string | null) => v ? <span className="text-xs text-t2">{dayjs(v).format("MM-DD HH:mm")}</span> : "-",
    },
    {
      title: "开仓价",
      dataIndex: "open_price",
      align: "right",
      render: (v: number) => <span className="font-mono">{v.toLocaleString()}</span>,
    },
    {
      title: "平仓价",
      dataIndex: "close_price",
      align: "right",
      render: (v: number) => <span className="font-mono">{v ? v.toLocaleString() : "-"}</span>,
    },
    {
      title: "盈亏",
      dataIndex: "pnl",
      align: "right",
      render: (v: number) => <PnlText value={v} suffix=" USDT" />,
    },
    {
      title: "盈亏比例",
      dataIndex: "pnl_pct",
      align: "right",
      render: (v: number) => <PnlText value={v} suffix="%" />,
    },
    {
      title: "持仓时间",
      dataIndex: "hold_seconds",
      align: "right",
      render: (v: number | null) => <span className="text-xs text-t2">{formatHold(v)}</span>,
    },
    { title: "杠杆", dataIndex: "leverage", width: 70, align: "right", render: (v: number) => <span className="font-mono">{v}x</span> },
  ];

  if (loading) {
    return <div className="p-4"><Skeleton active /></div>;
  }

  if (!detail) {
    return <EmptyState text="未找到该老师" />;
  }

  const stats = detail.stats;
  const risk = detail.risk;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>
          返回
        </Button>
        <h2 className="text-lg font-bold text-t1">{detail.teacher}</h2>
        <Tag color="gold">带单老师</Tag>
      </div>

      {/* 基础统计 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <StatCard title="总交易" value={stats.total_trades} />
        <StatCard
          title="净收益 (USDT)"
          value={<PnlText value={stats.net_profit} suffix=" USDT" />}
          accent
        />
        <StatCard
          title="胜率"
          value={<span>{stats.win_rate.toFixed(1)}%</span>}
          sub={`胜 ${stats.wins} / 负 ${stats.losses}`}
        />
        <StatCard
          title="盈亏比"
          value={
            <span className={((risk.profit_factor ?? 999) >= 1 ? "text-up" : "text-down")}>
              {risk.profit_factor === null ? "∞" : risk.profit_factor?.toFixed(2)}
            </span>
          }
        />
        <StatCard
          title="最大回撤"
          value={<span className="text-down">-{risk.max_drawdown.toFixed(2)}%</span>}
        />
        <StatCard
          title="夏普比率"
          value={<span>{risk.sharpe_ratio.toFixed(2)}</span>}
        />
      </div>

      {/* 风险指标详情 */}
      <div className="rounded-2xl border border-border bg-panel2 p-5">
        <Descriptions
          column={{ xs: 1, sm: 2, md: 4 }}
          size="small"
          title={<span className="text-sm font-semibold text-t1">风险指标</span>}
          items={[
            { key: "wr", label: "胜率", children: <span>{risk.win_rate.toFixed(2)}%</span> },
            { key: "pf", label: "盈亏比", children: <span>{risk.profit_factor === null ? "∞" : risk.profit_factor?.toFixed(2)}</span> },
            { key: "dd", label: "最大回撤", children: <span className="text-down">-{risk.max_drawdown.toFixed(2)}%</span> },
            { key: "sh", label: "夏普比率(年化)", children: <span>{risk.sharpe_ratio.toFixed(2)}</span> },
            { key: "ah", label: "平均持仓时间", children: <span>{formatHold(Math.round(risk.avg_hold_hours * 3600))}</span> },
            { key: "aw", label: "平均盈利", children: <PnlText value={risk.avg_win} suffix=" USDT" /> },
            { key: "al", label: "平均亏损", children: <PnlText value={risk.avg_loss} suffix=" USDT" /> },
            { key: "mw", label: "最大单笔盈利", children: <PnlText value={risk.max_win} suffix=" USDT" /> },
            { key: "ml", label: "最大单笔亏损", children: <PnlText value={risk.max_loss} suffix=" USDT" /> },
          ]}
        />
      </div>

      {/* 收益曲线 */}
      <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="rounded-2xl border border-border bg-panel2 p-5">
          <h3 className="mb-2 text-sm font-semibold text-t1">近 7 天收益曲线</h3>
          {detail.curves.d7.length ? (
            <TeacherCurveChart points={detail.curves.d7} height={280} />
          ) : (
            <div className="flex h-[280px] items-center justify-center text-sm text-t2">暂无数据</div>
          )}
        </div>
        <div className="rounded-2xl border border-border bg-panel2 p-5">
          <h3 className="mb-2 text-sm font-semibold text-t1">近 30 天收益曲线</h3>
          {detail.curves.d30.length ? (
            <TeacherCurveChart points={detail.curves.d30} height={280} />
          ) : (
            <div className="flex h-[280px] items-center justify-center text-sm text-t2">暂无数据</div>
          )}
        </div>
      </section>

      {/* 交易历史 */}
      <div className="rounded-2xl border border-border bg-panel2 p-5">
        <h3 className="mb-2 text-sm font-semibold text-t1">
          交易历史 · 共 {trades?.total ?? 0} 笔
        </h3>
        <Table<TradeItem>
          rowKey="id"
          columns={columns}
          dataSource={trades?.items ?? []}
          size="small"
          scroll={{ x: 1100 }}
          locale={{ emptyText: <EmptyState /> }}
          pagination={{
            current: page,
            pageSize: 20,
            total: trades?.total ?? 0,
            onChange: (p) => {
              setPage(p);
              loadTrades(p);
            },
          }}
        />
      </div>
    </div>
  );
}
