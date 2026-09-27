import { useEffect, useRef, useState } from "react";
import type { AccountInfo, BotStatus, Position } from "../api/types";

/** 服务端每 5s 广播一帧快照（backend/services/background.py）。 */
export interface WsSnapshot {
  type: string;
  time: string;
  account?: AccountInfo;
  bot?: BotStatus;
  positions?: Position[];
}

/** 超过该时长未收到任何帧，判定通道静默失效，主动断开以触发重连。 */
const STALE_AFTER_MS = 15000;

/**
 * WebSocket 实时快照 Hook。断线自动重连（3s 退避）。
 * 通道无消息超过 15s 时标记 stale 并主动重连 —— 页面据此切回 REST 轮询兜底。
 */
export function useWebSocket(enabled = true) {
  const [connected, setConnected] = useState(false);
  const [stale, setStale] = useState(false);
  const [snapshot, setSnapshot] = useState<WsSnapshot | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const lastMsgAtRef = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let retry: ReturnType<typeof setTimeout>;
    let staleTimer: ReturnType<typeof setInterval>;

    const connect = () => {
      // 与后端同源直连（生产）或经 vite /api 代理（开发）——真实路径是 /api/ws
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const host = window.location.host;
      const ws = new WebSocket(`${proto}://${host}/api/ws`);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        setStale(false);
        lastMsgAtRef.current = Date.now();
      };
      ws.onclose = () => {
        setConnected(false);
        if (!disposed) retry = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (event) => {
        lastMsgAtRef.current = Date.now();
        try {
          setSnapshot(JSON.parse(event.data));
        } catch {
          /* 忽略非 JSON 帧 */
        }
      };
    };

    connect();

    // 静默断线检测：连上但 15s 无帧 → 标记 stale 并重连
    staleTimer = setInterval(() => {
      if (disposed) return;
      if (Date.now() - lastMsgAtRef.current > STALE_AFTER_MS) {
        setStale(true);
        wsRef.current?.close();
      }
    }, 5000);

    return () => {
      disposed = true;
      clearTimeout(retry);
      clearInterval(staleTimer);
      wsRef.current?.close();
    };
  }, [enabled]);

  return { connected, stale, snapshot };
}
