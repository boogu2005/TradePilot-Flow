import axios from "axios";
import type {
  AccountSummary,
  BotStatusResponse,
  DailyPnlItem,
  EquityPoint,
  Position,
  RankingResponse,
  TeacherDetail,
  TeacherRankItem,
  TradesResponse,
} from "./types";

const http = axios.create({
  baseURL: "/api",
  timeout: 15000,
  withCredentials: true, // 携带 HttpOnly 会话 Cookie
});

// 会话失效（401，且非登录请求自身）时跳转登录页
http.interceptors.response.use(
  (res) => res,
  (err) => {
    if (
      err.response?.status === 401 &&
      !String(err.config?.url ?? "").includes("/login")
    ) {
      if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }
    return Promise.reject(err);
  },
);

export async function login(
  username: string,
  password: string,
): Promise<{ ok: boolean }> {
  const { data } = await http.post("/login", { username, password });
  return data;
}

export async function logout(): Promise<{ ok: boolean }> {
  const { data } = await http.post("/logout");
  return data;
}

export async function getMe(): Promise<{ authed: boolean; username?: string }> {
  const { data } = await http.get("/me");
  return data;
}

export async function getAccount(): Promise<AccountSummary> {
  const { data } = await http.get("/account");
  return data;
}

export async function getPositions(live = true): Promise<Position[]> {
  const { data } = await http.get("/positions", { params: { live } });
  return data.items;
}

export interface TradesQuery {
  teacher?: string;
  pair?: string;
  start?: string;
  end?: string;
  direction?: string;
  result?: string;
  page?: number;
  page_size?: number;
}

export async function getTrades(query: TradesQuery): Promise<TradesResponse> {
  const { data } = await http.get("/trades", { params: query });
  return data;
}

export async function getTeachers(): Promise<TeacherRankItem[]> {
  const { data } = await http.get("/teachers");
  return data.items;
}

export async function getRanking(
  period: number,
  type: string,
  limit = 50,
): Promise<RankingResponse> {
  const { data } = await http.get("/teachers/ranking", {
    params: { period, type, limit },
  });
  return data;
}

export async function getTeacherDetail(
  teacher: string,
  days = 0,
): Promise<TeacherDetail> {
  const { data } = await http.get(`/teachers/${encodeURIComponent(teacher)}`, {
    params: { days },
  });
  return data;
}

export async function getTeacherTrades(
  teacher: string,
  page: number,
  page_size: number,
): Promise<TradesResponse> {
  const { data } = await http.get(
    `/teachers/${encodeURIComponent(teacher)}/trades`,
    { params: { page, page_size } },
  );
  return data;
}

export async function getEquity(days = 30): Promise<EquityPoint[]> {
  const { data } = await http.get("/charts/equity", { params: { days } });
  return data.points;
}

export async function getDailyPnl(days = 30): Promise<DailyPnlItem[]> {
  const { data } = await http.get("/charts/daily-pnl", { params: { days } });
  return data.items;
}

export async function getPnlDistribution(): Promise<{
  bins: number[];
  counts: number[];
}> {
  const { data } = await http.get("/charts/pnl-distribution");
  return data;
}

export async function getBotStatus(lines = 200): Promise<BotStatusResponse> {
  const { data } = await http.get("/bot/status", { params: { lines } });
  return data;
}

export const api = {
  login,
  logout,
  getMe,
  getAccount,
  getPositions,
  getTrades,
  getTeachers,
  getRanking,
  getTeacherDetail,
  getTeacherTrades,
  getEquity,
  getDailyPnl,
  getPnlDistribution,
  getBotStatus,
};

export default api;
