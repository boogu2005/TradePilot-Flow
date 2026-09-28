import React, { useState } from "react";
import { SunOutlined, MoonOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { useTheme } from "../theme";

/** Apple 风格极简登录页（纯 Tailwind，随主题白天/黑夜护眼切换）。 */
export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { mode, toggle } = useTheme();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;
    setLoading(true);
    setError("");
    try {
      await api.login(username.trim(), password);
      // 整页刷新：携带新会话 Cookie 重新探测登录态
      window.location.href = "/";
    } catch {
      setError("用户名或密码错误");
      setLoading(false);
    }
  };

  return (
    <div className="relative flex min-h-screen flex-col bg-panel">
      {/* 居中登录区 */}
      <div className="flex flex-1 items-center justify-center px-8">
        <div className="w-full max-w-sm">
          {/* 品牌 */}
          <div className="text-center">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-[var(--color-text-1)] text-2xl font-bold text-[var(--color-panel2)] shadow-sm">
              B
            </div>
            <h1 className="mt-5 text-[34px] font-semibold leading-tight tracking-tight text-t1">
              Trading Bot
            </h1>
            <p className="mt-2 text-[15px] text-t2">
              OKX 合约交易机器人 · 监控台
            </p>
          </div>

          {/* 表单 */}
          <form onSubmit={submit} className="mt-10 space-y-3.5">
            <input
              type="text"
              autoComplete="username"
              placeholder="用户名"
              value={username}
              onChange={(e) => {
                setUsername(e.target.value);
                if (error) setError("");
              }}
              className={`w-full appearance-none rounded-xl border bg-panel2 px-4 py-3 text-[15px] text-t1 outline-none transition placeholder:text-t3 focus:ring-4 ${
                error
                  ? "border-down/70 focus:border-down focus:ring-down/10"
                  : "border-border focus:border-accent focus:ring-accent/10"
              }`}
            />
            <input
              type="password"
              autoComplete="current-password"
              placeholder="密码"
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                if (error) setError("");
              }}
              className={`w-full appearance-none rounded-xl border bg-panel2 px-4 py-3 text-[15px] text-t1 outline-none transition placeholder:text-t3 focus:ring-4 ${
                error
                  ? "border-down/70 focus:border-down focus:ring-down/10"
                  : "border-border focus:border-accent focus:ring-accent/10"
              }`}
            />
            <button
              type="submit"
              disabled={loading}
              className="mt-2 w-full rounded-full bg-accent py-3 text-[15px] font-medium text-accent-contrast transition-all duration-200 hover:bg-accent/90 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {loading ? "登录中…" : "登 录"}
            </button>
            {error && (
              <p className="pt-1 text-center text-sm text-down">{error}</p>
            )}
          </form>
        </div>
      </div>

      {/* 右下角主题切换（未登录也可切护眼夜） */}
      <button
        onClick={toggle}
        title={mode === "light" ? "切换黑夜护眼模式" : "切换白天模式"}
        aria-label="切换主题"
        className="absolute bottom-6 right-6 flex h-10 w-10 items-center justify-center rounded-full border border-border bg-panel2 text-t2 transition-all duration-200 hover:border-accent/40 hover:text-accent"
      >
        {mode === "light" ? <MoonOutlined /> : <SunOutlined />}
      </button>

      {/* 页脚 */}
      <footer className="pb-8 text-center text-xs text-t3">
        © 2026 Trading Bot · 仅供授权账户访问 · 只读监控，不做任何下单操作
      </footer>
    </div>
  );
}
