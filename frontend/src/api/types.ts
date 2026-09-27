export interface AccountInfo {
  balance: number;
  usdt_balance: number;
  equity: number;
  available: number;
  open_positions: number;
  unrealized_pnl: number;
  today_pnl: number;
  today_roi_pct: number;
  total_pnl: number;
  total_roi_pct: number;
  total_trades: number;
  total_win_rate: number;
  source: string;
  updated_at: string;
}

export interface BotStatus {
  online: boolean;
  last_trade_at: string | null;
  today_trades: number;
  today_wins: number;
  today_losses: number;
  win_rate_7d: number;
  closed_7d: number;
  wins_7d: number;
  losses_7d: number;
  strategy: string;
  last_event_at: string | null;
  db_available: boolean;
}

export interface AccountSummary {
  account: AccountInfo;
  bot: BotStatus;
}

export interface Position {
  id: number;
  pair: string;
  direction: "LONG" | "SHORT";
  open_price: number;
  current_price: number;
  price_source: string;
  leverage: number;
  margin: number;
  quantity: number;
  amount: number;
  unrealized_pnl: number;
  roi_pct: number;
  stop_loss: number | null;
  tp1_price: number | null;
  tp_state: string;
  position_state: string;
  teacher: string | null;
  leverage_level: number;
  open_time: string | null;
}

export interface TradeItem {
  id: number;
  teacher: string;
  pair: string;
  direction: "LONG" | "SHORT";
  open_time: string | null;
  close_time: string | null;
  open_price: number;
  close_price: number;
  pnl: number;
  pnl_pct: number;
  hold_seconds: number | null;
  is_profit: boolean;
  leverage: number;
  margin: number;
  exit_reason: string;
  strategy: string;
}

export interface TradesResponse {
  items: TradeItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface TeacherRankItem {
  rank: number;
  teacher: string;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_profit: number;
  total_loss: number;
  net_profit: number;
  profit_factor: number | null;
  avg_win: number;
  avg_loss: number;
  max_win: number;
  max_loss: number;
  roi: number;
  avg_hold_hours: number;
  last_trade_time: string | null;
  period_days: number;
  /** 动态仓位分配榜：风险调整得分 = r̄ / σ */
  mean_ratio?: number;
  std_ratio?: number;
  risk_adjusted_score?: number;
  degenerate?: boolean;
  ratio_trades?: number;
  eligible?: boolean;
  /** 当月快照锁定档位（UI 增强：实时排名 ≠ 交易档位） */
  snapshot_month?: string | null;
  tier_rank?: number | null;
  position_ratio?: number | null;
  tier_band?: string | null;
}

export interface RankingResponse {
  period: number;
  type: string;
  items: TeacherRankItem[];
}

export interface CurvePoint {
  date?: string;
  time?: string;
  cumulative_pnl?: number;
  balance?: number;
  equity?: number;
  unrealized_pnl?: number;
  pnl?: number;
  cumulative?: number;
}

export interface TeacherDetail {
  teacher: string;
  stats: TeacherRankItem;
  window_stats: TeacherRankItem;
  window_days: number;
  risk: {
    win_rate: number;
    profit_factor: number | null;
    max_drawdown: number;
    sharpe_ratio: number;
    avg_hold_hours: number;
    avg_win: number;
    avg_loss: number;
    max_win: number;
    max_loss: number;
  };
  curves: {
    d7: CurvePoint[];
    d30: CurvePoint[];
    all: CurvePoint[];
  };
}

export interface DailyPnlItem {
  date: string;
  pnl: number;
  wins: number;
  losses: number;
  count: number;
}

export interface EquityPoint {
  time?: string;
  balance?: number;
  equity?: number;
  unrealized_pnl?: number;
  date?: string;
  pnl?: number;
  cumulative?: number;
}

/** 机器人 systemd 服务状态（/api/bot/status） */
export interface ServiceState {
  name: string;
  probe_ok: boolean;
  active_state: string;
  sub_state: string;
  load_state: string;
  pid: number | null;
  since: string | null;
  since_raw: string | null;
  restarts: number | null;
  memory_bytes: number | null;
}

export interface StatusLogs {
  file: string;
  available: boolean;
  lines: string[];
  truncated: boolean;
}

export interface TodayOverview {
  trade_count: number;
  wins: number;
  losses: number;
  win_rate: number;
  realized_profit: number;
  open_positions: number;
  last_close_at: string | null;
}

export interface RecentClosedTrade {
  id: number;
  pair: string;
  direction: "LONG" | "SHORT";
  pnl: number;
  close_time: string | null;
  exit_reason: string;
}

export interface BotStatusResponse {
  service: ServiceState;
  logs: StatusLogs;
  today: TodayOverview;
  recent: RecentClosedTrade[];
  bot_db_available: boolean;
  generated_at: string;
}
