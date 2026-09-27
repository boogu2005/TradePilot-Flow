import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 定时轮询 Hook：按 intervalMs 周期拉取数据。
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs = 5000,
  enabled = true,
) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const result = await fetcherRef.current();
      setData(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "请求失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    refresh();
    const timer = setInterval(refresh, intervalMs);
    return () => clearInterval(timer);
  }, [enabled, intervalMs, refresh]);

  return { data, loading, error, refresh };
}
