import React, { useMemo } from "react";
import { Alert, Skeleton, Tag } from "antd";
import {
  ApiOutlined,
  DollarOutlined,
  LineChartOutlined,
  RocketOutlined,
  SwapOutlined,
  ThunderboltOutlined,
  WalletOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import StatCard from "../components/StatCard";
import PnlText from "../components/PnlText";
import EmptyState from "../components/EmptyState";
import RealtimeBadge from "../components/RealtimeBadge";
import EquityChart from "../charts/EquityChart";
import DailyPnlChart from "../charts/DailyPnlChart";
import { usePolling } from "../hooks/usePolling";
import { useWebSocket } from "../hooks/useWebSocket";
import { api } from "../api/client";
import type { AccountSummary } from "../api/types";
import { fmtUSD } from "../utils/format";

export default function DashboardPage() {
  // 账户/机器人卡区：WS 推送为主通道，断线时自动回落到 5s REST 轮询
  const ws = useWebSocket();
  const wsActive = ws.connected && !ws.stale;
  const { data, loading, error } = usePolling<AccountSummary>(
    api.getAccount,
    5000,
    !wsActive, // WS 活跃时停表；掉线/静默后自动恢复
  );
  // 图表数据（分钟级变化）：保留慢轮询，不占用 WS
  const { data: equity } = usePolling(() => api.getEquity(30), 30000);
  const { data: dailyPnl } = usePolling(() => api.getDailyPnl(14), 30000);

  const account = ws.snapshot?.account ?? data?.account;
  const bot = ws.snapshot?.bot ?? data?.bot;
  const loadingView = loading && !ws.snapshot;

  const botTag = useMemo(() => {
    if (!bot) return <Tag>加载中</Tag>;
    return bot.online ? (
      <Tag color="success" icon={<ThunderboltOutlined />}>
        在线
      </Tag>
    ) : (
      <Tag color="error" icon={<ApiOutlined />}>
        离线
      </Tag>
    );
  }, [bot]);

  return (
    <div className="space-y-6">
      {error && (
        <Alert
          type="warning"
          showIcon
          message="后端连接异常"
          description={error}
        />
      )}

      {/* 账户概览 */}
      <section>
        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-t2">
          账户概览 · 数据源 {account?.source ?? "-"}
          <RealtimeBadge connected={ws.connected} stale={ws.stale} />
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
          <StatCard
            title="OKX 账户余额 (USDT)"
            value={
              loadingView ? <Skeleton.Input size="small" active /> : (
                <span className="text-accent">{fmtUSD(account?.balance)}</span>
              )
            }
            icon={<WalletOutlined />}
          />
          <StatCard
            title="USDT 可用余额"
            value={
              loadingView ? <Skeleton.Input size="small" active /> : (
                <span>{fmtUSD(account?.available)}</span>
              )
            }
            icon={<DollarOutlined />}
          />
          <StatCard
            title="当前权益"
            value={
              loadingView ? <Skeleton.Input size="small" active /> : (
                <span>{fmtUSD(account?.equity)}</span>
              )
            }
            icon={<LineChartOutlined />}
            accent
          />
          <StatCard
            title="当前持仓数量"
            value={loading ? <Skeleton.Input size="small" active /> : account?.open_positions}
            icon={<SwapOutlined />}
          />
          <StatCard
            title="近7日胜率"
            value={
              loadingView ? <Skeleton.Input size="small" active /> : (
                <span
                  className={
                    bot && bot.closed_7d > 0 && bot.win_rate_7d >= 50
                      ? "text-up"
                      : ""
                  }
                >
                  {bot && bot.closed_7d > 0
                    ? `${bot.win_rate_7d.toFixed(2)}%`
                    : "—"}
                </span>
              )
            }
            sub={
              bot ? (
                bot.closed_7d > 0 ? (
                  <span>
                    已平仓 {bot.closed_7d} 笔 · 胜 {bot.wins_7d} / 负{" "}
                    {bot.losses_7d}
                  </span>
                ) : (
                  <span>近 7 天暂无平仓记录</span>
                )
              ) : undefined
            }
          />
          <StatCard
            title="今日收益"
            value={
              loadingView ? <Skeleton.Input size="small" active /> : (
                <PnlText value={account?.today_pnl ?? 0} suffix=" USDT" />
              )
            }
            sub={
              account ? (
                <PnlText
                  value={account.today_roi_pct}
                  suffix="%"
                  precision={2}
                />
              ) : undefined
            }
          />
          <StatCard
            title="总收益"
            value={
              loadingView ? <Skeleton.Input size="small" active /> : (
                <PnlText value={account?.total_pnl ?? 0} suffix=" USDT" />
              )
            }
            sub={
              account ? (
                <PnlText
                  value={account.total_roi_pct}
                  suffix="%"
                  precision={2}
                />
              ) : undefined
            }
          />
          <StatCard
            title="累计胜率"
            value={
              loadingView ? <Skeleton.Input size="small" active /> : (
                <span>{account?.total_win_rate.toFixed(2)}%</span>
              )
            }
          />
        </div>
      </section>

      {/* 机器人状态 */}
      <section>
        <h2 className="mb-3 text-sm font-semibold text-t2">机器人状态</h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatCard
            title="在线状态"
            value={botTag}
            icon={<RocketOutlined />}
          />
          <StatCard
            title="最近一次交易"
            value={
              <span className="text-sm">
                {bot?.last_trade_at
                  ? dayjs(bot.last_trade_at).format("MM-DD HH:mm")
                  : "暂无交易"}
              </span>
            }
          />
          <StatCard
            title="今日交易次数"
            value={
              <span>{bot?.today_trades ?? 0}</span>
            }
            sub={
              bot ? (
                <span>
                  胜 {bot.today_wins} / 负 {bot.today_losses}
                </span>
              ) : undefined
            }
          />
          <StatCard
            title="当前运行策略"
            value={<span className="text-sm">{bot?.strategy ?? "-"}</span>}
          />
        </div>
      </section>

      {/* 图表 */}
      <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="rounded-2xl border border-border bg-panel2 p-5">
          <h3 className="mb-2 text-sm font-semibold text-t1">
            账户资金曲线（近 30 天）
          </h3>
          {equity && equity.length > 0 ? (
            <EquityChart points={equity} height={300} />
          ) : (
            <div className="flex h-[300px] items-center justify-center">
              <EmptyState text="暂无快照数据" compact />
            </div>
          )}
        </div>
        <div className="rounded-2xl border border-border bg-panel2 p-5">
          <h3 className="mb-2 text-sm font-semibold text-t1">
            每日盈亏（近 14 天）
          </h3>
          {dailyPnl && dailyPnl.length > 0 ? (
            <DailyPnlChart items={dailyPnl} height={300} />
          ) : (
            <div className="flex h-[300px] items-center justify-center">
              <EmptyState text="暂无交易数据" compact />
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
