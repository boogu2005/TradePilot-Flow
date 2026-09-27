import React, { useEffect, useMemo, useRef, useState } from "react";
import { Button, Skeleton, Tag } from "antd";
import { ArrowDownOutlined, ArrowUpOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import EmptyState from "../components/EmptyState";
import PnlText from "../components/PnlText";
import { usePolling } from "../hooks/usePolling";
import { api } from "../api/client";
import type {
  BotStatusResponse,
  RecentClosedTrade,
  ServiceState,
} from "../api/types";

/** 日志行 → 语义色（先看 loguru 残留标记，再看文本级别），并剥掉 <...> 标记。 */
function lineMeta(raw: string): { text: string; className: string } {
  const tag = raw.match(/<(red|yellow|green)>/i)?.[1]?.toLowerCase();
  let className = "text-t2";
  if (tag === "red" || /\b(ERROR|CRITICAL)\b/.test(raw)) {
    className = "text-down";
  } else if (tag === "yellow" || /\bWARNING\b/.test(raw)) {
    className = "text-warn";
  } else if (tag === "green" || /\bSUCCESS\b/.test(raw)) {
    className = "text-up";
  }
  return { text: raw.replace(/<[^>]*>/g, ""), className };
}

/** systemd 状态 → 徽章语义（纯展示）。 */
function serviceBadge(svc: ServiceState | undefined) {
  if (!svc) {
    return { dot: "bg-t3", label: "不可用", note: "服务状态不可用" };
  }
  const state = svc.active_state ?? "unknown";
  if (!svc.probe_ok) {
    return { dot: "bg-t3", label: "不可用", note: "systemd 探测不可用" };
  }
  switch (state) {
    case "active":
      return {
        dot: "bg-up",
        label: "运行中",
        note: svc.sub_state === "running" ? "正常运行" : `状态:${svc.sub_state}`,
      };
    case "inactive":
      return {
        dot: "bg-t3",
        label: "已停止",
        note: "机器人未运行 —— 以下为数据库历史镜像，今日数据为 0",
      };
    case "failed":
      return { dot: "bg-down", label: "异常", note: "服务启动失败，请到服务器排查" };
    case "activating":
    case "deactivating":
      return { dot: "bg-warn", label: "切换中", note: `状态:${svc.sub_state}` };
    default:
      return { dot: "bg-t3", label: state, note: `状态:${svc.sub_state}` };
  }
}

function ServiceCard({ svc }: { svc: ServiceState | undefined }) {
  const badge = serviceBadge(svc);
  const isStopped = svc?.active_state === "inactive";

  const rows: Array<[string, string | number]> = [
    ["加载状态", svc?.load_state || "—"],
    ["进程 PID", svc?.pid ?? "—"],
    [
      "启动时间",
      svc?.since ? dayjs(svc.since).format("MM-DD HH:mm:ss") : svc?.since_raw ?? "—",
    ],
    ["重启次数", svc?.restarts ?? 0],
    [
      "常驻内存",
      svc?.memory_bytes ? `${(svc.memory_bytes / 1048576).toFixed(0)} MB` : "—",
    ],
  ];

  return (
    <div className="rounded-2xl border border-border bg-panel2 p-5">
      <div className="text-xs tracking-wide text-t2">服务状态</div>
      <div className="mt-3 flex items-center gap-3">
        <span className="relative flex h-3 w-3">
          {badge.label === "运行中" && (
            <span
              className={`absolute inline-flex h-full w-full animate-ping rounded-full ${badge.dot} opacity-60`}
            />
          )}
          <span
            className={`relative inline-flex h-3 w-3 rounded-full ${badge.dot}`}
          />
        </span>
        <span className="text-lg font-semibold text-t1">{badge.label}</span>
        <Tag className="ml-1 font-mono text-[11px]">{svc?.name ?? "—"}</Tag>
      </div>
      <div className={`mt-1 text-xs ${isStopped ? "text-warn" : "text-t2"}`}>
        {badge.note}
      </div>
      <div className="mt-4 space-y-2 border-t border-border pt-4">
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="flex items-center justify-between text-xs"
          >
            <span className="text-t2">{label}</span>
            <span className="font-mono text-t1">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RecentRow({ trade }: { trade: RecentClosedTrade }) {
  return (
    <div className="flex items-center gap-2 rounded-lg px-1 py-1.5 text-xs hover:bg-hover">
      <Tag
        color={trade.direction === "LONG" ? "green" : "red"}
        className="m-0 w-14 text-center font-mono"
      >
        {trade.direction}
      </Tag>
      <span className="min-w-0 flex-1 truncate font-medium text-t1">
        {trade.pair}
      </span>
      <PnlText value={trade.pnl} precision={4} className="whitespace-nowrap" />
      <span className="hidden whitespace-nowrap text-t3 sm:inline">
        {trade.close_time ? dayjs(trade.close_time).format("MM-DD HH:mm") : "—"}
      </span>
      <span className="hidden max-w-[180px] truncate text-t3 lg:inline">
        {trade.exit_reason || "—"}
      </span>
    </div>
  );
}

function TodayCard({ data }: { data: BotStatusResponse | null }) {
  const today = data?.today;
  const items: Array<{ label: string; node: React.ReactNode }> = [
    { label: "今日成交", node: <span>{today?.trade_count ?? 0}</span> },
    {
      label: "今日盈亏",
      node: <PnlText value={today?.realized_profit ?? 0} suffix=" USDT" />,
    },
    {
      label: "胜率",
      node: <span>{(today?.win_rate ?? 0).toFixed(2)}%</span>,
    },
    {
      label: "当前持仓",
      node: <span>{today?.open_positions ?? 0}</span>,
    },
  ];

  return (
    <div className="rounded-2xl border border-border bg-panel2 p-5">
      <div className="text-xs tracking-wide text-t2">今日概况</div>
      <div className="mt-3 grid grid-cols-2 gap-3">
        {items.map((item) => (
          <div key={item.label} className="rounded-xl bg-hover/60 px-3 py-2.5">
            <div className="text-[11px] text-t2">{item.label}</div>
            <div className="mt-0.5 font-mono text-lg font-semibold text-t1">
              {item.node}
            </div>
          </div>
        ))}
      </div>
      <div className="mt-4 border-t border-border pt-3">
        <div className="mb-1 flex items-center justify-between">
          <span className="text-[11px] font-medium text-t2">最近平仓</span>
          <span className="text-[10px] text-t3">历史镜像 · 不限今日</span>
        </div>
        {data?.recent && data.recent.length > 0 ? (
          <div className="space-y-0.5">
            {data.recent.map((trade) => (
              <RecentRow key={trade.id} trade={trade} />
            ))}
          </div>
        ) : (
          <EmptyState text="暂无平仓记录" compact />
        )}
      </div>
    </div>
  );
}

function LogCard({ data }: { data: BotStatusResponse | null }) {
  const [autoScroll, setAutoScroll] = useState(true);
  const bodyRef = useRef<HTMLDivElement>(null);
  const lines = data?.logs?.lines ?? [];

  // 服务端已按新 → 旧排列，新日志到达时回到顶部
  useEffect(() => {
    if (autoScroll && bodyRef.current) bodyRef.current.scrollTop = 0;
  }, [lines, autoScroll]);

  const rendered = useMemo(
    () =>
      lines.map((raw, i) => {
        const meta = lineMeta(raw);
        return (
          <div
            key={`${i}-${raw.length}`}
            className={`whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed ${meta.className}`}
          >
            {meta.text || " "}
          </div>
        );
      }),
    [lines],
  );

  return (
    <div className="rounded-2xl border border-border bg-panel2 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-xs tracking-wide text-t2">最近日志</span>
          <code className="rounded bg-hover px-1.5 py-0.5 font-mono text-[10px] text-t3">
            user_data/logs/{data?.logs?.file ?? "systemd.log"}
          </code>
          {data?.logs?.truncated && (
            <span className="text-[10px] text-t3">(仅尾部，已截断)</span>
          )}
        </div>
        <Button
          size="small"
          icon={
            autoScroll ? <ArrowDownOutlined /> : <ArrowUpOutlined />
          }
          onClick={() => setAutoScroll((v) => !v)}
          className="text-xs"
        >
          {autoScroll ? "自动滚动" : "已冻结"}
        </Button>
      </div>
      <div
        ref={bodyRef}
        className="mt-3 max-h-[420px] overflow-y-auto rounded-xl bg-panel p-3"
      >
        {data?.logs?.available === false ? (
          <EmptyState text="日志文件不可用" compact />
        ) : rendered.length === 0 ? (
          <EmptyState text="暂无日志" compact />
        ) : (
          rendered
        )}
      </div>
    </div>
  );
}

export default function StatusPage() {
  const { data, loading } = usePolling<BotStatusResponse>(
    () => api.getBotStatus(300),
    10000,
  );

  if (loading && !data) {
    return (
      <div className="space-y-4">
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <ServiceCard svc={data?.service} />
        <TodayCard data={data} />
      </div>
      <LogCard data={data} />
    </div>
  );
}
