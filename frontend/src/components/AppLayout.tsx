import React, { useEffect, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  DashboardOutlined,
  FundOutlined,
  HistoryOutlined,
  TrophyOutlined,
  BarChartOutlined,
  SyncOutlined,
  MenuOutlined,
  CloseOutlined,
  LogoutOutlined,
  SunOutlined,
  MoonOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import { api } from "../api/client";
import { useTheme } from "../theme";

const navItems = [
  { key: "/", label: "总览", icon: <DashboardOutlined /> },
  { key: "/positions", label: "当前持仓", icon: <FundOutlined /> },
  { key: "/trades", label: "历史交易", icon: <HistoryOutlined /> },
  { key: "/ranking", label: "老师排行榜", icon: <TrophyOutlined /> },
  { key: "/charts", label: "图表分析", icon: <BarChartOutlined /> },
  { key: "/status", label: "机器人状态", icon: <SyncOutlined /> },
];

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { mode, toggle } = useTheme();

  const activeKey =
    navItems
      .filter((item) =>
        item.key === "/"
          ? location.pathname === "/"
          : location.pathname.startsWith(item.key),
      )
      .map((item) => item.key)[0] ?? "/";

  // 页面标题随路由
  useEffect(() => {
    const base = navItems.find((item) => item.key === activeKey)?.label ?? "总览";
    document.title = `${base} · Trading Bot 监控台`;
  }, [activeKey]);

  // 移动端抽屉打开时锁定背景滚动
  useEffect(() => {
    document.body.style.overflow = drawerOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [drawerOpen]);

  const handleLogout = async () => {
    try {
      await api.logout();
    } finally {
      window.location.href = "/login";
    }
  };

  const SidebarContent = (
    <div className="flex h-full flex-col">
      {/* 品牌区 */}
      <div className="flex items-center gap-3 px-5 pb-4 pt-6">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-border bg-accent/10 text-xl text-accent" aria-label="Trading Bot">
          <RobotOutlined />
        </div>
        <div>
          <div className="text-[15px] font-semibold tracking-tight text-t1">
            Trading Bot
          </div>
          <div className="text-[11px] text-t2">OKX 合约 · 监控台</div>
        </div>
      </div>
      {/* 导航 */}
      <nav className="flex-1 space-y-1 overflow-y-auto px-3">
        {navItems.map((item) => (
          <button
            key={item.key}
            onClick={() => {
              navigate(item.key);
              setDrawerOpen(false);
            }}
            className={`flex w-full items-center gap-3 rounded-full px-4 py-2.5 text-left text-sm transition-all duration-200 ${
              activeKey === item.key
                ? "bg-accent/10 font-semibold text-t1"
                : "text-t2 hover:bg-hover hover:text-t1"
            }`}
          >
            <span className={activeKey === item.key ? "text-accent" : ""}>
              {item.icon}
            </span>
            {item.label}
          </button>
        ))}
      </nav>
      {/* 页脚 */}
      <div className="border-t border-border px-5 py-3 text-[11px] text-t3">
        v1.0.0 · 只读后台
      </div>
    </div>
  );

  return (
    <div className="flex min-h-screen bg-panel text-t1">
      {/* 桌面端侧边栏 */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 border-r border-border bg-panel2 lg:block">
        {SidebarContent}
      </aside>

      {/* 移动端抽屉 */}
      {drawerOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => setDrawerOpen(false)}
          />
          <aside className="absolute inset-y-0 left-0 w-64 border-r border-border bg-panel2">
            <button
              className="absolute right-2 top-2 flex h-10 w-10 items-center justify-center rounded-full text-base text-t2 transition-colors hover:bg-hover hover:text-t1"
              onClick={() => setDrawerOpen(false)}
              aria-label="关闭菜单"
            >
              <CloseOutlined />
            </button>
            {SidebarContent}
          </aside>
        </div>
      )}

      <div className="flex min-h-screen w-full flex-col lg:pl-60">
        {/* 顶栏 */}
        <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-border bg-panel/80 px-4 py-3 backdrop-blur-xl lg:px-8">
          <button
            className="-ml-2 flex h-10 w-10 items-center justify-center rounded-full text-lg text-t2 transition-colors hover:bg-hover lg:hidden"
            onClick={() => setDrawerOpen(true)}
            aria-label="打开菜单"
          >
            <MenuOutlined />
          </button>
          <h1 className="text-[15px] font-semibold tracking-tight text-t1">
            {navItems.find((item) => item.key === activeKey)?.label ??
              "总览"}
          </h1>
          <div className="ml-auto flex items-center gap-2">
            <span className="hidden rounded-full border border-border bg-panel2 px-3 py-1 font-mono text-[11px] text-t2 sm:inline">
              {new Date().toLocaleTimeString("zh-CN", { hour12: false })}
            </span>
            {/* 主题切换：胶囊滑钮 */}
            <button
              onClick={toggle}
              title={
                mode === "light" ? "切换黑夜护眼模式" : "切换白天模式"
              }
              aria-label="切换主题"
              className="relative inline-flex h-9 w-16 items-center rounded-full border border-border bg-panel2 p-0.5 transition-all duration-200 hover:border-accent/40"
            >
              <span
                className={`absolute left-0.5 top-0.5 flex h-8 w-8 items-center justify-center rounded-full text-[13px] transition-all duration-300 ${
                  mode === "dark"
                    ? "translate-x-6 bg-accent text-accent-contrast shadow-sm"
                    : "bg-panel text-t2"
                }`}
              >
                {mode === "dark" ? <SunOutlined /> : <MoonOutlined />}
              </span>
            </button>
            <button
              onClick={handleLogout}
              title="退出登录"
              aria-label="退出登录"
              className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-panel2 text-t2 transition-all duration-200 hover:border-down/40 hover:text-down"
            >
              <LogoutOutlined />
            </button>
          </div>
        </header>
        <main className="flex-1 p-4 lg:p-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
