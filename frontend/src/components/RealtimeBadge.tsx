import React from "react";
import { Badge } from "antd";

interface Props {
  /** WebSocket 是否已连通 */
  connected: boolean;
  /** 通道静默超时（15s 无帧）——此时页面已切回 REST 轮询兜底 */
  stale: boolean;
}

/**
 * 数据通道三态徽标（antd Badge 语义色，随暗/亮主题）：
 * - 绿点「实时推送」：WS 正常收帧
 * - 琥珀点「轮询兜底」：WS 静默或断开，REST 5s 轮询接管
 * - 灰点「连接中」：首次建立连接
 */
export default function RealtimeBadge({ connected, stale }: Props) {
  const live = connected && !stale;
  const status = live ? "success" : stale ? "warning" : "default";
  const text = live ? "实时推送" : stale ? "轮询兜底" : "连接中";
  return (
    <span title="数据通道状态（实时推送 ≠ 机器人在线）">
      <Badge status={status} text={<span className="text-xs">{text}</span>} />
    </span>
  );
}
