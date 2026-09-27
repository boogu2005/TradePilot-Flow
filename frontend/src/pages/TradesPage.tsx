import React, { useCallback, useEffect, useState } from "react";
import {
  Button,
  DatePicker,
  Input,
  Select,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import EmptyState from "../components/EmptyState";
import PnlText from "../components/PnlText";
import { api } from "../api/client";
import type { TradeItem } from "../api/types";

const { RangePicker } = DatePicker;

function formatHold(seconds: number | null): string {
  if (seconds == null) return "-";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export default function TradesPage() {
  const [teachers, setTeachers] = useState<string[]>([]);
  const [filters, setFilters] = useState<{
    teacher?: string;
    pair?: string;
    direction?: string;
    result?: string;
    range?: [Dayjs, Dayjs] | null;
  }>({});
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [data, setData] = useState<{ items: TradeItem[]; total: number } | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.getTeachers().then((items) => setTeachers(items.map((i) => i.teacher)));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.getTrades({
        teacher: filters.teacher,
        pair: filters.pair,
        direction: filters.direction,
        result: filters.result,
        start: filters.range?.[0]?.startOf("day").toISOString(),
        end: filters.range?.[1]?.add(1, "day").startOf("day").toISOString(),
        page,
        page_size: pageSize,
      });
      setData({ items: res.items, total: res.total });
    } finally {
      setLoading(false);
    }
  }, [filters, page, pageSize]);

  useEffect(() => {
    load();
  }, [load]);

  const columns: ColumnsType<TradeItem> = [
    {
      title: "带单老师",
      dataIndex: "teacher",
      width: 130,
      render: (v: string) => (
        <span className="text-accent">{v}</span>
      ),
    },
    { title: "币种", dataIndex: "pair", width: 130, render: (v: string) => <span className="font-mono">{v}</span> },
    {
      title: "方向",
      dataIndex: "direction",
      width: 80,
      render: (v: string) => (
        <Tag color={v === "LONG" ? "green" : "red"} className="font-mono">{v}</Tag>
      ),
    },
    {
      title: "开仓时间",
      dataIndex: "open_time",
      width: 130,
      render: (v: string | null) => v ? <span className="text-xs text-t2">{dayjs(v).format("YYYY-MM-DD HH:mm")}</span> : "-",
    },
    {
      title: "平仓时间",
      dataIndex: "close_time",
      width: 130,
      render: (v: string | null) => v ? <span className="text-xs text-t2">{dayjs(v).format("YYYY-MM-DD HH:mm")}</span> : "-",
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
      title: "盈亏金额",
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
    {
      title: "杠杆",
      dataIndex: "leverage",
      width: 70,
      align: "right",
      render: (v: number) => <span className="font-mono">{v}x</span>,
    },
    {
      title: "平仓原因",
      dataIndex: "exit_reason",
      width: 110,
      render: (v: string) => v ? <span className="text-xs">{v}</span> : "-",
    },
  ];

  return (
    <div className="space-y-4">
      {/* 搜索区 */}
      <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-border bg-panel2 p-4">
        <Select
          allowClear
          showSearch
          placeholder="老师"
          style={{ width: 140 }}
          value={filters.teacher}
          onChange={(v) => setFilters((f) => ({ ...f, teacher: v }))}
          options={teachers.map((t) => ({ label: t, value: t }))}
        />
        <Input
          placeholder="币种，如 BTC"
          style={{ width: 140 }}
          allowClear
          value={filters.pair}
          onChange={(e) => setFilters((f) => ({ ...f, pair: e.target.value }))}
        />
        <Select
          allowClear
          placeholder="方向"
          style={{ width: 100 }}
          value={filters.direction}
          onChange={(v) => setFilters((f) => ({ ...f, direction: v }))}
          options={[
            { label: "LONG", value: "LONG" },
            { label: "SHORT", value: "SHORT" },
          ]}
        />
        <Select
          allowClear
          placeholder="盈亏"
          style={{ width: 100 }}
          value={filters.result}
          onChange={(v) => setFilters((f) => ({ ...f, result: v }))}
          options={[
            { label: "盈利", value: "win" },
            { label: "亏损", value: "loss" },
          ]}
        />
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center sm:gap-2">
          <RangePicker
            className="w-full sm:w-auto"
            value={filters.range}
            onChange={(v) =>
              setFilters((f) => ({ ...f, range: v as [Dayjs, Dayjs] | null }))
            }
          />
          <Button
            type="primary"
            icon={<SearchOutlined />}
            onClick={() => setPage(1)}
          >
            查询
          </Button>
          <Button icon={<ReloadOutlined />} onClick={load}>
            刷新
          </Button>
        </div>
      </div>

      <div className="overflow-x-auto rounded-2xl border border-border bg-panel2">
        <Table<TradeItem>
          rowKey="id"
          columns={columns}
          dataSource={data?.items ?? []}
          loading={loading}
          size="small"
          scroll={{ x: 1300 }}
          locale={{ emptyText: <EmptyState text="暂无历史交易" /> }}
          pagination={{
            current: page,
            pageSize,
            total: data?.total ?? 0,
            showSizeChanger: true,
            showTotal: (t) => `共 ${t} 笔`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
        />
      </div>
    </div>
  );
}
