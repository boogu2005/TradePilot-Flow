import React, { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { ThemeProvider, useTheme } from "./theme";
import AppLayout from "./components/AppLayout";
import DashboardPage from "./pages/DashboardPage";
import LoginPage from "./pages/LoginPage";
import PositionsPage from "./pages/PositionsPage";
import TradesPage from "./pages/TradesPage";
import RankingPage from "./pages/RankingPage";
import TeacherDetailPage from "./pages/TeacherDetailPage";
import ChartsPage from "./pages/ChartsPage";
import StatusPage from "./pages/StatusPage";
import { api } from "./api/client";

/** 登录态探测中的极简加载页。 */
function LoadingSplash() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-panel">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
    </div>
  );
}

const fontFamily =
  '-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", sans-serif';

/** 亮色：OKX 白纸黑字极简（黑白灰 + 红绿语义）。 */
const lightTheme = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: "#000000",
    colorLink: "#000000",
    colorSuccess: "#0a9c5e",
    colorError: "#e5484d",
    colorWarning: "#b25e00",
    colorBgBase: "#f7f7f8",
    colorBgContainer: "#ffffff",
    colorBgElevated: "#ffffff",
    colorBorder: "#e7e7ea",
    colorText: "#17171a",
    colorTextSecondary: "#6e6e73",
    borderRadius: 12,
    controlHeight: 40,
    fontFamily,
  },
  components: {
    Table: {
      headerBg: "#f7f7f8",
      rowHoverBg: "#efeff2",
      borderColor: "#e7e7ea",
      headerSplitColor: "#e7e7ea",
    },
    Button: {
      borderRadius: 999, // 胶囊按钮
    },
    Tag: {
      borderRadiusSM: 999, // 胶囊标签
    },
  },
};

/** 暗色：OKX 深度暗色（黑纸白字镜像，红绿语义保留夜视低炫光）。 */
const darkTheme = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: "#ffffff",
    colorLink: "#ffffff",
    colorSuccess: "#30d158",
    colorError: "#ff453a",
    colorWarning: "#e0a83e",
    colorBgBase: "#0d0f12",
    colorBgContainer: "#16181c",
    colorBgElevated: "#1f2228",
    colorBorder: "#2a2d33",
    colorText: "#e8eaed",
    colorTextSecondary: "#9ba1aa",
    borderRadius: 12,
    controlHeight: 40,
    fontFamily,
  },
  components: {
    Table: {
      headerBg: "#1b1e24",
      rowHoverBg: "#202328",
      borderColor: "#2a2d33",
      headerSplitColor: "#2a2d33",
    },
    Button: {
      borderRadius: 999,
    },
    Tag: {
      borderRadiusSM: 999,
    },
  },
};

function ThemedApp() {
  const { mode } = useTheme();
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    api
      .getMe()
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false));
  }, []);

  if (authed === null) {
    return <LoadingSplash />;
  }

  return (
    <ConfigProvider
      locale={zhCN}
      theme={mode === "light" ? lightTheme : darkTheme}
    >
      <BrowserRouter>
        <Routes>
          <Route
            path="/login"
            element={authed ? <Navigate to="/" replace /> : <LoginPage />}
          />
          <Route
            path="/"
            element={authed ? <AppLayout /> : <Navigate to="/login" replace />}
          >
            <Route index element={<DashboardPage />} />
            <Route path="positions" element={<PositionsPage />} />
            <Route path="trades" element={<TradesPage />} />
            <Route path="ranking" element={<RankingPage />} />
            <Route path="teachers/:teacher" element={<TeacherDetailPage />} />
            <Route path="charts" element={<ChartsPage />} />
            <Route path="status" element={<StatusPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ConfigProvider>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <ThemedApp />
    </ThemeProvider>
  );
}
