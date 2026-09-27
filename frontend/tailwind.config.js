/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 语义色：值取自 CSS 变量，由白天/黑夜主题切换
        panel: "var(--color-panel)",
        panel2: "var(--color-panel2)",
        hover: "var(--color-hover)",
        border: "var(--color-border)",
        t1: "var(--color-text-1)",
        t2: "var(--color-text-2)",
        t3: "var(--color-text-3)",
        accent: "var(--color-accent)",
        "accent-contrast": "var(--color-accent-contrast)",
        "accent-card-from": "var(--color-accent-card-from)",
        "accent-card-to": "var(--color-accent-card-to)",
        up: "var(--color-up)",
        down: "var(--color-down)",
        warn: "var(--color-warn)",
      },
      fontFamily: {
        mono: ["JetBrains Mono", "SFMono-Regular", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
  corePlugins: {
    preflight: false, // 与 AntD 冲突，关闭基础样式重置
  },
};
