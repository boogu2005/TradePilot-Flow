import React from "react";
import { InboxOutlined } from "@ant-design/icons";

export default function EmptyState({
  text = "暂无数据",
  compact = false,
}: {
  text?: string;
  compact?: boolean;
}) {
  return (
    <div
      className={`flex flex-col items-center justify-center text-t2 ${
        compact ? "py-6" : "py-16"
      }`}
    >
      <InboxOutlined className={`opacity-40 ${compact ? "text-2xl" : "text-4xl"}`} />
      <div className={`${compact ? "mt-1.5 text-xs" : "mt-3 text-sm"}`}>
        {text}
      </div>
    </div>
  );
}
