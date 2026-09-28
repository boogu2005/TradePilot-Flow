import React from "react";
import { Alert, Button, Skeleton, Table, Tag } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import EmptyState from "../components/EmptyState";
import PnlText from "../components/PnlText";
import RealtimeBadge from "../components/RealtimeBadge";
import { usePolling } from "../hooks/usePolling";
import { useWebSocket } from "../hooks/useWebSocket";
import { api } from "../api/client";
import type { Position } from "../api/types";
import { formatPrice } from "../utils/format";

export default function PositionsPage() {
  // WS 推送为主通道（服务端固定 live 口径），断线时回落到 5s REST 轮询
  const ws = useWebSocket();
  const wsActive = ws.connected && !ws.stale;
  const { data, loading, error, refresh } = usePolling<Position[]>(
    () => api.getPositions(true),
    5000,
    !wsActive,
  );
  const positions = ws.snapshot?.positions ?? data;
  const loadingView = loading && !ws.snapshot;

  const columns: ColumnsType<Position> = [
    {
      title: "交易品种",
      dataIndex: "pair",
      render: (v: string, r) => (
        <div>
          <div className="font-semibold text-t1">{v}</div>
          {r.teacher && <div className="text-[11px] text-t2">{r.teacher}</div>}
        </div>
      ),
    },
    {
      title: "方向",
      dataIndex: "direction",
      width: 80,
      render: (v: string) => (
        <Tag color={v === "LONG" ? "green" : "red"} className="font-mono">
          {v}
        </Tag>
      ),
    },
    {
      title: "开仓价",
      dataIndex: "open_price",
      align: "right",
      render: (v: number) => <span className="font-mono">{formatPrice(v)}</span>,
    },
    {
      title: "当前价",
      dataIndex: "current_price",
      align: "right",
      render: (v: number, r) => (
        <span className="font-mono">
          {formatPrice(v)}
          {r.price_source === "open_rate" && (
            <span className="ml-1 text-[10px] text-t3">(开仓价)</span>
          )}
        </span>
      ),
    },
    {
      title: "杠杆",
      dataIndex: "leverage",
      width: 70,
      align: "right",
      render: (v: number) => <span className="font-mono">{v}x</span>,
    },
    {
      title: "保证金",
      dataIndex: "margin",
      align: "right",
      render: (v: number) => (
        <span className="font-mono">${v.toLocaleString()}</span>
      ),
    },
    {
      title: "持仓数量",
      dataIndex: "quantity",
      align: "right",
      render: (_v: number, r) => (
        <span className="font-mono">{r.amount.toFixed(6)}</span>
      ),
    },
    {
      title: "未实现盈亏",
      dataIndex: "unrealized_pnl",
      align: "right",
      render: (v: number) => <PnlText value={v} suffix=" USDT" precision={4} />,
    },
    {
      title: "收益率",
      dataIndex: "roi_pct",
      align: "right",
      render: (v: number) => <PnlText value={v} suffix="%" />,
    },
    {
      title: "止损价",
      dataIndex: "stop_loss",
      align: "right",
      responsive: ["md"],
      render: (v: number | null) =>
        v ? <span className="font-mono text-down">{formatPrice(v)}</span> : "-",
    },
    {
      title: "止盈状态",
      dataIndex: "tp_state",
      width: 120,
      responsive: ["md"],
      render: (v: string) => (
        <Tag
          color={
            v === "已触发TP1"
              ? "green"
              : v === "移动止盈止损中"
                ? "processing"
                : v === "已达标"
                  ? "gold"
                  : "default"
          }
        >
          {v}
        </Tag>
      ),
    },
    {
      title: "开仓时间",
      dataIndex: "open_time",
      width: 130,
      responsive: ["md"],
      render: (v: string | null) =>
        v ? (
          <span className="text-xs text-t2">
            {dayjs(v).format("MM-DD HH:mm")}
          </span>
        ) : (
          "-"
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-t2">
          当前持仓 · {positions?.length ?? 0} 个
          <RealtimeBadge connected={ws.connected} stale={ws.stale} />
        </h2>
        <Button
          size="small"
          icon={<ReloadOutlined />}
          onClick={refresh}
          className="text-xs"
        >
          刷新
        </Button>
      </div>

      {error && <Alert type="warning" showIcon message={error} />}
      {loadingView && !positions ? (
        <Skeleton active />
      ) : positions && positions.length === 0 ? (
        <div className="rounded-2xl border border-border bg-panel2">
          <EmptyState text="当前没有持仓" />
        </div>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-border bg-panel2">
          <Table<Position>
            rowKey="id"
            columns={columns}
            dataSource={positions ?? []}
            pagination={false}
            size="small"
            scroll={{ x: 1100 }}
            locale={{ emptyText: <EmptyState /> }}
          />
        </div>
      )}
    </div>
  );
}
