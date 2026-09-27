import React, { useMemo, useRef } from "react";
import type { ReactNode } from "react";

interface StatCardProps {
  title: string;
  value: ReactNode;
  sub?: ReactNode;
  icon?: ReactNode;
  accent?: boolean;
}

/** 提取可比较的文本指纹（string/number/element 的 children 或 value 属性）。 */
function nodeText(node: ReactNode): string | null {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("|");
  if (React.isValidElement(node)) {
    const props = node.props as { children?: ReactNode; value?: unknown };
    if (props.children !== undefined) return nodeText(props.children);
    if (props.value !== undefined) return String(props.value);
    return null; // 无文本内容（如 Skeleton），不触发动效
  }
  return null;
}

export default function StatCard({
  title,
  value,
  sub,
  icon,
  accent = false,
}: StatCardProps) {
  const prevRef = useRef<string | null>(null);
  const text = useMemo(() => nodeText(value), [value]);
  const changed = text !== null && text !== prevRef.current;
  prevRef.current = text;

  return (
    <div
      className={`min-w-0 rounded-2xl border p-4 transition-colors sm:p-5 ${
        accent
          ? "border-accent/30 bg-gradient-to-br from-accent-card-from to-accent-card-to"
          : "border-border bg-panel2"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 truncate text-xs tracking-wide text-t2">
          {title}
        </div>
        {icon && <div className="shrink-0 text-base text-t3">{icon}</div>}
      </div>
      <div
        key={changed ? `${title}-${text}` : undefined}
        className={`mt-2.5 min-w-0 break-words font-mono text-xl font-semibold tracking-tight text-t1 sm:text-2xl ${
          changed ? "animate-flash" : ""
        }`}
      >
        {value}
      </div>
      {sub && <div className="mt-1.5 text-xs text-t2">{sub}</div>}
    </div>
  );
}
